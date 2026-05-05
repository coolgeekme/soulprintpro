"""Regression test for the smart_start "orphan conversation" bug.

Repro: a user types a prompt with external URLs into the team page's
smart-start composer, and the production deployment's web-fetch /
LLM step raises (timeout, rate-limit, transient network blip).
Pre-fix behaviour:
  * The conversation + user message were already inserted in MongoDB.
  * The exception bubbled up as HTTP 500 to the client.
  * The user retried, creating ANOTHER conversation (and another, and
    another) — each leaving an orphan thread in the database.

Post-fix behaviour:
  * The exception is caught.
  * The endpoint still returns 200 with the conversation + user
    message + a friendly fallback agent message.
  * Total convs created for N retries == N (not N², not 0).

This test patches `generate_agent_reply` to raise and confirms the
endpoint stays 200 + only one conversation is created per call.
"""
import os
import asyncio
import pytest
from unittest.mock import patch, AsyncMock

# Required env so server.py imports
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "smart_start_test")
os.environ.setdefault("CORS_ORIGINS", "*")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("EMERGENT_LLM_KEY", "sk-test")


@pytest.mark.asyncio
async def test_smart_start_returns_200_when_agent_reply_raises():
    """Patch `generate_agent_reply` to raise — assert smart_start still
    returns a serializable conversation + a fallback agent message."""
    import server as srv

    # Stub team + user
    fake_team = {
        "_id": "fake_team_oid", "name": "Test Team",
        "soulprint": {"mission": "test", "tone": "calm", "audience": "devs", "values": "honesty"},
        "org_id": "fake_org_oid",
    }
    fake_user = {"id": "u1", "name": "Tester"}

    inserts: list[dict] = []

    async def fake_insert_one(doc):
        inserts.append(doc)
        # Mimic motor's insert result API
        class _R: inserted_id = "mock_conv_oid"
        return _R()

    async def fake_team_find_one(query):
        return fake_team

    # Mock the DB collections used by smart_start
    fake_conversations = type("_C", (), {})()
    fake_conversations.insert_one = fake_insert_one
    fake_messages = type("_M", (), {})()
    fake_messages.insert_one = fake_insert_one
    fake_teams = type("_T", (), {})()
    fake_teams.find_one = fake_team_find_one

    fake_db = type("_DB", (), {})()
    fake_db.conversations = fake_conversations
    fake_db.messages = fake_messages
    fake_db.teams = fake_teams

    body = srv.SmartStartBody(text="Review https://example.com and recommend.")

    with patch.object(srv, "db", fake_db), \
         patch.object(srv, "require_team_member", new=AsyncMock(return_value={"team": fake_team})), \
         patch.object(srv, "ObjectId", side_effect=lambda x: x), \
         patch.object(srv, "route_best_agent", new=AsyncMock(return_value="specialized-chief-of-staff")), \
         patch.object(srv, "detect_image_intent", new=AsyncMock(return_value=False)), \
         patch.object(srv, "detect_video_intent", new=AsyncMock(return_value=False)), \
         patch.object(srv, "generate_agent_reply", new=AsyncMock(side_effect=RuntimeError("brave fetch timeout"))), \
         patch.object(srv, "process_agent_artifacts", new=AsyncMock()), \
         patch.dict(srv.AGENTS, {"specialized-chief-of-staff": {"name": "Chief of Staff"}}, clear=False):
        result = await srv.smart_start(team_id="t1", body=body, u=fake_user)

    # The endpoint MUST return a usable response (no 500)
    assert result["routed_agent_id"] == "specialized-chief-of-staff"
    assert "conversation" in result
    assert "user_message" in result
    assert "agent_message" in result
    # The fallback agent message must carry the friendly nudge, not be empty
    assert "hiccup" in result["agent_message"]["text"].lower() \
        or "try" in result["agent_message"]["text"].lower()
    # Three writes: conv, user message, fallback agent message — exactly one conv
    conv_writes = [d for d in inserts if "team_id" in d and "agent_ids" in d]
    msg_writes = [d for d in inserts if d.get("conversation_id")]
    assert len(conv_writes) == 1, f"expected 1 conv, got {len(conv_writes)}"
    assert len(msg_writes) == 2, f"expected user + fallback agent, got {len(msg_writes)}"


if __name__ == "__main__":
    asyncio.run(test_smart_start_returns_200_when_agent_reply_raises())
    print("OK")
