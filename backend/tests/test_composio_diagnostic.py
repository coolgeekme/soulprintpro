"""Regression tests for the Composio agent's "no active tools" diagnostic
path. Production bug (Feb 2026): user said "check my Gmail" to the Chief
of Staff in their org; the agent replied "Gmail isn't connected" even
though Gmail HAD been connected (in this org, but the row was stuck at
INITIATED; or in a sibling org). The fallback to LlmChat had no idea
about connection state and the LLM just trusted its own ignorance.

Tests cover:
  • Keyword heuristic — `_likely_needed_toolkits`
  • Stuck-in-this-org rows produce remediation copy
  • Active-in-other-org rows produce switch/connect copy
  • Missing rows → returns None (caller falls back to LlmChat as before)
  • get_tools_for_user inline-refreshes stale INITIATED rows
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import composio_agent
import composio_integration


# ── Keyword heuristic ─────────────────────────────────────────────────────
def test_keyword_match_gmail() -> None:
    assert "gmail" in composio_agent._likely_needed_toolkits(
        "Hey, can you check my Gmail inbox?",
    )


def test_keyword_match_calendar_only() -> None:
    hits = composio_agent._likely_needed_toolkits(
        "What's on my calendar this week?",
    )
    assert hits == ["googlecalendar"]


def test_keyword_no_false_match_on_unrelated_text() -> None:
    assert composio_agent._likely_needed_toolkits(
        "Tell me about Q3 strategy.",
    ) == []


def test_keyword_multiple_hits() -> None:
    hits = composio_agent._likely_needed_toolkits(
        "Pull my inbox and tell me what's on my calendar",
    )
    assert set(hits) == {"gmail", "googlecalendar"}


# Regression for the Feb 2026 production bug: user asked "i have a pb.vision
# account, want to host an event on 3 courts for 2 hours, what's the best
# way to set this up?" The previous keyword list matched "calendar" /
# "schedule" / "meeting" / "appointment" / "email" alone, so this benign
# pickleball question wrongly triggered Gmail + Calendar diagnostics.
def test_keyword_pickleball_event_question_no_false_positive() -> None:
    pickleball_q = (
        "i have a https://pb.vision account and i'd like to host an event "
        "where i will record game play and be able to use PB Vision to give "
        "each player an eDUPR. I have 3 courts available and 2 hours. "
        "Whats the best way to set this up?"
    )
    assert composio_agent._likely_needed_toolkits(pickleball_q) == []


def test_keyword_does_not_match_bare_email_noun() -> None:
    # Just mentioning the word "email" should not trigger Gmail — only
    # verb-anchored phrases like "send an email" or possessives like
    # "my inbox" should.
    assert "gmail" not in composio_agent._likely_needed_toolkits(
        "Customer support email response time matters.",
    )


def test_keyword_does_not_match_bare_schedule_verb() -> None:
    # "What's the best way to schedule this event?" should NOT trigger
    # googlecalendar — there's no possessive ("my calendar"), no clear
    # tool action ("schedule a MEETING/call/appointment"), just the
    # generic word "schedule".
    assert "googlecalendar" not in composio_agent._likely_needed_toolkits(
        "What's the best way to schedule this event?",
    )


# ── Diagnostic builder ────────────────────────────────────────────────────
class _FakeCursor:
    """Tiny stand-in for motor cursors — supports `await .to_list(n)`."""
    def __init__(self, rows): self._rows = rows
    def to_list(self, _n): return _Awaitable(self._rows)


class _Awaitable:
    def __init__(self, val): self._val = val
    def __await__(self):
        async def _(): return self._val
        return _().__await__()


def _fake_db(rows_by_collection):
    db = MagicMock()
    def _find_factory(coll):
        def _find(query, projection=None):
            return _FakeCursor(rows_by_collection.get(coll, []))
        return _find
    db.composio_connections.find.side_effect = _find_factory("composio_connections")
    db.organizations.find_one = AsyncMock(side_effect=lambda q, p=None: {
        "_id": q.get("_id"),
        "name": "Other Workspace",
    })
    return db


@pytest.mark.asyncio
async def test_diagnostic_silent_when_no_keyword_match() -> None:
    db = _fake_db({})
    out = await composio_agent._diagnose_missing_tools(
        db, "org_a", "user_1", "Hello there",
    )
    assert out is None


@pytest.mark.asyncio
async def test_diagnostic_silent_when_no_rows() -> None:
    db = _fake_db({"composio_connections": []})
    out = await composio_agent._diagnose_missing_tools(
        db, "org_a", "user_1", "Check my gmail",
    )
    assert out is None


@pytest.mark.asyncio
async def test_diagnostic_in_org_stuck_row_surfaces_check_cta() -> None:
    rows = [{
        "toolkit": "gmail",
        "status": "INITIATED",
        "org_id": "org_a",
        "user_id": "user_1",
    }]
    db = _fake_db({"composio_connections": rows})
    out = await composio_agent._diagnose_missing_tools(
        db, "org_a", "user_1", "Check my Gmail inbox",
    )
    assert out is not None
    assert "stuck" in out.lower()
    assert "gmail" in out.lower()
    # Remediation copy must point to the Connections panel.
    assert "connections" in out.lower()


@pytest.mark.asyncio
async def test_diagnostic_other_org_active_surfaces_switch_cta() -> None:
    # The dummy ObjectId-shaped string we'll re-use — the production
    # path imports bson.ObjectId at module-load and tries to construct
    # it from r["org_id"]. We patch _diagnose's ObjectId import target
    # so tests don't need real ObjectIds.
    rows = [{
        "toolkit": "gmail",
        "status": "ACTIVE",
        "org_id": "5f00000000000000000000ff",
        "user_id": "user_1",
        "nickname": "Work Gmail",
    }]
    db = _fake_db({"composio_connections": rows})
    out = await composio_agent._diagnose_missing_tools(
        db, "org_a", "user_1", "Send an email to acme",
    )
    assert out is not None
    assert "another workspace" in out.lower() or "other workspace" in out.lower()
    assert "work gmail" in out.lower()


@pytest.mark.asyncio
async def test_diagnostic_prefers_in_org_stuck_when_both_present() -> None:
    rows = [
        {"toolkit": "gmail", "status": "INITIATED", "org_id": "org_a",
         "user_id": "user_1"},
        {"toolkit": "gmail", "status": "ACTIVE", "org_id": "5f00000000000000000000ff",
         "user_id": "user_1", "nickname": "Personal"},
    ]
    db = _fake_db({"composio_connections": rows})
    out = await composio_agent._diagnose_missing_tools(
        db, "org_a", "user_1", "Pull my Gmail inbox",
    )
    assert out is not None
    # In-org-stuck advice always appears first.
    assert "stuck" in out.lower()


# ── run_with_tools surfaces diagnostic instead of falling back ────────────
@pytest.mark.asyncio
async def test_run_with_tools_returns_diagnostic_when_no_tools_but_stuck_row() -> None:
    """End-to-end: empty tools list + stuck row → run_with_tools should
    return a non-None text reply (the diagnostic) rather than falling
    through to LlmChat."""
    with patch.object(composio_integration, "is_available", return_value=True), \
         patch.object(composio_agent.composio, "get_tools_for_user",
                       new=AsyncMock(return_value=[])), \
         patch.object(composio_agent, "_diagnose_missing_tools",
                       new=AsyncMock(return_value="Heads-up: Gmail stuck …")):
        text, audit = await composio_agent.run_with_tools(
            db=MagicMock(),
            org_id="org_a", user_id="user_1",
            agent_id="aid", agent_name="Chief of Staff",
            conv_id="cid",
            system_prompt="sys",
            user_prompt="Conversation so far:\nUser: Check my Gmail",
            latest_user_text="Check my Gmail",
        )
    assert text == "Heads-up: Gmail stuck …"
    assert audit == []


@pytest.mark.asyncio
async def test_run_with_tools_falls_back_when_no_diagnostic() -> None:
    """When no stuck/cross-org rows are found, we MUST still return
    (None, []) so the caller's LlmChat fallback runs."""
    with patch.object(composio_integration, "is_available", return_value=True), \
         patch.object(composio_agent.composio, "get_tools_for_user",
                       new=AsyncMock(return_value=[])), \
         patch.object(composio_agent, "_diagnose_missing_tools",
                       new=AsyncMock(return_value=None)):
        text, audit = await composio_agent.run_with_tools(
            db=MagicMock(),
            org_id="org_a", user_id="user_1",
            agent_id="aid", agent_name="Chief of Staff",
            conv_id="cid",
            system_prompt="sys", user_prompt="Generic chat",
            latest_user_text="Generic chat",
        )
    assert text is None
    assert audit == []


