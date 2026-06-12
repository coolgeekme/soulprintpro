"""Iter-33 backend tests — Org Ownership Transfer Flow.

State machine: outgoing owner initiates → recipient accepts/declines (or
outgoing cancels) → role mutation. Plan tier gate routes the recipient
to Stripe upgrade (402) if their plan doesn't cover org member count.

Tests cover:
  • Create pending transfer (status, required_plan_tier, expires_at, 7d)
  • /me/pending-ownership-transfers visibility for recipient
  • /orgs/{id}/ownership-transfers visibility rules (admin sees all,
    member sees only own-targeted)
  • 403: non-owner initiation
  • 400: self-transfer, non-member target, dup pending
  • Accept happy path: roles mutate atomically, transfer marked accepted
  • Accept 402 when plan insufficient (recipient on spark, org > 1 mbr)
  • Accept with also_leave_after=true scrubs outgoing owner
  • 403: non-recipient cannot accept
  • DELETE: initiator → cancelled, recipient → declined, others 403
  • Accept on non-pending → 400
"""
import os
import uuid
import time

import pytest
import requests
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    with open("/app/frontend/.env") as fh:
        for line in fh:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                break
API = f"{BASE_URL}/api"

SUPERADMIN_EMAIL = "reggie@coolgeek.me"
SUPERADMIN_PASSWORD = "superadminpass123"


def _h(tok):
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


@pytest.fixture(scope="session")
def admin_token():
    r = requests.post(f"{API}/auth/login",
                      json={"email": SUPERADMIN_EMAIL, "password": SUPERADMIN_PASSWORD},
                      timeout=30)
    assert r.status_code == 200, f"superadmin login: {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def access_code(admin_token):
    code_val = f"TESTITER33{uuid.uuid4().hex[:8].upper()}"
    r = requests.post(f"{API}/admin/access-codes",
                      headers=_h(admin_token),
                      json={"code": code_val, "usage_type": "multi", "max_uses": 80,
                            "note": "iter-33 ownership transfer tests"},
                      timeout=30)
    assert r.status_code == 200, f"mint code: {r.text}"
    return r.json()["code"]


def _register(code, prefix="iter33"):
    email = f"TEST_{prefix}_{uuid.uuid4().hex[:8]}@example.com"
    for attempt in range(10):
        r = requests.post(
            f"{API}/auth/register",
            json={"name": f"Test {prefix}", "email": email,
                  "password": "testpass123", "access_code": code},
            timeout=30,
        )
        if r.status_code == 429:
            time.sleep(5.0 + 3 * attempt)
            continue
        assert r.status_code == 200, f"register: {r.status_code} {r.text}"
        j = r.json()
        return {"token": j["access_token"], "id": j["id"], "email": email}
    raise AssertionError("register: too many 429s")


def _make_org(owner):
    r = requests.post(f"{API}/orgs", headers=_h(owner["token"]),
                      json={"name": f"TEST_Iter33_Org_{uuid.uuid4().hex[:6]}"},
                      timeout=30)
    assert r.status_code == 200, r.text
    return r.json()


def _invite(org_id, owner, invitee, role="member"):
    r = requests.post(f"{API}/orgs/{org_id}/invites",
                      headers=_h(owner["token"]),
                      json={"role": role}, timeout=30)
    assert r.status_code == 200, r.text
    code = r.json()["code"]
    r = requests.post(f"{API}/invites/accept",
                      headers=_h(invitee["token"]),
                      json={"code": code}, timeout=30)
    assert r.status_code == 200, r.text


def _members(org_id, who):
    r = requests.get(f"{API}/orgs/{org_id}/members",
                     headers=_h(who["token"]), timeout=30)
    assert r.status_code == 200, r.text
    return r.json()


def _set_plan_in_db(user_id, plan):
    """Direct Mongo plan stamp for plan-tier acceptance tests."""
    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME")
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]
    import asyncio
    async def _go():
        await db.users.update_one({"_id": ObjectId(user_id)},
                                  {"$set": {"plan": plan}})
    asyncio.get_event_loop().run_until_complete(_go())
    client.close()


# ── Tests ──────────────────────────────────────────────────────────────

