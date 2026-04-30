"""Social Style Guide — analyze public social profiles + paste-in samples to
produce a structured brand-voice / visual-style guide for the org.

The synthesized guide is automatically injected into:
  * every Social Queue draft prompt (primary use case)
  * every agent's system prompt as a soft "social voice" hint
  * the social-image generation prompt (so visual style flows through too)

Storage: a single ``style_guide`` document on the organization record. The
list of profiles + raw analyses lives alongside it so the user can re-sync.

Schema (org.style_guide):
{
  "profiles": [
    {"id", "platform", "url", "handle", "role": "own"|"inspiration", "notes"}
  ],
  "samples": "free-text user paste (existing posts / desired voice)",
  "analyses": [
    {"profile_id", "text", "synthesized_at"}
  ],
  "guide": {
    "tone_voice", "vocabulary", "do_not_use",
    "post_structure", "themes",
    "image_video_style", "cadence",
    "summary"
  },
  "generated_at", "is_manually_edited"
}
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from perplexity_client import (
    perplexity_analyze_social_profile,
    is_available as perplexity_available,
)

logger = logging.getLogger(__name__)

# ── Models ─────────────────────────────────────────────────────────────────
ALLOWED_PLATFORMS = {
    "twitter", "x", "linkedin", "instagram", "tiktok", "youtube",
    "threads", "bluesky", "mastodon", "pinterest", "reddit", "facebook",
    "substack", "medium", "other",
}


class ProfileBody(BaseModel):
    platform: str = Field(..., min_length=1)
    url: str = Field("", max_length=500)
    handle: str = Field("", max_length=120)
    role: str = Field("own", pattern=r"^(own|inspiration)$")
    notes: str = Field("", max_length=500)


class GuideEditBody(BaseModel):
    """Patch fields on the synthesized guide. Empty strings clear a field."""
    tone_voice: Optional[str] = None
    vocabulary: Optional[str] = None
    do_not_use: Optional[str] = None
    post_structure: Optional[str] = None
    themes: Optional[str] = None
    image_video_style: Optional[str] = None
    cadence: Optional[str] = None
    summary: Optional[str] = None
    samples: Optional[str] = None


class SamplesBody(BaseModel):
    samples: str = Field("", max_length=8000)


# ── Synthesis ──────────────────────────────────────────────────────────────
def _empty_guide() -> dict:
    return {
        "tone_voice": "", "vocabulary": "", "do_not_use": "",
        "post_structure": "", "themes": "",
        "image_video_style": "", "cadence": "", "summary": "",
    }


def _empty_style_doc() -> dict:
    return {
        "profiles": [], "samples": "",
        "analyses": [], "guide": _empty_guide(),
        "generated_at": None, "is_manually_edited": False,
    }


async def _gather_profile_analyses(profiles: List[dict]) -> List[dict]:
    """Fan out perplexity calls in parallel for all public profiles."""
    if not profiles:
        return []
    tasks = [
        perplexity_analyze_social_profile(
            p.get("platform", ""), p.get("url", ""),
            handle=p.get("handle", ""),
            role=p.get("role", "own"),
        )
        for p in profiles
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: List[dict] = []
    for p, r in zip(profiles, results):
        if isinstance(r, Exception):
            logger.warning("Profile analysis failed for %s: %s", p.get("url"), r)
            continue
        text = (r or "").strip()
        if not text:
            continue
        out.append({
            "profile_id": p.get("id"),
            "platform": p.get("platform"),
            "url": p.get("url"),
            "handle": p.get("handle"),
            "role": p.get("role"),
            "text": text,
            "synthesized_at": datetime.now(timezone.utc).isoformat(),
        })
    return out


SYNTHESIS_SYSTEM_PROMPT = (
    "You are a senior social media brand strategist. Distill the input into "
    "a tight, actionable STYLE GUIDE. Return STRICT JSON, no prose, no code "
    "fences. Be concrete: name colors, quote phrases, give counts. The guide "
    "will be injected into the system prompt of AI agents that generate "
    "social posts and visuals — so every field must be directly actionable."
)

SYNTHESIS_SCHEMA_HINT = """
Return JSON with EXACTLY these string keys (no nesting, no arrays):
{
  "tone_voice": "3–5 adjectives + one-sentence gestalt",
  "vocabulary": "favorite phrases / signature words / slang to use",
  "do_not_use": "words, phrases, or topics to avoid",
  "post_structure": "typical length, hook patterns, CTA shape, hashtag/emoji usage, line breaks",
  "themes": "topics that consistently land",
  "image_video_style": "color palette, composition, illustration vs photo, motion, typography",
  "cadence": "posting frequency, best-performing post types, day/time patterns if visible",
  "summary": "one paragraph that an agent can read once and internalize"
}
Every field is REQUIRED. Use empty string if truly nothing applicable.
"""


async def _synthesize_structured_guide(
    *, profiles: List[dict], analyses: List[dict],
    samples: str, org_soul: dict,
    LlmChat, UserMessage, EMERGENT_LLM_KEY: str,
) -> dict:
    """Run the LLM to fold all sources into a structured guide dict."""
    if not analyses and not samples.strip():
        return _empty_guide()

    parts: List[str] = []
    if org_soul:
        soul_lines = []
        for k, label in [("mission", "Mission"), ("audience", "Audience"),
                          ("tone", "Existing org tone"), ("values", "Values"),
                          ("north_star", "North star")]:
            if org_soul.get(k):
                soul_lines.append(f"- {label}: {org_soul[k]}")
        if soul_lines:
            parts.append("ORG SOUL (must align with):\n" + "\n".join(soul_lines))

    own_chunks = [a["text"] for a in analyses if a.get("role") == "own"]
    insp_chunks = [a["text"] for a in analyses if a.get("role") == "inspiration"]
    if own_chunks:
        parts.append("OUR OWN PROFILES — current voice:\n\n" + "\n\n---\n\n".join(own_chunks))
    if insp_chunks:
        parts.append("INSPIRATION PROFILES — voice to emulate:\n\n" + "\n\n---\n\n".join(insp_chunks))
    if samples.strip():
        parts.append("MANUAL SAMPLES (user pasted existing or desired posts):\n\n" + samples.strip())

    parts.append(SYNTHESIS_SCHEMA_HINT)

    chat = LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=f"style-guide:{uuid.uuid4()}",
        system_message=SYNTHESIS_SYSTEM_PROMPT,
    ).with_model("openai", "gpt-4.1-mini")

    try:
        raw = await chat.send_message(UserMessage(text="\n\n".join(parts)))
    except Exception as exc:
        logger.warning("Style guide LLM error: %s", exc)
        return _empty_guide()

    raw = (raw or "").strip()
    # Strip code fences if the model added any
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        # Last resort: try to find a JSON object substring
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        try:
            data = json.loads(m.group(0)) if m else {}
        except (ValueError, TypeError, AttributeError):
            data = {}

    out = _empty_guide()
    for k in out.keys():
        v = data.get(k, "")
        if isinstance(v, list):
            v = "; ".join(str(x) for x in v if x)
        out[k] = (v or "").strip()[:4000]
    return out


# ── Prompt injection helpers (called from server.py / social_posts.py) ─────
def format_guide_for_prompt(guide: Optional[dict]) -> str:
    """Compact, agent-readable rendering of the style guide. Empty string if
    the guide is empty / missing — caller can drop it in unconditionally."""
    if not guide or not isinstance(guide, dict):
        return ""
    fields = [
        ("tone_voice", "Tone & voice"),
        ("vocabulary", "Vocabulary patterns"),
        ("do_not_use", "Do not use"),
        ("post_structure", "Post structure"),
        ("themes", "Recurring themes"),
        ("image_video_style", "Image / video style"),
        ("cadence", "Cadence"),
    ]
    lines: List[str] = []
    for k, label in fields:
        v = (guide.get(k) or "").strip()
        if v:
            lines.append(f"- {label}: {v}")
    summary = (guide.get("summary") or "").strip()
    if not lines and not summary:
        return ""
    block = "\n".join([
        "SOCIAL STYLE GUIDE — apply this to any customer-facing copy and any visuals you generate:",
        *lines,
    ])
    if summary:
        block += f"\n- In short: {summary}"
    return block


async def load_style_guide(db, org_id: str) -> Optional[dict]:
    """Cheap fetch — returns the org's `style_guide.guide` sub-doc or None."""
    try:
        org = await db.organizations.find_one(
            {"_id": ObjectId(org_id)},
            {"style_guide": 1},
        )
    except Exception:
        return None
    sg = (org or {}).get("style_guide") or {}
    return sg.get("guide") or None


