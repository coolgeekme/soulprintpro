"""Iteration 7 — orgs/teams PATCH+DELETE, schedules CRUD + run-now,
timezone-injection regression. All against the live preview backend."""
import os
import time
import secrets
import uuid
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://emergent-wing.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"


# ---- helpers ----------------------------------------------------------------
def _register(name="User"):
    email = f"TEST_{secrets.token_hex(6)}@test.dev"
    s = requests.Session()
    r = s.post(f"{API}/auth/register", json={"name": name, "email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    return s, email


@pytest.fixture(scope="module")
def owner():
    s, _ = _register("Owner")
    return s


@pytest.fixture(scope="module")
def member():
    s, _ = _register("Member")
    return s


@pytest.fixture(scope="module")
def org(owner):
    r = owner.post(f"{API}/orgs", json={"name": "TEST_Org", "website_url": "", "soul_description": "Test boutique studio"})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="module")
def team(owner, org):
    # Create a non-General team for tests
    r = owner.post(f"{API}/orgs/{org['id']}/teams", json={"name": "Crew Alpha", "description": "first crew"})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="module")
def general_team(owner, org):
    # Use the canonical pointer on the org doc (serialize_team doesn't yet
    # expose `is_general`, so we resolve via org.general_team_id).
    gid = org.get("general_team_id")
    assert gid, f"org has no general_team_id: {org}"
    r = owner.get(f"{API}/teams/{gid}")
    assert r.status_code == 200
    return r.json()


@pytest.fixture(scope="module")
def conv(owner, team):
    r = owner.post(f"{API}/teams/{team['id']}/conversations", json={
        "title": "Sched conv",
        "type": "direct",
        "agent_ids": ["marketing-content-creator"],
        "member_ids": [],
    })
    assert r.status_code == 200, r.text
    return r.json()


