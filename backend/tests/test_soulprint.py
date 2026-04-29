"""Backend integration tests for SoulPrint Engine.

Covers: auth, orgs, invites, teams/soulprint, agents catalogue, direct & group
conversations (with @mention + auto_respond), access control, and LLM reply.
"""
import os, uuid, time, pytest, requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/") if os.environ.get("REACT_APP_BACKEND_URL") else None
if not BASE_URL:
    # Fallback read from frontend/.env if env var not set in process
    with open("/app/frontend/.env") as f:
        for ln in f:
            if ln.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = ln.strip().split("=", 1)[1].rstrip("/")
                break

assert BASE_URL, "REACT_APP_BACKEND_URL not configured"

API = f"{BASE_URL}/api"


def _uniq(prefix="t"):
    return f"TEST_{prefix}_{uuid.uuid4().hex[:10]}"


def _new_session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


def _register(s, name=None, email=None, password="password123"):
    name = name or _uniq("user")
    email = email or f"{name.lower()}@test.dev"
    r = s.post(f"{API}/auth/register", json={"name": name, "email": email, "password": password})
    assert r.status_code == 200, f"register failed: {r.status_code} {r.text}"
    return r.json(), email, password


# ── Auth ──────────────────────────────────────────────────────────────────────
class TestAuth:
    def test_register_login_me_logout(self):
        s = _new_session()
        u, email, pw = _register(s)
        assert u["email"] == email and "id" in u
        # /me via cookie
        r = s.get(f"{API}/auth/me")
        assert r.status_code == 200
        assert r.json()["email"] == email
        # logout clears cookie
        r = s.post(f"{API}/auth/logout")
        assert r.status_code == 200
        r = s.get(f"{API}/auth/me")
        assert r.status_code == 401
        # login again
        r = s.post(f"{API}/auth/login", json={"email": email, "password": pw})
        assert r.status_code == 200
        r = s.get(f"{API}/auth/me")
        assert r.status_code == 200

    def test_login_invalid(self):
        s = _new_session()
        r = s.post(f"{API}/auth/login", json={"email": "nope@x.com", "password": "xxxxxx"})
        assert r.status_code == 401


# ── Orgs + Invites ────────────────────────────────────────────────────────────
class TestOrgsInvites:
    def test_org_crud_and_members(self):
        s = _new_session()
        _register(s)
        r = s.post(f"{API}/orgs", json={"name": _uniq("org")})
        assert r.status_code == 200
        org = r.json()
        assert "id" in org and org["name"].startswith("TEST_")
        # list contains org with role
        r = s.get(f"{API}/orgs")
        assert r.status_code == 200
        items = r.json()
        assert any(o["id"] == org["id"] and o["role"] == "owner" for o in items)
        # get one with role
        r = s.get(f"{API}/orgs/{org['id']}")
        assert r.status_code == 200 and r.json()["role"] == "owner"
        # members
        r = s.get(f"{API}/orgs/{org['id']}/members")
        assert r.status_code == 200 and len(r.json()) == 1

    def test_other_user_cannot_see_org(self):
        s1 = _new_session(); _register(s1)
        r = s1.post(f"{API}/orgs", json={"name": _uniq("org")})
        org_id = r.json()["id"]
        s2 = _new_session(); _register(s2)
        r = s2.get(f"{API}/orgs/{org_id}")
        assert r.status_code == 403
        r = s2.get(f"{API}/orgs/{org_id}/members")
        assert r.status_code == 403

    def test_invite_flow_and_member_cannot_invite(self):
        # owner creates org + invite
        owner = _new_session(); _register(owner)
        org_id = owner.post(f"{API}/orgs", json={"name": _uniq("org")}).json()["id"]
        r = owner.post(f"{API}/orgs/{org_id}/invites", json={"role": "member"})
        assert r.status_code == 200
        code = r.json()["code"]
        # second user accepts
        u2 = _new_session(); _register(u2)
        r = u2.post(f"{API}/invites/accept", json={"code": code})
        assert r.status_code == 200 and r.json()["org_id"] == org_id
        # invite is single-use
        u3 = _new_session(); _register(u3)
        r = u3.post(f"{API}/invites/accept", json={"code": code})
        assert r.status_code == 404
        # member role cannot create invites
        r = u2.post(f"{API}/orgs/{org_id}/invites", json={"role": "member"})
        assert r.status_code == 403


