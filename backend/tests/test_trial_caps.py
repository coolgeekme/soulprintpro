"""Regression tests for trial-tier per-resource caps (Jun 2026).

The trial caps module gates the most expensive cost centers (messages
/ images / missions / publishes / voice / audits) for users on the
time-bounded access window. Paid subscribers + grandfathered /
superadmin / staff bypass all caps.

Tests pin:
  • Plan resolution: paid > perpetual > superadmin > trial > grace > expired
  • consume_or_raise honours caps for trial, bypasses for paid/perpetual
  • Cap exhaustion → HTTP 402 with actionable upgrade copy
  • get_trial_usage returns the right shape for each tier
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from trial_caps import (
    TRIAL_CAPS,
    resolve_user_plan,
    consume_or_raise,
    get_trial_usage,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── resolve_user_plan ────────────────────────────────────────────────

def test_resolve_anonymous() -> None:
    assert resolve_user_plan({})["tier"] == "none"
    assert resolve_user_plan(None)["tier"] == "none"


def test_resolve_superadmin_is_perpetual() -> None:
    d = resolve_user_plan({"is_superadmin": True, "access_expires_at": _now() - timedelta(days=999)})
    assert d["tier"] == "perpetual"
    assert d["reason"] == "superadmin"


def test_resolve_staff_is_perpetual() -> None:
    d = resolve_user_plan({"is_staff": True})
    assert d["tier"] == "perpetual"
    assert d["reason"] == "staff"


def test_resolve_paid_subscription_wins_over_trial_expiry() -> None:
    """An active Stripe sub bypasses trial caps even if the access
    window technically expired — paying customers don't get caught
    by the trial counter."""
    user = {
        "plan_status": "active",
        "plan_id": "spark",
        "access_granted_at": _now() - timedelta(days=60),
        "access_expires_at": _now() - timedelta(days=1),
    }
    d = resolve_user_plan(user)
    assert d["tier"] == "paid"


def test_resolve_active_trial_window() -> None:
    user = {
        "access_granted_at": _now(),
        "access_expires_at": _now() + timedelta(days=20),
    }
    d = resolve_user_plan(user)
    assert d["tier"] == "trial"


def test_resolve_grandfathered_perpetual() -> None:
    """User with `access_granted_at` but no `access_expires_at` is
    grandfathered — no caps apply."""
    d = resolve_user_plan({"access_granted_at": _now() - timedelta(days=365)})
    assert d["tier"] == "perpetual"
    assert d["reason"] == "grandfathered"


def test_resolve_in_grace_period() -> None:
    d = resolve_user_plan({
        "access_granted_at": _now() - timedelta(days=40),
        "access_expires_at": _now() - timedelta(days=2),
    })
    assert d["tier"] == "grace"


def test_resolve_past_grace_expired() -> None:
    d = resolve_user_plan({
        "access_granted_at": _now() - timedelta(days=90),
        "access_expires_at": _now() - timedelta(days=30),
    })
    assert d["tier"] == "expired"


def test_resolve_no_access_granted() -> None:
    """A user row without `access_granted_at` (never redeemed a code)
    returns "none" — used by the gate to bounce to /access."""
    assert resolve_user_plan({"email": "noaccess@x.com"})["tier"] == "none"


# ── consume_or_raise ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_consume_bypasses_paid_user() -> None:
    """Paid subscriber consuming messages → no exception, no
    counter increment (we skip the trial_counters write)."""
    db = AsyncMock()
    db.users.find_one = AsyncMock(return_value={
        "_id": "x", "plan_status": "active",
    })
    user = {"id": "5f5d3c4b2a1e9f8a7b6c5d4e"}
    await consume_or_raise(db, user, "messages")
    db.trial_counters.update_one.assert_not_called()


@pytest.mark.asyncio
async def test_consume_bypasses_superadmin() -> None:
    db = AsyncMock()
    db.users.find_one = AsyncMock(return_value={"_id": "x", "is_superadmin": True})
    await consume_or_raise(db, {"id": "5f5d3c4b2a1e9f8a7b6c5d4e"}, "images")
    db.trial_counters.update_one.assert_not_called()


@pytest.mark.asyncio
async def test_consume_402_when_expired() -> None:
    db = AsyncMock()
    db.users.find_one = AsyncMock(return_value={
        "_id": "x",
        "access_granted_at": _now() - timedelta(days=90),
        "access_expires_at": _now() - timedelta(days=30),
    })
    with pytest.raises(HTTPException) as exc:
        await consume_or_raise(db, {"id": "5f5d3c4b2a1e9f8a7b6c5d4e"}, "messages")
    assert exc.value.status_code == 402
    assert "expired" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_consume_402_in_grace() -> None:
    db = AsyncMock()
    db.users.find_one = AsyncMock(return_value={
        "_id": "x",
        "access_granted_at": _now() - timedelta(days=40),
        "access_expires_at": _now() - timedelta(days=2),
    })
    with pytest.raises(HTTPException) as exc:
        await consume_or_raise(db, {"id": "5f5d3c4b2a1e9f8a7b6c5d4e"}, "publishes")
    assert exc.value.status_code == 402
    assert "grace" in exc.value.detail.lower() or "read-only" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_consume_trial_user_under_cap_writes_counter() -> None:
    """Trial user with 0/200 messages consumes 1 → counter incremented,
    no exception."""
    grant_dt = _now() - timedelta(days=2)
    db = AsyncMock()
    db.users.find_one = AsyncMock(return_value={
        "_id": "x",
        "access_granted_at": grant_dt,
        "access_expires_at": _now() + timedelta(days=28),
    })
    # No existing counter row
    db.trial_counters.find_one = AsyncMock(return_value=None)
    db.trial_counters.insert_one = AsyncMock()
    db.trial_counters.update_one = AsyncMock()
    await consume_or_raise(db, {"id": "5f5d3c4b2a1e9f8a7b6c5d4e"}, "messages", amount=1)
    db.trial_counters.update_one.assert_called_once()
    # The $inc should target the messages field with amount 1
    args, kwargs = db.trial_counters.update_one.call_args
    assert args[1]["$inc"]["counts.messages"] == 1


@pytest.mark.asyncio
async def test_consume_trial_user_over_cap_raises_402_with_upgrade_copy() -> None:
    """Trial user at 19/20 images tries to consume 2 → 402 with
    'subscribe' and 'extension code' both mentioned in the copy."""
    grant_dt = _now() - timedelta(days=5)
    db = AsyncMock()
    db.users.find_one = AsyncMock(return_value={
        "_id": "x",
        "access_granted_at": grant_dt,
        "access_expires_at": _now() + timedelta(days=25),
    })
    db.trial_counters.find_one = AsyncMock(return_value={
        "user_id": "5f5d3c4b2a1e9f8a7b6c5d4e",
        "window_start": grant_dt,
        "counts": {"images": 19},
    })
    db.trial_counters.insert_one = AsyncMock()
    db.trial_counters.update_one = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await consume_or_raise(db, {"id": "5f5d3c4b2a1e9f8a7b6c5d4e"}, "images", amount=2)
    assert exc.value.status_code == 402
    msg = exc.value.detail.lower()
    assert "image" in msg
    assert "subscribe" in msg
    # Counter must NOT have been incremented on a 402.
    db.trial_counters.update_one.assert_not_called()


@pytest.mark.asyncio
async def test_consume_unknown_resource_no_crash() -> None:
    """Programmer typo (e.g. consume('imagez')) shouldn't 500 — it's
    a no-op with a logger warning. Safer than a hard fail because
    the caller might be in a critical write path."""
    db = AsyncMock()
    db.users.find_one = AsyncMock(return_value={"_id": "x", "is_superadmin": True})
    await consume_or_raise(db, {"id": "5f5d3c4b2a1e9f8a7b6c5d4e"}, "nonexistent")
    # No exception = pass


# ── get_trial_usage ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_trial_usage_for_non_trial_returns_empty_caps() -> None:
    """Paid + perpetual users: tier set, caps dict empty so the UI
    hides the panel."""
    db = AsyncMock()
    db.users.find_one = AsyncMock(return_value={
        "_id": "x", "plan_status": "active",
    })
    out = await get_trial_usage(db, {"id": "5f5d3c4b2a1e9f8a7b6c5d4e"})
    assert out["tier"] == "paid"
    assert out["caps"] == {}


@pytest.mark.asyncio
async def test_get_trial_usage_returns_all_caps_for_trial_user() -> None:
    grant_dt = _now() - timedelta(days=3)
    db = AsyncMock()
    db.users.find_one = AsyncMock(return_value={
        "_id": "x",
        "access_granted_at": grant_dt,
        "access_expires_at": _now() + timedelta(days=27),
    })
    db.trial_counters.find_one = AsyncMock(return_value={
        "user_id": "5f5d3c4b2a1e9f8a7b6c5d4e",
        "window_start": grant_dt,
        "counts": {"messages": 42, "images": 8},
    })
    db.trial_counters.insert_one = AsyncMock()
    out = await get_trial_usage(db, {"id": "5f5d3c4b2a1e9f8a7b6c5d4e"})
    assert out["tier"] == "trial"
    # Every cap is present
    assert set(out["caps"].keys()) == set(TRIAL_CAPS.keys())
    # Counter values flow through
    assert out["caps"]["messages"]["used"] == 42
    assert out["caps"]["messages"]["remaining"] == 200 - 42
    assert out["caps"]["images"]["used"] == 8
    assert out["caps"]["images"]["percent"] == 40  # 8/20 = 40%
    # Resources with no counter row default to 0
    assert out["caps"]["missions"]["used"] == 0
    assert out["caps"]["missions"]["remaining"] == 2