# ── HTTP router ────────────────────────────────────────────────────────────
def make_style_guide_router(*, db, AGENTS, EMERGENT_LLM_KEY,
                            LlmChat, UserMessage,
                            require_org_member, get_current_user):
    r = APIRouter(prefix="/api")

    def _serialize(sg: dict) -> dict:
        if not sg:
            sg = _empty_style_doc()
        ga = sg.get("generated_at")
        return {
            "profiles": sg.get("profiles") or [],
            "samples": sg.get("samples") or "",
            "guide": sg.get("guide") or _empty_guide(),
            "analyses_count": len(sg.get("analyses") or []),
            "generated_at": ga.isoformat() if isinstance(ga, datetime) else ga,
            "is_manually_edited": bool(sg.get("is_manually_edited")),
            "perplexity_available": perplexity_available(),
        }

    async def _get_org(org_id: str) -> dict:
        try:
            org = await db.organizations.find_one({"_id": ObjectId(org_id)})
        except Exception:
            org = None
        if not org:
            raise HTTPException(404, "Organization not found")
        return org

    @r.get("/orgs/{org_id}/style-guide")
    async def get_guide(org_id: str, u: dict = Depends(get_current_user)):
        await require_org_member(org_id, u["id"])
        org = await _get_org(org_id)
        return _serialize(org.get("style_guide") or _empty_style_doc())

    @r.post("/orgs/{org_id}/style-guide/profiles")
    async def add_profile(org_id: str, body: ProfileBody,
                          u: dict = Depends(get_current_user)):
        await require_org_member(org_id, u["id"])
        plat = body.platform.strip().lower()
        if plat not in ALLOWED_PLATFORMS:
            raise HTTPException(400, f"Unknown platform: {plat}")
        if not body.url.strip() and not body.handle.strip():
            raise HTTPException(400, "Provide a URL or a handle")
        new_profile = {
            "id": str(uuid.uuid4()),
            "platform": plat,
            "url": body.url.strip(),
            "handle": body.handle.strip().lstrip("@"),
            "role": body.role,
            "notes": body.notes.strip(),
            "added_at": datetime.now(timezone.utc).isoformat(),
        }
        await db.organizations.update_one(
            {"_id": ObjectId(org_id)},
            {"$push": {"style_guide.profiles": new_profile}},
        )
        # If the org doesn't have a style_guide doc yet, the $push above
        # auto-creates the path; but `samples`/`guide`/etc may still be
        # missing. Backfill defaults so reads are predictable.
        await db.organizations.update_one(
            {"_id": ObjectId(org_id), "style_guide.guide": {"$exists": False}},
            {"$set": {
                "style_guide.guide": _empty_guide(),
                "style_guide.samples": "",
                "style_guide.analyses": [],
                "style_guide.is_manually_edited": False,
            }},
        )
        org = await _get_org(org_id)
        return _serialize(org.get("style_guide") or _empty_style_doc())

    @r.delete("/orgs/{org_id}/style-guide/profiles/{profile_id}")
    async def remove_profile(org_id: str, profile_id: str,
                             u: dict = Depends(get_current_user)):
        await require_org_member(org_id, u["id"])
        await db.organizations.update_one(
            {"_id": ObjectId(org_id)},
            {"$pull": {
                "style_guide.profiles": {"id": profile_id},
                "style_guide.analyses": {"profile_id": profile_id},
            }},
        )
        org = await _get_org(org_id)
        return _serialize(org.get("style_guide") or _empty_style_doc())

    @r.put("/orgs/{org_id}/style-guide/samples")
    async def update_samples(org_id: str, body: SamplesBody,
                             u: dict = Depends(get_current_user)):
        await require_org_member(org_id, u["id"])
        await db.organizations.update_one(
            {"_id": ObjectId(org_id)},
            {"$set": {"style_guide.samples": (body.samples or "").strip()}},
        )
        org = await _get_org(org_id)
        return _serialize(org.get("style_guide") or _empty_style_doc())

    @r.post("/orgs/{org_id}/style-guide/synthesize")
    async def synthesize(org_id: str, u: dict = Depends(get_current_user)):
        """Run Perplexity over each profile (in parallel), then ask the LLM
        to distill the analyses + manual samples into a structured guide.

        Returns 422 with a clear message if nothing to analyze or if all
        Perplexity calls came back empty AND no manual samples are present.
        """
        await require_org_member(org_id, u["id"])
        org = await _get_org(org_id)
        sg = org.get("style_guide") or _empty_style_doc()
        profiles = sg.get("profiles") or []
        samples = (sg.get("samples") or "").strip()

        if not profiles and not samples:
            raise HTTPException(422,
                "Add at least one profile or paste sample posts before synthesizing.")

        analyses: List[dict] = []
        pplx_warn = ""
        if profiles:
            if not perplexity_available():
                pplx_warn = "Perplexity isn't configured — only manual samples will be used."
            else:
                analyses = await _gather_profile_analyses(profiles)
                if not analyses and not samples:
                    raise HTTPException(422,
                        "Couldn't read any of the supplied profiles. Some platforms "
                        "block automated lookups — paste a few sample posts in the "
                        "Samples box and try again.")

        guide = await _synthesize_structured_guide(
            profiles=profiles, analyses=analyses, samples=samples,
            org_soul=org.get("soul") or {},
            LlmChat=LlmChat, UserMessage=UserMessage,
            EMERGENT_LLM_KEY=EMERGENT_LLM_KEY,
        )

        now = datetime.now(timezone.utc)
        await db.organizations.update_one(
            {"_id": ObjectId(org_id)},
            {"$set": {
                "style_guide.guide": guide,
                "style_guide.analyses": analyses,
                "style_guide.generated_at": now,
                "style_guide.is_manually_edited": False,
            }},
        )
        org = await _get_org(org_id)
        out = _serialize(org.get("style_guide") or _empty_style_doc())
        if pplx_warn:
            out["warning"] = pplx_warn
        return out

    @r.patch("/orgs/{org_id}/style-guide")
    async def patch_guide(org_id: str, body: GuideEditBody,
                          u: dict = Depends(get_current_user)):
        await require_org_member(org_id, u["id"])
        updates: dict = {}
        guide_fields = {"tone_voice", "vocabulary", "do_not_use",
                        "post_structure", "themes",
                        "image_video_style", "cadence", "summary"}
        for k, v in body.model_dump(exclude_unset=True).items():
            if v is None:
                continue
            if k == "samples":
                updates["style_guide.samples"] = (v or "").strip()
            elif k in guide_fields:
                updates[f"style_guide.guide.{k}"] = (v or "").strip()
        if not updates:
            org = await _get_org(org_id)
            return _serialize(org.get("style_guide") or _empty_style_doc())
        updates["style_guide.is_manually_edited"] = True
        await db.organizations.update_one(
            {"_id": ObjectId(org_id)}, {"$set": updates},
        )
        org = await _get_org(org_id)
        return _serialize(org.get("style_guide") or _empty_style_doc())

    return r


__all__ = [
    "make_style_guide_router",
    "format_guide_for_prompt",
    "load_style_guide",
]
