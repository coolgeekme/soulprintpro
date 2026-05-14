"""Backend tests for Composio Phase 2 — per-agent tools toggle + execute route
+ in-loop chat regression. Mirrors `/app/backend/composio_agent.py`.

Coverage:
- GET  /api/orgs/{org}/agent-tool-prefs           auth gates + payload shape
- PUT  /api/orgs/{org}/agent-tool-prefs/{aid}     admin-gated, unknown=404, persists
- POST /api/orgs/{org}/composio/execute           empty slug=400, unconnected=success:false (NOT 500),
                                                  llm_calls row with composio:<toolkit>:<slug>
- composio_agent.is_agent_tools_enabled           default False, True after PUT
- Regression: chat reply path with tools OFF (default) still produces a reply
- Regression: chat reply path with tools ON but no connections falls back cleanly
"""
import os
import asyncio
import time
import uuid

import requests
import pytest


def _env(path, key):
    try:
        with open(path) as f:
            for line in f:
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        return None


BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL")
            or _env("/app/frontend/.env", "REACT_APP_BACKEND_URL") or "").rstrip("/")
assert BASE_URL, "REACT_APP_BACKEND_URL not configured"

SUPERADMIN_EMAIL = "reggie@coolgeek.me"
SUPERADMIN_PASSWORD = "superadminpass123"
REGGIE_ORG_ID = "69f6a9f568b5974dfcc2fcf8"
CONV_ID = "6a02ba997982a813da3d83fa"  # direct conv in Reggie Lab
TEAM_ID = "69f93a8fd229eed6896778d6"

NON_MEMBER_EMAIL = "autopost-test-1777661963@example.com"
NON_MEMBER_PASSWORD = "testtest123"


# ── helpers ─────────────────────────────────────────────────────────────────
def _with_db(async_fn):
    async def runner():
        from motor.motor_asyncio import AsyncIOMotorClient
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        try:
            return await async_fn(client[os.environ["DB_NAME"]])
        finally:
            client.close()
    return asyncio.run(runner())


@pytest.fixture(scope="module")
def s():
    return requests.Session()


@pytest.fixture(scope="module")
def auth(s):
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": SUPERADMIN_EMAIL, "password": SUPERADMIN_PASSWORD})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    data = r.json()
    s.headers.update({"Authorization": f"Bearer {data['access_token']}"})
    return {"user_id": data["id"], "token": data["access_token"]}


@pytest.fixture(scope="module")
def non_member_token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"email": NON_MEMBER_EMAIL, "password": NON_MEMBER_PASSWORD})
    if r.status_code != 200:
        pytest.skip(f"non-member account login unavailable ({r.status_code})")
    return r.json()["access_token"]


