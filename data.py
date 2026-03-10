"""Data layer for Nudge — file I/O, validation, sanitization, and constants.

All persistence logic lives here. Route handlers in server.py call these
functions instead of touching the JSON file directly.
"""

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timedelta

# ── Paths ────────────────────────────────────────────────────────────

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(_BASE_DIR, "reminders.json")
PAIR_FILE = os.path.join(_BASE_DIR, ".paired_devices.json")

# ── Constants ────────────────────────────────────────────────────────

MAX_REMINDERS = 200
MAX_DELETED_IDS = 500
DEFAULT_RETENTION_DAYS = 60

DEFAULT_CONFIG = {
    "popup_interval_minutes": 60,
    "server_port": 5123,
    "start_with_windows": False,
    "completed_retention_days": 60,
    "auto_refresh_seconds": 90,
}

# ── Locking ──────────────────────────────────────────────────────────

data_lock = threading.Lock()

# ── Validation helpers ───────────────────────────────────────────────

_SAFE_ID_RE = re.compile(r'^[a-zA-Z0-9\-]+$')


def is_safe_id(val):
    """Check that an ID contains only safe characters (alphanumeric, hyphens)."""
    return isinstance(val, str) and 1 <= len(val) <= 100 and _SAFE_ID_RE.match(val) is not None


def clamp_timestamp(ts):
    """Validate and clamp an ISO timestamp string. Reject far-future values."""
    if not isinstance(ts, str) or len(ts) > 40:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        # Strip timezone for comparison (phone sends UTC, server uses naive local)
        dt_naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
        max_dt = datetime.now() + timedelta(days=1)
        if dt_naive > max_dt:
            return max_dt.isoformat()
        return ts
    except (ValueError, TypeError):
        return None


def safe_int(val, default=0):
    """Convert a value to int, rejecting NaN/Infinity."""
    if isinstance(val, (int, float)) and val == val and val not in (float('inf'), float('-inf')):
        return int(val)
    return default


def sanitize_item(item, allow_completed=False):
    """Strip an item down to only known safe fields. Prevents payload bloat and injection."""
    safe = {
        "id": str(item.get("id", "")),
        "text": str(item.get("text", "")).strip()[:500],
        "created_at": clamp_timestamp(item.get("created_at")) or datetime.now().isoformat(),
        "completed_at": None,
        "pinned": bool(item.get("pinned", False)),
        "order": safe_int(item.get("order"), 0),
    }
    if allow_completed and item.get("completed_at"):
        safe["completed_at"] = clamp_timestamp(item["completed_at"])
    ts = clamp_timestamp(item.get("updated_at"))
    if ts:
        safe["updated_at"] = ts
    if isinstance(item.get("deletion_flagged"), bool):
        safe["deletion_flagged"] = item["deletion_flagged"]
    if isinstance(item.get("hidden"), bool):
        safe["hidden"] = item["hidden"]
    ht = clamp_timestamp(item.get("hidden_at"))
    if ht:
        safe["hidden_at"] = ht
    return safe


# ── Atomic file I/O ──────────────────────────────────────────────────

def _atomic_write_json(filepath, obj):
    """Write JSON to a file atomically (temp file + rename)."""
    dir_name = os.path.dirname(filepath)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
        os.replace(tmp_path, filepath)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ── Data file ────────────────────────────────────────────────────────

def load_data(data_file=None):
    """Load reminders.json, filling in any missing keys with defaults."""
    path = data_file or DATA_FILE
    defaults = {
        "config": dict(DEFAULT_CONFIG),
        "reminders": [],
        "completed": [],
    }
    try:
        with open(path, "r") as f:
            data = json.load(f)
        for key in defaults:
            if key not in data:
                data[key] = defaults[key]
            elif key in ("reminders", "completed") and not isinstance(data[key], list):
                data[key] = defaults[key]
        return data
    except (json.JSONDecodeError, FileNotFoundError):
        return defaults


def save_data(data, data_file=None):
    """Atomically save data to reminders.json."""
    _atomic_write_json(data_file or DATA_FILE, data)


def init_data_file(data_file=None):
    """Create reminders.json with defaults if it doesn't exist."""
    path = data_file or DATA_FILE
    if not os.path.exists(path):
        save_data({
            "config": dict(DEFAULT_CONFIG),
            "reminders": [],
            "completed": [],
        }, path)


def get_retention_days(data=None, data_file=None):
    """Get the completed retention period in days from config."""
    if data is None:
        data = load_data(data_file)
    val = data.get("config", {}).get("completed_retention_days", DEFAULT_RETENTION_DAYS)
    return max(1, int(val)) if isinstance(val, (int, float)) else DEFAULT_RETENTION_DAYS


def prune_completed(data):
    """Remove completed items older than the retention period. Returns True if any were pruned."""
    retention = get_retention_days(data)
    cutoff = (datetime.now() - timedelta(days=retention)).isoformat()
    before = len(data["completed"])
    data["completed"] = [c for c in data["completed"] if (c.get("completed_at") or "") >= cutoff]
    return len(data["completed"]) < before


def track_deletion(data, *item_ids):
    """Record deleted item IDs so sync won't resurrect them from the phone."""
    if "deleted_ids" not in data:
        data["deleted_ids"] = []
    existing = set(data["deleted_ids"])
    for item_id in item_ids:
        if is_safe_id(item_id) and item_id not in existing:
            data["deleted_ids"].append(item_id)
            existing.add(item_id)
    if len(data["deleted_ids"]) > MAX_DELETED_IDS:
        data["deleted_ids"] = data["deleted_ids"][-MAX_DELETED_IDS:]


# ── Paired devices file ─────────────────────────────────────────────

def load_paired_devices(pair_file=None):
    """Load the set of paired device IDs from disk."""
    path = pair_file or PAIR_FILE
    try:
        with open(path, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                return set(str(d) for d in data if isinstance(d, str) and len(d) <= 100)
            return set()
    except (json.JSONDecodeError, FileNotFoundError):
        return set()


def save_paired_devices(devices, pair_file=None):
    """Atomically save paired device IDs to disk."""
    _atomic_write_json(pair_file or PAIR_FILE, list(devices))
