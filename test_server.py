"""Comprehensive test suite for Nudge server API.

Covers: reminders CRUD, completed CRUD, uncomplete, sync merge,
config, pairing, batch operations, soft-delete/hidden state,
validation, retention, and capacity limits.
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import data as data_mod
import pairing
import server


@pytest.fixture(autouse=True)
def fresh_server(tmp_path):
    """Reset server state before each test."""
    data_file = str(tmp_path / "reminders.json")

    data_mod.DATA_FILE = data_file
    data_mod.init_data_file()

    pairing._pairing_code = None
    pairing._pair_attempts = 0
    pairing._pair_attempt_reset = None
    pairing.paired_devices = set()

    app = server.app
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


# ── Helpers ──────────────────────────────────────────────────────────

def _make_reminder(text="Test item", **kwargs):
    r = {"text": text}
    r.update(kwargs)
    return r


def _post_reminder(client, text="Test item", **kwargs):
    return client.post("/api/reminders", json=_make_reminder(text, **kwargs))


def _get_reminders(client):
    return client.get("/api/reminders").get_json()


def _get_completed(client):
    return client.get("/api/completed").get_json()


def _complete(client, rid):
    return client.patch(f"/api/reminders/{rid}/complete")


def _uncomplete(client, cid):
    return client.patch(f"/api/completed/{cid}/uncomplete")


# ── ID Validation ────────────────────────────────────────────────────

class TestIdValidation:
    def test_safe_id_accepted(self, fresh_server):
        resp = _post_reminder(fresh_server, id="abc-123-DEF")
        assert resp.status_code == 201

    def test_unsafe_id_rejected(self, fresh_server):
        resp = _post_reminder(fresh_server, id="<script>alert(1)</script>")
        assert resp.status_code == 400

    def test_empty_id_generates_uuid(self, fresh_server):
        # Empty string ID is falsy, so server generates a UUID
        resp = _post_reminder(fresh_server, id="")
        assert resp.status_code == 201
        assert len(resp.get_json()["id"]) > 0

    def test_long_id_truncated_to_100(self, fresh_server):
        # Server truncates to 100 chars before validation
        resp = _post_reminder(fresh_server, id="a" * 101)
        assert resp.status_code == 201
        assert len(resp.get_json()["id"]) == 100

    def test_max_length_id_accepted(self, fresh_server):
        resp = _post_reminder(fresh_server, id="a" * 100)
        assert resp.status_code == 201

    def test_id_with_spaces_rejected(self, fresh_server):
        resp = _post_reminder(fresh_server, id="has spaces")
        assert resp.status_code == 400

    def test_id_with_underscores_rejected(self, fresh_server):
        # Only alphanumeric + hyphens allowed
        resp = _post_reminder(fresh_server, id="has_underscore")
        assert resp.status_code == 400


# ── Reminders CRUD ──────────────────────────────────────────────────

class TestRemindersCRUD:
    def test_create_reminder(self, fresh_server):
        resp = _post_reminder(fresh_server, "Buy groceries")
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["text"] == "Buy groceries"
        assert "id" in data
        assert "created_at" in data

    def test_create_with_custom_id(self, fresh_server):
        resp = _post_reminder(fresh_server, id="custom-id-1")
        assert resp.status_code == 201
        assert resp.get_json()["id"] == "custom-id-1"

    def test_duplicate_id_rejected(self, fresh_server):
        _post_reminder(fresh_server, id="dup-1")
        resp = _post_reminder(fresh_server, id="dup-1")
        assert resp.status_code == 409

    def test_list_reminders_sorted_by_order(self, fresh_server):
        _post_reminder(fresh_server, "First", order=1)
        _post_reminder(fresh_server, "Second", order=2)
        _post_reminder(fresh_server, "Third", order=3)
        items = _get_reminders(fresh_server)
        assert items[0]["text"] == "Third"
        assert items[2]["text"] == "First"

    def test_edit_reminder_text(self, fresh_server):
        resp = _post_reminder(fresh_server, "Original")
        rid = resp.get_json()["id"]
        resp = fresh_server.put(f"/api/reminders/{rid}", json={"text": "Updated"})
        assert resp.status_code == 200
        assert resp.get_json()["text"] == "Updated"

    def test_edit_nonexistent_returns_404(self, fresh_server):
        resp = fresh_server.put("/api/reminders/nonexistent", json={"text": "X"})
        assert resp.status_code == 404

    def test_delete_reminder(self, fresh_server):
        resp = _post_reminder(fresh_server)
        rid = resp.get_json()["id"]
        resp = fresh_server.delete(f"/api/reminders/{rid}")
        assert resp.status_code == 200
        assert len(_get_reminders(fresh_server)) == 0

    def test_delete_nonexistent_returns_404(self, fresh_server):
        resp = fresh_server.delete("/api/reminders/nonexistent")
        assert resp.status_code == 404

    def test_text_truncated_to_500(self, fresh_server):
        resp = _post_reminder(fresh_server, "x" * 600)
        assert len(resp.get_json()["text"]) == 500

    def test_empty_text_rejected(self, fresh_server):
        resp = fresh_server.post("/api/reminders", json={"text": ""})
        assert resp.status_code == 400

    def test_missing_text_rejected(self, fresh_server):
        resp = fresh_server.post("/api/reminders", json={})
        assert resp.status_code == 400

    def test_auto_order_increments(self, fresh_server):
        _post_reminder(fresh_server, "A")
        resp = _post_reminder(fresh_server, "B")
        b = resp.get_json()
        assert b["order"] >= 1

    def test_edit_pinned(self, fresh_server):
        resp = _post_reminder(fresh_server)
        rid = resp.get_json()["id"]
        resp = fresh_server.put(f"/api/reminders/{rid}", json={"pinned": True})
        assert resp.get_json()["pinned"] is True

    def test_edit_deletion_flagged(self, fresh_server):
        resp = _post_reminder(fresh_server)
        rid = resp.get_json()["id"]
        resp = fresh_server.put(f"/api/reminders/{rid}", json={"deletion_flagged": True})
        assert resp.get_json()["deletion_flagged"] is True


# ── Reorder ──────────────────────────────────────────────────────────

class TestReorder:
    def test_reorder_reminders(self, fresh_server):
        r1 = _post_reminder(fresh_server, "A").get_json()
        r2 = _post_reminder(fresh_server, "B").get_json()
        r3 = _post_reminder(fresh_server, "C").get_json()
        resp = fresh_server.post("/api/reminders/reorder", json=[
            {"id": r1["id"], "order": 3},
            {"id": r2["id"], "order": 1},
            {"id": r3["id"], "order": 2},
        ])
        assert resp.status_code == 200
        items = _get_reminders(fresh_server)
        assert items[0]["text"] == "A"  # order 3 = first (desc sort)
        assert items[2]["text"] == "B"  # order 1 = last

    def test_reorder_with_invalid_id_skipped(self, fresh_server):
        _post_reminder(fresh_server, "A", id="a1")
        resp = fresh_server.post("/api/reminders/reorder", json=[
            {"id": "a1", "order": 5},
            {"id": "nonexistent", "order": 1},
        ])
        assert resp.status_code == 200


# ── Completion ───────────────────────────────────────────────────────

class TestCompletion:
    def test_complete_reminder(self, fresh_server):
        resp = _post_reminder(fresh_server)
        rid = resp.get_json()["id"]
        resp = _complete(fresh_server, rid)
        assert resp.status_code == 200
        assert resp.get_json()["completed_at"] is not None
        assert len(_get_reminders(fresh_server)) == 0
        assert len(_get_completed(fresh_server)) == 1

    def test_complete_nonexistent_returns_404(self, fresh_server):
        resp = _complete(fresh_server, "nonexistent")
        assert resp.status_code == 404

    def test_uncomplete_moves_back(self, fresh_server):
        resp = _post_reminder(fresh_server, "Restore me")
        rid = resp.get_json()["id"]
        _complete(fresh_server, rid)
        resp = _uncomplete(fresh_server, rid)
        assert resp.status_code == 200
        assert resp.get_json()["completed_at"] is None
        assert len(_get_reminders(fresh_server)) == 1
        assert len(_get_completed(fresh_server)) == 0

    def test_uncomplete_nonexistent_returns_404(self, fresh_server):
        resp = _uncomplete(fresh_server, "nonexistent")
        assert resp.status_code == 404

    def test_batch_complete(self, fresh_server):
        ids = []
        for i in range(3):
            r = _post_reminder(fresh_server, f"Item {i}").get_json()
            ids.append(r["id"])
        # Batch endpoints expect a plain JSON array, not {"ids": [...]}
        resp = fresh_server.post("/api/reminders/batch-complete", json=ids[:2])
        assert resp.status_code == 200
        assert resp.get_json()["completed"] == 2
        assert len(_get_reminders(fresh_server)) == 1
        assert len(_get_completed(fresh_server)) == 2

    def test_batch_delete(self, fresh_server):
        ids = []
        for i in range(3):
            r = _post_reminder(fresh_server, f"Item {i}").get_json()
            ids.append(r["id"])
        # Batch endpoints expect a plain JSON array
        resp = fresh_server.post("/api/reminders/batch-delete", json=ids[:2])
        assert resp.status_code == 200
        assert resp.get_json()["deleted"] == 2
        assert len(_get_reminders(fresh_server)) == 1


# ── Completed Items ──────────────────────────────────────────────────

class TestCompleted:
    def test_list_completed_sorted_newest_first(self, fresh_server):
        r1 = _post_reminder(fresh_server, "First").get_json()
        r2 = _post_reminder(fresh_server, "Second").get_json()
        _complete(fresh_server, r1["id"])
        time.sleep(0.01)  # Ensure different timestamps
        _complete(fresh_server, r2["id"])
        completed = _get_completed(fresh_server)
        assert completed[0]["text"] == "Second"

    def test_delete_completed(self, fresh_server):
        r = _post_reminder(fresh_server).get_json()
        _complete(fresh_server, r["id"])
        resp = fresh_server.delete(f"/api/completed/{r['id']}")
        assert resp.status_code == 200
        assert len(_get_completed(fresh_server)) == 0

    def test_delete_completed_nonexistent(self, fresh_server):
        resp = fresh_server.delete("/api/completed/nonexistent")
        assert resp.status_code == 404


# ── Retention / Pruning ──────────────────────────────────────────────

class TestRetention:
    def test_old_completed_pruned_on_get(self, fresh_server):

        # Create and complete a reminder
        r = _post_reminder(fresh_server).get_json()
        _complete(fresh_server, r["id"])

        # Manually age the completed item beyond retention
        data = data_mod.load_data()
        for c in data["completed"]:
            if c["id"] == r["id"]:
                old_date = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
                c["completed_at"] = old_date
        data_mod.save_data(data)

        completed = _get_completed(fresh_server)
        assert len(completed) == 0


# ── Capacity Limits ──────────────────────────────────────────────────

class TestCapacity:
    def test_max_200_reminders(self, fresh_server):
        for i in range(200):
            resp = _post_reminder(fresh_server, f"Item {i}")
            assert resp.status_code == 201
        resp = _post_reminder(fresh_server, "One too many")
        assert resp.status_code == 400

    def test_uncomplete_blocked_at_capacity(self, fresh_server):
        # Fill up to 200
        for i in range(200):
            _post_reminder(fresh_server, f"Item {i}")
        # Complete one, then add one more to fill
        items = _get_reminders(fresh_server)
        _complete(fresh_server, items[0]["id"])
        _post_reminder(fresh_server, "Filler")
        # Try to uncomplete — should fail at 200
        completed = _get_completed(fresh_server)
        resp = _uncomplete(fresh_server, completed[0]["id"])
        assert resp.status_code == 400


# ── Config ───────────────────────────────────────────────────────────

class TestConfig:
    def test_get_config(self, fresh_server):
        resp = fresh_server.get("/api/config")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "popup_interval_minutes" in data
        assert "completed_retention_days" in data

    def test_update_config(self, fresh_server):
        resp = fresh_server.put("/api/config", json={
            "popup_interval_minutes": 30,
            "completed_retention_days": 90,
        })
        assert resp.status_code == 200
        config = fresh_server.get("/api/config").get_json()
        assert config["popup_interval_minutes"] == 30
        assert config["completed_retention_days"] == 90

    def test_invalid_interval_rejected(self, fresh_server):
        resp = fresh_server.put("/api/config", json={"popup_interval_minutes": 0})
        assert resp.status_code == 400

    def test_invalid_port_rejected(self, fresh_server):
        resp = fresh_server.put("/api/config", json={"server_port": 80})
        assert resp.status_code == 400

    def test_invalid_retention_rejected(self, fresh_server):
        resp = fresh_server.put("/api/config", json={"completed_retention_days": 0})
        assert resp.status_code == 400

    def test_auto_refresh_minimum(self, fresh_server):
        resp = fresh_server.put("/api/config", json={"auto_refresh_seconds": 5})
        assert resp.status_code == 400
        resp = fresh_server.put("/api/config", json={"auto_refresh_seconds": 10})
        assert resp.status_code == 200

    def test_unknown_config_fields_ignored(self, fresh_server):
        resp = fresh_server.put("/api/config", json={"unknown_field": "value"})
        assert resp.status_code == 200

    def test_start_with_windows_boolean(self, fresh_server):
        resp = fresh_server.put("/api/config", json={"start_with_windows": True})
        assert resp.status_code == 200
        config = fresh_server.get("/api/config").get_json()
        assert config["start_with_windows"] is True


# ── Pairing ──────────────────────────────────────────────────────────

class TestPairing:
    def test_generate_pairing_code(self, fresh_server):
        resp = fresh_server.post("/api/pair/generate")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "code" in data
        assert len(data["code"]) == 6

    def test_validate_pairing(self, fresh_server):
        code = fresh_server.post("/api/pair/generate").get_json()["code"]
        resp = fresh_server.post("/api/pair/validate", json={"code": code})
        assert resp.status_code == 200
        assert resp.get_json()["paired"] is True

    def test_validate_wrong_code(self, fresh_server):
        fresh_server.post("/api/pair/generate")
        resp = fresh_server.post("/api/pair/validate", json={"code": "000000"})
        assert resp.status_code == 400  # Server returns 400, not 403

    def test_validate_expired_code(self, fresh_server):

        code = fresh_server.post("/api/pair/generate").get_json()["code"]
        # Manually expire the pairing code dict
        pairing._pairing_code["expires"] = datetime.now() - timedelta(minutes=1)
        resp = fresh_server.post("/api/pair/validate", json={"code": code})
        assert resp.status_code == 400  # Server returns 400 for expired

    def test_pairing_status(self, fresh_server):
        code = fresh_server.post("/api/pair/generate").get_json()["code"]
        result = fresh_server.post("/api/pair/validate", json={"code": code})
        device_id = result.get_json()["device_id"]
        resp = fresh_server.get(f"/api/pair/status?device_id={device_id}")
        assert resp.get_json()["paired"] is True

    def test_unpaired_status(self, fresh_server):
        resp = fresh_server.get("/api/pair/status?device_id=unknown-device")
        assert resp.get_json()["paired"] is False

    def test_unpair_device(self, fresh_server):
        code = fresh_server.post("/api/pair/generate").get_json()["code"]
        result = fresh_server.post("/api/pair/validate", json={"code": code})
        device_id = result.get_json()["device_id"]
        resp = fresh_server.post("/api/pair/unpair", json={"device_id": device_id})
        assert resp.status_code == 200
        status = fresh_server.get(f"/api/pair/status?device_id={device_id}")
        assert status.get_json()["paired"] is False

    def test_rate_limiting(self, fresh_server):
        fresh_server.post("/api/pair/generate")
        for _ in range(5):
            fresh_server.post("/api/pair/validate", json={"code": "000000"})
        resp = fresh_server.post("/api/pair/validate", json={"code": "000000"})
        assert resp.status_code == 429


# ── Sync ─────────────────────────────────────────────────────────────

def _pair_device(client, device_id="test-device"):
    """Helper to pair a device for sync tests."""
    pairing.paired_devices.add(device_id)


class TestSync:
    def test_sync_requires_pairing(self, fresh_server):
        resp = fresh_server.post("/api/sync", json={
            "device_id": "unpaired-device",
            "reminders": [],
            "completed": [],
        })
        assert resp.status_code == 403

    def test_sync_basic_merge(self, fresh_server):
        _pair_device(fresh_server)
        # Server has one reminder
        _post_reminder(fresh_server, "Server item", id="s1")

        # Phone sends one reminder
        resp = fresh_server.post("/api/sync", json={
            "device_id": "test-device",
            "reminders": [{"id": "p1", "text": "Phone item", "created_at": datetime.now(timezone.utc).isoformat(), "order": 0}],
            "completed": [],
        })
        assert resp.status_code == 200
        merged = resp.get_json()
        ids = [r["id"] for r in merged["reminders"]]
        assert "s1" in ids
        assert "p1" in ids

    def test_sync_completion_wins(self, fresh_server):
        """If an item is completed on one side, it should be completed in merged result."""
        _pair_device(fresh_server)
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        # Server has active reminder
        _post_reminder(fresh_server, "Shared item", id="shared-1")

        # Phone sends same item as completed (with valid completed_at)
        resp = fresh_server.post("/api/sync", json={
            "device_id": "test-device",
            "reminders": [],
            "completed": [{"id": "shared-1", "text": "Shared item",
                          "created_at": now, "completed_at": now, "order": 0}],
        })
        assert resp.status_code == 200
        merged = resp.get_json()
        completed_ids = [c["id"] for c in merged.get("completed", [])]
        reminder_ids = [r["id"] for r in merged.get("reminders", [])]
        assert "shared-1" in completed_ids
        assert "shared-1" not in reminder_ids

    def test_sync_deleted_ids_not_resurrected(self, fresh_server):
        """Items in deleted_ids should not come back via sync."""
        _pair_device(fresh_server)


        # Create and delete a reminder (adds to deleted_ids)
        _post_reminder(fresh_server, "Gone", id="del-1")
        fresh_server.delete("/api/reminders/del-1")

        # Phone tries to sync the deleted item back
        now = datetime.now(timezone.utc).isoformat()
        resp = fresh_server.post("/api/sync", json={
            "device_id": "test-device",
            "reminders": [{"id": "del-1", "text": "Gone", "created_at": now, "order": 0}],
            "completed": [],
        })
        merged = resp.get_json()
        ids = [r["id"] for r in merged["reminders"]]
        assert "del-1" not in ids

    def test_sync_hidden_is_sticky(self, fresh_server):
        """If either side marks hidden, item stays hidden."""
        _pair_device(fresh_server)
        now = datetime.now(timezone.utc).isoformat()

        # Server has active reminder
        _post_reminder(fresh_server, "Hide me", id="hide-1")

        # Phone sends same item with hidden=true
        resp = fresh_server.post("/api/sync", json={
            "device_id": "test-device",
            "reminders": [{"id": "hide-1", "text": "Hide me", "created_at": now,
                          "order": 0, "hidden": True, "hidden_at": now}],
            "completed": [],
        })
        merged = resp.get_json()
        item = next(r for r in merged["reminders"] if r["id"] == "hide-1")
        assert item.get("hidden") is True

    def test_sync_hidden_with_null_hidden_at(self, fresh_server):
        """Sync shouldn't crash when hidden_at is None on one or both sides."""
        _pair_device(fresh_server)
        now = datetime.now(timezone.utc).isoformat()

        # Server has item hidden with hidden_at = None (legacy data)
        _post_reminder(fresh_server, "Legacy hide", id="null-hide-1")
        data = data_mod.load_data()
        for r in data["reminders"]:
            if r["id"] == "null-hide-1":
                r["hidden"] = True
                r["hidden_at"] = None  # legacy — key present but null
        data_mod.save_data(data)

        # Phone sends same item with hidden=true and proper hidden_at
        resp = fresh_server.post("/api/sync", json={
            "device_id": "test-device",
            "reminders": [{"id": "null-hide-1", "text": "Legacy hide",
                          "created_at": now, "updated_at": now, "order": 0,
                          "hidden": True, "hidden_at": now}],
            "completed": [],
        })
        assert resp.status_code == 200
        item = next(r for r in resp.get_json()["reminders"] if r["id"] == "null-hide-1")
        assert item["hidden"] is True
        # hidden_at should be the non-null value from the phone side
        assert item.get("hidden_at") is not None

    def test_sync_hidden_with_missing_hidden_at(self, fresh_server):
        """Sync handles items where hidden=true but hidden_at key is absent."""
        _pair_device(fresh_server)
        now = datetime.now(timezone.utc).isoformat()

        _post_reminder(fresh_server, "No ts", id="no-ts-1")
        data = data_mod.load_data()
        for r in data["reminders"]:
            if r["id"] == "no-ts-1":
                r["hidden"] = True
                # no hidden_at key at all
        data_mod.save_data(data)

        resp = fresh_server.post("/api/sync", json={
            "device_id": "test-device",
            "reminders": [{"id": "no-ts-1", "text": "No ts",
                          "created_at": now, "updated_at": now, "order": 0,
                          "hidden": True, "hidden_at": now}],
            "completed": [],
        })
        assert resp.status_code == 200
        item = next(r for r in resp.get_json()["reminders"] if r["id"] == "no-ts-1")
        assert item["hidden"] is True

    def test_sync_both_hidden_at_null(self, fresh_server):
        """Sync handles both sides having hidden_at as None."""
        _pair_device(fresh_server)
        now = datetime.now(timezone.utc).isoformat()

        _post_reminder(fresh_server, "Both null", id="both-null-1")
        data = data_mod.load_data()
        for r in data["reminders"]:
            if r["id"] == "both-null-1":
                r["hidden"] = True
                r["hidden_at"] = None
        data_mod.save_data(data)

        resp = fresh_server.post("/api/sync", json={
            "device_id": "test-device",
            "reminders": [{"id": "both-null-1", "text": "Both null",
                          "created_at": now, "updated_at": now, "order": 0,
                          "hidden": True, "hidden_at": None}],
            "completed": [],
        })
        assert resp.status_code == 200
        item = next(r for r in resp.get_json()["reminders"] if r["id"] == "both-null-1")
        assert item["hidden"] is True
        assert item["hidden_at"] is None  # both were null, stays null

    def test_sync_null_updated_at(self, fresh_server):
        """Sync handles updated_at being None (falls back to created_at)."""
        _pair_device(fresh_server)
        old = "2020-01-01T00:00:00+00:00"
        new = "2026-01-01T00:00:00+00:00"

        _post_reminder(fresh_server, "Old text", id="null-upd-1")
        data = data_mod.load_data()
        for r in data["reminders"]:
            if r["id"] == "null-upd-1":
                r["updated_at"] = None  # key present but null
                r["created_at"] = old
        data_mod.save_data(data)

        resp = fresh_server.post("/api/sync", json={
            "device_id": "test-device",
            "reminders": [{"id": "null-upd-1", "text": "New text",
                          "created_at": old, "updated_at": new, "order": 0}],
            "completed": [],
        })
        assert resp.status_code == 200
        item = next(r for r in resp.get_json()["reminders"] if r["id"] == "null-upd-1")
        assert item["text"] == "New text"

    def test_sync_latest_update_wins(self, fresh_server):
        """When both sides have the same active item, latest updated_at wins."""
        _pair_device(fresh_server)


        old = "2020-01-01T00:00:00+00:00"
        new = "2026-01-01T00:00:00+00:00"

        # Create item, then manually set old timestamps
        _post_reminder(fresh_server, "Old text", id="merge-1")
        data = data_mod.load_data()
        for r in data["reminders"]:
            if r["id"] == "merge-1":
                r["updated_at"] = old
                r["created_at"] = old
                r["text"] = "Old text"
        data_mod.save_data(data)

        # Phone sends same item with newer timestamp
        resp = fresh_server.post("/api/sync", json={
            "device_id": "test-device",
            "reminders": [{"id": "merge-1", "text": "New text", "created_at": old,
                          "updated_at": new, "order": 0}],
            "completed": [],
        })
        merged = resp.get_json()
        item = next(r for r in merged["reminders"] if r["id"] == "merge-1")
        assert item["text"] == "New text"

    def test_sync_capacity_limit(self, fresh_server):
        """Sync should cap merged reminders at 200."""
        _pair_device(fresh_server)
        now = datetime.now(timezone.utc).isoformat()

        # Create 150 server-side reminders
        for i in range(150):
            _post_reminder(fresh_server, f"Server {i}")

        # Phone sends 100 more
        phone_reminders = [
            {"id": f"phone-{i}", "text": f"Phone {i}", "created_at": now, "order": i}
            for i in range(100)
        ]
        resp = fresh_server.post("/api/sync", json={
            "device_id": "test-device",
            "reminders": phone_reminders,
            "completed": [],
        })
        merged = resp.get_json()
        assert len(merged["reminders"]) <= 200

    def test_sync_returns_deleted_ids(self, fresh_server):
        """Sync response should include deleted_ids for client-side cleanup."""
        _pair_device(fresh_server)
        _post_reminder(fresh_server, "Will delete", id="to-delete")
        fresh_server.delete("/api/reminders/to-delete")

        resp = fresh_server.post("/api/sync", json={
            "device_id": "test-device",
            "reminders": [],
            "completed": [],
        })
        merged = resp.get_json()
        assert "to-delete" in merged.get("deleted_ids", [])

    def test_sync_sanitizes_phone_data(self, fresh_server):
        """Phone data should be sanitized (text truncated, timestamps clamped)."""
        _pair_device(fresh_server)
        far_future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

        resp = fresh_server.post("/api/sync", json={
            "device_id": "test-device",
            "reminders": [{
                "id": "san-1",
                "text": "x" * 600,
                "created_at": far_future,
                "order": 0,
            }],
            "completed": [],
        })
        merged = resp.get_json()
        item = next(r for r in merged["reminders"] if r["id"] == "san-1")
        assert len(item["text"]) == 500


