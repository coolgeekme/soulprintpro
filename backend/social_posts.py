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
import os
import re
import uuid
from datetime import datetime, timezone
from typing import List, Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# Module-level hook for the cover-image generator. Set inside
# `make_social_posts_router` when the social router is built, so other
# subsystems (e.g. the mission engine's `draft_to_social_queue` tool)
# can kick off Nano Banana cover-image generation on freshly-created
# drafts without duplicating the 100+ line `_generate_post_image`
# closure. None until the router boots; mission tool calls become
# no-ops if the router hasn't been built yet (boot-order safety).
_POST_IMAGE_CALLBACK = None  # type: ignore[assignment]


async def kickoff_post_image_generation(*, db, post_id: str) -> bool:
    """Public entry point — kicks off background cover-image generation
    for an existing draft. Used by the mission tool after drafting posts
    so each card lands in the queue with a brand-consistent cover image.

    Returns True if generation was scheduled, False if the post / org
    couldn't be loaded or the image callback isn't registered.
    """
    if _POST_IMAGE_CALLBACK is None:
        logger.info(
            "kickoff_post_image_generation: callback not registered yet — "
            "skipping for post %s",
            post_id,
        )
        return False
    try:
        from bson import ObjectId as _OID
        post = await db.social_posts.find_one({"_id": _OID(post_id)})
        if not post:
            return False
        # Flip the post into pending so SocialPage's polling loop picks
        # up the eventual ready/error state.
        await db.social_posts.update_one(
            {"_id": _OID(post_id)},
            {"$set": {
                "image_status": "pending",
                "image_pending_at": datetime.now(timezone.utc),
            }, "$unset": {"image_error": ""}},
        )
        org = await db.organizations.find_one({"_id": _OID(post["org_id"])})

        async def _run():
            try:
                img = await _POST_IMAGE_CALLBACK(post, org or {}, "")
                if not img:
                    raise RuntimeError("image generator returned empty result")
                await db.social_posts.update_one(
                    {"_id": _OID(post_id)},
                    {"$set": {
                        "image": img,
                        "image_status": "ready",
                        "image_updated_at": datetime.now(timezone.utc),
                    }, "$unset": {"image_error": "", "image_pending_at": ""}},
                )
            except Exception as exc:
                logger.warning(
                    "Mission-kickoff image gen failed for post %s: %s",
                    post_id, exc,
                )
                await db.social_posts.update_one(
                    {"_id": _OID(post_id)},
                    {"$set": {
                        "image_status": "error",
                        "image_error": "Generation failed — tap Regenerate to retry.",
                    }, "$unset": {"image_pending_at": ""}},
                )

        asyncio.create_task(_run())
        return True
    except Exception as exc:
        logger.warning(
            "kickoff_post_image_generation failed for post %s: %s",
            post_id, exc,
        )
        return False

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
    # Optional one-off visual-style override applied to every post drafted
    # in this batch. When provided, REPLACES the brand-default visual style
    # segments in the cover-image prompt (so a one-off infographic / 3D
    # render / flat illustration request actually beats the brand default).
    # Stays on each draft as `image_style_override` so later "Regenerate"
    # calls keep the same look unless the user clears it.
    style_override: str = Field("", max_length=1000)


class SocialPostUpdateBody(BaseModel):
    text: Optional[str] = None
    platforms: Optional[List[str]] = None
    scheduled_for: Optional[str] = None  # ISO 8601, or "" to unschedule
    status: Optional[Literal["draft", "queued", "publishing", "published", "partial", "failed", "skipped"]] = None
    hashtags: Optional[List[str]] = None
    link_url: Optional[str] = None
    auto_publish: Optional[bool] = None  # when True, scheduled posts fire Composio publish automatically


class GenerateImageBody(BaseModel):
    """Body for POST /social/posts/{id}/generate-image.

    Optional. If `prompt` is provided, replaces the default "post text" body
    in the image prompt — so users can hand-author or tweak what the image
    depicts without changing the post copy. The org's Soul + visual style
    still get prepended automatically.

    `style_override` is a separate, persistent override of the *visual
    aesthetic* (e.g. "Flat infographic with bold typography, callout stats,
    no people"). When set, REPLACES the org's brand-default visual-style
    segments so a one-off infographic / 3D render / illustration actually
    beats the brand default. Pass `clear_style_override: true` to drop
    the per-post override and fall back to the brand default.
    """
    prompt: str = Field("", max_length=2000)
    style_override: Optional[str] = Field(None, max_length=1000)
    clear_style_override: bool = False


# Video models we expose on the Social Queue UI. Subset of the Kie.ai
# marketplace plus the legacy Veo paths. The labels are surfaced in the
# frontend dropdown.
SOCIAL_VIDEO_MODELS: dict[str, str] = {
    "veo3_fast":      "Veo 3.1 (fast · default)",
    "veo3":           "Veo 3.1 (full · highest fidelity)",
    "kling-3":        "Kling 3 (great for product shots)",
    "kling-3-pro":    "Kling 3 Pro (slower, more detail)",
    "seedance-1-5":   "Seedance 1.5 (stylised / animation)",
    "wan-2-6":        "Wan 2.6 (long-form / scenes)",
    "sora-2":         "Sora 2 (narrative)",
    "runway-aleph":   "Runway Aleph (cinematic)",
}

DEFAULT_SOCIAL_VIDEO_MODEL = "veo3_fast"


class GenerateVideoBody(BaseModel):
    """Body for POST /social/posts/{id}/generate-video.

    `prompt` — optional override (defaults to post copy when blank).
    `model` — one of SOCIAL_VIDEO_MODELS keys.
    `aspect_ratio` — "Auto" (derive from platform), "16:9", "9:16", or "1:1".
                     IG Reels / TikTok / YT Shorts → 9:16 · YT/LinkedIn → 16:9 · IG feed → 1:1.
    """
    prompt: str = Field("", max_length=2000)
    model: str = Field(DEFAULT_SOCIAL_VIDEO_MODEL, max_length=40)
    aspect_ratio: str = Field("Auto", max_length=8)