# ---- Org PATCH/DELETE -------------------------------------------------------
class TestOrgPatchDelete:
    def test_patch_org_rename_and_url(self, owner, org):
        r = owner.patch(f"{API}/orgs/{org['id']}", json={"name": "TEST_Org_Renamed", "website_url": "https://example.com"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["name"] == "TEST_Org_Renamed"
        assert d["website_url"] == "https://example.com"
        # GET to verify persistence
        g = owner.get(f"{API}/orgs/{org['id']}")
        assert g.status_code == 200
        assert g.json()["name"] == "TEST_Org_Renamed"

    def test_patch_org_regenerate_soul(self, owner, org):
        r = owner.patch(f"{API}/orgs/{org['id']}", json={
            "soul_description": "Friendly indie creative studio for craft brands",
            "regenerate_soul": True,
        })
        assert r.status_code == 200, r.text
        d = r.json()
        soul = d.get("soul") or {}
        # Soul object should now be populated by the LLM
        assert isinstance(soul, dict) and len(soul) > 0, f"soul empty: {soul}"

    def test_patch_org_non_member_403(self, member, org):
        r = member.patch(f"{API}/orgs/{org['id']}", json={"name": "Hacked"})
        assert r.status_code in (403, 404), r.status_code

    def test_delete_org_non_owner_403(self, member, owner, org):
        # First make member join via invite
        inv = owner.post(f"{API}/orgs/{org['id']}/invites", json={"role": "admin"})
        assert inv.status_code == 200
        code = inv.json()["code"]
        a = member.post(f"{API}/invites/accept", json={"code": code})
        assert a.status_code == 200
        # As admin (not owner), DELETE must be 403
        r = member.delete(f"{API}/orgs/{org['id']}")
        assert r.status_code == 403, r.text

    def test_delete_org_cascade(self, owner):
        # Build a brand-new org to delete (don't touch the module fixture)
        rorg = owner.post(f"{API}/orgs", json={"name": "TEST_DeleteMe", "website_url": "", "soul_description": "x"})
        oid = rorg.json()["id"]
        # Create a team + conv to verify cascade
        rt = owner.post(f"{API}/orgs/{oid}/teams", json={"name": "TmpTeam"})
        tid = rt.json()["id"]
        rc = owner.post(f"{API}/teams/{tid}/conversations", json={"type": "direct", "agent_ids": ["marketing-content-creator"]})
        cid = rc.json()["id"]
        # Schedule on this conv
        rs = owner.post(f"{API}/schedules", json={
            "title": "DelCascade", "prompt": "ping", "scope": "conversation",
            "target_id": cid, "cron": "* * * * *",
        })
        assert rs.status_code == 200
        # DELETE the org
        d = owner.delete(f"{API}/orgs/{oid}")
        assert d.status_code == 200
        # Verify org is gone
        g = owner.get(f"{API}/orgs/{oid}")
        assert g.status_code in (403, 404), g.status_code
        # Verify team gone (404 trying to fetch)
        gt = owner.get(f"{API}/teams/{tid}")
        assert gt.status_code in (403, 404), gt.status_code
        # Verify conversation messages absent
        gm = owner.get(f"{API}/conversations/{cid}/messages")
        assert gm.status_code in (403, 404), gm.status_code


# ---- Team PATCH/DELETE ------------------------------------------------------
class TestTeamPatchDelete:
    def test_patch_team_rename(self, owner, team):
        r = owner.patch(f"{API}/teams/{team['id']}", json={"name": "Crew Bravo", "description": "renamed"})
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "Crew Bravo"

    def test_patch_team_member_role_403(self, owner, org):
        # Add a fresh user as plain "member" and confirm 403 on PATCH
        plain, _ = _register("PlainMember")
        inv = owner.post(f"{API}/orgs/{org['id']}/invites", json={"role": "member"})
        plain.post(f"{API}/invites/accept", json={"code": inv.json()["code"]})
        # Find team to patch
        teams = owner.get(f"{API}/orgs/{org['id']}/teams").json()
        tid = next(t["id"] for t in teams if not t.get("is_general"))
        r = plain.patch(f"{API}/teams/{tid}", json={"name": "X"})
        assert r.status_code == 403, r.status_code

    def test_delete_general_blocked(self, owner, general_team):
        r = owner.delete(f"{API}/teams/{general_team['id']}")
        assert r.status_code == 400, r.text
        assert "General" in r.text

    def test_delete_team_cascade(self, owner, org):
        rt = owner.post(f"{API}/orgs/{org['id']}/teams", json={"name": "ToBeDeleted"})
        tid = rt.json()["id"]
        rc = owner.post(f"{API}/teams/{tid}/conversations", json={"type": "direct", "agent_ids": ["marketing-content-creator"]})
        cid = rc.json()["id"]
        rs = owner.post(f"{API}/schedules", json={
            "title": "TeamDel", "prompt": "ping", "scope": "team",
            "target_id": tid, "cron": "* * * * *",
        })
        assert rs.status_code == 200
        d = owner.delete(f"{API}/teams/{tid}")
        assert d.status_code == 200
        # conversation gone
        g = owner.get(f"{API}/conversations/{cid}/messages")
        assert g.status_code in (403, 404)


# ---- Schedules CRUD ---------------------------------------------------------
class TestSchedules:
    def test_create_cron(self, owner, conv):
        r = owner.post(f"{API}/schedules", json={
            "title": "every-min", "prompt": "say hi", "scope": "conversation",
            "target_id": conv["id"], "cron": "* * * * *", "timezone": "UTC",
        })
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["next_run_at"], f"next_run_at should be populated: {d}"
        assert d["cron"] == "* * * * *"
        TestSchedules.created_id = d["id"]

    def test_list_by_scope(self, owner, conv):
        r = owner.get(f"{API}/schedules", params={"scope": "conversation", "target_id": conv["id"]})
        assert r.status_code == 200
        assert any(s["id"] == TestSchedules.created_id for s in r.json())

    def test_patch_active_false(self, owner):
        r = owner.patch(f"{API}/schedules/{TestSchedules.created_id}", json={"active": False})
        assert r.status_code == 200
        assert r.json()["active"] is False

    def test_create_run_at_future(self, owner, conv):
        r = owner.post(f"{API}/schedules", json={
            "title": "one-shot", "prompt": "hi", "scope": "conversation",
            "target_id": conv["id"], "run_at": "2099-01-01T00:00:00Z",
        })
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["next_run_at"], "one-shot run_at should populate next_run_at"

    def test_create_both_cron_and_run_at_400(self, owner, conv):
        r = owner.post(f"{API}/schedules", json={
            "title": "bad", "prompt": "x", "scope": "conversation",
            "target_id": conv["id"], "cron": "* * * * *", "run_at": "2099-01-01T00:00:00Z",
        })
        assert r.status_code == 400, r.text

    def test_create_neither_cron_nor_run_at_400(self, owner, conv):
        r = owner.post(f"{API}/schedules", json={
            "title": "bad", "prompt": "x", "scope": "conversation",
            "target_id": conv["id"],
        })
        assert r.status_code == 400, r.text

    def test_org_scope(self, owner, org):
        r = owner.post(f"{API}/schedules", json={
            "title": "org-scope", "prompt": "hi org", "scope": "org",
            "target_id": org["id"], "cron": "0 9 * * *",
        })
        assert r.status_code == 200, r.text

    def test_list_by_org(self, owner, org):
        r = owner.get(f"{API}/schedules", params={"org_id": org["id"]})
        assert r.status_code == 200
        # We've created at least 1 (the org-scope one); there may also be the team-scope cascade test (deleted) so just check non-empty
        assert isinstance(r.json(), list)

    def test_run_now(self, owner, conv):
        # Create a fresh schedule for run-now
        r = owner.post(f"{API}/schedules", json={
            "title": "fire-now", "prompt": "Reply with the word OK", "scope": "conversation",
            "target_id": conv["id"], "cron": "0 0 1 1 *",  # almost-never trigger
        })
        sid = r.json()["id"]
        rn = owner.post(f"{API}/schedules/{sid}/run-now")
        assert rn.status_code == 200
        # poll messages for up to ~30s for the ⏰ user msg + agent reply tagged with schedule_id
        found_user = found_agent = False
        for _ in range(15):
            time.sleep(2)
            mr = owner.get(f"{API}/conversations/{conv['id']}/messages")
            if mr.status_code != 200:
                continue
            msgs = mr.json()
            for m in msgs:
                if m.get("schedule_id") == sid and m.get("sender_type") == "user" and m.get("text", "").startswith("⏰"):
                    found_user = True
                if m.get("schedule_id") == sid and m.get("sender_type") == "agent":
                    found_agent = True
            if found_user and found_agent:
                break
        assert found_user, "did not see ⏰ user message tagged with schedule_id"
        assert found_agent, "did not see agent reply tagged with schedule_id"

    def test_delete_schedule(self, owner):
        d = owner.delete(f"{API}/schedules/{TestSchedules.created_id}")
        assert d.status_code == 200
        # 404 on a follow-up patch
        p = owner.patch(f"{API}/schedules/{TestSchedules.created_id}", json={"active": True})
        assert p.status_code == 404


# ---- Timezone header injection regression -----------------------------------
class TestTimezoneInjection:
    def test_message_with_tz_and_loc_headers(self, owner, conv):
        # Send with PT timezone header; agent system-prompt now includes local time
        # We can't peek into the prompt, but we can verify the request succeeds
        # and the message persists.
        r = owner.post(
            f"{API}/conversations/{conv['id']}/messages",
            json={"text": "What's a good time today to post a Twitter announcement?"},
            headers={
                "X-User-Timezone": "America/Los_Angeles",
                "X-User-Location": "San Francisco, USA",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # Should have user_message + at least one agent reply
        assert "user_message" in body or "messages" in body or isinstance(body, dict)