def test_initiate_creates_pending_transfer(access_code):
    owner = _register(access_code, "ownA")
    recipient = _register(access_code, "rcpA")
    org = _make_org(owner)
    _invite(org["id"], owner, recipient)

    r = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers",
        headers=_h(owner["token"]),
        json={"recipient_user_id": recipient["id"],
              "outgoing_role_after": "admin",
              "also_leave_after": False},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status"] == "pending"
    assert j["recipient_user_id"] == recipient["id"]
    assert j["initiated_by"] == owner["id"]
    assert j["outgoing_role_after"] == "admin"
    assert j["also_leave_after"] is False
    # 2 members → temper required
    assert j["required_plan_tier"] == "temper"
    assert j["member_count_at_init"] == 2
    assert j["expires_at"] and j["created_at"]
    # 7d-ish window
    from datetime import datetime
    created = datetime.fromisoformat(j["created_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(j["expires_at"].replace("Z", "+00:00"))
    delta_days = (expires - created).total_seconds() / 86400.0
    assert 6.9 < delta_days < 7.1


def test_required_tier_for_single_member_is_spark(access_code):
    owner = _register(access_code, "ownSingle")
    # add a 2nd member then... no: spec says single org member = spark
    # but we still need a recipient (must be a member). For single-member
    # required_plan_tier path, we just verify with 1-member case impossible
    # because recipient must be in org. So smallest case is 2 members
    # (already covered above → temper). This is a "documented constraint"
    # test that exercises _required_tier_for_member_count via member ct.
    # Instead, sanity-check the >5 → refine path.
    org = _make_org(owner)
    members = []
    for i in range(6):  # invite 6 more → 7 total
        m = _register(access_code, f"big{i}")
        _invite(org["id"], owner, m)
        members.append(m)
    target = members[0]
    r = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers",
        headers=_h(owner["token"]),
        json={"recipient_user_id": target["id"],
              "outgoing_role_after": "member",
              "also_leave_after": False},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    assert r.json()["required_plan_tier"] == "refine"
    assert r.json()["member_count_at_init"] >= 6


def test_my_pending_transfers_visible_to_recipient(access_code):
    owner = _register(access_code, "ownB")
    recipient = _register(access_code, "rcpB")
    org = _make_org(owner)
    _invite(org["id"], owner, recipient)
    create_r = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers",
        headers=_h(owner["token"]),
        json={"recipient_user_id": recipient["id"],
              "outgoing_role_after": "admin"},
        timeout=30,
    )
    assert create_r.status_code == 200
    tid = create_r.json()["id"]

    r = requests.get(f"{API}/me/pending-ownership-transfers",
                     headers=_h(recipient["token"]), timeout=30)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert any(t["id"] == tid for t in rows)
    # And NOT visible in initiator's /me inbox
    r2 = requests.get(f"{API}/me/pending-ownership-transfers",
                      headers=_h(owner["token"]), timeout=30)
    assert r2.status_code == 200
    assert not any(t["id"] == tid for t in r2.json())


def test_list_org_transfers_visibility(access_code):
    owner = _register(access_code, "ownC")
    recipient = _register(access_code, "rcpC")
    onlooker = _register(access_code, "obs1C")
    org = _make_org(owner)
    _invite(org["id"], owner, recipient)
    _invite(org["id"], owner, onlooker)

    cr = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers",
        headers=_h(owner["token"]),
        json={"recipient_user_id": recipient["id"],
              "outgoing_role_after": "admin"},
        timeout=30,
    )
    assert cr.status_code == 200
    tid = cr.json()["id"]

    # owner sees all
    r = requests.get(f"{API}/orgs/{org['id']}/ownership-transfers",
                     headers=_h(owner["token"]), timeout=30)
    assert r.status_code == 200
    assert any(t["id"] == tid for t in r.json())

    # recipient sees only own
    r2 = requests.get(f"{API}/orgs/{org['id']}/ownership-transfers",
                      headers=_h(recipient["token"]), timeout=30)
    assert r2.status_code == 200
    rcp_ids = [t["id"] for t in r2.json()]
    assert tid in rcp_ids

    # uninvolved member sees nothing pertaining to others
    r3 = requests.get(f"{API}/orgs/{org['id']}/ownership-transfers",
                      headers=_h(onlooker["token"]), timeout=30)
    assert r3.status_code == 200
    assert not any(t["id"] == tid for t in r3.json())