@pytest.mark.asyncio
async def test_run_with_tools_ignores_transcript_only_keyword_hits() -> None:
    """Regression for the Feb 2026 production bug: every Chief-of-Staff
    reply was being prefixed with the Gmail/Calendar "Heads-up before I
    try anything…" diagnostic — even on completely unrelated questions —
    because the diagnostic was scanning the FULL `user_prompt` (which
    includes the transcript tail). After this fix the diagnostic must
    look ONLY at the latest user message.

    Setup: latest message is about pickleball (no toolkit keywords).
    The full `user_prompt` (transcript tail) contains an old turn where
    the user mentioned "send an email" — this MUST be ignored."""
    full_prompt_with_transcript = (
        "Conversation so far:\n"
        "User: hey, can you send an email to acme?\n"      # <- old turn
        "Chief of Staff (agent): Sure, drafted it.\n"
        "User: thanks!\n"
        "Most recent message is from User: i have a https://pb.vision "
        "account and i'd like to host an event where i will record game "
        "play. I have 3 courts and 2 hours. Whats the best way to set "
        "this up?\n"
        "Respond as Chief of Staff."
    )
    latest = (
        "i have a https://pb.vision account and i'd like to host an event "
        "where i will record game play. I have 3 courts and 2 hours. "
        "Whats the best way to set this up?"
    )
    diag_mock = AsyncMock(return_value=None)
    with patch.object(composio_integration, "is_available", return_value=True), \
         patch.object(composio_agent.composio, "get_tools_for_user",
                       new=AsyncMock(return_value=[])), \
         patch.object(composio_agent, "_diagnose_missing_tools", new=diag_mock):
        text, audit = await composio_agent.run_with_tools(
            db=MagicMock(),
            org_id="org_a", user_id="user_1",
            agent_id="aid", agent_name="Chief of Staff",
            conv_id="cid",
            system_prompt="sys",
            user_prompt=full_prompt_with_transcript,
            latest_user_text=latest,
        )
    # Diagnostic was called with the LATEST text, not the transcript.
    diag_mock.assert_awaited_once()
    args, _ = diag_mock.call_args
    assert args[3] == latest, (
        "Diagnostic must scan ONLY the latest user message — passing the "
        "full transcript causes the warning to fire every turn forever."
    )
    # And since the latest text has zero toolkit keywords, the call
    # returns (None, []) so the LlmChat fallback runs.
    assert text is None
    assert audit == []


