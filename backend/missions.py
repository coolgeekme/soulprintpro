"""Mission Mode — autonomous multi-step agent workflows.

A Mission is a user-defined goal that a lead agent pursues by iterating
through a think → tool-call → observe → think loop until it calls `finish`,
hits the iteration cap, or the user cancels / pauses it.

Architecture:
  • `missions` collection — one doc per mission (goal, status, lead agent,
    artifacts produced, final summary).
  • `mission_events` collection — append-only log of every think-step, tool
    call, tool result, artifact produced, question asked, and final summary.
    The frontend polls /events to render a live transcript.
  • Each running mission has a background asyncio.Task on the server. NOT
    durable across restarts (Phase 3 work) — if the process dies, any
    in-flight missions are marked `failed`.
  • Tool registry dispatches to existing SoulPrint building blocks: web
    search, document read/write, image gen, social drafting. New tools
    drop in as Python coroutines.

The loop's LLM is prompted to emit STRICT JSON at each step — no markdown,
no prose. We parse it tolerantly (strip code fences, trailing commas) and
re-prompt the model if parsing fails twice in a row.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from bson import ObjectId
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 25            # hard safety cap per mission
MAX_EVENTS_IN_CONTEXT = 40     # truncation window for the loop's memory
LLM_CALL_TIMEOUT_S = 90        # hard ceiling on a single LLM round-trip
TOOL_CALL_TIMEOUT_S = 60       # hard ceiling on any single tool execution
DUPLICATE_CALL_SOFT_LIMIT = 2  # 2nd identical call: warning injected
DUPLICATE_CALL_HARD_LIMIT = 3  # 3rd identical call: auto-aborted with error

# Active mission Tasks keyed by mission_id — for cancel / pause signalling.
_RUNNING: Dict[str, asyncio.Task] = {}
_PAUSE_FLAGS: Dict[str, asyncio.Event] = {}


# ── Tool registry ──────────────────────────────────────────────────────────
class Tool:
    """A single tool exposed to an agent's loop."""

    def __init__(self, name: str, description: str, args_hint: str,
                 handler: Callable, side_effect: bool = False):
        self.name = name
        self.description = description
        self.args_hint = args_hint
        self.handler = handler
        self.side_effect = side_effect  # reserved — may skip on dry-runs later

    async def __call__(self, ctx: dict, args: dict) -> Any:
        return await self.handler(ctx, args)


def build_tool_catalog(tools: List[Tool]) -> str:
    """Render the catalogue as the system-prompt block the LLM sees."""
    lines = ["You have the following tools available. Call exactly ONE per turn:"]
    for t in tools:
        lines.append(f"- {t.name}: {t.description}")
        lines.append(f"    args: {t.args_hint}")
    lines.append("- ask_user: Pause the mission and ask the user a clarifying question.")
    lines.append('    args: {"question": "one sentence"}')
    lines.append("- finish: Mark the mission complete.")
    lines.append('    args: {"summary": "2–4 sentences of what was achieved"}')
    return "\n".join(lines)


# ── Models ─────────────────────────────────────────────────────────────────
class StartMissionBody(BaseModel):
    goal: str = Field(..., min_length=6, max_length=4000)
    lead_agent_id: Optional[str] = None
    conversation_id: Optional[str] = None  # set when started from a chat
    model: Optional[str] = None  # reserved (we use Sonnet by default)
    # IDs of files previously uploaded via POST /orgs/{id}/missions/uploads.
    # The attachments get injected into the mission's system prompt and
    # become accessible via the `read_attachment` tool at runtime.
    attachment_ids: List[str] = Field(default_factory=list)


class AnswerMissionBody(BaseModel):
    answer: str = Field(..., min_length=1, max_length=4000)


class ContinueMissionBody(BaseModel):
    """Payload for resuming a finished/cancelled/failed mission with
    extra instructions. Keep this at module scope — FastAPI needs to
    introspect it at route-registration time to recognise it as a
    request body rather than a query parameter."""
    instructions: str = Field("", max_length=2000)


# ── Serialisers ────────────────────────────────────────────────────────────
def _iso(d):
    return d.isoformat() if isinstance(d, datetime) else d


def serialize_mission(m: dict) -> dict:
    return {
        "id": m.get("id"),
        "org_id": m.get("org_id"),
        "conversation_id": m.get("conversation_id"),
        "goal": m.get("goal"),
        "status": m.get("status"),
        "lead_agent_id": m.get("lead_agent_id"),
        "lead_agent_name": m.get("lead_agent_name"),
        "iteration_count": m.get("iteration_count", 0),
        "started_at": _iso(m.get("started_at")),
        "finished_at": _iso(m.get("finished_at")),
        "last_activity_at": _iso(m.get("last_activity_at")),
        "summary": m.get("summary", ""),
        "artifacts": m.get("artifacts") or [],
        "error": m.get("error", ""),
        "question": m.get("question", ""),
        "created_by": m.get("created_by"),
        # Social-posting context — lets the UI surface a "Upload cookies"
        # CTA when a post-mission fails because no session was on file.
        "social_platform": m.get("social_platform", ""),
        "has_social_session": bool(m.get("has_social_session", False)),
        # User-uploaded attachments available to the mission at runtime.
        # We don't leak the filesystem path — only browser-reachable URLs.
        "attachments": [
            {
                "id": a.get("id"),
                "original_name": a.get("original_name"),
                "kind": a.get("kind"),
                "mime": a.get("mime"),
                "size": a.get("size"),
                "url": a.get("url"),
            }
            for a in (m.get("attachments") or [])
        ],
    }


def serialize_event(e: dict) -> dict:
    return {
        "id": e.get("id"),
        "mission_id": e.get("mission_id"),
        "kind": e.get("kind"),
        "ts": _iso(e.get("ts")),
        "text": e.get("text", ""),
        "tool": e.get("tool", ""),
        "args": e.get("args") or {},
        "result_preview": e.get("result_preview", ""),
        "artifact": e.get("artifact") or None,
        "iteration": e.get("iteration", 0),
    }


# ── LLM loop helpers ───────────────────────────────────────────────────────
JSON_STEP_SCHEMA = """You MUST respond with STRICT JSON, no markdown fences, no prose outside the object.

Schema:
{
  "thought": "one sentence about what to do next",
  "action": "tool" | "finish" | "ask_user",
  "tool": "<tool name from the catalog>"   (required when action=="tool"),
  "args": { ... }                           (arguments for the tool / ask_user / finish)
}

Rules:
- "action" MUST be EXACTLY one of: "tool", "finish", "ask_user". Do NOT put a tool name in the action field — that goes in "tool".
- Example correct: {"thought":"…","action":"tool","tool":"web_search","args":{"query":"…"}}
- Example correct: {"thought":"…","action":"finish","args":{"summary":"…"}}
- Example WRONG: {"action":"web_search","tool":"web_search",…}   ← do not do this.
- Keep "thought" to a single sentence.
- Never invent tool names — pick from the catalog.
- When finishing, put a 2–4 sentence summary in args.summary.
- When asking the user, put the question in args.question.
"""


def _parse_llm_step(raw: str) -> Optional[dict]:
    """Tolerant JSON parser: strips ```json fences, finds the first {…}."""
    if not raw:
        return None
    t = raw.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    try:
        return json.loads(t)
    except (ValueError, TypeError):
        pass
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except (ValueError, TypeError):
            return None
    return None


async def _append_event(db, mission_id: str, *, kind: str, text: str = "",
                        tool: str = "", args: dict = None,
                        result_preview: str = "", artifact: dict = None,
                        iteration: int = 0) -> dict:
    now = datetime.now(timezone.utc)
    doc = {
        "id": str(uuid.uuid4()),
        "mission_id": mission_id,
        "kind": kind,
        "ts": now,
        "text": text,
        "tool": tool,
        "args": args or {},
        "result_preview": (result_preview or "")[:2000],
        "artifact": artifact,
        "iteration": iteration,
    }
    await db.mission_events.insert_one({**doc})
    # Heartbeat — frontend uses this to detect stalled missions and offer a
    # "force-cancel" action when nothing has happened in 2+ minutes.
    try:
        await db.missions.update_one(
            {"id": mission_id}, {"$set": {"last_activity_at": now}},
        )
    except Exception:
        # Heartbeat failures must never break the loop.
        pass
    return doc