# ── Timestamp Clamping ───────────────────────────────────────────────

class TestTimestampClamping:
    def test_future_created_at_clamped(self, fresh_server):
        far_future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        resp = _post_reminder(fresh_server, created_at=far_future)
        assert resp.status_code == 201
        created_str = resp.get_json()["created_at"]
        # The clamped timestamp should be different from the far future one
        assert created_str != far_future

    def test_valid_past_timestamp_preserved(self, fresh_server):
        past = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        resp = _post_reminder(fresh_server, created_at=past)
        assert resp.status_code == 201

    def test_timezone_aware_timestamp_accepted(self, fresh_server):
        """UTC timestamps with +00:00 suffix should not be rejected."""
        ts = datetime.now(timezone.utc).isoformat()  # e.g. 2026-03-10T09:00:00.123456+00:00
        resp = _post_reminder(fresh_server, created_at=ts)
        assert resp.status_code == 201
        # The original timestamp should be preserved (not replaced by now())
        assert resp.get_json()["created_at"] == ts

    def test_clamp_timestamp_none_returns_none(self, fresh_server):
        """clamp_timestamp(None) should return None, not crash."""
        from data import clamp_timestamp
        assert clamp_timestamp(None) is None

    def test_clamp_timestamp_empty_string_returns_none(self, fresh_server):
        """clamp_timestamp('') should return None (invalid ISO)."""
        from data import clamp_timestamp
        assert clamp_timestamp("") is None

    def test_clamp_timestamp_naive_preserved(self, fresh_server):
        """Naive (no timezone) timestamps should be accepted."""
        from data import clamp_timestamp
        ts = datetime.now().isoformat()
        assert clamp_timestamp(ts) == ts

    def test_clamp_timestamp_utc_preserved(self, fresh_server):
        """UTC timestamps with +00:00 should be accepted and preserved."""
        from data import clamp_timestamp
        ts = datetime.now(timezone.utc).isoformat()
        assert clamp_timestamp(ts) == ts

    def test_sync_preserves_phone_utc_timestamps(self, fresh_server):
        """Phone timestamps (UTC with +00:00) should survive sanitization during sync."""
        _pair_device(fresh_server)
        now = datetime.now(timezone.utc).isoformat()

        resp = fresh_server.post("/api/sync", json={
            "device_id": "test-device",
            "reminders": [{"id": "utc-1", "text": "UTC test",
                          "created_at": now, "updated_at": now, "order": 0}],
            "completed": [],
        })
        assert resp.status_code == 200
        item = next(r for r in resp.get_json()["reminders"] if r["id"] == "utc-1")
        # Timestamps should be preserved, not stripped to None/defaults
        assert item["created_at"] == now
        assert item.get("updated_at") == now


