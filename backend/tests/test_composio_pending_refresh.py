"""Tests for the Composio Connections panel auto-refresh + age stamp.

Feb 2026 production bug: a Facebook OAuth grant completed server-side
but the UI was stuck on "Connecting…" because the post-callback poll
window (30s) was too short and `list_connections` returned cached
INITIATED status without ever re-pinging Composio.

Fix verified here:
  • `list_connections` inline-refreshes INITIATED rows older than
    `_STALE_INITIATED_SECONDS` (cap 5 per call).
  • Every INITIATED row gets a `pending_age_seconds` stamp the FE
    uses to render "Connecting… (3 min)" + a "taking too long" nudge.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import composio_integration as ci  # noqa: E402


class FakeCollection:
    def __init__(self, rows):
        # `rows` is the seed dataset; we mutate it on update_one so
        # refresh_connection_status can simulate flipping status.
        self._rows = list(rows)

    def find(self, query=None, projection=None):
        # Return a self-mimic that supports .sort().to_list(n).
        seq = list(self._rows)
        chain = _Cursor(seq)
        return chain

    async def find_one(self, query, projection=None):
        for r in self._rows:
            if all(r.get(k) == v for k, v in (query or {}).items()):
                return dict(r)
        return None

    async def update_one(self, query, update, upsert=False):
        # Apply $set to the first match.
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


class _Cursor:
    def __init__(self, seq):
        self.seq = seq
    def sort(self, *args, **kwargs):
        return self
    async def to_list(self, n):
        return [dict(r) for r in self.seq[:n]]


class FakeDB:
    def __init__(self, conn_rows):
        self.composio_connections = FakeCollection(conn_rows)


# ── pending_age_seconds is stamped on INITIATED rows ───────────────────────
@pytest.mark.asyncio
async def test_pending_age_seconds_is_stamped_on_initiated_row(monkeypatch):
    now = datetime.now(timezone.utc)
    db = FakeDB([{
        "connection_id": "fb-pending-1",
        "org_id": "org1",
        "user_id": "u1",
        "composio_user_id": "org1:u1",
        "toolkit": "facebook",
        "status": "INITIATED",
        "initiated_at": now - timedelta(seconds=20),  # NEW: < threshold
    }])
    # Block the auto-refresh path so we only test the stamp.
    monkeypatch.setattr(ci, "refresh_connection_status", AsyncMock())
    rows = await ci.list_connections(db, "org1", "u1")
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "INITIATED"
    assert "pending_age_seconds" in r
    # Within a few seconds of 20 — the exact value depends on test runtime.
    assert 15 <= r["pending_age_seconds"] <= 35


@pytest.mark.asyncio
async def test_active_row_has_no_pending_age_stamp(monkeypatch):
    """Don't pollute ACTIVE rows with a meaningless pending_age field."""
    now = datetime.now(timezone.utc)
    db = FakeDB([{
        "connection_id": "fb-active-1",
        "composio_user_id": "org1:u1",
        "toolkit": "gmail",
        "status": "ACTIVE",
        "initiated_at": now - timedelta(minutes=5),
        "activated_at": now - timedelta(minutes=4),
    }])
    monkeypatch.setattr(ci, "refresh_connection_status", AsyncMock())
    rows = await ci.list_connections(db, "org1", "u1")
    assert rows[0]["status"] == "ACTIVE"
    assert "pending_age_seconds" not in rows[0]


# ── stale INITIATED rows trigger inline refresh ────────────────────────────
@pytest.mark.asyncio
async def test_stale_initiated_row_triggers_inline_refresh(monkeypatch):
    """An INITIATED row older than _STALE_INITIATED_SECONDS should be
    auto-refreshed against Composio on the next list_connections call."""
    now = datetime.now(timezone.utc)
    age = ci._STALE_INITIATED_SECONDS + 30  # safely past the threshold
    db = FakeDB([{
        "connection_id": "fb-stale-1",
        "composio_user_id": "org1:u1",
        "toolkit": "facebook",
        "status": "INITIATED",
        "initiated_at": now - timedelta(seconds=age),
    }])
    refresh_mock = AsyncMock()
    monkeypatch.setattr(ci, "refresh_connection_status", refresh_mock)
    rows = await ci.list_connections(db, "org1", "u1")
    refresh_mock.assert_awaited_once_with(db, "fb-stale-1")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_fresh_initiated_row_skipped_for_refresh(monkeypatch):
    """A just-minted INITIATED row shouldn't trigger an inline refresh
    on the very next list — we'd just thrash Composio."""
    now = datetime.now(timezone.utc)
    db = FakeDB([{
        "connection_id": "fb-fresh-1",
        "composio_user_id": "org1:u1",
        "toolkit": "facebook",
        "status": "INITIATED",
        "initiated_at": now - timedelta(seconds=5),
    }])
    refresh_mock = AsyncMock()
    monkeypatch.setattr(ci, "refresh_connection_status", refresh_mock)
    await ci.list_connections(db, "org1", "u1")
    refresh_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_inline_refresh_capped_at_5(monkeypatch):
    """Don't fanout >5 refresh calls per list — caps Composio load when
    a user has many stale rows (e.g. a bot test left 20 in INITIATED)."""
    now = datetime.now(timezone.utc)
    seed = []
    for i in range(8):
        seed.append({
            "connection_id": f"stale-{i}",
            "composio_user_id": "org1:u1",
            "toolkit": "facebook",
            "status": "INITIATED",
            "initiated_at": now - timedelta(seconds=ci._STALE_INITIATED_SECONDS + 60),
        })
    db = FakeDB(seed)
    refresh_mock = AsyncMock()
    monkeypatch.setattr(ci, "refresh_connection_status", refresh_mock)
    await ci.list_connections(db, "org1", "u1")
    assert refresh_mock.await_count == 5


@pytest.mark.asyncio
async def test_inline_refresh_failure_does_not_break_list(monkeypatch):
    """If Composio 5xxs on the refresh, we should still return the
    cached rows (not crash the entire toolkits panel)."""
    now = datetime.now(timezone.utc)
    db = FakeDB([{
        "connection_id": "stale-x",
        "composio_user_id": "org1:u1",
        "toolkit": "facebook",
        "status": "INITIATED",
        "initiated_at": now - timedelta(seconds=ci._STALE_INITIATED_SECONDS + 60),
    }])
    monkeypatch.setattr(
        ci, "refresh_connection_status",
        AsyncMock(side_effect=RuntimeError("Composio 502")),
    )
    rows = await ci.list_connections(db, "org1", "u1")
    # Original row is still returned despite the refresh failure.
    assert len(rows) == 1
    assert rows[0]["connection_id"] == "stale-x"


# ── constant sanity ────────────────────────────────────────────────────────
def test_stale_threshold_is_reasonable():
    """60s is the sweet spot. <30s thrashes Composio; >5min defeats
    the purpose (user has already lost confidence by then)."""
    assert 30 <= ci._STALE_INITIATED_SECONDS <= 300
