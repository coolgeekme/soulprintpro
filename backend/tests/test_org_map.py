"""Regression tests for the Org Mind Map data builder (org_map.py).

Validates the time-spent heatmap fields land on the payload:
  • Each node has time_ms/calls keys (even if zero).
  • Org-level totals include time_ms_max for client-side heatmap normalisation.
  • The aggregator reads from the real `llm_calls` collection.
  • COST is intentionally NOT in the payload — owners said they don't
    want LLM $ figures surfaced in the org-facing Mind Map.
"""
import os
import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

from org_map import build_org_map


@pytest_asyncio.fixture
async def db():
    url = os.environ.get("MONGO_URL")
    name = os.environ.get("DB_NAME") or "test_db"
    if not url:
        pytest.skip("MONGO_URL not configured")
    client = AsyncIOMotorClient(url)
    yield client[name]
    client.close()


async def _seed_org(db, *, with_telemetry=True):
    """Create a minimal but realistic org graph:
       org → 1 team → 1 conversation → telemetry rows → optionally a mission.
    """
    org = await db.organizations.insert_one({
        "name": "OrgMap Test Co",
        "soul_description": "Unit-test org",
        "created_at": datetime.now(timezone.utc),
    })
    org_id = str(org.inserted_id)
    team = await db.teams.insert_one({
        "org_id": org_id, "name": "QA Team",
        "agents": ["reggie-the-strategist"],
    })
    team_id = str(team.inserted_id)
    conv = await db.conversations.insert_one({
        "org_id": org_id, "team_id": team_id, "title": "test conv",
    })
    conv_id = str(conv.inserted_id)
    # 5 messages, 3 agent replies
    now = datetime.now(timezone.utc)
    msgs = []
    for i in range(5):
        msgs.append({
            "conversation_id": conv_id,
            "sender_type": "user" if i % 2 == 0 else "agent",
            "agent_id": None if i % 2 == 0 else "reggie-the-strategist",
            "timestamp": now - timedelta(minutes=i),
            "text": f"hello {i}",
        })
    await db.messages.insert_many(msgs)
    if with_telemetry:
        # 3 chat calls totalling 4500ms latency.
        for _ in range(3):
            await db.llm_calls.insert_one({
                "provider": "openai", "model": "gpt-4o-mini", "kind": "chat",
                "prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130,
                "cost_usd": 0.15, "latency_ms": 1500, "ok": True,
                "org_id": org_id, "user_id": "u1", "agent_id": "reggie-the-strategist",
                "conversation_id": conv_id, "created_at": now,
            })
    return org_id, team_id, conv_id


async def _cleanup(db, org_id):
    await db.organizations.delete_one({"_id": ObjectId(org_id)})
    await db.teams.delete_many({"org_id": org_id})
    await db.conversations.delete_many({"org_id": org_id})
    await db.messages.delete_many({})
    await db.llm_calls.delete_many({"org_id": org_id})
    await db.missions.delete_many({"org_id": org_id})
    await db.social_posts.delete_many({"org_id": org_id})


@pytest.mark.asyncio
async def test_org_map_time_heatmap_fields(db):
    """Every node carries time_ms/calls + org totals expose time_ms_max."""
    org_id, _team_id, _conv_id = await _seed_org(db)
    try:
        payload = await build_org_map(db, org_id, window_days=30)
        assert "totals" in payload and "nodes" in payload and "edges" in payload
        totals = payload["totals"]
        for k in ("time_ms_max", "llm_calls", "llm_time_ms", "members"):
            assert k in totals, f"missing totals.{k}"
        # Cost must NOT be surfaced in the payload (per owner request).
        assert "llm_cost_usd" not in totals
        assert "cost_max" not in totals
        # Telemetry should land in totals
        assert totals["llm_calls"] == 3
        assert totals["llm_time_ms"] == 4500
        assert totals["time_ms_max"] > 0
        # Node-level fields
        kinds = {n["kind"]: n for n in payload["nodes"]}
        assert "soul" in kinds and "team" in kinds
        # Phase-2b adds conversation nodes per the live conversations in
        # the org. With our seed we expect at least one.
        assert "conversation" in kinds, "expected a conversation node"
        for n in payload["nodes"]:
            for k in ("time_ms", "calls"):
                assert k in n, f"node {n['id']} missing {k}"
            assert "cost" not in n, f"node {n['id']} should not carry cost"
        # Team should have aggregated time from its conversation's telemetry
        assert kinds["team"]["time_ms"] == 4500
        # The conversation node should carry a category label.
        conv_node = kinds["conversation"]
        assert conv_node.get("category"), "conversation node missing category"
        assert " · " in conv_node["label"], "conversation label should include category prefix"
    finally:
        await _cleanup(db, org_id)


@pytest.mark.asyncio
async def test_org_map_no_telemetry_returns_zero_max(db):
    """Empty telemetry => time_ms_max = 0; nodes still render."""
    org_id, _team_id, _conv_id = await _seed_org(db, with_telemetry=False)
    try:
        payload = await build_org_map(db, org_id, window_days=30)
        assert payload["totals"]["time_ms_max"] == 0
        assert payload["totals"]["llm_calls"] == 0
        # Node-level time_ms/calls should be 0 but the keys must exist.
        for n in payload["nodes"]:
            assert n["time_ms"] == 0
            assert n["calls"] == 0
            assert "cost" not in n
    finally:
        await _cleanup(db, org_id)
