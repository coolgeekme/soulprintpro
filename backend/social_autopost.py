"""Autonomous scheduled social posting — Mission-powered.

When a user schedules a social post with `autopost_enabled=True`, we register
an APScheduler one-shot job that fires at the chosen time. The job:

  1. Loads the post + org + stored Playwright `storage_state` for the target
     platform (so the headless browser starts already logged in).
  2. Creates a new Mission whose lead agent's goal is platform-specific web
     automation instructions — "open X composer, paste text, click post".
  3. Registers the storage_state with `browser_tools.register_mission_storage_state`
     so the mission's sticky context boots with the cookies loaded.
  4. Launches the existing `run_mission_loop`. Runs asynchronously.
  5. When the mission finishes, updates the post with `autopost_status` =
     `posted` / `failed` and surfaces any error to the UI.

Social sessions (`social_sessions` collection) store the Playwright
`storage_state` JSON per (org, platform). Users upload these via a
"Social Sessions" tab in Org settings — typical flow is to use a browser
extension (e.g. Get cookies.txt LOCALLY or a Playwright codegen export)
to produce the JSON and paste it in. We keep the bytes opaque — the
browser handles them natively.

This file also exposes a `make_social_autopost_router` factory that
registers the HTTP endpoints (/api/social/posts/{id}/schedule-autopost,
CRUD for social sessions) and a `rehydrate_autopost_jobs` startup hook.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

from apscheduler.triggers.date import DateTrigger
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Same supported list as the draft queue — a post can only autopost to a
# platform we also allow as a draft target.
SUPPORTED_AUTOPOST_PLATFORMS = (
    "twitter", "linkedin", "facebook",
    "threads", "bluesky", "mastodon", "reddit",
)

# Composer URLs per platform. Used to seed the mission's browse_url step.
# Platforms without a public composer URL (Instagram, TikTok) are omitted
# — we don't let users schedule autoposts there in the UI.
PLATFORM_COMPOSERS = {
    "twitter":  "https://x.com/compose/post",
    "linkedin": "https://www.linkedin.com/feed/?shareActive=true",
    "facebook": "https://www.facebook.com/",
    "threads":  "https://www.threads.net/",
    "bluesky":  "https://bsky.app/",
    "mastodon": "https://mastodon.social/home",  # users can override per-instance in session
    "reddit":   "https://www.reddit.com/submit",
}


def _schedule_job_id(post_id: str) -> str:
    return f"autopost:{post_id}"


def _compose_post_body(post: dict) -> str:
    """Build the text the agent should paste into the composer."""
    text = (post.get("text") or "").strip()
    hashtags = post.get("hashtags") or []
    if hashtags:
        tag_line = " ".join(
            (h if str(h).startswith("#") else f"#{h}") for h in hashtags
        )
        if tag_line and tag_line not in text:
            text = f"{text}\n\n{tag_line}".strip()
    link = (post.get("link_url") or "").strip()
    if link and link not in text:
        text = f"{text}\n{link}".strip()
    return text


def _build_autopost_goal(post: dict, platform: str, session_present: bool) -> str:
    """Build the Mission goal string — a detailed instruction set the lead
    agent will follow step-by-step. Platform-specific composer URL + post
    body + login expectations."""
    composer = PLATFORM_COMPOSERS.get(platform, "")
    body = _compose_post_body(post)
    # Light formatting — the mission loop's system prompt already covers
    # general rules (idempotency, tool-use discipline). We keep this goal
    # imperative and scoped to the task at hand.
    login_line = (
        "A Playwright storage_state with saved cookies for this account has "
        "been pre-loaded into your browser context, so you should already be "
        "logged in. If you encounter a login wall, STOP IMMEDIATELY and "
        "finish with summary=\"FAILED: login session expired — user needs "
        "to re-upload cookies\"."
        if session_present else
        "NO authenticated session is available for this platform. Your first "
        "browse_url will likely hit a login wall. If it does, STOP "
        "IMMEDIATELY and finish with summary=\"FAILED: no session cookies "
        "on file — user must upload a storage_state first\"."
    )
    return (
        f"Autonomously publish a social post to {platform.upper()} on behalf "
        f"of the owning organization.\n\n"
        f"Target composer URL: {composer or '(none configured — research it via web_search)'}\n\n"
        f"--- POST TEXT TO PUBLISH (copy verbatim, do NOT reword) ---\n"
        f"{body}\n"
        f"--- END POST TEXT ---\n\n"
        f"{login_line}\n\n"
        f"Steps you should follow:\n"
        f"  1. browse_url to the composer URL above.\n"
        f"  2. Read the page_map that comes back — identify the tweet/post "
        f"     textarea (usually a contenteditable div or <textarea>) and "
        f"     the primary Post/Publish button.\n"
        f"  3. browse_fill the textarea with the POST TEXT verbatim.\n"
        f"  4. browse_click (or browse_submit) the primary Post/Publish button.\n"
        f"  5. Wait for the composer to close / URL to change — then "
        f"     browse_url back to the same composer URL (or the profile "
        f"     page) to confirm the post appeared.\n"
        f"  6. finish the mission — see OUTCOME FORMAT below.\n\n"
        f"OUTCOME FORMAT — CRITICAL:\n"
        f"When you call `finish`, your summary MUST start with one of:\n"
        f"  • \"PUBLISHED:\" — if you verified the post went live. Include "
        f"    the verified URL if you found one.\n"
        f"  • \"FAILED:\" — if anything went wrong (login wall, disabled "
        f"    button, platform error, post text too long, etc.). Include "
        f"    the root cause in one sentence.\n"
        f"The runtime parses this prefix to update the social post status, "
        f"so omitting it defaults to FAILED.\n\n"
        f"HARD RULES:\n"
        f"  • Do NOT edit, rephrase, or 'improve' the post text. Publish it "
        f"    verbatim.\n"
        f"  • Do NOT attach media in this mission — if the draft had media, "
        f"    post the text-only version. Media autopost is a future feature.\n"
        f"  • Do NOT reply to, follow, or browse other accounts. Publish "
        f"    and leave.\n"
        f"  • If the Post button is disabled (e.g. the text is too long), "
        f"    finish with \"FAILED: post exceeds platform length limit\" — "
        f"    do NOT truncate silently.\n"
    )


# ── Pydantic bodies ─────────────────────────────────────────────────────────
class AutopostScheduleBody(BaseModel):
    scheduled_for: str = Field(..., description="ISO 8601 datetime (UTC or with tz)")
    platform: str = Field(..., description="Target platform, e.g. 'twitter'")


class SocialSessionBody(BaseModel):
    platform: str = Field(..., description="Platform key, e.g. 'twitter'")
    storage_state: dict = Field(
        ..., description="Playwright storage_state JSON object "
                         "(cookies + origins). Bytes are stored opaquely."
    )
    label: str = Field("", max_length=120,
                       description="Optional human-readable label for this session")


# ── Serialization ───────────────────────────────────────────────────────────
def _serialize_session(s: dict) -> dict:
    def _iso(v):
        if isinstance(v, datetime):
            return v.isoformat()
        return v
    ss = s.get("storage_state") or {}
    return {
        "id": str(s["_id"]),
        "org_id": s["org_id"],
        "platform": s["platform"],
        "label": s.get("label", ""),
        # Don't leak raw cookies — just report a summary so the UI can show
        # "cookies: 42 / origins: 3" without shipping the sensitive payload.
        "cookies_count": len(ss.get("cookies") or []),
        "origins_count": len(ss.get("origins") or []),
        "updated_at": _iso(s.get("updated_at")),
        "created_at": _iso(s.get("created_at")),
        "created_by": s.get("created_by", ""),
    }


# ── Router factory ──────────────────────────────────────────────────────────
def make_social_autopost_router(
    *,
    db,
    scheduler,  # the AsyncIOScheduler instance from server.py
    AGENTS: dict,
    EMERGENT_LLM_KEY: str,
    LlmChat,
    UserMessage,
    require_org_member: Callable,
    get_current_user: Callable,
    build_tools_factory: Callable[[], list],
    build_lead_system_prompt: Callable[[str], str],
):
    """Build the /api/social autopost router. Uses the same mission
    infrastructure as user-initiated missions — we just build the goal
    text and launch the loop from a scheduler callback instead of a
    user's HTTP request."""
    from missions import run_mission_loop

    r = APIRouter(prefix="/api")

    async def _fetch_session(org_id: str, platform: str) -> Optional[dict]:
        return await db.social_sessions.find_one(
            {"org_id": org_id, "platform": platform},
        )

    async def _run_autopost(post_id: str) -> None:
        """APScheduler callback. Launches the mission."""
        try:
            post = await db.social_posts.find_one({"_id": ObjectId(post_id)})
        except Exception:
            logger.warning("autopost: invalid post id %s", post_id)
            return
        if not post:
            logger.info("autopost: post %s vanished before firing", post_id)
            return
        if not post.get("autopost_enabled"):
            logger.info("autopost: post %s autopost disabled — skipping", post_id)
            return
        platform = (post.get("autopost_platform") or "").strip().lower()
        if platform not in SUPPORTED_AUTOPOST_PLATFORMS:
            logger.warning("autopost: unsupported platform %s for post %s",
                           platform, post_id)
            await db.social_posts.update_one(
                {"_id": ObjectId(post_id)},
                {"$set": {
                    "autopost_status": "failed",
                    "autopost_error": f"Unsupported platform: {platform}",
                    "autopost_last_run_at": datetime.now(timezone.utc),
                }},
            )
            return

        org_id = post["org_id"]
        session = await _fetch_session(org_id, platform)
        storage_state = (session or {}).get("storage_state") or {}

        # Build the mission doc manually — we don't go via the HTTP
        # start_mission endpoint because that requires a user context.
        lead = ("social-media-autopilot"
                if "social-media-autopilot" in AGENTS
                else ("marketing-social-media-strategist"
                      if "marketing-social-media-strategist" in AGENTS
                      else next(iter(AGENTS))))
        mission_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        goal = _build_autopost_goal(post, platform, bool(storage_state))
        mission = {
            "id": mission_id,
            "org_id": org_id,
            "conversation_id": None,
            "goal": goal,
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
            "created_by": post.get("created_by", ""),
            # Link back so the completion watcher can update the post.
            "social_post_id": post_id,
            "autopost_platform": platform,
        }
        await db.missions.insert_one({**mission})
        await db.social_posts.update_one(
            {"_id": ObjectId(post_id)},
            {"$set": {
                "autopost_status": "running",
                "autopost_mission_id": mission_id,
                "autopost_error": "",
                "autopost_last_run_at": now,
            }},
        )

        # Load storage_state into the browser BEFORE the mission starts
        # using the page, so cookies are applied on context creation.
        if storage_state:
            try:
                from browser_tools import register_mission_storage_state
                register_mission_storage_state(mission_id, storage_state)
            except Exception as exc:
                logger.warning("autopost: failed to register storage_state: %s", exc)

        tools = build_tools_factory()
        ctx = {
            "org_id": org_id,
            "mission_id": mission_id,
            "lead_agent_id": lead,
            "lead_agent_name": AGENTS[lead]["name"],
            "created_by": post.get("created_by", ""),
        }

        async def _drive_and_finalize():
            """Run the mission and write the outcome back to the post."""
            try:
                await run_mission_loop(
                    db=db, mission=mission, tools=tools, ctx=ctx,
                    LlmChat=LlmChat, UserMessage=UserMessage,
                    EMERGENT_LLM_KEY=EMERGENT_LLM_KEY,
                    build_lead_system_prompt=build_lead_system_prompt,
                )
            except Exception as exc:
                logger.exception("autopost: mission %s crashed: %s", mission_id, exc)
            # Read final mission state and map to post status.
            final = await db.missions.find_one({"id": mission_id})
            if not final:
                return
            summary = (final.get("summary") or "").strip()
            mission_status = final.get("status") or ""
            # Parse explicit PUBLISHED: / FAILED: prefix the autopost prompt
            # asks the agent to emit. If the mission crashed or was
            # cancelled, force the mapped status. If the summary has no
            # prefix on a "finished" mission, default to failed because the
            # agent didn't actually verify a publish.
            if mission_status == "cancelled":
                mapped_status, mapped_err = "cancelled", "mission cancelled"
            elif mission_status == "failed":
                mapped_status, mapped_err = "failed", final.get("error") or "mission failed"
            elif mission_status == "finished":
                upper = summary.upper()
                if upper.startswith("PUBLISHED:"):
                    mapped_status, mapped_err = "posted", ""
                elif upper.startswith("FAILED:"):
                    mapped_status, mapped_err = "failed", summary[len("FAILED:"):].strip() or "mission reported failure"
                else:
                    mapped_status, mapped_err = (
                        "failed",
                        "mission finished without a PUBLISHED: confirmation — "
                        "the agent may not have verified publication",
                    )
            else:
                mapped_status, mapped_err = "failed", f"unexpected mission status: {mission_status}"
            post_update: dict = {
                "autopost_status": mapped_status,
                "autopost_error": mapped_err,
                "autopost_last_run_at": datetime.now(timezone.utc),
            }
            if mapped_status == "posted":
                post_update["status"] = "published"
                post_update["published_at"] = datetime.now(timezone.utc)
            await db.social_posts.update_one(
                {"_id": ObjectId(post_id)}, {"$set": post_update},
            )

        asyncio.create_task(_drive_and_finalize())

    # Expose the internal runner on the router so server.py can also call it
    # on startup (for retries). A tiny attribute on the FastAPI router is
    # a pragmatic handoff; we don't export it via HTTP.
    r._run_autopost = _run_autopost  # type: ignore[attr-defined]

    def _register_job(post: dict) -> None:
        """Add/replace the APScheduler job for a post's autopost window."""
        post_id = str(post["_id"])
        job_id = _schedule_job_id(post_id)
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass
        when = post.get("scheduled_for")
        if not when or not post.get("autopost_enabled"):
            return
        if isinstance(when, str):
            try:
                when = datetime.fromisoformat(when.replace("Z", "+00:00"))
            except Exception:
                logger.warning("autopost: bad scheduled_for on %s", post_id)
                return
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        # If the scheduled time is already past (e.g. on server restart),
        # fire immediately so we don't silently drop late jobs.
        trigger = DateTrigger(run_date=when)
        scheduler.add_job(
            _run_autopost,
            trigger=trigger,
            id=job_id,
            args=[post_id],
            replace_existing=True,
            misfire_grace_time=60 * 60,  # 1h grace for server restarts
        )

    r._register_job = _register_job  # type: ignore[attr-defined]

    @r.post("/social/posts/{post_id}/schedule-autopost")
    async def schedule_autopost(
        post_id: str, body: AutopostScheduleBody,
        u: dict = Depends(get_current_user),
    ):
        try:
            post = await db.social_posts.find_one({"_id": ObjectId(post_id)})
        except Exception as exc:
            raise HTTPException(400, "Invalid post id") from exc
        if not post:
            raise HTTPException(404, "Post not found")
        await require_org_member(post["org_id"], u["id"])

        platform = body.platform.strip().lower()
        if platform not in SUPPORTED_AUTOPOST_PLATFORMS:
            raise HTTPException(
                422, f"Platform not supported for autopost. Supported: "
                     f"{', '.join(SUPPORTED_AUTOPOST_PLATFORMS)}",
            )
        try:
            when = datetime.fromisoformat(body.scheduled_for.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(422, "scheduled_for must be ISO 8601") from exc
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        # Don't allow scheduling in the past — but allow "fire in ~1 min" so
        # users can smoke-test the flow quickly.
        now = datetime.now(timezone.utc)
        if when < now:
            raise HTTPException(422, "scheduled_for is in the past")

        update = {
            "scheduled_for": when,
            "autopost_enabled": True,
            "autopost_platform": platform,
            "autopost_status": "pending",
            "autopost_error": "",
            # If platform was already in `platforms`, keep the user's list;
            # otherwise prepend it so the UI shows the autopost target too.
            "status": "queued" if post.get("status") == "draft" else post.get("status", "queued"),
        }
        await db.social_posts.update_one(
            {"_id": ObjectId(post_id)}, {"$set": update},
        )
        fresh = await db.social_posts.find_one({"_id": ObjectId(post_id)})
        _register_job(fresh)
        # Return the serialized post for the frontend to refresh its state.
        from social_posts import serialize_social_post
        return serialize_social_post(fresh)

    @r.delete("/social/posts/{post_id}/schedule-autopost")
    async def cancel_autopost(
        post_id: str, u: dict = Depends(get_current_user),
    ):
        try:
            post = await db.social_posts.find_one({"_id": ObjectId(post_id)})
        except Exception as exc:
            raise HTTPException(400, "Invalid post id") from exc
        if not post:
            raise HTTPException(404, "Post not found")
        await require_org_member(post["org_id"], u["id"])
        try:
            scheduler.remove_job(_schedule_job_id(post_id))
        except Exception:
            pass
        await db.social_posts.update_one(
            {"_id": ObjectId(post_id)},
            {"$set": {
                "autopost_enabled": False,
                "autopost_status": "cancelled" if post.get("autopost_status") in ("pending", "running") else "",
            }},
        )
        fresh = await db.social_posts.find_one({"_id": ObjectId(post_id)})
        from social_posts import serialize_social_post
        return serialize_social_post(fresh)

    # ── Social sessions (Playwright storage_state) CRUD ─────────────────
    @r.get("/orgs/{org_id}/social/sessions")
    async def list_sessions(
        org_id: str, u: dict = Depends(get_current_user),
    ):
        await require_org_member(org_id, u["id"])
        cur = db.social_sessions.find({"org_id": org_id}).sort("platform", 1)
        return [_serialize_session(s) async for s in cur]

    @r.post("/orgs/{org_id}/social/sessions")
    async def upsert_session(
        org_id: str, body: SocialSessionBody,
        u: dict = Depends(get_current_user),
    ):
        await require_org_member(org_id, u["id"])
        platform = body.platform.strip().lower()
        if platform not in SUPPORTED_AUTOPOST_PLATFORMS:
            raise HTTPException(
                422, f"Unsupported platform. Supported: "
                     f"{', '.join(SUPPORTED_AUTOPOST_PLATFORMS)}",
            )
        if not isinstance(body.storage_state, dict):
            raise HTTPException(422, "storage_state must be an object")
        # Validate shape — Playwright expects either `cookies` or `origins`
        # (or both). Reject empty payloads early so users get useful feedback.
        cookies = body.storage_state.get("cookies") or []
        origins = body.storage_state.get("origins") or []
        if not cookies and not origins:
            raise HTTPException(
                422, "storage_state must contain 'cookies' or 'origins'",
            )
        now = datetime.now(timezone.utc)
        doc = {
            "org_id": org_id,
            "platform": platform,
            "label": body.label.strip()[:120],
            "storage_state": body.storage_state,
            "updated_at": now,
            "created_by": u["id"],
        }
        existing = await db.social_sessions.find_one(
            {"org_id": org_id, "platform": platform},
        )
        if existing:
            await db.social_sessions.update_one(
                {"_id": existing["_id"]}, {"$set": doc},
            )
            fresh = await db.social_sessions.find_one({"_id": existing["_id"]})
        else:
            doc["created_at"] = now
            res = await db.social_sessions.insert_one(doc)
            fresh = await db.social_sessions.find_one({"_id": res.inserted_id})
        return _serialize_session(fresh)

    @r.delete("/orgs/{org_id}/social/sessions/{platform}")
    async def delete_session(
        org_id: str, platform: str, u: dict = Depends(get_current_user),
    ):
        await require_org_member(org_id, u["id"])
        await db.social_sessions.delete_one(
            {"org_id": org_id, "platform": platform.strip().lower()},
        )
        return {"ok": True}

    return r


async def rehydrate_autopost_jobs(*, db, router) -> int:
    """On server startup, re-register APScheduler jobs for any posts whose
    autopost window is still in the future (or just a bit in the past, due
    to a restart). Returns the count of jobs rehydrated."""
    now = datetime.now(timezone.utc)
    count = 0
    cur = db.social_posts.find({
        "autopost_enabled": True,
        "autopost_status": {"$in": ["pending", "running"]},
    })
    async for post in cur:
        when = post.get("scheduled_for")
        if not when:
            continue
        # Mongo returns naive datetimes — normalise to UTC so the subtraction
        # below doesn't raise.
        if isinstance(when, datetime) and when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        # Missed by > 1 hour? Mark as failed so the UI surfaces it.
        if isinstance(when, datetime) and (now - when).total_seconds() > 3600:
            await db.social_posts.update_one(
                {"_id": post["_id"]},
                {"$set": {
                    "autopost_status": "failed",
                    "autopost_error": "missed by >1h during server downtime",
                    "autopost_last_run_at": now,
                }},
            )
            continue
        try:
            router._register_job(post)  # type: ignore[attr-defined]
            count += 1
        except Exception as exc:
            logger.warning("autopost rehydrate failed for %s: %s",
                           post.get("_id"), exc)
    if count:
        logger.info("Rehydrated %d autopost jobs", count)
    return count


__all__ = [
    "make_social_autopost_router",
    "rehydrate_autopost_jobs",
    "SUPPORTED_AUTOPOST_PLATFORMS",
    "PLATFORM_COMPOSERS",
]