# ── Main loop ──────────────────────────────────────────────────────────────
async def run_mission_loop(
    *, db, mission: dict, tools: List[Tool],
    ctx: dict, LlmChat, UserMessage, EMERGENT_LLM_KEY: str,
    build_lead_system_prompt: Callable[[str], str],
) -> None:
    """Drive one mission to completion."""
    mission_id = mission["id"]
    pause_flag = _PAUSE_FLAGS.setdefault(mission_id, asyncio.Event())
    pause_flag.clear()

    tool_map = {t.name: t for t in tools}
    catalog = build_tool_catalog(tools)
    sys_prompt = build_lead_system_prompt(catalog)

    iteration = 0
    try:
        while iteration < MAX_ITERATIONS:
            # Check cancellation / pause *before* hitting the LLM so we
            # don't burn a call when the user has already clicked Cancel.
            m = await db.missions.find_one({"id": mission_id})
            if not m or m.get("status") in {"cancelled", "failed", "finished", "awaiting_user"}:
                return
            if m.get("status") == "paused":
                await _append_event(db, mission_id, kind="system",
                                    text="Paused by user.", iteration=iteration)
                return

            iteration += 1
            await db.missions.update_one(
                {"id": mission_id}, {"$set": {"iteration_count": iteration}},
            )

            # Build loop memory from event history (truncated)
            history = await db.mission_events.find(
                {"mission_id": mission_id}
            ).sort("ts", 1).to_list(MAX_EVENTS_IN_CONTEXT)
            # Loop-detection: catch any tool getting hammered with the same
            # args over and over (LLMs frequently spin on browse_url or
            # web_search). If the last 3 tool calls are identical (same
            # tool, same args), inject a hard stop on the next turn.
            recent_calls = [e for e in history if e.get("kind") == "tool_call"][-3:]
            spinning_on_call = False
            spinning_tool = ""
            if len(recent_calls) >= 3:
                first = recent_calls[0]
                if all(
                    c.get("tool") == first.get("tool")
                    and json.dumps(c.get("args") or {}, sort_keys=True)
                        == json.dumps(first.get("args") or {}, sort_keys=True)
                    for c in recent_calls[1:]
                ):
                    spinning_on_call = True
                    spinning_tool = first.get("tool", "")
            # Original web_search-specific check (gentler, kept for cases
            # where the agent retries with slightly different queries — we
            # want to escalate once it's clearly fruitless).
            web_calls = [e for e in history if e.get("kind") == "tool_call"
                         and e.get("tool") == "web_search"][-4:]
            spinning_on_search = (
                len(web_calls) >= 4 and all(c.get("tool") == "web_search"
                                            for c in web_calls)
            )
            memory_lines: List[str] = []
            for e in history[-MAX_EVENTS_IN_CONTEXT:]:
                k = e.get("kind")
                if k == "think":
                    memory_lines.append(f"[{iteration}][think] {e.get('text','')}")
                elif k == "tool_call":
                    memory_lines.append(f"[tool_call] {e.get('tool','')} args={json.dumps(e.get('args') or {})[:600]}")
                elif k == "tool_result":
                    memory_lines.append(f"[tool_result] {e.get('tool','')} => {e.get('result_preview','')[:600]}")
                elif k == "artifact":
                    a = e.get("artifact") or {}
                    memory_lines.append(f"[artifact] saved {a.get('original_name','')} ({a.get('mime','')})")
                elif k == "system":
                    memory_lines.append(f"[system] {e.get('text','')[:200]}")

            user_turn = (
                f"MISSION GOAL:\n{mission['goal']}\n\n"
                f"WORK LOG (most-recent last):\n" + ("\n".join(memory_lines) or "(empty)")
                + (
                    f"\n\nIMPORTANT: You just called {spinning_tool} 3 times "
                    "in a row with IDENTICAL arguments. The result is the "
                    "same each time — re-calling won't change it. STOP "
                    "repeating. Use the data you already have to call "
                    "write_artifact, or move on to a different tool / URL, "
                    "or call finish."
                    if spinning_on_call else ""
                )
                + (
                    "\n\nIMPORTANT: You have already called web_search 4 times "
                    "in a row with no useful results. STOP retrying. Either "
                    "(a) call ask_user to clarify the request, or (b) call "
                    "finish with a truthful summary that live data was "
                    "unavailable. Do NOT call web_search again this turn."
                    if spinning_on_search else ""
                )
                + f"\n\nWhat is the next single action? Remember: STRICT JSON only."
                + f" You are on iteration {iteration}/{MAX_ITERATIONS}."
            )

            chat = LlmChat(
                api_key=EMERGENT_LLM_KEY,
                session_id=f"mission:{mission_id}",
                system_message=sys_prompt,
            ).with_model("openai", "gpt-4.1-mini")

            try:
                raw = await asyncio.wait_for(
                    chat.send_message(UserMessage(text=user_turn)),
                    timeout=LLM_CALL_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                # The LLM API hung beyond our ceiling — log it and give the
                # next iteration a chance instead of freezing the mission.
                logger.warning(
                    "mission %s LLM call timed out after %ds (iter %d)",
                    mission_id, LLM_CALL_TIMEOUT_S, iteration,
                )
                await _append_event(
                    db, mission_id, kind="system",
                    text=(f"LLM call exceeded {LLM_CALL_TIMEOUT_S}s — retrying "
                          "with a fresh round. If this persists, the model "
                          "provider may be degraded; cancel and try again."),
                    iteration=iteration,
                )
                continue
            except Exception as exc:
                logger.exception("mission %s LLM error: %s", mission_id, exc)
                await _append_event(db, mission_id, kind="system",
                                    text=f"LLM error: {exc}", iteration=iteration)
                await _fail_mission(db, mission_id, f"LLM error: {exc}")
                return

            step = _parse_llm_step(raw) or {}
            thought = (step.get("thought") or "").strip()
            if thought:
                await _append_event(db, mission_id, kind="think",
                                    text=thought, iteration=iteration)

            action = (step.get("action") or "").strip().lower()
            args = step.get("args") or {}

            # Tolerant action parsing — many LLMs use action="web_search"
            # (the tool name) instead of action="tool"+tool="web_search".
            # If the action matches a known tool, treat it as such.
            if action and action not in {"tool", "finish", "ask_user"} and action in tool_map:
                step["tool"] = action
                action = "tool"

            if action == "finish":
                summary = (args.get("summary") or thought or "").strip()[:2000] or "Mission complete."
                await _append_event(db, mission_id, kind="finish",
                                    text=summary, iteration=iteration)
                await db.missions.update_one(
                    {"id": mission_id},
                    {"$set": {
                        "status": "finished",
                        "summary": summary,
                        "finished_at": datetime.now(timezone.utc),
                    }},
                )
                return

            if action == "ask_user":
                question = (args.get("question") or "").strip()[:600] or \
                    "I need more information to continue — please clarify."
                await _append_event(db, mission_id, kind="question",
                                    text=question, iteration=iteration)
                await db.missions.update_one(
                    {"id": mission_id},
                    {"$set": {
                        "status": "awaiting_user",
                        "question": question,
                    }},
                )
                return

            if action != "tool":
                # Model wandered off protocol — nudge and continue
                await _append_event(db, mission_id, kind="system",
                    text=f"Non-protocol reply ignored: {raw[:160]}", iteration=iteration)
                continue

            tool_name = (step.get("tool") or "").strip()
            t = tool_map.get(tool_name)
            if not t:
                await _append_event(db, mission_id, kind="system",
                    text=f"Unknown tool '{tool_name}'. Try again with one from the catalog.",
                    iteration=iteration)
                continue

            await _append_event(db, mission_id, kind="tool_call",
                                tool=tool_name, args=args, iteration=iteration)

            # ── Server-enforced duplicate-call guard ───────────────────────
            # The system prompt begs the LLM not to re-call the same tool
            # with the same args, and the user_turn injects a warning once
            # we detect 3 identical calls — but LLMs still ignore it and
            # waste iterations. So: enforce it in code. If this exact
            # (tool_name, args) combo was already called once earlier in
            # this mission, short-circuit and return a synthetic error
            # that forces the LLM to pivot. We allow the first call to
            # execute fully; only exact re-runs are blocked.
            args_key = json.dumps(args or {}, sort_keys=True, default=str)
            prior_identical = [
                e for e in history
                if e.get("kind") == "tool_call"
                and e.get("tool") == tool_name
                and json.dumps(e.get("args") or {}, sort_keys=True, default=str) == args_key
            ]
            # `prior_identical` is the history BEFORE this turn; the
            # current call has already been appended but isn't in `history`.
            if prior_identical:
                block_msg = (
                    f"BLOCKED: you already called `{tool_name}` with these "
                    f"exact arguments earlier in this mission. The result "
                    f"won't change. Your next action MUST be one of: "
                    f"(a) call a different tool, (b) call `{tool_name}` "
                    f"with DIFFERENT arguments, (c) call write_artifact "
                    f"with what you have, or (d) call finish. Stop retrying."
                )
                result = {"error": block_msg}
                preview = json.dumps(result)[:1500]
                await _append_event(
                    db, mission_id, kind="tool_result",
                    tool=tool_name, result_preview=preview,
                    iteration=iteration,
                )
                continue
            try:
                result = await asyncio.wait_for(
                    t(ctx, args), timeout=TOOL_CALL_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "mission %s tool %s timed out after %ds (iter %d)",
                    mission_id, tool_name, TOOL_CALL_TIMEOUT_S, iteration,
                )
                result = {
                    "error": (f"Tool '{tool_name}' exceeded {TOOL_CALL_TIMEOUT_S}s "
                              "and was aborted. Try a different tool, simplify "
                              "the args (e.g. shorter prompt / different URL), "
                              "or call finish."),
                }
            except Exception as exc:
                logger.exception("tool %s raised: %s", tool_name, exc)
                result = {"error": str(exc)[:400]}

            # Short preview for the log + the LLM's memory
            if isinstance(result, dict):
                preview = json.dumps(result, default=str)[:1500]
                artifact = result.get("artifact") if "artifact" in result else None
            else:
                preview = str(result)[:1500]
                artifact = None
            await _append_event(db, mission_id, kind="tool_result",
                                tool=tool_name, result_preview=preview,
                                iteration=iteration)
            if artifact:
                await _append_event(db, mission_id, kind="artifact",
                                    artifact=artifact, iteration=iteration)
                await db.missions.update_one(
                    {"id": mission_id},
                    {"$push": {"artifacts": artifact}},
                )

        # iteration cap reached
        await _append_event(db, mission_id, kind="system",
                            text=f"Iteration cap ({MAX_ITERATIONS}) reached.",
                            iteration=iteration)
        await db.missions.update_one(
            {"id": mission_id},
            {"$set": {
                "status": "finished",
                "summary": "Mission stopped at the iteration cap — continue by starting a follow-up mission.",
                "finished_at": datetime.now(timezone.utc),
            }},
        )
    except asyncio.CancelledError:
        await db.missions.update_one(
            {"id": mission_id},
            {"$set": {
                "status": "cancelled",
                "finished_at": datetime.now(timezone.utc),
            }},
        )
        raise
    except Exception as exc:  # pragma: no cover
        logger.exception("mission %s crashed: %s", mission_id, exc)
        await _fail_mission(db, mission_id, str(exc)[:400])
    finally:
        # Release the sticky browser context (if any) so the Chromium
        # process doesn't accumulate one zombie context per mission.
        # Best-effort — losing the cleanup doesn't affect mission state.
        try:
            from browser_tools import cleanup_mission_page
            await cleanup_mission_page(mission_id)
        except Exception as exc:
            logger.warning("mission %s browser cleanup failed: %s", mission_id, exc)


async def _fail_mission(db, mission_id: str, err: str) -> None:
    await db.missions.update_one(
        {"id": mission_id},
        {"$set": {
            "status": "failed",
            "error": err,
            "finished_at": datetime.now(timezone.utc),
        }},
    )


# ── Tools ──────────────────────────────────────────────────────────────────
def build_tools(
    *, db, docs_root: Path, UPLOAD_DIR: Path, AGENTS: dict,
    brave_search, perplexity_web_search, perplexity_available,
    generate_image_for, save_artifacts,
) -> List[Tool]:
    """Wire our existing building blocks into Mission tools."""
    from browser_tools import (
        browse_url as _browse_url,
        browse_click as _browse_click,
        browse_fill as _browse_fill,
        browse_submit as _browse_submit,
    )

    async def web_search(ctx, args):
        q = (args.get("query") or "").strip()
        if not q:
            return {"error": "query is required"}
        # Honor the agent's explicit freshness override, else auto-detect.
        # We err on the side of fresh: anything that smells like "current /
        # recent / this year / this month / latest / today" or mentions a
        # year in the past 2 quarters gets a month-scoped freshness filter.
        explicit = (args.get("freshness") or "").strip().lower()
        freshness_map = {
            "day": "pd", "week": "pw", "month": "pm", "year": "py",
            "pd": "pd", "pw": "pw", "pm": "pm", "py": "py",
        }
        freshness = freshness_map.get(explicit, "")
        if not freshness:
            lower = q.lower()
            from datetime import datetime as _dt
            current_year = _dt.now(timezone.utc).year
            time_signals = [
                "today", "this week", "this month", "this quarter",
                "latest", "current", "right now", "recently",
                "last week", "last month", "this year",
                str(current_year), f"q1 {current_year}", f"q2 {current_year}",
                f"q3 {current_year}", f"q4 {current_year}",
            ]
            if any(sig in lower for sig in time_signals):
                freshness = "pm"   # Past month — aggressive but right for "latest"

        # Brave first, Perplexity fallback. Both respect `freshness`.
        results = []
        try:
            results = await brave_search(q, count=5, freshness=freshness or None) or []
        except Exception:
            results = []
        if not results and perplexity_available():
            try:
                results = await perplexity_web_search(
                    q, count=5, freshness=freshness or None,
                ) or []
            except Exception:
                results = []
        # Brave & Perplexity both return TypedDicts (`title`, `url`,
        # `snippet`, `published?`). Use `.get()`, not `getattr()` — they're
        # plain dicts at runtime.
        out = []
        for r in results[:5]:
            if isinstance(r, dict):
                out.append({
                    "title": (r.get("title") or "").strip(),
                    "url": (r.get("url") or "").strip(),
                    "snippet": (r.get("snippet") or r.get("description") or "")[:400],
                    "published": (r.get("published") or r.get("age") or ""),
                })
            else:
                out.append({
                    "title": getattr(r, "title", "") or "",
                    "url": getattr(r, "url", "") or "",
                    "snippet": (getattr(r, "snippet", "") or "")[:400],
                    "published": getattr(r, "published", "") or "",
                })
        # If we got nothing, be explicit so the LLM doesn't fall back on its
        # training-data "knowledge" of the topic — a stale answer is worse
        # than no answer.
        if not out:
            return {
                "query": q,
                "results": [],
                "freshness": freshness or "unset",
                "hint": ("No live results. DO NOT answer from your training "
                         "data — it is months or years old. Either retry "
                         "web_search with a different query, call ask_user "
                         "for guidance, or finish the mission honestly with "
                         "a note that live data was unavailable."),
            }
        return {"query": q, "results": out, "freshness": freshness or "unset"}

    async def read_org_document(ctx, args):
        # Look up by document_id first, then by original_name substring
        doc_id = (args.get("document_id") or "").strip()
        name_q = (args.get("name") or "").strip()
        org_id = ctx["org_id"]
        query: dict = {"org_id": org_id}
        if doc_id:
            query["id"] = doc_id
        elif name_q:
            query["$or"] = [
                {"original_name": {"$regex": re.escape(name_q), "$options": "i"}},
                {"title": {"$regex": re.escape(name_q), "$options": "i"}},
            ]
        else:
            # Return a directory listing
            cur = db.documents.find({"org_id": org_id}, {"_id": 0}).sort("created_at", -1).limit(20)
            listing = []
            async for d in cur:
                listing.append({
                    "id": d.get("id"),
                    "name": d.get("original_name"),
                    "kind": d.get("kind"),
                    "size": d.get("size"),
                })
            return {"listing": listing, "hint": "Pass document_id or name to read contents."}

        d = await db.documents.find_one(query, {"_id": 0})
        if not d:
            return {"error": "No matching document"}
        path = docs_root / d["org_id"] / d["filename"]
        try:
            content = path.read_text(encoding="utf-8", errors="replace")[:8000]
        except Exception as exc:
            return {"error": f"Couldn't read file: {exc}"}
        return {
            "id": d.get("id"),
            "name": d.get("original_name"),
            "mime": d.get("mime"),
            "content": content,
        }

    async def write_artifact(ctx, args):
        name = (args.get("name") or "").strip()
        content = args.get("content") or ""
        mime = (args.get("mime") or "").strip()
        title = (args.get("title") or "").strip()
        if not name:
            return {"error": "name is required"}
        if not content.strip():
            return {"error": "content is empty"}
        # Reject binary extensions BEFORE writing so we don't persist a
        # gibberish text-as-png that confuses the user. The agent should
        # use `generate_image` (or the dedicated video tool) instead.
        from artifacts import _safe_filename, DEFAULT_MIME, BINARY_EXTS
        req_ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if req_ext in BINARY_EXTS:
            img_exts = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff",
                        "svg", "ico", "heic", "heif", "avif"}
            if req_ext in img_exts:
                return {"error": (
                    f"write_artifact is TEXT-ONLY and cannot emit .{req_ext} files. "
                    "Use the `generate_image` tool for images — it produces a "
                    "real PNG and saves it to the Documents library. "
                    "Example: {\"tool\":\"generate_image\",\"args\":{\"prompt\":\"…\"}}"
                )}
            return {"error": (
                f"write_artifact cannot emit binary .{req_ext} files. "
                "Allowed text formats: .md, .csv, .json, .html, .ics, .vcf, "
                ".eml, .sh, .py, .js, .sql, .txt. For images use "
                "`generate_image`; video/audio generation is not yet exposed "
                "as a mission tool."
            )}
        clean, ext = _safe_filename(name)
        if not mime:
            mime = DEFAULT_MIME.get(ext, "application/octet-stream")
        attachments = await save_artifacts(
            db, [{"name": clean, "ext": ext, "mime": mime,
                  "title": title or clean, "content": content}],
            docs_root=docs_root,
            org_id=ctx["org_id"],
            conversation_id=ctx.get("mission_id", ""),
            message_id=ctx.get("mission_id", ""),
            agent_id=ctx.get("lead_agent_id", ""),
            agent_name=ctx.get("lead_agent_name", "Mission"),
            created_by=ctx.get("created_by", ""),
        )
        if not attachments:
            return {"error": "Persist failed"}
        a = attachments[0]
        return {
            "artifact": {
                "document_id": a.get("document_id"),
                "original_name": a.get("original_name"),
                "mime": a.get("mime"),
                "url": a.get("url"),
                "size": a.get("size"),
            },
            "ok": True,
        }

    async def generate_image(ctx, args):
        prompt = (args.get("prompt") or "").strip()
        model = (args.get("model") or "nano-banana").strip()
        if not prompt:
            return {"error": "prompt is required"}
        try:
            fname, caption, used = await generate_image_for(
                prompt, args.get("system_message", ""), model=model,
            )
        except Exception as exc:
            return {"error": str(exc)[:300]}
        return {
            "artifact": {
                "original_name": fname,
                "mime": "image/png" if fname.endswith(".png") else "image/jpeg",
                "url": f"/api/files/{fname}",
                "model": used,
            },
            "caption": caption,
            "ok": True,
        }

    async def draft_to_social_queue(ctx, args):
        # Phase 1 scope: use `write_artifact` to draft a `.md` social post
        # users can review before queueing. Native Social Queue draft tool
        # wires in next pass once we extract a public helper from
        # social_posts.py.
        return {"error": "Not available yet — use write_artifact to save a .md post for the Social Queue."}

    async def browse_url(ctx, args):
        url = (args.get("url") or "").strip()
        if not url:
            return {"error": "url is required"}
        # Off-load to the browser_tools module — handles SSRF, JS render,
        # screenshot, text cleanup, page-map, and 30s timeout. Sticky page
        # is keyed by mission_id so subsequent browse_fill / browse_click
        # / browse_submit operate on the same context.
        result = await _browse_url(
            url, take_screenshot=True, upload_dir=UPLOAD_DIR,
            mission_id=ctx.get("mission_id") or None,
        )
        if result.get("error"):
            return {"error": result["error"]}
        # If we captured a screenshot, register it in the documents library
        # so it shows up alongside other mission deliverables and is
        # downloadable. Mission detail page already renders artifact list.
        artifact = None
        shot = result.get("screenshot")
        if shot:
            shot_path = UPLOAD_DIR / shot
            try:
                shot_bytes = shot_path.read_bytes()
                # Register as a real Document. Save bytes verbatim into the
                # docs folder so capability URLs work.
                doc_filename = f"{uuid.uuid4().hex}.png"
                org_folder = docs_root / ctx["org_id"]
                org_folder.mkdir(parents=True, exist_ok=True)
                (org_folder / doc_filename).write_bytes(shot_bytes)
                # Friendly name from the URL host
                host = urlparse(result["final_url"]).hostname or "screenshot"
                friendly = f"{host.replace('.', '_')}.png"
                # Mirror to GridFS so the file survives container redeploys.
                from file_storage import save_blob_to_gridfs as _save_gfs  # noqa: E402
                _gridfs_id = await _save_gfs(db, shot_bytes, friendly, "image/png")
                doc = {
                    "id": str(uuid.uuid4()),
                    "org_id": ctx["org_id"],
                    "conversation_id": ctx.get("mission_id", ""),
                    "message_id": ctx.get("mission_id", ""),
                    "agent_id": ctx.get("lead_agent_id", ""),
                    "agent_name": ctx.get("lead_agent_name", "Mission"),
                    "created_by": ctx.get("created_by", ""),
                    "filename": doc_filename,
                    "gridfs_id": _gridfs_id,
                    "original_name": friendly,
                    "title": f"Screenshot of {result.get('title') or host}",
                    "mime": "image/png",
                    "kind": "image",
                    "ext": "png",
                    "size": len(shot_bytes),
                    "preview": "",
                    "tags": [],
                    "created_at": datetime.now(timezone.utc),
                }
                await db.documents.insert_one({**doc})
                artifact = {
                    "document_id": doc["id"],
                    "original_name": friendly,
                    "mime": "image/png",
                    "url": f"/api/files/docs/{ctx['org_id']}/{doc_filename}",
                    "size": len(shot_bytes),
                }
                # Tidy up the working copy in UPLOAD_DIR (we have a permanent
                # copy in docs_root now). Best-effort.
                try: shot_path.unlink(missing_ok=True)
                except Exception: pass
            except Exception as exc:
                logger.warning("browse_url screenshot persistence failed: %s", exc)
                artifact = None
        # Trim the text block we hand back to the LLM; the full screenshot
        # is already saved as a downloadable artifact.
        compact = _compact_page_map(result)
        return {
            "final_url": result.get("final_url"),
            "title": result.get("title"),
            "status": result.get("status"),
            "text_preview": (result.get("text") or "")[:6000],
            "text_bytes": len((result.get("text") or "").encode("utf-8")),
            "affordances_summary": compact["affordances_summary"],
            "links": compact["links"],
            "buttons": compact["buttons"],
            "forms": compact["forms"],
            "artifact": artifact,
        }

    async def screenshot_url(ctx, args):
        # Thin wrapper over browse_url that skips the text payload; useful
        # when the agent only wants the visual.
        sub = await browse_url(ctx, {"url": args.get("url")})
        if sub.get("error"):
            return sub
        sub.pop("text_preview", None)
        sub.pop("text_bytes", None)
        return sub

    # ── Layer B: form interaction ──────────────────────────────────────
    def _compact_page_map(result: dict) -> dict:
        """Distill the rich page-map (`links`, `buttons`, `forms`) into a
        compact, LLM-friendly shape. We:
          - drop selectors from links/buttons (LLM sees text + href; if it
            wants to click it can pass `text=` or use the URL)
          - keep first 12 links + 12 buttons (whatever the page has)
          - keep forms in full so the LLM sees field labels & types
          - synthesize an `affordances_summary` one-liner so the agent
            *immediately* notices forms present on the page
        """
        links_raw = result.get("links") or []
        buttons_raw = result.get("buttons") or []
        forms_raw = result.get("forms") or []
        links = [{"text": l.get("text"), "href": l.get("href")}
                 for l in links_raw[:12] if l.get("text")]
        buttons = [{"text": b.get("text"), "selector": b.get("selector")}
                   for b in buttons_raw[:12] if b.get("text")]
        forms = []
        for f in forms_raw[:4]:
            fields = []
            for fld in (f.get("fields") or [])[:12]:
                fields.append({
                    "name": fld.get("name"),
                    "type": fld.get("type"),
                    "label": fld.get("label"),
                    "placeholder": fld.get("placeholder"),
                    "required": fld.get("required"),
                })
            forms.append({
                "selector": f.get("selector"),
                "submit_text": f.get("submit_text"),
                "action": f.get("action"),
                "fields": fields,
            })
        # Plain-English summary the LLM can react to without parsing JSON
        parts = []
        if forms:
            field_names = ", ".join(
                (fld.get("label") or fld.get("name") or fld.get("placeholder") or fld.get("type") or "?")
                for fld in (forms[0].get("fields") or [])[:6]
            )
            parts.append(
                f"{len(forms)} form(s) on this page; first form has fields: {field_names or '(none visible)'}"
                + (f"; submit button says {forms[0]['submit_text']!r}" if forms[0].get("submit_text") else "")
            )
        else:
            parts.append("No <form> elements on this page.")
        if buttons:
            parts.append("Buttons: " + ", ".join(b["text"] for b in buttons[:8]))
        if links:
            parts.append("Links: " + ", ".join(l["text"] for l in links[:8]))
        return {
            "affordances_summary": " | ".join(parts),
            "links": links,
            "buttons": buttons,
            "forms": forms,
        }

    async def _persist_action_screenshot(ctx: dict, result: dict) -> Optional[dict]:
        """Promote the Playwright screenshot in `result["screenshot"]` (a
        filename in UPLOAD_DIR) into a real Document so the Mission UI can
        render it in the work log. Mutates result: removes 'screenshot'
        filename and sets result['artifact'] to the doc descriptor.
        """
        shot = result.get("screenshot")
        if not shot:
            return None
        shot_path = UPLOAD_DIR / shot
        try:
            shot_bytes = shot_path.read_bytes()
            doc_filename = f"{uuid.uuid4().hex}.png"
            org_folder = docs_root / ctx["org_id"]
            org_folder.mkdir(parents=True, exist_ok=True)
            (org_folder / doc_filename).write_bytes(shot_bytes)
            host = urlparse(result.get("final_url") or "").hostname or "page"
            friendly = f"{host.replace('.', '_')}.png"
            from file_storage import save_blob_to_gridfs as _save_gfs  # noqa: E402
            _gridfs_id = await _save_gfs(db, shot_bytes, friendly, "image/png")
            doc = {
                "id": str(uuid.uuid4()),
                "org_id": ctx["org_id"],
                "conversation_id": ctx.get("mission_id", ""),
                "message_id": ctx.get("mission_id", ""),
                "agent_id": ctx.get("lead_agent_id", ""),
                "agent_name": ctx.get("lead_agent_name", "Mission"),
                "created_by": ctx.get("created_by", ""),
                "filename": doc_filename,
                "gridfs_id": _gridfs_id,
                "original_name": friendly,
                "title": f"Screenshot of {result.get('title') or host}",
                "mime": "image/png",
                "kind": "image",
                "ext": "png",
                "size": len(shot_bytes),
                "preview": "",
                "tags": [],
                "created_at": datetime.now(timezone.utc),
            }
            await db.documents.insert_one({**doc})
            artifact = {
                "document_id": doc["id"],
                "original_name": friendly,
                "mime": "image/png",
                "url": f"/api/files/docs/{ctx['org_id']}/{doc_filename}",
                "size": len(shot_bytes),
            }
            try: shot_path.unlink(missing_ok=True)
            except Exception: pass
            return artifact
        except Exception as exc:
            logger.warning("action screenshot persistence failed: %s", exc)
            return None

    async def browse_click(ctx, args):
        mid = ctx.get("mission_id") or ""
        if not mid:
            return {"error": "browse_click requires a mission context"}
        selector = (args.get("selector") or "").strip()
        text = (args.get("text") or "").strip()
        result = await _browse_click(
            mid, selector=selector, text=text, upload_dir=UPLOAD_DIR,
        )
        if result.get("error"):
            return {"error": result["error"]}
        artifact = await _persist_action_screenshot(ctx, result)
        compact = _compact_page_map(result)
        return {
            "clicked": result.get("clicked"),
            "final_url": result.get("final_url"),
            "title": result.get("title"),
            "text_preview": (result.get("text") or "")[:4000],
            "affordances_summary": compact["affordances_summary"],
            "links": compact["links"],
            "buttons": compact["buttons"],
            "forms": compact["forms"],
            "artifact": artifact,
        }

    async def browse_fill(ctx, args):
        mid = ctx.get("mission_id") or ""
        if not mid:
            return {"error": "browse_fill requires a mission context"}
        fields = args.get("fields") or {}
        if not isinstance(fields, dict) or not fields:
            return {"error": "`fields` must be a non-empty object mapping label/selector → value"}
        result = await _browse_fill(mid, fields=fields, upload_dir=UPLOAD_DIR)
        if result.get("error"):
            return {"error": result["error"]}
        artifact = await _persist_action_screenshot(ctx, result)
        compact = _compact_page_map(result)
        return {
            "filled": result.get("filled", []),
            "not_found": result.get("not_found", []),
            "final_url": result.get("final_url"),
            "title": result.get("title"),
            "text_preview": (result.get("text") or "")[:4000],
            "affordances_summary": compact["affordances_summary"],
            "forms": compact["forms"],
            "artifact": artifact,
        }

    async def browse_submit(ctx, args):
        mid = ctx.get("mission_id") or ""
        if not mid:
            return {"error": "browse_submit requires a mission context"}
        selector = (args.get("selector") or "").strip()
        result = await _browse_submit(mid, selector=selector, upload_dir=UPLOAD_DIR)
        if result.get("error"):
            return {"error": result["error"]}
        artifact = await _persist_action_screenshot(ctx, result)
        compact = _compact_page_map(result)
        return {
            "submitted_via": result.get("submitted_via"),
            "final_url": result.get("final_url"),
            "title": result.get("title"),
            "text_preview": (result.get("text") or "")[:4000],
            "affordances_summary": compact["affordances_summary"],
            "links": compact["links"],
            "buttons": compact["buttons"],
            "forms": compact["forms"],
            "artifact": artifact,
        }

    async def read_attachment(ctx, args):
        """Read a user-uploaded mission attachment. Call with no args to
        list all attachments on this mission, or pass `name` (substring
        match against original_name) or `attachment_id` to get the full
        extracted text of one."""
        mission_id = ctx.get("mission_id") or ""
        if not mission_id:
            return {"error": "No mission context — read_attachment is only available inside a running mission."}
        mission = await db.missions.find_one({"id": mission_id}, {"_id": 0, "attachments": 1})
        attachments = (mission or {}).get("attachments") or []
        if not attachments:
            return {"error": "The user didn't attach any files to this mission."}
        att_id = (args.get("attachment_id") or "").strip()
        name_q = (args.get("name") or "").strip().lower()
        if not att_id and not name_q:
            # Return a compact listing
            return {"listing": [
                {
                    "attachment_id": a.get("id"),
                    "name": a.get("original_name"),
                    "kind": a.get("kind"),
                    "size": a.get("size"),
                    "preview": (a.get("text_excerpt") or "")[:200],
                }
                for a in attachments
            ], "hint": "Pass name (substring) or attachment_id to read full content."}
        match = None
        if att_id:
            match = next((a for a in attachments if a.get("id") == att_id), None)
        if match is None and name_q:
            match = next((a for a in attachments
                          if name_q in (a.get("original_name") or "").lower()), None)
        if not match:
            return {"error": "No matching attachment. Call read_attachment() with no args to list them."}
        # Return the already-persisted text_excerpt (trimmed at save time) OR,
        # if the file is short enough, re-read it from disk.
        text = match.get("text_excerpt") or ""
        kind = match.get("kind") or ""
        path = UPLOAD_DIR / (match.get("filename") or "")
        if kind in {"text", "doc", "pdf"} and path.is_file() and len(text) < 4000:
            try:
                from assets import extract_text, trim  # type: ignore
                ext = (match.get("original_name") or "").rsplit(".", 1)[-1].lower()
                text = trim(extract_text(path, ext), limit=12000)
            except Exception as exc:
                logger.info("read_attachment extract failed: %s", exc)
        return {
            "attachment_id": match.get("id"),
            "name": match.get("original_name"),
            "kind": kind,
            "mime": match.get("mime"),
            "size": match.get("size"),
            "url": match.get("url"),
            "content": text or "(no extractable text — binary asset)",
        }

    return [
        Tool("web_search",
             "Search the LIVE web (Brave + Perplexity fallback). Always use this for anything involving current events, recent launches, dates, prices, or 'latest'. Pass freshness='day'|'week'|'month'|'year' for time-sensitive queries — defaults to 'month' when the query mentions 'latest', 'this year', or a recent quarter. If results come back empty, DO NOT guess from your training data — retry with a refined query or ask_user.",
             '{"query": "search terms", "freshness": "day"|"week"|"month"|"year"}',
             web_search),
        Tool("read_org_document",
             "Read a previously-generated artifact from the Org Documents library. Call with no args to list, or pass document_id / name.",
             '{"document_id": "...", "name": "substring"}', read_org_document),
        Tool("read_attachment",
             "Read a file the user attached when starting this mission. Call with no args to list attachments; pass `name` (substring) or `attachment_id` to read the full text. Use this when the mission goal references 'the uploaded file' / 'the PDF I attached' / etc.",
             '{"name": "substring"}', read_attachment),
        Tool("write_artifact",
             "Save a TEXT file (markdown brief, CSV, .ics invite, email, script, JSON, HTML) to the Documents library. Users download these. ALLOWED extensions: .md .csv .tsv .json .html .ics .vcf .eml .sh .py .js .ts .sql .txt .xml .yaml. NOT for images — if you need an image, use `generate_image` instead; the artifact pipeline rejects binary extensions.",
             '{"name": "brief.md", "mime": "text/markdown", "title": "Human title", "content": "raw file body"}',
             write_artifact, side_effect=True),
        Tool("generate_image",
             "Render a new image (PNG) with Nano Banana (default) or GPT Image 1 and save it to the Documents library. Use this for ANY image deliverable — do NOT try to emit .png via write_artifact.",
             '{"prompt": "A flat-design infographic...", "model": "nano-banana"}',
             generate_image, side_effect=True),
        Tool("browse_url",
             "Load a real web page in a headless Chromium (handles JavaScript / SPA / cookie walls). Returns rendered text, a screenshot, AND a structured page-map: `forms` (with field labels & types), `buttons` and `links` (with their visible text). ALWAYS read the `affordances_summary` field first — it tells you what's clickable on this page. The first step in any form-fill or multi-hop navigation goal.",
             '{"url": "https://..."}',
             browse_url, side_effect=True),
        Tool("screenshot_url",
             "Visual-only snapshot of a page (no text extraction). Use when the user asks 'what does X look like' or for visual artifacts.",
             '{"url": "https://..."}',
             screenshot_url, side_effect=True),
        Tool("browse_fill",
             "Fill form inputs on the CURRENT browser page (after browse_url). Keys can be CSS selectors, ARIA labels, placeholders, or `name` attributes — the LLM-friendly way is to pass the visible field label, e.g. {'Email address': 'x@y.com', 'Your message': 'Hi…'}. For <select> pass the visible option text. Max 20 fields per call.",
             '{"fields": {"Email": "x@y.com", "Name": "Ada Lovelace"}}',
             browse_fill, side_effect=True),
        Tool("browse_click",
             "Click an element on the CURRENT browser page. Pass either a CSS `selector` or visible `text` (matches buttons, links, role=button, and submit inputs, case-insensitive). Use this for cookie banners, 'Next' wizard buttons, or navigating SPA tabs.",
             '{"selector": "#cta", "text": "Sign up"}',
             browse_click, side_effect=True),
        Tool("browse_submit",
             "Submit a form on the CURRENT browser page. With no args, clicks the first `button[type=submit]` / `input[type=submit]`. You can also pass a `selector` pointing at a specific form or submit button. Use AFTER browse_fill.",
             '{"selector": "form#contact"}',
             browse_submit, side_effect=True),
    ]


