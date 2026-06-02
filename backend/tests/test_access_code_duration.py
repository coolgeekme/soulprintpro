"""Regression tests for time-limited access codes (Jun 2026).

Feature: admin-issued access codes can carry a `grants_duration_days`
field. When a user redeems one, their `access_expires_at` is extended
by that many days (stacking from `max(current_expiry, now)`). After
expiry, a 7-day soft-block (read-only) grace window applies; after
that, the user is hard-cut.

Tests pin the pure-function helpers (`_user_has_access`, `access_state`,
`assert_full_access`) — no Mongo / FastAPI runtime needed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from access_codes import (
    _user_has_access,
    access_state,
    assert_full_access,
    ACCESS_GRACE_DAYS,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── _user_has_access ─────────────────────────────────────────────────

def test_user_has_access_perpetual() -> None:
    """A user with `access_granted_at` but no `access_expires_at` keeps
    perpetual access (the grandfathered / pre-duration-codes case)."""
    user = {"access_granted_at": _now() - timedelta(days=365)}
    allowed, reason = _user_has_access(user)
    assert allowed is True
    assert reason == "private_code"


def test_user_has_access_active_window() -> None:
    """Within the duration window → full access."""
    user = {
        "access_granted_at": _now() - timedelta(days=10),
        "access_expires_at": _now() + timedelta(days=20),
    }
    allowed, reason = _user_has_access(user)
    assert allowed is True
    assert reason == "private_code"


def test_user_has_access_in_grace_period() -> None:
    """Expired BUT within the 7-day grace → still allowed,
    reason flips to `grace_period`."""
    user = {
        "access_granted_at": _now() - timedelta(days=40),
        "access_expires_at": _now() - timedelta(days=3),
    }
    allowed, reason = _user_has_access(user)
    assert allowed is True
    assert reason == "grace_period"


def test_user_has_access_past_grace_period() -> None:
    """Past the 7-day grace → hard cut."""
    user = {
        "access_granted_at": _now() - timedelta(days=60),
        "access_expires_at": _now() - timedelta(days=ACCESS_GRACE_DAYS + 1),
    }
    allowed, reason = _user_has_access(user)
    assert allowed is False
    assert reason == "expired"


def test_user_has_access_superadmin_overrides_expiry() -> None:
    """Superadmins always pass, even with an expired window."""
    user = {
        "is_superadmin": True,
        "access_expires_at": _now() - timedelta(days=999),
    }
    allowed, reason = _user_has_access(user)
    assert allowed is True
    assert reason == "superadmin"


def test_user_has_access_naive_datetime_handled() -> None:
    """A `access_expires_at` without tzinfo (legacy data) must not
    crash the access check — we coerce to UTC."""
    naive_future = (_now() + timedelta(days=10)).replace(tzinfo=None)
    user = {
        "access_granted_at": _now(),
        "access_expires_at": naive_future,
    }
    allowed, reason = _user_has_access(user)
    assert allowed is True
    assert reason == "private_code"


# ── access_state ─────────────────────────────────────────────────────

def test_access_state_perpetual_user() -> None:
    user = {"access_granted_at": _now() - timedelta(days=100)}
    st = access_state(user)
    assert st["mode"] == "full"
    assert st["is_perpetual"] is True
    assert st["expires_at"] is None
    assert st["days_remaining"] is None


def test_access_state_active_window_days_remaining() -> None:
    user = {
        "access_granted_at": _now(),
        "access_expires_at": _now() + timedelta(days=30, hours=2),
    }
    st = access_state(user)
    assert st["mode"] == "full"
    assert st["is_perpetual"] is False
    assert st["days_remaining"] == 30  # rounds DOWN to whole days
    assert st["expires_at"] is not None
    assert st["in_grace"] is False


def test_access_state_in_grace_period_mode_readonly() -> None:
    """During grace, mode flips to `readonly` and the grace counter
    is exposed so the banner can render "5 days of read-only left"."""
    user = {
        "access_granted_at": _now() - timedelta(days=35),
        "access_expires_at": _now() - timedelta(days=2),
    }
    st = access_state(user)
    assert st["mode"] == "readonly"
    assert st["reason"] == "grace_period"
    assert st["in_grace"] is True
    # 7 - 2 = 5 days of grace remaining (allow off-by-one for clock skew)
    assert 4 <= st["grace_days_remaining"] <= 5
    assert st["days_remaining"] == 0


def test_access_state_past_grace_mode_blocked() -> None:
    user = {
        "access_granted_at": _now() - timedelta(days=90),
        "access_expires_at": _now() - timedelta(days=ACCESS_GRACE_DAYS + 5),
    }
    st = access_state(user)
    assert st["mode"] == "blocked"
    assert st["reason"] == "expired"
    assert st["days_remaining"] == 0
    assert st["in_grace"] is False


def test_access_state_superadmin_always_full_perpetual() -> None:
    user = {
        "is_superadmin": True,
        "access_expires_at": _now() - timedelta(days=999),
    }
    st = access_state(user)
    assert st["mode"] == "full"
    assert st["is_perpetual"] is True


# ── assert_full_access ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_assert_full_access_passes_for_full_mode() -> None:
    """Active user — no exception, returns None."""
    user = {
        "id": "5f5d3c4b2a1e9f8a7b6c5d4e",
        "access_granted_at": _now(),
        "access_expires_at": _now() + timedelta(days=30),
    }
    db = AsyncMock()
    db.users.find_one = AsyncMock(return_value=user)
    result = await assert_full_access(db, user)
    assert result is None


@pytest.mark.asyncio
async def test_assert_full_access_402_during_grace() -> None:
    """User in grace tries to perform a write — 402 + actionable copy."""
    user = {
        "id": "5f5d3c4b2a1e9f8a7b6c5d4e",
        "access_granted_at": _now() - timedelta(days=40),
        "access_expires_at": _now() - timedelta(days=2),
    }
    db = AsyncMock()
    db.users.find_one = AsyncMock(return_value=user)
    with pytest.raises(HTTPException) as exc_info:
        await assert_full_access(db, user)
    assert exc_info.value.status_code == 402
    assert "read-only" in exc_info.value.detail.lower()
    assert "extend access" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_assert_full_access_402_when_blocked() -> None:
    """Hard cut → 402 + "expired" copy."""
    user = {
        "id": "5f5d3c4b2a1e9f8a7b6c5d4e",
        "access_granted_at": _now() - timedelta(days=90),
        "access_expires_at": _now() - timedelta(days=30),
    }
    db = AsyncMock()
    db.users.find_one = AsyncMock(return_value=user)
    with pytest.raises(HTTPException) as exc_info:
        await assert_full_access(db, user)
    assert exc_info.value.status_code == 402
    assert "expired" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_assert_full_access_superadmin_passes_even_when_expired() -> None:
    user = {
        "id": "5f5d3c4b2a1e9f8a7b6c5d4e",
        "is_superadmin": True,
        "access_expires_at": _now() - timedelta(days=999),
    }
    db = AsyncMock()
    db.users.find_one = AsyncMock(return_value=user)
    assert await assert_full_access(db, user) is None
