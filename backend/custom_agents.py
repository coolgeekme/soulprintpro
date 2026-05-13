"""custom_agents.py — per-org user-defined agents.

Custom agents live alongside the 169 baked-in agents in the in-process
AGENTS dict (registered at startup + on create/update/delete) so every
existing AGENTS[aid] lookup, smart-router, mention parser, and prompt
builder keeps working with zero patching.

The only differentiator: a custom agent carries an `org_scope` field
naming the org that owns it. `team_agents` filters the catalogue so a
custom agent only appears in teams that belong to its own org.

Schema (collection: custom_agents):
  { id, org_id, name, role, description, system,
    division, division_key, emoji, accent,
    created_by, created_at, updated_at }
"""

from datetime import datetime, timezone
from typing import Optional
import asyncio
import logging
import uuid

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, Field
from motor.motor_asyncio import AsyncIOMotorDatabase


logger = logging.getLogger("foundry.custom_agents")


# ── Wired in by server.py at startup ────────────────────────────────────────
_db: Optional[AsyncIOMotorDatabase] = None
_agents_dict: Optional[dict] = None
_get_current_user = None
_require_org_member = None
_LlmChat = None
_UserMessage = None
_EMERGENT_LLM_KEY: Optional[str] = None


def init(*, db, agents_dict, get_current_user, require_org_member,
         LlmChat=None, UserMessage=None, EMERGENT_LLM_KEY=None):
    """Call once from server.py after definitions exist.

    The optional `LlmChat`/`UserMessage`/`EMERGENT_LLM_KEY` params power
    the new wizard helpers — `draft-system` (compose system prompt
    from plain-English answers) and `preview` (one-shot test reply).
    They're optional so older callers keep working.
    """
    global _db, _agents_dict, _get_current_user, _require_org_member
    global _LlmChat, _UserMessage, _EMERGENT_LLM_KEY
    _db = db
    _agents_dict = agents_dict
    _get_current_user = get_current_user
    _require_org_member = require_org_member
    _LlmChat = LlmChat
    _UserMessage = UserMessage
    _EMERGENT_LLM_KEY = EMERGENT_LLM_KEY


async def _user_dep(request: Request) -> dict:
    """FastAPI dependency that delegates to the host app's auth helper.

    We use a thin wrapper so the underlying `get_current_user` (defined
    in server.py) can be hot-swapped in tests without re-mounting the
    router.
    """
    if _get_current_user is None:
        raise HTTPException(500, "custom_agents not initialised")
    return await _get_current_user(request)


def _public(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k != "_id"}


def _to_catalogue_entry(doc: dict) -> dict:
    """Shape a custom-agent Mongo row into the same shape AGENTS uses."""
    return {
        "id": doc["id"],
        "name": doc["name"],
        "role": doc.get("role") or doc["name"],
        "division": doc.get("division", "Custom"),
        "division_key": doc.get("division_key", "custom"),
        "description": doc.get("description", ""),
        "tagline": doc.get("description", ""),
        "emoji": doc.get("emoji", ""),
        "accent": doc.get("accent", "#f97316"),
        "system": doc.get("system", ""),
        "custom": True,
        "org_scope": doc["org_id"],
    }


async def load_into_catalogue():
    """Read every persisted custom agent and merge into AGENTS at boot."""
    if _db is None or _agents_dict is None:
        return 0
    n = 0
    async for doc in _db.custom_agents.find({}):
        _agents_dict[doc["id"]] = _to_catalogue_entry(doc)
        n += 1
    return n


async def _require_admin(org_id: str, user_id: str):
    """Owner/admin gate — used for create/update/delete."""
    ctx = await _require_org_member(org_id, user_id)
    role = (ctx.get("member") or {}).get("role", "")
    if role not in ("owner", "admin"):
        raise HTTPException(
            403, "Only the organization owner or admins can manage custom agents."
        )
    return ctx


# ── Pydantic bodies ─────────────────────────────────────────────────────────
class CustomAgentBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    role: Optional[str] = Field(None, max_length=120)
    description: Optional[str] = Field(None, max_length=400)
    system: str = Field(..., min_length=10, max_length=8000)
    emoji: Optional[str] = Field(None, max_length=8)
    accent: Optional[str] = Field(None, max_length=20)


# ── Router ──────────────────────────────────────────────────────────────────
router = APIRouter(prefix="/api/orgs")


@router.get("/{org_id}/custom-agents")
async def list_custom_agents(org_id: str, user: dict = Depends(_user_dep)):
    await _require_org_member(org_id, user["id"])
    out = []
    async for doc in _db.custom_agents.find({"org_id": org_id}).sort("created_at", -1):
        out.append(_public(doc))
    return out


