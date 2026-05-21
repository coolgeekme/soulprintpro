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
import os
import re
import uuid

import httpx
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
TOOL_CALL_TIMEOUT_S = 60       # default hard ceiling on a single tool execution
# Browser tools (browse_url, browse_click, etc.) get a longer ceiling
# because Cloudflare / Akamai / PerimeterX anti-bot challenges on
# protected sites (Shopify, Squarespace, ecommerce, news) routinely
# take 30-90s to clear. Below 120s the agent burns retry budget on
# sites that would have loaded fine given another 30-60s. Cap is still
# hard-bounded so a permanently-stuck navigation can't hang the loop.
BROWSER_TOOL_TIMEOUT_S = 150
_BROWSER_TOOL_NAMES = {
    "browse_url", "screenshot_url", "browse_click", "browse_fill",
    "browse_submit", "browse_text", "browse_keyboard",
    "web_scrape", "web_crawl", "web_extract",
}
DUPLICATE_CALL_SOFT_LIMIT = 2  # 2nd identical call: warning injected
DUPLICATE_CALL_HARD_LIMIT = 3  # 3rd identical call: auto-aborted with error

# ── Research-breadth enforcement ────────────────────────────────────────────
# Goals that imply broad, multi-source research must finish with ≥ N
# distinct domains browsed. Fewer than that → finish call is rejected
# and the agent is forced back into research mode.
_RESEARCH_GOAL_TERMS = (
    "research", "find ", "identify ", "common ", "top ", " most ", " biggest ",
    " best ", "compare", "analyse", "analyze", "analysis", "survey", "study",
    "trend", "industry", "market ", "competitive", "audit", "evaluate",
    "review", "report on", "investigate", "complaint", " issues", "problem",
    "pain point", "list of", "breakdown", "overview", "findings",
)
RESEARCH_MIN_DISTINCT_DOMAINS = 5


def _is_research_goal(goal: str) -> bool:
    """Heuristic: does this mission goal demand broad multi-source research?
    Liberal on purpose — false positives just trigger a soft enforcement
    that costs the agent 1-2 extra iterations on a non-research goal,
    which is far cheaper than letting a real research goal finish thin."""
    if not goal:
        return False
    g = " " + goal.lower() + " "
    return any(term in g for term in _RESEARCH_GOAL_TERMS)


# ── Dataset / lead-hunt enforcement ────────────────────────────────────────
# Goals that imply "harvest a list of N rows" (companies, leads, contacts,
# nonprofits, prospects, …) must finish with a CSV artifact carrying at
# least DATASET_MIN_ROWS data rows. Without this gate the agent happily
# ships a methodology PDF with zero rows — the exact bug reported Feb 2026
# on a "harvest businesses in 5 metros" mission.
_DATASET_GOAL_TERMS = (
    "harvest ", "lead hunt", "lead-hunt", "lead list", "leadgen",
    "lead-gen", "lead gen ", "leads in ", "leads from ",
    "list of compan", "list of business", "list of lead",
    "list of contact", "list of organi", "list of nonprofit",
    "list of prospect", "list of restaur", "list of agenc",
    "list of firm", "list of vendor", "list of suppl",
    "company list", "business list", "contact list", "prospect list",
    "dataset of ", "directory of ", "roster of ",
    "build a list", "build me a list", "build a database",
    "generate a list", "generate leads",
    "scrape compan", "scrape business", "scrape lead",
    "harvest a dataset", "harvest dataset",
)
DATASET_MIN_ROWS = 15
# How many times the dataset-row gate can fire before letting finish go
# through. One nudge wasn't enough in production — agents ignored it
# and re-called finish, and the old one-shot pattern let them through.
# Three nudges escalates with explicit urgency text on each attempt.
DATASET_GATE_MAX_NUDGES = 3
# Maximum rows we let the agent push into a single csv_append_rows call.
# Above this we truncate + warn — keeps the LLM honest and prevents one
# bogus call from polluting the dataset with hallucinated bulk.
DATASET_MAX_ROWS_PER_CALL = 25
# Hard ceiling on accumulated rows per CSV. Anything beyond this is almost
# certainly the agent over-claiming. Real lead-hunt missions in the
# wild produce 30-200 rows; 500 is a defensive cap.
DATASET_MAX_ROWS_TOTAL = 500


def _is_dataset_goal(goal: str) -> bool:
    """Heuristic: does this mission goal ask for a row-oriented dataset
    (list of companies / leads / contacts / etc.)? Same liberal-tendency
    rationale as `_is_research_goal`."""
    if not goal:
        return False
    g = " " + goal.lower() + " "
    return any(term in g for term in _DATASET_GOAL_TERMS)


def _count_csv_data_rows(artifacts: list) -> int:
    """Look at the mission's accumulated artifacts and return the largest
    `csv_row_count` field across .csv artifacts. We trust the field
    stamped by `csv_append_rows`; if absent (legacy write_artifact CSV),
    fall back to 0 so the gate trips and the agent goes back to the
    incremental tool."""
    best = 0
    for a in (artifacts or []):
        name = (a.get("original_name") or "").lower()
        if not name.endswith(".csv"):
            continue
        n = int(a.get("csv_row_count") or 0)
        if n > best:
            best = n
    return best



def _extract_domain(url: str) -> Optional[str]:
    """Return the registrable hostname (minus www.) or None if not a URL."""
    if not url or not isinstance(url, str):
        return None
    try:
        host = urlparse(url).hostname
        if not host:
            return None
        host = host.lower()
        if host.startswith("www."):
            host = host[4:]
        return host or None
    except Exception:
        return None


def _count_distinct_browsed_domains(history: list) -> int:
    """Walk the mission history of tool_call events and count distinct
    domains that `browse_url` was invoked against."""
    domains = set()
    for ev in history or []:
        if ev.get("kind") != "tool_call":
            continue
        if ev.get("tool") != "browse_url":
            continue
        args = ev.get("args") or {}
        url = args.get("url") or args.get("href") or ""
        dom = _extract_domain(url)
        if dom:
            domains.add(dom)
    return len(domains)


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


# Regex catches phrases that mean "stop asking, work with what you have".
# When the user's answer matches, we set a `user_declined_more_info` flag
# on the mission so every subsequent `ask_user` call is blocked. This is
# the fix for the production bug where missions kept asking the same
# clarifying question even after the user explicitly said "proceed" or
# "I have nothing more to add." Direct response to a real user report
# Feb 2026 (AstraZeneca Virginia plant audit looped on the same
# "provide sources/URLs" question).
_USER_DONE_PATTERNS = re.compile(
    r"\b("
    r"proceed|continue(?:\s+anyway)?|go\s+ahead|just\s+(?:go|do\s+it|finish)|"
    r"skip|move\s+on|carry\s+on|"
    r"finish|wrap\s+(?:it\s+)?up|complete\s+(?:it|the\s+mission)|"
    r"nothing\s+(?:more|else|to\s+add)|no\s+more(?:\s+info(?:rmation)?)?|"
    r"don'?t\s+have|i\s+have\s+no(?:thing)?|i'?ve\s+(?:said|told|replied|answered)|"
    r"already\s+(?:said|told|replied|answered|gave)|"
    r"use\s+what\s+you\s+(?:have|know)|work\s+with\s+what\s+you\s+have|"
    r"that'?s\s+(?:all|it)|no\s+(?:further|additional)|stop\s+asking|"
    r"don'?t\s+ask\s+again"
    r")\b",
    re.IGNORECASE,
)


def _looks_like_user_declined(text: str) -> bool:
    """Does the user's reply mean 'stop asking and proceed with what
    you have'? Used to short-circuit further ask_user calls in the
    mission loop."""
    return bool(_USER_DONE_PATTERNS.search((text or "")))


# Hard cap on how many times a single mission can call ask_user.
# After this many asks the loop intercepts the next ask_user and
# converts it into a synthetic system message that forces the agent
# to proceed with public data or finish with a documented gap.
MAX_ASK_USER_PER_MISSION = 2


class ContinueMissionBody(BaseModel):
    """Payload for resuming a finished/cancelled/failed mission with
    extra instructions. Keep this at module scope — FastAPI needs to
    introspect it at route-registration time to recognise it as a
    request body rather than a query parameter."""
    instructions: str = Field("", max_length=2000)


class DraftGoalBody(BaseModel):
    """Payload for the goal drafter — turns a rough user idea into a
    polished mission goal that follows our internal best practices
    (concrete deliverable, named format, measurable success criteria,
    source-breadth requirement, no fluff)."""
    goal: str = Field(..., min_length=3, max_length=2000)
    deliverable: str = Field("", max_length=400)
    success_looks_like: str = Field("", max_length=600)
    # Optional refine step: if set we treat `goal` as the previously-
    # drafted goal and apply `refine_instruction` to it.
    refine_instruction: str = Field("", max_length=400)


# Target character budget for drafted mission goals. Verified across
# the last 30 finished missions in this Foundry deployment — 200-1200
# chars is the sweet spot (≤200 too vague; ≥1200 starts contradicting
# itself). The drafter prompt enforces this.
GOAL_MIN_CHARS = 200
GOAL_MAX_CHARS = 1800

