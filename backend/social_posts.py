"""Social posts queue — Generate & Queue MVP.

Self-contained router + helpers for the "generate social content with agents,
queue it for the user to one-click publish" feature. No platform OAuth — we
deep-link into each platform's web composer where possible, and fall back to
copy-text + download-media for IG / TikTok / YouTube which don't expose one.

Posts live in the ``social_posts`` Mongo collection with a tiny lifecycle:

  draft  → user generated, may still need editing
  queued → scheduled for a specific time (auto-promoted from draft by APScheduler)
  published → user clicked "Mark posted" or the auto-publisher (phase 2) ran
  skipped  → user dismissed without posting

The conversation→queue flow re-uses an existing agent reply (sibling of the
"continue privately" fork action). The composer flow asks an agent to draft N
posts in one shot and stages them as drafts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import List, Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Platforms we know how to handle. Order is intentional — UI renders pills in
# this sequence. Anything outside this set is rejected at API boundary.
SUPPORTED_PLATFORMS = (
    "twitter", "linkedin", "facebook", "instagram", "tiktok",
    "youtube", "pinterest", "threads", "bluesky", "mastodon", "reddit",
)

# Platforms with no web composer — frontend shows "Copy + download" instead of
# "Open in <platform>". Kept on the backend only as documentation; the actual
# deeplink rendering is in `frontend/.../SocialPage.js`.
NO_DEEPLINK_PLATFORMS = ("instagram", "tiktok", "youtube")


def _norm_platforms(raw: List[str]) -> List[str]:
    out: list[str] = []
    seen: set[str] = set()
    for p in raw or []:
        p = (p or "").strip().lower()
        if p in SUPPORTED_PLATFORMS and p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ── Pydantic bodies ─────────────────────────────────────────────────────────
class SocialDraftBody(BaseModel):
    prompt: str = Field(..., description="Topic / brief for the agent to draft from")
    platforms: List[str] = Field(default_factory=lambda: ["twitter"])
    count: int = Field(3, ge=1, le=10)
    agent_id: Optional[str] = None  # if omitted, smart-routed
    conversation_id: Optional[str] = None  # optional source thread


class SocialPostUpdateBody(BaseModel):
    text: Optional[str] = None
    platforms: Optional[List[str]] = None
    scheduled_for: Optional[str] = None  # ISO 8601, or "" to unschedule
    status: Optional[Literal["draft", "queued", "published", "skipped"]] = None
    hashtags: Optional[List[str]] = None
    link_url: Optional[str] = None


class GenerateImageBody(BaseModel):
    """Body for POST /social/posts/{id}/generate-image.

    Optional. If `prompt` is provided, replaces the default "post text" body
    in the image prompt — so users can hand-author or tweak what the image
    depicts without changing the post copy. The org's Soul + visual style
    still get prepended automatically.
    """
    prompt: str = Field("", max_length=2000)


class SuggestTopicsBody(BaseModel):
    """Body for POST /orgs/{id}/social/suggest-topics.

    Produces a batch of topic IDEAS (not drafts) calibrated to the org's
    Soul + current news in their space. Users pick one and it prefills
    the draft composer.
    """
    count: int = Field(6, ge=3, le=12)
    # Optional nudge toward a particular content angle. Left blank = mix.
    angle: Literal["", "news", "evergreen", "promotional", "community", "thought-leadership"] = ""
    # Optional free-text hint (e.g. "launching our annual whitepaper next
    # week, lean into that"). The suggester will bias toward it.
    hint: str = Field("", max_length=500)


# ── Serialization ───────────────────────────────────────────────────────────
def serialize_social_post(p: dict) -> dict:
    def _iso(v):
        if isinstance(v, datetime):
            return v.isoformat()
        return v or None
    return {
        "id": str(p["_id"]),
        "org_id": p["org_id"],
        "team_id": p.get("team_id", ""),
        "conversation_id": p.get("conversation_id", ""),
        "source_message_id": p.get("source_message_id", ""),
        "agent_id": p.get("agent_id", ""),
        "agent_name": p.get("agent_name", ""),
        "status": p.get("status", "draft"),
        "platforms": p.get("platforms", []),
        "text": p.get("text", ""),
        "hashtags": p.get("hashtags", []),
        "link_url": p.get("link_url", ""),
        "media": p.get("media", []),
        "image": p.get("image") or None,  # auto-generated cover image (with logo overlay)
        # "none" = nothing happening, "pending" = background gen in flight,
        # "ready" = image ready (also implies image is non-null), "failed" = gen errored.
        # Frontend uses this to render a shimmer skeleton while pending so
        # the user knows a cover is on the way after /draft returns.
        "image_status": p.get("image_status", "ready" if p.get("image") else "none"),
        "image_prompt": p.get("image_prompt", ""),  # user-authored override prompt
        "logo_position": p.get("logo_position") or "bottom-right",
        "scheduled_for": _iso(p.get("scheduled_for")),
        "published_at": _iso(p.get("published_at")),
        "created_at": _iso(p.get("created_at")),
        "created_by": p.get("created_by", ""),
        # Autonomous Mission-driven posting — see social_autopost.py
        "autopost_enabled": bool(p.get("autopost_enabled", False)),
        "autopost_platform": p.get("autopost_platform", ""),
        "autopost_status": p.get("autopost_status", ""),  # "" | pending | running | posted | failed | cancelled
        "autopost_mission_id": p.get("autopost_mission_id", ""),
        "autopost_error": p.get("autopost_error", ""),
        "autopost_last_run_at": _iso(p.get("autopost_last_run_at")),
    }


# ── LLM helper: ask an agent to draft N posts as JSON ───────────────────────
async def _draft_via_agent(
    *,
    LlmChat, UserMessage, EMERGENT_LLM_KEY,
    AGENTS: dict,
    agent_id: str,
    org_soul: dict,
    team_soulprint: dict,
    prompt: str,
    platforms: List[str],
    count: int,
    style_guide_block: str = "",
) -> List[dict]:
    """Returns a list of `{platform, text, hashtags}` dicts. Empty list on
    parse failure — caller decides whether to surface an error.

    Falls back to a single post containing the raw model output as text if the
    JSON parse fails — this prevents the user staring at "0 drafts generated"
    when the model adds prose around its JSON.
    """
    if agent_id not in AGENTS:
        raise HTTPException(400, "Agent not available")
    persona = AGENTS[agent_id]
    soul_lines = []
    for k in ("mission", "tone", "audience", "values", "north_star"):
        v = (org_soul or {}).get(k) or (team_soulprint or {}).get(k) or ""
        if v:
            soul_lines.append(f"- {k.replace('_', ' ').title()}: {v}")
    soul_block = "\n".join(soul_lines) or "(no Soul provided — be tasteful and on-brand by default)"

    pf_list = ", ".join(platforms)
    char_hints = []
    if "twitter" in platforms or "bluesky" in platforms or "threads" in platforms:
        char_hints.append("Twitter/Bluesky/Threads: ≤ 280 chars")
    if "linkedin" in platforms:
        char_hints.append("LinkedIn: 1–3 short paragraphs, hook first")
    if "instagram" in platforms:
        char_hints.append("Instagram: punchy caption + 5–10 hashtags")
    if "tiktok" in platforms:
        char_hints.append("TikTok: 1–2 sentences + 3–5 hashtags")
    char_hints_block = "\n".join(f"- {h}" for h in char_hints)

    system = (
        f"You are {persona['name']} ({persona.get('division','')}), drafting "
        f"social posts that match the company's Soul:\n{soul_block}\n\n"
        + (f"{style_guide_block}\n\n" if style_guide_block else "")
        + f"Output ONLY a JSON array of post objects, no markdown, no commentary. "
        f"Each object: "
        f'{{"platform":"<one of: ' + ",".join(platforms) + f'>","text":"...","hashtags":["#x","#y"]}}\n'
        f"Tailor each post to its platform:\n{char_hints_block}\n"
        f"Generate exactly {count} posts, distributed across the requested "
        f"platforms ({pf_list}). Never include the JSON keys in the post copy."
    )
    chat = LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=f"social-draft:{uuid.uuid4()}",
        system_message=system,
    ).with_model("openai", "gpt-4.1-mini")
    raw = (await chat.send_message(UserMessage(text=prompt))) or ""

    # Strip stray ```json fences and grab the first complete JSON array.
    # The earlier `\[.*?\]` non-greedy regex truncated arrays whose objects
    # contained nested braces (e.g. nested hashtag dicts) — we now bracket-
    # count to find the matching `]` so the whole array is parsed.
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(),
                     flags=re.IGNORECASE | re.MULTILINE)
    arr_text = ""
    start = cleaned.find("[")
    if start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(cleaned)):
            ch = cleaned[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    arr_text = cleaned[start:i + 1]
                    break
    if not arr_text:
        # Fallback: emit one draft per requested platform with the raw text
        logger.info("Social draft: agent did not return JSON, falling back")
        return [
            {"platform": p, "text": raw.strip()[:600], "hashtags": []}
            for p in platforms[:count]
        ]
    try:
        data = json.loads(arr_text)
    except json.JSONDecodeError:
        return [
            {"platform": p, "text": raw.strip()[:600], "hashtags": []}
            for p in platforms[:count]
        ]
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data[:count]:
        if not isinstance(item, dict):
            continue
        platform = (item.get("platform") or "").strip().lower()
        if platform not in platforms:
            # If the model made up a platform, snap it to the first requested
            platform = platforms[0]
        text = (item.get("text") or "").strip()[:1200]
        if not text:
            continue
        hashtags = item.get("hashtags") or []
        if isinstance(hashtags, str):
            hashtags = [h.strip() for h in re.split(r"[\s,]+", hashtags) if h.strip()]
        hashtags = [str(h)[:60] for h in hashtags if isinstance(h, (str, int))][:15]
        out.append({"platform": platform, "text": text, "hashtags": hashtags})
    return out


# ── Topic suggester ───────────────────────────────────────────────────────
async def _suggest_social_topics(
    *, org: dict, count: int, angle: str, hint: str,
    LlmChat, UserMessage, EMERGENT_LLM_KEY: str,
) -> List[dict]:
    """Return a list of topic ideas tailored to the org's Soul + current news.

    Strategy:
      1. Pull the org's Soul (mission/audience/tone/values) so topics feel
         on-brand and relevant to what the business actually cares about.
      2. Query Perplexity for recent (pw = past week) news in whatever
         space the Soul implies. Perplexity output feeds back in with
         citations, giving the suggester live grounding.
      3. Ask GPT-4.1-mini to synthesise N specific topic ideas as JSON —
         each a headline + 1-sentence pitch + angle tag + optional source
         URL so the user can click through and verify before drafting.

    Output shape (per topic):
        {headline, pitch, angle, tags: [], source_url, source_title}
    """
    soul = org.get("soul") or {}
    name = (org.get("name") or "").strip() or "the company"
    mission = (soul.get("mission") or "").strip()
    audience = (soul.get("audience") or "").strip()
    tone = (soul.get("tone") or "").strip()
    values = (soul.get("values") or "").strip()
    summary = (soul.get("summary") or "").strip()

    # Build a search query from whatever Soul we have. Prefer the richest
    # signal — mission usually contains the industry vertical.
    seed = mission or summary or audience or name
    news_query = (
        f"Latest news, trends, and launches in the space relevant to "
        f"'{seed[:200]}'. Focus on developments in the past week."
    )

    # Live-news grounding via Perplexity. Gracefully degrade to "" when
    # the integration is unavailable (same pattern as mission mode).
    news_blurb = ""
    news_sources: List[dict] = []
    try:
        from perplexity_client import perplexity_web_search, is_available as _p_avail  # type: ignore
        if _p_avail():
            results = await perplexity_web_search(
                news_query, count=6, freshness="pw",
            )
            if results:
                news_sources = [
                    {"title": r.get("title") or "", "url": r.get("url") or ""}
                    for r in results if r.get("url")
                ][:6]
                news_blurb = "\n".join(
                    f"- {r.get('title') or r.get('url')}: {(r.get('description') or r.get('snippet') or '')[:200]}"
                    for r in results[:6]
                )
    except Exception as exc:
        logger.info("Topic suggester: news grounding skipped (%s)", exc)

    angle_line = (
        f"Bias roughly 60% of topics toward the '{angle}' angle; the rest "
        f"can be any angle that fits."
        if angle else
        "Mix angles across the set — some news-reactive, some evergreen "
        "thought-leadership, at least one community-oriented or behind-the-scenes."
    )
    hint_line = f"\nAdditional user direction: {hint}" if hint else ""

    sys_msg = (
        "You are a senior social media strategist. Your job is to propose "
        f"{count} distinct, actually-postable topic IDEAS for a company to "
        "share on social, tailored to its Soul and current news in its "
        "space. Output ONLY a single JSON array of objects on one line, "
        "no markdown, no prose outside the array. Each object MUST have "
        "keys: headline (6-14 words, concrete, avoids clickbait), pitch "
        "(1 sentence, ~25 words, explains the angle), angle (one of: "
        "news, evergreen, promotional, community, thought-leadership), "
        "tags (array of 2-4 short tags, no # prefix), source_url (string, "
        "the URL the topic is inspired by — empty string if not tied to "
        "a specific article), source_title (string, matching the URL's "
        "article title — empty string if source_url is empty).\n"
        "Constraints:\n"
        "  • Headlines must read like something the company would ACTUALLY "
        "    post — specific, grounded, no hype words like 'game-changing'.\n"
        "  • Do NOT invent URLs. If you reference a source, it MUST be one "
        "    of the URLs from the news brief; otherwise leave source_url "
        "    empty.\n"
        "  • Align the tone with the Soul: if tone is 'warm and practical', "
        "    don't propose corporate-speak topics.\n"
        f"  • {angle_line}"
    )
    user_payload = (
        f"COMPANY: {name}\n"
        f"MISSION: {mission or '(not captured)'}\n"
        f"AUDIENCE: {audience or '(not captured)'}\n"
        f"TONE: {tone or '(not captured)'}\n"
        f"VALUES: {values or '(not captured)'}\n"
        f"{('SUMMARY: ' + summary + chr(10)) if summary else ''}"
        f"{hint_line}\n\n"
        f"CURRENT NEWS BRIEF (past week, use as optional grounding — "
        f"do NOT force every topic to be news-pegged):\n"
        f"{news_blurb or '(no live news available — lean on evergreen angles)'}\n\n"
        f"Produce the JSON array of {count} topic ideas now."
    )
    try:
        chat = LlmChat(
            api_key=EMERGENT_LLM_KEY,
            session_id=f"topic-suggest:{uuid.uuid4()}",
            system_message=sys_msg,
        ).with_model("openai", "gpt-4.1-mini")
        raw = (await chat.send_message(UserMessage(text=user_payload))) or ""
    except Exception as exc:
        logger.warning("Topic suggester LLM error: %s", exc)
        raise HTTPException(502, "Topic suggester temporarily unavailable. Try again in a moment.") from exc

    # Parse the JSON array out of the raw response. Models sometimes wrap
    # in markdown fences or prose — be forgiving.
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        logger.info("Topic suggester returned no JSON array; raw=%r", raw[:300])
        raise HTTPException(502, "Topic suggester returned an unexpected response. Try again.")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise HTTPException(502, "Topic suggester returned malformed JSON. Try again.") from exc
    if not isinstance(data, list):
        raise HTTPException(502, "Topic suggester returned the wrong shape.")

    valid_urls = {s["url"] for s in news_sources if s.get("url")}
    valid_angles = {"news", "evergreen", "promotional", "community", "thought-leadership"}
    out: List[dict] = []
    for item in data[:count]:
        if not isinstance(item, dict):
            continue
        headline = (item.get("headline") or "").strip()[:180]
        if not headline:
            continue
        pitch = (item.get("pitch") or "").strip()[:400]
        item_angle = (item.get("angle") or "").strip().lower()
        if item_angle not in valid_angles:
            item_angle = "evergreen"
        tags_raw = item.get("tags") or []
        if isinstance(tags_raw, str):
            tags_raw = [t.strip() for t in re.split(r"[\s,]+", tags_raw) if t.strip()]
        tags = [str(t).lstrip("#").strip()[:40] for t in tags_raw if isinstance(t, (str, int))][:6]
        src_url = (item.get("source_url") or "").strip()
        src_title = (item.get("source_title") or "").strip()[:200]
        # Model-hallucinated URLs get dropped — we only keep ones we can
        # verify came back from Perplexity's citation set.
        if src_url and src_url not in valid_urls:
            src_url = ""
            src_title = ""
        out.append({
            "id": str(uuid.uuid4()),
            "headline": headline,
            "pitch": pitch,
            "angle": item_angle,
            "tags": tags,
            "source_url": src_url,
            "source_title": src_title,
        })
    if not out:
        raise HTTPException(502, "Topic suggester returned no usable topics. Try again.")
    return out


# ── Router factory ──────────────────────────────────────────────────────────
def make_social_router(*, db, AGENTS, EMERGENT_LLM_KEY, LlmChat, UserMessage,
                       require_org_member, get_current_user, route_best_agent,
                       load_conv_or_403,
                       generate_image_via_nano_banana=None,
                       compose_logo_on_image=None,
                       upload_dir=None,
                       recover_bytes_from_gridfs=None):
    """Build the /api/social router with closure-bound dependencies from server.py.

    ``recover_bytes_from_gridfs`` is an async callable that, given an
    on-disk filename, returns ``(bytes, mime, original_name)`` from
    MongoDB GridFS — or ``(None, None, None)`` if we have no mirror.
    Used to restore brand-asset / image files that were lost when a
    Kubernetes pod restart wiped ephemeral disk storage. Without this
    rehydration, the user gets "no logo available" / "image missing"
    errors even though the source is safely persisted in Mongo.
    """
    from bson import ObjectId as _OID
    from pathlib import Path as _Path
    r = APIRouter(prefix="/api")

    async def _ensure_local(filename: str) -> Optional["_Path"]:
        """Return a local Path for ``filename``, rehydrating from GridFS
        if the disk copy was wiped by a redeploy. Returns None if the
        file doesn't exist on disk AND can't be recovered from GridFS
        (genuinely lost — e.g. user deleted the brand asset).
        """
        if not filename or not upload_dir:
            return None
        p = _Path(upload_dir) / filename
        if p.is_file():
            return p
        # Try GridFS rehydration
        if recover_bytes_from_gridfs is None:
            return None
        try:
            blob, _mime, _orig = await recover_bytes_from_gridfs(filename)
        except Exception as exc:
            logger.warning("GridFS rehydrate failed for %s: %s", filename, exc)
            return None
        if not blob:
            return None
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(blob)
            logger.info("Rehydrated %s from GridFS (%d bytes)", filename, len(blob))
            return p
        except Exception as exc:
            logger.warning("Failed to write rehydrated %s to disk: %s", filename, exc)
            return None

    async def _generate_post_image(post: dict, org_doc: dict, override_prompt: str = "") -> Optional[dict]:
        """Generate a cover image for a single post using Nano Banana, then
        composite the org's primary logo on top at the chosen anchor.
        Returns ``{filename, url, with_logo}`` or None on failure.

        Image prompt = org Soul (mission/tone/audience/values) + post text →
        keeps every social image visually on-brand. If ``override_prompt``
        is provided (user hand-authored or edited the prompt), it REPLACES
        the post-text body while keeping the Soul + visual-style context
        so the image still matches the brand.
        """
        if not generate_image_via_nano_banana or not upload_dir:
            return None
        soul = (org_doc or {}).get("soul") or {}
        ba = (org_doc or {}).get("brand_assets") or {}
        # Compose the soul context. We deliberately keep it short — Nano Banana
        # follows its own composition heuristics best with concise direction.
        soul_bits: list[str] = []
        if soul.get("tone"):
            soul_bits.append(f"Tone: {soul['tone']}")
        if soul.get("audience"):
            soul_bits.append(f"Audience: {soul['audience']}")
        if soul.get("mission"):
            soul_bits.append(f"Mission: {soul['mission']}")
        colors = ba.get("brand_colors") or []
        if colors:
            soul_bits.append(f"Brand palette: {', '.join(colors[:4])}")
        visual_style = (ba.get("visual_style") or "").strip()
        if visual_style:
            # User-authored visual direction — this overrides the model's
            # default tendency toward generic stock imagery and keeps a
            # consistent look across every post the org ever drafts.
            soul_bits.append(f"Visual style: {visual_style[:400]}")
        # Visual style synthesized from the org's social profiles (if any)
        sg_guide = ((org_doc or {}).get("style_guide") or {}).get("guide") or {}
        sg_visual = (sg_guide.get("image_video_style") or "").strip()
        if sg_visual:
            soul_bits.append(f"Synthesized visual style (match this): {sg_visual[:600]}")
        sys_msg = (
            "You generate a single 1:1 social post cover image. The image must "
            "feel on-brand for the company described below. Avoid text inside "
            "the image — typography is added separately. Output only the image."
        )
        body_text = (override_prompt or "").strip() or (post.get("text") or "").strip()[:600]
        prompt_parts = [
            "Create a striking, on-brand 1:1 cover image illustrating the "
            "social post copy below. Vivid, bold, professional.",
            "\n--- Company brand ---",
            *soul_bits,
            "\n--- Post copy / user direction ---",
            body_text,
        ]
        prompt = "\n".join(p for p in prompt_parts if p)
        try:
            # Phase 1 of the Dynamic Intelligence image router (Feb 5
            # 2026): social-post cover images route to kie.ai's Flux
            # Kontext Pro by default. Why:
            #   • Cleaner "marketing-grade" aesthetic out of the box
            #     than Nano Banana, with comparable cost.
            #   • Decouples social cover gen from chat editing flows
            #     (which keep Nano Banana — it's still the best at
            #     image-to-image edits).
            #   • The kie.ai client is already in our infra (used for
            #     video) so adding image traffic here doesn't expand
            #     our dependency surface.
            # Falls back to Nano Banana on ANY kie.ai failure (404, 5xx,
            # timeout, missing key, empty result) so the user always
            # gets a cover image regardless of kie.ai's uptime.
            fname, source_model = await _generate_post_image_via_kie_then_fallback(
                prompt=prompt, system_message=sys_msg,
            )
        except Exception as exc:
            logger.warning("Social image generation failed: %s", exc)
            return None
        # Composite the primary logo onto the freshly-generated image.
        # We ALWAYS try to re-apply the logo on every (re)generation so users
        # don't see naked images after editing their prompt. The only time
        # with_logo=False is when the org has no primary logo uploaded OR
        # the compose step raised an exception (logged below).
        primary = (ba.get("primary_logo") or {}) if ba else {}
        logo_filename = (primary or {}).get("filename") or ""
        with_logo = False
        final_filename = fname
        if not logo_filename:
            logger.info(
                "Social image: org %s has no primary_logo — returning raw image",
                org_doc.get("_id") if org_doc else "?",
            )
        elif compose_logo_on_image is None:
            logger.warning("Social image: compose_logo_on_image helper missing — skipping overlay")
        else:
            try:
                # Rehydrate from GridFS if either file got wiped by a
                # pod restart. This is the production "no logo
                # available" fix — disk is volatile, GridFS is durable.
                base_path = await _ensure_local(fname)
                logo_path = await _ensure_local(logo_filename)
                if base_path is None:
                    logger.warning(
                        "Social image: base file %s missing on disk AND in GridFS",
                        fname,
                    )
                elif logo_path is None:
                    logger.warning(
                        "Social image: logo file %s missing on disk AND in GridFS "
                        "(brand asset truly lost — owner needs to re-upload)",
                        logo_filename,
                    )
                else:
                    composed = compose_logo_on_image(
                        base_path, logo_path,
                        position=post.get("logo_position") or "bottom-right",
                        out_dir=_Path(upload_dir),
                    )
                    final_filename = composed.name
                    with_logo = True
                    logger.info(
                        "Social image: composed logo onto %s → %s",
                        fname, final_filename,
                    )
            except Exception as exc:
                logger.warning("Logo composite failed (using raw image): %s", exc)
        return {
            "filename": final_filename,
            "url": f"/api/files/{final_filename}",
            "with_logo": with_logo,
            "raw_filename": fname,  # un-composited base, kept so we can re-anchor logo cheaply
            "model_used": source_model,  # "kie:flux-kontext-pro" or "nano-banana" — admin trail
        }

    async def _generate_post_image_via_kie_then_fallback(
        *, prompt: str, system_message: str,
    ) -> tuple[str, str]:
        """Phase-1 Dynamic Intelligence image router for social-post covers.

        Tries kie.ai's Flux Kontext Pro first. Falls back to Nano Banana
        on any failure (timeout, 4xx, 5xx, missing key, empty result).
        Returns ``(filename, model_label)``. Raises only if BOTH backends
        fail, so the caller can surface a single clear error.

        Why this layout (kie-first, NB fallback) — not the reverse:
          • Switching on success is a no-op (fast path stays fast).
          • Falling back on failure means the user always gets a cover.
          • If kie.ai is having a bad day (Suno's already 404ing in our
            logs), social-post drafts don't go out unstyled.
        """
        from kie_client import generate_via_kie, kie_provider_info  # local import keeps boot light
        # kie.ai's Flux Kontext doesn't take a system message — fold it
        # into the prompt so the brand direction still lands.
        full_prompt = f"{system_message.strip()}\n\n{prompt.strip()}" if system_message else prompt
        try:
            if kie_provider_info("flux-kontext-pro"):
                urls = await generate_via_kie(
                    model_key="flux-kontext-pro", prompt=full_prompt,
                )
                if urls:
                    # Download the image and persist locally so we have a
                    # stable filename. kie.ai URLs expire in 7-14 days
                    # which would break old social posts.
                    import httpx
                    async with httpx.AsyncClient(timeout=30) as client:
                        resp = await client.get(urls[0])
                    if resp.status_code == 200 and resp.content:
                        filename = f"{uuid.uuid4().hex}.png"
                        from pathlib import Path
                        if upload_dir:
                            target = Path(upload_dir) / filename
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(resp.content)
                        # Mirror to GridFS so a pod restart doesn't lose
                        # the image. The persist helper is the same one
                        # the rest of the app uses for media durability.
                        try:
                            from server import persist_bytes  # late import, app startup ordering
                            await persist_bytes(
                                Path(upload_dir) / filename, resp.content,
                                original_name=filename, mime="image/png",
                            )
                        except Exception:
                            pass  # GridFS mirror is best-effort
                        return filename, "kie:flux-kontext-pro"
                    logger.warning("kie.ai returned URL but download failed (%s)", resp.status_code)
                else:
                    logger.warning("kie.ai flux-kontext-pro returned no URLs — falling back")
        except Exception as exc:
            logger.warning("kie.ai social-cover gen failed: %s — falling back to Nano Banana", exc)
        # Fallback: Nano Banana (the original path). Any failure here
        # propagates up so the outer try/except reports it once cleanly.
        fname, _caption = await generate_image_via_nano_banana(prompt, system_message)
        return fname, "nano-banana"

    @r.post("/orgs/{org_id}/social/draft")
    async def draft_social_posts(
        org_id: str, body: SocialDraftBody, u: dict = Depends(get_current_user),
    ):
        ctx = await require_org_member(org_id, u["id"])
        platforms = _norm_platforms(body.platforms)
        if not platforms:
            raise HTTPException(422, "Pick at least one supported platform")
        if not body.prompt.strip():
            raise HTTPException(422, "Prompt required")

        # Decide which agent drafts. If caller passed an agent_id, honour it.
        # Otherwise smart-route — the "social media strategist" wins for most
        # social copy briefs but the user might be after a crisper PR voice.
        agent_id = (body.agent_id or "").strip()
        team_soulprint: dict = {}
        if not agent_id and route_best_agent is not None:
            try:
                # No team context here — use a synthetic team dict so router
                # sees an empty soulprint and falls back to its built-in heuristics.
                synth_team = {"_id": ObjectId(), "soulprint": {}, "agent_ids": list(AGENTS.keys())[:30]}
                agent_id = await route_best_agent(synth_team, body.prompt, exclude_ids=None, transcript="")
            except Exception as exc:
                logger.warning("social draft router error: %s", exc)
                agent_id = ""
        if agent_id not in AGENTS:
            agent_id = "marketing-social-media-strategist" if "marketing-social-media-strategist" in AGENTS else next(iter(AGENTS))

        org_soul = (ctx["org"] or {}).get("soul") or {}
        # Pull the org's social style guide and fold it into the draft prompt
        # so every generated post matches the brand voice synthesized from
        # the org's profiles + samples.
        try:
            from style_guide import format_guide_for_prompt as _fmt_sg
            sg_doc = (ctx["org"] or {}).get("style_guide") or {}
            style_guide_block = _fmt_sg(sg_doc.get("guide") or {})
        except Exception:
            style_guide_block = ""
        try:
            posts = await _draft_via_agent(
                LlmChat=LlmChat, UserMessage=UserMessage, EMERGENT_LLM_KEY=EMERGENT_LLM_KEY,
                AGENTS=AGENTS, agent_id=agent_id, org_soul=org_soul,
                team_soulprint=team_soulprint, prompt=body.prompt,
                platforms=platforms, count=body.count,
                style_guide_block=style_guide_block,
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("social draft generation failed")
            raise HTTPException(502, f"Could not generate drafts: {exc}") from exc

        if not posts:
            raise HTTPException(502, "Agent returned no usable drafts. Try a more specific prompt.")

        now = datetime.now(timezone.utc)
        docs: list[dict] = []
        agent_meta = AGENTS[agent_id]
        # Hybrid image strategy: generate ONE on-brand cover image for the
        # first post in the batch (so users have something visual to react
        # to immediately) and let them hit "Generate image" on the rest.
        # Saves on Nano Banana cost (~$0.04 each) for big multi-post drafts.
        #
        # Important: the image gen is dispatched as a BACKGROUND task so
        # the endpoint returns the text drafts in ~8s instead of waiting
        # the additional ~6-10s for Nano Banana + logo composite. The
        # frontend polls `/social/posts` and the cover image appears
        # in the UI when it lands. This is the same fire-and-forget
        # pattern used for chat /generate-media.
        for idx, p in enumerate(posts):
            doc = {
                "org_id": org_id,
                "team_id": "",
                "conversation_id": (body.conversation_id or "").strip(),
                "source_message_id": "",
                "agent_id": agent_id,
                "agent_name": agent_meta["name"],
                "status": "draft",
                "platforms": [p["platform"]],
                "text": p["text"],
                "hashtags": p.get("hashtags", []),
                "link_url": "",
                "media": [],
                "image": None,
                # When we kick off background image gen, mark it pending
                # so the frontend can show a shimmer skeleton in place of
                # the cover-image card.
                "image_status": "pending" if idx == 0 else "none",
                # Stamp the resolved body prompt up front so the editor
                # has something to show even before the image lands.
                "image_prompt": (p["text"] or "").strip()[:600] if idx == 0 else "",
                "logo_position": "bottom-right",
                "scheduled_for": None,
                "published_at": None,
                "created_at": now,
                "created_by": u["id"],
            }
            res = await db.social_posts.insert_one(doc)
            doc["_id"] = res.inserted_id
            docs.append(doc)

        # Fire-and-forget the first-post image gen. We capture the
        # inserted post id + a snapshot of the org doc so the worker
        # doesn't need to re-fetch (avoids racing a brand-asset edit).
        first_post_id = docs[0]["_id"] if docs else None
        if first_post_id is not None:
            org_snapshot = ctx["org"]
            post_snapshot = dict(docs[0])
            post_snapshot.pop("_id", None)

            async def _bg_first_image():
                try:
                    img = await _generate_post_image(post_snapshot, org_snapshot)
                except Exception as exc:
                    logger.warning("Background first-post image gen failed: %s", exc)
                    img = None
                update: dict = {"image_status": "ready" if img else "failed"}
                if img:
                    update["image"] = img
                await db.social_posts.update_one(
                    {"_id": first_post_id}, {"$set": update},
                )
            # Don't await — let the response return immediately.
            asyncio.create_task(_bg_first_image())

        return {"posts": [serialize_social_post(d) for d in docs]}

    @r.post("/orgs/{org_id}/social/suggest-topics")
    async def suggest_social_topics(
        org_id: str,
        body: SuggestTopicsBody = Body(default_factory=SuggestTopicsBody),
        u: dict = Depends(get_current_user),
    ):
        """Suggest a batch of on-brand topic IDEAS (not full drafts) based
        on the org's Soul + current news. Users pick one and it prefills
        the draft composer. Cheap — one Perplexity call + one LLM call,
        no image generation."""
        await require_org_member(org_id, u["id"])
        org = await db.organizations.find_one({"_id": _OID(org_id)})
        if not org:
            raise HTTPException(404, "Org not found")
        topics = await _suggest_social_topics(
            org=org,
            count=body.count,
            angle=body.angle,
            hint=body.hint.strip(),
            LlmChat=LlmChat, UserMessage=UserMessage,
            EMERGENT_LLM_KEY=EMERGENT_LLM_KEY,
        )
        return {"topics": topics}

    @r.get("/orgs/{org_id}/social/posts")
    async def list_social_posts(
        org_id: str, status: Optional[str] = None,
        u: dict = Depends(get_current_user),
    ):
        await require_org_member(org_id, u["id"])
        q: dict = {"org_id": org_id}
        if status:
            statuses = [s.strip() for s in status.split(",") if s.strip()]
            if statuses:
                q["status"] = {"$in": statuses}
        cur = db.social_posts.find(q).sort("created_at", -1)
        out = [serialize_social_post(p) async for p in cur]
        return out

    @r.patch("/social/posts/{post_id}")
    async def update_social_post(
        post_id: str, body: SocialPostUpdateBody,
        u: dict = Depends(get_current_user),
    ):
        try:
            post = await db.social_posts.find_one({"_id": ObjectId(post_id)})
        except Exception as exc:
            raise HTTPException(400, "Invalid post id") from exc
        if not post:
            raise HTTPException(404, "Post not found")
        await require_org_member(post["org_id"], u["id"])
        update: dict = {}
        if body.text is not None:
            update["text"] = body.text.strip()[:1200]
        if body.platforms is not None:
            pf = _norm_platforms(body.platforms)
            if not pf:
                raise HTTPException(422, "At least one supported platform required")
            update["platforms"] = pf
        if body.hashtags is not None:
            update["hashtags"] = [str(h).strip()[:60] for h in body.hashtags if str(h).strip()][:15]
        if body.link_url is not None:
            update["link_url"] = body.link_url.strip()[:500]
        if body.scheduled_for is not None:
            sf = body.scheduled_for.strip()
            if not sf:
                update["scheduled_for"] = None
            else:
                try:
                    # Accept "...Z" suffix as ISO/UTC
                    parsed = datetime.fromisoformat(sf.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise HTTPException(422, "scheduled_for must be ISO 8601") from exc
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                update["scheduled_for"] = parsed
                # Auto-flip status if the user is staging a future post and
                # didn't explicitly set status. Drafts that get a time become
                # "queued" so they show up in the right tab.
                if body.status is None and post.get("status") == "draft":
                    update["status"] = "queued"
        if body.status is not None:
            update["status"] = body.status
            if body.status == "published":
                update["published_at"] = datetime.now(timezone.utc)
        if not update:
            return serialize_social_post(post)
        await db.social_posts.update_one({"_id": ObjectId(post_id)}, {"$set": update})
        fresh = await db.social_posts.find_one({"_id": ObjectId(post_id)})
        return serialize_social_post(fresh)

    @r.delete("/social/posts/{post_id}")
    async def delete_social_post(post_id: str, u: dict = Depends(get_current_user)):
        try:
            post = await db.social_posts.find_one({"_id": ObjectId(post_id)})
        except Exception as exc:
            raise HTTPException(400, "Invalid post id") from exc
        if not post:
            raise HTTPException(404, "Post not found")
        await require_org_member(post["org_id"], u["id"])
        await db.social_posts.delete_one({"_id": ObjectId(post_id)})
        return {"ok": True}

    @r.post("/conversations/{conv_id}/messages/{msg_id}/queue-social")
    async def queue_message_as_social(
        conv_id: str, msg_id: str,
        u: dict = Depends(get_current_user),
    ):
        """One-click queue: take an agent reply from a chat thread and stage
        it as a draft social post — `platforms` defaults to twitter and the
        user picks the rest in the queue UI. Sibling of `/fork`.
        """
        conv = await load_conv_or_403(conv_id, u["id"])
        msg = await db.messages.find_one({"id": msg_id, "conversation_id": conv_id})
        if not msg:
            raise HTTPException(404, "Message not found")
        if msg.get("sender_type") != "agent":
            raise HTTPException(400, "Only agent replies can be queued for social")
        agent_id = msg.get("agent_id") or ""
        if agent_id not in AGENTS:
            raise HTTPException(400, "Source agent is no longer available")
        # Resolve the org via the conversation's team
        team = await db.teams.find_one({"_id": ObjectId(conv["team_id"])})
        if not team:
            raise HTTPException(404, "Team not found")
        # Carry over media if the agent reply included an image/video
        media: list[dict] = []
        if msg.get("media_type") in ("image", "video") and msg.get("media_url"):
            media.append({
                "url": msg["media_url"],
                "kind": msg["media_type"],
                "caption": msg.get("media_caption", ""),
            })
        for att in (msg.get("attachments") or []):
            if isinstance(att, dict) and att.get("kind") in ("image", "video") and att.get("url"):
                media.append({
                    "url": att["url"],
                    "kind": att["kind"],
                    "caption": att.get("original_name", ""),
                })

        now = datetime.now(timezone.utc)
        doc = {
            "org_id": team["org_id"],
            "team_id": str(team["_id"]),
            "conversation_id": conv_id,
            "source_message_id": msg_id,
            "agent_id": agent_id,
            "agent_name": AGENTS[agent_id]["name"],
            "status": "draft",
            "platforms": ["twitter"],
            "text": (msg.get("text") or "").strip()[:1200],
            "hashtags": [],
            "link_url": "",
            "media": media,
            "scheduled_for": None,
            "published_at": None,
            "created_at": now,
            "created_by": u["id"],
        }
        res = await db.social_posts.insert_one(doc)
        doc["_id"] = res.inserted_id
        return serialize_social_post(doc)

    @r.post("/social/posts/{post_id}/generate-image")
    async def generate_post_image(
        post_id: str,
        body: GenerateImageBody = Body(default_factory=GenerateImageBody),
        u: dict = Depends(get_current_user),
    ):
        """On-demand cover image generation. Optional `prompt` body lets
        the user override the image direction (same Soul + visual-style
        scaffold, different creative body). The override is persisted on
        the post as `image_prompt` so subsequent regenerations keep it
        unless the user clears or re-edits it.
        """
        try:
            post = await db.social_posts.find_one({"_id": _OID(post_id)})
        except Exception as exc:
            raise HTTPException(400, "Invalid post id") from exc
        if not post:
            raise HTTPException(404, "Post not found")
        await require_org_member(post["org_id"], u["id"])
        org = await db.organizations.find_one({"_id": _OID(post["org_id"])})
        override = (body.prompt or "").strip()
        # Fall back to the persisted image_prompt from a previous edit
        effective_prompt = override or (post.get("image_prompt") or "")
        img = await _generate_post_image(post, org or {}, effective_prompt)
        if not img:
            raise HTTPException(502, "Could not generate image. Try again in a moment.")
        update = {"image": img}
        # Persist the resolved body prompt on EVERY successful generation,
        # not just when the user explicitly typed an override. This way the
        # "Edit prompt" affordance always shows the actual text Nano Banana
        # used (be it the user's override, a previously-saved override, or
        # the auto-derived post copy). Before this fix, auto-generated
        # posts had `image_prompt = ""` and the editor opened blank, leaving
        # users guessing what was sent.
        resolved_body = (
            override
            or (post.get("image_prompt") or "").strip()
            or (post.get("text") or "").strip()[:600]
        )
        if resolved_body:
            update["image_prompt"] = resolved_body
        await db.social_posts.update_one(
            {"_id": _OID(post_id)}, {"$set": update},
        )
        fresh = await db.social_posts.find_one({"_id": _OID(post_id)})
        return serialize_social_post(fresh)

    @r.post("/social/posts/{post_id}/use-asset")
    async def use_existing_asset_as_image(
        post_id: str,
        asset_id: str = Body(..., embed=True),
        u: dict = Depends(get_current_user),
    ):
        """Phase 2 — attach an org-owned Media Library asset as the post's
        cover image. No LLM call; we just copy the reference + re-apply
        the org's primary logo via the usual compose step."""
        try:
            post = await db.social_posts.find_one({"_id": _OID(post_id)})
        except Exception as exc:
            raise HTTPException(400, "Invalid post id") from exc
        if not post:
            raise HTTPException(404, "Post not found")
        await require_org_member(post["org_id"], u["id"])
        try:
            asset = await db.assets.find_one({"_id": _OID(asset_id), "org_id": post["org_id"]})
        except Exception as exc:
            raise HTTPException(400, "Invalid asset id") from exc
        if not asset or asset.get("kind") != "image":
            raise HTTPException(404, "Asset not found or not an image")

        # Point the post at the asset file directly — same capability URL
        # pattern as generated images. We still run the logo compositor
        # so branding stays consistent, but we keep the raw asset path
        # intact so reposition/remix can go back to the un-branded source.
        src_filename = asset["filename"]
        from pathlib import Path as _Path
        # upload_dir is /app/backend/uploads; assets live at
        # /app/backend/uploads/assets/{org_id}/{filename}.
        assets_dir = _Path(upload_dir) / "assets" / post["org_id"]
        src_path = assets_dir / src_filename
        if not src_path.is_file():
            # Fallback for cases where assets live elsewhere; serve handler
            # will still resolve via GridFS.
            pass

        # Copy into uploads/ so the composer can read it; new random name
        # so future deletions of the source asset don't blow up the post.
        import shutil as _shutil
        import uuid as _uuid
        dest_name = f"{_uuid.uuid4().hex}.{asset.get('ext') or 'png'}"
        dest_path = _Path(upload_dir) / dest_name
        try:
            _shutil.copyfile(src_path, dest_path)
        except Exception as exc:
            logger.warning("Failed to copy asset into uploads/: %s", exc)
            raise HTTPException(500, "Could not attach this asset")

        org = await db.organizations.find_one({"_id": _OID(post["org_id"])})
        ba = (org or {}).get("brand_assets") or {}
        primary = (ba.get("primary_logo") or {}) if ba else {}
        logo_filename = (primary or {}).get("filename") or ""
        final_filename = dest_name
        with_logo = False
        if logo_filename and compose_logo_on_image:
            try:
                # Rehydrate the logo from GridFS if disk was wiped.
                logo_path = await _ensure_local(logo_filename)
                if logo_path is not None:
                    composed = compose_logo_on_image(
                        dest_path, logo_path,
                        position=post.get("logo_position") or "bottom-right",
                    )
                    if composed:
                        final_filename = composed.name
                        with_logo = True
            except Exception as exc:
                logger.warning("Asset logo compose failed: %s", exc)

        image = {
            "filename": final_filename,
            "raw_filename": dest_name,  # un-branded base for reposition/remix
            "url": f"/api/files/{final_filename}",
            "with_logo": with_logo,
            "source": "asset",
            "source_asset_id": str(asset["_id"]),
        }
        await db.social_posts.update_one(
            {"_id": _OID(post_id)},
            {"$set": {"image": image, "image_updated_at": datetime.now(timezone.utc)}},
        )
        fresh = await db.social_posts.find_one({"_id": _OID(post_id)})
        return serialize_social_post(fresh)

    @r.post("/social/posts/{post_id}/remix-asset")
    async def remix_asset_into_cover(
        post_id: str,
        body: dict = Body(...),
        u: dict = Depends(get_current_user),
    ):
        """Phase 3 — use an existing asset as the BASE image and send it +
        a prompt to Nano Banana for an image-to-image remix (new
        background, lifestyle variant, platform retarget, etc.). Body:

            { "asset_id": str, "prompt": str }
        """
        asset_id = (body.get("asset_id") or "").strip()
        prompt = (body.get("prompt") or "").strip()
        if not asset_id or not prompt:
            raise HTTPException(422, "asset_id and prompt are both required")
        try:
            post = await db.social_posts.find_one({"_id": _OID(post_id)})
        except Exception as exc:
            raise HTTPException(400, "Invalid post id") from exc
        if not post:
            raise HTTPException(404, "Post not found")
        await require_org_member(post["org_id"], u["id"])
        try:
            asset = await db.assets.find_one({"_id": _OID(asset_id), "org_id": post["org_id"]})
        except Exception as exc:
            raise HTTPException(400, "Invalid asset id") from exc
        if not asset or asset.get("kind") != "image":
            raise HTTPException(404, "Asset not found or not an image")

        from pathlib import Path as _Path
        assets_dir = _Path(upload_dir) / "assets" / post["org_id"]
        src_path = assets_dir / asset["filename"]
        if not src_path.is_file():
            raise HTTPException(404, "Source asset file missing on disk")

        org = await db.organizations.find_one({"_id": _OID(post["org_id"])})
        # Persist the user's prompt so subsequent "Regenerate" calls keep
        # intent; mirrors the override flow in /generate-image.
        await db.social_posts.update_one(
            {"_id": _OID(post_id)},
            {"$set": {"image_prompt": prompt}},
        )
        # Fold Soul + visual-style into the prompt so the remix stays
        # on-brand. Nano Banana composes best with concise direction; we
        # cap each segment.
        soul = (org or {}).get("soul") or {}
        brand_bits: list[str] = []
        if soul.get("tone"): brand_bits.append(f"Tone: {soul['tone']}")
        if soul.get("audience"): brand_bits.append(f"Audience: {soul['audience']}")
        ba = (org or {}).get("brand_assets") or {}
        if (ba.get("visual_style") or "").strip():
            brand_bits.append(f"Visual style: {ba['visual_style'][:300]}")
        full_prompt = (
            "Using the SOURCE IMAGE provided as the base, create a new on-brand "
            "variation. Keep the subject/product clearly recognizable from the "
            "source image.\n\n"
            f"--- User direction ---\n{prompt}\n\n"
            + ("--- Brand context ---\n" + "\n".join(brand_bits) if brand_bits else "")
        ).strip()
        sys_msg = (
            "You are a brand-aware image artist. Produce ONE photograph/"
            "illustration that fits the user's direction while keeping the "
            "source subject visually coherent. Square 1:1 by default unless the "
            "user asks otherwise. High quality, vivid, on-brand."
        )
        try:
            fname, _caption = await generate_image_via_nano_banana(
                full_prompt, sys_msg, input_image_paths=[src_path],
            )
        except Exception as exc:
            logger.warning("Asset remix failed: %s", exc)
            raise HTTPException(502, "Remix failed — try a different prompt or check back in a moment")

        # Reapply logo (same compose pattern as /generate-image).
        primary = (ba.get("primary_logo") or {}) if ba else {}
        logo_filename = (primary or {}).get("filename") or ""
        final_filename = fname
        with_logo = False
        if logo_filename and compose_logo_on_image:
            try:
                # Rehydrate base + logo from GridFS if disk was wiped.
                base_path = await _ensure_local(fname)
                logo_path = await _ensure_local(logo_filename)
                if base_path is not None and logo_path is not None:
                    composed = compose_logo_on_image(
                        base_path, logo_path,
                        position=post.get("logo_position") or "bottom-right",
                    )
                    if composed:
                        final_filename = composed.name
                        with_logo = True
            except Exception as exc:
                logger.warning("Remix logo compose failed: %s", exc)

        image = {
            "filename": final_filename,
            "raw_filename": fname,
            "url": f"/api/files/{final_filename}",
            "with_logo": with_logo,
            "source": "asset_remix",
            "source_asset_id": str(asset["_id"]),
        }
        await db.social_posts.update_one(
            {"_id": _OID(post_id)},
            {"$set": {"image": image, "image_updated_at": datetime.now(timezone.utc)}},
        )
        fresh = await db.social_posts.find_one({"_id": _OID(post_id)})
        return serialize_social_post(fresh)

    @r.patch("/social/posts/{post_id}/logo-position")
    async def reposition_logo(
        post_id: str, position: str, u: dict = Depends(get_current_user),
    ):
        """Reposition the logo overlay without re-generating the base image.

        We always keep `image.raw_filename` (un-composited) on disk so this
        is a cheap PIL paste — no LLM call. For legacy posts generated
        before we started persisting `raw_filename`, we fall back to the
        currently-displayed image as the base (which in those cases is
        ALSO un-composited because logo compositing failed — same file).
        """
        if position not in ("top-left", "top-right", "bottom-left", "bottom-right", "center"):
            raise HTTPException(422, "Invalid position")
        try:
            post = await db.social_posts.find_one({"_id": _OID(post_id)})
        except Exception as exc:
            raise HTTPException(400, "Invalid post id") from exc
        if not post:
            raise HTTPException(404, "Post not found")
        await require_org_member(post["org_id"], u["id"])
        update: dict = {"logo_position": position}
        org = await db.organizations.find_one({"_id": _OID(post["org_id"])})
        ba = (org or {}).get("brand_assets") or {}
        primary = (ba.get("primary_logo") or {}) if ba else {}
        logo_filename = (primary or {}).get("filename") or ""
        existing_img = post.get("image") or {}
        # Prefer the un-composited raw when we have it (modern posts),
        # otherwise fall back to the current image.filename — in legacy
        # posts with with_logo=False, image.filename IS the un-composited
        # image (because compositing failed at generation time). Using the
        # current composed file as the base would cause double-branding,
        # which we explicitly guard against.
        base_filename = existing_img.get("raw_filename") or ""
        if not base_filename and not existing_img.get("with_logo"):
            base_filename = existing_img.get("filename") or ""
        if not existing_img:
            raise HTTPException(
                422,
                "This post has no generated image yet — use 'Generate cover "
                "image' first, then position the logo.",
            )
        if not logo_filename:
            raise HTTPException(
                422,
                "Your organization has no primary logo uploaded. Go to the "
                "Org page and upload one, then try again.",
            )
        if not base_filename:
            raise HTTPException(
                422,
                "Can't re-apply the logo — this image is already composited "
                "and the un-composited source is no longer on file. Click "
                "'Regenerate' to produce a fresh image with the logo.",
            )
        if compose_logo_on_image is None or not upload_dir:
            raise HTTPException(500, "Logo composite helper unavailable.")
        # Rehydrate from GridFS so a redeploy that wiped ephemeral pod
        # storage doesn't show "Your logo file is missing" — the user's
        # logo IS safe in Mongo, we just need to restore the disk copy.
        logo_path = await _ensure_local(logo_filename)
        base_path = await _ensure_local(base_filename)
        if logo_path is None:
            raise HTTPException(
                422,
                "Your logo file is missing from storage — please re-upload "
                "the primary logo on the Org page.",
            )
        if base_path is None:
            raise HTTPException(
                422,
                "The original image file is missing from storage. Click "
                "'Regenerate' to produce a fresh image.",
            )
        try:
            composed = compose_logo_on_image(
                base_path, logo_path, position=position,
                out_dir=_Path(upload_dir),
            )
        except Exception as exc:
            logger.warning("Reposition logo failed: %s", exc)
            raise HTTPException(500, f"Couldn't apply logo: {exc}") from exc
        update["image"] = {
            "filename": composed.name,
            "url": f"/api/files/{composed.name}",
            "with_logo": True,
            "raw_filename": base_filename,
        }
        await db.social_posts.update_one({"_id": _OID(post_id)}, {"$set": update})
        fresh = await db.social_posts.find_one({"_id": _OID(post_id)})
        return serialize_social_post(fresh)

    return r


# ── APScheduler hook: promote queued posts whose time has come ──────────────
async def promote_due_drafts(*, db) -> int:
    """Promote any `queued` posts past their scheduled_for to status="ready".

    NOTE: in MVP we don't auto-publish — we just mark them as "ready" so the
    queue UI surfaces them at the top of the Scheduled tab. Returns the count
    of promoted posts.
    """
    now = datetime.now(timezone.utc)
    res = await db.social_posts.update_many(
        {
            "status": "queued",
            "scheduled_for": {"$lte": now},
            "ready_at": {"$exists": False},
        },
        {"$set": {"ready_at": now}},
    )
    if res.modified_count:
        logger.info("Marked %d social posts ready to publish", res.modified_count)
    return res.modified_count