def test_non_owner_initiation_returns_403(access_code):
    owner = _register(access_code, "ownD")
    member = _register(access_code, "memD")
    target = _register(access_code, "tgtD")
    org = _make_org(owner)
    _invite(org["id"], owner, member)
    _invite(org["id"], owner, target)

    r = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers",
        headers=_h(member["token"]),
        json={"recipient_user_id": target["id"],
              "outgoing_role_after": "member"},
        timeout=30,
    )
    assert r.status_code == 403, r.text


def test_self_transfer_400(access_code):
    owner = _register(access_code, "ownE")
    org = _make_org(owner)
    r = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers",
        headers=_h(owner["token"]),
        json={"recipient_user_id": owner["id"],
              "outgoing_role_after": "member"},
        timeout=30,
    )
    assert r.status_code == 400


def test_transfer_to_non_member_400(access_code):
    owner = _register(access_code, "ownF")
    outsider = _register(access_code, "outF")
    org = _make_org(owner)
    r = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers",
        headers=_h(owner["token"]),
        json={"recipient_user_id": outsider["id"],
              "outgoing_role_after": "member"},
        timeout=30,
    )
    assert r.status_code == 400


def test_duplicate_pending_transfer_400(access_code):
    owner = _register(access_code, "ownG")
    recipient = _register(access_code, "rcpG")
    org = _make_org(owner)
    _invite(org["id"], owner, recipient)

    r1 = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers",
        headers=_h(owner["token"]),
        json={"recipient_user_id": recipient["id"],
              "outgoing_role_after": "admin"},
        timeout=30,
    )
    assert r1.status_code == 200
    r2 = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers",
        headers=_h(owner["token"]),
        json={"recipient_user_id": recipient["id"],
              "outgoing_role_after": "admin"},
        timeout=30,
    )
    assert r2.status_code == 400
    assert "already" in r2.text.lower() or "pending" in r2.text.lower()


def test_accept_402_when_plan_insufficient(access_code):
    owner = _register(access_code, "ownH")
    recipient = _register(access_code, "rcpH")
    org = _make_org(owner)
    _invite(org["id"], owner, recipient)
    # Ensure recipient on spark (default)
    _set_plan_in_db(recipient["id"], "spark")

    cr = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers",
        headers=_h(owner["token"]),
        json={"recipient_user_id": recipient["id"],
              "outgoing_role_after": "admin"},
        timeout=30,
    )
    tid = cr.json()["id"]

    r = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers/{tid}/accept",
        headers=_h(recipient["token"]), json={}, timeout=30,
    )
    assert r.status_code == 402, r.text
    body = r.json()
    detail = body.get("detail", body)
    assert detail.get("code") == "plan_upgrade_required"
    assert detail.get("required_tier") == "temper"
    assert detail.get("current_tier") == "spark"
    assert "message" in detail


def test_accept_happy_path_sufficient_plan(access_code):
    owner = _register(access_code, "ownI")
    recipient = _register(access_code, "rcpI")
    org = _make_org(owner)
    _invite(org["id"], owner, recipient)
    _set_plan_in_db(recipient["id"], "temper")

    cr = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers",
        headers=_h(owner["token"]),
        json={"recipient_user_id": recipient["id"],
              "outgoing_role_after": "admin",
              "also_leave_after": False},
        timeout=30,
    )
    tid = cr.json()["id"]

    r = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers/{tid}/accept",
        headers=_h(recipient["token"]), json={}, timeout=30,
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status"] == "accepted"
    assert j["new_owner_id"] == recipient["id"]
    assert j["outgoing_member_left"] is False

    # Verify role mutation: recipient now owner; outgoing demoted to admin
    rows = _members(org["id"], recipient)
    by_uid = {m["user_id"]: m for m in rows}
    assert by_uid[recipient["id"]]["role"] == "owner"
    assert by_uid[owner["id"]]["role"] == "admin"

    # Accept again → 400 because not pending anymore
    r2 = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers/{tid}/accept",
        headers=_h(recipient["token"]), json={}, timeout=30,
    )
    assert r2.status_code == 400