_DRAFTER_SYSTEM = """You rewrite a rough user idea into a polished autonomous-mission goal for the Foundry lead-agent loop.

Foundry missions are run by an LLM that picks tools (web_search, browse_url, browse_click/fill/submit, write_artifact, generate_image, read_org_document, …), iterates, and ships a deliverable. The single biggest predictor of a successful run is a precise goal. Your job is to write one.

OUTPUT FORMAT — STRICT
- Return JSON only. No prose, no markdown, no fences.
- Shape: {"goal": "<the polished mission goal>"}
- The `goal` value must be between 200 and 1800 characters.

BEST PRACTICES THE GOAL MUST FOLLOW
1. Lead with the concrete deliverable, named with a real filename and extension. Examples:
   • "Produce `competitor_teardown.pdf` — a 1-page brief…"
   • "Produce `linkedin_posts.md` — 5 ready-to-publish posts…"
   • "Produce `top_pricing_changes.csv` — one row per competitor with columns name, plan, monthly price, source URL."
2. Default file formats: `.pdf` for briefs/reports/audits/proposals; `.docx` only if the user mentioned Word; `.csv` for lists/tables/datasets/lead-hunts; `.md` only when the user explicitly asked for raw markdown; `.ics` for calendars. NEVER `.txt`.
   • **DATASET / LEAD-HUNT RULE**: if the input mentions "harvest", "list of companies / businesses / leads / contacts / nonprofits", "lead hunt / list / gen", "prospect list", "directory", "roster", "build a list / database", or "scrape companies / businesses", the deliverable MUST be a `.csv`. Spell out the columns explicitly in the goal (e.g. "columns: Business Name, City, State, Years in Business, Annual Revenue (USD), Website URL, Contact Name, Contact Phone, Contact Email, Source URL"). Set a minimum-rows target ("≥ 25 verified rows"). Tell the agent to use the `csv_append_rows` tool and to cite the Source URL on every row. Name free public sources by category in one short line ("harvest from Chamber of Commerce directories, BBB, Yelp, SEC EDGAR, state Secretary of State registries, OpenCorporates, USAspending.gov, and industry association rosters").
   • **CODE-SNIPPET RECIPE**: if the input asks for code, a snippet, a tag, an embed, a regex, a SQL query, an HTML/CSS/JS/TS block, a React/Vue component, a watermark (HTML/CSS overlay), a meta tag, a tracking pixel, a Dockerfile, a shell/bash script, a webhook payload, a cron expression, a terraform config, or any other piece of programming source — the deliverable MUST be a single code file with the correct extension (`.html`, `.css`, `.js`, `.ts`, `.tsx`, `.jsx`, `.py`, `.sh`, `.sql`, `.json`, `.yaml`, `.xml`, `.tf`, etc.). Tell the agent to use `write_artifact` with the matching extension and the body as plain source code (NOT markdown wrapped, NOT base64). Explicitly say "do NOT generate an image — output source code only" so the loop never mis-routes to `generate_image`. Add 2-3 concrete acceptance criteria the agent can verify: e.g. "the snippet validates as HTML5", "the CSS uses only `position`, `bottom`, `right`, `opacity`, `pointer-events` properties", "the JS does not depend on any external library", "the regex passes these 3 test strings: …".
   • **SOCIAL-POSTS RECIPE**: if the input asks the agent to "generate / draft / write / create / queue up N posts / tweets / IG posts / LinkedIn posts / Facebook posts / threads / reels captions / TikTok captions" — or any other variant where the deliverable is a batch of social-media posts — the deliverable MUST be DRAFTS IN THE SOCIAL QUEUE, NOT a PDF or a .md file. Tell the agent to use the `draft_to_social_queue` tool (NOT `write_artifact`). The goal text should name the platforms explicitly, the count, and the theme. Example phrasing: "Use `draft_to_social_queue` with platforms=['instagram'], count=3, and a prompt that captures the brand voice for the spring drop announcement. Each draft will land in the Social Queue → Drafts tab ready for the user to click Schedule on. Do NOT save these posts as a .pdf or .md — that's the wrong deliverable type and the user will have to re-draft them by hand."
3. Spell out 3-5 measurable success criteria — concrete things the agent can verify it did (e.g. "cite at least 5 distinct sources with URLs", "include a comparison table with ≥4 rows", "include current pricing for each app").
4. If the work involves research, demand **breadth**: "cite ≥5 distinct sources" or "browse the official site of each of the top 5 competitors". Foundry's research-breadth gate refuses to finish a research mission with <5 sources, so this aligns with the engine.
5. If the work involves visiting a specific site, include the exact URL.
6. Strip vague qualifiers: "comprehensive", "thorough", "in-depth", "robust", "world-class", "robust analysis". Keep only quantifiable specifics.
7. Strip empty-promise verbs that imply background work: "monitor", "keep an eye on", "stay on top of". Missions are single autonomous runs, not subscriptions.
8. Never include sensitive data (API keys, passwords, full card numbers) even if it appears in the rough input — silently drop it.
9. End with one short "Definition of done" sentence so the agent knows when to call `finish`.

TONE
- Direct, imperative, no fluff. The reader is an autonomous agent, not a teammate.
- No politeness ("please", "kindly", "would you").
- Present tense.

REFINEMENT MODE
- If a `refine_instruction` is present, the input `goal` is your existing draft. Apply the refinement (e.g. "make it shorter", "add a pricing focus") while still following ALL rules above. Return the updated goal in the same JSON shape.
"""


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
        # ChatGPT-style remote-browser control. True while a user has
        # taken over the live Chromium stream (agent loop pauses).
        "user_controls_browser": bool(m.get("user_controls_browser", False)),
        # Login-wall auto-pause (B3): set to the eTLD+1 (e.g. "google.com")
        # when the agent's last browser action landed on a login page.
        # The frontend renders a "Login required for X — Take control"
        # chip when this is non-empty.
        "needs_login_for": m.get("needs_login_for", "") or "",
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
        # Remote-browser control hand-off marker (user|agent|None) so the
        # work log can render a clear "you took control" / "agent resumed"
        # row.
        "control": e.get("control") or None,
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


