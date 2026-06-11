"""Iter-32 backend tests — Org member role edits + self-leave.

PATCH /api/orgs/{org_id}/members/{user_id}  — owner-only role change
DELETE /api/orgs/{org_id}/members/me        — self-leave (with last-owner guard)

Critical invariants:
  • Only owners can change roles (admins cannot).
  • Owners cannot edit their OWN role (self-demote lock).
  • Last-owner leave is blocked (would orphan the org).
  • Self-leave removes the org_members row AND strips team memberships.
"""
import os
import uuid

import pytest
import requests


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
    code_val = f"TESTITER32{uuid.uuid4().hex[:8].upper()}"
    r = requests.post(f"{API}/admin/access-codes",
                      headers=_h(admin_token),
                      json={"code": code_val, "usage_type": "multi", "max_uses": 30,
                            "note": "iter-32 roles + leave tests"},
                      timeout=30)
    assert r.status_code == 200, f"mint code: {r.text}"
    return r.json()["code"]


def _register(code, prefix="iter32"):
    """Self-register and return token + id. Includes a 429-aware retry
    because creating ~20 fresh users in one test run can trip the
    per-IP rate limit on /auth/register."""
    import time
    email = f"TEST_{prefix}_{uuid.uuid4().hex[:8]}@example.com"
    for attempt in range(6):
        r = requests.post(
            f"{API}/auth/register",
            json={"name": f"Test {prefix}", "email": email,
                  "password": "testpass123", "access_code": code},
            timeout=30,
        )
        if r.status_code == 429:
            time.sleep(2.5 + attempt)  # back off, then retry
            continue
        assert r.status_code == 200, f"register: {r.status_code} {r.text}"
        j = r.json()
        return {"token": j["access_token"], "id": j["id"], "email": email}
    raise AssertionError(f"register: gave up after 6 attempts with 429s on {email}")


def _make_org(owner):
    r = requests.post(f"{API}/orgs", headers=_h(owner["token"]),
                      json={"name": f"TEST_Iter32_Org_{uuid.uuid4().hex[:6]}"}, timeout=30)
    assert r.status_code == 200, r.text
    return r.json()


def _invite_and_accept(org_id, owner, invitee, role="member"):
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
    r = requests.get(f"{API}/orgs/{org_id}/members", headers=_h(who["token"]), timeout=30)
    assert r.status_code == 200, r.text
    return r.json()


