"""Regression test for the Feb 19 2026 production bug where
`list_connections` blew up on naive datetimes returned by MongoDB.

MongoDB stores BSON datetimes as UTC but returns them as **naive**
Python datetime objects by default (Motor doesn't enable
`tz_aware=True`). Subtracting an aware `datetime.now(timezone.utc)`
from a naive value raises `TypeError`. Until this fix, the production
Connections panel showed nothing but the soft-error banner on every
load because every existing row was naive.

These tests reproduce the case using naive datetimes — pre-fix they'd
crash, post-fix they hydrate cleanly.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import composio_integration as ci  # noqa: E402


class FakeCollection:
    def __init__(self, rows):
        self._rows = list(rows)
    def find(self, *a, **kw):
        return _Cursor(self._rows)
    async def find_one(self, q, projection=None):
        return None
    async def update_one(self, *a, **k):
        return None


class _Cursor:
    def __init__(self, seq):
        self.seq = seq
    def sort(self, *a, **k): return self
    async def to_list(self, n): return [dict(r) for r in self.seq[:n]]


class FakeDB:
    def __init__(self, rows):
        self.composio_connections = FakeCollection(rows)


# ── Naive-datetime regression ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_list_connections_handles_naive_datetimes_from_mongo(monkeypatch):
    """Before the fix, this case raised TypeError and the whole panel went blank."""
    # Naive datetime — exactly what Motor returns from BSON by default.
    naive_now = datetime.utcnow()
    rows = [
        {
            "connection_id": "fb-1", "composio_user_id": "org1:u1",
            "toolkit": "facebook", "status": "INITIATED",
            "initiated_at": naive_now - timedelta(minutes=10),  # naive!
        },
        {
            "connection_id": "ig-1", "composio_user_id": "org1:u1",
            "toolkit": "instagram", "status": "ACTIVE",
            "initiated_at": naive_now - timedelta(hours=1),     # naive!
            "activated_at": naive_now - timedelta(minutes=58),  # naive!
            "instagram_username": "azpaindocs",
        },
    ]
    db = FakeDB(rows)
    monkeypatch.setattr(ci, "refresh_connection_status", AsyncMock())

    # This must NOT raise.
    result = await ci.list_connections(db, "org1", "u1")
    assert len(result) == 2
    # The IG row's username surfaces as display_label.
    ig = next(r for r in result if r["toolkit"] == "instagram")
    assert ig["display_label"] == "@azpaindocs"
    # Stale FB row gets the pending_age_seconds stamp.
    fb = next(r for r in result if r["toolkit"] == "facebook")
    assert "pending_age_seconds" in fb
    assert fb["pending_age_seconds"] >= 590  # 10 min - a few sec for clock drift
    # All datetimes serialised to ISO strings (JSON-safe).
    assert isinstance(fb["initiated_at"], str)
    assert isinstance(ig["activated_at"], str)


@pytest.mark.asyncio
async def test_one_malformed_row_does_not_tank_the_whole_list(monkeypatch):
    """If a single row throws during hydration (e.g. a corrupted display
    field), the rest of the list still renders."""
    naive_now = datetime.utcnow()
    # Construct a row that'll trigger an exception in our hydrator by
    # making `instagram_username` a non-string that breaks f-string.
    class _BoomString:
        def __str__(self):
            raise RuntimeError("boom")
        def __repr__(self):
            return "<boom>"

    rows = [
        {
            "connection_id": "bad-row", "composio_user_id": "org1:u1",
            "toolkit": "instagram", "status": "ACTIVE",
            "initiated_at": naive_now - timedelta(hours=1),
            "instagram_username": _BoomString(),
        },
        {
            "connection_id": "good-row", "composio_user_id": "org1:u1",
            "toolkit": "gmail", "status": "ACTIVE",
            "initiated_at": naive_now - timedelta(hours=2),
        },
    ]
    db = FakeDB(rows)
    monkeypatch.setattr(ci, "refresh_connection_status", AsyncMock())

    result = await ci.list_connections(db, "org1", "u1")
    # Both rows survive — bad one as fallback, good one fully hydrated.
    assert len(result) == 2
    ids = {r["connection_id"] for r in result}
    assert ids == {"bad-row", "good-row"}
    good = next(r for r in result if r["connection_id"] == "good-row")
    assert good["toolkit_name"] == "Gmail"


@pytest.mark.asyncio
async def test_ensure_aware_handles_none_and_aware_inputs():
    """The helper must be safe on every shape: None passthrough,
    already-aware passthrough, naive → UTC-aware."""
    assert ci._ensure_aware(None) is None
    naive = datetime(2026, 1, 1, 12, 0, 0)
    aware = ci._ensure_aware(naive)
    assert aware is not None
    assert aware.tzinfo == timezone.utc
    aware_in = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    aware_out = ci._ensure_aware(aware_in)
    assert aware_out == aware_in