def test_accept_with_also_leave_after_scrubs_outgoing(access_code):
    owner = _register(access_code, "ownJ")
    recipient = _register(access_code, "rcpJ")
    org = _make_org(owner)
    _invite(org["id"], owner, recipient)
    _set_plan_in_db(recipient["id"], "temper")

    cr = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers",
        headers=_h(owner["token"]),
        json={"recipient_user_id": recipient["id"],
              "outgoing_role_after": "member",
              "also_leave_after": True},
        timeout=30,
    )
    tid = cr.json()["id"]

    r = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers/{tid}/accept",
        headers=_h(recipient["token"]), json={}, timeout=30,
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["outgoing_member_left"] is True

    rows = _members(org["id"], recipient)
    uids = [m["user_id"] for m in rows]
    assert owner["id"] not in uids
    assert recipient["id"] in uids


def test_non_recipient_cannot_accept(access_code):
    owner = _register(access_code, "ownK")
    recipient = _register(access_code, "rcpK")
    bystander = _register(access_code, "byK")
    org = _make_org(owner)
    _invite(org["id"], owner, recipient)
    _invite(org["id"], owner, bystander)
    cr = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers",
        headers=_h(owner["token"]),
        json={"recipient_user_id": recipient["id"],
              "outgoing_role_after": "admin"},
        timeout=30,
    )
    tid = cr.json()["id"]
    r = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers/{tid}/accept",
        headers=_h(bystander["token"]), json={}, timeout=30,
    )
    assert r.status_code == 403


def test_cancel_by_initiator_marks_cancelled(access_code):
    owner = _register(access_code, "ownL")
    recipient = _register(access_code, "rcpL")
    org = _make_org(owner)
    _invite(org["id"], owner, recipient)
    cr = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers",
        headers=_h(owner["token"]),
        json={"recipient_user_id": recipient["id"],
              "outgoing_role_after": "admin"},
        timeout=30,
    )
    tid = cr.json()["id"]
    r = requests.delete(
        f"{API}/orgs/{org['id']}/ownership-transfers/{tid}",
        headers=_h(owner["token"]), timeout=30,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"


def test_decline_by_recipient_marks_declined(access_code):
    owner = _register(access_code, "ownM")
    recipient = _register(access_code, "rcpM")
    org = _make_org(owner)
    _invite(org["id"], owner, recipient)
    cr = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers",
        headers=_h(owner["token"]),
        json={"recipient_user_id": recipient["id"],
              "outgoing_role_after": "admin"},
        timeout=30,
    )
    tid = cr.json()["id"]
    r = requests.delete(
        f"{API}/orgs/{org['id']}/ownership-transfers/{tid}",
        headers=_h(recipient["token"]), timeout=30,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "declined"


def test_cancel_by_third_party_403(access_code):
    owner = _register(access_code, "ownN")
    recipient = _register(access_code, "rcpN")
    third = _register(access_code, "thN")
    org = _make_org(owner)
    _invite(org["id"], owner, recipient)
    _invite(org["id"], owner, third)
    cr = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers",
        headers=_h(owner["token"]),
        json={"recipient_user_id": recipient["id"],
              "outgoing_role_after": "admin"},
        timeout=30,
    )
    tid = cr.json()["id"]
    r = requests.delete(
        f"{API}/orgs/{org['id']}/ownership-transfers/{tid}",
        headers=_h(third["token"]), timeout=30,
    )
    assert r.status_code == 403


def test_accept_already_cancelled_returns_400(access_code):
    owner = _register(access_code, "ownO")
    recipient = _register(access_code, "rcpO")
    org = _make_org(owner)
    _invite(org["id"], owner, recipient)
    _set_plan_in_db(recipient["id"], "temper")
    cr = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers",
        headers=_h(owner["token"]),
        json={"recipient_user_id": recipient["id"],
              "outgoing_role_after": "admin"},
        timeout=30,
    )
    tid = cr.json()["id"]
    # cancel
    requests.delete(
        f"{API}/orgs/{org['id']}/ownership-transfers/{tid}",
        headers=_h(owner["token"]), timeout=30,
    )
    # accept now
    r = requests.post(
        f"{API}/orgs/{org['id']}/ownership-transfers/{tid}/accept",
        headers=_h(recipient["token"]), json={}, timeout=30,
    )
    assert r.status_code == 400
