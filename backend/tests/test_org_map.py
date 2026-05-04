"""Regression tests for the Org Mind Map data builder (org_map.py).

Validates the Phase 2 cost & time heatmap fields land on the payload:
  • Each node has cost/time_ms/calls keys (even if zero).
  • Org-level totals include cost_max + time_ms_max for client-side
    heatmap normalisation.
  • The aggregator reads from the real `llm_calls` collection (the bug
    Phase 2 fixed — earlier code queried a non-existent collection).
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
        # 3 chat calls totalling $0.45 cost / 4500ms latency.
        await db.llm_calls.insert_many([
            {
                "provider": "openai", "model": "gpt-4o-mini", "kind": "chat",
                "prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130,
                "cost_usd": 0.15, "latency_ms": 1500, "ok": True,
                "org_id": org_id, "user_id": "u1", "agent_id": "reggie-the-strategist",
                "conversation_id": conv_id, "created_at": now,
            },
            {
                "provider": "openai", "model": "gpt-4o-mini", "kind": "chat",
                "prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130,
                "cost_usd": 0.15, "latency_ms": 1500, "ok": True,
                "org_id": org_id, "user_id": "u1", "agent_id": "reggie-the-strategist",
                "conversation_id": conv_id, "created_at": now,
            },
            {
                "provider": "openai", "model": "gpt-4o-mini", "kind": "chat",
                "prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130,
                "cost_usd": 0.15, "latency_ms": 1500, "ok": True,
                "org_id": org_id, "user_id": "u1", "agent_id": "reggie-the-strategist",
                "conversation_id": conv_id, "created_at": now,
            },
        ])
    return org_id, team_id, conv_id


async def _cleanup(db, org_id):
    await db.organizations.delete_one({"_id": ObjectId(org_id)})
    await db.teams.delete_many({"org_id": org_id})
    await db.conversations.delete_many({"org_id": org_id})
    await db.messages.delete_many({})  # only seeded ones in this conv
    await db.llm_calls.delete_many({"org_id": org_id})
    await db.missions.delete_many({"org_id": org_id})
    await db.social_posts.delete_many({"org_id": org_id})


@pytest.mark.asyncio
async def test_org_map_phase2_heatmap_fields(db):
    """Every node carries cost/time_ms/calls + org totals expose maxes."""
    org_id, team_id, _conv_id = await _seed_org(db)
    try:
        payload = await build_org_map(db, org_id, window_days=30)
        # Top-level shape
        assert "totals" in payload and "nodes" in payload and "edges" in payload
        totals = payload["totals"]
        for k in ("cost_max", "time_ms_max", "llm_cost_usd", "llm_calls", "llm_time_ms"):
            assert k in totals, f"missing totals.{k}"
        # Telemetry should land in totals
        assert totals["llm_cost_usd"] >= 0.44, totals  # ~$0.45 with rounding
        assert totals["llm_calls"] == 3
        assert totals["llm_time_ms"] == 4500
        # Heatmap maxes at least equal to the team/user node values
        assert totals["cost_max"] > 0
        assert totals["time_ms_max"] > 0
        # Node-level fields
        kinds = {n["kind"]: n for n in payload["nodes"]}
        assert "soul" in kinds and "team" in kinds and "user" in kinds
        for n in payload["nodes"]:
            for k in ("cost", "time_ms", "calls"):
                assert k in n, f"node {n['id']} missing {k}"
        # Team should have aggregated cost from its conversation's telemetry
        assert kinds["team"]["cost"] >= 0.44
        assert kinds["team"]["time_ms"] == 4500
        # User node should reflect the same spend (same telemetry was tagged with user_id)
        assert kinds["user"]["cost"] >= 0.44
    finally:
        await _cleanup(db, org_id)


@pytest.mark.asyncio
async def test_org_map_no_telemetry_returns_zero_maxes(db):
    """Empty telemetry => cost_max/time_ms_max = 0; nodes still render."""
    org_id, _team_id, _conv_id = await _seed_org(db, with_telemetry=False)
    try:
        payload = await build_org_map(db, org_id, window_days=30)
        assert payload["totals"]["llm_cost_usd"] == 0
        assert payload["totals"]["cost_max"] == 0
        assert payload["totals"]["time_ms_max"] == 0
        # Node-level cost/time_ms should be 0 but the keys must exist
        for n in payload["nodes"]:
            assert n["cost"] == 0
            assert n["time_ms"] == 0
    finally:
        await _cleanup(db, org_id)
