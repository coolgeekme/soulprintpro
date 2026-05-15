"""Social Queue → live publishing via Composio.

Glue layer between `social_posts.py` (queue + scheduling) and
`composio_integration.py` (the user's connected SaaS accounts). The
public surface is intentionally tiny:

    publish_post(db, *, post, actor_user_id) -> dict

The function fans out the post to every requested platform sequentially,
calls the platform's "create post" tool through Composio, and writes a
per-platform result back to the document. Status transitions:

    queued/ready/draft  →  publishing  →  published        (all platforms OK)
                                       ↘  partial          (some failed)
                                       ↘  failed           (none worked)

We do NOT spawn one task per platform — Composio rate-limits per user
and the failure modes are easier to debug serially. ~3-8s total for 5
platforms is fine for a background task.

Platforms wired in v1:
    linkedin, facebook, instagram, reddit, tiktok

Twitter intentionally omitted because Composio doesn't have managed
OAuth credentials for it (would need user-supplied client_id/secret).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import composio_integration as composio

logger = logging.getLogger(__name__)

# Platform → (toolkit slug, default tool slug, builder fn name).
# Each builder receives the post dict + active connection row and returns
# the kwargs Composio expects. Keeps the dispatch table compact.
PLATFORM_PUBLISHERS: Dict[str, Dict[str, Any]] = {
    "linkedin":  {"toolkit": "linkedin",  "tool": "LINKEDIN_CREATE_LINKED_IN_POST", "builder": "_build_linkedin"},
    "facebook":  {"toolkit": "facebook",  "tool": "FACEBOOK_CREATE_POST",           "builder": "_build_facebook"},
    "instagram": {"toolkit": "instagram", "tool": "INSTAGRAM_CREATE_POST",          "builder": "_build_instagram"},
    "reddit":    {"toolkit": "reddit",    "tool": "REDDIT_CREATE_REDDIT_POST",      "builder": "_build_reddit"},
    "tiktok":    {"toolkit": "tiktok",    "tool": "TIKTOK_POST_PHOTO",              "builder": "_build_tiktok"},
}


def is_platform_supported(platform: str) -> bool:
    return platform in PLATFORM_PUBLISHERS


def supported_platforms() -> List[str]:
    return list(PLATFORM_PUBLISHERS.keys())


async def _has_active_connection(db, org_id: str, user_id: str, toolkit: str) -> bool:
    cu = composio.composio_user_id(org_id, user_id)
    return bool(await db.composio_connections.find_one(
        {"composio_user_id": cu, "toolkit": toolkit, "status": "ACTIVE"},
        {"_id": 1},
    ))


async def connectable_platforms_for_user(
    db, org_id: str, user_id: str,
) -> Dict[str, bool]:
    """For each supported platform, return whether the user has an active
    connection. Drives the SocialPage's "Publish via Foundry" button —
    hidden when no platforms are reachable."""
    out: Dict[str, bool] = {}
    for p, cfg in PLATFORM_PUBLISHERS.items():
        out[p] = await _has_active_connection(db, org_id, user_id, cfg["toolkit"])
    return out


# ── Per-platform argument builders ──────────────────────────────────────────
def _text_for(post: Dict[str, Any], platform: str) -> str:
    """Pull the platform-specific copy, falling back to the universal `text`.

    `social_posts.py` stores variants under `posts[i].variants[platform]`
    when the agent generated platform-tuned copy; otherwise the top-level
    `text` is used for everything.
    """
    variants = post.get("variants") or {}
    pv = variants.get(platform)
    if pv:
        return (pv.get("text") or pv.get("body") or "").strip()
    return (post.get("text") or "").strip()


def _image_url(post: Dict[str, Any]) -> Optional[str]:
    img = post.get("image") or {}
    return img.get("url") or img.get("public_url") or None


def _build_linkedin(post, _conn) -> Dict[str, Any]:
    """LINKEDIN_CREATE_LINKED_IN_POST takes commentary text. Media is a
    second-class citizen — for v1 we publish text only. Adding image
    support means an extra UPLOAD_LINKED_IN_IMAGE call; left for v2.
    """
    return {"commentary": _text_for(post, "linkedin") or post.get("text", "")}


def _build_facebook(post, conn) -> Dict[str, Any]:
    """FACEBOOK_CREATE_POST: needs the Page ID + message. The Page is
    resolved from the user's connected Facebook account by Composio if
    `page_id` isn't provided; we let Composio do the work."""
    return {"message": _text_for(post, "facebook") or post.get("text", "")}


def _build_instagram(post, conn) -> Dict[str, Any]:
    """INSTAGRAM_CREATE_POST: requires image_url + caption (IG doesn't
    support text-only posts). We fail fast if the post has no image."""
    img = _image_url(post)
    if not img:
        raise ValueError("Instagram requires an image — generate or attach one first.")
    return {
        "image_url": img,
        "caption": _text_for(post, "instagram") or post.get("text", ""),
    }


