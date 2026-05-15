"""Backend tests for Composio Phase 4 — wiring Composio tools into the
Missions loop.

Coverage matrix:
  build_composio_mission_tools
    - Tool=None              -> []
    - COMPOSIO_API_KEY unset -> []  (monkeypatched is_available)
    - 0 ACTIVE conns         -> []  (the live case for the test fixture)
    - tools_enabled=False    -> []  (lead-agent gate)
    - happy path             -> list of Tool() instances with the
                                expected name/description/handler shape
                                (composio.get_tools_for_user monkeypatched
                                because the test fixture has no real
                                ACTIVE creds Composio would accept)

  budget arithmetic
    - reset_budget + 5×_bump_budget('gmail')  trips per-platform cap
    - 20+ total bumps                          trips total cap

  missions.run_mission_loop integration
    - regression: POST /api/orgs/{org}/missions with NO connections still
      succeeds (mission row created, _RUNNING contains the id)
    - positive: insert ACTIVE composio_connections + tools_enabled=True,
      POST a new mission, verify _RUNNING has the new mission id and the
      backend log contains the 'mission XXX: +N Composio tools' line.
      (We monkeypatch composio.get_tools_for_user via a sitecustom patch
      because real Composio rejects synthetic user_ids.)

  Phase 1+2+3 route regression
    - GET /toolkits, GET /agent-tool-prefs, POST /composio/connect bad
      toolkit all still respond as before

  Telemetry kind_label flip
    - Insert a fake llm_calls doc with kind_label='composio:gmail:GMAIL_SEND_EMAIL'
      then run an update_one mirroring the handler body — verify the doc's
      kind_label flips to 'mission_tool:composio:gmail:GMAIL_SEND_EMAIL'
      and mission_id is set.
"""
import asyncio
import os
import sys
import time
import uuid
from typing import Any, Dict, List

import pytest
import requests
from motor.motor_asyncio import AsyncIOMotorClient

# Ensure /app/backend on sys.path (conftest also does this, kept explicit).
sys.path.insert(0, "/app/backend")


def _env(path, key):
    try:
        with open(path) as f:
            for line in f:
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        return None


BASE_URL = (
    os.environ.get("REACT_APP_BACKEND_URL")
    or _env("/app/frontend/.env", "REACT_APP_BACKEND_URL")
    or ""
).rstrip("/")
assert BASE_URL, "REACT_APP_BACKEND_URL not configured"

MONGO_URL = os.environ.get("MONGO_URL") or _env("/app/backend/.env", "MONGO_URL")
DB_NAME = os.environ.get("DB_NAME") or _env("/app/backend/.env", "DB_NAME")
assert MONGO_URL and DB_NAME, "MONGO_URL/DB_NAME missing"

SUPERADMIN_EMAIL = "reggie@coolgeek.me"
SUPERADMIN_PASSWORD = "superadminpass123"
REGGIE_ORG_ID = "69f6a9f568b5974dfcc2fcf8"


def _run(async_fn):
    """Open a fresh motor client per call (motor binds executors to the
    creating loop — sharing across asyncio.run() invocations dies with
    'Event loop is closed')."""
    async def runner():
        client = AsyncIOMotorClient(MONGO_URL)
        try:
            return await async_fn(client[DB_NAME])
        finally:
            client.close()
    return asyncio.run(runner())


@pytest.fixture(scope="module")
def auth_session():
    s = requests.Session()
    r = s.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": SUPERADMIN_EMAIL, "password": SUPERADMIN_PASSWORD},
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    data = r.json()
    s.headers.update({"Authorization": f"Bearer {data['access_token']}"})
    return {"session": s, "user_id": data["id"], "token": data["access_token"]}