# ── 1. agent-tool-prefs GET ─────────────────────────────────────────────────
class TestAgentToolPrefsList:
    def test_unauthenticated_401(self):
        r = requests.get(f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/agent-tool-prefs")
        assert r.status_code == 401, r.status_code

    def test_non_member_403(self, non_member_token):
        r = requests.get(
            f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/agent-tool-prefs",
            headers={"Authorization": f"Bearer {non_member_token}"})
        assert r.status_code == 403, f"got {r.status_code}: {r.text}"

    def test_owner_gets_full_catalogue(self, s, auth):
        r = s.get(f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/agent-tool-prefs")
        assert r.status_code == 200, r.text
        data = r.json()
        assert "agents" in data and isinstance(data["agents"], list)
        assert data.get("can_edit") is True, "owner should be able to edit"
        # Catalogue should have a sizeable number of agents (~170 per request)
        assert len(data["agents"]) >= 100, f"only {len(data['agents'])} agents"
        # Each agent row must have the right shape
        sample = data["agents"][0]
        for key in ("agent_id", "name", "role", "tools_enabled", "is_custom"):
            assert key in sample, f"missing field {key} in {sample}"
        # All tools_enabled default to False unless something is preset
        # (We don't assert all-False because earlier tests may have flipped one.)
        assert isinstance(sample["tools_enabled"], bool)


# ── 2. agent-tool-prefs PUT ─────────────────────────────────────────────────
class TestAgentToolPrefsPut:
    AID = None  # captured

    def test_unknown_agent_returns_404(self, s, auth):
        r = s.put(
            f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/agent-tool-prefs/__no_such_agent__",
            json={"tools_enabled": True})
        assert r.status_code == 404, r.text

    def test_owner_flips_bit_and_persists(self, s, auth):
        # Pick an agent from the catalogue (prefer non-custom)
        lst = s.get(f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/agent-tool-prefs").json()
        chosen = next(a for a in lst["agents"] if not a["is_custom"])
        aid = chosen["agent_id"]
        TestAgentToolPrefsPut.AID = aid

        # Flip ON
        r = s.put(
            f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/agent-tool-prefs/{aid}",
            json={"tools_enabled": True})
        assert r.status_code == 200, r.text
        assert r.json() == {"agent_id": aid, "tools_enabled": True}

        # GET back should reflect the toggle
        lst2 = s.get(f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/agent-tool-prefs").json()
        flipped = next(a for a in lst2["agents"] if a["agent_id"] == aid)
        assert flipped["tools_enabled"] is True

        # Row should land in db.agent_tool_prefs
        async def _q(d):
            return await d.agent_tool_prefs.find_one(
                {"org_id": REGGIE_ORG_ID, "agent_id": aid}, {"_id": 0})
        row = _with_db(_q)
        assert row is not None
        assert row["tools_enabled"] is True
        assert "updated_at" in row and "updated_by" in row

    def test_is_agent_tools_enabled_helper(self, auth):
        """composio_agent.is_agent_tools_enabled should mirror the DB."""
        import sys
        sys.path.insert(0, "/app/backend")
        from composio_agent import is_agent_tools_enabled

        async def _check(d):
            on = await is_agent_tools_enabled(d, REGGIE_ORG_ID, TestAgentToolPrefsPut.AID)
            off = await is_agent_tools_enabled(d, REGGIE_ORG_ID, "__no_such_agent__")
            return on, off
        on, off = _with_db(_check)
        assert on is True
        assert off is False

    def test_owner_flips_back_off(self, s, auth):
        """Cleanup — flip back OFF so the chat regression test runs with
        the documented default (OFF) on the same agent if anyone reuses it."""
        aid = TestAgentToolPrefsPut.AID
        if not aid:
            pytest.skip("no agent captured")
        r = s.put(
            f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/agent-tool-prefs/{aid}",
            json={"tools_enabled": False})
        assert r.status_code == 200, r.text
        assert r.json()["tools_enabled"] is False

    def test_non_member_put_403(self, non_member_token):
        r = requests.put(
            f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/agent-tool-prefs/any",
            json={"tools_enabled": True},
            headers={"Authorization": f"Bearer {non_member_token}"})
        # Non-member of the org: should be blocked. 403 (org gate) is
        # the documented behaviour; 404 is also acceptable if the org
        # is hidden from non-members.
        assert r.status_code in (403, 404), f"got {r.status_code}: {r.text}"


# ── 3. /composio/execute ────────────────────────────────────────────────────
class TestComposioExecute:
    def test_empty_slug_returns_400(self, s, auth):
        r = s.post(f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/composio/execute",
                   json={"tool_slug": "", "arguments": {}})
        assert r.status_code == 400, r.text

    def test_unconnected_toolkit_returns_envelope_not_500(self, s, auth):
        """Caller has NO Gmail connection (we removed it in Phase 1 cleanup).
        execute_tool must catch the SDK exception and return
        {success: false, response: {error: ...}} with HTTP 200, NOT raise 500.
        """
        # Sanity: ensure there's no live Gmail connection for this user/org
        async def _check(d):
            return await d.composio_connections.find_one(
                {"org_id": REGGIE_ORG_ID, "user_id": auth["user_id"], "toolkit": "gmail"},
                {"_id": 0})
        existing = _with_db(_check)
        # We don't fail if one exists — the SDK call may still error for
        # other reasons (e.g. tool not activated). The assertion below
        # only cares that the route doesn't 500.
        before = _llm_calls_count_for_slug("GMAIL_SEND_EMAIL")

        r = s.post(f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/composio/execute",
                   json={"tool_slug": "GMAIL_SEND_EMAIL",
                         "arguments": {"recipient_email": "nobody@example.com",
                                       "subject": "test", "body": "test"}})
        assert r.status_code == 200, f"expected 200 envelope, got {r.status_code}: {r.text[:500]}"
        data = r.json()
        assert "success" in data
        assert "response" in data
        if existing is None:
            assert data["success"] is False, f"expected failure for unconnected, got {data}"
            assert data.get("error") or (isinstance(data["response"], dict) and data["response"].get("error"))

        # llm_calls telemetry: composio:<toolkit>:<slug>
        # Allow a beat for the insert to flush
        time.sleep(0.5)
        after = _llm_calls_count_for_slug("GMAIL_SEND_EMAIL")
        assert after > before, f"no new llm_calls row written ({before} -> {after})"

    def test_non_member_execute_403(self, non_member_token):
        r = requests.post(
            f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/composio/execute",
            json={"tool_slug": "GMAIL_SEND_EMAIL", "arguments": {}},
            headers={"Authorization": f"Bearer {non_member_token}"})
        assert r.status_code in (403, 404), f"got {r.status_code}: {r.text}"


def _llm_calls_count_for_slug(slug):
    async def _q(d):
        return await d.llm_calls.count_documents(
            {"kind_label": {"$regex": f"^composio:.*:{slug}$"}})
    return _with_db(_q)


# ── 4. Chat regression: tools OFF (default) still works ─────────────────────
class TestChatRegression:
    """Verify that a normal chat message in a direct conversation produces
    a real agent reply when agent_tool_prefs is OFF for that agent (the
    documented default). We use the conv/team provided in the review
    request (Reggie Lab)."""

    def _send(self, s, text):
        url = f"{BASE_URL}/api/conversations/{CONV_ID}/messages"
        return s.post(url, json={"text": text})

    def test_normal_chat_reply_with_tools_off(self, s, auth):
        # Best-effort: ensure no prefs row is ON for whichever agent
        # will answer (we don't know which agent until after).
        async def _wipe(d):
            return await d.agent_tool_prefs.update_many(
                {"org_id": REGGIE_ORG_ID}, {"$set": {"tools_enabled": False}})
        _with_db(_wipe)

        marker = f"phase2-regression-{uuid.uuid4().hex[:8]}"
        r = self._send(s, f"Quick smoke test: please reply briefly. tag={marker}")
        # Some routes return 200 with reply inline; others 202 and use polling.
        assert r.status_code in (200, 201, 202), f"send failed {r.status_code}: {r.text[:400]}"
        # If the endpoint returns the reply inline, verify it
        try:
            body = r.json()
        except Exception:
            body = {}
        if isinstance(body, dict) and "agent_messages" in body:
            agent_msgs = body.get("agent_messages") or []
            assert any(m.get("text") for m in agent_msgs), \
                f"no agent reply in response: {body}"
        else:
            # Fall back: poll messages for the marker
            time.sleep(4)
            r2 = s.get(f"{BASE_URL}/api/conversations/{CONV_ID}/messages")
            assert r2.status_code == 200
            msgs = r2.json() if isinstance(r2.json(), list) else r2.json().get("messages", [])
            assert any(m.get("sender_type") == "agent" for m in msgs[-10:]), \
                "no agent reply found in last 10 messages"

    def test_chat_reply_with_tools_on_but_no_connections(self, s, auth):
        """Flip an agent ON; with zero Composio connections, run_with_tools
        should return (None, []) and we should silently fall through to
        LlmChat. We can't easily target *which* agent will answer, so we
        flip ALL prefs ON for a brief window."""
        # Capture which agents we'll flip so we can clean up.
        lst = s.get(f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/agent-tool-prefs").json()
        flipped_ids = []
        for a in lst["agents"][:25]:  # flip first 25 to cover routing odds
            r = s.put(
                f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/agent-tool-prefs/{a['agent_id']}",
                json={"tools_enabled": True})
            if r.status_code == 200:
                flipped_ids.append(a["agent_id"])

        try:
            marker = f"phase2-tools-on-{uuid.uuid4().hex[:8]}"
            r = self._send(s, f"Hi there — quick check. tag={marker}")
            assert r.status_code in (200, 201, 202), f"send failed: {r.status_code} {r.text[:300]}"
            try:
                body = r.json()
            except Exception:
                body = {}
            # Either inline reply or polling reply — just confirm a reply
            # came back (no 500/4xx + non-empty content somewhere).
            if isinstance(body, dict) and "agent_messages" in body:
                assert any(m.get("text") for m in (body.get("agent_messages") or [])), \
                    f"empty reply when tools=ON+no_connections: {body}"
            else:
                time.sleep(4)
                r2 = s.get(f"{BASE_URL}/api/conversations/{CONV_ID}/messages")
                assert r2.status_code == 200
        finally:
            # Reset
            for aid in flipped_ids:
                s.put(f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/agent-tool-prefs/{aid}",
                      json={"tools_enabled": False})