# ── get_tools_for_user inline refresh ─────────────────────────────────────
@pytest.mark.asyncio
async def test_get_tools_for_user_refreshes_stale_initiated_rows() -> None:
    """When zero ACTIVE rows exist but a stale INITIATED row does,
    `get_tools_for_user` MUST inline-refresh it before giving up.

    We mock `refresh_connection_status` to flip the row to ACTIVE on
    the second find() call — proves the re-query path runs."""
    old_ts = datetime.now(timezone.utc) - timedelta(seconds=600)
    # State across find() calls: first two are empty (no ACTIVE), then
    # the stale find returns one INITIATED row, then after refresh the
    # final ACTIVE find returns one row.
    call_log: list[dict] = []

    def _make_cursor(rows):
        c = MagicMock()
        c.to_list = AsyncMock(return_value=rows)
        return c

    def _fake_find(query, projection=None):
        call_log.append(query)
        # Query 1: ACTIVE-only → []
        # Query 2: INITIATED stale → one row
        # Query 3: ACTIVE-only again (post-refresh) → one row
        n = len([q for q in call_log if q.get("status")])
        if n == 1 and query.get("status") == "ACTIVE":
            return _make_cursor([])
        if query.get("status", {}) and "$in" in (query.get("status") or {}):
            return _make_cursor([{
                "connection_id": "conn_xyz",
                "initiated_at": old_ts,
                "toolkit": "gmail",
            }])
        if query.get("status") == "ACTIVE":
            return _make_cursor([{"toolkit": "gmail"}])
        return _make_cursor([])

    db = MagicMock()
    db.composio_connections.find = _fake_find

    refresh_calls = []

    async def _fake_refresh(_db, cid):
        refresh_calls.append(cid)

    with patch.object(composio_integration, "is_available", return_value=True), \
         patch.object(composio_integration, "refresh_connection_status",
                       new=_fake_refresh), \
         patch.object(composio_integration, "_get_client") as gc:
        gc.return_value.tools.get.return_value = [{"function": {"name": "GMAIL_SEND_EMAIL"}}]
        out = await composio_integration.get_tools_for_user(
            db, "org_a", "user_1",
        )
    # We refreshed the one stale row + ended up with one tool exposed.
    assert refresh_calls == ["conn_xyz"]
    assert out == [{"function": {"name": "GMAIL_SEND_EMAIL"}}]