def test_owner_can_promote_member_to_admin(access_code):
    owner = _register(access_code, "ownerA")
    member = _register(access_code, "memberA")
    org = _make_org(owner)
    _invite_and_accept(org["id"], owner, member, role="member")

    r = requests.patch(
        f"{API}/orgs/{org['id']}/members/{member['id']}",
        headers=_h(owner["token"]), json={"role": "admin"}, timeout=30,
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["role"] == "admin" and j["changed"] is True

    rows = _members(org["id"], owner)
    target = next(m for m in rows if m["user_id"] == member["id"])
    assert target["role"] == "admin"


def test_admin_cannot_change_roles(access_code):
    owner = _register(access_code, "ownerB")
    admin = _register(access_code, "adminB")
    target = _register(access_code, "targetB")
    org = _make_org(owner)
    _invite_and_accept(org["id"], owner, admin, role="admin")
    _invite_and_accept(org["id"], owner, target, role="member")

    r = requests.patch(
        f"{API}/orgs/{org['id']}/members/{target['id']}",
        headers=_h(admin["token"]), json={"role": "admin"}, timeout=30,
    )
    assert r.status_code == 403, r.text


def test_owner_cannot_change_own_role(access_code):
    owner = _register(access_code, "ownerC")
    org = _make_org(owner)
    r = requests.patch(
        f"{API}/orgs/{org['id']}/members/{owner['id']}",
        headers=_h(owner["token"]), json={"role": "admin"}, timeout=30,
    )
    assert r.status_code == 400
    assert "own role" in r.text.lower()


def test_promote_to_owner_then_co_owner_can_demote_original(access_code):
    """Multiple owners supported; promoting + co-owner demote both work."""
    owner = _register(access_code, "ownerD")
    second = _register(access_code, "secondD")
    org = _make_org(owner)
    _invite_and_accept(org["id"], owner, second, role="member")

    r = requests.patch(f"{API}/orgs/{org['id']}/members/{second['id']}",
                       headers=_h(owner["token"]), json={"role": "owner"}, timeout=30)
    assert r.status_code == 200

    # Two owners now; second demotes the original owner — works.
    r = requests.patch(f"{API}/orgs/{org['id']}/members/{owner['id']}",
                       headers=_h(second["token"]), json={"role": "member"}, timeout=30)
    assert r.status_code == 200
    rows = _members(org["id"], second)
    assert next(m for m in rows if m["user_id"] == owner["id"])["role"] == "member"
    assert next(m for m in rows if m["user_id"] == second["id"])["role"] == "owner"


def test_member_can_self_leave(access_code):
    owner = _register(access_code, "ownerE")
    member = _register(access_code, "memberE")
    org = _make_org(owner)
    _invite_and_accept(org["id"], owner, member, role="member")

    r = requests.delete(f"{API}/orgs/{org['id']}/members/me",
                        headers=_h(member["token"]), timeout=30)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # Membership gone.
    rows = _members(org["id"], owner)
    assert all(m["user_id"] != member["id"] for m in rows)

    # Org no longer visible to the departed user.
    r = requests.get(f"{API}/orgs/{org['id']}", headers=_h(member["token"]), timeout=30)
    assert r.status_code in (403, 404), r.text


def test_last_owner_cannot_leave(access_code):
    owner = _register(access_code, "ownerF")
    org = _make_org(owner)
    r = requests.delete(f"{API}/orgs/{org['id']}/members/me",
                        headers=_h(owner["token"]), timeout=30)
    assert r.status_code == 400, r.text
    assert "only owner" in r.text.lower()


def test_owner_can_leave_after_promoting_successor(access_code):
    owner = _register(access_code, "ownerG")
    successor = _register(access_code, "successorG")
    org = _make_org(owner)
    _invite_and_accept(org["id"], owner, successor, role="member")
    r = requests.patch(f"{API}/orgs/{org['id']}/members/{successor['id']}",
                       headers=_h(owner["token"]), json={"role": "owner"}, timeout=30)
    assert r.status_code == 200
    r = requests.delete(f"{API}/orgs/{org['id']}/members/me",
                        headers=_h(owner["token"]), timeout=30)
    assert r.status_code == 200, r.text
    # Successor is still owner.
    rows = _members(org["id"], successor)
    assert next(m for m in rows if m["user_id"] == successor["id"])["role"] == "owner"


def test_role_change_on_non_member_404(access_code):
    owner = _register(access_code, "ownerH")
    stranger = _register(access_code, "strangerH")
    org = _make_org(owner)
    r = requests.patch(f"{API}/orgs/{org['id']}/members/{stranger['id']}",
                       headers=_h(owner["token"]), json={"role": "admin"}, timeout=30)
    assert r.status_code == 404


def test_patch_empty_body_400(access_code):
    """Backend requires `role` field. Pydantic returns 422 when missing."""
    owner = _register(access_code, "ownerI")
    member = _register(access_code, "memberI")
    org = _make_org(owner)
    _invite_and_accept(org["id"], owner, member, role="member")
    r = requests.patch(f"{API}/orgs/{org['id']}/members/{member['id']}",
                       headers=_h(owner["token"]), json={}, timeout=30)
    assert r.status_code in (400, 422), r.text


def test_invalid_role_value_rejected(access_code):
    owner = _register(access_code, "ownerJ")
    member = _register(access_code, "memberJ")
    org = _make_org(owner)
    _invite_and_accept(org["id"], owner, member, role="member")
    r = requests.patch(f"{API}/orgs/{org['id']}/members/{member['id']}",
                       headers=_h(owner["token"]), json={"role": "superduper"}, timeout=30)
    assert r.status_code in (400, 422), r.text
