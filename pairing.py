"""Pairing Blueprint — device pairing and management for phone sync."""

import secrets
import socket
import threading
import uuid
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify

from data import load_paired_devices, save_paired_devices

pairing_bp = Blueprint("pairing", __name__)

# ── State ────────────────────────────────────────────────────────────

_pairing_lock = threading.Lock()
_pairing_code = None
_pair_attempts = 0
_pair_attempt_reset = None

MAX_PAIR_ATTEMPTS = 5
MAX_PAIRED_DEVICES = 10

paired_devices = load_paired_devices()

# ── Routes ───────────────────────────────────────────────────────────


@pairing_bp.route("/api/pair/generate", methods=["POST"])
def generate_pairing_code():
    """Generate a 6-digit pairing code valid for 5 minutes."""
    global _pairing_code
    with _pairing_lock:
        code = f"{secrets.randbelow(1000000):06d}"
        expires_dt = datetime.now() + timedelta(minutes=5)
        _pairing_code = {"code": code, "expires": expires_dt}
        expires = expires_dt.isoformat()
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


@pairing_bp.route("/api/pair/validate", methods=["POST"])
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

        if len(paired_devices) >= MAX_PAIRED_DEVICES and device_id not in paired_devices:
            return jsonify({"error": "too many paired devices, unpair one first"}), 400
        paired_devices.add(device_id)
        save_paired_devices(paired_devices)
        _pairing_code = None
        _pair_attempts = 0

    return jsonify({"paired": True, "device_id": device_id})


@pairing_bp.route("/api/pair/status", methods=["GET"])
def pairing_status():
    """Check if a device is paired."""
    device_id = request.args.get("device_id", "")
    if not device_id or len(device_id) > 100:
        return jsonify({"paired": False})
    with _pairing_lock:
        paired = device_id in paired_devices
    return jsonify({"paired": paired})


@pairing_bp.route("/api/pair/count", methods=["GET"])
def paired_device_count():
    """Return the number of currently paired devices."""
    with _pairing_lock:
        count = len(paired_devices)
    return jsonify({"count": count})


@pairing_bp.route("/api/pair/unpair", methods=["POST"])
def unpair_device():
    """Remove a device from the paired set."""
    body = request.get_json(silent=True)
    device_id = str(body.get("device_id", "")) if body else ""
    if not device_id or len(device_id) > 100:
        return jsonify({"error": "invalid device_id"}), 400
    with _pairing_lock:
        paired_devices.discard(device_id)
        save_paired_devices(paired_devices)
    return jsonify({"unpaired": True})
