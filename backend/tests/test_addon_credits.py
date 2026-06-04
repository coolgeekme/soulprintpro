"""Tests for the add-on credit debit helpers in ``trial_caps.py``.

These prove:
  • ``_debit_credit`` is atomic and never goes below zero.
  • ``consume_credit_or_quota`` prefers credit over quota.
  • Once credit is exhausted it falls back to ``consume_or_raise``.
  • ``credit_browser_minutes`` has no fallback (silently allows overage).
  • ``consume_credit_after_use`` settles usage and drains gracefully.
  • Concurrent debits don't over-spend the balance (a quick smoke).
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import pytest_asyncio
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

import trial_caps


@pytest_asyncio.fixture
async def db():
    mongo_url = os.environ.get("MONGO_URL")
    if not mongo_url:
        pytest.skip("MONGO_URL not configured.")
    name = f"caps_{uuid.uuid4().hex[:8]}"
    client = AsyncIOMotorClient(mongo_url)
    db = client[name]
    yield db
    client.drop_database(name)
    client.close()


@pytest_asyncio.fixture
async def trial_user(db):
    """A trial-tier user — useful for testing quota fallback paths."""
    user_oid = ObjectId()
    await db.users.insert_one({
        "_id": user_oid,
        "email": f"trial_{uuid.uuid4().hex[:6]}@test.example",
        "plan": "spark",
        "credit_messages": 0,
        "credit_missions": 0,
        "credit_browser_minutes": 0,
    })
    return {
        "id": str(user_oid),
        "email": f"trial_{uuid.uuid4().hex[:6]}@test.example",
    }


async def _set_credit(db, user_id: str, field: str, amount: int) -> None:
    await db.users.update_one(
        {"_id": ObjectId(user_id)}, {"$set": {field: amount}},
    )


# ── _debit_credit ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_debit_credit_returns_amount_when_available(db, trial_user) -> None:
    await _set_credit(db, trial_user["id"], "credit_messages", 100)
    debited = await trial_caps._debit_credit(
        db, trial_user["id"], "credit_messages", amount=5,
    )
    assert debited == 5
    u = await db.users.find_one({"_id": ObjectId(trial_user["id"])})
    assert u["credit_messages"] == 95


@pytest.mark.asyncio
async def test_debit_credit_caps_at_available_balance(db, trial_user) -> None:
    """If we ask for 10 but only 3 are left, we debit 3, never -7."""
    await _set_credit(db, trial_user["id"], "credit_messages", 3)
    debited = await trial_caps._debit_credit(
        db, trial_user["id"], "credit_messages", amount=10,
    )
    assert debited == 3
    u = await db.users.find_one({"_id": ObjectId(trial_user["id"])})
    assert u["credit_messages"] == 0


@pytest.mark.asyncio
async def test_debit_credit_zero_balance_returns_zero(db, trial_user) -> None:
    debited = await trial_caps._debit_credit(
        db, trial_user["id"], "credit_messages", amount=1,
    )
    assert debited == 0


@pytest.mark.asyncio
async def test_debit_credit_concurrent_calls_dont_oversubtract(db, trial_user) -> None:
    """Two concurrent debits both succeed but combined ≤ available."""
    await _set_credit(db, trial_user["id"], "credit_messages", 10)
    results = await asyncio.gather(*[
        trial_caps._debit_credit(db, trial_user["id"], "credit_messages", 6)
        for _ in range(2)
    ])
    total = sum(results)
    assert total <= 10
    u = await db.users.find_one({"_id": ObjectId(trial_user["id"])})
    assert u["credit_messages"] == 10 - total
    assert u["credit_messages"] >= 0


# ── consume_credit_or_quota ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_consume_credit_or_quota_credit_skips_quota_check(
    db, trial_user,
) -> None:
    """Critical UX guarantee: a user with EXPIRED access who topped up
    a message pack must still be able to send messages — credit MUST
    completely satisfy the request without invoking ``consume_or_raise``.
    Otherwise users couldn't redeem the value they just paid for.
    """
    await _set_credit(db, trial_user["id"], "credit_messages", 100)
    # This user has no active plan / access_expires_at, so if we
    # accidentally fell through to consume_or_raise we'd get 402.
    # The fact that this call succeeds is the whole point of the credit.
    await trial_caps.consume_credit_or_quota(
        db, trial_user, "credit_messages", amount=1,
    )
    u = await db.users.find_one({"_id": ObjectId(trial_user["id"])})
    assert u["credit_messages"] == 99


@pytest.mark.asyncio
async def test_consume_credit_or_quota_prefers_credit(db, trial_user) -> None:
    """User has plenty of credit → no quota call required."""
    await _set_credit(db, trial_user["id"], "credit_messages", 100)
    await trial_caps.consume_credit_or_quota(
        db, trial_user, "credit_messages", amount=1,
    )
    u = await db.users.find_one({"_id": ObjectId(trial_user["id"])})
    assert u["credit_messages"] == 99


@pytest.mark.asyncio
async def test_consume_credit_or_quota_falls_back_to_quota(db, trial_user) -> None:
    """No credit → falls back to ``consume_or_raise`` for the matching
    resource. A user with no active plan/access raises HTTPException
    402 — proves the fallback path is actually invoked instead of
    silently letting the request through. (A real Spark subscriber
    would NOT raise — `consume_or_raise` returns early for non-trial
    tiers; this trial_user fixture has no subscription so it lands
    on the 'expired/none' branch.)"""
    from fastapi.exceptions import HTTPException
    with pytest.raises(HTTPException) as exc:
        await trial_caps.consume_credit_or_quota(
            db, trial_user, "credit_messages", amount=1,
        )
    assert exc.value.status_code == 402
    u = await db.users.find_one({"_id": ObjectId(trial_user["id"])})
    assert u["credit_messages"] == 0


@pytest.mark.asyncio
async def test_consume_credit_or_quota_browser_minutes_no_quota_fallback(
    db, trial_user,
) -> None:
    """`credit_browser_minutes` has no quota-fallback mapping —
    overages are silently allowed. Verifies we don't raise even with
    zero balance + amount > 0."""
    # Should NOT raise — silent overage.
    await trial_caps.consume_credit_or_quota(
        db, trial_user, "credit_browser_minutes", amount=42,
    )
    u = await db.users.find_one({"_id": ObjectId(trial_user["id"])})
    assert u["credit_browser_minutes"] == 0


# ── consume_credit_after_use ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_consume_credit_after_use_drains_partial_balance(db, trial_user) -> None:
    """If the user only has 7 left and we ask for 30, we drain 7 and
    don't claw back — the user already got the value."""
    await _set_credit(db, trial_user["id"], "credit_browser_minutes", 7)
    debited = await trial_caps.consume_credit_after_use(
        db, trial_user, "credit_browser_minutes", amount=30,
    )
    assert debited == 7
    u = await db.users.find_one({"_id": ObjectId(trial_user["id"])})
    assert u["credit_browser_minutes"] == 0


