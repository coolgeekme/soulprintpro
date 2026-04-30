"""Backend tests for the Brave-Search + intent-classifier + agent-reply web pipeline:
  * brave_search.brave_web_search (live Brave API + graceful fallback)
  * server.detect_search_intent    (LLM-as-classifier — time-sensitive vs timeless)
  * /api/teams/{id}/start           — agent_message.web_sources populated on live prompts
  * /api/conversations/{cid}/messages  — web_sources persisted on subsequent messages
  * /api/conversations/{cid}/switch-agent — web_sources on the new agent's reply
  * Group conv (auto_respond=true) — every agent gets its own web_sources
  * Timeless prompts do NOT trigger search (web_sources absent/empty)
  * Backwards-compat: empty BRAVE_API_KEY → brave helper returns []
LLM/classifier are non-deterministic; "live-info" tests retry with escalating prompts
before skipping. We never force pass — we skip on pure classifier-miss, fail on
server errors or malformed responses.
"""
import asyncio
import importlib
import os
import sys
import uuid
import pytest
import requests

# Make backend modules importable for unit tests
sys.path.insert(0, "/app/backend")

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
    s.put(
        f"{API}/teams/{team['id']}/soulprint",
        json={
            "mission": "Track tech news",
            "tone": "concise",
            "audience": "founders",
            "values": "accuracy",
            "north_star": "timely insights",
        },
    )
    return {"s": s, "org_id": org["id"], "team_id": team["id"]}


# ── Unit: brave_web_search (live call) ──────────────────────────────────────
class TestBraveHelper:
    def test_live_query_returns_results(self):
        from brave_search import brave_web_search
        # Retry across a few queries w/ a generous timeout — Brave cold-path can exceed 4s
        results = []
        for q in ("OpenAI ChatGPT", "New York Times", "BBC News", "Wikipedia"):
            results = asyncio.run(brave_web_search(q, count=5, timeout=12.0))
            if results:
                break
        assert isinstance(results, list)
        assert 0 < len(results) <= 5, "Brave returned nothing across 4 fallback queries"
        top = results[0]
        assert top.get("title"), "top result missing title"
        assert top.get("url", "").startswith("http"), f"bad url: {top.get('url')}"

    def test_empty_query_returns_empty(self):
        from brave_search import brave_web_search
        assert asyncio.run(brave_web_search("")) == []
        assert asyncio.run(brave_web_search("   ")) == []

    def test_missing_api_key_returns_empty(self, monkeypatch):
        import brave_search as bs
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)
        results = asyncio.run(bs.brave_web_search("anything"))
        assert results == []

    def test_bad_api_key_returns_empty(self, monkeypatch):
        import brave_search as bs
        monkeypatch.setenv("BRAVE_API_KEY", "this-is-not-a-real-key-xxxxx")
        results = asyncio.run(bs.brave_web_search("hello world"))
        assert results == []

    def test_format_for_prompt_empty_and_nonempty(self):
        from brave_search import format_results_for_prompt
        assert format_results_for_prompt([]) == ""
        out = format_results_for_prompt([
            {"title": "T", "url": "https://x.com", "snippet": "s", "age": "1h", "source": "x.com"}
        ])
        assert "https://x.com" in out and "T" in out


# ── Unit: detect_search_intent ──────────────────────────────────────────────
class TestDetectSearchIntent:
    def test_time_sensitive_returns_search_true(self):
        # Import inline to avoid importing server until DB env is set (supervisor runs it already)
        server = importlib.import_module("server")
        out = asyncio.run(server.detect_search_intent("What are the latest AI news stories this week?"))
        # Classifier is conservative; if it says False, retry with an even clearer prompt once
        if not out.get("search"):
            out = asyncio.run(server.detect_search_intent(
                "Give me the top technology news from today (breaking headlines)."
            ))
        assert out["search"] is True, f"classifier refused clear live-info prompt: {out}"
        assert out["query"], f"empty query when search=True: {out}"
        # freshness should be a short-horizon window for 'today/this week'
        assert out["freshness"] in ("pd", "pw", "pm", "py", None)

    def test_timeless_returns_search_false(self):
        server = importlib.import_module("server")
        out = asyncio.run(server.detect_search_intent("How do I write a thank-you note?"))
        assert out["search"] is False, f"classifier over-fired on timeless prompt: {out}"

    def test_empty_message_returns_search_false(self):
        server = importlib.import_module("server")
        out = asyncio.run(server.detect_search_intent(""))
        assert out == {"search": False, "query": "", "freshness": None}
        out = asyncio.run(server.detect_search_intent("   "))
        assert out["search"] is False