class GenerateMusicVideoBody(BaseModel):
    """Body for POST /social/posts/{id}/generate-music-video.

    One of `song_url` / `song_prompt` is required (we need a song to
    sync to). When `song_prompt` is set we mint a Suno track first and
    use it as the audio bed. `vibe_prompt` shapes the Kling 2.1 Master
    image-to-video clip. The post's existing cover `image.url` is used
    as the visual seed — no separate field needed."""
    song_url: str = Field("", max_length=2000)
    song_prompt: str = Field("", max_length=1500)
    vibe_prompt: str = Field("", max_length=1500)
    # Optional override cap on the output length. Defaults to FULL
    # song duration (user explicitly chose Option "no cap" — see the
    # Feb 2026 handoff).
    max_seconds: Optional[float] = Field(None, ge=3, le=900)



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
        # User-authored visual-style override for THIS post only. When set,
        # the image prompt swaps the brand-default `Visual style:` / `Synthesized
        # visual style:` segments for this string — so a one-off infographic
        # / 3D render request actually wins over the brand default.
        "image_style_override": p.get("image_style_override", ""),
        "logo_position": p.get("logo_position") or "bottom-right",
        # ── Video cover (new) ────────────────────────────────────────
        # Independent from `image`: a post can have either, both, or
        # neither. UI shows the video player when present, image
        # otherwise, "no cover" placeholder when both are missing.
        "video": p.get("video") or None,
        "video_status": p.get("video_status", "ready" if p.get("video") else "none"),
        "video_prompt": p.get("video_prompt", ""),
        "video_model": p.get("video_model", ""),
        "video_aspect_ratio": p.get("video_aspect_ratio", ""),
        "video_error": p.get("video_error", ""),
        # ── Music video (Kling 2.1 Master + ffmpeg song-mux) ─────────
        # Independent from `image` / `video` — a single post can in
        # theory carry an image cover, a video cover AND a music
        # video; UI currently surfaces just the music video player
        # when present (it implies its own visual).
        "music_video": p.get("music_video") or None,
        "music_video_status": p.get(
            "music_video_status",
            "ready" if p.get("music_video") else "none",
        ),
        "music_video_error": p.get("music_video_error", ""),
        "music_video_song_prompt": p.get("music_video_song_prompt", ""),
        "music_video_vibe_prompt": p.get("music_video_vibe_prompt", ""),
        "scheduled_for": _iso(p.get("scheduled_for")),
        "published_at": _iso(p.get("published_at")),
        # Live-publishing telemetry (set by social_publishing.publish_post).
        # `publish_results` is {platform: {ok, tool, response, error}} so the
        # UI can render per-platform success/failure chips on each card.
        "publish_results": p.get("publish_results") or {},
        "published_count": int(p.get("published_count") or 0),
        "failed_count": int(p.get("failed_count") or 0),
        "publish_started_at": _iso(p.get("publish_started_at")),
        "publish_finished_at": _iso(p.get("publish_finished_at")),
        "auto_publish": bool(p.get("auto_publish", True)),  # default: live-publish when ready
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

    # ── Resilient LLM call ─────────────────────────────────────────────
    # Three failure modes seen in production:
    #   (a) Transient OpenAI 5xx / rate-limit
    #   (b) gpt-4.1-mini slow run that drifts past the production-ingress
    #       proxy's 60-100s timeout, killing the HTTP request
    #   (c) Empty / malformed model output that the JSON parser below
    #       can't recover
    # We mitigate (a)+(b) here with: 50s per-attempt timeout, ONE retry,
    # and a faster fallback model. (c) is handled by the parser below.
    _MODEL_ATTEMPTS = [
        ("openai", "gpt-4.1-mini", 50.0),  # primary
        ("openai", "gpt-4.1-mini", 50.0),  # immediate retry on same model
        ("openai", "gpt-4.1-nano", 35.0),  # cheaper + faster fallback
    ]
    raw = ""
    last_exc: Optional[Exception] = None
    for provider, model, timeout_s in _MODEL_ATTEMPTS:
        try:
            chat = LlmChat(
                api_key=EMERGENT_LLM_KEY,
                session_id=f"social-draft:{uuid.uuid4()}",
                system_message=system,
            ).with_model(provider, model)
            raw = await asyncio.wait_for(
                chat.send_message(UserMessage(text=prompt)),
                timeout=timeout_s,
            )
            raw = (raw or "").strip()
            if raw:
                break
            last_exc = RuntimeError(f"{model} returned empty body")
        except asyncio.TimeoutError as exc:
            last_exc = exc
            logger.warning("Social draft: %s timed out after %ss", model, timeout_s)
        except Exception as exc:
            last_exc = exc
            logger.warning("Social draft: %s failed (%s)", model, exc)
    if not raw:
        # All attempts failed — surface a precise error the caller can
        # convert into a clean toast.
        kind = (
            "Timeout — try again in a few seconds; the AI provider was slow."
            if isinstance(last_exc, asyncio.TimeoutError)
            else f"AI provider hiccup: {last_exc}"
        )
        raise HTTPException(503, kind)

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
                       recover_bytes_from_gridfs=None,
                       persist_bytes=None,
                       kie_generate_video=None,
                       kie_submit_kie_task=None,
                       kie_wait_for_kie_task=None,
                       kie_provider_info=None,
                       save_remote_url_to_uploads=None):
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

    async def _mirror_composed_to_gridfs(composed_path: "_Path") -> None:
        """Mirror a freshly-composited social cover to GridFS.

        `compose_logo_on_image` writes only to disk; without this mirror,
        a production pod restart wipes the composed file and the post's
        `image.url` 404s forever (the raw un-composited bytes are mirrored
        by `persist_bytes` inside the generator, but the composite is not).
        Best-effort — never raises; the post is still usable from disk
        until the next redeploy.
        """
        if persist_bytes is None or composed_path is None:
            return
        try:
            if not composed_path.is_file():
                return
            content = composed_path.read_bytes()
            await persist_bytes(
                composed_path, content,
                original_name=composed_path.name,
                mime="image/png",
            )
        except Exception as exc:
            logger.warning(
                "Failed to mirror composed image %s to GridFS: %s",
                composed_path.name, exc,
            )

    async def _generate_post_image(post: dict, org_doc: dict, override_prompt: str = "") -> Optional[dict]:
        """Generate a cover image for a single post using Nano Banana, then
        composite the org's primary logo on top at the chosen anchor.
        Returns ``{filename, url, with_logo}`` or None on failure.

        Image prompt = org Soul (mission/tone/audience/values) + post text →
        keeps every social image visually on-brand. If ``override_prompt``
        is provided (user hand-authored or edited the prompt), it REPLACES
        the post-text body while keeping the Soul + visual-style context
        so the image still matches the brand.

        Per-post `image_style_override` (when set on the post doc) REPLACES
        the brand-default ``Visual style:`` / ``Synthesized visual style:``
        segments — letting a one-off infographic / 3D render / flat
        illustration request actually beat the brand default. Everything
        else (tone, audience, palette, logo) stays in place so the override
        only flips the *aesthetic*, not the brand identity.
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
        # Per-post style override takes precedence over org defaults.
        style_override = (post.get("image_style_override") or "").strip()
        if style_override:
            # Strong directive — tell the model this is a hard requirement
            # so it doesn't quietly defer back to its training-data default
            # (which is usually "tasteful photo" no matter what we ask for).
            soul_bits.append(
                f"Visual style (HARD OVERRIDE — match exactly, ignore brand defaults): {style_override[:800]}"
            )
        else:
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
                    # Mirror composed bytes to GridFS so the post survives
                    # a production pod redeploy (raw was already mirrored
                    # by `persist_bytes` inside the generator; composite is
                    # not, hence the explicit call here).
                    await _mirror_composed_to_gridfs(composed)
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
            "model_used": source_model,  # "nano-banana" (default) or "kie:flux-kontext-pro" (fallback) — admin trail
        }

    # Register the closure as the module-level callback so subsystems
    # outside this router (e.g. the mission engine's
    # `draft_to_social_queue` tool) can kick off cover-image generation
    # on a freshly-created draft. See `kickoff_post_image_generation`
    # at the top of this file.
    global _POST_IMAGE_CALLBACK
    _POST_IMAGE_CALLBACK = _generate_post_image

    async def _generate_post_image_via_kie_then_fallback(
        *, prompt: str, system_message: str,
    ) -> tuple[str, str]:
        """Phase-1 Dynamic Intelligence image router for social-post covers.

        Tries kie.ai's Flux Kontext Pro first. Falls back to Nano Banana
        on any failure (timeout, 4xx, 5xx, missing key, empty result).
        Returns ``(filename, model_label)``. Raises only if BOTH backends
        fail, so the caller can surface a single clear error.

        Why Nano Banana primary (May 2026 onwards):
          • Social posts frequently bake brand text into the cover (CTA,
            tagline, product name). Flux Kontext often produces "almost
            right" typography with subtle spelling errors — unfit for
            published content. Nano Banana's text rendering is the
            current best-in-class for image-with-text generation, so we
            promoted it to primary on user feedback.
          • Flux stays as the safety-net so a Nano Banana outage still
            yields a cover instead of a broken draft.
        """
        # Try Nano Banana first — best typography in the field.
        try:
            fname, _caption = await generate_image_via_nano_banana(prompt, system_message)
            if fname:
                return fname, "nano-banana"
            logger.warning("Nano Banana returned no filename — falling back to Flux")
        except Exception as exc:
            logger.warning("Nano Banana social-cover gen failed: %s — falling back to Flux", exc)

        # Fallback: kie.ai Flux Kontext Pro. Same pipeline as before
        # (download + GridFS mirror) since their URLs expire in ~7-14 days.
        from kie_client import generate_via_kie, kie_provider_info  # local import keeps boot light
        full_prompt = f"{system_message.strip()}\n\n{prompt.strip()}" if system_message else prompt
        try:
            if kie_provider_info("flux-kontext-pro"):
                urls = await generate_via_kie(
                    model_key="flux-kontext-pro", prompt=full_prompt,
                )
                if urls:
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
                        try:
                            from server import persist_bytes  # late import, app startup ordering
                            await persist_bytes(
                                Path(upload_dir) / filename, resp.content,
                                original_name=filename, mime="image/png",
                            )
                        except Exception:
                            pass
                        return filename, "kie:flux-kontext-pro"
                    logger.warning("kie.ai returned URL but download failed (%s)", resp.status_code)
                else:
                    logger.warning("kie.ai flux-kontext-pro returned no URLs")
        except Exception as exc:
            logger.warning("kie.ai social-cover fallback also failed: %s", exc)
        # Both backends failed — surface a clean error.
        raise RuntimeError(
            "Image generation unavailable — both Nano Banana and Flux "
            "Kontext fallback failed. Please try again in a moment.",
        )

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
        # Trial-cap enforcement (Jun 2026): drafting N posts consumes
        # N "messages" + 1 "images" (first draft auto-generates a
        # cover image; subsequent posts only get images on explicit
        # regenerate). Raises 402 if the user is on trial and over
        # their allowance — paid / perpetual users bypass.
        from trial_caps import consume_or_raise as _consume
        await _consume(db, u, "messages", amount=body.count)
        await _consume(db, u, "images", amount=1)

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
            # Best-effort error notification — never blocks the response.
            try:
                from error_reporter import fire_and_forget as _report_err
                _report_err(
                    kind="social_draft",
                    org_id=org_id,
                    user_email=u.get("email"),
                    user_id=u.get("id"),
                    message=str(exc),
                    exc=exc,
                    extra={
                        "prompt": (body.prompt or "")[:300],
                        "platforms": ",".join(platforms or []),
                        "count": body.count,
                    },
                )
            except Exception:
                pass
            # Network-class errors get a friendlier toast — these are the
            # "random" failures the user reported. "AI provider hiccup"
            # is verbatim copy the FE renders so they know to retry.
            raise HTTPException(503, f"AI provider hiccup: {exc}") from exc

        if not posts:
            raise HTTPException(503, "AI provider returned no usable drafts — try again with a more specific prompt or retry in a moment.")

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
                # Per-post visual-style override — applied to EVERY post in
                # this batch (so a single "make these infographics" request
                # styles all 5 drafts the same way). User can edit per-post
                # afterwards via the Edit Prompt modal.
                "image_style_override": (body.style_override or "").strip(),
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

    @r.get("/orgs/{org_id}/social/publishable-platforms")
    async def publishable_platforms(
        org_id: str, u: dict = Depends(get_current_user),
    ):
        """For every platform Foundry can publish to directly, return
        whether the caller has an active connection, plus FB/IG
        target-pin state so the SocialPage UI can surface "Posting to: X"
        chips with a Change link. Drives the "Publish via Foundry"
        button visibility on SocialPage."""
        await require_org_member(org_id, u["id"])
        try:
            from social_publishing import (
                connectable_platforms_for_user, supported_platforms,
                meta_target_status_for_user,
            )
            connected = await connectable_platforms_for_user(db, org_id, u["id"])
            meta_targets = await meta_target_status_for_user(db, org_id, u["id"])
        except Exception:
            logger.exception("publishable-platforms lookup failed")
            return {"supported": [], "connected": {}, "meta": {}}
        return {
            "supported": supported_platforms(),
            "connected": connected,
            "meta": meta_targets,
        }

    @r.post("/social/posts/{post_id}/publish")
    async def publish_social_post(
        post_id: str, u: dict = Depends(get_current_user),
    ):
        """Kick off a live publish via Composio. Runs as a background
        task so the request returns immediately and the UI flips to
        'Publishing…' — final per-platform results land on the post
        document over the next few seconds.
        """
        try:
            post = await db.social_posts.find_one({"_id": _OID(post_id)})
        except Exception:
            raise HTTPException(404, "Post not found")
        if not post:
            raise HTTPException(404, "Post not found")
        # Trial cap: each publish call consumes 1 "publishes" credit
        # regardless of platform count (a multi-platform post still
        # represents one user "send" intent). Raised before any
        # Composio side-effects so a 402 is recoverable.
        from trial_caps import consume_or_raise as _consume
        await _consume(db, u, "publishes", amount=1)
        await require_org_member(post["org_id"], u["id"])
        if post.get("status") == "publishing":
            return {"status": "publishing", "message": "Already in flight"}
        # Mark immediately so the UI doesn't show stale state in the
        # gap between this return and the background task starting.
        await db.social_posts.update_one(
            {"_id": post["_id"]},
            {"$set": {
                "status": "publishing",
                "publish_started_at": datetime.now(timezone.utc),
                "publish_results": {},
                "published_count": 0,
                "failed_count": 0,
            }},
        )
        from social_publishing import publish_post

        async def _run():
            # Re-fetch so we have the freshest copy (in case the user
            # tweaked the text between click and task pick-up).
            fresh = await db.social_posts.find_one({"_id": post["_id"]})
            if not fresh:
                return
            await publish_post(db, post=fresh, actor_user_id=u["id"])

        asyncio.create_task(_run())
        return {"status": "publishing", "post_id": post_id}

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
        if body.auto_publish is not None:
            update["auto_publish"] = bool(body.auto_publish)
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

    @r.post("/social/posts/{post_id}/generate-image", status_code=202)
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

        Runs Nano Banana in a background task — production-ingress
        proxy timeouts (60-100s) would otherwise kill the HTTP request
        mid-generation. Returns 202 with the post flipped to
        ``image_status: "pending"`` and the SocialPage polling loop
        swaps in the finished image when ready.
        """
        try:
            post = await db.social_posts.find_one({"_id": _OID(post_id)})
        except Exception as exc:
            raise HTTPException(400, "Invalid post id") from exc
        if not post:
            raise HTTPException(404, "Post not found")
        await require_org_member(post["org_id"], u["id"])
        # Trial cap: each regenerate consumes one image.
        from trial_caps import consume_or_raise as _consume
        await _consume(db, u, "images", amount=1)

        override = (body.prompt or "").strip()
        # Persist the prompt synchronously so the post survives a
        # refresh — without this, the Edit-prompt editor would open
        # blank mid-regeneration if the user navigates away and back.
        resolved_body = (
            override
            or (post.get("image_prompt") or "").strip()
            or (post.get("text") or "").strip()[:600]
        )
        pending_update: dict = {
            "image_status": "pending",
            "image_pending_at": datetime.now(timezone.utc),
        }
        if resolved_body:
            pending_update["image_prompt"] = resolved_body
        pending_unset: dict = {"image_error": ""}
        # Per-post visual-style override handling. Three modes:
        #   • clear_style_override=true → drop the override, fall back to brand default
        #   • style_override is a non-empty string → persist as the new override
        #   • style_override is None (omitted) → leave existing override untouched
        if body.clear_style_override:
            pending_unset["image_style_override"] = ""
        elif body.style_override is not None:
            pending_update["image_style_override"] = body.style_override.strip()
        await db.social_posts.update_one(
            {"_id": _OID(post_id)},
            {"$set": pending_update, "$unset": pending_unset},
        )

        async def _run_generate_bg():
            try:
                org = await db.organizations.find_one({"_id": _OID(post["org_id"])})
                # Re-fetch the post so `image_style_override` reflects the
                # just-applied $set/$unset above. Without this re-fetch the
                # background task would use the pre-update snapshot and the
                # override request silently no-ops on first regeneration.
                fresh_post = await db.social_posts.find_one({"_id": _OID(post_id)}) or post
                effective_prompt = override or (fresh_post.get("image_prompt") or "")
                img = await _generate_post_image(fresh_post, org or {}, effective_prompt)
                if not img:
                    raise RuntimeError("Nano Banana returned empty image")
                update = {
                    "image": img,
                    "image_status": "ready",
                    "image_updated_at": datetime.now(timezone.utc),
                }
                if resolved_body:
                    update["image_prompt"] = resolved_body
                await db.social_posts.update_one(
                    {"_id": _OID(post_id)},
                    {"$set": update, "$unset": {"image_error": "", "image_pending_at": ""}},
                )
            except Exception as exc:
                logger.warning("Post image generation failed: %s", exc)
                try:
                    from error_reporter import report_error_async as _report
                    await _report(
                        kind="image_generate",
                        org_id=str(post.get("org_id") or ""),
                        user_email=u.get("email"),
                        user_id=u.get("id"),
                        message=str(exc),
                        exc=exc,
                        extra={"post_id": str(post.get("_id") or "")},
                    )
                except Exception:
                    pass
                await db.social_posts.update_one(
                    {"_id": _OID(post_id)},
                    {"$set": {
                        "image_status": "error",
                        "image_error": "Generation failed — tap Regenerate to retry.",
                    }, "$unset": {"image_pending_at": ""}},
                )

        asyncio.create_task(_run_generate_bg())
        fresh = await db.social_posts.find_one({"_id": _OID(post_id)})
        return serialize_social_post(fresh)

    # ── Aspect-ratio helper for video gen ────────────────────────────
    # Maps the post's first platform onto the right Kie.ai aspect_ratio.
    # Returns one of "16:9", "9:16", "1:1" — Kie.ai accepts the first
    # two; "1:1" gracefully degrades to 9:16 for engines that don't
    # support it (the player still respects the actual rendered frame).
    def _video_aspect_for_platform(platforms: list[str]) -> str:
        if not platforms:
            return "16:9"
        p = (platforms[0] or "").lower()
        # Vertical-first surfaces (Reels / Shorts / TikTok)
        if p in ("instagram", "tiktok", "threads"):
            return "9:16"
        if p in ("youtube",) and False:  # placeholder for shorts
            return "9:16"
        # Default to landscape for LinkedIn / Facebook / Twitter / YouTube / etc.
        return "16:9"

    @r.post("/social/posts/{post_id}/generate-video", status_code=202)
    async def generate_post_video(
        post_id: str,
        body: GenerateVideoBody = Body(default_factory=GenerateVideoBody),
        u: dict = Depends(get_current_user),
    ):
        """Generate a video cover for a social post via Kie.ai. Fires the
        actual generation in the background — Veo / Kling / Wan / Sora
        can take 60-240s and would otherwise blow past the production
        ingress proxy timeout.

        Body fields:
          • `prompt` — optional override (defaults to post copy)
          • `model` — one of SOCIAL_VIDEO_MODELS (default veo3_fast)
          • `aspect_ratio` — "Auto" (derive from platform), "16:9", "9:16"

        Persists `video_status: "pending"` synchronously so the
        SocialPage poll picks up the spinner immediately. When the
        background task completes it patches `video` (with our local
        capability URL) and flips `video_status: "ready"`.
        """
        if not os.environ.get("KIE_API_KEY"):
            raise HTTPException(503, "Video generation isn't configured (KIE_API_KEY missing).")
        try:
            post = await db.social_posts.find_one({"_id": _OID(post_id)})
        except Exception as exc:
            raise HTTPException(400, "Invalid post id") from exc
        if not post:
            raise HTTPException(404, "Post not found")
        await require_org_member(post["org_id"], u["id"])

        # Model whitelist
        model = (body.model or DEFAULT_SOCIAL_VIDEO_MODEL).strip()
        if model not in SOCIAL_VIDEO_MODELS:
            raise HTTPException(
                422,
                f"Unknown model {model!r} — pick one of {sorted(SOCIAL_VIDEO_MODELS)}.",
            )

        # Aspect ratio resolution
        ar = (body.aspect_ratio or "Auto").strip()
        if ar == "Auto":
            ar = _video_aspect_for_platform(post.get("platforms") or [])
        if ar not in ("16:9", "9:16"):
            ar = "16:9"

        # Prompt resolution + persist override so refresh doesn't blank
        # the editor mid-generation.
        override = (body.prompt or "").strip()
        resolved_prompt = (
            override
            or (post.get("video_prompt") or "").strip()
            or (post.get("text") or "").strip()[:600]
        )
        if not resolved_prompt:
            raise HTTPException(422, "Need a prompt or some post text to generate a video.")

        pending_update: dict = {
            "video_status": "pending",
            "video_pending_at": datetime.now(timezone.utc),
            "video_prompt": resolved_prompt,
            "video_model": model,
            "video_aspect_ratio": ar,
        }
        await db.social_posts.update_one(
            {"_id": _OID(post_id)},
            {"$set": pending_update, "$unset": {"video_error": ""}},
        )

        # Fold the org Soul + style guide into the prompt so the video
        # stays on-brand. Light touch — Kie's engines reward focused
        # cinematic direction over walls of brand context.
        org = await db.organizations.find_one({"_id": _OID(post["org_id"])})
        soul = (org or {}).get("soul") or {}
        sg = (org or {}).get("style_guide") or {}
        ba = (org or {}).get("brand_assets") or {}
        brand_bits: list[str] = []
        if soul.get("tone"):
            brand_bits.append(f"Tone: {soul['tone']}")
        if soul.get("audience"):
            brand_bits.append(f"Audience: {soul['audience']}")
        if (sg.get("image_video_style") or "").strip():
            brand_bits.append(f"Visual style: {sg['image_video_style'][:300]}")
        elif (ba.get("visual_style") or "").strip():
            brand_bits.append(f"Visual style: {ba['visual_style'][:300]}")
        full_prompt = resolved_prompt
        if brand_bits:
            full_prompt = (
                f"{resolved_prompt}\n\n--- Brand context ---\n"
                + "\n".join(brand_bits)
            )

        async def _run_video_bg():
            try:
                # Veo paths use the legacy single-shot helper; everything
                # else goes through the marketplace submit/wait API.
                cdn_url: Optional[str] = None
                if model in ("veo3", "veo3_fast", "veo3_lite"):
                    if not kie_generate_video:
                        raise RuntimeError("Veo client not wired in")
                    cdn_url = await kie_generate_video(
                        full_prompt, model=model, aspect_ratio=ar,
                    )
                else:
                    if not kie_submit_kie_task or not kie_wait_for_kie_task:
                        raise RuntimeError("Kie marketplace client not wired in")
                    info = kie_provider_info(model) if kie_provider_info else None
                    if not info:
                        raise RuntimeError(f"Provider info missing for {model}")
                    task_id = await kie_submit_kie_task(
                        model_key=model, prompt=full_prompt,
                        extra={"aspect_ratio": ar},
                    )
                    if not task_id:
                        raise RuntimeError("Kie.ai refused the task (no taskId returned)")
                    urls = await kie_wait_for_kie_task(
                        task_id=task_id, model_key=model,
                    )
                    if urls:
                        cdn_url = urls[0]
                if not cdn_url:
                    raise RuntimeError("Kie.ai returned no video URL")

                # Mirror to local storage + GridFS so the link never
                # expires when Kie's CDN evicts it.
                local_path = None
                if save_remote_url_to_uploads:
                    try:
                        local_path = await save_remote_url_to_uploads(
                            cdn_url, suffix=".mp4",
                        )
                    except Exception as exc:
                        logger.warning("Social video local-mirror failed: %s", exc)

                # ── 0-byte / corrupt guard ────────────────────────────
                # _save_remote_url_to_uploads returns None on download
                # failure, but a partial download can still leave a tiny
                # file on disk. Anything under 50 KB definitely isn't a
                # playable Veo3/Kling clip — short-circuit and surface
                # an error rather than ship a 0:00 player to the user.
                if local_path and local_path.is_file():
                    try:
                        size = local_path.stat().st_size
                    except Exception:
                        size = 0
                    if size < 50 * 1024:
                        logger.warning(
                            "Social video mirror produced %d-byte file "
                            "for %s — treating as failure.", size, local_path,
                        )
                        try:
                            local_path.unlink()
                        except Exception:
                            pass
                        local_path = None

                if not local_path:
                    # Local mirror failed AND we won't trust an
                    # external Kie CDN URL in a video tag — those URLs
                    # expire and frequently CORS-block in production
                    # browsers. Mark as error so the user can retry.
                    raise RuntimeError(
                        "Video file mirror failed — try generating "
                        "again. (Veo's CDN occasionally drops the "
                        "final download; a retry usually works.)"
                    )

                final_url = f"/api/files/{local_path.name}"
                video = {
                    "filename": local_path.name,
                    "url": final_url,
                    "remote_url": cdn_url,
                    "model": model,
                    "aspect_ratio": ar,
                    "prompt": resolved_prompt[:600],
                }
                await db.social_posts.update_one(
                    {"_id": _OID(post_id)},
                    {"$set": {
                        "video": video,
                        "video_status": "ready",
                        "video_updated_at": datetime.now(timezone.utc),
                    }, "$unset": {"video_error": "", "video_pending_at": ""}},
                )
            except Exception as exc:
                logger.warning("Post video generation failed: %s", exc)
                try:
                    from error_reporter import report_error_async as _report
                    await _report(
                        kind="video_generate",
                        org_id=str(post.get("org_id") or ""),
                        user_email=u.get("email"),
                        user_id=u.get("id"),
                        message=str(exc),
                        exc=exc,
                        extra={
                            "post_id": str(post.get("_id") or ""),
                            "model": model,
                            "aspect_ratio": ar,
                        },
                    )
                except Exception:
                    pass
                await db.social_posts.update_one(
                    {"_id": _OID(post_id)},
                    {"$set": {
                        "video_status": "error",
                        "video_error": (
                            "Video generation failed — try a different "
                            "model or shorten the prompt."
                        ),
                    }, "$unset": {"video_pending_at": ""}},
                )

        asyncio.create_task(_run_video_bg())
        fresh = await db.social_posts.find_one({"_id": _OID(post_id)})
        return serialize_social_post(fresh)

    @r.get("/social/video-models")
    async def list_video_models(u: dict = Depends(get_current_user)):
        """Surface the available video models for the UI dropdown.

        Auth-gated only (no org binding) — power users may want to see
        the list before they pick a post to generate from.
        """
        configured = bool(os.environ.get("KIE_API_KEY"))
        return {
            "configured": configured,
            "default": DEFAULT_SOCIAL_VIDEO_MODEL,
            "models": [{"key": k, "label": v} for k, v in SOCIAL_VIDEO_MODELS.items()],
        }

    @r.delete("/social/posts/{post_id}/video")
    async def remove_post_video(
        post_id: str, u: dict = Depends(get_current_user),
    ):
        """Detach a video from the post. Doesn't delete the underlying
        mp4 on disk — that may be referenced by `publish_results` /
        scheduled jobs we don't want to break — but flips the post back
        to a no-cover state so the UI offers Generate again."""
        try:
            post = await db.social_posts.find_one({"_id": _OID(post_id)})
        except Exception as exc:
            raise HTTPException(400, "Invalid post id") from exc
        if not post:
            raise HTTPException(404, "Post not found")
        await require_org_member(post["org_id"], u["id"])
        await db.social_posts.update_one(
            {"_id": _OID(post_id)},
            {"$set": {"video_status": "none"},
             "$unset": {"video": "", "video_prompt": "", "video_model": "",
                        "video_aspect_ratio": "", "video_error": "",
                        "video_pending_at": ""}},
        )
        fresh = await db.social_posts.find_one({"_id": _OID(post_id)})
        return serialize_social_post(fresh)

    @r.post("/social/posts/{post_id}/generate-music-video", status_code=202)
    async def generate_post_music_video(
        post_id: str,
        body: GenerateMusicVideoBody = Body(default_factory=GenerateMusicVideoBody),
        u: dict = Depends(get_current_user),
    ):
        """Stitch the post's cover image + a song into a music-video
        MP4 (Kling 2.1 Master Image-to-Video → ffmpeg-loop with audio
        muxed on top). Fires in the background; flips
        `music_video_status` to ``pending`` synchronously so the UI
        renders a spinner immediately, then to ``ready`` /
        ``error`` when the worker finishes.

        Body fields:
          • `song_url` — direct URL to mp3/wav (preferred — fastest)
          • `song_prompt` — describe a song; we mint one with Suno first
          • `vibe_prompt` — cinematic direction for Kling
          • `max_seconds` — cap output (default = full song duration)

        Requires the post to already have a cover image (`image.url`).
        """
        if not os.environ.get("KIE_API_KEY"):
            raise HTTPException(503, "Music video generation isn't configured (KIE_API_KEY missing).")
        try:
            from music_video import ffmpeg_available  # noqa: WPS433 — local
        except ImportError as exc:
            raise HTTPException(503, "music_video module is unavailable.") from exc
        if not ffmpeg_available():
            raise HTTPException(503, "Server is missing ffmpeg — install ffmpeg/ffprobe to enable music videos.")
        try:
            post = await db.social_posts.find_one({"_id": _OID(post_id)})
        except Exception as exc:
            raise HTTPException(400, "Invalid post id") from exc
        if not post:
            raise HTTPException(404, "Post not found")
        await require_org_member(post["org_id"], u["id"])

        # Require an image cover on the post — the music video needs a
        # visual seed and we don't want to silently fall back to generic.
        image = post.get("image") or {}
        image_path = image.get("url") or ""
        if not image_path:
            raise HTTPException(
                422,
                "Generate a cover image first — the music video needs a base visual.",
            )

        song_url = (body.song_url or "").strip()
        song_prompt = (body.song_prompt or "").strip()
        vibe = (body.vibe_prompt or "").strip()
        if not song_url and not song_prompt:
            raise HTTPException(
                422,
                "Pass `song_url` (direct mp3 link) or `song_prompt` (we'll compose one for you).",
            )

        now = datetime.now(timezone.utc)
        await db.social_posts.update_one(
            {"_id": _OID(post_id)},
            {"$set": {
                "music_video_status": "pending",
                "music_video_pending_at": now,
                "music_video_song_prompt": song_prompt[:600],
                "music_video_vibe_prompt": vibe[:600],
            }, "$unset": {"music_video_error": ""}},
        )

        # Resolve the cover image to a publicly fetchable URL — kie.ai
        # downloads `image_url` server-side, so internal paths
        # (/api/files/...) won't work.
        public_base = (
            (post.get("image") or {}).get("public_url")
            or os.environ.get("PUBLIC_BACKEND_URL")
            or os.environ.get("REACT_APP_BACKEND_URL")
            or os.environ.get("FRONTEND_BASE_URL")
            or ""
        ).rstrip("/")
        if image_path.startswith("http"):
            public_image_url = image_path
        else:
            public_image_url = f"{public_base}{image_path}" if public_base else image_path

        async def _run_mv_bg():
            from music_video import build_music_video_bytes, MusicVideoError
            from kie_client import generate_via_kie
            try:
                resolved_song_url = song_url
                # Mint a Suno track if the caller only supplied a prompt.
                if not resolved_song_url and song_prompt:
                    suno_urls = await generate_via_kie(
                        model_key="suno", prompt=song_prompt,
                    )
                    if not suno_urls:
                        raise MusicVideoError(
                            "Suno couldn't compose a track for that prompt — try again or pass `song_url`."
                        )
                    resolved_song_url = suno_urls[0]
                mp4_bytes = await build_music_video_bytes(
                    image_url=public_image_url,
                    song_url=resolved_song_url,
                    vibe_prompt=vibe,
                    max_seconds=float(body.max_seconds) if body.max_seconds else None,
                )
                # Persist to disk (+ GridFS via persist_bytes).
                if upload_dir is None or persist_bytes is None:
                    raise MusicVideoError("Server file storage wasn't initialised.")
                fname = f"music-video-{uuid.uuid4().hex[:12]}.mp4"
                fp = upload_dir / fname
                await persist_bytes(fp, mp4_bytes, original_name=fname, mime="video/mp4")
                final_url = f"/api/files/{fname}"
                music_video_obj = {
                    "filename": fname,
                    "url": final_url,
                    "mime": "video/mp4",
                    "size": len(mp4_bytes),
                    "song_url": resolved_song_url,
                    "vibe_prompt": vibe[:600],
                    "song_prompt": song_prompt[:600],
                    "model": "kling-v21-master-i2v + ffmpeg",
                }
                await db.social_posts.update_one(
                    {"_id": _OID(post_id)},
                    {"$set": {
                        "music_video": music_video_obj,
                        "music_video_status": "ready",
                        "music_video_updated_at": datetime.now(timezone.utc),
                    }, "$unset": {"music_video_error": "", "music_video_pending_at": ""}},
                )
            except MusicVideoError as exc:
                logger.warning("Post music_video failed: %s", exc)
                await db.social_posts.update_one(
                    {"_id": _OID(post_id)},
                    {"$set": {
                        "music_video_status": "error",
                        "music_video_error": str(exc)[:500],
                    }, "$unset": {"music_video_pending_at": ""}},
                )
            except Exception as exc:  # pragma: no cover — defensive
                logger.exception("Post music_video crashed: %s", exc)
                try:
                    from error_reporter import report_error_async as _report
                    await _report(
                        kind="music_video_generate",
                        org_id=str(post.get("org_id") or ""),
                        user_email=u.get("email"),
                        user_id=u.get("id"),
                        message=str(exc),
                        exc=exc,
                        extra={"post_id": str(post.get("_id") or "")},
                    )
                except Exception:
                    pass
                await db.social_posts.update_one(
                    {"_id": _OID(post_id)},
                    {"$set": {
                        "music_video_status": "error",
                        "music_video_error": "Music video assembly crashed — try again.",
                    }, "$unset": {"music_video_pending_at": ""}},
                )

        asyncio.create_task(_run_mv_bg())
        fresh = await db.social_posts.find_one({"_id": _OID(post_id)})
        return serialize_social_post(fresh)

    @r.delete("/social/posts/{post_id}/music-video")
    async def remove_post_music_video(
        post_id: str, u: dict = Depends(get_current_user),
    ):
        """Detach the music video from a post (mirrors remove_post_video)."""
        try:
            post = await db.social_posts.find_one({"_id": _OID(post_id)})
        except Exception as exc:
            raise HTTPException(400, "Invalid post id") from exc
        if not post:
            raise HTTPException(404, "Post not found")
        await require_org_member(post["org_id"], u["id"])
        await db.social_posts.update_one(
            {"_id": _OID(post_id)},
            {"$set": {"music_video_status": "none"},
             "$unset": {
                "music_video": "", "music_video_error": "",
                "music_video_song_prompt": "",
                "music_video_vibe_prompt": "",
                "music_video_pending_at": "",
             }},
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
        # If the file isn't on disk, try to rehydrate from the GridFS
        # mirror BEFORE shutil.copyfile would otherwise fail. This is
        # the same fix as the /remix-asset endpoint — every "use the
        # asset bytes locally" code path has to defend against
        # disk-was-wiped-by-redeploy, not just the serve handler.
        if not src_path.is_file():
            try:
                from server import recover_bytes_from_gridfs
                blob, _mime, _orig = await recover_bytes_from_gridfs(src_filename)
                if blob:
                    assets_dir.mkdir(parents=True, exist_ok=True)
                    src_path.write_bytes(blob)
                    logger.info(
                        "Use-as-cover: rehydrated asset %s for org %s from GridFS (%d bytes)",
                        src_filename, post["org_id"], len(blob),
                    )
            except Exception as exc:
                logger.warning(
                    "Use-as-cover: GridFS rehydrate attempt failed for asset %s: %s",
                    src_filename, exc,
                )
            if not src_path.is_file():
                raise HTTPException(
                    404,
                    "Source asset file is no longer available. It was uploaded "
                    "before durable storage was enabled — please re-upload it "
                    "and try again.",
                )

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
                        # Mirror to GridFS — see `_generate_post_image` for why.
                        await _mirror_composed_to_gridfs(composed)
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

    @r.post("/social/posts/{post_id}/remix-asset", status_code=202)
    async def remix_asset_into_cover(
        post_id: str,
        body: dict = Body(...),
        u: dict = Depends(get_current_user),
    ):
        """Phase 3 — use an existing asset as the BASE image and send it +
        a prompt to Nano Banana for an image-to-image remix (new
        background, lifestyle variant, platform retarget, etc.). Body:

            { "asset_id": str, "prompt": str }

        Runs Nano Banana in a background task so production-ingress
        proxy timeouts (60-100s on most CDNs) can't kill the request
        mid-generation. Returns 202 immediately with the post in
        ``image_status: "pending"`` and the SocialPage polling loop
        (already watching that field) picks up the finished image.
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
        # If the on-disk asset file is missing (typical after a container
        # redeploy wipes ephemeral disk), try to rehydrate from the
        # GridFS mirror. The serve handler already does this for read
        # requests — remix has to do it too or the user sees a thumbnail
        # in the picker (because the picker rehydrates) but a hard
        # "Source asset file missing on disk" error when they hit Remix.
        if not src_path.is_file():
            recovered = False
            try:
                from server import recover_bytes_from_gridfs
                blob, _mime, _orig = await recover_bytes_from_gridfs(asset["filename"])
                if blob:
                    assets_dir.mkdir(parents=True, exist_ok=True)
                    src_path.write_bytes(blob)
                    recovered = True
                    logger.info(
                        "Remix: rehydrated asset %s for org %s from GridFS (%d bytes)",
                        asset["filename"], post["org_id"], len(blob),
                    )
            except Exception as exc:
                logger.warning(
                    "Remix: GridFS rehydrate attempt failed for asset %s: %s",
                    asset["filename"], exc,
                )
            if not recovered:
                raise HTTPException(
                    404,
                    "Source asset file is no longer available. It was uploaded "
                    "before durable storage was enabled — please re-upload it "
                    "and try again.",
                )

        # Flip the post into "pending" immediately + persist the prompt
        # so the SocialPage poll picks up the spinner state.
        await db.social_posts.update_one(
            {"_id": _OID(post_id)},
            {"$set": {
                "image_prompt": prompt,
                "image_status": "pending",
                "image_pending_at": datetime.now(timezone.utc),
            }},
        )

        async def _run_remix_bg():
            try:
                org = await db.organizations.find_one({"_id": _OID(post["org_id"])})
                # Fold Soul + visual-style into the prompt so the remix stays
                # on-brand.
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
                fname, _caption = await generate_image_via_nano_banana(
                    full_prompt, sys_msg, input_image_paths=[src_path],
                )
                # Reapply logo (same compose pattern as /generate-image).
                primary = (ba.get("primary_logo") or {}) if ba else {}
                logo_filename = (primary or {}).get("filename") or ""
                final_filename = fname
                with_logo = False
                if logo_filename and compose_logo_on_image:
                    try:
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
                    {"$set": {
                        "image": image,
                        "image_status": "ready",
                        "image_updated_at": datetime.now(timezone.utc),
                    }, "$unset": {"image_error": "", "image_pending_at": ""}},
                )
            except Exception as exc:
                logger.warning("Asset remix failed: %s", exc)
                try:
                    from error_reporter import report_error_async as _report
                    await _report(
                        kind="asset_remix",
                        org_id=str(post.get("org_id") or ""),
                        user_email=u.get("email"),
                        user_id=u.get("id"),
                        message=str(exc),
                        exc=exc,
                        extra={
                            "post_id": str(post.get("_id") or ""),
                            "asset_id": str(asset.get("_id") or ""),
                            "prompt": (prompt or "")[:200],
                        },
                    )
                except Exception:
                    pass
                await db.social_posts.update_one(
                    {"_id": _OID(post_id)},
                    {"$set": {
                        "image_status": "error",
                        "image_error": "Remix failed — try a different prompt.",
                    }, "$unset": {"image_pending_at": ""}},
                )

        asyncio.create_task(_run_remix_bg())
        # Return the post in its pending state so the FE can show the
        # spinner immediately. The polling loop will swap in the final
        # image once the background task lands.
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


# ── APScheduler hook: auto-publish queued posts whose time has come ─────────
async def promote_due_drafts(*, db) -> int:
    """Find any `queued` posts past their scheduled_for and act on them.

    For posts with `auto_publish=true` (default): fire the live publish
    via Composio in the background. For posts opted out of auto-publish:
    just mark them `ready_at` so the UI surfaces them at the top of the
    queue for the user to click "Publish via Foundry" manually.

    Returns the count of posts processed in this tick.
    """
    now = datetime.now(timezone.utc)
    # Only promote posts whose cover media has finished generating. A
    # post with `image_status: "pending"` or `video_status: "pending"`
    # would otherwise auto-publish without a cover (or with a broken
    # mirror URL) — and we've seen this fail silently on IG Reels
    # because the Kie.ai mp4 wasn't written to disk yet. We re-poll on
    # the next tick (60s later) once the cover is ready.
    due = await db.social_posts.find(
        {
            "status": "queued",
            "scheduled_for": {"$lte": now},
            "ready_at": {"$exists": False},
            "image_status": {"$nin": ["pending"]},
            "video_status": {"$nin": ["pending"]},
        },
    ).to_list(200)
    if not due:
        return 0
    # Mark them ready first so duplicates can't fire if this task overlaps.
    ids = [p["_id"] for p in due]
    await db.social_posts.update_many(
        {"_id": {"$in": ids}},
        {"$set": {"ready_at": now}},
    )
    # Decide per-post: auto-publish or wait for manual.
    auto_count = 0
    try:
        from social_publishing import publish_post  # local import to avoid cycles
    except Exception as exc:
        logger.warning("Auto-publish skipped (publishing module unavailable): %s", exc)
        publish_post = None  # type: ignore[assignment]
    for p in due:
        if publish_post is None or not p.get("auto_publish", True):
            continue
        # Fire-and-forget — `publish_post` writes its own results back
        # to the post document; the UI's polling picks them up.
        try:
            asyncio.create_task(publish_post(db, post=p))
            auto_count += 1
        except Exception as exc:
            logger.warning("Auto-publish kick-off failed for post %s: %s", p.get("_id"), exc)
    if auto_count:
        logger.info(
            "Marked %d social posts ready, auto-publishing %d", len(due), auto_count,
        )
    else:
        logger.info("Marked %d social posts ready to publish", len(due))
    return len(due)
