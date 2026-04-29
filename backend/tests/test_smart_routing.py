"""Backend tests for the smart-routing feature set:
  * POST /api/teams/{team_id}/start            (smart-start router)
  * POST /api/conversations/{cid}/switch-agent (sticky direct chat handoff)
  * Group smart_expand=true / false           (auto-add new agent on topic shift)
  * /messages response now includes 'conversation'

LLM is non-deterministic; we assert on division-prefix, not exact agent ids.
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
    r = s.post(
        f"{API}/auth/register",
        json={"name": name, "email": email, "password": "password123"},
    )
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="module")
def ctx():
    s = _sess()
    _register(s)
    org = s.post(f"{API}/orgs", json={"name": _uniq("org")}).json()
    team = s.post(
        f"{API}/orgs/{org['id']}/teams",
        json={"name": _uniq("team"), "description": "d"},
    ).json()
    # Set a meaningful soulprint so the router has context
    s.put(
        f"{API}/teams/{team['id']}/soulprint",
        json={
            "mission": "Help SMBs grow",
            "tone": "warm, expert",
            "audience": "founders",
            "values": "ship fast",
            "north_star": "weekly customer wins",
        },
    )
    return {"s": s, "org_id": org["id"], "team_id": team["id"]}


# ── Smart Start router ──────────────────────────────────────────────────────
class TestSmartStart:
    @pytest.mark.parametrize("text,expected_div", [
        ("Our React app is sluggish — what should I check first to speed up renders?",
         "engineering"),
        ("Help me write a punchy LinkedIn post launching our new SaaS pricing tier.",
         "marketing"),
        ("How should we forecast monthly cash runway and present it to our board?",
         "finance"),
    ])
    def test_routes_to_correct_division(self, ctx, text, expected_div):
        s, tid = ctx["s"], ctx["team_id"]
        r = s.post(f"{API}/teams/{tid}/start", json={"text": text})
        assert r.status_code == 200, r.text
        data = r.json()
        for key in ("conversation", "routed_agent_id", "user_message", "agent_message"):
            assert key in data, f"missing key: {key}"
        agent_id = data["routed_agent_id"]
        # The router can pick adjacent divisions; assert prefix is sensible.
        assert agent_id.startswith(expected_div + "-") or expected_div in agent_id, (
            f"expected {expected_div}-prefixed agent for text {text!r}, got {agent_id}"
        )
        conv = data["conversation"]
        assert conv["type"] == "direct"
        assert conv["agent_ids"] == [agent_id]
        assert data["user_message"]["text"] == text
        assert data["agent_message"]["agent_id"] == agent_id
        assert isinstance(data["agent_message"]["text"], str)
        assert len(data["agent_message"]["text"].strip()) > 0
        # Cleanup
        s.delete(f"{API}/conversations/{conv['id']}")

    def test_empty_text_400(self, ctx):
        s, tid = ctx["s"], ctx["team_id"]
        r = s.post(f"{API}/teams/{tid}/start", json={"text": "   "})
        assert r.status_code == 400

    def test_non_member_blocked(self, ctx):
        outsider = _sess(); _register(outsider)
        r = outsider.post(
            f"{API}/teams/{ctx['team_id']}/start", json={"text": "hello there"}
        )
        assert r.status_code in (403, 404)


# ── Switch Agent ────────────────────────────────────────────────────────────
class TestSwitchAgent:
    @pytest.fixture
    def direct_conv(self, ctx):
        s, tid = ctx["s"], ctx["team_id"]
        r = s.post(
            f"{API}/teams/{tid}/conversations",
            json={
                "type": "direct",
                "agent_ids": ["engineering-frontend-developer"],
                "title": "TEST_switch_seed",
            },
        )
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        yield cid
        s.delete(f"{API}/conversations/{cid}")

    def test_switch_agent_happy_path(self, ctx, direct_conv):
        s = ctx["s"]
        cid = direct_conv
        target = "marketing-content-creator"
        r = s.post(
            f"{API}/conversations/{cid}/switch-agent",
            json={"agent_id": target, "text": "Need a marketing perspective now."},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["conversation"]["agent_ids"] == [target]
        assert data["agent_message"]["agent_id"] == target
        assert data["user_message"]["sender_type"] == "user"
        assert "marketing perspective" in data["user_message"]["text"]
        # Subsequent /messages POST should be answered by the NEW agent only
        r = s.post(
            f"{API}/conversations/{cid}/messages",
            json={"text": "Give me a one-line headline."},
        )
        assert r.status_code == 200, r.text
        replies = r.json()["agent_messages"]
        assert len(replies) == 1
        assert replies[0]["agent_id"] == target

    def test_switch_unknown_agent_400(self, ctx, direct_conv):
        r = ctx["s"].post(
            f"{API}/conversations/{direct_conv}/switch-agent",
            json={"agent_id": "not-a-real-agent"},
        )
        assert r.status_code == 400

    def test_switch_same_agent_400(self, ctx, direct_conv):
        r = ctx["s"].post(
            f"{API}/conversations/{direct_conv}/switch-agent",
            json={"agent_id": "engineering-frontend-developer"},
        )
        assert r.status_code == 400

    def test_switch_on_group_400(self, ctx):
        s, tid = ctx["s"], ctx["team_id"]
        r = s.post(
            f"{API}/teams/{tid}/conversations",
            json={
                "type": "group",
                "agent_ids": ["engineering-frontend-developer", "design-ui-designer"],
                "title": "TEST_switch_group",
            },
        )
        cid = r.json()["id"]
        try:
            r = s.post(
                f"{API}/conversations/{cid}/switch-agent",
                json={"agent_id": "marketing-content-creator"},
            )
            assert r.status_code == 400
        finally:
            s.delete(f"{API}/conversations/{cid}")

    def test_switch_non_member_403(self, ctx, direct_conv):
        outsider = _sess(); _register(outsider)
        r = outsider.post(
            f"{API}/conversations/{direct_conv}/switch-agent",
            json={"agent_id": "marketing-content-creator"},
        )
        assert r.status_code in (403, 404)


# ── Smart Expand on group conversations ─────────────────────────────────────
class TestSmartExpand:
    def test_smart_expand_true_adds_new_agent_on_topic_shift(self, ctx):
        s, tid = ctx["s"], ctx["team_id"]
        r = s.post(
            f"{API}/teams/{tid}/conversations",
            json={
                "type": "group",
                "agent_ids": ["engineering-frontend-developer"],
                "auto_respond": True,
                "smart_expand": True,
                "title": "TEST_smart_expand_on",
            },
        )
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        try:
            # Off-topic: GDPR / legal — clearly outside engineering-frontend
            r = s.post(
                f"{API}/conversations/{cid}/messages",
                json={
                    "text": (
                        "We need a GDPR-compliant privacy policy and a data-processing "
                        "agreement for EU customers — what should it cover legally?"
                    )
                },
            )
            assert r.status_code == 200, r.text
            data = r.json()
            assert "conversation" in data
            new_ids = data["conversation"]["agent_ids"]
            assert len(new_ids) >= 2, (
                f"expected expansion, agent_ids stayed at {new_ids}"
            )
            assert "engineering-frontend-developer" in new_ids
            added = [a for a in new_ids if a != "engineering-frontend-developer"]
            assert added, "no new agent added"
            new_aid = added[0]
            # The added agent should NOT be in the engineering division (different domain)
            assert not new_aid.startswith("engineering-"), (
                f"expansion picked another engineering agent: {new_aid}"
            )
            # The added agent should also have a reply in this turn
            reply_ids = {m["agent_id"] for m in data["agent_messages"]}
            assert new_aid in reply_ids, f"added agent didn't reply: {reply_ids}"

            # Follow-up in the SAME (now-covered) domain — no further expansion
            r = s.post(
                f"{API}/conversations/{cid}/messages",
                json={"text": "Add a paragraph about cookie consent banners."},
            )
            assert r.status_code == 200, r.text
            data2 = r.json()
            assert set(data2["conversation"]["agent_ids"]) == set(new_ids), (
                f"unexpected further expansion: {data2['conversation']['agent_ids']}"
            )
        finally:
            s.delete(f"{API}/conversations/{cid}")

    def test_smart_expand_false_does_not_add_agent(self, ctx):
        s, tid = ctx["s"], ctx["team_id"]
        r = s.post(
            f"{API}/teams/{tid}/conversations",
            json={
                "type": "group",
                "agent_ids": ["engineering-frontend-developer"],
                "auto_respond": True,
                "smart_expand": False,
                "title": "TEST_smart_expand_off",
            },
        )
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        try:
            r = s.post(
                f"{API}/conversations/{cid}/messages",
                json={
                    "text": (
                        "Draft a GDPR privacy policy and outline what a DPA must cover."
                    )
                },
            )
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["conversation"]["agent_ids"] == ["engineering-frontend-developer"]
        finally:
            s.delete(f"{API}/conversations/{cid}")

    def test_messages_response_has_conversation_field_for_direct(self, ctx):
        s, tid = ctx["s"], ctx["team_id"]
        r = s.post(
            f"{API}/teams/{tid}/conversations",
            json={
                "type": "direct",
                "agent_ids": ["engineering-frontend-developer"],
                "title": "TEST_msg_resp_shape",
            },
        )
        cid = r.json()["id"]
        try:
            r = s.post(
                f"{API}/conversations/{cid}/messages",
                json={"text": "Hello briefly please."},
            )
            assert r.status_code == 200, r.text
            data = r.json()
            assert "conversation" in data
            conv = data["conversation"]
            assert conv["id"] == cid
            assert conv["type"] == "direct"
            assert conv["agent_ids"] == ["engineering-frontend-developer"]
        finally:
            s.delete(f"{API}/conversations/{cid}")