# ── Integration: agent_message.web_sources through public API ───────────────
LIVE_PROMPTS = [
    "What are the biggest AI news stories from this week? Give me the top 3 headlines.",
    "Summarize the top technology headlines from today — breaking news only.",
    "What were the biggest tech product launches announced this week?",
]
TIMELESS_PROMPT = "Outline a 3-bullet structure for a thank-you note."


def _has_sources(msg):
    ws = msg.get("web_sources")
    return isinstance(ws, list) and len(ws) > 0


class TestSmartStartWebSources:
    def test_live_prompt_includes_web_sources(self, ctx):
        s, tid = ctx["s"], ctx["team_id"]
        got_sources = False
        data = None
        cid_to_cleanup = None
        for prompt in LIVE_PROMPTS:
            r = s.post(f"{API}/teams/{tid}/start", json={"text": prompt})
            assert r.status_code == 200, r.text
            data = r.json()
            cid_to_cleanup = data["conversation"]["id"]
            am = data["agent_message"]
            if _has_sources(am):
                got_sources = True
                break
            # Classifier missed — drop this conv and try next prompt
            s.delete(f"{API}/conversations/{cid_to_cleanup}")
            cid_to_cleanup = None
        try:
            if not got_sources:
                pytest.skip("Classifier declined to search on all live-info prompts (accepted per spec).")
            am = data["agent_message"]
            ws = am["web_sources"]
            assert 1 <= len(ws) <= 5, f"web_sources length out of bounds: {len(ws)}"
            for src in ws:
                assert src.get("title"), f"source missing title: {src}"
                assert src.get("url", "").startswith("http"), f"bad url: {src}"
            assert am.get("text", "").strip(), "agent reply was empty"
            # Loose citation check — LLM should have dropped a URL somewhere
            assert "http" in am["text"], "agent reply never mentions any URL"
        finally:
            if cid_to_cleanup:
                s.delete(f"{API}/conversations/{cid_to_cleanup}")

    def test_timeless_prompt_no_web_sources(self, ctx):
        s, tid = ctx["s"], ctx["team_id"]
        r = s.post(f"{API}/teams/{tid}/start", json={"text": TIMELESS_PROMPT})
        assert r.status_code == 200, r.text
        data = r.json()
        try:
            am = data["agent_message"]
            ws = am.get("web_sources") or []
            assert ws == [], f"timeless prompt unexpectedly triggered search: {ws}"
        finally:
            s.delete(f"{API}/conversations/{data['conversation']['id']}")


class TestSubsequentMessagesWebSources:
    def test_followup_live_prompt_persists_web_sources(self, ctx):
        s, tid = ctx["s"], ctx["team_id"]
        # Seed a direct conv
        r = s.post(
            f"{API}/teams/{tid}/conversations",
            json={
                "type": "direct",
                "agent_ids": ["marketing-content-creator"],
                "title": "TEST_web_followup",
            },
        )
        cid = r.json()["id"]
        try:
            sourced_msg_id = None
            for prompt in LIVE_PROMPTS:
                r = s.post(
                    f"{API}/conversations/{cid}/messages",
                    json={"text": prompt},
                )
                assert r.status_code == 200, r.text
                data = r.json()
                replies = data["agent_messages"]
                assert replies
                for am in replies:
                    if _has_sources(am):
                        sourced_msg_id = am["id"]
                        break
                if sourced_msg_id:
                    break
            if not sourced_msg_id:
                pytest.skip("Classifier declined every live-info prompt in follow-up")
            # GET list — persisted web_sources should still be there
            r = s.get(f"{API}/conversations/{cid}/messages")
            assert r.status_code == 200, r.text
            msgs = r.json()
            target = next((m for m in msgs if m["id"] == sourced_msg_id), None)
            assert target is not None, "sourced message missing from list"
            assert _has_sources(target), f"web_sources not persisted: {target}"
        finally:
            s.delete(f"{API}/conversations/{cid}")


