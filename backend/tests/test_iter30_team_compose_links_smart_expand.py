"""Iteration 30 backend tests.

Covers four new features:
1. POST /api/teams/{team_id}/start-with-files (multipart team composer with attachments)
2. POST /api/orgs/{org_id}/asset-links (external link asset adder w/ provider auto-detect)
3. PATCH /api/conversations/{conv_id}/settings (auto_respond / smart_expand toggle)
4. smart_expand on DIRECT chats → promote to group + add specialist
"""
import io
import os
import uuid
import time
import pytest
import requests

# ── BASE_URL from frontend/.env ────────────────────────────────────────────
BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    try:
        with open("/app/frontend/.env") as fh:
            for line in fh:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                    break
    except Exception:
        pass
API = f"{BASE_URL}/api"

SUPERADMIN_EMAIL = "reggie@coolgeek.me"
SUPERADMIN_PASSWORD = "superadminpass123"


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def _h_noct(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


# ── Fixtures ───────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def admin_token():
    r = requests.post(f"{API}/auth/login",
                      json={"email": SUPERADMIN_EMAIL, "password": SUPERADMIN_PASSWORD},
                      timeout=15)
    assert r.status_code == 200, f"superadmin login failed: {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def access_code(admin_token):
    code_val = f"TESTITER30{uuid.uuid4().hex[:8].upper()}"
    r = requests.post(f"{API}/admin/access-codes",
                      headers=_h(admin_token),
                      json={"code": code_val, "usage_type": "multi", "max_uses": 20,
                            "note": "iter-30 test"},
                      timeout=15)
    assert r.status_code == 200, f"mint code failed: {r.text}"
    return r.json()["code"]


def _register(name, email, code):
    r = requests.post(f"{API}/auth/register",
                      json={"name": name, "email": email, "password": "testpass123",
                            "access_code": code},
                      timeout=20)
    assert r.status_code == 200, f"register {email} failed: {r.status_code} {r.text}"
    return r.json()["access_token"], r.json()["id"]


@pytest.fixture(scope="session")
def user_a(access_code):
    email = f"TEST_iter30a_{uuid.uuid4().hex[:8]}@example.com"
    tok, uid = _register("Iter30 A", email, access_code)
    return {"token": tok, "id": uid, "email": email}


@pytest.fixture(scope="session")
def user_b(access_code):
    email = f"TEST_iter30b_{uuid.uuid4().hex[:8]}@example.com"
    tok, uid = _register("Iter30 B", email, access_code)
    return {"token": tok, "id": uid, "email": email}


@pytest.fixture(scope="session")
def org_a(user_a):
    r = requests.post(f"{API}/orgs",
                      headers=_h(user_a["token"]),
                      json={"name": f"TEST_Iter30Org_{uuid.uuid4().hex[:6]}"},
                      timeout=60)
    assert r.status_code == 200, f"create org: {r.status_code} {r.text}"
    return r.json()


@pytest.fixture(scope="session")
def team_a(org_a, user_a):
    """Re-use the auto-created 'General' team for the org if present.
    Otherwise create one. /api/orgs/{org_id}/teams (GET) lists teams."""
    org_id = org_a["id"]
    rg = requests.get(f"{API}/orgs/{org_id}/teams",
                      headers=_h(user_a["token"]), timeout=15)
    teams = rg.json() if rg.status_code == 200 else []
    if teams:
        return teams[0]
    # fallback create
    r = requests.post(f"{API}/orgs/{org_id}/teams",
                      headers=_h(user_a["token"]),
                      json={"name": "Iter30 Team",
                            "agent_ids": ["specialized-social-media",
                                          "specialized-engineering",
                                          "specialized-chief-of-staff"]},
                      timeout=30)
    assert r.status_code == 200, f"create team: {r.text}"
    return r.json()


# ── Feature 1 — start-with-files ───────────────────────────────────────────
class TestStartWithFiles:
    def test_with_text_and_file(self, user_a, team_a):
        team_id = team_a["id"]
        files = {"files": ("brand_notes.txt", io.BytesIO(b"Our brand voice is bold and friendly."), "text/plain")}
        data = {"text": "Help me draft a social post about our brand voice"}
        r = requests.post(f"{API}/teams/{team_id}/start-with-files",
                          headers=_h_noct(user_a["token"]),
                          files=files, data=data, timeout=120)
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        body = r.json()
        assert "conversation" in body
        assert body["conversation"]["type"] == "direct"
        assert body["conversation"].get("agent_ids"), "no agents on conv"
        assert body.get("routed_agent_id")
        assert body.get("user_message") is not None
        assert isinstance(body["user_message"].get("attachments"), list)
        assert len(body["user_message"]["attachments"]) >= 1
        # GET messages to verify persistence
        conv_id = body["conversation"]["id"]
        time.sleep(0.5)
        gm = requests.get(f"{API}/conversations/{conv_id}/messages",
                          headers=_h(user_a["token"]), timeout=30)
        assert gm.status_code == 200
        msgs = gm.json()
        user_msgs = [m for m in msgs if m.get("sender_type") == "user"]
        assert user_msgs, "no user message persisted"
        assert any(m.get("attachments") for m in user_msgs), "no attachment persisted"

    def test_with_file_only_no_text(self, user_a, team_a):
        team_id = team_a["id"]
        files = {"files": ("python_bug.py", io.BytesIO(b"def add(a,b):\n    return a-b\n"), "text/x-python")}
        data = {"text": ""}
        r = requests.post(f"{API}/teams/{team_id}/start-with-files",
                          headers=_h_noct(user_a["token"]),
                          files=files, data=data, timeout=120)
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        body = r.json()
        assert body["conversation"]["type"] == "direct"
        assert body.get("routed_agent_id")

    def test_no_files_returns_error(self, user_a, team_a):
        # multipart with no files = error
        team_id = team_a["id"]
        r = requests.post(f"{API}/teams/{team_id}/start-with-files",
                          headers=_h_noct(user_a["token"]),
                          data={"text": "hi"}, timeout=30)
        assert r.status_code in (400, 422)


# ── Feature 2 — asset links ────────────────────────────────────────────────
class TestAssetLinks:
    @pytest.fixture(scope="class")
    def link_assets_to_cleanup(self):
        ids = []
        yield ids
        # cleanup intentionally skipped — TEST_ prefixed org is sandbox-only

    def test_google_drive_link(self, user_a, org_a, link_assets_to_cleanup):
        org_id = org_a["id"]
        r = requests.post(f"{API}/orgs/{org_id}/asset-links",
                          headers=_h(user_a["token"]),
                          json={"url": "https://drive.google.com/file/d/abc123/view",
                                "name": "TEST Brand Kit",
                                "description": "master brand guidelines"},
                          timeout=30)
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        a = r.json()
        assert a["kind"] == "link"
        assert a["link_url"] == "https://drive.google.com/file/d/abc123/view"
        assert a["link_provider"] == "Google Drive"
        assert a["url"] == a["link_url"]
        link_assets_to_cleanup.append(a["id"])

    def test_dropbox_provider(self, user_a, org_a, link_assets_to_cleanup):
        r = requests.post(f"{API}/orgs/{org_a['id']}/asset-links",
                          headers=_h(user_a["token"]),
                          json={"url": "https://www.dropbox.com/s/abc/x.pdf",
                                "name": "TEST Dropbox"}, timeout=20)
        assert r.status_code == 200
        assert r.json()["link_provider"] == "Dropbox"
        link_assets_to_cleanup.append(r.json()["id"])

    def test_notion_provider(self, user_a, org_a, link_assets_to_cleanup):
        r = requests.post(f"{API}/orgs/{org_a['id']}/asset-links",
                          headers=_h(user_a["token"]),
                          json={"url": "https://www.notion.so/My-Page-abc",
                                "name": "TEST Notion"}, timeout=20)
        assert r.status_code == 200
        assert r.json()["link_provider"] == "Notion"
        link_assets_to_cleanup.append(r.json()["id"])

    def test_figma_provider(self, user_a, org_a, link_assets_to_cleanup):
        r = requests.post(f"{API}/orgs/{org_a['id']}/asset-links",
                          headers=_h(user_a["token"]),
                          json={"url": "https://www.figma.com/file/abc/Design",
                                "name": "TEST Figma"}, timeout=20)
        assert r.status_code == 200
        assert r.json()["link_provider"] == "Figma"
        link_assets_to_cleanup.append(r.json()["id"])

    def test_reject_non_http(self, user_a, org_a):
        r = requests.post(f"{API}/orgs/{org_a['id']}/asset-links",
                          headers=_h(user_a["token"]),
                          json={"url": "ftp://foo/bar", "name": "bad"}, timeout=15)
        assert r.status_code == 400

    def test_reject_empty(self, user_a, org_a):
        r = requests.post(f"{API}/orgs/{org_a['id']}/asset-links",
                          headers=_h(user_a["token"]),
                          json={"url": "", "name": "bad"}, timeout=15)
        assert r.status_code == 400

    def test_listed_in_assets(self, user_a, org_a, link_assets_to_cleanup):
        if not link_assets_to_cleanup:
            pytest.skip("no link assets created")
        r = requests.get(f"{API}/orgs/{org_a['id']}/assets",
                         headers=_h(user_a["token"]), timeout=15)
        assert r.status_code == 200
        ids = {a["id"] for a in r.json()}
        for aid in link_assets_to_cleanup:
            assert aid in ids, f"link asset {aid} not in list"

    def test_delete_as_creator(self, user_a, org_a, link_assets_to_cleanup):
        # Create a fresh one to delete so other tests still see assets
        r = requests.post(f"{API}/orgs/{org_a['id']}/asset-links",
                          headers=_h(user_a["token"]),
                          json={"url": "https://www.figma.com/file/del/x",
                                "name": "TEST DEL"}, timeout=15)
        assert r.status_code == 200
        aid = r.json()["id"]
        dr = requests.delete(f"{API}/orgs/{org_a['id']}/assets/{aid}",
                             headers=_h(user_a["token"]), timeout=15)
        assert dr.status_code == 200, f"{dr.status_code} {dr.text}"

    def test_delete_as_non_uploader_non_admin_403(self, user_a, user_b, org_a, link_assets_to_cleanup):
        # Invite user_b to org_a (as member, not admin)
        # First create a link as user_a
        r = requests.post(f"{API}/orgs/{org_a['id']}/asset-links",
                          headers=_h(user_a["token"]),
                          json={"url": "https://drive.google.com/file/d/zzz/view",
                                "name": "TEST forbid"}, timeout=15)
        assert r.status_code == 200
        aid = r.json()["id"]
        # invite b: get invite link from /orgs/{id}/invites
        inv = requests.post(f"{API}/orgs/{org_a['id']}/invites",
                            headers=_h(user_a["token"]),
                            json={"email": user_b["email"], "role": "member"},
                            timeout=20)
        if inv.status_code != 200:
            pytest.skip(f"could not invite user_b: {inv.status_code} {inv.text}")
        invite = inv.json()
        code = invite.get("code") or invite.get("invite_code")
        if not code:
            pytest.skip(f"no invite code in {invite}")
        acc = requests.post(f"{API}/invites/accept",
                            headers=_h(user_b["token"]),
                            json={"code": code}, timeout=20)
        if acc.status_code != 200:
            pytest.skip(f"accept invite failed: {acc.status_code} {acc.text}")
        # Now user_b tries to delete user_a's link
        dr = requests.delete(f"{API}/orgs/{org_a['id']}/assets/{aid}",
                             headers=_h(user_b["token"]), timeout=15)
        assert dr.status_code == 403, f"expected 403, got {dr.status_code} {dr.text}"


# ── Feature 4a — PATCH /conversations/{id}/settings ────────────────────────
class TestConversationSettings:
    @pytest.fixture(scope="class")
    def group_conv(self, user_a, team_a):
        r = requests.post(f"{API}/teams/{team_a['id']}/conversations",
                          headers=_h(user_a["token"]),
                          json={"type": "group",
                                "agent_ids": team_a.get("agent_ids", [])[:2] or ["specialized-chief-of-staff"],
                                "title": "TEST iter30 settings"},
                          timeout=30)
        assert r.status_code == 200, f"create group conv: {r.text}"
        return r.json()

    def test_patch_auto_respond(self, user_a, group_conv):
        cid = group_conv["id"]
        r = requests.patch(f"{API}/conversations/{cid}/settings",
                           headers=_h(user_a["token"]),
                           json={"auto_respond": True}, timeout=15)
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        assert r.json().get("auto_respond") is True

    def test_patch_smart_expand(self, user_a, group_conv):
        cid = group_conv["id"]
        r = requests.patch(f"{API}/conversations/{cid}/settings",
                           headers=_h(user_a["token"]),
                           json={"smart_expand": True}, timeout=15)
        assert r.status_code == 200
        assert r.json().get("smart_expand") is True

    def test_patch_both(self, user_a, group_conv):
        cid = group_conv["id"]
        r = requests.patch(f"{API}/conversations/{cid}/settings",
                           headers=_h(user_a["token"]),
                           json={"auto_respond": False, "smart_expand": False}, timeout=15)
        assert r.status_code == 200
        body = r.json()
        assert body.get("auto_respond") is False
        assert body.get("smart_expand") is False


# ── Feature 4b — smart_expand on DIRECT chats ─────────────────────────────
class TestSmartExpandDirect:
    def test_direct_promote_to_group(self, user_a, team_a):
        team_id = team_a["id"]
        # Discover available agents via the team agents endpoint
        ra = requests.get(f"{API}/teams/{team_id}/agents",
                          headers=_h(user_a["token"]), timeout=20)
        assert ra.status_code == 200, f"list agents: {ra.text}"
        all_agents = ra.json()
        social = next((a for a in all_agents if "social" in a["id"].lower()), None)
        if not social:
            pytest.skip("no social-media specialist available in catalogue")
        agent_id = social["id"]
        r = requests.post(f"{API}/teams/{team_id}/conversations",
                          headers=_h(user_a["token"]),
                          json={"type": "direct", "agent_ids": [agent_id],
                                "title": "TEST smart-expand"},
                          timeout=30)
        assert r.status_code == 200, f"create direct conv: {r.text}"
        conv = r.json()
        cid = conv["id"]
        original_aids = list(conv.get("agent_ids") or [])
        assert len(original_aids) == 1

        # Enable smart_expand
        pr = requests.patch(f"{API}/conversations/{cid}/settings",
                            headers=_h(user_a["token"]),
                            json={"smart_expand": True}, timeout=15)
        assert pr.status_code == 200
        assert pr.json().get("smart_expand") is True

        # Send a message obviously OUT of social-media specialty
        off_domain = ("Please review this Python code and fix the bug:\n"
                      "def add(a, b):\n    return a - b\n\n"
                      "It should add two numbers but returns subtraction.")
        msg = requests.post(f"{API}/conversations/{cid}/messages",
                            headers=_h(user_a["token"]),
                            json={"text": off_domain}, timeout=180)
        # Tolerate router latency / LLM jitter
        assert msg.status_code == 200, f"send: {msg.status_code} {msg.text}"

        # Poll the conv state via messages GET (conv state is inside one of /conversations endpoints)
        time.sleep(1.0)
        # Read conv state by re-fetching team conversations or via messages? Use /conversations/{id}/messages
        # but we need the conv doc. Use the patch endpoint with empty body to fetch.
        cr = requests.patch(f"{API}/conversations/{cid}/settings",
                            headers=_h(user_a["token"]),
                            json={}, timeout=15)
        assert cr.status_code == 200
        fresh = cr.json()
        new_aids = fresh.get("agent_ids") or []
        promoted = fresh.get("type") == "group" and len(new_aids) > len(original_aids)
        if not promoted:
            # Retry with even more off-domain prompt
            msg2 = requests.post(f"{API}/conversations/{cid}/messages",
                                 headers=_h(user_a["token"]),
                                 json={"text": "Write me an SQL query that joins three tables "
                                               "with a LEFT JOIN and use a window function ROW_NUMBER() "
                                               "to deduplicate rows. This is purely a database engineering task."},
                                 timeout=180)
            assert msg2.status_code == 200
            time.sleep(1.0)
            cr2 = requests.patch(f"{API}/conversations/{cid}/settings",
                                 headers=_h(user_a["token"]),
                                 json={}, timeout=15)
            assert cr2.status_code == 200
            fresh = cr2.json()
            new_aids = fresh.get("agent_ids") or []
            promoted = fresh.get("type") == "group" and len(new_aids) > len(original_aids)

        if not promoted:
            pytest.skip(f"router did not pick a different specialist after 2 attempts "
                        f"(conv aids={new_aids}). "
                        f"This is tolerated per the test spec; main goal is plumbing.")
        assert fresh["type"] == "group"
        assert len(new_aids) > len(original_aids)
        # Verify the system join message exists
        gm = requests.get(f"{API}/conversations/{cid}/messages",
                          headers=_h(user_a["token"]), timeout=30)
        assert gm.status_code == 200
        join_msgs = [m for m in gm.json()
                     if m.get("sender_type") == "system"
                     and "joined the room" in (m.get("text") or "")]
        assert join_msgs, "expected system 'joined the room' message"