# ── Teams + SoulPrint + Agents ────────────────────────────────────────────────
@pytest.fixture(scope="module")
def owner_ctx():
    s = _new_session()
    u, email, pw = _register(s)
    org_id = s.post(f"{API}/orgs", json={"name": _uniq("org")}).json()["id"]
    team = s.post(f"{API}/orgs/{org_id}/teams", json={"name": _uniq("team"), "description": "d"}).json()
    return {"sess": s, "user": u, "org_id": org_id, "team": team}


class TestTeams:
    def test_team_create_list_get(self, owner_ctx):
        s, org_id, team = owner_ctx["sess"], owner_ctx["org_id"], owner_ctx["team"]
        assert "id" in team and team["org_id"] == org_id
        r = s.get(f"{API}/orgs/{org_id}/teams")
        assert r.status_code == 200 and any(t["id"] == team["id"] for t in r.json())
        r = s.get(f"{API}/teams/{team['id']}")
        assert r.status_code == 200

    def test_soulprint_persists(self, owner_ctx):
        s, tid = owner_ctx["sess"], owner_ctx["team"]["id"]
        payload = {
            "mission": "Help SMBs thrive",
            "tone": "warm, direct, no jargon",
            "audience": "solo founders",
            "values": "bias to action",
            "north_star": "every customer ships weekly",
        }
        r = s.put(f"{API}/teams/{tid}/soulprint", json=payload)
        assert r.status_code == 200
        r = s.get(f"{API}/teams/{tid}")
        sp = r.json()["soulprint"]
        for k, v in payload.items():
            assert sp[k] == v

    def test_agents_catalogue(self, owner_ctx):
        r = owner_ctx["sess"].get(f"{API}/teams/{owner_ctx['team']['id']}/agents")
        assert r.status_code == 200
        items = r.json()
        # Catalogue is now 169 agents — just sanity-check shape.
        assert len(items) == 169
        ids = {a["id"] for a in items}
        for expected in ("engineering-frontend-developer", "design-brand-guardian",
                         "marketing-content-creator", "engineering-senior-developer"):
            assert expected in ids
        for a in items[:5]:
            assert {"id", "name", "accent"}.issubset(a.keys())

    def test_team_access_forbidden(self, owner_ctx):
        tid = owner_ctx["team"]["id"]
        stranger = _new_session(); _register(stranger)
        r = stranger.get(f"{API}/teams/{tid}")
        assert r.status_code == 403


