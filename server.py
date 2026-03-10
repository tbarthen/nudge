"""Nudge Flask server — REST API for reminders, completed items, config, and sync."""

import uuid
from datetime import datetime

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS

from data import (
    MAX_REMINDERS,
    clamp_timestamp, data_lock, is_safe_id, safe_int, sanitize_item,
    load_data, save_data,
    prune_completed, track_deletion,
)
import pairing
from pairing import pairing_bp

# ── App setup ────────────────────────────────────────────────────────

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
CORS(app, resources={r"/api/*": {"origins": [
    "http://localhost:*",
    "http://127.0.0.1:*",
    "https://tbarthen.github.io",
]}})

app.register_blueprint(pairing_bp)


@app.errorhandler(Exception)
def handle_exception(e):
    """Return JSON 500 for unhandled errors."""
    import traceback
    traceback.print_exc()
    return jsonify({"error": "internal server error"}), 500


# ── Web UI ───────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── Config API ───────────────────────────────────────────────────────

_CONFIG_VALIDATORS = {
    "popup_interval_minutes": lambda v: isinstance(v, (int, float)) and v >= 1,
    "server_port": lambda v: isinstance(v, int) and 1024 <= v <= 65535,
    "start_with_windows": lambda v: isinstance(v, bool),
    "completed_retention_days": lambda v: isinstance(v, (int, float)) and v >= 1,
    "auto_refresh_seconds": lambda v: isinstance(v, (int, float)) and v >= 10,
}

_CONFIG_INT_KEYS = {"popup_interval_minutes", "completed_retention_days", "auto_refresh_seconds"}


@app.route("/api/config", methods=["GET"])
def get_config():
    data = load_data()
    return jsonify(data["config"])


@app.route("/api/config", methods=["PUT"])
def put_config():
    updates = request.get_json(silent=True)
    if not updates:
        return jsonify({"error": "invalid JSON body"}), 400
    with data_lock:
        data = load_data()
        for key, val in updates.items():
            validator = _CONFIG_VALIDATORS.get(key)
            if not validator:
                continue
            if not validator(val):
                return jsonify({"error": f"{key} has invalid value"}), 400
            if key in _CONFIG_INT_KEYS:
                val = int(val)
            data["config"][key] = val
        save_data(data)
    return jsonify(data["config"])


# ── Reminders API ────────────────────────────────────────────────────

@app.route("/api/reminders", methods=["GET"])
def get_reminders():
    data = load_data()
    reminders = sorted(data["reminders"], key=lambda r: -r.get("order", 0))
    return jsonify(reminders)


@app.route("/api/reminders", methods=["POST"])
def add_reminder():
    body = request.get_json(silent=True)
    if not body or not isinstance(body, dict):
        return jsonify({"error": "invalid JSON body"}), 400
    text = str(body.get("text", "")).strip()[:500]
    if not text:
        return jsonify({"error": "text is required"}), 400
    pinned = body.get("pinned", False)
    if not isinstance(pinned, bool):
        pinned = False
    client_id = str(body.get("id", "")).strip()[:100] if body.get("id") else ""
    if client_id and not is_safe_id(client_id):
        return jsonify({"error": "invalid id format"}), 400
    client_created = clamp_timestamp(body.get("created_at"))
    with data_lock:
        data = load_data()
        if len(data["reminders"]) >= MAX_REMINDERS:
            return jsonify({"error": f"max {MAX_REMINDERS} reminders"}), 400
        if client_id and any(r["id"] == client_id for r in data["reminders"]):
            return jsonify({"error": "duplicate id"}), 409
        max_order = max((r.get("order", 0) for r in data["reminders"]), default=-1)
        reminder = {
            "id": client_id or str(uuid.uuid4()),
            "text": text,
            "created_at": client_created or datetime.now().isoformat(),
            "completed_at": None,
            "pinned": pinned,
            "order": safe_int(body.get("order"), max_order + 1),
        }
        data["reminders"].append(reminder)
        save_data(data)
    return jsonify(reminder), 201


@app.route("/api/reminders/<reminder_id>", methods=["PUT"])
def edit_reminder(reminder_id):
    if not is_safe_id(reminder_id):
        return jsonify({"error": "invalid id"}), 400
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "invalid JSON body"}), 400
    with data_lock:
        data = load_data()
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
                if "order" in body:
                    order = safe_int(body["order"], None)
                    if order is not None:
                        r["order"] = order
                        changed = True
                if "deletion_flagged" in body and isinstance(body["deletion_flagged"], bool):
                    r["deletion_flagged"] = body["deletion_flagged"]
                    changed = True
                if changed:
                    r["updated_at"] = datetime.now().isoformat()
                    save_data(data)
                return jsonify(r)
    return jsonify({"error": "not found"}), 404


