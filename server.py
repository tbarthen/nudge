import json
import os
import secrets
import socket
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
CORS(app, resources={r"/api/*": {"origins": [
    "http://localhost:*",
    "http://127.0.0.1:*",
    "https://tbarthen.github.io",
]}})


@app.errorhandler(Exception)
def handle_exception(e):
    """Return JSON 500 for unhandled errors (e.g. disk write failures)."""
    import traceback
    traceback.print_exc()
    return jsonify({"error": "internal server error"}), 500


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


def _clamp_timestamp(ts):
    """Validate and clamp an ISO timestamp string. Reject far-future values."""
    if not isinstance(ts, str) or len(ts) > 30:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        # Clamp to at most 1 day in the future (clock skew tolerance)
        max_dt = datetime.now() + timedelta(days=1)
        if dt > max_dt:
            return max_dt.isoformat()
        return ts
    except (ValueError, TypeError):
        return None


def _safe_int(val, default=0):
    """Convert a value to int, rejecting NaN/Infinity."""
    if isinstance(val, (int, float)) and val == val and val not in (float('inf'), float('-inf')):
        return int(val)
    return default


def _sanitize_item(item, allow_completed=False):
    """Strip an item down to only known safe fields. Prevents payload bloat and injection."""
    safe = {
        "id": str(item.get("id", "")),
        "text": str(item.get("text", "")).strip()[:500],
        "created_at": _clamp_timestamp(item.get("created_at")) or datetime.now().isoformat(),
        "completed_at": None,
        "pinned": bool(item.get("pinned", False)),
        "order": _safe_int(item.get("order"), 0),
    }
    if allow_completed and item.get("completed_at"):
        safe["completed_at"] = _clamp_timestamp(item["completed_at"])
    ts = _clamp_timestamp(item.get("updated_at"))
    if ts:
        safe["updated_at"] = ts
    if isinstance(item.get("deletion_flagged"), bool):
        safe["deletion_flagged"] = item["deletion_flagged"]
    if isinstance(item.get("hidden"), bool):
        safe["hidden"] = item["hidden"]
    ht = _clamp_timestamp(item.get("hidden_at"))
    if ht:
        safe["hidden_at"] = ht
    return safe


def _load_data():
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        # Corrupted or missing — return safe defaults
        return {"config": {"popup_interval_minutes": 60, "server_port": 5123, "start_with_windows": False, "completed_retention_days": 60, "auto_refresh_seconds": 90}, "reminders": [], "completed": []}


def _get_retention_days(data=None):
    if data is None:
        data = _load_data()
    val = data.get("config", {}).get("completed_retention_days", DEFAULT_RETENTION_DAYS)
    return max(1, int(val)) if isinstance(val, (int, float)) else DEFAULT_RETENTION_DAYS


MAX_DELETED_IDS = 500  # Cap tracked deletions to prevent unbounded growth


def _track_deletion(data, *item_ids):
    """Record deleted item IDs so sync won't resurrect them from the phone."""
    if "deleted_ids" not in data:
        data["deleted_ids"] = []
    existing = set(data["deleted_ids"])
    for item_id in item_ids:
        if _is_safe_id(item_id) and item_id not in existing:
            data["deleted_ids"].append(item_id)
            existing.add(item_id)
    # Cap: keep only the most recent deletions
    if len(data["deleted_ids"]) > MAX_DELETED_IDS:
        data["deleted_ids"] = data["deleted_ids"][-MAX_DELETED_IDS:]


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
                "auto_refresh_seconds": 90,
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
    allowed = {"popup_interval_minutes", "server_port", "start_with_windows", "completed_retention_days", "auto_refresh_seconds"}
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
            elif key == "auto_refresh_seconds":
                if not isinstance(val, (int, float)) or val < 10:
                    return jsonify({"error": "auto_refresh_seconds must be >= 10"}), 400
                val = int(val)
            data["config"][key] = val
        _save_data(data)
    return jsonify(data["config"])


# --- Reminders API ---

@app.route("/api/reminders", methods=["GET"])
def get_reminders():
    data = _load_data()
    reminders = sorted(data["reminders"], key=lambda r: -r.get("order", 0))
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
            "order": _safe_int(body.get("order"), max_order + 1),
        }
        data["reminders"].append(reminder)
        _save_data(data)
    return jsonify(reminder), 201


@app.route("/api/reminders/<reminder_id>", methods=["PUT"])
def edit_reminder(reminder_id):
    if not _is_safe_id(reminder_id):
        return jsonify({"error": "invalid id"}), 400
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
                if "order" in body and isinstance(body["order"], (int, float)) and body["order"] == body["order"] and body["order"] not in (float('inf'), float('-inf')):
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
            if isinstance(item, dict) and "id" in item and "order" in item:
                order_val = _safe_int(item.get("order"))
                item_id = str(item["id"])
                if not _is_safe_id(item_id):
                    continue
                order_map[item_id] = order_val
        for r in data["reminders"]:
            if r["id"] in order_map:
                r["order"] = order_map[r["id"]]
        _save_data(data)
    reminders = sorted(data["reminders"], key=lambda r: -r.get("order", 0))
    return jsonify(reminders)


