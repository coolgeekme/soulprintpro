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
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 18            # hard safety cap per mission
MAX_EVENTS_IN_CONTEXT = 40     # truncation window for the loop's memory

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
    model: Optional[str] = None  # reserved (we use Sonnet by default)


class AnswerMissionBody(BaseModel):
    answer: str = Field(..., min_length=1, max_length=4000)


# ── Serialisers ────────────────────────────────────────────────────────────
def _iso(d):
    return d.isoformat() if isinstance(d, datetime) else d


def serialize_mission(m: dict) -> dict:
    return {
        "id": m.get("id"),
        "org_id": m.get("org_id"),
        "goal": m.get("goal"),
        "status": m.get("status"),
        "lead_agent_id": m.get("lead_agent_id"),
        "lead_agent_name": m.get("lead_agent_name"),
        "iteration_count": m.get("iteration_count", 0),
        "started_at": _iso(m.get("started_at")),
        "finished_at": _iso(m.get("finished_at")),
        "summary": m.get("summary", ""),
        "artifacts": m.get("artifacts") or [],
        "error": m.get("error", ""),
        "question": m.get("question", ""),
        "created_by": m.get("created_by"),
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
    doc = {
        "id": str(uuid.uuid4()),
        "mission_id": mission_id,
        "kind": kind,
        "ts": datetime.now(timezone.utc),
        "text": text,
        "tool": tool,
        "args": args or {},
        "result_preview": (result_preview or "")[:2000],
        "artifact": artifact,
        "iteration": iteration,
    }
    await db.mission_events.insert_one({**doc})
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
                raw = await chat.send_message(UserMessage(text=user_turn))
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
            try:
                result = await t(ctx, args)
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
        # Use the same artifact pipeline that chat uses — parsed block would
        # have these exact fields, so we synthesize one.
        from artifacts import _safe_filename, DEFAULT_MIME
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
        # Per-mission cache: if the same URL has already been fetched in
        # this mission's recent events, return the cached result with a
        # nudge so the LLM stops re-calling. Saves Playwright cycles AND
        # helps the loop-detector kick in faster.
        try:
            mid = ctx.get("mission_id", "")
            if mid:
                prev_cur = db.mission_events.find(
                    {"mission_id": mid, "kind": "tool_result", "tool": "browse_url"},
                    {"_id": 0, "result_preview": 1, "args": 1},
                ).sort("ts", -1).limit(20)
                async for prev in prev_cur:
                    # We don't store args on tool_result, so peek at the
                    # paired tool_call. Cheap heuristic: look for matching
                    # URL substring in the result_preview.
                    if url in (prev.get("result_preview") or ""):
                        return {
                            "cached": True,
                            "note": ("You already browsed this URL earlier "
                                     "in this mission. Use the prior text "
                                     "to write an artifact or move on."),
                            "url": url,
                        }
        except Exception:
            pass
        # Off-load to the browser_tools module — handles SSRF, JS render,
        # screenshot, text cleanup, and 30s timeout.
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
                doc = {
                    "id": str(uuid.uuid4()),
                    "org_id": ctx["org_id"],
                    "conversation_id": ctx.get("mission_id", ""),
                    "message_id": ctx.get("mission_id", ""),
                    "agent_id": ctx.get("lead_agent_id", ""),
                    "agent_name": ctx.get("lead_agent_name", "Mission"),
                    "created_by": ctx.get("created_by", ""),
                    "filename": doc_filename,
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
        return {
            "final_url": result.get("final_url"),
            "title": result.get("title"),
            "status": result.get("status"),
            "text_preview": (result.get("text") or "")[:8000],
            "text_bytes": len((result.get("text") or "").encode("utf-8")),
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
            doc = {
                "id": str(uuid.uuid4()),
                "org_id": ctx["org_id"],
                "conversation_id": ctx.get("mission_id", ""),
                "message_id": ctx.get("mission_id", ""),
                "agent_id": ctx.get("lead_agent_id", ""),
                "agent_name": ctx.get("lead_agent_name", "Mission"),
                "created_by": ctx.get("created_by", ""),
                "filename": doc_filename,
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
        return {
            "clicked": result.get("clicked"),
            "final_url": result.get("final_url"),
            "title": result.get("title"),
            "text_preview": (result.get("text") or "")[:4000],
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
        return {
            "filled": result.get("filled", []),
            "not_found": result.get("not_found", []),
            "final_url": result.get("final_url"),
            "title": result.get("title"),
            "text_preview": (result.get("text") or "")[:4000],
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
        return {
            "submitted_via": result.get("submitted_via"),
            "final_url": result.get("final_url"),
            "title": result.get("title"),
            "text_preview": (result.get("text") or "")[:4000],
            "artifact": artifact,
        }

    return [
        Tool("web_search",
             "Search the LIVE web (Brave + Perplexity fallback). Always use this for anything involving current events, recent launches, dates, prices, or 'latest'. Pass freshness='day'|'week'|'month'|'year' for time-sensitive queries — defaults to 'month' when the query mentions 'latest', 'this year', or a recent quarter. If results come back empty, DO NOT guess from your training data — retry with a refined query or ask_user.",
             '{"query": "search terms", "freshness": "day"|"week"|"month"|"year"}',
             web_search),
        Tool("read_org_document",
             "Read a previously-generated artifact from the Org Documents library. Call with no args to list, or pass document_id / name.",
             '{"document_id": "...", "name": "substring"}', read_org_document),
        Tool("write_artifact",
             "Save a new file (markdown brief, CSV, .ics invite, email, script) to the Documents library. Users download these.",
             '{"name": "file.md", "mime": "text/markdown", "title": "Human title", "content": "raw file body"}',
             write_artifact, side_effect=True),
        Tool("generate_image",
             "Render a new image (Nano Banana default, or GPT Image 1) and save it.",
             '{"prompt": "...", "model": "nano-banana"|"gpt-image-1"}',
             generate_image, side_effect=True),
        Tool("browse_url",
             "Load a real web page in a headless Chromium (handles JavaScript / SPA / cookie walls), return its rendered text and a screenshot. Use AFTER web_search when you need the full content of a specific result, or to read a page web_search couldn't index. Also the first step before browse_fill / browse_click / browse_submit on the same page.",
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
):
    r = APIRouter(prefix="/api")

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
        mission = {
            "id": str(uuid.uuid4()),
            "org_id": org_id,
            "goal": body.goal.strip(),
            "lead_agent_id": lead,
            "lead_agent_name": AGENTS[lead]["name"],
            "status": "running",
            "iteration_count": 0,
            "started_at": datetime.now(timezone.utc),
            "finished_at": None,
            "summary": "",
            "artifacts": [],
            "error": "",
            "question": "",
            "created_by": u["id"],
        }
        await db.missions.insert_one({**mission})

        tools = build_tools_factory()
        ctx = {
            "org_id": org_id,
            "mission_id": mission["id"],
            "lead_agent_id": lead,
            "lead_agent_name": AGENTS[lead]["name"],
            "created_by": u["id"],
        }
        task = asyncio.create_task(run_mission_loop(
            db=db, mission=mission, tools=tools, ctx=ctx,
            LlmChat=LlmChat, UserMessage=UserMessage,
            EMERGENT_LLM_KEY=EMERGENT_LLM_KEY,
            build_lead_system_prompt=build_lead_system_prompt,
        ))
        _RUNNING[mission["id"]] = task
        return serialize_mission(mission)

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

    class ContinueBody(BaseModel):
        instructions: str = Field("", max_length=2000)

    @r.post("/missions/{mission_id}/continue")
    async def continue_mission(mission_id: str, body: ContinueBody,
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
