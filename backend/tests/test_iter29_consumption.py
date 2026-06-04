"""Iter-29: verify mission consumption and browser-minute settlement
plug into the credit system end-to-end (no live HTTP needed).

We invoke the helpers directly so we exercise the wiring without
booting the full FastAPI app — and so the test stays deterministic
regardless of any preview-wide setup.
"""
from __future__ import annotations

import math
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

import missions as missions_mod
import trial_caps


@pytest_asyncio.fixture
async def db():
    mongo_url = os.environ.get("MONGO_URL")
    if not mongo_url:
        pytest.skip("MONGO_URL not configured.")
    name = f"iter29_consume_{uuid.uuid4().hex[:8]}"
    client = AsyncIOMotorClient(mongo_url)
    db = client[name]
    yield db
    client.drop_database(name)
    client.close()


# ── credit_missions decrement on POST /missions (helper level) ──────────


@pytest.mark.asyncio
async def test_consume_credit_or_quota_decrements_missions(db):
    """Mirrors what start_mission's first line does. With balance=5,
    one debit drops it to 4."""
    uid = ObjectId()
    await db.users.insert_one({
        "_id": uid,
        "email": f"m_{uuid.uuid4().hex[:6]}@test.example",
        "plan": "spark",
        "credit_missions": 5,
    })
    u = {"id": str(uid), "email": "m@x.test"}
    await trial_caps.consume_credit_or_quota(db, u, "credit_missions", amount=1)
    after = await db.users.find_one({"_id": uid})
    assert after["credit_missions"] == 4


@pytest.mark.asyncio
async def test_consume_credit_or_quota_messages_decrements(db):
    uid = ObjectId()
    await db.users.insert_one({
        "_id": uid,
        "email": f"mm_{uuid.uuid4().hex[:6]}@test.example",
        "plan": "spark",
        "credit_messages": 10,
    })
    u = {"id": str(uid), "email": "mm@x.test"}
    await trial_caps.consume_credit_or_quota(db, u, "credit_messages", amount=1)
    after = await db.users.find_one({"_id": uid})
    assert after["credit_messages"] == 9


@pytest.mark.asyncio
async def test_zero_credit_messages_triggers_402_for_no_plan_user(db):
    from fastapi.exceptions import HTTPException
    uid = ObjectId()
    await db.users.insert_one({
        "_id": uid,
        "email": f"z_{uuid.uuid4().hex[:6]}@test.example",
        # NO plan / access_expires_at — fallback should fire.
        "credit_messages": 0,
    })
    u = {"id": str(uid), "email": "z@x.test"}
    with pytest.raises(HTTPException) as exc:
        await trial_caps.consume_credit_or_quota(db, u, "credit_messages", amount=1)
    assert exc.value.status_code == 402


# ── _settle_browser_minutes idempotency + ceil math ─────────────────────


@pytest.mark.asyncio
async def test_settle_browser_minutes_ceil_and_owner_charge(db):
    """A mission that ran for 90 seconds → ceil(90/60) = 2 minutes.
    The OWNER's credit_browser_minutes drops by min(balance, 2)."""
    owner_uid = ObjectId()
    owner_id = str(owner_uid)
    org_oid = ObjectId()
    org_id = str(org_oid)
    invoker_id = str(ObjectId())  # different user invoked it

    await db.users.insert_one({
        "_id": owner_uid,
        "email": f"own_{uuid.uuid4().hex[:6]}@test.example",
        "plan": "spark",
        "credit_browser_minutes": 50,
    })
    await db.organizations.insert_one({
        "_id": org_oid, "plan": "spark", "name": "X",
    })
    await db.org_members.insert_one({
        "org_id": org_id, "user_id": owner_id, "role": "owner", "status": "active",
    })

    mission_id = str(uuid.uuid4())
    started = datetime.now(timezone.utc) - timedelta(seconds=90)
    finished = datetime.now(timezone.utc)
    await db.missions.insert_one({
        "id": mission_id,
        "org_id": org_id,
        "invoker_id": invoker_id,
        "started_at": started,
        "finished_at": finished,
        "status": "finished",
    })

    await missions_mod._settle_browser_minutes(db, mission_id)
    m = await db.missions.find_one({"id": mission_id})
    assert m["browser_minutes_settled"] is True
    expected = math.ceil(90 / 60.0)
    assert m["browser_minutes_used"] >= expected
    owner_doc = await db.users.find_one({"_id": owner_uid})
    assert owner_doc["credit_browser_minutes"] == 50 - expected

    # Idempotency: second call MUST NOT change balance.
    await missions_mod._settle_browser_minutes(db, mission_id)
    owner_doc2 = await db.users.find_one({"_id": owner_uid})
    assert owner_doc2["credit_browser_minutes"] == 50 - expected


@pytest.mark.asyncio
async def test_settle_browser_minutes_partial_drain_no_clawback(db):
    """Owner has 1 credit_browser_minutes left but the mission cost 5.
    Drain to 0, never below."""
    owner_uid = ObjectId()
    owner_id = str(owner_uid)
    org_oid = ObjectId()
    org_id = str(org_oid)

    await db.users.insert_one({
        "_id": owner_uid,
        "email": f"own_{uuid.uuid4().hex[:6]}@test.example",
        "plan": "spark",
        "credit_browser_minutes": 1,
    })
    await db.organizations.insert_one({
        "_id": org_oid, "plan": "spark", "name": "Y",
    })
    await db.org_members.insert_one({
        "org_id": org_id, "user_id": owner_id, "role": "owner", "status": "active",
    })

    mission_id = str(uuid.uuid4())
    started = datetime.now(timezone.utc) - timedelta(seconds=300)  # 5 min
    finished = datetime.now(timezone.utc)
    await db.missions.insert_one({
        "id": mission_id, "org_id": org_id,
        "started_at": started, "finished_at": finished, "status": "finished",
    })

    await missions_mod._settle_browser_minutes(db, mission_id)
    owner_doc = await db.users.find_one({"_id": owner_uid})
    assert owner_doc["credit_browser_minutes"] == 0
    m = await db.missions.find_one({"id": mission_id})
    assert m["browser_minutes_settled"] is True
    assert m["browser_minutes_used"] >= 5