@app.route("/api/reminders/reorder", methods=["POST"])
def reorder_reminders():
    body = request.get_json(silent=True)
    if not body or not isinstance(body, list):
        return jsonify({"error": "expected JSON array of {id, order}"}), 400
    if len(body) > MAX_REMINDERS:
        return jsonify({"error": "too many items"}), 400
    with data_lock:
        data = load_data()
        order_map = {}
        for item in body:
            if isinstance(item, dict) and "id" in item and "order" in item:
                item_id = str(item["id"])
                if not is_safe_id(item_id):
                    continue
                order_map[item_id] = safe_int(item["order"])
        now = datetime.now().isoformat()
        for r in data["reminders"]:
            if r["id"] in order_map:
                r["order"] = order_map[r["id"]]
                r["updated_at"] = now
        save_data(data)
    reminders = sorted(data["reminders"], key=lambda r: -r.get("order", 0))
    return jsonify(reminders)


@app.route("/api/reminders/<reminder_id>", methods=["DELETE"])
def delete_reminder(reminder_id):
    if not is_safe_id(reminder_id):
        return jsonify({"error": "invalid id"}), 400
    with data_lock:
        data = load_data()
        original_len = len(data["reminders"])
        data["reminders"] = [r for r in data["reminders"] if r["id"] != reminder_id]
        if len(data["reminders"]) == original_len:
            return jsonify({"error": "not found"}), 404
        track_deletion(data, reminder_id)
        save_data(data)
    return jsonify({"ok": True})


@app.route("/api/reminders/batch-delete", methods=["POST"])
def batch_delete():
    body = request.get_json(silent=True)
    if not body or not isinstance(body, list):
        return jsonify({"error": "expected JSON array of IDs"}), 400
    if len(body) > MAX_REMINDERS:
        return jsonify({"error": "too many items"}), 400
    ids = set(str(i) for i in body if isinstance(i, str) and is_safe_id(str(i)))
    with data_lock:
        data = load_data()
        before = len(data["reminders"])
        data["reminders"] = [r for r in data["reminders"] if r["id"] not in ids]
        deleted = before - len(data["reminders"])
        track_deletion(data, *ids)
        save_data(data)
    return jsonify({"deleted": deleted})


@app.route("/api/reminders/batch-complete", methods=["POST"])
def batch_complete():
    body = request.get_json(silent=True)
    if not body or not isinstance(body, list):
        return jsonify({"error": "expected JSON array of IDs"}), 400
    if len(body) > MAX_REMINDERS:
        return jsonify({"error": "too many items"}), 400
    ids = set(str(i) for i in body if isinstance(i, str) and is_safe_id(str(i)))
    with data_lock:
        data = load_data()
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
        prune_completed(data)
        save_data(data)
    return jsonify({"completed": len(completed)})


@app.route("/api/reminders/<reminder_id>/complete", methods=["PATCH"])
def complete_reminder(reminder_id):
    if not is_safe_id(reminder_id):
        return jsonify({"error": "invalid id"}), 400
    with data_lock:
        data = load_data()
        found = None
        for i, r in enumerate(data["reminders"]):
            if r["id"] == reminder_id:
                found = data["reminders"].pop(i)
                break
        if not found:
            return jsonify({"error": "not found"}), 404
        found["completed_at"] = datetime.now().isoformat()
        data["completed"].append(found)
        prune_completed(data)
        save_data(data)
    return jsonify(found)


# ── Completed API ────────────────────────────────────────────────────

@app.route("/api/completed/<completed_id>/uncomplete", methods=["PATCH"])
def uncomplete_reminder(completed_id):
    if not is_safe_id(completed_id):
        return jsonify({"error": "invalid id"}), 400
    with data_lock:
        data = load_data()
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
        save_data(data)
    return jsonify(found)


@app.route("/api/completed", methods=["GET"])
def get_completed():
    with data_lock:
        data = load_data()
        if prune_completed(data):
            save_data(data)
    completed = sorted(data["completed"], key=lambda c: c.get("completed_at") or "", reverse=True)
    return jsonify(completed)