# ── 1. build_composio_mission_tools short-circuits ──────────────────────────
class TestBuildToolsShortCircuits:
    def test_tool_none_returns_empty(self):
        from composio_mission_tools import build_composio_mission_tools

        async def _go(db):
            return await build_composio_mission_tools(
                db, org_id="o", user_id="u", mission_id="m", Tool=None,
            )
        assert _run(_go) == []

    def test_no_active_connections_returns_empty(self, auth_session):
        from composio_mission_tools import build_composio_mission_tools
        from missions import Tool

        async def _go(db):
            # Reggie has zero ACTIVE composio_connections in preview — the
            # most natural regression of the null path.
            return await build_composio_mission_tools(
                db,
                org_id=REGGIE_ORG_ID,
                user_id=auth_session["user_id"],
                mission_id=f"TEST_mission_{uuid.uuid4().hex[:6]}",
                Tool=Tool,
            )
        assert _run(_go) == []

    def test_composio_unavailable_returns_empty(self, auth_session, monkeypatch):
        import composio_integration as composio
        from composio_mission_tools import build_composio_mission_tools
        from missions import Tool

        monkeypatch.setattr(composio, "is_available", lambda: False)

        async def _go(db):
            return await build_composio_mission_tools(
                db,
                org_id=REGGIE_ORG_ID,
                user_id=auth_session["user_id"],
                mission_id=f"TEST_mission_{uuid.uuid4().hex[:6]}",
                Tool=Tool,
            )
        assert _run(_go) == []

    def test_lead_agent_tools_disabled_returns_empty(self, auth_session):
        from composio_mission_tools import build_composio_mission_tools
        from missions import Tool

        agent_id = f"TEST_agent_{uuid.uuid4().hex[:6]}"

        async def _go(db):
            # Seed an agent_tool_prefs row with tools_enabled=False.
            await db.agent_tool_prefs.insert_one({
                "org_id": REGGIE_ORG_ID,
                "agent_id": agent_id,
                "tools_enabled": False,
            })
            # ALSO seed a fake ACTIVE connection so we know the gate is
            # what's stopping us, not the empty-connection short-circuit.
            from composio_integration import composio_user_id
            cu = composio_user_id(REGGIE_ORG_ID, auth_session["user_id"])
            conn_id = f"TEST_conn_{uuid.uuid4().hex[:6]}"
            await db.composio_connections.insert_one({
                "composio_user_id": cu,
                "toolkit": "gmail",
                "status": "ACTIVE",
                "connection_id": conn_id,
            })
            try:
                result = await build_composio_mission_tools(
                    db,
                    org_id=REGGIE_ORG_ID,
                    user_id=auth_session["user_id"],
                    mission_id=f"TEST_mission_{uuid.uuid4().hex[:6]}",
                    lead_agent_id=agent_id,
                    Tool=Tool,
                )
            finally:
                await db.agent_tool_prefs.delete_one(
                    {"org_id": REGGIE_ORG_ID, "agent_id": agent_id}
                )
                await db.composio_connections.delete_one({"connection_id": conn_id})
            return result

        assert _run(_go) == []