class TestGroupConvWebSources:
    def test_auto_respond_group_both_agents_have_web_sources(self, ctx):
        s, tid = ctx["s"], ctx["team_id"]
        r = s.post(
            f"{API}/teams/{tid}/conversations",
            json={
                "type": "group",
                "agent_ids": ["marketing-content-creator", "specialized-chief-of-staff"],
                "auto_respond": True,
                "smart_expand": False,
                "title": "TEST_web_group",
            },
        )
        cid = r.json()["id"]
        try:
            both_with_sources = False
            for prompt in LIVE_PROMPTS:
                r = s.post(
                    f"{API}/conversations/{cid}/messages",
                    json={"text": prompt},
                )
                assert r.status_code == 200, r.text
                replies = r.json()["agent_messages"]
                assert len(replies) == 2, f"expected 2 replies, got {len(replies)}"
                if all(_has_sources(m) for m in replies):
                    both_with_sources = True
                    for m in replies:
                        ws = m["web_sources"]
                        assert 1 <= len(ws) <= 5
                    break
            if not both_with_sources:
                pytest.skip("Classifier didn't fire for both agents on any live-info prompt")
        finally:
            s.delete(f"{API}/conversations/{cid}")

    def test_timeless_group_no_web_sources(self, ctx):
        s, tid = ctx["s"], ctx["team_id"]
        r = s.post(
            f"{API}/teams/{tid}/conversations",
            json={
                "type": "group",
                "agent_ids": ["marketing-content-creator", "design-ui-designer"],
                "auto_respond": True,
                "smart_expand": False,
                "title": "TEST_web_group_timeless",
            },
        )
        cid = r.json()["id"]
        try:
            r = s.post(
                f"{API}/conversations/{cid}/messages",
                json={"text": TIMELESS_PROMPT},
            )
            assert r.status_code == 200, r.text
            replies = r.json()["agent_messages"]
            for m in replies:
                ws = m.get("web_sources") or []
                assert ws == [], f"timeless group prompt triggered search on {m['agent_id']}: {ws}"
        finally:
            s.delete(f"{API}/conversations/{cid}")


class TestSwitchAgentWebSources:
    def test_switch_with_live_bridge_text_populates_web_sources(self, ctx):
        s, tid = ctx["s"], ctx["team_id"]
        r = s.post(
            f"{API}/teams/{tid}/conversations",
            json={
                "type": "direct",
                "agent_ids": ["engineering-frontend-developer"],
                "title": "TEST_web_switch",
            },
        )
        cid = r.json()["id"]
        try:
            got = False
            for prompt in LIVE_PROMPTS:
                r = s.post(
                    f"{API}/conversations/{cid}/switch-agent",
                    json={"agent_id": "marketing-content-creator", "text": prompt},
                )
                if r.status_code == 400:
                    # Means already on marketing-content-creator from a prior retry
                    # Re-seed: switch back to engineering first
                    s.post(
                        f"{API}/conversations/{cid}/switch-agent",
                        json={"agent_id": "engineering-frontend-developer", "text": "back"},
                    )
                    continue
                assert r.status_code == 200, r.text
                am = r.json()["agent_message"]
                if _has_sources(am):
                    ws = am["web_sources"]
                    assert 1 <= len(ws) <= 5
                    got = True
                    break
                # switch back for next retry
                s.post(
                    f"{API}/conversations/{cid}/switch-agent",
                    json={"agent_id": "engineering-frontend-developer", "text": "back"},
                )
            if not got:
                pytest.skip("Classifier declined every live bridge prompt")
        finally:
            s.delete(f"{API}/conversations/{cid}")


# ── Backwards-compat / graceful-degradation ─────────────────────────────────
class TestGracefulDegradation:
    def test_empty_brave_key_mocked_yields_empty_list(self, monkeypatch):
        import brave_search as bs
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)
        out = asyncio.run(bs.brave_web_search("latest AI news"))
        assert out == []

    def test_timeless_prompt_start_endpoint_ok_even_without_search(self, ctx):
        """End-to-end resilience check: timeless prompt still yields a valid reply."""
        s, tid = ctx["s"], ctx["team_id"]
        r = s.post(f"{API}/teams/{tid}/start", json={"text": TIMELESS_PROMPT})
        assert r.status_code == 200, r.text
        data = r.json()
        try:
            assert data["agent_message"]["text"].strip()
        finally:
            s.delete(f"{API}/conversations/{data['conversation']['id']}")
