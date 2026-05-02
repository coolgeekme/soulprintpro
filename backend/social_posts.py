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
                       upload_dir=None):
    """Build the /api/social router with closure-bound dependencies from server.py."""
    from bson import ObjectId as _OID
    from pathlib import Path as _Path
    r = APIRouter(prefix="/api")

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
            fname, _caption = await generate_image_via_nano_banana(prompt, sys_msg)
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
                base_path = _Path(upload_dir) / fname
                logo_path = _Path(upload_dir) / logo_filename
                if not base_path.is_file():
                    logger.warning("Social image: base file %s missing on disk", base_path)
                elif not logo_path.is_file():
                    logger.warning(
                        "Social image: logo file %s missing on disk (org has stale brand_assets?)",
                        logo_path,
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
        }

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
                "logo_position": "bottom-right",
                "scheduled_for": None,
                "published_at": None,
                "created_at": now,
                "created_by": u["id"],
            }
            if idx == 0:
                img = await _generate_post_image(doc, ctx["org"])
                if img:
                    doc["image"] = img
            res = await db.social_posts.insert_one(doc)
            doc["_id"] = res.inserted_id
            docs.append(doc)
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
        if override:
            update["image_prompt"] = override
        await db.social_posts.update_one(
            {"_id": _OID(post_id)}, {"$set": update},
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
        base_path = _Path(upload_dir) / base_filename
        logo_path = _Path(upload_dir) / logo_filename
        if not logo_path.is_file():
            raise HTTPException(
                422,
                "Your logo file is missing from storage — please re-upload "
                "the primary logo on the Org page.",
            )
        if not base_path.is_file():
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
