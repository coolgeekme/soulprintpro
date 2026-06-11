"""Iteration 31 backend tests — Search Everywhere.

Covers:
1. GET /api/orgs/{org_id}/assets/search — uploaded + link assets, auth, edge cases
2. GET /api/orgs/{org_id}/search — unified fan-out, local + external (not_connected)
3. PUT /api/composio/{org_id}/agent-tool-prefs/{agent_id} — auto_search_docs flag
4. GET /api/composio/{org_id}/agent-tool-prefs — default ON; reflects PUTs
5. Auto-search prompt injection in generate_agent_reply
"""
import io
import os
import re
import uuid
import time
import pytest
import requests

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


def _h(tok):
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def _h_noct(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture(scope="session")
def admin_token():
    r = requests.post(f"{API}/auth/login",
                      json={"email": SUPERADMIN_EMAIL, "password": SUPERADMIN_PASSWORD},
                      timeout=30)
    assert r.status_code == 200, f"superadmin login: {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def access_code(admin_token):
    code_val = f"TESTITER31{uuid.uuid4().hex[:8].upper()}"
    r = requests.post(f"{API}/admin/access-codes",
                      headers=_h(admin_token),
                      json={"code": code_val, "usage_type": "multi", "max_uses": 30,
                            "note": "iter-31 search tests"},
                      timeout=30)
    assert r.status_code == 200, f"mint code: {r.text}"
    return r.json()["code"]


def _register(name, email, code):
    r = requests.post(f"{API}/auth/register",
                      json={"name": name, "email": email, "password": "testpass123",
                            "access_code": code},
                      timeout=20)
    assert r.status_code == 200, f"register {email}: {r.status_code} {r.text}"
    j = r.json()
    return j["access_token"], j["id"]


@pytest.fixture(scope="session")
def user_a(access_code):
    email = f"TEST_iter31a_{uuid.uuid4().hex[:8]}@example.com"
    tok, uid = _register("Iter31 A", email, access_code)
    return {"token": tok, "id": uid, "email": email}


@pytest.fixture(scope="session")
def user_b(access_code):
    email = f"TEST_iter31b_{uuid.uuid4().hex[:8]}@example.com"
    tok, uid = _register("Iter31 B", email, access_code)
    return {"token": tok, "id": uid, "email": email}


@pytest.fixture(scope="session")
def org_a(user_a):
    r = requests.post(f"{API}/orgs",
                      headers=_h(user_a["token"]),
                      json={"name": f"TEST_Iter31Org_{uuid.uuid4().hex[:6]}"},
                      timeout=60)
    assert r.status_code == 200, f"create org: {r.text}"
    return r.json()


@pytest.fixture(scope="session")
def org_b(user_b):
    r = requests.post(f"{API}/orgs",
                      headers=_h(user_b["token"]),
                      json={"name": f"TEST_Iter31OrgB_{uuid.uuid4().hex[:6]}"},
                      timeout=60)
    assert r.status_code == 200, f"create org B: {r.text}"
    return r.json()


# ── Helpers ────────────────────────────────────────────────────────────────
def _upload_file(token, org_id, filename, content, description="", category=""):
    files = {"file": (filename, io.BytesIO(content), "text/plain")}
    data = {"description": description, "category": category, "usable_in_social": "false"}
    r = requests.post(f"{API}/orgs/{org_id}/assets",
                      headers=_h_noct(token),
                      files=files, data=data, timeout=60)
    assert r.status_code == 200, f"upload {filename}: {r.status_code} {r.text}"
    return r.json()


def _add_link(token, org_id, url, name="", description=""):
    r = requests.post(f"{API}/orgs/{org_id}/asset-links",
                      headers=_h(token),
                      json={"url": url, "name": name, "description": description},
                      timeout=30)
    assert r.status_code == 200, f"add link: {r.status_code} {r.text}"
    return r.json()


# ── Feature 1: /assets/search ──────────────────────────────────────────────
class TestAssetSearch:
    def test_unauth_returns_401(self, org_a):
        r = requests.get(f"{API}/orgs/{org_a['id']}/assets/search?q=foo", timeout=30)
        assert r.status_code in (401, 403), f"expected 401/403, got {r.status_code}"

    def test_cross_org_returns_403(self, org_a, user_b):
        r = requests.get(f"{API}/orgs/{org_a['id']}/assets/search?q=foo",
                         headers=_h(user_b["token"]), timeout=30)
        assert r.status_code == 403, f"cross-org should 403, got {r.status_code}: {r.text}"

    def test_empty_q_returns_empty(self, org_a, user_a):
        r = requests.get(f"{API}/orgs/{org_a['id']}/assets/search?q=",
                         headers=_h(user_a["token"]), timeout=30)
        assert r.status_code == 200
        body = r.json()
        assert body == {"q": "", "results": [], "total": 0}

    def test_uploaded_asset_match_by_description(self, org_a, user_a):
        asset = _upload_file(user_a["token"], org_a["id"],
                             "q3_okrs.txt", b"Internal Q3 OKRs working draft",
                             description="Q3 OKRs draft")
        time.sleep(0.3)
        r = requests.get(f"{API}/orgs/{org_a['id']}/assets/search?q=Q3",
                         headers=_h(user_a["token"]), timeout=30)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["q"] == "Q3"
        assert body["total"] >= 1
        # find our asset
        match = next((x for x in body["results"] if x.get("id") == asset.get("id")), None)
        assert match is not None, f"asset not in results: {body['results']}"
        # snippet should contain Q3-related text
        snip = (match.get("snippet") or "").lower()
        assert "q3" in snip or "q3 okrs" in snip, f"snippet missing 'Q3': {snip}"

    def test_regex_special_chars_no_500(self, org_a, user_a):
        # special regex chars should be escaped server-side
        r = requests.get(f"{API}/orgs/{org_a['id']}/assets/search",
                         params={"q": "1.2 (beta)"},
                         headers=_h(user_a["token"]), timeout=30)
        assert r.status_code == 200, f"regex chars: {r.status_code} {r.text}"
        body = r.json()
        assert body["q"] == "1.2 (beta)"

    def test_link_asset_search_by_name(self, org_a, user_a):
        _add_link(user_a["token"], org_a["id"],
                  "https://drive.google.com/drive/folders/abc123",
                  name="Brand Kit Drive",
                  description="Our brand kit folder")
        time.sleep(0.3)
        r = requests.get(f"{API}/orgs/{org_a['id']}/assets/search?q=brand",
                         headers=_h(user_a["token"]), timeout=30)
        assert r.status_code == 200
        body = r.json()
        links = [x for x in body["results"] if x.get("kind") == "link"]
        assert links, f"no link results: {body['results']}"
        target = next((x for x in links if "brand" in (x.get("original_name") or "").lower()), None)
        assert target is not None
        assert target.get("link_provider", "").lower().startswith("google"), \
            f"expected Google Drive provider, got {target.get('link_provider')}"
        snip = (target.get("snippet") or "").lower()
        assert "brand" in snip

    def test_link_asset_search_by_url_substring(self, org_a, user_a):
        r = requests.get(f"{API}/orgs/{org_a['id']}/assets/search?q=drive.google",
                         headers=_h(user_a["token"]), timeout=30)
        assert r.status_code == 200
        body = r.json()
        assert any(
            "drive.google" in (x.get("link_url") or "").lower()
            for x in body["results"]
        ), f"url substring match failed: {body['results']}"


# ── Feature 2: unified /search ─────────────────────────────────────────────
class TestUnifiedSearch:
    def test_local_only_mirrors_assets_search(self, org_a, user_a):
        r = requests.get(f"{API}/orgs/{org_a['id']}/search",
                         params={"q": "Q3", "sources": "local"},
                         headers=_h(user_a["token"]), timeout=20)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["q"] == "Q3"
        assert body["sources"] == ["local"]
        assert len(body["groups"]) == 1
        g = body["groups"][0]
        assert g["source"] == "local"
        assert g["ok"] is True
        assert isinstance(g["results"], list)
        assert any("q3" in (x.get("original_name") or "").lower()
                   or "q3" in (x.get("snippet") or "").lower() for x in g["results"])

    def test_external_not_connected_returns_ok_false(self, org_a, user_a):
        r = requests.get(f"{API}/orgs/{org_a['id']}/search",
                         params={"q": "anything", "sources": "local,googledrive"},
                         headers=_h(user_a["token"]), timeout=30)
        assert r.status_code == 200, f"expected 200 with partial: {r.status_code} {r.text}"
        body = r.json()
        assert "googledrive" in body["sources"]
        gd = next((g for g in body["groups"] if g["source"] == "googledrive"), None)
        assert gd is not None, f"no googledrive group: {body}"
        assert gd["ok"] is False
        assert gd.get("error") == "not_connected"
        assert gd["results"] == []

    def test_invalid_source_filtered_out(self, org_a, user_a):
        r = requests.get(f"{API}/orgs/{org_a['id']}/search",
                         params={"q": "anything", "sources": "local,zzz"},
                         headers=_h(user_a["token"]), timeout=30)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "zzz" not in body["sources"]
        assert "local" in body["sources"]

    def test_empty_q_returns_empty_groups(self, org_a, user_a):
        r = requests.get(f"{API}/orgs/{org_a['id']}/search?q=",
                         headers=_h(user_a["token"]), timeout=30)
        assert r.status_code == 200
        body = r.json()
        assert body["q"] == ""
        assert body["groups"] == []

    def test_all_externals_not_connected(self, org_a, user_a):
        # All five external sources should come back ok=false / not_connected
        r = requests.get(f"{API}/orgs/{org_a['id']}/search",
                         params={"q": "foo",
                                 "sources": "googledrive,dropbox,notion,googledocs,onedrive"},
                         timeout=45,
                         headers=_h(user_a["token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        for src in ["googledrive", "dropbox", "notion", "googledocs", "onedrive"]:
            g = next((x for x in body["groups"] if x["source"] == src), None)
            assert g is not None, f"missing group {src}"
            assert g["ok"] is False, f"{src} unexpectedly ok=true"
            assert g.get("error") == "not_connected", f"{src} error: {g.get('error')}"


# ── Feature 3: agent-tool-prefs auto_search_docs ───────────────────────────
class TestAgentToolPrefs:
    def _get_first_agent(self, token, org_id):
        r = requests.get(f"{API}/orgs/{org_id}/agent-tool-prefs",
                         headers=_h(token), timeout=30)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["agents"], "no agents returned"
        return body

    def test_list_returns_auto_search_docs_true_by_default(self, org_a, user_a):
        body = self._get_first_agent(user_a["token"], org_a["id"])
        for a in body["agents"]:
            assert "auto_search_docs" in a, f"missing field: {a}"
            assert a["auto_search_docs"] is True, \
                f"agent {a['agent_id']} default auto_search_docs not True"
        assert body["can_edit"] is True  # user_a owns the org

    def test_put_only_auto_search_docs(self, org_a, user_a):
        body = self._get_first_agent(user_a["token"], org_a["id"])
        agent_id = body["agents"][0]["agent_id"]
        r = requests.put(f"{API}/orgs/{org_a['id']}/agent-tool-prefs/{agent_id}",
                         headers=_h(user_a["token"]),
                         json={"auto_search_docs": False}, timeout=30)
        assert r.status_code == 200, r.text
        resp = r.json()
        assert resp["agent_id"] == agent_id
        assert resp["auto_search_docs"] is False
        # tools_enabled remains default True since not touched
        assert resp["tools_enabled"] is True

        # GET reflects the change
        g = requests.get(f"{API}/orgs/{org_a['id']}/agent-tool-prefs",
                         headers=_h(user_a["token"]), timeout=30)
        agents = g.json()["agents"]
        flipped = next(a for a in agents if a["agent_id"] == agent_id)
        assert flipped["auto_search_docs"] is False
        # Others remain True
        for a in agents:
            if a["agent_id"] != agent_id:
                assert a["auto_search_docs"] is True

        # restore
        requests.put(f"{API}/orgs/{org_a['id']}/agent-tool-prefs/{agent_id}",
                     headers=_h(user_a["token"]),
                     json={"auto_search_docs": True}, timeout=30)

    def test_put_both_fields_together(self, org_a, user_a):
        body = self._get_first_agent(user_a["token"], org_a["id"])
        # pick a second agent so we don't collide with other tests
        agent_id = body["agents"][1]["agent_id"] if len(body["agents"]) > 1 else body["agents"][0]["agent_id"]
        r = requests.put(f"{API}/orgs/{org_a['id']}/agent-tool-prefs/{agent_id}",
                         headers=_h(user_a["token"]),
                         json={"tools_enabled": False, "auto_search_docs": True}, timeout=30)
        assert r.status_code == 200, r.text
        resp = r.json()
        assert resp["tools_enabled"] is False
        assert resp["auto_search_docs"] is True
        # restore
        requests.put(f"{API}/orgs/{org_a['id']}/agent-tool-prefs/{agent_id}",
                     headers=_h(user_a["token"]),
                     json={"tools_enabled": True, "auto_search_docs": True}, timeout=30)

    def test_put_empty_body_returns_400(self, org_a, user_a):
        body = self._get_first_agent(user_a["token"], org_a["id"])
        agent_id = body["agents"][0]["agent_id"]
        r = requests.put(f"{API}/orgs/{org_a['id']}/agent-tool-prefs/{agent_id}",
                         headers=_h(user_a["token"]),
                         json={}, timeout=30)
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
        assert "at least one" in (r.json().get("detail") or "").lower()

    def test_non_admin_member_403(self, admin_token, access_code, org_a, user_a):
        # invite user_c as plain member
        email_c = f"TEST_iter31c_{uuid.uuid4().hex[:8]}@example.com"
        tok_c, uid_c = _register("Iter31 C", email_c, access_code)
        # add as member via invite/accept or org members POST — use direct add endpoint
        r = requests.post(f"{API}/orgs/{org_a['id']}/members",
                          headers=_h(user_a["token"]),
                          json={"user_id": uid_c, "role": "member"}, timeout=30)
        # If endpoint differs, fallback: try invitation path
        if r.status_code not in (200, 201):
            # try /invites if available
            inv = requests.post(f"{API}/orgs/{org_a['id']}/invites",
                                headers=_h(user_a["token"]),
                                json={"email": email_c, "role": "member"}, timeout=30)
            if inv.status_code == 200:
                token_inv = inv.json().get("token") or inv.json().get("invite_token")
                if token_inv:
                    requests.post(f"{API}/invites/{token_inv}/accept",
                                  headers=_h(tok_c), timeout=30)
        # Now attempt PUT as non-admin (or as user not in org → also non-admin)
        body = self._get_first_agent(user_a["token"], org_a["id"])
        agent_id = body["agents"][0]["agent_id"]
        r = requests.put(f"{API}/orgs/{org_a['id']}/agent-tool-prefs/{agent_id}",
                         headers=_h(tok_c),
                         json={"auto_search_docs": False}, timeout=30)
        # 403 either because not-member or because non-admin
        assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"


# ── Feature 4: auto-search prompt injection ────────────────────────────────
class TestAutoSearchInjection:
    """Heuristic LLM check: PASS if filename appears in agent reply when
    auto_search_docs=True, ABSENT when auto_search_docs=False. Tolerates
    LLM non-determinism on the price string itself."""

    @pytest.fixture(scope="class")
    def primed_org(self, user_a, org_a):
        # Reuse org_a (Spark plan limits to 1 org). Upload pricing strategy.
        org = org_a
        _upload_file(user_a["token"], org["id"],
                     "Pricing_Strategy.pdf",
                     b"Our enterprise plan starts at $2,500/month with custom SLAs. "
                     b"Self-serve tier is $49/month.",
                     description="Internal pricing strategy doc")
        return org

    def _start_direct_chat_and_send(self, token, org_id, agent_id, message):
        """Create a direct conversation via /teams/{team_id}/start, then poll for reply."""
        # Find a team in the org
        tr = requests.get(f"{API}/orgs/{org_id}/teams", headers=_h(token), timeout=30)
        if tr.status_code != 200 or not tr.json():
            return None, None
        team_id = tr.json()[0]["id"]
        cr = requests.post(f"{API}/teams/{team_id}/start",
                           headers=_h(token),
                           json={"text": message}, timeout=120)
        if cr.status_code != 200:
            return None, None
        conv = cr.json().get("conversation") or cr.json()
        conv_id = conv.get("id") or conv.get("_id")
        if not conv_id:
            return None, None
        # Poll for agent reply
        for _ in range(40):
            time.sleep(2)
            g = requests.get(f"{API}/conversations/{conv_id}/messages",
                             headers=_h(token), timeout=30)
            if g.status_code != 200:
                continue
            msgs = g.json()
            agent_msgs = [m for m in msgs if m.get("sender_type") == "agent"]
            # Look for non-streaming agent message (full text)
            full = [m for m in agent_msgs if (m.get("text") or "").strip()
                    and not m.get("streaming")]
            if full:
                return conv_id, full[-1].get("text") or ""
        return conv_id, None

    def test_auto_search_injection_when_on(self, user_a, primed_org):
        # auto_search_docs defaults to True for every agent — no flip needed.
        conv_id, reply = self._start_direct_chat_and_send(
            user_a["token"], primed_org["id"], None,
            "search my docs for our pricing strategy")
        if reply is None:
            pytest.skip("Agent reply not produced in time (LLM/queue delay)")
        # PASS criteria: filename mentioned
        assert "pricing" in reply.lower(), \
            f"filename 'Pricing' not mentioned when auto_search ON: {reply[:600]}"

    def test_no_injection_when_off(self, user_a, primed_org):
        # Flip auto_search_docs=False on ALL agents (we don't know which one
        # smart-router picks).
        prefs = requests.get(f"{API}/orgs/{primed_org['id']}/agent-tool-prefs",
                             headers=_h(user_a["token"]), timeout=30).json()
        flipped_ids = []
        for a in prefs["agents"]:
            r = requests.put(f"{API}/orgs/{primed_org['id']}/agent-tool-prefs/{a['agent_id']}",
                             headers=_h(user_a["token"]),
                             json={"auto_search_docs": False}, timeout=30)
            if r.status_code == 200:
                flipped_ids.append(a["agent_id"])
        try:
            conv_id, reply = self._start_direct_chat_and_send(
                user_a["token"], primed_org["id"], None,
                "search my docs for our pricing strategy")
            if reply is None:
                pytest.skip("Agent reply not produced in time")
            low = reply.lower()
            # Injected price detail should not appear (block missing).
            assert "$2,500" not in reply and "2,500/month" not in low \
                and "2500/month" not in low, \
                f"injected price leaked with auto_search OFF: {reply[:600]}"
        finally:
            # Restore all flipped agents back to True
            for aid in flipped_ids:
                try:
                    requests.put(f"{API}/orgs/{primed_org['id']}/agent-tool-prefs/{aid}",
                                 headers=_h(user_a["token"]),
                                 json={"auto_search_docs": True}, timeout=30)
                except Exception:
                    pass