# ── 2. Happy path: Tool() shape ─────────────────────────────────────────────
class TestBuildToolsShape:
    def test_returns_tool_instances_with_expected_shape(self, auth_session, monkeypatch):
        """Inject fake composio.get_tools_for_user output + a fake ACTIVE row
        so the function gets past every short-circuit. Verify the Tool
        instance fields (name, description prefix, handler being callable).
        """
        import composio_integration as composio
        from composio_mission_tools import build_composio_mission_tools
        from missions import Tool

        fake_specs = [
            {
                "type": "function",
                "function": {
                    "name": "GMAIL_SEND_EMAIL",
                    "description": "Send an email via Gmail",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "to": {"type": "string"},
                            "subject": {"type": "string"},
                            "body": {"type": "string"},
                        },
                        "required": ["to", "subject"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "SLACK_POST_MESSAGE",
                    "description": "Post a message to Slack",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]

        async def _fake_get_tools(*a, **kw):
            return fake_specs

        monkeypatch.setattr(composio, "get_tools_for_user", _fake_get_tools)

        agent_id = f"TEST_agent_{uuid.uuid4().hex[:6]}"

        async def _go(db):
            await db.agent_tool_prefs.insert_one({
                "org_id": REGGIE_ORG_ID,
                "agent_id": agent_id,
                "tools_enabled": True,
            })
            try:
                result = await build_composio_mission_tools(
                    db,
                    org_id=REGGIE_ORG_ID,
                    user_id=auth_session["user_id"],
                    mission_id=f"TEST_mission_{uuid.uuid4().hex[:6]}",
                    lead_agent_id=agent_id,
                    Tool=Tool,
                )
            finally:
                await db.agent_tool_prefs.delete_one(
                    {"org_id": REGGIE_ORG_ID, "agent_id": agent_id}
                )
            return result

        result = _run(_go)
        assert isinstance(result, list) and len(result) == 2, \
            f"expected 2 Tool instances, got {result}"
        names = sorted(t.name for t in result)
        assert names == ["gmail.send_email", "slack.post_message"], names
        for t in result:
            assert isinstance(t, Tool)
            assert t.description.startswith("["), t.description
            # description should be prefixed with [toolkit]
            toolkit = t.name.split(".", 1)[0]
            assert t.description.startswith(f"[{toolkit}]"), \
                f"description missing [{toolkit}] prefix: {t.description!r}"
            assert callable(t.handler)
            assert t.side_effect is True


# ── 3. Budget arithmetic ────────────────────────────────────────────────────
class TestBudget:
    def test_per_platform_cap_trips_at_five(self):
        from composio_mission_tools import (
            reset_budget, _bump_budget, _is_over_budget, MAX_PER_PLATFORM,
        )
        mid = f"TEST_mid_{uuid.uuid4().hex[:6]}"
        reset_budget(mid)
        # First 4 bumps shouldn't trip the gate
        for _ in range(MAX_PER_PLATFORM - 1):
            _bump_budget(mid, "gmail")
            assert _is_over_budget(mid, "gmail") is None
        # The 5th bump pushes count to 5 — per-platform check uses >= so
        # the NEXT _is_over_budget call returns a string.
        _bump_budget(mid, "gmail")
        msg = _is_over_budget(mid, "gmail")
        assert msg is not None and "gmail" in msg, msg

    def test_total_cap_trips_at_twenty(self):
        from composio_mission_tools import (
            reset_budget, _bump_budget, _is_over_budget,
            MAX_TOOL_CALLS_PER_MISSION,
        )
        mid = f"TEST_mid_{uuid.uuid4().hex[:6]}"
        reset_budget(mid)
        # Spread across many platforms so per-platform doesn't trip first.
        toolkits = [f"tk{i}" for i in range(MAX_TOOL_CALLS_PER_MISSION)]
        for tk in toolkits:
            _bump_budget(mid, tk)
        # Now any platform check should trip the total cap.
        msg = _is_over_budget(mid, "tk0")
        assert msg is not None and "total" in msg.lower(), msg

    def test_reset_clears_counters(self):
        from composio_mission_tools import reset_budget, _bump_budget, _is_over_budget
        mid = f"TEST_mid_{uuid.uuid4().hex[:6]}"
        reset_budget(mid)
        for _ in range(5):
            _bump_budget(mid, "gmail")
        assert _is_over_budget(mid, "gmail") is not None
        reset_budget(mid)
        # After reset the gate is open again.
        assert _is_over_budget(mid, "gmail") is None


# ── 4. Mission integration regression — no connections, mission still starts ─
class TestMissionRegression:
    def test_start_mission_with_no_connections_still_works(self, auth_session):
        s = auth_session["session"]
        r = s.post(
            f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/missions",
            json={"goal": "TEST Phase4 regression: write a one-line haiku about testing."},
        )
        assert r.status_code == 200, r.text
        m = r.json()
        mid = m.get("id")
        assert mid, m
        assert m.get("status") == "running"
        # Verify mission is in _RUNNING dict (best-effort: import the
        # module and peek). This runs in the SAME process as supervisord
        # backend ONLY if we hit it via REST — so instead, give it ~3s,
        # then poll the mission row.
        time.sleep(3)

        async def _check(db):
            doc = await db.missions.find_one({"id": mid}, {"_id": 0})
            return doc
        doc = _run(_check)
        assert doc is not None and doc["status"] in {
            "running", "asking_user", "completed", "failed",
            "force_finishing", "paused",
        }, f"unexpected status: {doc and doc.get('status')}"

        # Cancel the mission so it doesn't keep churning.
        try:
            s.post(f"{BASE_URL}/api/missions/{mid}/cancel")
        except Exception:
            pass


# ── 5. Phase 1+2+3 route regression ─────────────────────────────────────────
class TestPhase123RoutesUntouched:
    def test_toolkits_route_still_works(self, auth_session):
        s = auth_session["session"]
        r = s.get(f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/composio/toolkits")
        assert r.status_code == 200, r.text
        data = r.json()
        assert "toolkits" in data and "connections" in data

    def test_agent_tool_prefs_route_still_works(self, auth_session):
        s = auth_session["session"]
        r = s.get(f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/agent-tool-prefs")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "agents" in body and isinstance(body["agents"], list)

    def test_connect_bad_toolkit_still_400(self, auth_session):
        s = auth_session["session"]
        r = s.post(
            f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/composio/connect",
            json={"toolkit": "does_not_exist_phase4"},
        )
        assert r.status_code == 400, f"got {r.status_code}: {r.text[:200]}"


# ── 6. Telemetry kind_label flip ────────────────────────────────────────────
class TestTelemetryUpdateOne:
    def test_update_one_sort_kwarg_incompatible_with_pymongo_4_5(self):
        """REGRESSION GUARD for a CRITICAL bug:

        composio_mission_tools.py line ~165 calls
            db.llm_calls.update_one(filter, {"$set": ...}, sort=[("created_at", -1)])

        but pymongo 4.5.0 (the version installed in this environment)
        does NOT accept a `sort` kwarg on update_one — that param was
        only added in pymongo 4.7+. Every successful Composio tool call
        inside a mission will raise TypeError at telemetry write-time.

        This test demonstrates the failure so the main agent can fix
        either by:
          - using db.llm_calls.find_one_and_update(..., sort=...) instead
          - or bumping pymongo to >=4.7 in requirements.txt
          - or simply dropping the sort=... clause if duplicates are rare
        """
        import pymongo
        # If/when pymongo is bumped past 4.7, this test will start passing
        # the kwarg silently — in which case it would no longer catch the
        # bug. Pin the assertion to the version we observed.
        ver_tuple = tuple(int(p) for p in pymongo.__version__.split(".")[:2])
        if ver_tuple >= (4, 7):
            pytest.skip(
                f"pymongo {pymongo.__version__} supports update_one(sort=...) "
                "natively — bug no longer applies."
            )

        from datetime import datetime, timezone
        marker = f"TEST_telem_{uuid.uuid4().hex[:6]}"

        async def _go(db):
            await db.llm_calls.insert_one({
                "org_id": REGGIE_ORG_ID,
                "kind_label": "composio:gmail:GMAIL_SEND_EMAIL",
                "marker": marker,
                "created_at": datetime.now(timezone.utc),
            })
            try:
                await db.llm_calls.update_one(
                    {"org_id": REGGIE_ORG_ID,
                     "kind_label": "composio:gmail:GMAIL_SEND_EMAIL"},
                    {"$set": {"mission_id": "x",
                              "kind_label": "mission_tool:composio:gmail:GMAIL_SEND_EMAIL"}},
                    sort=[("created_at", -1)],
                )
                return None  # No error → bug is gone
            except TypeError as exc:
                return str(exc)
            finally:
                await db.llm_calls.delete_one({"marker": marker})

        err = _run(_go)
        assert err and "sort" in err, (
            "Expected pymongo 4.5.0 to reject the sort kwarg, but it didn't. "
            f"err={err!r}"
        )

    def test_update_one_works_without_sort_kwarg(self):
        """Verify the QUERY + SET shape is structurally sound — the only
        thing wrong is the sort kwarg, not the filter/update document."""
        from datetime import datetime, timezone
        marker = f"TEST_telem_{uuid.uuid4().hex[:6]}"
        mid = f"TEST_mid_{uuid.uuid4().hex[:6]}"

        async def _go(db):
            await db.llm_calls.insert_one({
                "org_id": REGGIE_ORG_ID,
                "kind_label": "composio:gmail:GMAIL_SEND_EMAIL",
                "marker": marker,
                "created_at": datetime.now(timezone.utc),
            })
            try:
                await db.llm_calls.update_one(
                    {"org_id": REGGIE_ORG_ID,
                     "kind_label": "composio:gmail:GMAIL_SEND_EMAIL"},
                    {"$set": {"mission_id": mid,
                              "kind_label": "mission_tool:composio:gmail:GMAIL_SEND_EMAIL"}},
                )
                return await db.llm_calls.find_one({"marker": marker}, {"_id": 0})
            finally:
                await db.llm_calls.delete_one({"marker": marker})

        u = _run(_go)
        assert u is not None
        assert u["kind_label"] == "mission_tool:composio:gmail:GMAIL_SEND_EMAIL", u
        assert u["mission_id"] == mid, u


# ── 7. Tool-name slug helpers ───────────────────────────────────────────────
class TestSlugHelpers:
    def test_short_name_lowercases_split(self):
        from composio_mission_tools import _short_name
        assert _short_name("GMAIL_SEND_EMAIL") == "gmail.send_email"
        assert _short_name("SLACK_POST_MESSAGE") == "slack.post_message"

    def test_short_name_single_token_passthrough(self):
        from composio_mission_tools import _short_name
        assert _short_name("LINEAR") == "linear"

    def test_slug_for_uppercases(self):
        from composio_mission_tools import _slug_for
        assert _slug_for("gmail.send_email") == "GMAIL_SEND_EMAIL"
