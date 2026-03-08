import json
import os
import secrets
import tempfile
import threading
import uuid
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminders.json")
_data_lock = threading.Lock()

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024  # 256 KB max request body (sync payloads can be large)
CORS(app, resources={r"/api/*": {"origins": "*"}})

MAX_REMINDERS = 200
DEFAULT_RETENTION_DAYS = 60
_SAFE_ID_RE = None

def _is_safe_id(val):
    """Check that an ID contains only safe characters (alphanumeric, hyphens)."""
    global _SAFE_ID_RE
    if _SAFE_ID_RE is None:
        import re
        _SAFE_ID_RE = re.compile(r'^[a-zA-Z0-9\-]+$')
    return isinstance(val, str) and 1 <= len(val) <= 100 and _SAFE_ID_RE.match(val)


def _sanitize_item(item, allow_completed=False):
    """Strip an item down to only known safe fields. Prevents payload bloat and injection."""
    safe = {
        "id": str(item.get("id", "")),
        "text": str(item.get("text", "")).strip()[:500],
        "created_at": str(item.get("created_at", ""))[:30],
        "completed_at": None,
        "pinned": bool(item.get("pinned", False)),
        "order": int(item["order"]) if isinstance(item.get("order"), (int, float)) else 0,
    }
    if allow_completed and item.get("completed_at"):
        safe["completed_at"] = str(item["completed_at"])[:30]
    if isinstance(item.get("updated_at"), str):
        safe["updated_at"] = item["updated_at"][:30]
    if isinstance(item.get("deletion_flagged"), bool):
        safe["deletion_flagged"] = item["deletion_flagged"]
    return safe


def _load_data():
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        # Corrupted or missing — return safe defaults
        return {"config": {"popup_interval_minutes": 60, "server_port": 5123, "start_with_windows": False, "completed_retention_days": 60}, "reminders": [], "completed": []}