# ── Soft Delete / Hidden State ───────────────────────────────────────

class TestSoftDelete:
    def test_deleted_reminder_tracked(self, fresh_server):
        """Deleting a reminder should add its ID to deleted_ids."""

        r = _post_reminder(fresh_server, id="track-del").get_json()
        fresh_server.delete("/api/reminders/track-del")
        data = data_mod.load_data()
        assert "track-del" in data.get("deleted_ids", [])

    def test_deleted_ids_capped(self, fresh_server):
        """deleted_ids should not grow beyond 500."""

        for i in range(510):
            _post_reminder(fresh_server, f"Item {i}", id=f"cap-{i}")
            fresh_server.delete(f"/api/reminders/cap-{i}")
        data = data_mod.load_data()
        assert len(data.get("deleted_ids", [])) <= 500


# ── Concurrent Access ───────────────────────────────────────────────

class TestConcurrency:
    def test_concurrent_creates(self, fresh_server):
        """Multiple threads creating reminders should not corrupt data."""

        app = server.app
        errors = []

        def create_reminder(n):
            with app.test_client() as c:
                try:
                    resp = c.post("/api/reminders", json={"text": f"Thread {n}"})
                    if resp.status_code != 201:
                        errors.append(f"Thread {n}: status {resp.status_code}")
                except Exception as e:
                    errors.append(str(e))

        threads = [threading.Thread(target=create_reminder, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        items = _get_reminders(fresh_server)
        assert len(items) == 20


# ── Edge Cases ───────────────────────────────────────────────────────

class TestEdgeCases:
    def test_complete_then_delete_completed(self, fresh_server):
        r = _post_reminder(fresh_server).get_json()
        _complete(fresh_server, r["id"])
        fresh_server.delete(f"/api/completed/{r['id']}")
        assert len(_get_completed(fresh_server)) == 0
        assert len(_get_reminders(fresh_server)) == 0

    def test_uncomplete_restores_text(self, fresh_server):
        r = _post_reminder(fresh_server, "Remember me").get_json()
        _complete(fresh_server, r["id"])
        resp = _uncomplete(fresh_server, r["id"])
        assert resp.get_json()["text"] == "Remember me"

    def test_empty_sync(self, fresh_server):
        _pair_device(fresh_server)
        resp = fresh_server.post("/api/sync", json={
            "device_id": "test-device",
            "reminders": [],
            "completed": [],
        })
        assert resp.status_code == 200

    def test_sync_with_bad_items_rejected(self, fresh_server):
        """Sync rejects entire request if any item has an invalid ID."""
        _pair_device(fresh_server)
        now = datetime.now(timezone.utc).isoformat()
        resp = fresh_server.post("/api/sync", json={
            "device_id": "test-device",
            "reminders": [
                {"id": "good-id", "text": "Good", "created_at": now, "order": 0},
                {"id": "<bad>", "text": "Bad", "created_at": now, "order": 0},
            ],
            "completed": [],
        })
        assert resp.status_code == 400

    def test_sync_with_all_good_items(self, fresh_server):
        """Sync succeeds when all items have valid IDs."""
        _pair_device(fresh_server)
        now = datetime.now(timezone.utc).isoformat()
        resp = fresh_server.post("/api/sync", json={
            "device_id": "test-device",
            "reminders": [
                {"id": "good-1", "text": "Good", "created_at": now, "order": 0},
                {"id": "good-2", "text": "Also good", "created_at": now, "order": 1},
            ],
            "completed": [],
        })
        assert resp.status_code == 200
        merged = resp.get_json()
        ids = [r["id"] for r in merged["reminders"]]
        assert "good-1" in ids
        assert "good-2" in ids

    def test_whitespace_only_text_rejected(self, fresh_server):
        resp = fresh_server.post("/api/reminders", json={"text": "   "})
        assert resp.status_code == 400


# ── Crash & Corruption Resilience ────────────────────────────────────

class TestCorruptionResilience:
    def test_corrupted_data_file_recovers(self, fresh_server, tmp_path):
        """Server should recover from a corrupted JSON file."""

        # Write garbage to the data file
        with open(data_mod.DATA_FILE, "w") as f:
            f.write("{{{invalid json")
        # Server should return defaults, not crash
        items = _get_reminders(fresh_server)
        assert items == []

    def test_missing_data_file_recovers(self, fresh_server, tmp_path):
        """Server should recover if data file is deleted mid-session."""

        # Ensure file exists first by creating something
        _post_reminder(fresh_server, "Temp")
        os.remove(data_mod.DATA_FILE)
        items = _get_reminders(fresh_server)
        assert items == []
        # Should be able to create new items
        resp = _post_reminder(fresh_server, "After crash")
        assert resp.status_code == 201

    def test_partial_data_file(self, fresh_server, tmp_path):
        """Data file missing expected keys should not crash."""

        with open(data_mod.DATA_FILE, "w") as f:
            json.dump({"config": {}}, f)  # Missing reminders/completed
        items = _get_reminders(fresh_server)
        # Should handle missing keys gracefully
        assert isinstance(items, list)


# ── Malformed Request Bodies ─────────────────────────────────────────

class TestMalformedRequests:
    def test_no_json_body_create(self, fresh_server):
        resp = fresh_server.post("/api/reminders", data="not json",
                                content_type="text/plain")
        assert resp.status_code == 400

    def test_null_json_body_create(self, fresh_server):
        resp = fresh_server.post("/api/reminders", json=None)
        assert resp.status_code == 400

    def test_array_body_on_create(self, fresh_server):
        resp = fresh_server.post("/api/reminders", json=[{"text": "hi"}])
        assert resp.status_code == 400

    def test_no_json_body_edit(self, fresh_server):
        r = _post_reminder(fresh_server).get_json()
        resp = fresh_server.put(f"/api/reminders/{r['id']}", data="bad",
                               content_type="text/plain")
        assert resp.status_code == 400

    def test_no_json_body_config(self, fresh_server):
        resp = fresh_server.put("/api/config", data="bad",
                               content_type="text/plain")
        assert resp.status_code == 400

    def test_no_json_body_sync(self, fresh_server):
        _pair_device(fresh_server)
        resp = fresh_server.post("/api/sync", data="bad",
                                content_type="text/plain")
        assert resp.status_code == 400

    def test_sync_reminders_not_array(self, fresh_server):
        _pair_device(fresh_server)
        resp = fresh_server.post("/api/sync", json={
            "device_id": "test-device",
            "reminders": "not an array",
            "completed": [],
        })
        assert resp.status_code == 400

    def test_sync_item_missing_id(self, fresh_server):
        _pair_device(fresh_server)
        resp = fresh_server.post("/api/sync", json={
            "device_id": "test-device",
            "reminders": [{"text": "No ID", "created_at": "2026-01-01T00:00:00"}],
            "completed": [],
        })
        assert resp.status_code == 400

    def test_sync_item_with_numeric_id(self, fresh_server):
        _pair_device(fresh_server)
        resp = fresh_server.post("/api/sync", json={
            "device_id": "test-device",
            "reminders": [{"id": 12345, "text": "Numeric", "created_at": "2026-01-01T00:00:00"}],
            "completed": [],
        })
        assert resp.status_code == 400

    def test_batch_complete_empty_array(self, fresh_server):
        resp = fresh_server.post("/api/reminders/batch-complete", json=[])
        # Empty array is falsy in Python, should be rejected
        assert resp.status_code == 400

    def test_batch_delete_empty_array(self, fresh_server):
        resp = fresh_server.post("/api/reminders/batch-delete", json=[])
        assert resp.status_code == 400

    def test_reorder_non_array(self, fresh_server):
        resp = fresh_server.post("/api/reminders/reorder", json={"not": "array"})
        assert resp.status_code == 400


# ── Multi-Device Sync Scenarios ──────────────────────────────────────

class TestMultiDeviceSync:
    def test_two_devices_add_different_items(self, fresh_server):
        """Two devices adding different items should merge cleanly."""

        pairing.paired_devices.add("phone-1")
        pairing.paired_devices.add("phone-2")
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        # Phone 1 syncs with item A
        fresh_server.post("/api/sync", json={
            "device_id": "phone-1",
            "reminders": [{"id": "from-phone-1", "text": "Phone 1 item", "created_at": now, "order": 0}],
            "completed": [],
        })

        # Phone 2 syncs with item B
        resp = fresh_server.post("/api/sync", json={
            "device_id": "phone-2",
            "reminders": [{"id": "from-phone-2", "text": "Phone 2 item", "created_at": now, "order": 0}],
            "completed": [],
        })
        merged = resp.get_json()
        ids = [r["id"] for r in merged["reminders"]]
        assert "from-phone-1" in ids
        assert "from-phone-2" in ids

    def test_device_deletes_while_other_syncs(self, fresh_server):
        """Item deleted on server shouldn't come back when another device syncs it."""

        pairing.paired_devices.add("phone-1")
        pairing.paired_devices.add("phone-2")
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        # Create shared item
        _post_reminder(fresh_server, "Shared", id="shared-del")

        # Phone 1 has a copy
        phone_1_copy = {"id": "shared-del", "text": "Shared", "created_at": now, "order": 0}

        # Server deletes it
        fresh_server.delete("/api/reminders/shared-del")

        # Phone 1 syncs with its old copy — should NOT resurrect
        resp = fresh_server.post("/api/sync", json={
            "device_id": "phone-1",
            "reminders": [phone_1_copy],
            "completed": [],
        })
        merged = resp.get_json()
        ids = [r["id"] for r in merged["reminders"]]
        assert "shared-del" not in ids

    def test_sync_both_sides_complete_same_item(self, fresh_server):
        """Both sides completing same item should keep the latest completion."""
        _pair_device(fresh_server)


        old_time = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        new_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        # Server has it completed with old time
        _post_reminder(fresh_server, "Both complete", id="both-comp")
        _complete(fresh_server, "both-comp")
        data = data_mod.load_data()
        for c in data["completed"]:
            if c["id"] == "both-comp":
                c["completed_at"] = old_time
        data_mod.save_data(data)

        # Phone also has it completed with newer time
        resp = fresh_server.post("/api/sync", json={
            "device_id": "test-device",
            "reminders": [],
            "completed": [{"id": "both-comp", "text": "Both complete",
                          "created_at": old_time, "completed_at": new_time, "order": 0}],
        })
        assert resp.status_code == 200
        merged = resp.get_json()
        completed_ids = [c["id"] for c in merged.get("completed", [])]
        assert "both-comp" in completed_ids
        item = next(c for c in merged["completed"] if c["id"] == "both-comp")
        assert item["completed_at"] == new_time

    def test_sync_oversized_payload_rejected(self, fresh_server):
        """Sync with more than MAX_REMINDERS*2 items should be rejected."""
        _pair_device(fresh_server)
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        huge_list = [{"id": f"item-{i}", "text": f"Item {i}", "created_at": now, "order": i}
                     for i in range(401)]
        resp = fresh_server.post("/api/sync", json={
            "device_id": "test-device",
            "reminders": huge_list,
            "completed": [],
        })
        assert resp.status_code == 400


# ── State Transition Edge Cases ──────────────────────────────────────

class TestStateTransitions:
    def test_complete_then_uncomplete_then_complete(self, fresh_server):
        """Item should survive full lifecycle."""
        r = _post_reminder(fresh_server, "Cycle test").get_json()
        rid = r["id"]
        _complete(fresh_server, rid)
        _uncomplete(fresh_server, rid)
        _complete(fresh_server, rid)
        assert len(_get_reminders(fresh_server)) == 0
        assert len(_get_completed(fresh_server)) == 1

    def test_edit_empty_text_keeps_original(self, fresh_server):
        """Editing with empty text should not clear the reminder."""
        r = _post_reminder(fresh_server, "Keep me").get_json()
        resp = fresh_server.put(f"/api/reminders/{r['id']}", json={"text": ""})
        assert resp.status_code == 200
        assert resp.get_json()["text"] == "Keep me"

    def test_edit_whitespace_text_keeps_original(self, fresh_server):
        """Editing with whitespace text should not clear the reminder."""
        r = _post_reminder(fresh_server, "Keep me too").get_json()
        resp = fresh_server.put(f"/api/reminders/{r['id']}", json={"text": "   "})
        assert resp.status_code == 200
        assert resp.get_json()["text"] == "Keep me too"

    def test_edit_with_nan_order_ignored(self, fresh_server):
        """NaN order values should be rejected safely."""
        r = _post_reminder(fresh_server, "Order test").get_json()
        resp = fresh_server.put(f"/api/reminders/{r['id']}", json={"order": float('nan')})
        assert resp.status_code == 200
        # NaN should be rejected by the NaN check in edit

    def test_edit_with_infinity_order_ignored(self, fresh_server):
        r = _post_reminder(fresh_server, "Inf test").get_json()
        resp = fresh_server.put(f"/api/reminders/{r['id']}", json={"order": float('inf')})
        assert resp.status_code == 200

    def test_delete_then_create_same_id(self, fresh_server):
        """Recreating a deleted reminder with the same ID should work."""
        _post_reminder(fresh_server, "First", id="reuse-id")
        fresh_server.delete("/api/reminders/reuse-id")
        resp = _post_reminder(fresh_server, "Second", id="reuse-id")
        assert resp.status_code == 201
        assert resp.get_json()["text"] == "Second"

    def test_complete_already_completed(self, fresh_server):
        """Completing a non-existent reminder (already completed) returns 404."""
        r = _post_reminder(fresh_server).get_json()
        _complete(fresh_server, r["id"])
        resp = _complete(fresh_server, r["id"])
        assert resp.status_code == 404

    def test_uncomplete_then_sync_doesnt_duplicate(self, fresh_server):
        """Uncompleted item then synced shouldn't appear twice."""
        _pair_device(fresh_server)
        r = _post_reminder(fresh_server, "No dup", id="nodup-1").get_json()
        _complete(fresh_server, "nodup-1")
        _uncomplete(fresh_server, "nodup-1")

        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        resp = fresh_server.post("/api/sync", json={
            "device_id": "test-device",
            "reminders": [{"id": "nodup-1", "text": "No dup", "created_at": now, "order": 0}],
            "completed": [],
        })
        merged = resp.get_json()
        reminder_ids = [r["id"] for r in merged["reminders"]]
        assert reminder_ids.count("nodup-1") == 1


# ── Pairing Edge Cases ───────────────────────────────────────────────

class TestPairingEdgeCases:
    def test_max_paired_devices(self, fresh_server):
        """Should reject pairing beyond MAX_PAIRED_DEVICES."""

        pairing._pair_attempts = 0
        pairing._pair_attempt_reset = None
        for i in range(10):
            pairing.paired_devices.add(f"device-{i}")
        code = fresh_server.post("/api/pair/generate").get_json()["code"]
        resp = fresh_server.post("/api/pair/validate", json={
            "code": code, "device_id": "device-11"
        })
        assert resp.status_code == 400

    def test_re_pair_existing_device(self, fresh_server):
        """Re-pairing an already paired device should succeed."""

        pairing._pair_attempts = 0
        pairing._pair_attempt_reset = None
        for i in range(10):
            pairing.paired_devices.add(f"device-{i}")
        code = fresh_server.post("/api/pair/generate").get_json()["code"]
        resp = fresh_server.post("/api/pair/validate", json={
            "code": code, "device_id": "device-0"
        })
        assert resp.status_code == 200

    def test_pairing_code_single_use(self, fresh_server):
        """Code should only work once."""

        pairing._pair_attempts = 0
        pairing._pair_attempt_reset = None
        code = fresh_server.post("/api/pair/generate").get_json()["code"]
        fresh_server.post("/api/pair/validate", json={"code": code})
        # Second attempt with same code — no active code
        resp = fresh_server.post("/api/pair/validate", json={"code": code})
        assert resp.status_code == 400

    def test_generate_replaces_previous_code(self, fresh_server):
        """Generating a new code should invalidate the old one."""

        pairing._pair_attempts = 0
        pairing._pair_attempt_reset = None
        code1 = fresh_server.post("/api/pair/generate").get_json()["code"]
        code2 = fresh_server.post("/api/pair/generate").get_json()["code"]
        if code1 != code2:
            resp = fresh_server.post("/api/pair/validate", json={"code": code1})
            assert resp.status_code == 400

    def test_unpair_nonexistent_device(self, fresh_server):
        resp = fresh_server.post("/api/pair/unpair", json={"device_id": "ghost"})
        assert resp.status_code in (200, 400)


# ── Config Edge Cases ────────────────────────────────────────────────

class TestConfigEdgeCases:
    def test_config_string_for_int_field(self, fresh_server):
        resp = fresh_server.put("/api/config", json={"popup_interval_minutes": "not a number"})
        assert resp.status_code == 400

    def test_config_negative_interval(self, fresh_server):
        resp = fresh_server.put("/api/config", json={"popup_interval_minutes": -5})
        assert resp.status_code == 400

    def test_config_float_interval(self, fresh_server):
        resp = fresh_server.put("/api/config", json={"popup_interval_minutes": 30.5})
        assert resp.status_code == 200
        config = fresh_server.get("/api/config").get_json()
        assert config["popup_interval_minutes"] == 30  # Should be int

    def test_config_port_at_boundaries(self, fresh_server):
        resp = fresh_server.put("/api/config", json={"server_port": 1024})
        assert resp.status_code == 200
        resp = fresh_server.put("/api/config", json={"server_port": 65535})
        assert resp.status_code == 200
        resp = fresh_server.put("/api/config", json={"server_port": 1023})
        assert resp.status_code == 400
        resp = fresh_server.put("/api/config", json={"server_port": 65536})
        assert resp.status_code == 400