@pytest.mark.asyncio
async def test_consume_credit_after_use_full_debit(db, trial_user) -> None:
    await _set_credit(db, trial_user["id"], "credit_browser_minutes", 100)
    debited = await trial_caps.consume_credit_after_use(
        db, trial_user, "credit_browser_minutes", amount=25,
    )
    assert debited == 25
    u = await db.users.find_one({"_id": ObjectId(trial_user["id"])})
    assert u["credit_browser_minutes"] == 75


@pytest.mark.asyncio
async def test_consume_credit_after_use_zero_amount_noop(db, trial_user) -> None:
    """A no-op call (e.g. settling a 0-second mission) must not write."""
    await _set_credit(db, trial_user["id"], "credit_browser_minutes", 5)
    debited = await trial_caps.consume_credit_after_use(
        db, trial_user, "credit_browser_minutes", amount=0,
    )
    assert debited == 0
    u = await db.users.find_one({"_id": ObjectId(trial_user["id"])})
    assert u["credit_browser_minutes"] == 5


# ── get_addon_credit_balances ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_addon_credit_balances_returns_all_fields(db, trial_user) -> None:
    await _set_credit(db, trial_user["id"], "credit_messages", 42)
    await _set_credit(db, trial_user["id"], "credit_missions", 3)
    await _set_credit(db, trial_user["id"], "credit_browser_minutes", 17)
    balances = await trial_caps.get_addon_credit_balances(db, trial_user["id"])
    assert balances["credit_messages"] == 42
    assert balances["credit_missions"] == 3
    assert balances["credit_browser_minutes"] == 17
    # Extra counters default to 0 when unset.
    assert balances["extra_orgs"] == 0
    assert balances["extra_seats"] == 0


@pytest.mark.asyncio
async def test_get_addon_credit_balances_handles_missing_user() -> None:
    """An invalid / missing user id returns zeros, not an exception —
    keeps the billing page from crashing if the user row was just
    deleted (privacy hard-delete race)."""
    mongo_url = os.environ.get("MONGO_URL")
    if not mongo_url:
        pytest.skip("MONGO_URL not configured.")
    client = AsyncIOMotorClient(mongo_url)
    db = client[f"caps_missing_{uuid.uuid4().hex[:6]}"]
    try:
        balances = await trial_caps.get_addon_credit_balances(db, "not-an-objectid")
        assert balances == {
            "credit_messages": 0, "credit_missions": 0,
            "credit_browser_minutes": 0, "extra_orgs": 0, "extra_seats": 0,
        }
    finally:
        client.drop_database(db.name)
        client.close()
