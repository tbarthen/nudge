import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminders.json")
_data_lock = threading.Lock()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # 64 KB max request body

MAX_REMINDERS = 200


def _load_data():
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        # Corrupted or missing — return safe defaults
        return {"config": {"popup_interval_minutes": 60, "server_port": 5123, "start_with_windows": False}, "reminders": [], "completed": []}


def _save_data(data):
    # Atomic write: write to temp file then rename
    dir_name = os.path.dirname(DATA_FILE)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, DATA_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def init_data_file():
    """Create reminders.json with defaults if it doesn't exist."""
    if not os.path.exists(DATA_FILE):
        default = {
            "config": {
                "popup_interval_minutes": 60,
                "server_port": 5123,
                "start_with_windows": False,
            },
            "reminders": [],
            "completed": [],
        }
        _save_data(default)


# --- Web UI ---

@app.route("/")
def index():
    return render_template("index.html")


# --- Config API ---

@app.route("/api/config", methods=["GET"])
def get_config():
    data = _load_data()
    return jsonify(data["config"])


@app.route("/api/config", methods=["PUT"])
def put_config():
    updates = request.get_json(silent=True)
    if not updates:
        return jsonify({"error": "invalid JSON body"}), 400
    allowed = {"popup_interval_minutes", "server_port", "start_with_windows"}
    with _data_lock:
        data = _load_data()
        for key in updates:
            if key not in allowed:
                continue
            val = updates[key]
            if key == "popup_interval_minutes":
                if not isinstance(val, (int, float)) or val < 1:
                    return jsonify({"error": "popup_interval_minutes must be >= 1"}), 400
                val = int(val)
            elif key == "server_port":
                if not isinstance(val, int) or not (1024 <= val <= 65535):
                    return jsonify({"error": "server_port must be 1024-65535"}), 400
            elif key == "start_with_windows":
                if not isinstance(val, bool):
                    return jsonify({"error": "start_with_windows must be boolean"}), 400
            data["config"][key] = val
        _save_data(data)
    return jsonify(data["config"])


# --- Reminders API ---

@app.route("/api/reminders", methods=["GET"])
def get_reminders():
    data = _load_data()
    reminders = sorted(data["reminders"], key=lambda r: (not r.get("pinned", False), -r.get("order", 0)))
    return jsonify(reminders)


@app.route("/api/reminders", methods=["POST"])
def add_reminder():
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "invalid JSON body"}), 400
    text = str(body.get("text", "")).strip()[:500]
    if not text:
        return jsonify({"error": "text is required"}), 400
    pinned = body.get("pinned", False)
    if not isinstance(pinned, bool):
        pinned = False
    with _data_lock:
        data = _load_data()
        if len(data["reminders"]) >= MAX_REMINDERS:
            return jsonify({"error": f"max {MAX_REMINDERS} reminders"}), 400
        max_order = max((r.get("order", 0) for r in data["reminders"]), default=-1)
        reminder = {
            "id": str(uuid.uuid4()),
            "text": text,
            "created_at": datetime.now().isoformat(),
            "completed_at": None,
            "pinned": pinned,
            "order": max_order + 1,
        }
        data["reminders"].append(reminder)
        _save_data(data)
    return jsonify(reminder), 201


@app.route("/api/reminders/<reminder_id>", methods=["PUT"])
def edit_reminder(reminder_id):
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "invalid JSON body"}), 400
    with _data_lock:
        data = _load_data()
        for r in data["reminders"]:
            if r["id"] == reminder_id:
                if "text" in body:
                    text = str(body["text"]).strip()[:500]
                    if text:
                        r["text"] = text
                if "pinned" in body and isinstance(body["pinned"], bool):
                    r["pinned"] = body["pinned"]
                if "order" in body and isinstance(body["order"], (int, float)):
                    r["order"] = int(body["order"])
                _save_data(data)
                return jsonify(r)
    return jsonify({"error": "not found"}), 404


@app.route("/api/reminders/reorder", methods=["POST"])
def reorder_reminders():
    body = request.get_json(silent=True)
    if not body or not isinstance(body, list):
        return jsonify({"error": "expected JSON array of {id, order}"}), 400
    if len(body) > MAX_REMINDERS:
        return jsonify({"error": "too many items"}), 400
    with _data_lock:
        data = _load_data()
        order_map = {}
        for item in body:
            if isinstance(item, dict) and "id" in item and "order" in item and isinstance(item["order"], (int, float)):
                order_map[str(item["id"])] = int(item["order"])
        for r in data["reminders"]:
            if r["id"] in order_map:
                r["order"] = order_map[r["id"]]
        _save_data(data)
    reminders = sorted(data["reminders"], key=lambda r: (not r.get("pinned", False), -r.get("order", 0)))
    return jsonify(reminders)


@app.route("/api/reminders/<reminder_id>", methods=["DELETE"])
def delete_reminder(reminder_id):
    with _data_lock:
        data = _load_data()
        original_len = len(data["reminders"])
        data["reminders"] = [r for r in data["reminders"] if r["id"] != reminder_id]
        if len(data["reminders"]) == original_len:
            return jsonify({"error": "not found"}), 404
        _save_data(data)
    return jsonify({"ok": True})


@app.route("/api/reminders/<reminder_id>/complete", methods=["PATCH"])
def complete_reminder(reminder_id):
    with _data_lock:
        data = _load_data()
        found = None
        for i, r in enumerate(data["reminders"]):
            if r["id"] == reminder_id:
                found = data["reminders"].pop(i)
                break
        if not found:
            return jsonify({"error": "not found"}), 404
        found["completed_at"] = datetime.now().isoformat()
        data["completed"].append(found)
        # Prune completed older than 7 days
        cutoff = (datetime.now() - timedelta(days=7)).isoformat()
        data["completed"] = [c for c in data["completed"] if c.get("completed_at", "") >= cutoff]
        _save_data(data)
    return jsonify(found)


@app.route("/api/completed", methods=["GET"])
def get_completed():
    cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    with _data_lock:
        data = _load_data()
        before = len(data["completed"])
        data["completed"] = [c for c in data["completed"] if c.get("completed_at", "") >= cutoff]
        if len(data["completed"]) < before:
            _save_data(data)
    completed = sorted(data["completed"], key=lambda c: c.get("completed_at", ""), reverse=True)
    return jsonify(completed)
