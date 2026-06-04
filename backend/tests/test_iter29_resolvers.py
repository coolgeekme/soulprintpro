"""Iteration-29: resolver + seat policy wiring verification.

Validates the review-request claims that aren't already covered by
test_addons.py / test_addon_credits.py:

  • user_plan_limits lifts orgs_limit for Spark with extra_orgs.
  • user_plan_limits does NOT lift for Refine (already unlimited).
  • compute_seat_status reads owner.extra_seats and adds to seats_included.
  • compute_seat_status returns `extra_seats` field in dict.

We also verify the live /api/billing/plans surface for `mission_pack`
schema (id/name/price/unit/cadence) and the absence of `audit_pack`.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
import requests
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

import billing
import seat_policy

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Fall back to the frontend env file the same way the helper script does.
    try:
        with open("/app/frontend/.env") as fh:
            for line in fh:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                    break
    except Exception:
        BASE_URL = ""


@pytest_asyncio.fixture
async def db():
    mongo_url = os.environ.get("MONGO_URL")
    if not mongo_url:
        pytest.skip("MONGO_URL not configured.")
    name = f"iter29_{uuid.uuid4().hex[:8]}"
    client = AsyncIOMotorClient(mongo_url)
    db = client[name]
    yield db
    client.drop_database(name)
    client.close()


# ── Catalogue / summary surface (live HTTP) ─────────────────────────────


def test_plans_catalogue_has_mission_pack_no_audit_pack():
    if not BASE_URL:
        pytest.skip("REACT_APP_BACKEND_URL not configured.")
    r = requests.get(f"{BASE_URL}/api/billing/plans", timeout=15)
    assert r.status_code == 200
    addons = r.json().get("addons") or []
    ids = [a["id"] for a in addons]
    assert "mission_pack" in ids, f"mission_pack missing from {ids}"
    assert "audit_pack" not in ids, "Legacy audit_pack must be removed"
    mp = next(a for a in addons if a["id"] == "mission_pack")
    assert mp["name"] == "Mission pack"
    assert mp["price_usd"] == 10.0
    assert mp["unit"] == "/ 10 missions"
    assert mp["cadence"] == "one_time"


# ── Resolver: user_plan_limits with extra_orgs ──────────────────────────


@pytest.mark.asyncio
async def test_user_plan_limits_spark_lifts_orgs_with_extra_orgs(db, monkeypatch):
    """Spark user with extra_orgs=2 → orgs_limit = 1 + 2 = 3."""
    uid = ObjectId()
    user_id = str(uid)
    await db.users.insert_one({
        "_id": uid,
        "email": f"sp_{uuid.uuid4().hex[:6]}@test.example",
        "plan": "spark",
        "extra_orgs": 2,
    })
    limits = await billing.user_plan_limits(db, user_id, f"sp_{uid}@x.test")
    assert limits["plan_id"] == "spark"
    assert limits["orgs_limit"] == 3
    assert limits["extra_orgs"] == 2
    assert limits["is_staff"] is False


@pytest.mark.asyncio
async def test_user_plan_limits_no_extra_orgs_defaults_to_native(db):
    uid = ObjectId()
    await db.users.insert_one({
        "_id": uid,
        "email": f"sp2_{uuid.uuid4().hex[:6]}@test.example",
        "plan": "spark",
    })
    limits = await billing.user_plan_limits(db, str(uid), "sp2@x.test")
    assert limits["orgs_limit"] == billing.PLANS["spark"]["orgs_included"]
    assert limits["extra_orgs"] == 0


@pytest.mark.asyncio
async def test_user_plan_limits_refine_ignores_extra_orgs(db):
    """Refine is unlimited; extra_orgs MUST NOT be stacked on top."""
    uid = ObjectId()
    await db.users.insert_one({
        "_id": uid,
        "email": f"re_{uuid.uuid4().hex[:6]}@test.example",
        "plan": "refine",
        "extra_orgs": 5,
    })
    limits = await billing.user_plan_limits(db, str(uid), "re@x.test")
    assert limits["plan_id"] == "refine"
    assert limits["orgs_limit"] == billing.PLANS["refine"]["orgs_included"]
    # extra_orgs in dict should be 0 for refine (we don't read it)
    assert limits["extra_orgs"] == 0


# ── Seat policy: compute_seat_status with owner.extra_seats ─────────────


@pytest.mark.asyncio
async def test_compute_seat_status_lifts_with_extra_seats(db):
    """Spark org whose owner has extra_seats=3 → seats_included = 4."""
    owner_uid = ObjectId()
    owner_id = str(owner_uid)
    org_oid = ObjectId()
    org_id = str(org_oid)

    await db.users.insert_one({
        "_id": owner_uid,
        "email": f"own_{uuid.uuid4().hex[:6]}@test.example",
        "plan": "spark",
        "extra_seats": 3,
    })
    await db.organizations.insert_one({
        "_id": org_oid,
        "name": "Test Org",
        "plan": "spark",
    })
    await db.org_members.insert_one({
        "org_id": org_id,
        "user_id": owner_id,
        "role": "owner",
        "status": "active",
    })

    status = await seat_policy.compute_seat_status(db, org_id)
    assert status["seats_included"] == 4, (
        f"expected 1 base + 3 extras = 4, got {status['seats_included']}"
    )
    assert status["extra_seats"] == 3
    assert status["seats_used"] == 1
    assert status["over_cap"] is False


@pytest.mark.asyncio
async def test_compute_seat_status_no_extra_seats_returns_base(db):
    owner_uid = ObjectId()
    owner_id = str(owner_uid)
    org_oid = ObjectId()
    org_id = str(org_oid)
    await db.users.insert_one({
        "_id": owner_uid,
        "email": f"own2_{uuid.uuid4().hex[:6]}@test.example",
        "plan": "spark",
    })
    await db.organizations.insert_one({
        "_id": org_oid, "name": "Org2", "plan": "spark",
    })
    await db.org_members.insert_one({
        "org_id": org_id, "user_id": owner_id, "role": "owner", "status": "active",
    })
    status = await seat_policy.compute_seat_status(db, org_id)
    assert status["seats_included"] == seat_policy.SEATS_INCLUDED["spark"]
    assert status["extra_seats"] == 0
