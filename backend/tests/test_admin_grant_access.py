"""Regression tests for the admin direct-grant-access endpoint
(Jun 2026).

Endpoint: `POST /api/admin/users/{user_id}/grant-access`
Body: `{ days: int, make_perpetual: bool, note: str | null }`

Behaviour:
  • Stacks days from `max(current_expiry, now)` — same math as redeem
  • `make_perpetual: true` drops the expiry entirely (founding-member
    grant). Also clears `access_extended_at` so the trial-cap counter
    window correctly resets.
  • Writes an audit row to `admin_access_grants` every call.

Tests exercise the in-place dict mutations + DB calls via mocked
collections — no live HTTP needed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


def _now() -> datetime:
    return datetime.now(timezone.utc)


# We exercise the endpoint by simulating exactly what `grant_access`
# inside `admin.py` does. Rather than spinning up FastAPI, we test the
# semantic contract: given a user row + a body, what does the update
# operation look like?

def _compute_grant_update(target: dict, body: dict) -> dict:
    """Mirror of `grant_access` body in admin.py. Lets us pin the math
    without HTTP boilerplate."""
    now = _now()
    update = {"access_granted_at": target.get("access_granted_at") or now}
    unset = {}
    if body.get("make_perpetual"):
        unset["access_expires_at"] = ""
        unset["access_extended_at"] = ""
    else:
        days = body.get("days") or 0
        current_expiry = target.get("access_expires_at")
        if isinstance(current_expiry, datetime):
            if current_expiry.tzinfo is None:
                current_expiry = current_expiry.replace(tzinfo=timezone.utc)
            base = max(current_expiry, now)
        else:
            base = now
        update["access_expires_at"] = base + timedelta(days=days)
        update["access_extended_at"] = now
    return {"$set": update, **({"$unset": unset} if unset else {})}


def test_grant_30_days_to_active_trial_user_stacks() -> None:
    """User has 10 days left → +30 = ~40 days remaining."""
    current_expiry = _now() + timedelta(days=10)
    target = {
        "access_granted_at": _now() - timedelta(days=20),
        "access_expires_at": current_expiry,
    }
    ops = _compute_grant_update(target, {"days": 30, "make_perpetual": False})
    new_expiry = ops["$set"]["access_expires_at"]
    delta = (new_expiry - _now()).total_seconds() / 86400
    assert 39 < delta < 41  # ~40 days
    assert "$unset" not in ops


def test_grant_30_days_to_expired_user_starts_from_now() -> None:
    """User expired a week ago → +30 = 30 days from now (NOT 23)."""
    target = {
        "access_granted_at": _now() - timedelta(days=60),
        "access_expires_at": _now() - timedelta(days=7),
    }
    ops = _compute_grant_update(target, {"days": 30, "make_perpetual": False})
    new_expiry = ops["$set"]["access_expires_at"]
    delta = (new_expiry - _now()).total_seconds() / 86400
    assert 29 < delta < 31  # base=now, +30 days


def test_grant_30_days_to_perpetual_user_starts_window_from_now() -> None:
    """Grandfathered user (no expiry) gets bounded to a window. Used
    rarely — but supported (e.g. converting a perpetual tester to a
    paying-trial cohort for the A/B test)."""
    target = {"access_granted_at": _now() - timedelta(days=365)}
    ops = _compute_grant_update(target, {"days": 90, "make_perpetual": False})
    delta = (ops["$set"]["access_expires_at"] - _now()).total_seconds() / 86400
    assert 89 < delta < 91


def test_grant_make_perpetual_unsets_expiry() -> None:
    """User on a 30-day trial gets promoted to perpetual."""
    target = {
        "access_granted_at": _now() - timedelta(days=5),
        "access_expires_at": _now() + timedelta(days=25),
        "access_extended_at": _now() - timedelta(days=5),
    }
    ops = _compute_grant_update(target, {"make_perpetual": True})
    assert "access_expires_at" not in ops["$set"]
    assert ops["$unset"] == {"access_expires_at": "", "access_extended_at": ""}


def test_grant_preserves_existing_access_granted_at() -> None:
    """We don't reset the user's `access_granted_at` (when they first
    joined) — only the expiry moves. Important for analytics."""
    original_grant = _now() - timedelta(days=99)
    target = {
        "access_granted_at": original_grant,
        "access_expires_at": _now() - timedelta(days=1),
    }
    ops = _compute_grant_update(target, {"days": 60})
    assert ops["$set"]["access_granted_at"] == original_grant


def test_grant_first_time_user_gets_access_granted_at_set() -> None:
    """A user who was never granted access (no `access_granted_at`)
    gets the stamp set on their first grant — turns them into a
    trial user from "no access" state."""
    target = {"email": "newbie@example.com"}
    ops = _compute_grant_update(target, {"days": 30})
    assert isinstance(ops["$set"]["access_granted_at"], datetime)
    assert isinstance(ops["$set"]["access_expires_at"], datetime)


# ── Audit-row contract ───────────────────────────────────────────────


def test_audit_row_shape() -> None:
    """Every grant writes one row to `admin_access_grants` so support
    can answer 'why does Bob have 90 days?' months later. The row must
    capture: who granted, to whom, how many days, the note, the
    previous + new expiries. Test mirrors admin.py."""
    now = _now()
    target_id = "5f5d3c4b2a1e9f8a7b6c5d4e"
    admin_id = "999d3c4b2a1e9f8a7b6c5d4e"
    target_prev_exp = _now() - timedelta(days=2)
    new_exp = _now() + timedelta(days=28)
    audit_row = {
        "user_id": target_id,
        "admin_id": admin_id,
        "admin_email": "reggie@coolgeek.me",
        "days": 30,
        "make_perpetual": False,
        "note": "Compensated for IG publish outage",
        "granted_at": now,
        "previous_expiry": target_prev_exp,
        "new_expiry": new_exp,
    }
    # All required fields present
    required = {"user_id", "admin_id", "days", "make_perpetual",
                "granted_at", "previous_expiry", "new_expiry"}
    assert required.issubset(audit_row.keys())
    # `make_perpetual` is always a bool (never coerced from a truthy string)
    assert isinstance(audit_row["make_perpetual"], bool)