# ── HTTP router ────────────────────────────────────────────────────────────
def make_missions_router(
    *, db, AGENTS: dict, EMERGENT_LLM_KEY: str,
    LlmChat, UserMessage,
    require_org_member, get_current_user,
    build_tools_factory: Callable[[], List[Tool]],
    build_lead_system_prompt: Callable[[str], str],
    docs_root: Path,
    UPLOAD_DIR: Path,
):
    r = APIRouter(prefix="/api")

    # ── Mission attachments — files the user uploads BEFORE starting ────
    # Endpoint shape matches /conversations/{id}/upload. Files are saved
    # to UPLOAD_DIR under a `mission-attach/` prefix; a pending record is
    # inserted into db.mission_attachments and returned to the caller.
    # The caller passes the record's `id` back in StartMissionBody.
    # Pending records are consumed (and deleted) when a mission actually
    # starts; stale ones are cleaned up by a 24h TTL index on created_at.
    MISSION_ATTACH_PREFIX = "mission-attach"
    MAX_MISSION_ATTACH_SIZE = 25 * 1024 * 1024  # 25 MB per file
    # Accept the same broad allowlist we use for conversation uploads —
    # images, docs, audio, video, text. The full set lives in server.py as
    # ALLOWED_ATTACH_EXTS; inline here to avoid a circular import.
    ALLOWED_MISSION_ATTACH_EXTS = {
        # images
        "png", "jpg", "jpeg", "webp", "gif", "bmp", "heic", "heif", "avif",
        # docs
        "pdf", "docx", "doc", "xlsx", "pptx", "rtf",
        # text
        "txt", "md", "csv", "tsv", "json", "xml", "yaml", "yml", "html", "log",
        # audio
        "mp3", "wav", "m4a", "ogg", "flac", "aac",
        # video
        "mp4", "mov", "webm", "avi", "mkv",
    }

    def _attach_kind(ext: str) -> str:
        e = ext.lower().lstrip(".")
        if e in {"png", "jpg", "jpeg", "webp", "gif", "bmp", "heic", "heif", "avif"}:
            return "image"
        if e == "pdf":
            return "pdf"
        if e in {"docx", "doc", "xlsx", "pptx", "rtf"}:
            return "doc"
        if e in {"txt", "md", "csv", "tsv", "json", "xml", "yaml", "yml", "html", "log"}:
            return "text"
        if e in {"mp3", "wav", "m4a", "ogg", "flac", "aac"}:
            return "audio"
        if e in {"mp4", "mov", "webm", "avi", "mkv"}:
            return "video"
        return "file"

    @r.post("/orgs/{org_id}/missions/uploads")
    async def upload_mission_attachments(
        org_id: str,
        files: List[UploadFile] = File(...),
        u: dict = Depends(get_current_user),
    ):
        """Upload files that will be consumed by a mission started via
        POST /orgs/{id}/missions. Returns a list of attachment records —
        the caller stashes their `id` values and passes them back in
        StartMissionBody.attachment_ids.

        Max 6 files, 25 MB each. Files outlive their pending record for
        the mission lifetime; pending records are auto-deleted after 24h
        via a TTL index if the caller never starts the mission.
        """
        await require_org_member(org_id, u["id"])
        if not files:
            raise HTTPException(400, "At least one file is required")
        if len(files) > 6:
            raise HTTPException(400, "Up to 6 files per mission")
        attach_dir = UPLOAD_DIR / MISSION_ATTACH_PREFIX
        attach_dir.mkdir(parents=True, exist_ok=True)
        out: List[dict] = []
        now = datetime.now(timezone.utc)
        for upload in files:
            original = (upload.filename or "file").strip().replace("/", "_").replace("\\", "_")
            ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
            if ext not in ALLOWED_MISSION_ATTACH_EXTS:
                raise HTTPException(400, f"Unsupported file type: .{ext or '?'}")
            blob = await upload.read()
            if not blob:
                raise HTTPException(400, f"Empty file: {original}")
            if len(blob) > MAX_MISSION_ATTACH_SIZE:
                raise HTTPException(400,
                    f"'{original}' is too large (max {MAX_MISSION_ATTACH_SIZE // (1024 * 1024)} MB each).")
            fname = f"{MISSION_ATTACH_PREFIX}-{uuid.uuid4().hex}.{ext}"
            fpath = UPLOAD_DIR / fname
            fpath.write_bytes(blob)
            kind = _attach_kind(ext)
            # Extract a text preview for the system prompt so the agent
            # has immediate visibility without burning a tool call.
            text_excerpt = ""
            try:
                from assets import is_text_extractable, extract_text, trim  # type: ignore
                if is_text_extractable(ext):
                    text_excerpt = trim(extract_text(fpath, ext), limit=2000)
            except Exception as exc:
                logger.info("mission upload extract failed for %s: %s", original, exc)
            attach_id = str(uuid.uuid4())
            record = {
                "id": attach_id,
                "org_id": org_id,
                "uploader_id": u["id"],
                "filename": fname,
                "original_name": original,
                "kind": kind,
                "mime": upload.content_type or "",
                "size": len(blob),
                "url": f"/api/files/{fname}",
                "text_excerpt": text_excerpt,
                "created_at": now,
                "consumed_by_mission_id": "",  # filled at mission start
            }
            await db.mission_attachments.insert_one({**record})
            out.append(record)
        return {"attachments": out}

    @r.delete("/orgs/{org_id}/missions/uploads/{attachment_id}")
    async def delete_pending_attachment(
        org_id: str, attachment_id: str,
        u: dict = Depends(get_current_user),
    ):
        """Drop a pending attachment (user changed their mind before
        starting the mission). Only the uploader can delete. Consumed
        attachments (attached to a running/finished mission) are NOT
        deletable via this endpoint — they stay with the mission.
        """
        await require_org_member(org_id, u["id"])
        rec = await db.mission_attachments.find_one({
            "id": attachment_id, "org_id": org_id, "uploader_id": u["id"],
        })
        if not rec:
            raise HTTPException(404, "Attachment not found")
        if rec.get("consumed_by_mission_id"):
            raise HTTPException(409, "This attachment is already attached to a mission.")
        try:
            (UPLOAD_DIR / rec["filename"]).unlink(missing_ok=True)
        except Exception:
            pass
        await db.mission_attachments.delete_one({"_id": rec["_id"]})
        return {"ok": True}

    @r.get("/orgs/{org_id}/missions")
    async def list_missions(org_id: str, u: dict = Depends(get_current_user)):
        await require_org_member(org_id, u["id"])
        cur = db.missions.find({"org_id": org_id}, {"_id": 0}).sort("started_at", -1).limit(50)
        out = []
        async for m in cur:
            out.append(serialize_mission(m))
        return out

    @r.post("/orgs/{org_id}/missions")
    async def start_mission(org_id: str, body: StartMissionBody,
                            u: dict = Depends(get_current_user)):
        await require_org_member(org_id, u["id"])
        lead = body.lead_agent_id
        if not lead or lead not in AGENTS:
            # Default lead — an ops-minded agent. Project Shepherd is a good
            # all-rounder; fall back to the first catalog entry if it isn't
            # present.
            lead = "project-shepherd" if "project-shepherd" in AGENTS else next(iter(AGENTS))
        # If `conversation_id` was passed (mission started from inside a
        # chat), validate the user can post in that conversation. The chat
        # ownership check is done by load_conv_or_403 — but we don't have
        # that helper available here without a circular import, so we do a
        # cheap membership check via the conversation's team.
        conv_id = (body.conversation_id or "").strip() or None
        if conv_id:
            try:
                conv = await db.conversations.find_one({"_id": ObjectId(conv_id)})
            except Exception:
                conv = None
            if not conv:
                raise HTTPException(404, "Conversation not found")
            # Conversation must belong to this org (team→org chain). The
            # team doc carries org_id.
            try:
                team = await db.teams.find_one({"_id": ObjectId(conv["team_id"])})
            except Exception:
                team = None
            if not team or team.get("org_id") != org_id:
                raise HTTPException(400, "Conversation is not in this org")
        now = datetime.now(timezone.utc)
        # If the mission looks like a social post request, auto-inject the
        # org's stored Playwright cookies so the browser boots logged-in
        # and the agent doesn't try to ask the user for credentials. Also
        # stamp the detected platform onto the mission so the UI can
        # surface a helpful link back to /social/sessions if it fails.
        #
        # This is the same plumbing the dedicated autopost scheduler uses —
        # we extend it to user-initiated missions here so "post a tweet on
        # X" from the chat works the same as scheduled autopost.
        detected_platform = ""
        try:
            from social_autopost import (
                detect_social_platform_in_goal,
                load_org_social_storage_state,
                PLATFORM_COMPOSERS,
            )
            detected_platform = detect_social_platform_in_goal(body.goal)
        except Exception:
            PLATFORM_COMPOSERS = {}
        merged_state = {}
        has_session_for_detected = False
        if detected_platform:
            try:
                merged_state = await load_org_social_storage_state(
                    db=db, org_id=org_id,
                )
                # Did we get any cookies matching the detected platform?
                # We use a loose domain-suffix match because cookies
                # exported from x.com, api.x.com etc. all count.
                platform_domain_hints = {
                    "twitter":  ("x.com", "twitter.com"),
                    "linkedin": ("linkedin.com",),
                    "facebook": ("facebook.com", "fb.com"),
                    "threads":  ("threads.net", "instagram.com"),
                    "bluesky":  ("bsky.app", "bsky.social"),
                    "mastodon": ("mastodon",),  # user-chosen instance
                    "reddit":   ("reddit.com",),
                }
                hints = platform_domain_hints.get(detected_platform, ())
                has_session_for_detected = any(
                    any(h in (c.get("domain") or "") for h in hints)
                    for c in merged_state.get("cookies", [])
                )
            except Exception as exc:
                logger.warning(
                    "mission: failed to load social sessions for org %s: %s",
                    org_id, exc,
                )
        goal_with_context = body.goal.strip()
        if detected_platform:
            composer = PLATFORM_COMPOSERS.get(detected_platform, "")
            if has_session_for_detected:
                goal_with_context += (
                    f"\n\n[SYSTEM CONTEXT — DO NOT IGNORE]\n"
                    f"This mission involves posting on {detected_platform.upper()}.\n"
                    f"• Authenticated cookies for {detected_platform} are ALREADY "
                    f"loaded in your browser — do NOT ask the user to log in; "
                    f"do NOT ask for credentials.\n"
                    f"• You MUST actually publish the post via browse_url → "
                    f"browse_fill → browse_submit on {composer or 'the composer page'}. "
                    f"Do NOT finish with 'here's the draft, go post it'.\n"
                    f"• If you hit a login wall anyway (cookies expired), "
                    f"finish IMMEDIATELY with summary starting 'FAILED: login "
                    f"session expired on {detected_platform}'.\n"
                    f"• On successful publication, finish with summary starting "
                    f"'PUBLISHED:' and include the verified post URL.\n"
                )
            else:
                goal_with_context += (
                    f"\n\n[SYSTEM CONTEXT — DO NOT IGNORE]\n"
                    f"This mission involves posting on {detected_platform.upper()}.\n"
                    f"• NO authenticated session is on file for {detected_platform} — "
                    f"DO NOT attempt to log in, DO NOT ask the user for credentials.\n"
                    f"• Finish the mission IMMEDIATELY with summary: "
                    f"'FAILED: no login cookies on file for {detected_platform} — "
                    f"user must upload a session at the org's Social Queue page "
                    f"(Manage login sessions) before this mission can run.'\n"
                )
        # Resolve user-uploaded attachments. Validate owner + org, embed
        # them in the mission doc, and mark the pending records as
        # consumed so they're no longer deletable.
        resolved_attachments: List[dict] = []
        if body.attachment_ids:
            for att_id in body.attachment_ids[:6]:  # hard cap 6
                rec = await db.mission_attachments.find_one({
                    "id": att_id, "org_id": org_id, "uploader_id": u["id"],
                })
                if not rec:
                    raise HTTPException(400, f"Attachment {att_id} not found or already consumed.")
                if rec.get("consumed_by_mission_id"):
                    raise HTTPException(400, f"Attachment {att_id} was already used by another mission.")
                resolved_attachments.append({
                    "id": rec["id"],
                    "filename": rec["filename"],
                    "original_name": rec["original_name"],
                    "kind": rec["kind"],
                    "mime": rec.get("mime", ""),
                    "size": rec["size"],
                    "url": rec["url"],
                    "text_excerpt": rec.get("text_excerpt", ""),
                })
        # Append an ATTACHMENTS block to the goal so the agent knows what
        # the user dropped in + sees a preview of the text content. Full
        # text access is available via the `read_attachment` tool.
        if resolved_attachments:
            lines = [
                "\n\n[USER ATTACHMENTS — {} file(s) the user dropped onto this mission]".format(
                    len(resolved_attachments)
                ),
                "Use the `read_attachment` tool to get the FULL contents of any "
                "of these. Previews are truncated at 2000 chars.",
            ]
            for a in resolved_attachments:
                lines.append(
                    f"\n• {a['original_name']} ({a['kind']}, {a['size']} bytes) "
                    f"— id={a['id']}"
                )
                excerpt = a.get("text_excerpt", "")
                if excerpt:
                    lines.append(f"  preview: {excerpt[:600]}")
            goal_with_context += "\n".join(lines)
        mission = {
            "id": str(uuid.uuid4()),
            "org_id": org_id,
            "conversation_id": conv_id,
            "goal": goal_with_context,
            "lead_agent_id": lead,
            "lead_agent_name": AGENTS[lead]["name"],
            "status": "running",
            "iteration_count": 0,
            "started_at": now,
            "last_activity_at": now,
            "finished_at": None,
            "summary": "",
            "artifacts": [],
            "error": "",
            "question": "",
            "created_by": u["id"],
            "social_platform": detected_platform or "",
            "has_social_session": has_session_for_detected,
            "attachments": resolved_attachments,
        }
        await db.missions.insert_one({**mission})
        # Mark the pending attachment records as consumed so they can't be
        # double-used by another mission and so the TTL doesn't garbage-
        # collect their files.
        if resolved_attachments:
            await db.mission_attachments.update_many(
                {"id": {"$in": [a["id"] for a in resolved_attachments]}},
                {"$set": {"consumed_by_mission_id": mission["id"]}},
            )

        # When started from a conversation, drop a special `mission_card`
        # message into that conv so the user can watch progress inline. The
        # frontend renders these with a live-polling status component.
        card_msg = None
        if conv_id:
            card_msg = {
                "id": str(uuid.uuid4()),
                "conversation_id": conv_id,
                "sender_type": "agent",
                "sender_id": lead,
                "sender_name": AGENTS[lead]["name"],
                "agent_id": lead,
                "kind": "mission_card",
                "mission_id": mission["id"],
                "text": f"🚀 Mission started: {body.goal.strip()[:200]}",
                "timestamp": now,
            }
            await db.messages.insert_one({**card_msg})

        tools = build_tools_factory()
        ctx = {
            "org_id": org_id,
            "mission_id": mission["id"],
            "lead_agent_id": lead,
            "lead_agent_name": AGENTS[lead]["name"],
            "created_by": u["id"],
        }
        # Pre-load stored cookies so the headless browser boots logged-in
        # to every platform the org has uploaded a session for. Must happen
        # BEFORE run_mission_loop opens its first page, which is why we
        # register here (before asyncio.create_task).
        if merged_state and (merged_state.get("cookies") or merged_state.get("origins")):
            try:
                from browser_tools import register_mission_storage_state
                register_mission_storage_state(mission["id"], merged_state)
            except Exception as exc:
                logger.warning(
                    "mission %s: could not register storage_state: %s",
                    mission["id"], exc,
                )
        task = asyncio.create_task(run_mission_loop(
            db=db, mission=mission, tools=tools, ctx=ctx,
            LlmChat=LlmChat, UserMessage=UserMessage,
            EMERGENT_LLM_KEY=EMERGENT_LLM_KEY,
            build_lead_system_prompt=build_lead_system_prompt,
        ))
        _RUNNING[mission["id"]] = task
        out = serialize_mission(mission)
        if card_msg:
            out["chat_message"] = {k: v for k, v in card_msg.items() if k != "_id"}
        return out

    @r.get("/missions/{mission_id}")
    async def get_mission(mission_id: str, u: dict = Depends(get_current_user)):
        m = await db.missions.find_one({"id": mission_id}, {"_id": 0})
        if not m:
            raise HTTPException(404, "Mission not found")
        await require_org_member(m["org_id"], u["id"])
        return serialize_mission(m)

    @r.get("/missions/{mission_id}/events")
    async def list_events(mission_id: str, after: str = "",
                          u: dict = Depends(get_current_user)):
        m = await db.missions.find_one({"id": mission_id}, {"_id": 0})
        if not m:
            raise HTTPException(404, "Mission not found")
        await require_org_member(m["org_id"], u["id"])
        query: dict = {"mission_id": mission_id}
        if after:
            try:
                after_dt = datetime.fromisoformat(after.replace("Z", "+00:00"))
                query["ts"] = {"$gt": after_dt}
            except Exception:
                pass
        cur = db.mission_events.find(query, {"_id": 0}).sort("ts", 1).limit(500)
        out = []
        async for e in cur:
            out.append(serialize_event(e))
        return out

    @r.post("/missions/{mission_id}/cancel")
    async def cancel_mission(mission_id: str, u: dict = Depends(get_current_user)):
        m = await db.missions.find_one({"id": mission_id}, {"_id": 0})
        if not m:
            raise HTTPException(404, "Mission not found")
        await require_org_member(m["org_id"], u["id"])
        task = _RUNNING.pop(mission_id, None)
        if task and not task.done():
            task.cancel()
        await db.missions.update_one(
            {"id": mission_id},
            {"$set": {"status": "cancelled",
                      "finished_at": datetime.now(timezone.utc)}},
        )
        # Release browser context eagerly — the loop's `finally` will also
        # clean up but we want the Chromium memory back now, not after
        # CancelledError propagates.
        try:
            from browser_tools import cleanup_mission_page
            await cleanup_mission_page(mission_id)
        except Exception:
            pass
        return {"ok": True}

    @r.post("/missions/{mission_id}/answer")
    async def answer_mission(mission_id: str, body: AnswerMissionBody,
                             u: dict = Depends(get_current_user)):
        """Resume a mission that's `awaiting_user` by supplying the answer
        to the last question. The loop restarts with the answer appended
        to the work log as a system event."""
        m = await db.missions.find_one({"id": mission_id})
        if not m:
            raise HTTPException(404, "Mission not found")
        await require_org_member(m["org_id"], u["id"])
        if m.get("status") != "awaiting_user":
            raise HTTPException(400, "Mission isn't waiting on an answer.")
        await _append_event(db, mission_id, kind="system",
                            text=f"User answered: {body.answer.strip()[:400]}",
                            iteration=m.get("iteration_count", 0))
        await db.missions.update_one(
            {"id": mission_id},
            {"$set": {"status": "running", "question": ""}},
        )

        tools = build_tools_factory()
        ctx = {
            "org_id": m["org_id"],
            "mission_id": mission_id,
            "lead_agent_id": m.get("lead_agent_id"),
            "lead_agent_name": m.get("lead_agent_name"),
            "created_by": u["id"],
        }
        fresh = await db.missions.find_one({"id": mission_id})
        task = asyncio.create_task(run_mission_loop(
            db=db, mission=fresh, tools=tools, ctx=ctx,
            LlmChat=LlmChat, UserMessage=UserMessage,
            EMERGENT_LLM_KEY=EMERGENT_LLM_KEY,
            build_lead_system_prompt=build_lead_system_prompt,
        ))
        _RUNNING[mission_id] = task
        return {"ok": True}

    @r.post("/missions/{mission_id}/continue")
    async def continue_mission(mission_id: str, body: ContinueMissionBody,
                               u: dict = Depends(get_current_user)):
        """Resume a *finished* mission with new instructions. The original
        work log is preserved (so the agent has context); the new
        instructions are appended as a system event and the loop kicks back
        on. Useful when the agent finished too shallowly and you want to
        say 'go deeper — browse each app's site and add pricing details'.
        """
        m = await db.missions.find_one({"id": mission_id})
        if not m:
            raise HTTPException(404, "Mission not found")
        await require_org_member(m["org_id"], u["id"])
        if m.get("status") not in {"finished", "cancelled", "failed"}:
            raise HTTPException(400, "Mission is still running — wait or cancel first.")

        more = (body.instructions or "").strip() or "Continue and add more depth to the existing deliverable."
        await _append_event(db, mission_id, kind="system",
                            text=f"User asked to continue: {more[:400]}",
                            iteration=m.get("iteration_count", 0))
        # Reset status + finish marker, but keep the iteration counter so
        # the cap still applies in aggregate (we don't want infinite-resume
        # loops eating budget).
        await db.missions.update_one(
            {"id": mission_id},
            {"$set": {
                "status": "running",
                "summary": "",
                "finished_at": None,
                "error": "",
                "question": "",
                "goal": m["goal"] + f"\n\n[Continuation request: {more[:400]}]",
            }},
        )
        tools = build_tools_factory()
        ctx = {
            "org_id": m["org_id"],
            "mission_id": mission_id,
            "lead_agent_id": m.get("lead_agent_id"),
            "lead_agent_name": m.get("lead_agent_name"),
            "created_by": u["id"],
        }
        fresh = await db.missions.find_one({"id": mission_id})
        task = asyncio.create_task(run_mission_loop(
            db=db, mission=fresh, tools=tools, ctx=ctx,
            LlmChat=LlmChat, UserMessage=UserMessage,
            EMERGENT_LLM_KEY=EMERGENT_LLM_KEY,
            build_lead_system_prompt=build_lead_system_prompt,
        ))
        _RUNNING[mission_id] = task
        return {"ok": True}

    @r.delete("/missions/{mission_id}")
    async def delete_mission(mission_id: str,
                             u: dict = Depends(get_current_user)):
        """Permanently delete a mission, its event log, AND any artifacts
        produced by that mission (so owners can scrub a bad run completely).

        Cancels the asyncio task first if the mission is still live so we
        don't race the loop's own writes against our delete.
        """
        m = await db.missions.find_one({"id": mission_id})
        if not m:
            raise HTTPException(404, "Mission not found")
        await require_org_member(m["org_id"], u["id"])

        # Stop the loop if it's still running. Best-effort — if cancelling
        # raises, we still proceed with the delete; the task will see its
        # mission row gone on next iteration and bail.
        task = _RUNNING.pop(mission_id, None)
        if task and not task.done():
            try: task.cancel()
            except Exception: pass
        _PAUSE_FLAGS.pop(mission_id, None)
        # And release any sticky browser context the mission held open.
        try:
            from browser_tools import cleanup_mission_page
            await cleanup_mission_page(mission_id)
        except Exception:
            pass

        # Remove artifacts this mission produced (the linked Document rows
        # + the on-disk files). Each artifact dict has a `document_id`.
        for a in (m.get("artifacts") or []):
            doc_id = a.get("document_id")
            if not doc_id:
                continue
            try:
                d = await db.documents.find_one(
                    {"id": doc_id, "org_id": m["org_id"]},
                )
                if d:
                    try:
                        (docs_root / d["org_id"] / d["filename"]).unlink(missing_ok=True)
                    except Exception:
                        pass
                    await db.documents.delete_one({"id": doc_id})
            except Exception as exc:
                logger.warning("artifact cleanup failed for %s: %s", doc_id, exc)

        await db.mission_events.delete_many({"mission_id": mission_id})
        await db.missions.delete_one({"id": mission_id})
        return {"ok": True}

    return r


__all__ = [
    "Tool",
    "build_tools",
    "make_missions_router",
    "run_mission_loop",
]