async def _synthesize_brief_artifact(
    db, *, mission_id: str, mission: dict, summary_text: str, iteration: int,
) -> None:
    """Build a Mission Brief markdown deliverable from the mission's
    final summary and persist it the same way an [[ARTIFACT]] block would
    be — i.e. into the on-disk docs folder, GridFS, the ``documents``
    collection (so it shows up in the org Documents library) AND the
    mission's own ``artifacts`` array (so it shows up in the Mission
    Detail page's Deliverables card).

    Lazy-imports ``save_artifacts`` to avoid a circular dependency at
    module load time.
    """
    from artifacts import save_artifacts as _save_arts  # local import

    org_id = mission.get("org_id") or ""
    if not org_id:
        return
    goal = (mission.get("goal") or "").strip()
    short_goal = goal.split("\n")[0][:80] or "Mission Brief"
    docs_root = Path(os.environ.get("DOCS_DIR", "/app/backend/docs"))
    title = f"Mission Brief — {short_goal}"
    md = (
        f"# {title}\n\n"
        f"**Lead agent:** {mission.get('lead_agent_name') or mission.get('lead_agent_id') or 'Mission lead'}\n\n"
        f"**Goal:** {goal or '(no goal recorded)'}\n\n"
        f"---\n\n"
        f"## Outcome\n\n{summary_text}\n"
    )
    block = {
        "name": f"mission-brief-{mission_id[:8]}.md",
        "ext": "md",
        "mime": "text/markdown",
        "title": title,
        "content": md,
    }
    try:
        attachments = await _save_arts(
            db, [block],
            docs_root=docs_root,
            org_id=org_id,
            conversation_id=mission_id,   # the mission acts as the "conv"
            message_id=mission_id,
            agent_id=mission.get("lead_agent_id") or "",
            agent_name=mission.get("lead_agent_name") or "Mission lead",
            created_by=mission.get("created_by") or "",
        )
    except Exception as exc:
        logger.warning("save_artifacts failed for brief: %s", exc)
        return
    if not attachments:
        return
    a = attachments[0]
    artifact = {
        "kind": "document",
        "filename": a.get("filename"),
        "url": a.get("url"),
        "mime": a.get("mime"),
        "original_name": a.get("original_name"),
        "size": a.get("size"),
        "title": title,
        "auto_synthesized": True,
    }
    await db.missions.update_one(
        {"id": mission_id},
        {"$push": {"artifacts": artifact}},
    )
    await _append_event(db, mission_id, kind="artifact",
                        text=f"Auto-saved {a.get('original_name')}",
                        artifact=artifact, iteration=iteration)


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

    # Tag every LLM call this loop makes with mission/org/user metadata
    # so the admin dashboard can attribute spend correctly.
    from llm_logging import with_call_context, reset_call_context  # noqa: E402
    _ctx_token = with_call_context(
        org_id=ctx.get("org_id"),
        mission_id=mission_id,
        user_id=ctx.get("created_by"),
        agent_id=ctx.get("lead_agent_id"),
        kind_label="mission_lead",
    )

    tool_map = {t.name: t for t in tools}
    # Inject any Composio tools the user has actively connected, gated by
    # the lead agent's tools_enabled toggle. Lives here (not at the call
    # sites) so all five mission entry points — start, resume, force-finish,
    # continue, rehydrate — get the same treatment with no duplication.
    if ctx.get("org_id") and ctx.get("created_by"):
        try:
            extra = await _augment_with_composio_tools(
                db, [],
                org_id=ctx["org_id"],
                user_id=ctx["created_by"],
                mission_id=mission_id,
                lead_agent_id=ctx.get("lead_agent_id"),
            )
            if extra:
                tools = list(tools) + extra
                tool_map = {t.name: t for t in tools}
                logger.info(
                    "mission %s: +%d Composio tools (lead=%s)",
                    mission_id, len(extra), ctx.get("lead_agent_id"),
                )
        except Exception as exc:
            logger.warning("mission %s: Composio injection skipped: %s", mission_id, exc)
    catalog = build_tool_catalog(tools)
    sys_prompt = build_lead_system_prompt(catalog)

    iteration = 0

    # ── Mission-level heartbeat ──────────────────────────────────────
    # A single background task that ticks `last_activity_at` every 30s
    # while the mission is running. Lives for the entire lifetime of
    # `run_mission_loop`, surviving slow LLM round-trips, lengthy
    # browse_url calls (Google Maps interactions can take 60-90s), and
    # artifact synthesis. Failures are silent — this is best-effort
    # liveness reporting, not the source of truth.
    heartbeat_stop = asyncio.Event()

    async def _mission_heartbeat():
        try:
            while not heartbeat_stop.is_set():
                try:
                    await asyncio.wait_for(heartbeat_stop.wait(), timeout=30)
                except asyncio.TimeoutError:
                    try:
                        await db.missions.update_one(
                            {"id": mission_id, "status": "running"},
                            {"$set": {
                                "last_activity_at": datetime.now(timezone.utc),
                            }},
                        )
                    except Exception:
                        pass
        except asyncio.CancelledError:
            return

    heartbeat_task = asyncio.create_task(_mission_heartbeat())

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
            # User has taken over the browser (ChatGPT-style agent mode).
            # Hold the loop in place until they give control back so the
            # agent isn't clicking while the human is typing a password.
            # We poll every 2s instead of the long LLM call; when control
            # is released, resume the next iteration normally.
            if m.get("user_controls_browser"):
                await asyncio.sleep(2)
                continue

            iteration += 1
            await db.missions.update_one(
                {"id": mission_id},
                {"$set": {
                    "iteration_count": iteration,
                    # Heartbeat — keeps `last_activity_at` fresh while
                    # the LLM call is in flight (a slow 60-90s response
                    # would otherwise look "stalled" to the UI watchdog
                    # AND to the startup-recovery scan).
                    "last_activity_at": datetime.now(timezone.utc),
                }},
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
                # ── Research-breadth gate ───────────────────────────────
                # If the goal demands broad research, refuse to finish
                # until we've actually browsed multiple distinct sources.
                # Bypass when the goal could not be researched (`FAILED:`
                # / network errors) or when finishing as an `ask_user`
                # outcome rather than a real deliverable.
                mission_row = await db.missions.find_one(
                    {"id": mission_id},
                    {"goal": 1, "research_gate_warned": 1,
                     "dataset_gate_warned": 1, "artifacts": 1},
                )
                goal = (mission_row or {}).get("goal", "")
                already_warned = bool((mission_row or {}).get("research_gate_warned"))
                dataset_warn_count = int((mission_row or {}).get("dataset_gate_warn_count") or 0)
                summary_is_failure = summary.upper().startswith("FAILED:")
                if (
                    _is_research_goal(goal)
                    and not summary_is_failure
                    and not already_warned
                ):
                    n_domains = _count_distinct_browsed_domains(history)
                    if n_domains < RESEARCH_MIN_DISTINCT_DOMAINS:
                        needed = RESEARCH_MIN_DISTINCT_DOMAINS - n_domains
                        nudge = (
                            f"FINISH BLOCKED — research breadth not yet met. "
                            f"This goal looks like research / multi-source "
                            f"analysis, but you've only browsed {n_domains} "
                            f"distinct domain(s). A credible research artifact "
                            f"requires at least {RESEARCH_MIN_DISTINCT_DOMAINS} "
                            f"distinct sources (different domains). "
                            f"Go gather {needed} more — call web_search with "
                            f"a fresh angle (try alternative query phrasings: "
                            f"government stats, industry reports, trade pubs, "
                            f"practitioner forums), then browse_url 2-4 of "
                            f"the most relevant NEW results that aren't yet "
                            f"in your history. Update the artifact to cite "
                            f"each source inline with its URL. Then call "
                            f"finish again."
                        )
                        await _append_event(
                            db, mission_id, kind="system",
                            text=nudge, iteration=iteration,
                        )
                        # Stamp so we don't loop forever — the next finish
                        # call goes through even if breadth still short.
                        await db.missions.update_one(
                            {"id": mission_id},
                            {"$set": {"research_gate_warned": True}},
                        )
                        continue  # back into the loop, don't actually finish
                # ── Dataset row-count gate (multi-attempt) ──────────────
                # For list/harvest/lead-hunt missions, refuse to finish
                # until the CSV artifact has at least DATASET_MIN_ROWS
                # data rows. This is the fix for the Feb 2026 production
                # bug where multiple "harvest businesses across 5 metros"
                # missions shipped a CSV with 0-1 rows. Fires up to
                # DATASET_GATE_MAX_NUDGES times with escalating urgency —
                # one nudge wasn't enough (agent often ignored it and
                # called finish again, which the old one-shot gate let
                # through). After the cap, finish is allowed but the
                # mission is flagged `partial` so the user knows.
                if (
                    _is_dataset_goal(goal)
                    and not summary_is_failure
                ):
                    row_count = _count_csv_data_rows(
                        (mission_row or {}).get("artifacts") or [],
                    )
                    if row_count < DATASET_MIN_ROWS and dataset_warn_count < DATASET_GATE_MAX_NUDGES:
                        needed = DATASET_MIN_ROWS - row_count
                        attempt_n = dataset_warn_count + 1
                        urgency_prefix = {
                            1: "FINISH BLOCKED",
                            2: "FINISH BLOCKED — SECOND WARNING",
                            3: "FINISH BLOCKED — FINAL WARNING (this is your last chance — after this finish goes through but the mission will be marked PARTIAL)",
                        }.get(attempt_n, "FINISH BLOCKED")
                        nudge = (
                            f"{urgency_prefix} — dataset is too thin "
                            f"(attempt {attempt_n} of {DATASET_GATE_MAX_NUDGES}). "
                            f"CSV currently has {row_count} data row(s); "
                            f"target {DATASET_MIN_ROWS}+. Need {needed} more.\n\n"
                            f"STOP RE-CALLING THE SAME BLOCKED SCRAPERS. "
                            f"Chamber of Commerce, BBB, Yelp, Google Maps, "
                            f"and LinkedIn are protected by Cloudflare / "
                            f"login walls — they will return empty or "
                            f"403 to web_scrape. SKIP THEM. Use the "
                            f"JSON-API tools instead, which work reliably:\n"
                            f"  • `sec_edgar_search(query='...', state='AZ')` "
                            f"— companies that filed with the SEC (public "
                            f"companies + their subsidiaries; Foundry's "
                            f"target B2B segment).\n"
                            f"  • `usaspending_recipients(state='AZ', "
                            f"naics_keyword='healthcare')` — federal "
                            f"contractors / grantees with revenue + "
                            f"address pre-verified.\n"
                            f"  • `wikipedia_companies_list(query='Phoenix, "
                            f"Arizona')` — Wikipedia 'List of companies "
                            f"based in X' pages, instantly scrapable.\n"
                            f"  • `opencorporates_search(jurisdiction='us_az', "
                            f"q='consulting')` — registered businesses by "
                            f"state.\n"
                            f"After harvesting, ALWAYS push rows via "
                            f"`csv_append_rows` — DO NOT use write_artifact "
                            f"for the CSV. DO NOT fabricate phone numbers, "
                            f"emails, or revenue figures — leave blank if "
                            f"unknown."
                        )
                        await _append_event(
                            db, mission_id, kind="system",
                            text=nudge, iteration=iteration,
                        )
                        await db.missions.update_one(
                            {"id": mission_id},
                            {"$set": {"dataset_gate_warn_count": attempt_n}},
                        )
                        continue
                    if row_count < DATASET_MIN_ROWS:
                        # Cap reached → let finish go through but mark
                        # the mission `partial` so the user sees a clear
                        # status badge instead of a misleading "success".
                        await db.missions.update_one(
                            {"id": mission_id},
                            {"$set": {"partial_reason": (
                                f"Dataset goal under target: shipped "
                                f"{row_count} rows, target {DATASET_MIN_ROWS}+. "
                                f"Many public sources blocked scraping."
                            )}},
                        )
                await _append_event(db, mission_id, kind="finish",
                                    text=summary, iteration=iteration)
                # If the mission finishes WITHOUT producing any binary
                # deliverables (no [[ARTIFACT]] blocks), auto-synthesize a
                # "Mission Brief — {goal}.md" so the user always has at
                # least one tangible result that lands in BOTH the
                # mission's Deliverables card AND the org Documents
                # library. Without this, missions that produced only a
                # summary text would leave the user confused: "the run
                # finished but I see nothing to download."
                try:
                    cur = await db.missions.find_one(
                        {"id": mission_id}, {"artifacts": 1, "goal": 1, "org_id": 1, "lead_agent_name": 1, "lead_agent_id": 1}
                    )
                    has_artifacts = bool((cur or {}).get("artifacts"))
                    has_meaningful_summary = len(summary) >= 60
                    if not has_artifacts and has_meaningful_summary:
                        await _synthesize_brief_artifact(
                            db, mission_id=mission_id,
                            mission=cur or {}, summary_text=summary,
                            iteration=iteration,
                        )
                except Exception as exc:
                    logger.warning("Brief auto-synthesis failed: %s", exc)
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
                # ── Hard guards against ask_user abuse ─────────────────
                # Production bug Feb 2026: agents kept re-asking the same
                # clarifying question after the user had answered "I have
                # nothing more to add / proceed / finish" multiple times.
                # Two intercepts:
                #   (a) the user has explicitly declined to add more info
                #       (a flag set by the `answer` endpoint),
                #   (b) the per-mission ask_user count has hit
                #       MAX_ASK_USER_PER_MISSION.
                # In either case we DROP the ask_user, append a system
                # event telling the agent to proceed, and continue the
                # loop so the next iteration must call a real tool or
                # `finish` instead.
                fresh = await db.missions.find_one({"id": mission_id})
                declined = bool((fresh or {}).get("user_declined_more_info"))
                ask_count = int((fresh or {}).get("ask_user_count") or 0)
                if declined or ask_count >= MAX_ASK_USER_PER_MISSION:
                    reason = (
                        "The user has explicitly told you to proceed "
                        "without more info."
                        if declined else
                        f"You have already asked the user "
                        f"{ask_count} times this mission "
                        f"(cap is {MAX_ASK_USER_PER_MISSION})."
                    )
                    block_msg = (
                        f"BLOCKED ask_user — {reason} DO NOT call "
                        "ask_user again. On the next iteration you MUST "
                        "either (a) use web_search / browse_url / "
                        "another tool to find what you can from public "
                        "sources, or (b) call finish with a truthful "
                        "deliverable that documents the gap (\"based on "
                        "public sources X and Y; specific data on Z was "
                        "not available\"). Calling ask_user again is "
                        "forbidden."
                    )
                    await _append_event(
                        db, mission_id, kind="system",
                        text=block_msg, iteration=iteration,
                    )
                    # Heartbeat so the UI doesn't think we stalled.
                    await db.missions.update_one(
                        {"id": mission_id},
                        {"$set": {
                            "last_activity_at": datetime.now(timezone.utc),
                        }},
                    )
                    continue
                await _append_event(db, mission_id, kind="question",
                                    text=question, iteration=iteration)
                await db.missions.update_one(
                    {"id": mission_id},
                    {
                        "$set": {
                            "status": "awaiting_user",
                            "question": question,
                        },
                        "$inc": {"ask_user_count": 1},
                    },
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
            #
            # For browser tools we go further: match on `url` alone, NOT
            # the full args dict. Otherwise an agent that just timed out
            # on `browse_url(url=X)` can slip past by retrying with
            # `browse_url(url=X, timeout=30)` (different args ⇒ no
            # dedupe hit) — which is exactly what we observed in the
            # maradynehp.com mission.
            if tool_name in _BROWSER_TOOL_NAMES and (args or {}).get("url"):
                target_url = str((args or {}).get("url"))
                prior_identical = [
                    e for e in history
                    if e.get("kind") == "tool_call"
                    and e.get("tool") == tool_name
                    and str((e.get("args") or {}).get("url") or "") == target_url
                ]
            else:
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
                # Browser tools get a longer timeout because anti-bot
                # walls eat 30-90s before the page actually paints.
                tool_timeout = (
                    BROWSER_TOOL_TIMEOUT_S
                    if tool_name in _BROWSER_TOOL_NAMES
                    else TOOL_CALL_TIMEOUT_S
                )
                result = await asyncio.wait_for(
                    t(ctx, args), timeout=tool_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "mission %s tool %s timed out after %ds (iter %d)",
                    mission_id, tool_name, tool_timeout, iteration,
                )
                # Tailor the recovery hint to the tool that just timed
                # out. Browser tools should NOT be retried with the same
                # URL — the issue is anti-bot or the site is genuinely
                # slow, and a re-fetch will fail again. Push the agent
                # toward `web_search` (research) or `write_artifact` /
                # `finish` (use what you have) instead.
                if tool_name in _BROWSER_TOOL_NAMES:
                    hint = (
                        f"Tool '{tool_name}' could not load that URL within "
                        f"{tool_timeout}s — the site is either slow, "
                        f"behind heavy anti-bot protection, or down. DO NOT "
                        f"retry the same URL. Your next action MUST be one of: "
                        f"(a) call `web_search` to gather information about "
                        f"the site/business from external sources, (b) call "
                        f"`write_artifact` with what you already know plus "
                        f"public knowledge, or (c) call `finish` with a clear "
                        f"explanation that the source URL was unreachable."
                    )
                else:
                    hint = (
                        f"Tool '{tool_name}' exceeded {tool_timeout}s and "
                        f"was aborted. Try a different tool, simplify the "
                        f"args (e.g. shorter prompt), or call finish."
                    )
                result = {"error": hint}
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

            # ── B3: Login-wall auto-pause ────────────────────────────
            # If the just-completed browser tool landed on a login page,
            # transparently flip the mission into "needs login" state so
            # the UI shows a clear "Login required — click Take control"
            # chip and the next iteration's loop check pauses the agent.
            if (
                isinstance(result, dict)
                and tool_name in {"browse_url", "browse_click", "browse_fill",
                                  "browse_submit", "screenshot_url"}
            ):
                final_url = result.get("final_url") or ""
                title = result.get("title") or ""
                text = result.get("text") or result.get("text_preview") or ""
                try:
                    from credential_vault import looks_like_login_page, host_to_etld_plus_one
                    if final_url and looks_like_login_page(final_url, title, text):
                        host = host_to_etld_plus_one(
                            final_url.split("://", 1)[-1].split("/", 1)[0]
                        )
                        await db.missions.update_one(
                            {"id": mission_id},
                            {"$set": {
                                "needs_login_for": host,
                                "user_controls_browser": True,
                            }},
                        )
                        await _append_event(
                            db, mission_id, kind="system",
                            text=(
                                f"Login required for {host}. Pausing the "
                                "agent — open the live browser and click "
                                "**Take control** to sign in. The loop "
                                "will resume automatically when you hand "
                                "control back, and your session will be "
                                "saved for next time."
                            ),
                            iteration=iteration,
                        )
                        # Inject a synthetic last-tool-result so the LLM
                        # (when it eventually resumes) understands what
                        # happened.
                        await _append_event(
                            db, mission_id, kind="system",
                            text=(
                                "Once login completes the page should "
                                "advance past the auth wall — re-run the "
                                "same browse_url to continue."
                            ),
                            iteration=iteration,
                        )
                except Exception as exc:
                    logger.debug("Login-wall check failed: %s", exc)

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
        # Stop the mission-level heartbeat first so it doesn't keep
        # ticking against a finished mission row.
        try:
            heartbeat_stop.set()
            heartbeat_task.cancel()
        except Exception:
            pass
        # Release the sticky browser context (if any) so the Chromium
        # process doesn't accumulate one zombie context per mission.
        # Best-effort — losing the cleanup doesn't affect mission state.
        try:
            from browser_tools import cleanup_mission_page
            await cleanup_mission_page(mission_id)
        except Exception as exc:
            logger.warning("mission %s browser cleanup failed: %s", mission_id, exc)
        # Purge the encrypted storage_state row now the mission is done —
        # no need to retain cookies for missions that won't run again.
        try:
            from mission_browser_state import purge_mission_state
            await purge_mission_state(db, mission_id=mission_id)
        except Exception:
            pass
        try:
            reset_call_context(_ctx_token)
        except Exception:
            pass


async def _fail_mission(db, mission_id: str, err: str) -> None:
    await db.missions.update_one(
        {"id": mission_id},
        {"$set": {
            "status": "failed",
            "error": err,
            "finished_at": datetime.now(timezone.utc),
        }},
    )
    # Fire an error email so the team can triage failed missions. The
    # rate-limiter in error_reporter folds identical fingerprints to
    # once per 30 min, so a runaway buggy mission won't flood the inbox.
    try:
        m = await db.missions.find_one(
            {"id": mission_id},
            {"goal": 1, "org_id": 1, "created_by": 1, "lead_agent": 1},
        )
        if m:
            from error_reporter import report_error_async as _report
            user_email = None
            try:
                u_doc = await db.users.find_one(
                    {"id": m.get("created_by")}, {"email": 1}
                )
                user_email = (u_doc or {}).get("email")
            except Exception:
                pass
            await _report(
                kind="mission",
                org_id=str(m.get("org_id") or ""),
                user_email=user_email,
                user_id=str(m.get("created_by") or ""),
                message=err,
                extra={
                    "mission_id": mission_id,
                    "lead_agent": str(m.get("lead_agent") or ""),
                    "goal": (m.get("goal") or "")[:300],
                },
                link=f"/app/missions/{mission_id}",
            )
    except Exception as exc:
        logger.warning("error_reporter notify failed for mission %s: %s", mission_id, exc)


# ── Tools ──────────────────────────────────────────────────────────────────
def build_tools(
    *, db, docs_root: Path, UPLOAD_DIR: Path, AGENTS: dict,
    brave_search, perplexity_web_search, perplexity_available,
    generate_image_for, save_artifacts,
    LlmChat=None, UserMessage=None, EMERGENT_LLM_KEY: str = "",
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

        # Routing: Brave is a better general-purpose index (evergreen
        # research, long-tail queries). Perplexity Sonar is better for
        # time-sensitive queries — it's optimized for real-time synthesis
        # with citations rather than a raw result list. So:
        #
        #   - Time-sensitive  (freshness in pd/pw/pm): Perplexity FIRST,
        #     Brave as fallback
        #   - Evergreen (no freshness signal):         Brave FIRST,
        #     Perplexity as fallback
        #
        # Either way we get real-time internet access on every mission
        # search call — just via the engine that handles that kind of
        # query best.
        time_sensitive = freshness in ("pd", "pw", "pm")
        results = []
        primary = "perplexity" if (time_sensitive and perplexity_available()) else "brave"
        if primary == "perplexity":
            try:
                results = await perplexity_web_search(
                    q, count=5, freshness=freshness or None,
                ) or []
            except Exception:
                results = []
            if not results:
                try:
                    results = await brave_search(q, count=5, freshness=freshness or None) or []
                except Exception:
                    results = []
        else:
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
        # NOTE: `.pdf` and `.docx` are NOT rejected — the artifact pipeline
        # transcodes their Markdown bodies into real binary documents via
        # artifact_synthesis.synthesize().
        from artifacts import _safe_filename, DEFAULT_MIME, BINARY_EXTS, SYNTHESIZED_BINARY_EXTS
        req_ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if req_ext in BINARY_EXTS and req_ext not in SYNTHESIZED_BINARY_EXTS:
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

    async def generate_video(ctx, args):
        """Generate a video clip via Kie.ai (Veo 3.1 Fast by default).
        Saves the resulting MP4 as a Documents row + mission artifact.

        For other video models (Kling, Wan, Seedance, Sora 2, Runway
        Aleph), pass ``model`` explicitly. Use this — NOT generate_image
        — whenever the user wants motion / animation / a clip.
        """
        from kie_client import (
            generate_video as _veo_generate, generate_via_kie as _kie_generate,
            kie_provider_info,
        )
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return {"error": "prompt is required"}
        model = (args.get("model") or "veo3_fast").strip()
        aspect = (args.get("aspect_ratio") or "16:9").strip()
        # Veo legacy path
        if model in ("veo3_fast", "veo3", "veo3_lite"):
            video_url = await _veo_generate(
                prompt=prompt, aspect_ratio=aspect, model=model,
            )
            urls = [video_url] if video_url else []
        else:
            info = kie_provider_info(model)
            if not info or info["type"] != "video":
                return {"error": f"Unknown video model `{model}`."}
            urls = await _kie_generate(model_key=model, prompt=prompt)
        if not urls:
            return {"error": "Video generation failed or timed out — try again or pick a different model."}
        # Persist to Documents + mission artifacts
        artifacts = []
        for u in urls:
            try:
                async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                    resp = await client.get(u)
                if resp.status_code != 200:
                    continue
                blob = resp.content
            except Exception as exc:
                logger.warning("Video download failed: %s", exc)
                continue
            atts = await save_artifacts(
                db, [{
                    "name": f"video-{uuid.uuid4().hex[:8]}.mp4",
                    "ext": "mp4", "mime": "video/mp4",
                    "title": f"Video — {prompt[:60]}",
                    "content": "", "_raw_bytes": blob,
                }],
                docs_root=docs_root,
                org_id=ctx["org_id"],
                conversation_id=ctx.get("mission_id", ""),
                message_id=ctx.get("mission_id", ""),
                agent_id=ctx.get("lead_agent_id", ""),
                agent_name=ctx.get("lead_agent_name", "Mission"),
                created_by=ctx.get("created_by", ""),
            )
            if atts:
                a = atts[0]
                art = {
                    "kind": "video",
                    "filename": a.get("filename"),
                    "url": a.get("url"),
                    "mime": "video/mp4",
                    "original_name": a.get("original_name"),
                    "size": a.get("size"),
                    "title": f"Video — {prompt[:60]}",
                    "model": model,
                    "document_id": a.get("document_id"),
                }
                artifacts.append(art)
                await db.missions.update_one(
                    {"id": ctx.get("mission_id")},
                    {"$push": {"artifacts": art}},
                )
        if not artifacts:
            return {"error": "Generated but couldn't save the video file."}
        return {"ok": True, "produced": len(artifacts), "artifacts": artifacts,
                "summary": f"Generated {len(artifacts)} video clip(s) with {model}."}

    async def generate_music(ctx, args):
        """Generate a music track via Kie.ai Suno. Args: ``prompt``
        (required — describe the song), ``style`` (optional — e.g.
        'cinematic', 'lo-fi hip hop'), ``instrumental`` (default false).
        Saves the MP3 as a Documents row + mission artifact.
        """
        from kie_client import generate_via_kie as _kie_generate
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return {"error": "prompt is required"}
        extra = {}
        if args.get("style"):     extra["style"] = str(args["style"])
        if args.get("instrumental"): extra["instrumental"] = bool(args["instrumental"])
        urls = await _kie_generate(model_key="suno", prompt=prompt, extra=extra)
        if not urls:
            return {"error": "Music generation failed or timed out — try again."}
        artifacts = []
        for u in urls:
            try:
                async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                    resp = await client.get(u)
                if resp.status_code != 200:
                    continue
                blob = resp.content
            except Exception as exc:
                logger.warning("Music download failed: %s", exc)
                continue
            atts = await save_artifacts(
                db, [{
                    "name": f"track-{uuid.uuid4().hex[:8]}.mp3",
                    "ext": "mp3", "mime": "audio/mpeg",
                    "title": f"Music — {prompt[:60]}",
                    "content": "", "_raw_bytes": blob,
                }],
                docs_root=docs_root,
                org_id=ctx["org_id"],
                conversation_id=ctx.get("mission_id", ""),
                message_id=ctx.get("mission_id", ""),
                agent_id=ctx.get("lead_agent_id", ""),
                agent_name=ctx.get("lead_agent_name", "Mission"),
                created_by=ctx.get("created_by", ""),
            )
            if atts:
                a = atts[0]
                art = {
                    "kind": "audio",
                    "filename": a.get("filename"),
                    "url": a.get("url"),
                    "mime": "audio/mpeg",
                    "original_name": a.get("original_name"),
                    "size": a.get("size"),
                    "title": f"Music — {prompt[:60]}",
                    "model": "suno",
                    "document_id": a.get("document_id"),
                }
                artifacts.append(art)
                await db.missions.update_one(
                    {"id": ctx.get("mission_id")},
                    {"$push": {"artifacts": art}},
                )
        if not artifacts:
            return {"error": "Generated but couldn't save the audio file."}
        return {"ok": True, "produced": len(artifacts), "artifacts": artifacts,
                "summary": f"Composed {len(artifacts)} track(s) with Suno."}

    async def resize_image(ctx, args):
        """Resize ONE source image into MANY exact pixel sizes.

        Built specifically for the "recreate this image at six ad sizes"
        workflow that pure image-gen models can't fulfil (they output
        approximate aspect ratios, never exact pixel dimensions). PIL is
        deterministic, fast, and lossless — perfect for ad-tech.

        Args (all required unless noted):
          source: Either an attachment_id (preferred — references a file
            the user uploaded with the mission) OR a document_id (for
            re-processing an artifact this mission already produced) OR
            a path-like url such as "/api/files/<filename>" / "/api/files/docs/...".
          sizes: List of {label, width, height, mode?}. ``mode`` is one of
            "fit" (default — scale + pad), "fill" (cover + crop),
            "stretch" (distort to exact). For ad slots stick with "fit".
          base_name: optional filename stem for output files. Defaults
            to "image".

        Each variant is saved as its own Documents row (so the user can
        download each one individually) AND emitted as a mission artifact.
        """
        from image_resize import smart_resize_batch  # local import
        sizes = args.get("sizes") or []
        if not isinstance(sizes, list) or not sizes:
            return {"error": "Pass `sizes` as a non-empty list of {label, width, height, mode?}."}
        if len(sizes) > 24:
            return {"error": "Too many sizes — cap is 24 per call. Make multiple calls if needed."}

        # Resolve the source — try attachment first, then document, then URL.
        src_bytes: Optional[bytes] = None
        src_label = ""
        att_id = (args.get("attachment_id") or "").strip()
        doc_id = (args.get("document_id") or "").strip()
        source = (args.get("source") or "").strip()
        # 1. Mission attachment (the most common case for "uploaded image")
        mission_id = ctx.get("mission_id") or ""
        if mission_id and (att_id or (source and not source.startswith("/"))):
            mission = await db.missions.find_one({"id": mission_id}, {"_id": 0, "attachments": 1})
            for a in (mission or {}).get("attachments") or []:
                if a.get("id") == att_id or (
                    not att_id and source and source in (a.get("original_name") or "")
                ):
                    fp = UPLOAD_DIR / (a.get("filename") or "")
                    if fp.is_file():
                        src_bytes = fp.read_bytes()
                        src_label = a.get("original_name") or fp.name
                        break
        # 2. Org documents library
        if src_bytes is None and doc_id:
            d = await db.documents.find_one({"id": doc_id, "org_id": ctx["org_id"]})
            if d:
                fp = docs_root / d["org_id"] / d["filename"]
                if fp.is_file():
                    src_bytes = fp.read_bytes()
                    src_label = d.get("original_name") or fp.name
        # 3. URL/path  fallback (/api/files/... or /api/files/docs/<org>/<file>)
        if src_bytes is None and source:
            try:
                if "/files/docs/" in source:
                    parts = source.split("/files/docs/", 1)[1].split("/", 1)
                    if len(parts) == 2:
                        fp = docs_root / parts[0] / parts[1]
                        if fp.is_file():
                            src_bytes = fp.read_bytes()
                            src_label = fp.name
                elif "/files/" in source:
                    fname = source.rsplit("/", 1)[-1]
                    fp = UPLOAD_DIR / fname
                    if fp.is_file():
                        src_bytes = fp.read_bytes()
                        src_label = fname
            except Exception as exc:
                logger.warning("resize_image source lookup failed for %s: %s", source, exc)

        if src_bytes is None:
            return {
                "error": (
                    "Couldn't load source image. Pass `attachment_id` for a "
                    "file the user uploaded with the mission (call read_attachment "
                    "with no args first to list them), or `document_id` for "
                    "an artifact this mission generated, or `source` set to "
                    "a /api/files/... url."
                )
            }

        base = (args.get("base_name") or src_label.rsplit(".", 1)[0] or "image")
        try:
            variants = smart_resize_batch(src_bytes, sizes, base_name=base)
        except Exception as exc:
            return {"error": f"Resize batch failed: {exc}"[:300]}

        # Persist each variant as its own document + mission artifact.
        ok_results: list[dict] = []
        errors: list[dict] = []
        for v in variants:
            if v.get("error"):
                errors.append({"label": v.get("label"), "error": v["error"]})
                continue
            try:
                attachments = await save_artifacts(
                    db, [{
                        "name": v["filename"],
                        "ext": "png",
                        "mime": "image/png",
                        "title": f"{base} — {v['label']} ({v['width']}×{v['height']})",
                        "content": "",  # raw bytes path bypasses content
                        "_raw_bytes": v["bytes"],
                    }],
                    docs_root=docs_root,
                    org_id=ctx["org_id"],
                    conversation_id=mission_id,
                    message_id=mission_id,
                    agent_id=ctx.get("lead_agent_id", ""),
                    agent_name=ctx.get("lead_agent_name", "Mission"),
                    created_by=ctx.get("created_by", ""),
                )
            except Exception as exc:
                logger.warning("save_artifacts failed for variant %s: %s", v["label"], exc)
                errors.append({"label": v["label"], "error": str(exc)[:200]})
                continue
            if not attachments:
                errors.append({"label": v["label"], "error": "Persist returned empty"})
                continue
            a = attachments[0]
            artifact = {
                "kind": "image",
                "filename": a.get("filename"),
                "url": a.get("url"),
                "mime": "image/png",
                "original_name": a.get("original_name"),
                "size": a.get("size"),
                "width": v["width"],
                "height": v["height"],
                "label": v["label"],
                "title": f"{v['label']} ({v['width']}×{v['height']})",
                "document_id": a.get("document_id"),
            }
            ok_results.append(artifact)
            # Push directly so the mission detail page sees them as they arrive.
            await db.missions.update_one(
                {"id": mission_id},
                {"$push": {"artifacts": artifact}},
            )

        return {
            "ok": True,
            "produced": len(ok_results),
            "failed": len(errors),
            "variants": ok_results,
            "errors": errors,
            "summary": (
                f"Generated {len(ok_results)} image variant(s) "
                f"from '{src_label}' at the requested sizes."
                + (f" {len(errors)} failed." if errors else "")
            ),
        }

    async def draft_to_social_queue(ctx, args):
        """Drop one or more drafts straight into the org's Social Queue.

        This is the path the mission engine takes when the user says
        "create 3 IG posts about X". Returns the live serialized post
        ids so the mission's summary can link them. Replaces the older
        write-to-PDF fallback which was the source of the
        "I asked for IG posts and got a document" bug.

        Args:
            prompt: required — what the posts should be about
            platforms: list[str] — one or more of twitter, linkedin,
                facebook, instagram, threads, bluesky, mastodon, reddit,
                tiktok, youtube, mailchimp
            count: int — how many drafts to generate (max 10)
            agent_id: optional — which agent persona drafts (defaults
                to the mission's lead_agent_id)
            auto_image: bool — whether to also kick off Nano Banana
                cover-image generation in background (default True)
        """
        if not LlmChat or not UserMessage or not EMERGENT_LLM_KEY:
            return {"error": "Social draft tooling not wired into this mission runtime."}

        from social_posts import _draft_via_agent, serialize_social_post
        from bson import ObjectId

        org_id = ctx.get("org_id") or ""
        team_id = ctx.get("team_id") or ""
        agent_id = (args.get("agent_id") or ctx.get("lead_agent_id") or "").strip()
        if agent_id not in AGENTS:
            # Fall back to a default writing agent if available; many
            # agents can credibly draft a social post.
            for k in AGENTS:
                agent_id = k
                break
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return {"error": "prompt is required"}

        # Normalise the platform list. Accept singular strings, common
        # variations, or a list. Snap to the canonical lowercase slug.
        raw_pf = args.get("platforms") or args.get("platform") or ["instagram"]
        if isinstance(raw_pf, str):
            raw_pf = [raw_pf]
        canon = {
            "x": "twitter", "tw": "twitter", "twitter": "twitter",
            "li": "linkedin", "linkedin": "linkedin",
            "fb": "facebook", "facebook": "facebook",
            "ig": "instagram", "insta": "instagram", "instagram": "instagram",
            "th": "threads", "threads": "threads",
            "bsky": "bluesky", "bluesky": "bluesky",
            "mastodon": "mastodon",
            "reddit": "reddit",
            "tt": "tiktok", "tiktok": "tiktok",
            "yt": "youtube", "youtube": "youtube",
            "mailchimp": "mailchimp",
        }
        platforms: list[str] = []
        for p in raw_pf:
            slug = canon.get(str(p).strip().lower())
            if slug and slug not in platforms:
                platforms.append(slug)
        if not platforms:
            platforms = ["instagram"]

        count = int(args.get("count") or len(platforms) or 1)
        count = max(1, min(count, 10))

        # Pull the org Soul + team Soulprint so the drafts are on-brand.
        org = await db.organizations.find_one({"_id": ObjectId(org_id)}) if org_id else None
        team = await db.teams.find_one({"_id": ObjectId(team_id)}) if team_id else None
        org_soul = (org or {}).get("soul") or {}
        team_soulprint = (team or {}).get("soulprint") or {}
        style_guide_block = ""
        sg = (org or {}).get("style_guide") or {}
        if sg:
            style_guide_block = (
                f"Style guide:\n- Tone: {sg.get('tone','')}\n"
                f"- Image/video style: {sg.get('image_video_style','')}"
            ).strip()

        try:
            drafts = await _draft_via_agent(
                LlmChat=LlmChat, UserMessage=UserMessage,
                EMERGENT_LLM_KEY=EMERGENT_LLM_KEY,
                AGENTS=AGENTS, agent_id=agent_id,
                org_soul=org_soul, team_soulprint=team_soulprint,
                prompt=prompt, platforms=platforms, count=count,
                style_guide_block=style_guide_block,
            )
        except Exception as exc:
            return {"error": f"Drafting failed: {exc}"}

        if not drafts:
            return {"error": "Drafter returned no posts. Try a more specific prompt."}

        # Persist each draft into social_posts. Same shape the live
        # /api/social/draft endpoint produces, so downstream UI (queue
        # cards, scheduler, autopost worker) handles them identically.
        now = datetime.now(timezone.utc)
        created: list[dict] = []
        auto_image = bool(args.get("auto_image", True))
        for d in drafts:
            pf = d.get("platform") or platforms[0]
            doc = {
                "org_id": org_id,
                "team_id": team_id,
                "conversation_id": ctx.get("mission_id", ""),
                "source_message_id": "",
                "agent_id": agent_id,
                "agent_name": AGENTS.get(agent_id, {}).get("name", ""),
                "status": "draft",
                "platforms": [pf],
                "text": d.get("text", ""),
                "hashtags": d.get("hashtags", []),
                "link_url": "",
                "media": [],
                "scheduled_for": None,
                "published_at": None,
                "created_at": now,
                "created_by": ctx.get("created_by", ""),
            }
            res = await db.social_posts.insert_one(doc)
            doc["_id"] = res.inserted_id
            created.append(serialize_social_post(doc))

        # Posts land WITHOUT cover images by default. The user can hit
        # "Generate cover image" on each draft in the Social Queue UI to
        # kick off Nano Banana on demand. (Auto-image-on-create can be
        # wired in a later pass once we extract a reusable cover-gen
        # helper from social_posts.make_social_posts_router.)
        _ = bool(args.get("auto_image", False))  # reserved kwarg

        return {
            "ok": True,
            "drafted": len(created),
            "platforms": platforms,
            "posts": [{"id": c["id"], "platform": c["platforms"][0], "text_preview": c["text"][:120]} for c in created],
            "summary": (
                f"Drafted {len(created)} {('/'.join(platforms))} post(s) into the Social Queue. "
                f"Open the Social tab → Drafts to review and schedule them."
            ),
            "next_step": (
                "User can now click Schedule on each draft to pick a fire "
                "time and toggle auto-publish. Or chain a mission tool call "
                "to set scheduled_for + autopost_enabled on each post id "
                "if a schedule is already known."
            ),
        }

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

    async def web_scrape(ctx, args):
        """Forge Crawl single-page scrape — cheap, returns markdown."""
        url = (args.get("url") or "").strip()
        if not url:
            return {"error": "url is required"}
        render = (args.get("render") or "auto").strip().lower()
        if render not in {"auto", "force", "never"}:
            render = "auto"
        from forge_crawl import scrape as _forge_scrape
        result = await _forge_scrape(url, render=render)
        if not result.get("ok"):
            return {"error": result.get("error") or "scrape failed",
                    "final_url": result.get("final_url") or url,
                    "status": result.get("status") or 0}
        # Trim the LLM-facing payload — same ceiling we use for
        # web_search results, so the tool response stays cheap.
        md = (result.get("markdown") or "")[:12000]
        text = (result.get("text") or "")[:12000]
        return {
            "final_url": result.get("final_url"),
            "title": result.get("title"),
            "status": result.get("status"),
            "engine": result.get("engine"),
            "markdown": md,
            "text_preview": text[:2000],
            "links": (result.get("links") or [])[:50],
        }

    async def sec_edgar_search(ctx, args):
        """SEC EDGAR full-text search — returns companies that filed
        with the SEC matching the query. Strong B2B-lead signal:
        every match is a real operating company with a verified address
        and SIC → industry mapping."""
        from public_data_sources import search_sec_edgar
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "query is required"}
        state = (args.get("state") or "").strip().upper() or None
        limit = max(1, min(int(args.get("limit") or 25), 50))
        try:
            rows = await search_sec_edgar(query=query, state=state, limit=limit)
        except Exception as exc:
            logger.warning("sec_edgar_search failed: %s", exc)
            return {"error": f"SEC EDGAR query failed: {exc}", "rows": []}
        return {"rows": rows, "count": len(rows), "source": "SEC EDGAR"}

    async def usaspending_recipients(ctx, args):
        """USAspending.gov recipient search — federal contractors and
        grantees by state + (optional) NAICS / industry keyword."""
        from public_data_sources import search_usaspending
        state = (args.get("state") or "").strip().upper()
        if not state:
            return {"error": "state is required (e.g. 'AZ', 'TX', 'GA')"}
        kw = (args.get("naics_keyword") or args.get("keyword") or "").strip() or None
        limit = max(1, min(int(args.get("limit") or 25), 50))
        try:
            rows = await search_usaspending(state=state, naics_keyword=kw, limit=limit)
        except Exception as exc:
            logger.warning("usaspending_recipients failed: %s", exc)
            return {"error": f"USAspending query failed: {exc}", "rows": []}
        return {"rows": rows, "count": len(rows), "source": "USAspending.gov"}

    async def wikipedia_companies_list(ctx, args):
        """Pull companies from a Wikipedia 'List of companies …' page.
        Wikipedia is liberal-licence + scrape-friendly so this works
        reliably where Chamber of Commerce / BBB / Yelp do not."""
        from public_data_sources import search_wikipedia_companies
        query = (args.get("query") or args.get("city") or "").strip()
        if not query:
            return {"error": "query / city is required (e.g. 'Phoenix, Arizona', 'Austin')"}
        limit = max(1, min(int(args.get("limit") or 30), 50))
        try:
            rows = await search_wikipedia_companies(query=query, limit=limit)
        except Exception as exc:
            logger.warning("wikipedia_companies_list failed: %s", exc)
            return {"error": f"Wikipedia query failed: {exc}", "rows": []}
        return {"rows": rows, "count": len(rows), "source": "Wikipedia"}

    async def opencorporates_search(ctx, args):
        """OpenCorporates jurisdiction-scoped company search.

        Note: as of Feb 2026 the free public REST endpoint requires an
        API token even for low-volume reads (returns HTTP 401 without).
        We fail-soft with a hint so the agent retries another source
        instead of looping on us.
        """
        from public_data_sources import search_opencorporates
        jurisdiction = (args.get("jurisdiction") or "").strip().lower()
        q = (args.get("q") or args.get("query") or "").strip()
        if not jurisdiction or not q:
            return {"error": "jurisdiction (e.g. 'us_az') and q are required"}
        limit = max(1, min(int(args.get("limit") or 25), 30))
        try:
            rows = await search_opencorporates(jurisdiction=jurisdiction, q=q, limit=limit)
        except Exception as exc:
            logger.warning("opencorporates_search failed: %s", exc)
            return {"error": f"OpenCorporates query failed: {exc}", "rows": []}
        if not rows:
            return {
                "rows": [], "count": 0, "source": "OpenCorporates",
                "hint": (
                    "OpenCorporates returned 0 rows — their free tier "
                    "now requires an API key. Use `sec_edgar_search`, "
                    "`usaspending_recipients`, or `wikipedia_companies_list` "
                    "instead — those are all key-free."
                ),
            }
        return {"rows": rows, "count": len(rows), "source": "OpenCorporates"}




    async def web_crawl(ctx, args):
        """Shallow same-host crawl — homepage + priority pages."""
        url = (args.get("url") or "").strip()
        if not url:
            return {"error": "url is required"}
        max_pages = int(args.get("max_pages") or 5)
        render = (args.get("render") or "auto").strip().lower()
        if render not in {"auto", "force", "never"}:
            render = "auto"
        from forge_crawl import crawl as _forge_crawl
        result = await _forge_crawl(url, max_pages=max_pages, render=render)
        if not result.get("ok"):
            return {"error": result.get("error") or "crawl failed"}
        # Compact per-page payload so we don't drown the LLM.
        pages = []
        for p in (result.get("pages") or []):
            if not p:
                continue
            pages.append({
                "url": p.get("final_url"),
                "title": p.get("title"),
                "ok": p.get("ok"),
                "status": p.get("status"),
                "engine": p.get("engine"),
                "markdown": (p.get("markdown") or "")[:6000],
            })
        return {"seed": url, "pages": pages, "count": len(pages)}

    async def web_extract(ctx, args):
        """Scrape `url` then coerce content into a JSON schema via LLM."""
        url = (args.get("url") or "").strip()
        schema = args.get("schema")
        if not url:
            return {"error": "url is required"}
        if not isinstance(schema, dict) or not schema:
            return {"error": "schema must be a non-empty JSON object describing the fields you want"}
        instructions = (args.get("instructions") or "").strip()
        render = (args.get("render") or "auto").strip().lower()
        if render not in {"auto", "force", "never"}:
            render = "auto"
        # Lazy import the LLM bindings off server.py to avoid forcing
        # build_tools to take them as args (would ripple through every
        # caller).
        from emergentintegrations.llm.chat import LlmChat, UserMessage
        import os as _os
        emergent_key = _os.environ.get("EMERGENT_LLM_KEY", "")
        # Honour the user-key-first policy used elsewhere in the
        # backend: if they've connected their own OpenAI key, prefer it.
        try:
            from server import EMERGENT_LLM_KEY as _SERVER_KEY  # type: ignore
            if _SERVER_KEY:
                emergent_key = _SERVER_KEY
        except Exception:
            pass
        from forge_crawl import extract as _forge_extract
        result = await _forge_extract(
            url, schema=schema, instructions=instructions, render=render,
            llm_chat_cls=LlmChat, user_message_cls=UserMessage,
            emergent_llm_key=emergent_key,
        )
        if not result.get("ok"):
            return {"error": result.get("error") or "extract failed",
                    "final_url": result.get("final_url") or url}
        return {
            "final_url": result.get("final_url"),
            "data": result.get("data"),
            "engine": result.get("engine"),
        }


    async def csv_append_rows(ctx, args):
        """Incrementally build a CSV deliverable across multiple turns.

        Args:
            name (str):         CSV filename, e.g. "phoenix_leads.csv".
                                 Same name across calls = same growing CSV.
            columns (list[str], required first call): ordered column headers.
                                 Ignored on subsequent calls (locked at first call).
            rows (list[list] | list[dict]): rows to append. dicts use the
                                 declared columns as keys; lists are positional.
            title (str, optional): human-friendly title for the Documents library.
                                 Defaults to a slug from `name`.
            replace (bool, optional): if true, REPLACE existing rows instead
                                 of appending. Use for de-dupe corrections.

        Behaviour:
            - Validates row shape against locked columns; skips malformed
              rows with a warning.
            - Caps a single call at DATASET_MAX_ROWS_PER_CALL rows.
            - Caps total accumulated rows at DATASET_MAX_ROWS_TOTAL.
            - Persists state in `missions.csv_workspace.<name>` and
              regenerates the CSV bytes on every call.
            - Pulls the previous version of the artifact (same original_name)
              off `mission.artifacts` so the user sees one growing file
              rather than a parade of revisions.
            - Stamps `csv_row_count` on the artifact dict so the dataset-
              row gate in the finish handler can verify breadth.
        """
        import csv as _csv
        import io as _io

        mission_id = ctx.get("mission_id") or ""
        if not mission_id:
            return {"error": "No mission context — csv_append_rows is only available inside a running mission."}

        name = (args.get("name") or "").strip()
        if not name:
            return {"error": "name is required (CSV filename like 'phoenix_leads.csv')"}
        if not name.lower().endswith(".csv"):
            name = name + ".csv"

        columns_in = args.get("columns") or []
        rows_in = args.get("rows") or []
        replace = bool(args.get("replace"))
        title = (args.get("title") or "").strip()

        if not isinstance(rows_in, list):
            return {"error": "rows must be a list of dicts or list of lists"}
        if not rows_in and not replace:
            return {"error": "rows is empty — pass at least one row (or replace=true to clear)"}

        if len(rows_in) > DATASET_MAX_ROWS_PER_CALL:
            rows_in = rows_in[:DATASET_MAX_ROWS_PER_CALL]
            truncated_call = True
        else:
            truncated_call = False

        # Load existing workspace state for this name (if any).
        mission_row = await db.missions.find_one(
            {"id": mission_id},
            {"csv_workspace": 1, "artifacts": 1},
        ) or {}
        # `csv_workspace` is a LIST of {"name", "columns", "rows", "updated_at"}
        # entries — we can't key it by filename because Mongo treats `.` in
        # dotted-path keys as nesting (`x.csv` would become {x: {csv: ...}}).
        workspace = list(mission_row.get("csv_workspace") or [])
        slot_idx = next(
            (i for i, w in enumerate(workspace) if (w or {}).get("name") == name),
            -1,
        )
        slot = workspace[slot_idx] if slot_idx >= 0 else {}
        columns = slot.get("columns") or []
        existing_rows = slot.get("rows") or []

        # First call must declare columns; later calls can omit them.
        if not columns:
            if not isinstance(columns_in, list) or not columns_in:
                return {"error": (
                    "First call to csv_append_rows for this file MUST include "
                    "`columns` (a list of header strings). Example: "
                    '{"name":"leads.csv","columns":["Company","City","Website","Source URL"],"rows":[...]}'
                )}
            columns = [str(c).strip() for c in columns_in if str(c).strip()]
            if not columns:
                return {"error": "columns is empty after trimming"}
        # If caller passed columns on a later call, ignore them silently
        # but tell the LLM so it doesn't think it changed the schema.

        # Coerce rows into list-of-strings aligned with columns.
        normalised: List[List[str]] = []
        skipped: List[str] = []
        for idx, row in enumerate(rows_in):
            if isinstance(row, dict):
                normalised.append([str(row.get(c, "") or "") for c in columns])
            elif isinstance(row, list):
                # Pad or trim to match column count.
                rstr = [str(v or "") for v in row[:len(columns)]]
                while len(rstr) < len(columns):
                    rstr.append("")
                normalised.append(rstr)
            else:
                skipped.append(f"row #{idx}: not a dict or list")

        # Drop completely-empty rows (every cell blank) — those add no signal.
        normalised = [r for r in normalised if any((c or "").strip() for c in r)]

        if replace:
            merged = normalised
        else:
            merged = list(existing_rows) + normalised

        # Enforce total cap.
        if len(merged) > DATASET_MAX_ROWS_TOTAL:
            merged = merged[:DATASET_MAX_ROWS_TOTAL]
            truncated_total = True
        else:
            truncated_total = False

        # Render CSV bytes.
        buf = _io.StringIO()
        writer = _csv.writer(buf, quoting=_csv.QUOTE_MINIMAL)
        writer.writerow(columns)
        for r in merged:
            writer.writerow(r)
        csv_text = buf.getvalue()

        # Pull any previous artifact entry for this file so the library
        # doesn't accumulate revisions on every append.
        try:
            await db.missions.update_one(
                {"id": mission_id},
                {"$pull": {"artifacts": {"original_name": name}}},
            )
        except Exception:
            pass
        # Also delete the previous Documents row(s) for this CSV under
        # this mission so the org library shows ONE current file.
        try:
            await db.documents.delete_many({
                "org_id": ctx["org_id"],
                "message_id": ctx.get("mission_id") or "",
                "original_name": name,
            })
        except Exception:
            pass

        from artifacts import _safe_filename
        clean, ext = _safe_filename(name)
        attachments = await save_artifacts(
            db, [{
                "name": clean, "ext": ext, "mime": "text/csv",
                "title": title or clean, "content": csv_text,
            }],
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

        # Stash the workspace state for the next call. We rebuild the
        # whole array each time (it's bounded by the rows cap so this
        # stays cheap) — keeps the Mongo write atomic per call.
        new_slot = {
            "name": name,
            "columns": columns,
            "rows": merged,
            "updated_at": datetime.now(timezone.utc),
        }
        if slot_idx >= 0:
            workspace[slot_idx] = new_slot
        else:
            workspace.append(new_slot)
        try:
            await db.missions.update_one(
                {"id": mission_id},
                {"$set": {"csv_workspace": workspace}},
            )
        except Exception as exc:
            logger.warning("csv_workspace persist failed: %s", exc)

        # Stamp `csv_row_count` on the artifact dict — the dataset-row
        # gate in the finish handler reads this.
        artifact_dict = {
            "document_id": a.get("document_id"),
            "original_name": a.get("original_name"),
            "filename": a.get("filename"),
            "mime": a.get("mime"),
            "url": a.get("url"),
            "size": a.get("size"),
            "csv_row_count": len(merged),
            "csv_columns": columns,
        }

        return {
            "artifact": artifact_dict,
            "ok": True,
            "row_count": len(merged),
            "rows_appended": len(normalised),
            "rows_skipped": skipped[:5],
            "columns": columns,
            "truncated_call": truncated_call,
            "truncated_total": truncated_total,
            "hint": (
                f"CSV now has {len(merged)} row(s). Keep going — "
                f"target is {DATASET_MIN_ROWS}+ rows before finishing. "
                "Next: harvest more from a DIFFERENT source (Chamber of "
                "Commerce, BBB, Yelp, Google Maps, SEC EDGAR, OpenCorporates) "
                "and call csv_append_rows again with the next batch."
            ),
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
             "Save a deliverable to the Documents library — users download these. PREFER **.pdf** for briefs / reports / plans / audits / memos / proposals (body is Markdown, the system renders a polished PDF). Use **.docx** when the goal mentions Word. Use .md only when the goal explicitly asks for raw markdown. Use .csv for tabular data — BUT prefer `csv_append_rows` for any list / dataset / lead-hunt mission (it builds the CSV row-by-row across multiple turns so you can harvest 50-200 rows within the iteration budget). ALLOWED extensions: .pdf .docx .md .csv .tsv .json .html .ics .vcf .eml .sh .py .js .ts .sql .txt .xml .yaml. NEVER save a long-form brief as .txt — it strips formatting. NOT for images — use `generate_image` instead.",
             '{"name": "audit_report.pdf", "mime": "application/pdf", "title": "SEO Audit", "content": "# Heading\\n\\nMarkdown body — system renders to PDF."}',
             write_artifact, side_effect=True),
        Tool("csv_append_rows",
             "Incrementally build a CSV deliverable across multiple turns. Use this — NOT write_artifact — for ANY list / dataset / lead-hunt / directory / contacts / company list mission. First call MUST declare `columns` (ordered list of header strings); subsequent calls only need `name` + `rows` (list of dicts keyed by column, or list of lists in column order). The CSV grows on every call — the library shows ONE growing file, not a parade of revisions. Cap per call: 25 rows; total cap: 500 rows. After every call you'll receive an updated row count + a hint reminding you what's left. Target ≥ 15 rows before finishing (the dataset-row finish gate enforces this). Strategy: web_search → web_scrape / web_extract per result → csv_append_rows with the harvested batch → repeat with a DIFFERENT source.",
             '{"name": "phoenix_leads.csv", "columns": ["Company","City","State","Website","Industry","Source URL"], "rows": [{"Company":"Acme Tech","City":"Phoenix","State":"AZ","Website":"https://acme.example","Industry":"SaaS","Source URL":"https://chamber.example/members"}]}',
             csv_append_rows, side_effect=True),
        Tool("sec_edgar_search",
             "FAST LEAD SOURCE — SEC EDGAR full-text search. Returns companies that filed with the SEC (10-K, 10-Q, S-1, DEF 14A) matching the query. Every match is a real operating company with a verified address and SIC → industry mapping. Use this FIRST for any B2B lead hunt — it never blocks (no Cloudflare / login walls) and returns 25+ rows per call. Pass `query` (industry keyword OR company name) and optional `state` (2-letter code like 'AZ', 'TX'). Each returned row is already shaped for the standard lead-hunt CSV — push the array straight to csv_append_rows.",
             '{"query": "healthcare", "state": "AZ", "limit": 25}',
             sec_edgar_search),
        Tool("usaspending_recipients",
             "FAST LEAD SOURCE — USAspending.gov federal-money recipient search. Returns businesses that have received federal contracts or grants, filtered by `state` (required) and optional `naics_keyword` / industry keyword. Tens of thousands of businesses indexed with state + city pre-attached. Rows shape-compatible with csv_append_rows. Use this — NOT Yelp / BBB / Chamber of Commerce — for any government / regulated-industry / B2B-services lead hunt; those scrape sources are Cloudflare-blocked and return empty.",
             '{"state": "AZ", "naics_keyword": "consulting", "limit": 25}',
             usaspending_recipients),
        Tool("wikipedia_companies_list",
             "FAST LEAD SOURCE — pull companies from a Wikipedia 'List of companies based in <City>' / 'List of companies in <Industry>' article. Wikipedia is liberal-licence + scrape-friendly so this works reliably where Chamber of Commerce / BBB / Yelp / LinkedIn do not. Pass `query` as a city + state (e.g. 'Phoenix, Arizona') or a sector ('biotech'). Returns 30 rows max per call, shape-compatible with csv_append_rows.",
             '{"query": "Phoenix, Arizona", "limit": 30}',
             wikipedia_companies_list),
        Tool("opencorporates_search",
             "FAST LEAD SOURCE — OpenCorporates jurisdiction-scoped registered-business search. Pass `jurisdiction` (e.g. 'us_az', 'us_tx', 'us_ga', 'us_md', 'us_fl') and `q` (company name or industry keyword). Free tier — moderate volume OK without an API key. Returns registered businesses with name, address, and registration metadata. Rows shape-compatible with csv_append_rows.",
             '{"jurisdiction": "us_az", "q": "consulting", "limit": 25}',
             opencorporates_search),
        Tool("generate_image",
             "Render a new image (PNG) with Nano Banana (default) or GPT Image 1 and save it to the Documents library. Use this for ANY image deliverable — do NOT try to emit .png via write_artifact.",
             '{"prompt": "A flat-design infographic...", "model": "nano-banana"}',
             generate_image, side_effect=True),
        Tool("generate_video",
             "Render a video clip and save it to the Documents library. Models: 'veo3_fast' (default — Google Veo 3.1 fast tier), 'veo3' (full Veo 3.1), 'kling-3' / 'kling-3-pro' (good for product shots), 'wan-2-6' (long-form), 'seedance-1-5' (animation), 'sora-2' (narrative), 'runway-aleph' (cinematic). Pass `aspect_ratio` like '16:9' or '9:16'. Use this — NOT generate_image — when the user wants motion, animation, or a clip.",
             '{"prompt": "30-second cinematic shot of a coffee cup pouring", "model": "veo3_fast", "aspect_ratio": "16:9"}',
             generate_video, side_effect=True),
        Tool("generate_music",
             "Compose a music track via Suno and save it to the Documents library as MP3. Args: `prompt` (describe the song), optional `style` (e.g. 'cinematic', 'lo-fi hip hop'), optional `instrumental: true` for no vocals. Use for background music, jingles, podcast intros.",
             '{"prompt": "Upbeat 60s synthwave with female vocals about AI dreams", "style": "synthwave", "instrumental": false}',
             generate_music, side_effect=True),
        Tool("resize_image",
             "Resize a SINGLE source image (a user-uploaded attachment OR an artifact this mission already produced) into MANY exact pixel sizes. Use this — NOT generate_image — when the user asks for a hero image at multiple ad/banner slots like 'Wide Skyscraper 160x600, Rectangle 300x250, Leaderboard 728x90'. Always call read_attachment with no args first to discover available `attachment_id`s, then call resize_image once with the full `sizes` list. Modes: 'fit' (default — scale + pad with averaged edge colour, no cropping; best for ads), 'fill' (cover + center-crop), 'stretch' (distort to exact). Outputs are saved as individual PNGs in the Documents library and emitted as mission artifacts.",
             '{"attachment_id": "att_xxx", "base_name": "hero", "sizes": [{"label": "Wide Skyscraper", "width": 160, "height": 600}, {"label": "Rectangle", "width": 300, "height": 250}, {"label": "Mobile Leaderboard", "width": 320, "height": 50}, {"label": "Leaderboard", "width": 728, "height": 90}, {"label": "Half-Page", "width": 300, "height": 600}, {"label": "Billboard", "width": 970, "height": 250}]}',
             resize_image, side_effect=True),
        Tool("browse_url",
             "Load a real web page in a headless Chromium (handles JavaScript / SPA / cookie walls). Returns rendered text, a screenshot, AND a structured page-map: `forms` (with field labels & types), `buttons` and `links` (with their visible text). ALWAYS read the `affordances_summary` field first — it tells you what's clickable on this page. The first step in any form-fill or multi-hop navigation goal. Prefer `web_scrape` for read-only content extraction — it's faster.",
             '{"url": "https://..."}',
             browse_url, side_effect=True),
        Tool("web_scrape",
             "Fast single-page scrape via Forge Crawl. Returns CLEAN markdown of the main content (chrome/nav/ads stripped) plus same-host outbound links. Uses httpx for server-rendered sites and auto-falls-back to Playwright for JS-heavy SPAs. Use this — NOT browse_url — when you just need to READ a page (research, fact-grab, content audit). For form-fill / multi-step navigation, use browse_url. Pass render='force' to skip httpx (slower, more reliable) or 'never' (httpx only).",
             '{"url": "https://...", "render": "auto"}',
             web_scrape),
        Tool("web_crawl",
             "Shallow same-host crawl via Forge Crawl. Scrapes the seed page, then up to `max_pages-1` priority pages discovered on it (about, pricing, contact, services …). Returns markdown for every page. Use this for business audits / competitor reconnaissance / site-wide content sweeps — it's much cheaper than chaining web_scrape calls yourself. Max 20 pages.",
             '{"url": "https://...", "max_pages": 5, "render": "auto"}',
             web_crawl),
        Tool("web_extract",
             "Scrape a page then have an LLM coerce the content into the JSON `schema` you specify. Returns ONE structured object. Use this when you need specific fields off a page (e.g., product price + title + image, founder name + LinkedIn, pricing tiers) and don't want to parse markdown yourself. `schema` is a free-form JSON object describing field names + expected types; the LLM does best-effort coercion and uses null/[] when a field is genuinely missing.",
             '{"url": "https://...", "schema": {"product_name": "string", "price_usd": "number", "features": ["string"]}, "instructions": "(optional extra guidance)"}',
             web_extract),
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
        Tool("draft_to_social_queue",
             "Create ON-BRAND social posts directly in the org's Social Queue. Use this — NOT write_artifact — for ANY 'generate N posts for [platform]' / 'queue up X tweets/IG posts/LinkedIn posts' / 'draft this week's social content' goal. Each draft lands in the Social tab → Drafts ready for the user to click Schedule on (one-click flow). DO NOT save social posts as a .pdf — that's the wrong deliverable. `platforms` accepts singular or list (twitter, linkedin, facebook, instagram, threads, bluesky, mastodon, reddit, tiktok, youtube, mailchimp). `count` clamps to 1-10. Posts land without cover images by default — the user clicks 'Generate cover image' on each draft to render brand-consistent artwork via Nano Banana.",
             '{"prompt": "Announce our spring HalfSalt drop — premium pistachio gift boxes", "platforms": ["instagram", "linkedin"], "count": 3}',
             draft_to_social_queue, side_effect=True),
    ] + _maybe_github_tools(db)


def _maybe_github_tools(db) -> List[Tool]:
    """Append GitHub tools to the catalog ONLY when GITHUB_CLIENT_ID is
    set on the backend. Avoids confusing the LLM with tools it can't
    actually use on a deploy where the OAuth app wasn't configured."""
    try:
        from github_integration import make_github_tools, _is_configured
        if not _is_configured():
            return []
        out = []
        for name, desc, args_hint, handler in make_github_tools(db=db):
            out.append(Tool(name, desc, args_hint, handler, side_effect=True))
        return out
    except Exception:
        return []


async def _augment_with_composio_tools(
    db, tools: List[Tool], *, org_id: str, user_id: str,
    mission_id: str, lead_agent_id: Optional[str],
) -> List[Tool]:
    """Append the user's connected Composio tools to the mission catalog.

    Silently no-op when COMPOSIO_API_KEY isn't set, the user has no
    ACTIVE connections, or the lead agent's tools_enabled toggle is OFF.
    See `composio_mission_tools.build_composio_mission_tools` for the
    full gating rules.
    """
    try:
        from composio_mission_tools import build_composio_mission_tools
        extra = await build_composio_mission_tools(
            db,
            org_id=org_id,
            user_id=user_id,
            mission_id=mission_id,
            lead_agent_id=lead_agent_id,
            Tool=Tool,
        )
        return list(tools) + list(extra)
    except Exception as exc:
        logger.warning("Composio mission-tool injection failed: %s", exc)
        return tools


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

    @r.post("/missions/draft-goal")
    async def draft_mission_goal(
        body: DraftGoalBody, u: dict = Depends(get_current_user),
    ):
        """Take a user's rough idea + optional structured hints and
        return a polished mission goal that follows Foundry's mission
        best practices (concrete deliverable, named format, measurable
        success criteria, source-breadth requirement). Also supports a
        `refine_instruction` mode so users can iterate on the draft
        without re-typing everything.

        Auth-gated only — no org binding because this is a stateless
        LLM helper that doesn't read or write any user data.
        """
        # Compose the user-turn payload deterministically so the LLM
        # always sees the same shape regardless of which fields the
        # client populated.
        if body.refine_instruction.strip():
            payload = json.dumps({
                "mode": "refine",
                "existing_goal": body.goal,
                "refine_instruction": body.refine_instruction,
            }, ensure_ascii=False)
        else:
            payload = json.dumps({
                "mode": "draft",
                "rough_goal": body.goal,
                "deliverable_hint": body.deliverable or "(none — infer the best deliverable)",
                "success_looks_like": body.success_looks_like or "(none — infer reasonable criteria)",
            }, ensure_ascii=False)
        try:
            chat = LlmChat(
                api_key=EMERGENT_LLM_KEY,
                session_id=f"mission-drafter:{u['id']}",
                system_message=_DRAFTER_SYSTEM,
            ).with_model("openai", "gpt-4.1-mini")
            raw = await asyncio.wait_for(
                chat.send_message(UserMessage(text=payload)),
                timeout=45,
            )
        except asyncio.TimeoutError:
            raise HTTPException(504, "Goal drafter timed out — try again.")
        except Exception as exc:
            logger.warning("mission goal drafter LLM error: %s", exc)
            raise HTTPException(502, "Goal drafter is temporarily unavailable.")

        text = (raw or "").strip()
        # Defensive: strip a markdown fence if the model emitted one
        # despite the strict-JSON instruction.
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.M)
        try:
            data = json.loads(text)
            polished = (data.get("goal") or "").strip()
        except json.JSONDecodeError:
            # Fall back: if it returned plain text, use it raw — the
            # client still gets a usable string.
            polished = text

        if not polished:
            raise HTTPException(502, "Goal drafter returned an empty response.")
        # Clip to the upper budget so the textarea never gets flooded.
        if len(polished) > GOAL_MAX_CHARS:
            polished = polished[:GOAL_MAX_CHARS].rstrip()
        return {
            "goal": polished,
            "char_count": len(polished),
            "char_target": [GOAL_MIN_CHARS, GOAL_MAX_CHARS],
        }


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
        # ── B4: pull saved per-domain credentials that match URLs in
        # the goal so the agent boots logged-in to every site the user
        # has previously authenticated for.
        try:
            from credential_vault import merge_credentials_for_mission
            urls_in_goal = re.findall(
                r"https?://[^\s\)]+", body.goal or "",
            )
            vault_state = await merge_credentials_for_mission(
                db=db, org_id=org_id, urls=urls_in_goal,
            )
            if vault_state:
                if not merged_state:
                    merged_state = {"cookies": [], "origins": []}
                merged_state["cookies"] = (
                    (merged_state.get("cookies") or [])
                    + (vault_state.get("cookies") or [])
                )
                merged_state["origins"] = (
                    (merged_state.get("origins") or [])
                    + (vault_state.get("origins") or [])
                )
        except Exception as exc:
            logger.warning(
                "mission: failed to merge domain credentials for %s: %s",
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
                # No saved session for the detected platform. Previous
                # behaviour: hard-fail the mission before it even ran,
                # forcing the user to upload cookies via "Manage login
                # sessions" first. New behaviour: let the mission start,
                # tell the agent to navigate to the composer normally,
                # and rely on the existing login-wall auto-pause +
                # **Take control** flow (see B3 in the runner) so the
                # user can log in interactively in the live browser
                # canvas. After login, the agent resumes and the cookies
                # are persisted for next time — no detour required.
                goal_with_context += (
                    f"\n\n[SYSTEM CONTEXT — DO NOT IGNORE]\n"
                    f"This mission involves posting on {detected_platform.upper()}.\n"
                    f"• NO authenticated session is on file for {detected_platform} yet — "
                    f"that's OK. Navigate to {composer or 'the composer page'} "
                    f"normally with browse_url.\n"
                    f"• When {detected_platform.upper()} shows the login screen, "
                    f"DO NOT type credentials and DO NOT call browse_fill on "
                    f"login forms. The runner will auto-pause the mission and "
                    f"the user will sign in manually via the live browser "
                    f"canvas (Take control). Their session is then saved.\n"
                    f"• After the user hands control back, the page should be "
                    f"logged in. Continue with browse_url → browse_fill → "
                    f"browse_submit on the composer to publish the post.\n"
                    f"• On successful publication, finish with summary starting "
                    f"'PUBLISHED:' and include the verified post URL.\n"
                    f"• If anything else goes wrong post-login, finish with "
                    f"summary starting 'FAILED:' and a one-line reason.\n"
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
        # If the user is clearly saying "stop asking, proceed" — flag
        # the mission so the loop blocks any further ask_user calls.
        # See _looks_like_user_declined for the matched phrases.
        update_set: Dict[str, Any] = {"status": "running", "question": ""}
        if _looks_like_user_declined(body.answer):
            update_set["user_declined_more_info"] = True
            await _append_event(
                db, mission_id, kind="system",
                text=("User signaled they have nothing more to add. "
                      "ask_user is now BLOCKED for the rest of this "
                      "mission — proceed with public sources or finish."),
                iteration=m.get("iteration_count", 0),
            )
        await db.missions.update_one(
            {"id": mission_id},
            {"$set": update_set},
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

    @r.post("/missions/{mission_id}/force-finish")
    async def force_finish_mission(mission_id: str,
                                   u: dict = Depends(get_current_user)):
        """User-initiated escape hatch from an `awaiting_user` mission
        when the agent is asking the same question on repeat. The user
        clicks "Just finish with what you have" → we set the
        user_declined_more_info flag, log an explicit "user forced
        finish" event, then resume the loop. The hardened ask_user
        guard will now refuse any further questions, forcing the agent
        to deliver from public data or finish honestly with the gap
        documented.

        Safe on any mission state: if it's not awaiting_user we still
        flip the flag so the rest of the run is question-free.
        """
        m = await db.missions.find_one({"id": mission_id})
        if not m:
            raise HTTPException(404, "Mission not found")
        await require_org_member(m["org_id"], u["id"])

        was_awaiting = m.get("status") == "awaiting_user"
        await _append_event(
            db, mission_id, kind="system",
            text=("User clicked 'Just finish with what you have'. "
                  "ask_user is BLOCKED for the rest of this mission. "
                  "Deliver from public sources or finish with a "
                  "documented data gap."),
            iteration=m.get("iteration_count", 0),
        )
        await db.missions.update_one(
            {"id": mission_id},
            {"$set": {
                "user_declined_more_info": True,
                # Only flip status if we were stuck awaiting an answer.
                **({"status": "running", "question": ""} if was_awaiting else {}),
            }},
        )

        # Restart the loop only if the mission was paused on the user.
        # For a running mission the live loop already polls the flag
        # via the fresh DB read in the ask_user branch.
        if was_awaiting:
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


async def rehydrate_running_missions(
    *, db, AGENTS: dict, EMERGENT_LLM_KEY: str,
    LlmChat, UserMessage,
    build_tools_factory: Callable[[], List[Tool]],
    build_lead_system_prompt: Callable[[str], str],
    stale_minutes: int = 60,
) -> int:
    """Re-spawn the asyncio task for any mission that was `running` or
    `awaiting_user` when the previous backend process died (pod restart,
    redeploy, OOM, scale event).

    Without this, the `missions` row is left in `running` state forever
    until the watchdog reaps it 15+ minutes later — which is exactly the
    "my mission stopped because of watchdog" complaint. By restarting the
    loop here we keep missions truly autonomous across deploys.

    Strategy:
      * `running` + recent heartbeat (< stale_minutes) → restart the loop.
        The loop reads the mission's event history on iteration 1 so it
        picks up where it left off.
      * `awaiting_user` → leave alone. The user owes an answer; the loop
        is correctly suspended until they post via /missions/{id}/answer.
      * `running` + heartbeat older than `stale_minutes` → leave for the
        startup-recovery scan in server.py to fail with a clear message.
        These are usually missions where Mongo writes succeeded but the
        process hung mid-tool-call (e.g. Playwright deadlock) and the
        cleanest recovery is "let the user click Continue".

    Returns the number of missions rehydrated.
    """
    from datetime import timedelta as _td
    cutoff = datetime.now(timezone.utc) - _td(minutes=stale_minutes)
    rehydrated = 0
    cur = db.missions.find(
        {"status": "running"},
        {"_id": 0},
    )
    async for m in cur:
        mid = m.get("id") or ""
        if not mid:
            continue
        # Skip missions that are already running on THIS process (rare —
        # only happens if rehydrate is called twice in a row).
        if mid in _RUNNING and not _RUNNING[mid].done():
            continue
        # Heartbeat freshness gate — too-stale missions get failed by the
        # startup recovery scan instead.
        last = m.get("last_activity_at") or m.get("updated_at") or m.get("started_at")
        if isinstance(last, datetime):
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if last < cutoff:
                continue
        # Re-stamp the heartbeat NOW so the watchdog doesn't reap us
        # mid-rehydrate. Also append a system event so the work log
        # shows the resume point clearly to the user.
        now = datetime.now(timezone.utc)
        try:
            await db.missions.update_one(
                {"id": mid, "status": "running"},
                {"$set": {"last_activity_at": now}},
            )
            await db.mission_events.insert_one({
                "id": str(uuid.uuid4()),
                "mission_id": mid,
                "kind": "system",
                "ts": now,
                "text": ("Backend restart detected — mission auto-resumed "
                         "from work log. No action required."),
                "iteration": int(m.get("iteration_count", 0)),
            })
        except Exception as exc:
            logger.warning("Could not stamp resume heartbeat for %s: %s", mid, exc)
        # Build the same ctx the original start_mission did.
        ctx = {
            "org_id": m.get("org_id") or "",
            "mission_id": mid,
            "lead_agent_id": m.get("lead_agent_id"),
            "lead_agent_name": m.get("lead_agent_name"),
            "created_by": m.get("created_by") or "",
        }
        tools = build_tools_factory()
        try:
            task = asyncio.create_task(run_mission_loop(
                db=db, mission=m, tools=tools, ctx=ctx,
                LlmChat=LlmChat, UserMessage=UserMessage,
                EMERGENT_LLM_KEY=EMERGENT_LLM_KEY,
                build_lead_system_prompt=build_lead_system_prompt,
            ))
            _RUNNING[mid] = task
            rehydrated += 1
        except Exception as exc:
            logger.warning("Could not rehydrate mission %s: %s", mid, exc)
    if rehydrated:
        logger.info("Mission rehydrate: re-spawned %d running mission(s)", rehydrated)
    return rehydrated


__all__ = [
    "Tool",
    "build_tools",
    "make_missions_router",
    "run_mission_loop",
    "rehydrate_running_missions",
]