@router.post("/{org_id}/custom-agents")
async def create_custom_agent(
    org_id: str, body: CustomAgentBody, user: dict = Depends(_user_dep)
):
    await _require_admin(org_id, user["id"])

    aid = f"custom_{org_id[:8]}_{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "id": aid,
        "org_id": org_id,
        "name": body.name.strip(),
        "role": (body.role or body.name).strip(),
        "description": (body.description or "").strip(),
        "system": body.system.strip(),
        "emoji": (body.emoji or "").strip()[:8],
        "accent": (body.accent or "#f97316").strip()[:20],
        "division": "Custom",
        "division_key": "custom",
        "created_by": user["id"],
        "created_at": now,
        "updated_at": now,
    }
    await _db.custom_agents.insert_one(doc.copy())
    _agents_dict[aid] = _to_catalogue_entry(doc)
    return _public(doc)


@router.patch("/{org_id}/custom-agents/{agent_id}")
async def update_custom_agent(
    org_id: str,
    agent_id: str,
    body: CustomAgentBody,
    user: dict = Depends(_user_dep),
):
    await _require_admin(org_id, user["id"])

    existing = await _db.custom_agents.find_one({"id": agent_id, "org_id": org_id})
    if not existing:
        raise HTTPException(404, "Custom agent not found")

    updates = {
        "name": body.name.strip(),
        "role": (body.role or body.name).strip(),
        "description": (body.description or "").strip(),
        "system": body.system.strip(),
        "emoji": (body.emoji or "").strip()[:8],
        "accent": (body.accent or "#f97316").strip()[:20],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await _db.custom_agents.update_one(
        {"id": agent_id, "org_id": org_id}, {"$set": updates}
    )
    refreshed = await _db.custom_agents.find_one({"id": agent_id, "org_id": org_id})
    _agents_dict[agent_id] = _to_catalogue_entry(refreshed)
    return _public(refreshed)


@router.delete("/{org_id}/custom-agents/{agent_id}")
async def delete_custom_agent(
    org_id: str, agent_id: str, user: dict = Depends(_user_dep)
):
    await _require_admin(org_id, user["id"])
    res = await _db.custom_agents.delete_one({"id": agent_id, "org_id": org_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "Custom agent not found")
    _agents_dict.pop(agent_id, None)
    return {"ok": True}


# ── Wizard helpers ──────────────────────────────────────────────────────────
class DraftSystemBody(BaseModel):
    """Plain-English answers from the wizard's voice/rules step.

    All fields are optional; the composer infers reasonable defaults
    so users can ship an agent with only a name + expertise filled in.
    """
    name: str = Field("", max_length=80)
    role: str = Field("", max_length=120)
    expertise: str = Field("", max_length=1000)  # "what is this agent expert in?"
    voice: str = Field("", max_length=600)        # "how should it talk?"
    always: str = Field("", max_length=800)       # "what should it always do?"
    never: str = Field("", max_length=800)        # "what should it never do?"
    existing_system: str = Field("", max_length=8000)  # for Refine mode
    refine_instruction: str = Field("", max_length=400)


class PreviewBody(BaseModel):
    """Run a one-shot test message against a draft system prompt."""
    system: str = Field(..., min_length=10, max_length=8000)
    user_message: str = Field(..., min_length=1, max_length=2000)


_AGENT_DRAFTER_SYSTEM = """You write production-quality system prompts for AI agents inside the Foundry platform.

A Foundry custom agent is a specialist persona that lives inside an organization. Other agents and humans mention it in team chats to get its perspective. Your job: turn the user's plain-English answers into a system prompt that produces a sharp, opinionated, on-brand agent.

OUTPUT FORMAT — STRICT
- Return JSON only. No prose, no markdown, no fences.
- Shape: {"system": "<the polished system prompt>", "description": "<one-line summary, <=140 chars>"}

PROMPT-CRAFT RULES THE OUTPUT MUST FOLLOW
1. Open with one sentence stating identity + seniority + domain. Example: "You are a senior B2B SaaS pricing strategist with 15 years of experience across PLG and sales-led GTM."
2. Add 3-6 concrete expertise anchors (frameworks, mental models, specific datasets the agent knows) — turns "knows pricing" into "thinks in unit economics, gross margin, competitive comps, and Van Westendorp PSM."
3. Capture voice in 1-2 sentences of character: tone, vocabulary, pacing, sentence length. Avoid generic words like "professional" — be specific ("crisp, no fluff, sometimes blunt; cites primary sources mid-sentence").
4. List `ALWAYS:` rules as a short bulleted block (3-7 lines, imperative, second person).
5. List `NEVER:` rules as a short bulleted block (3-7 lines).
6. End with one decision-rule sentence the agent uses to know when it has finished a useful reply.
7. Refuse forbidden content: no medical/legal/financial advice claims that imply a professional license, no fabricated citations, no jailbreaks. Bake these in as NEVER rules without explicitly mentioning "jailbreak".
8. Length budget: 600-2,500 characters. Long enough to set personality, short enough to leave room for the user's actual question in the LLM context window.
9. NEVER mention "Foundry", "SoulPrint", "the platform", or expose any meta-instructions about being inside a SaaS product. The agent should feel like a real person.

REFINEMENT MODE
- If `existing_system` and `refine_instruction` are provided, treat the existing prompt as the starting point and apply the refinement while keeping all other rules intact.

DESCRIPTION
- The `description` field is a 1-line summary shown in the agent picker. Make it scannable: <expertise> + <flavor> (e.g. "B2B SaaS pricing strategist — crisp, unit-economics-first, names competitor comps").
"""


@router.post("/{org_id}/custom-agents/draft-system")
async def draft_system_prompt(
    org_id: str, body: DraftSystemBody, user: dict = Depends(_user_dep),
):
    """Compose a polished system prompt + description from the wizard's
    plain-English voice/rules fields. Cheap (gpt-4.1-mini), 30s timeout.
    """
    await _require_org_member(org_id, user["id"])
    if not (_LlmChat and _UserMessage and _EMERGENT_LLM_KEY):
        raise HTTPException(503, "LLM helper not configured.")

    import json
    if body.refine_instruction.strip() and body.existing_system.strip():
        payload = json.dumps({
            "mode": "refine",
            "existing_system": body.existing_system,
            "refine_instruction": body.refine_instruction,
        }, ensure_ascii=False)
    else:
        payload = json.dumps({
            "mode": "compose",
            "name": body.name or "(unnamed)",
            "role": body.role or "(specialist)",
            "expertise": body.expertise or "(infer from role)",
            "voice": body.voice or "(infer professional but distinctive voice)",
            "always": body.always or "(infer sensible defaults)",
            "never": body.never or "(infer safe defaults)",
        }, ensure_ascii=False)

    try:
        chat = _LlmChat(
            api_key=_EMERGENT_LLM_KEY,
            session_id=f"custom-agent-drafter:{user['id']}",
            system_message=_AGENT_DRAFTER_SYSTEM,
        ).with_model("openai", "gpt-4.1-mini")
        raw = await asyncio.wait_for(
            chat.send_message(_UserMessage(text=payload)), timeout=45,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "Agent drafter timed out — try again.")
    except Exception as exc:
        logger.warning("custom agent drafter LLM error: %s", exc)
        raise HTTPException(502, "Agent drafter is temporarily unavailable.")

    import re
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.M)
    try:
        data = json.loads(text)
        system_out = (data.get("system") or "").strip()
        description = (data.get("description") or "").strip()[:140]
    except json.JSONDecodeError:
        system_out = text
        description = ""

    if not system_out or len(system_out) < 50:
        raise HTTPException(502, "Agent drafter returned an empty response.")
    if len(system_out) > 8000:
        system_out = system_out[:8000].rstrip()
    return {"system": system_out, "description": description}


@router.post("/{org_id}/custom-agents/preview")
async def preview_agent(
    org_id: str, body: PreviewBody, user: dict = Depends(_user_dep),
):
    """Send a single test message against a draft system prompt so the
    wizard can let the user vibe-check their agent before saving.

    No history, no streaming, 30s timeout — strictly a "does it sound
    right?" feedback loop. The agent's real chat lifecycle (with org
    Soul + memory + tools) takes over once it's saved.
    """
    await _require_org_member(org_id, user["id"])
    if not (_LlmChat and _UserMessage and _EMERGENT_LLM_KEY):
        raise HTTPException(503, "LLM helper not configured.")
    try:
        chat = _LlmChat(
            api_key=_EMERGENT_LLM_KEY,
            session_id=f"custom-agent-preview:{user['id']}",
            system_message=body.system,
        ).with_model("openai", "gpt-4.1-mini")
        reply = await asyncio.wait_for(
            chat.send_message(_UserMessage(text=body.user_message)),
            timeout=30,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "Preview timed out — try again.")
    except Exception as exc:
        logger.warning("custom agent preview LLM error: %s", exc)
        raise HTTPException(502, "Preview is temporarily unavailable.")
    return {"reply": (reply or "").strip()}