def _build_reddit(post, _conn) -> Dict[str, Any]:
    """REDDIT_CREATE_REDDIT_POST: subreddit + title + content.

    The user must specify the subreddit; we read it from per-platform
    variants (`variants.reddit.subreddit`) or post-level metadata
    (`post.reddit_subreddit`). Title falls back to first line of text.
    """
    variants = post.get("variants") or {}
    rv = variants.get("reddit") or {}
    sub = rv.get("subreddit") or post.get("reddit_subreddit")
    if not sub:
        raise ValueError("Reddit post needs a `subreddit` — set it in the post variants.")
    text = _text_for(post, "reddit") or post.get("text", "")
    title = rv.get("title") or text.splitlines()[0][:300] if text else "Untitled"
    body = rv.get("body") or text
    return {"subreddit": sub.lstrip("r/").strip(), "title": title, "content": body}


def _build_tiktok(post, _conn) -> Dict[str, Any]:
    """TIKTOK_POST_PHOTO: photo URL + caption. TikTok has no text-only
    post type. Video posts use a different tool (PUBLISH_VIDEO); we'll
    add that branch once posts carry a video asset."""
    img = _image_url(post)
    if not img:
        raise ValueError("TikTok requires a photo or video — generate one first.")
    return {
        "media_url": img,
        "title": _text_for(post, "tiktok") or post.get("text", "")[:150],
    }


_BUILDERS = {
    "_build_linkedin": _build_linkedin,
    "_build_facebook": _build_facebook,
    "_build_instagram": _build_instagram,
    "_build_reddit": _build_reddit,
    "_build_tiktok": _build_tiktok,
}


# ── Public entry point ─────────────────────────────────────────────────────
async def publish_post(
    db,
    *,
    post: Dict[str, Any],
    actor_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Publish one social_posts document to all of its requested platforms.

    Returns a result envelope of the shape::

        {
          "status": "published"|"partial"|"failed"|"skipped",
          "results": {<platform>: {"ok": bool, "tool": str,
                                   "response": Any, "error": str}},
          "published_count": int,
          "failed_count": int,
        }

    The caller (background task) is responsible for writing the final
    status back to the post document. We update the per-platform
    `publish_results` field as we go so the UI can render progress live.
    """
    pid = post.get("id") or post.get("_id")
    org_id = post.get("org_id")
    # The connection's user is whoever created the post (preferred) or the
    # actor who clicked Publish. Composio routes by composio_user_id so
    # the credentials match.
    user_id = post.get("created_by") or post.get("user_id") or actor_user_id
    if not (org_id and user_id):
        return {
            "status": "failed",
            "results": {},
            "published_count": 0,
            "failed_count": 0,
            "error": "Post is missing org_id or user_id",
        }
    platforms = list(post.get("platforms") or [])
    if not platforms:
        return {"status": "skipped", "results": {}, "published_count": 0, "failed_count": 0}

    # Mark the post as actively publishing so the UI can flip the chip.
    await db.social_posts.update_one(
        {"_id": post["_id"]} if "_id" in post else {"id": pid},
        {"$set": {"status": "publishing", "publish_started_at": datetime.now(timezone.utc)}},
    )

    results: Dict[str, Dict[str, Any]] = {}
    ok = 0
    bad = 0
    for platform in platforms:
        cfg = PLATFORM_PUBLISHERS.get(platform)
        if not cfg:
            results[platform] = {"ok": False, "error": "Platform not supported for direct publishing yet"}
            bad += 1
            continue
        toolkit = cfg["toolkit"]
        # No active connection → skip this platform but don't fail the
        # whole post; the user will see "needs connection" on this row.
        if not await _has_active_connection(db, org_id, user_id, toolkit):
            results[platform] = {"ok": False, "error": "No connected account for " + toolkit}
            bad += 1
            continue
        builder = _BUILDERS[cfg["builder"]]
        try:
            args = builder(post, None)
        except ValueError as exc:
            results[platform] = {"ok": False, "error": str(exc)}
            bad += 1
            continue
        try:
            res = await composio.execute_tool(
                db, org_id, user_id, cfg["tool"], args,
            )
            success = bool(res.get("success"))
            results[platform] = {
                "ok": success,
                "tool": cfg["tool"],
                "response": res.get("response"),
                "error": res.get("error") if not success else None,
            }
            if success:
                ok += 1
            else:
                bad += 1
        except Exception as exc:
            logger.exception("Publish %s failed for post %s", platform, pid)
            results[platform] = {"ok": False, "tool": cfg["tool"], "error": str(exc)[:300]}
            bad += 1
        # Persist progress so the UI doesn't wait for the whole fan-out.
        await db.social_posts.update_one(
            {"_id": post["_id"]} if "_id" in post else {"id": pid},
            {"$set": {f"publish_results.{platform}": results[platform]}},
        )

    if ok and not bad:
        final = "published"
    elif ok and bad:
        final = "partial"
    elif bad and not ok:
        final = "failed"
    else:
        final = "skipped"
    await db.social_posts.update_one(
        {"_id": post["_id"]} if "_id" in post else {"id": pid},
        {"$set": {
            "status": final,
            "publish_finished_at": datetime.now(timezone.utc),
            "publish_results": results,
            "published_count": ok,
            "failed_count": bad,
            **({"published_at": datetime.now(timezone.utc)} if ok else {}),
        }},
    )
    logger.info(
        "social-publish: post=%s status=%s ok=%d bad=%d", pid, final, ok, bad,
    )
    return {
        "status": final,
        "results": results,
        "published_count": ok,
        "failed_count": bad,
    }