@app.route("/api/reminders/<reminder_id>", methods=["DELETE"])
def delete_reminder(reminder_id):
    if not _is_safe_id(reminder_id):
        return jsonify({"error": "invalid id"}), 400
    with _data_lock:
        data = _load_data()
        original_len = len(data["reminders"])
        data["reminders"] = [r for r in data["reminders"] if r["id"] != reminder_id]
        if len(data["reminders"]) == original_len:
            return jsonify({"error": "not found"}), 404
        _track_deletion(data, reminder_id)
        _save_data(data)
    return jsonify({"ok": True})


@app.route("/api/reminders/batch-complete", methods=["POST"])
def batch_complete():
    body = request.get_json(silent=True)
    if not body or not isinstance(body, list):
        return jsonify({"error": "expected JSON array of IDs"}), 400
    if len(body) > MAX_REMINDERS:
        return jsonify({"error": "too many items"}), 400
    ids = set(str(i) for i in body if isinstance(i, str) and _is_safe_id(str(i)))
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
    ids = set(str(i) for i in body if isinstance(i, str) and _is_safe_id(str(i)))
    with _data_lock:
        data = _load_data()
        before = len(data["reminders"])
        data["reminders"] = [r for r in data["reminders"] if r["id"] not in ids]
        deleted = before - len(data["reminders"])
        _track_deletion(data, *ids)
        _save_data(data)
    return jsonify({"deleted": deleted})


@app.route("/api/reminders/<reminder_id>/complete", methods=["PATCH"])
def complete_reminder(reminder_id):
    if not _is_safe_id(reminder_id):
        return jsonify({"error": "invalid id"}), 400
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
    if not _is_safe_id(completed_id):
        return jsonify({"error": "invalid id"}), 400
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


@app.route("/api/completed/<completed_id>", methods=["DELETE"])
def delete_completed(completed_id):
    if not _is_safe_id(completed_id):
        return jsonify({"error": "invalid id"}), 400
    with _data_lock:
        data = _load_data()
        original_len = len(data["completed"])
        data["completed"] = [c for c in data["completed"] if c["id"] != completed_id]
        if len(data["completed"]) == original_len:
            return jsonify({"error": "not found"}), 404
        _track_deletion(data, completed_id)
        _save_data(data)
    return jsonify({"ok": True})


# --- Pairing API ---

_pairing_lock = threading.Lock()
_pairing_code = None        # {"code": "123456", "expires": datetime}
_pair_attempts = 0          # Failed validation attempts (rate limiting)
_pair_attempt_reset = None  # datetime when to reset the attempt counter

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
        expires_dt = datetime.now() + timedelta(minutes=5)
        _pairing_code = {
            "code": code,
            "expires": expires_dt,
        }
        expires = expires_dt.isoformat()
    # Include local IP so phone knows where to connect
    local_ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        pass
    return jsonify({"code": code, "expires": expires, "ip": local_ip})


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
        # Rate limiting (proper datetime comparison)
        now = datetime.now()
        if _pair_attempt_reset and now >= _pair_attempt_reset:
            _pair_attempts = 0
            _pair_attempt_reset = None
        if _pair_attempts >= MAX_PAIR_ATTEMPTS:
            return jsonify({"error": "too many attempts, try again later"}), 429

        if not _pairing_code:
            _pair_attempts += 1
            if not _pair_attempt_reset:
                _pair_attempt_reset = now + timedelta(minutes=5)
            return jsonify({"error": "no active pairing code"}), 400
        if now >= _pairing_code["expires"]:
            _pairing_code = None
            return jsonify({"error": "pairing code expired"}), 400
        if not secrets.compare_digest(code, _pairing_code["code"]):
            _pair_attempts += 1
            if not _pair_attempt_reset:
                _pair_attempt_reset = now + timedelta(minutes=5)
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
    if not device_id or len(device_id) > 100:
        return jsonify({"paired": False})
    with _pairing_lock:
        paired = device_id in _paired_devices
    return jsonify({"paired": paired})


@app.route("/api/pair/unpair", methods=["POST"])
def unpair_device():
    """Remove a device from the paired set."""
    body = request.get_json(silent=True)
    device_id = str(body.get("device_id", "")) if body else ""
    if not device_id or len(device_id) > 100:
        return jsonify({"error": "invalid device_id"}), 400
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

        # IDs that were explicitly deleted on the server — never resurrect these
        deleted_ids = set(data.get("deleted_ids", []))

        # Build lookup maps by ID
        s_rem = {r["id"]: r for r in server_reminders}
        s_comp = {c["id"]: c for c in server_completed}
        p_rem = {r["id"]: r for r in phone_reminders}
        p_comp = {c["id"]: c for c in phone_completed}

        all_ids = set(s_rem) | set(s_comp) | set(p_rem) | set(p_comp)

        merged_reminders = []
        merged_completed = []

        for item_id in all_ids:
            # Skip items explicitly deleted on the server
            if item_id in deleted_ids:
                continue

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
                    winner = dict(p)
                else:
                    winner = dict(s)
                # hidden is sticky: if either side hid it, keep it hidden
                if s.get("hidden") or p.get("hidden"):
                    winner["hidden"] = True
                    winner["hidden_at"] = max(
                        s.get("hidden_at", ""), p.get("hidden_at", "")
                    ) or None
                merged_reminders.append(winner)
                continue

            # Only on one side as active — keep it (preserve hidden flag)
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
        "deleted_ids": list(deleted_ids),
    })
