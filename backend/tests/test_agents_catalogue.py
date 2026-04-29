"""Backend tests for the expanded 169-agent catalogue + mention-by-short-slug.

Covers (from review_request):
  * GET /api/agents/divisions
  * GET /api/teams/{id}/agents (no filter, by division, by search query, with limit)
  * direct conversations with the new agent ids
  * group conversations with @mention by short-slug, full id, plain text (no reply)
  * group with auto_respond=true → all agents reply
  * access control (403) + invalid agent id (400) + invalid division (empty list)
"""
import os
import uuid
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for ln in f:
            if ln.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = ln.strip().split("=", 1)[1]
                break
BASE_URL = (BASE_URL or "").rstrip("/")
assert BASE_URL, "REACT_APP_BACKEND_URL not configured"
API = f"{BASE_URL}/api"


def _uniq(p="t"):
    return f"TEST_{p}_{uuid.uuid4().hex[:10]}"


def _sess():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


def _register(s, name=None):
    name = name or _uniq("user")
    email = f"{name.lower()}@test.dev"
    r = s.post(f"{API}/auth/register", json={"name": name, "email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="module")
def ctx():
    s = _sess()
    _register(s)
    org = s.post(f"{API}/orgs", json={"name": _uniq("org")}).json()
    team = s.post(f"{API}/orgs/{org['id']}/teams", json={"name": _uniq("team"), "description": "d"}).json()
    return {"s": s, "org_id": org["id"], "team_id": team["id"]}


# ── Agents catalogue ─────────────────────────────────────────────────────────
class TestDivisionsAndAgents:
    def test_divisions(self, ctx):
        r = ctx["s"].get(f"{API}/agents/divisions")
        assert r.status_code == 200
        divs = r.json()
        assert len(divs) == 14, f"expected 14 divisions, got {len(divs)}: {[d['key'] for d in divs]}"
        total = sum(d["count"] for d in divs)
        assert total == 169, f"expected sum=169, got {total}"
        keymap = {d["key"]: d["count"] for d in divs}
        assert keymap.get("engineering") == 29
        assert keymap.get("marketing") == 30
        assert keymap.get("specialized") == 41
        for d in divs:
            assert d["count"] > 0
            assert isinstance(d["label"], str) and d["label"]

    def test_agents_default_169_sorted_no_system(self, ctx):
        r = ctx["s"].get(f"{API}/teams/{ctx['team_id']}/agents")
        assert r.status_code == 200
        items = r.json()
        assert len(items) == 169
        required = {"id", "name", "division", "division_key", "description", "tagline", "emoji", "accent"}
        for a in items[:5] + items[-5:]:
            assert required.issubset(a.keys()), f"missing keys: {required - set(a.keys())}"
            assert "system" not in a
        # sorted by (division, name)
        keys = [(a["division"], a["name"]) for a in items]
        assert keys == sorted(keys), "agents should be sorted by (division, name)"

    def test_filter_by_engineering(self, ctx):
        r = ctx["s"].get(f"{API}/teams/{ctx['team_id']}/agents", params={"division": "engineering"})
        assert r.status_code == 200
        items = r.json()
        assert len(items) == 29
        assert all(a["division_key"] == "engineering" for a in items)

    def test_filter_by_specialized(self, ctx):
        r = ctx["s"].get(f"{API}/teams/{ctx['team_id']}/agents", params={"division": "specialized"})
        assert r.status_code == 200
        items = r.json()
        assert len(items) == 41
        assert all(a["division_key"] == "specialized" for a in items)

    def test_search_frontend(self, ctx):
        r = ctx["s"].get(f"{API}/teams/{ctx['team_id']}/agents", params={"q": "frontend"})
        assert r.status_code == 200
        items = r.json()
        ids = {a["id"] for a in items}
        assert len(items) > 0
        assert "engineering-frontend-developer" in ids

    def test_search_brand(self, ctx):
        r = ctx["s"].get(f"{API}/teams/{ctx['team_id']}/agents", params={"q": "brand"})
        assert r.status_code == 200
        items = r.json()
        ids = {a["id"] for a in items}
        assert "design-brand-guardian" in ids

    def test_limit_caps_results(self, ctx):
        r = ctx["s"].get(f"{API}/teams/{ctx['team_id']}/agents", params={"limit": 5})
        assert r.status_code == 200
        assert len(r.json()) == 5

    def test_invalid_division_returns_empty(self, ctx):
        r = ctx["s"].get(f"{API}/teams/{ctx['team_id']}/agents", params={"division": "no_such_division"})
        assert r.status_code == 200
        assert r.json() == []

    def test_team_agents_forbidden_for_outsider(self, ctx):
        outsider = _sess(); _register(outsider)
        r = outsider.get(f"{API}/teams/{ctx['team_id']}/agents")
        assert r.status_code == 403


# ── Direct conversations with new agent ids ──────────────────────────────────
DIRECT_AGENTS = [
    ("engineering-senior-developer", "Senior Developer"),
    ("design-brand-guardian", "Brand Guardian"),
    ("marketing-content-creator", "Content Creator"),
]


class TestDirectConversations:
    @pytest.mark.parametrize("agent_id,expected_name", DIRECT_AGENTS)
    def test_direct_with_new_agent(self, ctx, agent_id, expected_name):
        s, tid = ctx["s"], ctx["team_id"]
        r = s.post(f"{API}/teams/{tid}/conversations",
                   json={"type": "direct", "agent_ids": [agent_id], "title": f"TEST_{agent_id}"})
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        r = s.post(f"{API}/conversations/{cid}/messages",
                   json={"text": "Briefly say hi in one sentence."})
        assert r.status_code == 200, r.text
        data = r.json()
        replies = data["agent_messages"]
        assert len(replies) == 1
        assert replies[0]["agent_id"] == agent_id
        assert replies[0]["sender_name"] == expected_name
        assert isinstance(replies[0]["text"], str) and len(replies[0]["text"].strip()) > 0
        s.delete(f"{API}/conversations/{cid}")

    def test_invalid_agent_id_400(self, ctx):
        s, tid = ctx["s"], ctx["team_id"]
        r = s.post(f"{API}/teams/{tid}/conversations",
                   json={"type": "direct", "agent_ids": ["not-a-real-agent"]})
        assert r.status_code == 400


# ── Group conversations with mention-by-short-slug ───────────────────────────
class TestGroupMentionShortSlug:
    @pytest.fixture
    def group_conv(self, ctx):
        s, tid = ctx["s"], ctx["team_id"]
        r = s.post(f"{API}/teams/{tid}/conversations", json={
            "type": "group",
            "agent_ids": [
                "engineering-frontend-developer",
                "design-ui-designer",
                "marketing-content-creator",
            ],
            "auto_respond": False,
            "title": "TEST_group_short_slug",
        })
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        yield cid
        s.delete(f"{API}/conversations/{cid}")

    def test_short_slug_frontend_developer(self, ctx, group_conv):
        s = ctx["s"]
        r = s.post(f"{API}/conversations/{group_conv}/messages",
                   json={"text": "@frontend-developer can you prototype a hero?"})
        assert r.status_code == 200, r.text
        replies = r.json()["agent_messages"]
        assert len(replies) == 1, f"expected 1, got {[m['agent_id'] for m in replies]}"
        assert replies[0]["agent_id"] == "engineering-frontend-developer"

    def test_short_slug_ui_designer(self, ctx, group_conv):
        s = ctx["s"]
        r = s.post(f"{API}/conversations/{group_conv}/messages",
                   json={"text": "@ui-designer what would you change?"})
        assert r.status_code == 200, r.text
        replies = r.json()["agent_messages"]
        assert len(replies) == 1, f"expected 1, got {[m['agent_id'] for m in replies]}"
        assert replies[0]["agent_id"] == "design-ui-designer"

    def test_short_slug_content_creator(self, ctx, group_conv):
        s = ctx["s"]
        r = s.post(f"{API}/conversations/{group_conv}/messages",
                   json={"text": "@content-creator write a tagline"})
        assert r.status_code == 200, r.text
        replies = r.json()["agent_messages"]
        assert len(replies) == 1, f"expected 1, got {[m['agent_id'] for m in replies]}"
        assert replies[0]["agent_id"] == "marketing-content-creator"

    def test_full_id_mention(self, ctx, group_conv):
        s = ctx["s"]
        r = s.post(f"{API}/conversations/{group_conv}/messages",
                   json={"text": "@engineering-frontend-developer please ship"})
        assert r.status_code == 200, r.text
        replies = r.json()["agent_messages"]
        assert len(replies) == 1
        assert replies[0]["agent_id"] == "engineering-frontend-developer"

    def test_no_mention_no_reply(self, ctx, group_conv):
        s = ctx["s"]
        r = s.post(f"{API}/conversations/{group_conv}/messages",
                   json={"text": "Hi everyone, just a plain message."})
        assert r.status_code == 200, r.text
        assert r.json()["agent_messages"] == []


class TestGroupAutoRespond:
    def test_auto_respond_replies_from_all(self, ctx):
        s, tid = ctx["s"], ctx["team_id"]
        r = s.post(f"{API}/teams/{tid}/conversations", json={
            "type": "group",
            "agent_ids": ["engineering-frontend-developer", "design-ui-designer"],
            "auto_respond": True,
            "title": "TEST_auto",
        })
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        try:
            r = s.post(f"{API}/conversations/{cid}/messages",
                       json={"text": "Plain message — both should chime in."})
            assert r.status_code == 200, r.text
            ids = {m["agent_id"] for m in r.json()["agent_messages"]}
            assert ids == {"engineering-frontend-developer", "design-ui-designer"}, f"got {ids}"
        finally:
            s.delete(f"{API}/conversations/{cid}")