def _get_retention_days(data=None):
    if data is None:
        data = _load_data()
    val = data.get("config", {}).get("completed_retention_days", DEFAULT_RETENTION_DAYS)
    return max(1, int(val)) if isinstance(val, (int, float)) else DEFAULT_RETENTION_DAYS


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
                "completed_retention_days": 60,
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
    allowed = {"popup_interval_minutes", "server_port", "start_with_windows", "completed_retention_days"}
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
            elif key == "completed_retention_days":
                if not isinstance(val, (int, float)) or val < 1:
                    return jsonify({"error": "completed_retention_days must be >= 1"}), 400
                val = int(val)
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
    # Accept client-provided id to stay in sync with IDB
    client_id = str(body.get("id", "")).strip()[:100] if body.get("id") else ""
    if client_id and not _is_safe_id(client_id):
        return jsonify({"error": "invalid id format"}), 400
    # Validate created_at if provided
    client_created = body.get("created_at")
    if client_created:
        client_created = str(client_created)[:30]
    with _data_lock:
        data = _load_data()
        if len(data["reminders"]) >= MAX_REMINDERS:
            return jsonify({"error": f"max {MAX_REMINDERS} reminders"}), 400
        # Reject duplicate IDs
        if client_id and any(r["id"] == client_id for r in data["reminders"]):
            return jsonify({"error": "duplicate id"}), 409
        max_order = max((r.get("order", 0) for r in data["reminders"]), default=-1)
        reminder = {
            "id": client_id or str(uuid.uuid4()),
            "text": text,
            "created_at": client_created or datetime.now().isoformat(),
            "completed_at": None,
            "pinned": pinned,
            "order": body.get("order", max_order + 1) if isinstance(body.get("order"), (int, float)) else max_order + 1,
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
                changed = False
                if "text" in body:
                    text = str(body["text"]).strip()[:500]
                    if text:
                        r["text"] = text
                        changed = True
                if "pinned" in body and isinstance(body["pinned"], bool):
                    r["pinned"] = body["pinned"]
                    changed = True
                if "order" in body and isinstance(body["order"], (int, float)):
                    r["order"] = int(body["order"])
                    changed = True
                if "deletion_flagged" in body and isinstance(body["deletion_flagged"], bool):
                    r["deletion_flagged"] = body["deletion_flagged"]
                    changed = True
                if changed:
                    r["updated_at"] = datetime.now().isoformat()
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


@app.route("/api/reminders/batch-complete", methods=["POST"])
def batch_complete():
    body = request.get_json(silent=True)
    if not body or not isinstance(body, list):
        return jsonify({"error": "expected JSON array of IDs"}), 400
    if len(body) > MAX_REMINDERS:
        return jsonify({"error": "too many items"}), 400
    ids = set(str(i) for i in body if isinstance(i, str))
    with _data_lock:
        data = _load_data()
        completed = []
        remaining = []
        for r in data["reminders"]:
            if r["id"] in ids:
                r["completed_at"] = datetime.now().isoformat()
                data["completed"].append(r)
                completed.append(r)
            else:
                remaining.append(r)
        data["reminders"] = remaining
        retention = _get_retention_days(data)
        cutoff = (datetime.now() - timedelta(days=retention)).isoformat()
        data["completed"] = [c for c in data["completed"] if c.get("completed_at", "") >= cutoff]
        _save_data(data)
    return jsonify({"completed": len(completed)})


@app.route("/api/reminders/batch-delete", methods=["POST"])
def batch_delete():
    body = request.get_json(silent=True)
    if not body or not isinstance(body, list):
        return jsonify({"error": "expected JSON array of IDs"}), 400
    if len(body) > MAX_REMINDERS:
        return jsonify({"error": "too many items"}), 400
    ids = set(str(i) for i in body if isinstance(i, str))
    with _data_lock:
        data = _load_data()
        before = len(data["reminders"])
        data["reminders"] = [r for r in data["reminders"] if r["id"] not in ids]
        deleted = before - len(data["reminders"])
        _save_data(data)
    return jsonify({"deleted": deleted})


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
        # Prune completed older than retention period
        retention = _get_retention_days(data)
        cutoff = (datetime.now() - timedelta(days=retention)).isoformat()
        data["completed"] = [c for c in data["completed"] if c.get("completed_at", "") >= cutoff]
        _save_data(data)
    return jsonify(found)


@app.route("/api/completed/<completed_id>/uncomplete", methods=["PATCH"])
def uncomplete_reminder(completed_id):
    with _data_lock:
        data = _load_data()
        found = None
        for i, c in enumerate(data["completed"]):
            if c["id"] == completed_id:
                found = data["completed"].pop(i)
                break
        if not found:
            return jsonify({"error": "not found"}), 404
        if len(data["reminders"]) >= MAX_REMINDERS:
            return jsonify({"error": f"max {MAX_REMINDERS} reminders"}), 400
        found["completed_at"] = None
        data["reminders"].append(found)
        _save_data(data)
    return jsonify(found)


@app.route("/api/completed", methods=["GET"])
def get_completed():
    with _data_lock:
        data = _load_data()
        retention = _get_retention_days(data)
        cutoff = (datetime.now() - timedelta(days=retention)).isoformat()
        before = len(data["completed"])
        data["completed"] = [c for c in data["completed"] if c.get("completed_at", "") >= cutoff]
        if len(data["completed"]) < before:
            _save_data(data)
    completed = sorted(data["completed"], key=lambda c: c.get("completed_at", ""), reverse=True)
    return jsonify(completed)


# --- Pairing API ---

_pairing_lock = threading.Lock()
_pairing_code = None        # {"code": "123456", "expires": datetime}
_pair_attempts = 0          # Failed validation attempts (rate limiting)
_pair_attempt_reset = None  # When to reset the attempt counter

PAIR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".paired_devices.json")
MAX_PAIR_ATTEMPTS = 5       # Max failed attempts per 5-minute window
MAX_PAIRED_DEVICES = 10     # Reasonable cap for a personal reminder app