@app.route("/api/completed/<completed_id>", methods=["DELETE"])
def delete_completed(completed_id):
    if not is_safe_id(completed_id):
        return jsonify({"error": "invalid id"}), 400
    with data_lock:
        data = load_data()
        original_len = len(data["completed"])
        data["completed"] = [c for c in data["completed"] if c["id"] != completed_id]
        if len(data["completed"]) == original_len:
            return jsonify({"error": "not found"}), 404
        track_deletion(data, completed_id)
        save_data(data)
    return jsonify({"ok": True})


# ── Sync API ─────────────────────────────────────────────────────────

@app.route("/api/sync", methods=["POST"])
def sync_data():
    """Merge phone state with server state.

    Conflict resolution:
    - Completed always wins over active
    - Latest timestamp wins for edits
    - Deletion only wins if item is completed everywhere
    - New items from either side are kept
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "invalid JSON body"}), 400

    device_id = str(body.get("device_id", ""))
    if device_id not in pairing.paired_devices:
        return jsonify({"error": "device not paired"}), 403

    phone_reminders = body.get("reminders", [])
    phone_completed = body.get("completed", [])

    if not isinstance(phone_reminders, list) or not isinstance(phone_completed, list):
        return jsonify({"error": "reminders and completed must be arrays"}), 400
    if len(phone_reminders) > MAX_REMINDERS * 2 or len(phone_completed) > MAX_REMINDERS * 2:
        return jsonify({"error": "payload too large"}), 400

    for item in phone_reminders + phone_completed:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return jsonify({"error": "each item must have a string id"}), 400
        if not is_safe_id(item["id"]):
            return jsonify({"error": "invalid item id format"}), 400

    phone_reminders = [sanitize_item(r) for r in phone_reminders]
    phone_completed = [sanitize_item(c, allow_completed=True) for c in phone_completed]

    with data_lock:
        data = load_data()
        server_reminders = data["reminders"]
        server_completed = data["completed"]
        deleted_ids = set(data.get("deleted_ids", []))

        s_rem = {r["id"]: r for r in server_reminders}
        s_comp = {c["id"]: c for c in server_completed}
        p_rem = {r["id"]: r for r in phone_reminders}
        p_comp = {c["id"]: c for c in phone_completed}

        all_ids = set(s_rem) | set(s_comp) | set(p_rem) | set(p_comp)

        merged_reminders = []
        merged_completed = []

        for item_id in all_ids:
            if item_id in deleted_ids:
                continue

            in_s_comp = item_id in s_comp
            in_p_comp = item_id in p_comp

            # Completed on either side -> completed
            if in_s_comp or in_p_comp:
                if in_s_comp and in_p_comp:
                    if (p_comp[item_id].get("completed_at") or "") > (s_comp[item_id].get("completed_at") or ""):
                        comp_item = p_comp[item_id]
                    else:
                        comp_item = s_comp[item_id]
                elif in_p_comp:
                    comp_item = p_comp[item_id]
                else:
                    comp_item = s_comp[item_id]
                merged_completed.append(comp_item)
                continue

            in_s_rem = item_id in s_rem
            in_p_rem = item_id in p_rem

            # Both active -> latest wins
            if in_s_rem and in_p_rem:
                s = s_rem[item_id]
                p = p_rem[item_id]
                s_time = s.get("updated_at") or s.get("created_at") or ""
                p_time = p.get("updated_at") or p.get("created_at") or ""
                winner = dict(p) if p_time > s_time else dict(s)
                # Hidden is sticky
                if s.get("hidden") or p.get("hidden"):
                    winner["hidden"] = True
                    winner["hidden_at"] = max(s.get("hidden_at") or "", p.get("hidden_at") or "") or None
                merged_reminders.append(winner)
                continue

            # Only on one side
            if in_s_rem:
                merged_reminders.append(s_rem[item_id])
            elif in_p_rem:
                merged_reminders.append(p_rem[item_id])

        if len(merged_reminders) > MAX_REMINDERS:
            merged_reminders.sort(key=lambda r: r.get("order", 0), reverse=True)
            merged_reminders = merged_reminders[:MAX_REMINDERS]

        data["reminders"] = merged_reminders
        data["completed"] = merged_completed
        prune_completed(data)
        save_data(data)

    return jsonify({
        "reminders": merged_reminders,
        "completed": data["completed"],
        "deleted_ids": list(deleted_ids),
    })