# ── Conversations ─────────────────────────────────────────────────────────────
class TestConversations:
    def test_direct_conversation_single_reply(self, owner_ctx):
        s, tid = owner_ctx["sess"], owner_ctx["team"]["id"]
        r = s.post(f"{API}/teams/{tid}/conversations",
                   json={"type": "direct", "agent_ids": ["engineering-senior-developer"], "title": "TEST_direct"})
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        r = s.post(f"{API}/conversations/{cid}/messages",
                   json={"text": "Give me one sharp positioning idea for a B2B SaaS."})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["user_message"]["text"].startswith("Give me")
        assert len(data["agent_messages"]) == 1
        assert data["agent_messages"][0]["agent_id"] == "engineering-senior-developer"
        assert len(data["agent_messages"][0]["text"]) > 0
        # cleanup
        s.delete(f"{API}/conversations/{cid}")

    def test_direct_validation_errors(self, owner_ctx):
        s, tid = owner_ctx["sess"], owner_ctx["team"]["id"]
        r = s.post(f"{API}/teams/{tid}/conversations",
                   json={"type": "direct", "agent_ids": ["engineering-senior-developer", "design-brand-guardian"]})
        assert r.status_code == 400
        r = s.post(f"{API}/teams/{tid}/conversations",
                   json={"type": "direct", "agent_ids": ["unknown_agent"]})
        assert r.status_code == 400

    def test_group_mention_only(self, owner_ctx):
        s, tid = owner_ctx["sess"], owner_ctx["team"]["id"]
        r = s.post(f"{API}/teams/{tid}/conversations",
                   json={"type": "group", "agent_ids": ["marketing-content-creator", "engineering-senior-developer"],
                         "auto_respond": False, "title": "TEST_group_mention"})
        assert r.status_code == 200
        cid = r.json()["id"]
        # No mention → no agent replies
        r = s.post(f"{API}/conversations/{cid}/messages", json={"text": "hello team, plain message"})
        assert r.status_code == 200
        assert r.json()["agent_messages"] == []
        # @content-creator (short slug) → only marketing-content-creator
        r = s.post(f"{API}/conversations/{cid}/messages",
                   json={"text": "@content-creator write me a one-liner for our onboarding email"})
        assert r.status_code == 200
        replies = r.json()["agent_messages"]
        assert len(replies) == 1 and replies[0]["agent_id"] == "marketing-content-creator"
        # @engineering-senior-developer by full id → only that agent
        r = s.post(f"{API}/conversations/{cid}/messages",
                   json={"text": "@engineering-senior-developer what is our edge?"})
        assert r.status_code == 200
        replies = r.json()["agent_messages"]
        assert len(replies) == 1 and replies[0]["agent_id"] == "engineering-senior-developer"
        s.delete(f"{API}/conversations/{cid}")

    def test_group_auto_respond(self, owner_ctx):
        s, tid = owner_ctx["sess"], owner_ctx["team"]["id"]
        r = s.post(f"{API}/teams/{tid}/conversations",
                   json={"type": "group", "agent_ids": ["marketing-content-creator", "design-ui-designer"],
                         "auto_respond": True, "title": "TEST_group_auto"})
        assert r.status_code == 200
        cid = r.json()["id"]
        r = s.post(f"{API}/conversations/{cid}/messages",
                   json={"text": "Plain message — no mention, autopilot."})
        assert r.status_code == 200
        replies = r.json()["agent_messages"]
        agent_ids = {m["agent_id"] for m in replies}
        assert agent_ids == {"marketing-content-creator", "design-ui-designer"}, f"got {agent_ids}"
        s.delete(f"{API}/conversations/{cid}")

    def test_conversation_access_and_delete(self, owner_ctx):
        s, tid = owner_ctx["sess"], owner_ctx["team"]["id"]
        r = s.post(f"{API}/teams/{tid}/conversations",
                   json={"type": "direct", "agent_ids": ["engineering-senior-developer"]})
        cid = r.json()["id"]
        # stranger cannot read messages
        stranger = _new_session(); _register(stranger)
        r = stranger.get(f"{API}/conversations/{cid}/messages")
        assert r.status_code in (403, 404)
        # Add stranger to same org via invite → still not a conv member, should 403
        owner_sess = s
        code = owner_sess.post(f"{API}/orgs/{owner_ctx['org_id']}/invites",
                               json={"role": "member"}).json()["code"]
        stranger.post(f"{API}/invites/accept", json={"code": code})
        # Now stranger is org member and can read team (team-membership semantics)
        r = stranger.get(f"{API}/teams/{tid}")
        assert r.status_code == 200
        # But still not a conversation participant
        r = stranger.get(f"{API}/conversations/{cid}/messages")
        assert r.status_code == 403
        # Stranger cannot delete
        r = stranger.delete(f"{API}/conversations/{cid}")
        assert r.status_code in (403, 404)
        # Creator can delete
        r = s.delete(f"{API}/conversations/{cid}")
        assert r.status_code == 200

    def test_soulprint_is_used_reply_nonempty(self, owner_ctx):
        # SoulPrint is already set in TestTeams.test_soulprint_persists (same module-scope fixture)
        s, tid = owner_ctx["sess"], owner_ctx["team"]["id"]
        r = s.post(f"{API}/teams/{tid}/conversations",
                   json={"type": "direct", "agent_ids": ["marketing-content-creator"]})
        cid = r.json()["id"]
        r = s.post(f"{API}/conversations/{cid}/messages",
                   json={"text": "Introduce yourself in one sentence."})
        assert r.status_code == 200
        reply = r.json()["agent_messages"][0]["text"]
        assert isinstance(reply, str) and len(reply.strip()) > 5
        s.delete(f"{API}/conversations/{cid}")