def _load_paired_devices():
    try:
        with open(PAIR_FILE, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                return set(str(d) for d in data if isinstance(d, str) and len(d) <= 100)
            return set()
    except (json.JSONDecodeError, FileNotFoundError):
        return set()


def _save_paired_devices(devices):
    dir_name = os.path.dirname(PAIR_FILE)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(list(devices), f)
        os.replace(tmp_path, PAIR_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


_paired_devices = _load_paired_devices()


@app.route("/api/pair/generate", methods=["POST"])
def generate_pairing_code():
    """Generate a 6-digit pairing code valid for 5 minutes."""
    global _pairing_code
    with _pairing_lock:
        code = f"{secrets.randbelow(1000000):06d}"
        expires = (datetime.now() + timedelta(minutes=5)).isoformat()
        _pairing_code = {
            "code": code,
            "expires": expires,
        }
    return jsonify({"code": code, "expires": expires})


@app.route("/api/pair/validate", methods=["POST"])
def validate_pairing_code():
    """Validate a pairing code and register the device."""
    global _pairing_code, _pair_attempts, _pair_attempt_reset
    body = request.get_json(silent=True)
    if not body or "code" not in body:
        return jsonify({"error": "code required"}), 400

    code = str(body.get("code", ""))
    if not code or len(code) > 10:
        return jsonify({"error": "invalid code format"}), 400

    device_id = str(body.get("device_id", ""))
    if not device_id or len(device_id) > 100:
        device_id = str(uuid.uuid4())

    with _pairing_lock:
        # Rate limiting
        now = datetime.now()
        if _pair_attempt_reset and now.isoformat() > _pair_attempt_reset:
            _pair_attempts = 0
            _pair_attempt_reset = None
        if _pair_attempts >= MAX_PAIR_ATTEMPTS:
            return jsonify({"error": "too many attempts, try again later"}), 429

        if not _pairing_code:
            _pair_attempts += 1
            if not _pair_attempt_reset:
                _pair_attempt_reset = (now + timedelta(minutes=5)).isoformat()
            return jsonify({"error": "no active pairing code"}), 400
        if now.isoformat() > _pairing_code["expires"]:
            _pairing_code = None
            return jsonify({"error": "pairing code expired"}), 400
        if code != _pairing_code["code"]:
            _pair_attempts += 1
            if not _pair_attempt_reset:
                _pair_attempt_reset = (now + timedelta(minutes=5)).isoformat()
            return jsonify({"error": "invalid code"}), 400

        if len(_paired_devices) >= MAX_PAIRED_DEVICES and device_id not in _paired_devices:
            return jsonify({"error": "too many paired devices, unpair one first"}), 400
        _paired_devices.add(device_id)
        _save_paired_devices(_paired_devices)
        _pairing_code = None  # One-time use
        _pair_attempts = 0    # Reset on success

    return jsonify({"paired": True, "device_id": device_id})


@app.route("/api/pair/status", methods=["GET"])
def pairing_status():
    """Check if a device is paired (by device_id query param)."""
    device_id = request.args.get("device_id", "")
    return jsonify({"paired": device_id in _paired_devices})


@app.route("/api/pair/unpair", methods=["POST"])
def unpair_device():
    """Remove a device from the paired set."""
    body = request.get_json(silent=True)
    device_id = str(body.get("device_id", "")) if body else ""
    with _pairing_lock:
        _paired_devices.discard(device_id)
        _save_paired_devices(_paired_devices)
    return jsonify({"unpaired": True})


# --- Sync API ---

@app.route("/api/sync", methods=["POST"])
def sync_data():
    """
    Sync endpoint: receives the phone's full state, merges with server state,
    returns the merged result.

    Conflict resolution rules:
    - Completed always wins over active (if item is completed on either side)
    - Latest timestamp wins for edits (text, pinned, order)
    - Deletion only wins if item is completed everywhere
    - New items from either side are kept
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "invalid JSON body"}), 400

    device_id = str(body.get("device_id", ""))
    if device_id not in _paired_devices:
        return jsonify({"error": "device not paired"}), 403

    phone_reminders = body.get("reminders", [])
    phone_completed = body.get("completed", [])

    # Validate payload structure
    if not isinstance(phone_reminders, list) or not isinstance(phone_completed, list):
        return jsonify({"error": "reminders and completed must be arrays"}), 400
    if len(phone_reminders) > MAX_REMINDERS * 2 or len(phone_completed) > MAX_REMINDERS * 2:
        return jsonify({"error": "payload too large"}), 400

    # Validate and sanitize each item
    for item in phone_reminders + phone_completed:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return jsonify({"error": "each item must have a string id"}), 400
        if not _is_safe_id(item["id"]):
            return jsonify({"error": "invalid item id format"}), 400

    # Strip items to known safe fields only
    phone_reminders = [_sanitize_item(r) for r in phone_reminders]
    phone_completed = [_sanitize_item(c, allow_completed=True) for c in phone_completed]

    with _data_lock:
        data = _load_data()
        server_reminders = data["reminders"]
        server_completed = data["completed"]

        # Build lookup maps by ID
        s_rem = {r["id"]: r for r in server_reminders}
        s_comp = {c["id"]: c for c in server_completed}
        p_rem = {r["id"]: r for r in phone_reminders}
        p_comp = {c["id"]: c for c in phone_completed}

        all_ids = set(s_rem) | set(s_comp) | set(p_rem) | set(p_comp)

        merged_reminders = []
        merged_completed = []

        for item_id in all_ids:
            in_s_rem = item_id in s_rem
            in_s_comp = item_id in s_comp
            in_p_rem = item_id in p_rem
            in_p_comp = item_id in p_comp

            # If completed on either side, it's completed
            if in_s_comp or in_p_comp:
                # Use the completed version with the latest completed_at
                comp_item = s_comp.get(item_id) or p_comp.get(item_id)
                if in_s_comp and in_p_comp:
                    # Both completed — keep the one with latest completed_at
                    if (p_comp[item_id].get("completed_at", "") >
                            s_comp[item_id].get("completed_at", "")):
                        comp_item = p_comp[item_id]
                    else:
                        comp_item = s_comp[item_id]
                elif in_p_comp:
                    comp_item = p_comp[item_id]
                merged_completed.append(comp_item)
                continue

            # Both have it as active — merge fields, latest wins
            if in_s_rem and in_p_rem:
                s = s_rem[item_id]
                p = p_rem[item_id]
                # Use created_at as tiebreaker proxy for "last modified"
                # In future, add an updated_at field
                s_time = s.get("updated_at", s.get("created_at", ""))
                p_time = p.get("updated_at", p.get("created_at", ""))
                if p_time > s_time:
                    merged_reminders.append(p)
                else:
                    merged_reminders.append(s)
                continue

            # Only on one side as active — keep it
            if in_s_rem:
                merged_reminders.append(s_rem[item_id])
            elif in_p_rem:
                merged_reminders.append(p_rem[item_id])

        # Cap active reminders to prevent unbounded growth
        if len(merged_reminders) > MAX_REMINDERS:
            merged_reminders.sort(key=lambda r: r.get("order", 0), reverse=True)
            merged_reminders = merged_reminders[:MAX_REMINDERS]

        # Prune completed by retention
        retention = _get_retention_days(data)
        cutoff = (datetime.now() - timedelta(days=retention)).isoformat()
        merged_completed = [c for c in merged_completed if c.get("completed_at", "") >= cutoff]

        data["reminders"] = merged_reminders
        data["completed"] = merged_completed
        _save_data(data)

    return jsonify({
        "reminders": merged_reminders,
        "completed": merged_completed,
    })
