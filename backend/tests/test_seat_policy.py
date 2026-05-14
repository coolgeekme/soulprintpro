"""Seat-policy tests — verifies soft enforcement + the access-code boost.

Production rule (Feb 2026): Foundry doesn't block over-cap invites but
shows a warning. Owners who joined via a private/trial access code get
Temper-equivalent (5-seat) allowance for the lifetime of their code.
"""
from datetime import datetime, timedelta, timezone

import pytest

from seat_policy import (
    SEATS_INCLUDED,
    compute_seat_status,
    warning_for_status,
    _owner_access_code_expiry,
)


# Tiny in-memory fake of the four collections we touch. Saves us from
# having to spin up Motor for these pure-logic tests.
class _Fakes:
    def __init__(self):
        self.users = _FakeColl()
        self.organizations = _FakeColl()
        self.org_members = _FakeColl()
        self.access_codes = _FakeColl()
        self.invites = _FakeColl()


class _FakeColl:
    def __init__(self):
        self.rows = []

    async def find_one(self, q, projection=None):
        for r in self.rows:
            if all(r.get(k) == v for k, v in q.items()):
                return dict(r)
        return None

    async def count_documents(self, q):
        n = 0
        for r in self.rows:
            ok = True
            for k, v in q.items():
                if isinstance(v, dict) and "$ne" in v:
                    if r.get(k) == v["$ne"]:
                        ok = False; break
                elif r.get(k) != v:
                    ok = False; break
            if ok:
                n += 1
        return n


@pytest.fixture
def db():
    return _Fakes()


def _make_oid_pair():
    """Return (string_id, mock objectid-like). We use plain strings so
    compute_seat_status can build an ObjectId('hex') successfully."""
    # 24-char hex string accepted by bson.ObjectId
    return "5f9d5a9b1c9d440000000000"


@pytest.mark.asyncio
async def test_spark_org_no_boost_warns(db):
    oid = _make_oid_pair()
    db.organizations.rows = [{"_id": __import__("bson").ObjectId(oid), "plan": "spark"}]
    db.org_members.rows = [
        {"org_id": oid, "user_id": "u1", "role": "owner"},
        {"org_id": oid, "user_id": "u2", "role": "member"},
        {"org_id": oid, "user_id": "u3", "role": "member"},
    ]
    db.users.rows = [{"_id": __import__("bson").ObjectId("5f9d5a9b1c9d440000000001"), "email": "u1@x"}]
    status = await compute_seat_status(db, oid)
    assert status["plan"] == "spark"
    assert status["effective_plan"] == "spark"
    assert status["seats_included"] == 1
    assert status["seats_used"] == 3
    assert status["over_cap"] is True
    msg = warning_for_status(status)
    assert msg and "Upgrade to Temper" in msg


@pytest.mark.asyncio
async def test_access_code_boost_lifts_to_temper(db):
    bson = __import__("bson")
    oid = "5f9d5a9b1c9d440000000010"
    code_id = "5f9d5a9b1c9d440000000020"
    owner_uid = "5f9d5a9b1c9d440000000030"
    db.organizations.rows = [{"_id": bson.ObjectId(oid), "plan": "spark"}]
    db.org_members.rows = [{"org_id": oid, "user_id": owner_uid, "role": "owner"}]
    db.users.rows = [{
        "_id": bson.ObjectId(owner_uid),
        "access_code_id": code_id,
        "trial_expires_at": None,
    }]
    far_future = datetime.now(timezone.utc) + timedelta(days=180)
    db.access_codes.rows = [{
        "_id": bson.ObjectId(code_id),
        "is_active": True,
        "expires_at": far_future,
    }]
    status = await compute_seat_status(db, oid)
    assert status["plan"] == "spark"
    assert status["effective_plan"] == "temper", "boost should bump effective plan"
    assert status["seats_included"] == 5
    assert status["boost_source"] == "access_code"
    # Within the 5-seat boost allowance — no warning yet.
    assert warning_for_status(status) is None


@pytest.mark.asyncio
async def test_access_code_boost_expires_drops_back(db):
    bson = __import__("bson")
    oid = "5f9d5a9b1c9d440000000040"
    code_id = "5f9d5a9b1c9d440000000050"
    owner_uid = "5f9d5a9b1c9d440000000060"
    db.organizations.rows = [{"_id": bson.ObjectId(oid), "plan": "spark"}]
    db.org_members.rows = [{"org_id": oid, "user_id": owner_uid, "role": "owner"}]
    db.users.rows = [{
        "_id": bson.ObjectId(owner_uid),
        "access_code_id": code_id,
        "trial_expires_at": None,
    }]
    past = datetime.now(timezone.utc) - timedelta(days=1)
    db.access_codes.rows = [{
        "_id": bson.ObjectId(code_id),
        "is_active": True,
        "expires_at": past,
    }]
    status = await compute_seat_status(db, oid)
    assert status["effective_plan"] == "spark"
    assert status["boost_source"] is None


@pytest.mark.asyncio
async def test_temper_over_cap_with_boost_still_warns(db):
    bson = __import__("bson")
    oid = "5f9d5a9b1c9d440000000070"
    code_id = "5f9d5a9b1c9d440000000080"
    owner_uid = "5f9d5a9b1c9d440000000090"
    db.organizations.rows = [{"_id": bson.ObjectId(oid), "plan": "spark"}]
    members = [{"org_id": oid, "user_id": owner_uid, "role": "owner"}]
    members += [{"org_id": oid, "user_id": f"m{i}", "role": "member"} for i in range(6)]
    db.org_members.rows = members
    db.users.rows = [{"_id": bson.ObjectId(owner_uid), "access_code_id": code_id}]
    db.access_codes.rows = [{
        "_id": bson.ObjectId(code_id),
        "is_active": True,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
    }]
    status = await compute_seat_status(db, oid)
    assert status["seats_used"] == 7
    assert status["seats_included"] == 5  # Temper-equivalent boost
    assert status["over_cap"] is True
    msg = warning_for_status(status)
    assert msg is not None
    assert "access-code boost" in msg.lower(), \
        "should note the boost when warning a boosted Temper-equivalent over-cap"


def test_seat_constants_match_pricing_page():
    # If you change these, update PricingPage.js + CHANGELOG.
    assert SEATS_INCLUDED["spark"] == 1
    assert SEATS_INCLUDED["temper"] == 5
    assert SEATS_INCLUDED["refine"] >= 10_000
