"""Tests for the STUCK + zombie-prune Composio fixes.

Production bug (reported Feb 2026, second time): user's Facebook
connection sat on "Connecting…" forever even after the first round of
fixes shipped inline auto-refresh + a Cancel button. Root cause: when
Composio kept returning INITIATED past the OAuth grant deadline (popup
closed early, Meta never POSTed the callback, etc.), every refresh
just persisted "still INITIATED" with no terminal state, so the row
became a permanent zombie that needed manual deletion before "+ Add"
worked.

These tests cover the two-layer fix:

  Layer 1 — `initiate_connection` prunes stale INITIATED / STUCK rows
  for the same (composio_user_id, toolkit) tuple that are >120s old,
  so clicking "+ Add" after a stuck attempt always starts clean without
  the user manually clicking Cancel first.

  Layer 2 — `refresh_connection_status` detects a true zombie (status
  still INITIATED + age >=5min + refresh_attempts >=3) and flips it to
  a terminal STUCK status. The FE then surfaces it as a red "Connection
  failed — Retry" call-to-action instead of a misleading spinner.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import composio_integration as ci  # noqa: E402


# Reused minimal fakes from the round-1 test file — kept independent so
# this suite can be run on its own without test-collection ordering.
class FakeCollection:
    def __init__(self, rows=None):
        self._rows = list(rows or [])
        self.deleted_filters = []

    def find(self, *a, **kw):
        return _Cursor(list(self._rows))

    async def find_one(self, query, projection=None):
        for r in self._rows:
            if all(r.get(k) == v for k, v in (query or {}).items()):
                return dict(r)
        return None

    async def update_one(self, query, update, upsert=False):
        for r in self._rows:
            if all(r.get(k) == v for k, v in (query or {}).items()):
                for k, v in (update.get("$set") or {}).items():
                    r[k] = v
                return
        if upsert:
            new = {}
            new.update(query or {})
            new.update(update.get("$set") or {})
            self._rows.append(new)

    async def delete_many(self, query):
        self.deleted_filters.append(query)
        # Apply the deletion against in-memory rows.
        before = len(self._rows)
        def _matches(r):
            for k, v in (query or {}).items():
                if isinstance(v, dict) and "$in" in v:
                    if r.get(k) not in v["$in"]:
                        return False
                elif isinstance(v, dict) and "$lt" in v:
                    if not (r.get(k) and r[k] < v["$lt"]):
                        return False
                else:
                    if r.get(k) != v:
                        return False
            return True
        self._rows = [r for r in self._rows if not _matches(r)]
        return type("R", (), {"deleted_count": before - len(self._rows)})()


class _Cursor:
    def __init__(self, seq):
        self.seq = seq
    def sort(self, *a, **k):
        return self
    async def to_list(self, n):
        return [dict(r) for r in self.seq[:n]]


class FakeDB:
    def __init__(self, rows=None):
        self.composio_connections = FakeCollection(rows)


# ── Layer 1: zombie prune on initiate ───────────────────────────────────────
@pytest.mark.asyncio
async def test_initiate_prunes_stale_initiated_rows_for_same_toolkit(monkeypatch):
    now = datetime.now(timezone.utc)
    db = FakeDB([
        # Stale FB INITIATED row — must be pruned.
        {
            "connection_id": "fb-stuck", "composio_user_id": "org1:u1",
            "toolkit": "facebook", "status": "INITIATED",
            "initiated_at": now - timedelta(minutes=10),
        },
        # Fresh FB INITIATED row — too new to prune.
        {
            "connection_id": "fb-fresh", "composio_user_id": "org1:u1",
            "toolkit": "facebook", "status": "INITIATED",
            "initiated_at": now - timedelta(seconds=30),
        },
        # IG ACTIVE row — different toolkit, MUST be preserved.
        {
            "connection_id": "ig-active", "composio_user_id": "org1:u1",
            "toolkit": "instagram", "status": "ACTIVE",
            "initiated_at": now - timedelta(hours=1),
        },
        # FB ACTIVE row for a different user — MUST be preserved.
        {
            "connection_id": "fb-other-user-active", "composio_user_id": "org1:u2",
            "toolkit": "facebook", "status": "ACTIVE",
            "initiated_at": now - timedelta(hours=1),
        },
    ])
    # Stub Composio client + auth config + user-id helper.
    fake_req = type("R", (), {
        "id": "fb-new-attempt",
        "redirect_url": "https://composio.example/oauth",
        "status": "INITIATED",
    })()
    fake_client = MagicMock()
    fake_client.connected_accounts.link.return_value = fake_req
    monkeypatch.setattr(ci, "_get_client", lambda: fake_client)
    monkeypatch.setattr(ci, "get_auth_config_id", AsyncMock(return_value="auth-fb"))
    monkeypatch.setattr(ci, "composio_user_id", lambda o, u: "org1:u1")

    result = await ci.initiate_connection(
        db, "org1", "u1", "facebook",
        callback_url="https://app.example/cb",
    )
    assert result["connection_id"] == "fb-new-attempt"
    # Stale FB row is gone, fresh FB row still pending (it was too new to prune),
    # IG ACTIVE preserved, other-user's FB ACTIVE preserved.
    surviving = {r["connection_id"] for r in db.composio_connections._rows}
    assert "fb-stuck" not in surviving
    assert "fb-fresh" in surviving
    assert "ig-active" in surviving
    assert "fb-other-user-active" in surviving
    # New attempt was upserted.
    assert "fb-new-attempt" in surviving


@pytest.mark.asyncio
async def test_initiate_prunes_stuck_rows_too(monkeypatch):
    """STUCK rows are zombies too — they should be wiped on a fresh
    + Add click for the same toolkit so the user gets a clean retry."""
    now = datetime.now(timezone.utc)
    db = FakeDB([{
        "connection_id": "fb-stuck-terminal", "composio_user_id": "org1:u1",
        "toolkit": "facebook", "status": "STUCK",
        "initiated_at": now - timedelta(minutes=20),
    }])
    fake_req = type("R", (), {
        "id": "fb-retry", "redirect_url": "https://x", "status": "INITIATED",
    })()
    fake_client = MagicMock()
    fake_client.connected_accounts.link.return_value = fake_req
    monkeypatch.setattr(ci, "_get_client", lambda: fake_client)
    monkeypatch.setattr(ci, "get_auth_config_id", AsyncMock(return_value="auth-fb"))
    monkeypatch.setattr(ci, "composio_user_id", lambda o, u: "org1:u1")

    await ci.initiate_connection(db, "org1", "u1", "facebook", callback_url="x")
    surviving = {r["connection_id"] for r in db.composio_connections._rows}
    assert "fb-stuck-terminal" not in surviving
    assert "fb-retry" in surviving


# ── Layer 2: STUCK transition on refresh ───────────────────────────────────
@pytest.mark.asyncio
async def test_refresh_marks_old_initiated_row_as_stuck(monkeypatch):
    now = datetime.now(timezone.utc)
    db = FakeDB([{
        "connection_id": "fb-zombie",
        "composio_user_id": "org1:u1", "org_id": "org1", "user_id": "u1",
        "toolkit": "facebook",
        "status": "INITIATED",
        # 6 minutes old + 3 prior refresh attempts → exceeds threshold.
        "initiated_at": now - timedelta(minutes=6),
        "refresh_attempts": 2,
    }])
    fake_acct = MagicMock()
    fake_acct.status = "INITIATED"  # Composio still says INITIATED
    fake_acct.params = None
    fake_client = MagicMock()
    fake_client.connected_accounts.get.return_value = fake_acct
    monkeypatch.setattr(ci, "_get_client", lambda: fake_client)

    result = await ci.refresh_connection_status(db, "fb-zombie")
    assert result["status"] == "STUCK"
    # Persisted into the row.
    row = db.composio_connections._rows[0]
    assert row["status"] == "STUCK"
    assert row["refresh_attempts"] == 3


@pytest.mark.asyncio
async def test_refresh_does_not_mark_young_initiated_row_as_stuck(monkeypatch):
    """A row only 90s old must NOT be marked STUCK on its first refresh —
    that's still well within the OAuth grant deadline. Increment the
    counter but keep status INITIATED."""
    now = datetime.now(timezone.utc)
    db = FakeDB([{
        "connection_id": "fb-fresh",
        "composio_user_id": "org1:u1", "org_id": "org1", "user_id": "u1",
        "toolkit": "facebook", "status": "INITIATED",
        "initiated_at": now - timedelta(seconds=90),
        "refresh_attempts": 0,
    }])
    fake_acct = MagicMock()
    fake_acct.status = "INITIATED"
    fake_acct.params = None
    fake_client = MagicMock()
    fake_client.connected_accounts.get.return_value = fake_acct
    monkeypatch.setattr(ci, "_get_client", lambda: fake_client)

    result = await ci.refresh_connection_status(db, "fb-fresh")
    assert result["status"] == "INITIATED"
    row = db.composio_connections._rows[0]
    assert row["refresh_attempts"] == 1


@pytest.mark.asyncio
async def test_refresh_handles_composio_5xx_gracefully(monkeypatch):
    """Composio's connected_accounts.get sometimes 5xxs on freshly-minted
    connection ids (eventual consistency). The refresh must NOT crash —
    it should return the existing row's snapshot so the UI doesn't see
    a hard failure."""
    now = datetime.now(timezone.utc)
    db = FakeDB([{
        "connection_id": "fb-flaky",
        "composio_user_id": "org1:u1", "org_id": "org1", "user_id": "u1",
        "toolkit": "facebook", "status": "INITIATED",
        "initiated_at": now - timedelta(seconds=10),
    }])
    fake_client = MagicMock()
    fake_client.connected_accounts.get.side_effect = RuntimeError("Composio 502")
    monkeypatch.setattr(ci, "_get_client", lambda: fake_client)

    result = await ci.refresh_connection_status(db, "fb-flaky")
    # No exception raised; status passed through unchanged.
    assert result["status"] == "INITIATED"


# ── pending_age_seconds is stamped on STUCK rows too ───────────────────────
@pytest.mark.asyncio
async def test_list_connections_stamps_pending_age_on_stuck_rows(monkeypatch):
    """STUCK rows are pending in the user-facing sense — show the age
    so the FE can render 'failed 8 min ago' style copy."""
    now = datetime.now(timezone.utc)
    db = FakeDB([{
        "connection_id": "stuck-1",
        "composio_user_id": "org1:u1",
        "toolkit": "facebook",
        "status": "STUCK",
        "initiated_at": now - timedelta(minutes=8),
    }])
    # No refresh fired for STUCK (not in the auto-refresh path).
    monkeypatch.setattr(ci, "refresh_connection_status", AsyncMock())
    rows = await ci.list_connections(db, "org1", "u1")
    assert rows[0]["status"] == "STUCK"
    assert "pending_age_seconds" in rows[0]
    assert rows[0]["pending_age_seconds"] >= 480
