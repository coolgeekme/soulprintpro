"""Social Queue → live publishing via Composio.

Glue layer between `social_posts.py` (queue + scheduling) and
`composio_integration.py` (the user's connected SaaS accounts).

Per-platform publishing is owned by `_publish_<platform>` functions.
Most platforms are a single Composio tool call; Instagram requires
Meta's two-step container/publish flow which we model as two
sequential `execute_tool` invocations sharing a single audit
``publish_results.instagram`` row.

Status transitions on the post document:
    queued/ready/draft  →  publishing  →  published        (all platforms OK)
                                       ↘  partial          (some failed)
                                       ↘  failed           (none worked)

Platforms wired in v2:
    linkedin, facebook (Page-aware), instagram (two-step), reddit, tiktok

Twitter intentionally omitted because Composio doesn't have managed
OAuth credentials for it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import composio_integration as composio

logger = logging.getLogger(__name__)


# ── Connection lookup ───────────────────────────────────────────────────────
async def _active_connection(
    db, org_id: str, user_id: str, toolkit: str,
) -> Optional[Dict[str, Any]]:
    cu = composio.composio_user_id(org_id, user_id)
    return await db.composio_connections.find_one(
        {"composio_user_id": cu, "toolkit": toolkit, "status": "ACTIVE"},
        {"_id": 0},
    )


async def _has_active_connection(db, org_id: str, user_id: str, toolkit: str) -> bool:
    return bool(await _active_connection(db, org_id, user_id, toolkit))


PLATFORM_TOOLKITS = {
    "linkedin":  "linkedin",
    "facebook":  "facebook",
    "instagram": "instagram",
    "reddit":    "reddit",
    "tiktok":    "tiktok",
}


def is_platform_supported(platform: str) -> bool:
    return platform in PLATFORM_TOOLKITS


def supported_platforms() -> List[str]:
    return list(PLATFORM_TOOLKITS.keys())


async def connectable_platforms_for_user(
    db, org_id: str, user_id: str,
) -> Dict[str, bool]:
    out: Dict[str, bool] = {}
    for p, toolkit in PLATFORM_TOOLKITS.items():
        out[p] = await _has_active_connection(db, org_id, user_id, toolkit)
    return out


# ── Helpers ─────────────────────────────────────────────────────────────────
def _text_for(post: Dict[str, Any], platform: str) -> str:
    variants = post.get("variants") or {}
    pv = variants.get(platform)
    if pv:
        return (pv.get("text") or pv.get("body") or "").strip()
    return (post.get("text") or "").strip()


def _image_url(post: Dict[str, Any]) -> Optional[str]:
    img = post.get("image") or {}
    return img.get("url") or img.get("public_url") or None


async def _exec(
    db, org_id: str, user_id: str, tool_slug: str, args: Dict[str, Any],
) -> Dict[str, Any]:
    return await composio.execute_tool(db, org_id, user_id, tool_slug, args)


def _result_payload(success: bool, *, tool: str, response: Any = None, error: Optional[str] = None) -> Dict[str, Any]:
    return {"ok": success, "tool": tool, "response": response, "error": error if not success else None}


def _result_envelope(exec_result: Dict[str, Any], *, tool: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Translate `execute_tool` envelope → publish_results envelope."""
    ok = bool(exec_result.get("success"))
    out = {
        "ok": ok,
        "tool": tool,
        "response": exec_result.get("response"),
        "error": exec_result.get("error") if not ok else None,
    }
    if extra:
        out.update(extra)
    return out


def _extract_id(resp: Any) -> Optional[str]:
    """Pull an `id` out of a Composio tool response. Composio wraps the
    third-party response — we defensively check a couple of shapes.
    """
    if not isinstance(resp, dict):
        return None
    for shape in (resp.get("data"), resp.get("response"), resp):
        if isinstance(shape, dict) and shape.get("id"):
            return str(shape["id"])
    return None


def _flatten_pages(resp: Any) -> List[Dict[str, Any]]:
    """Pull the list of {id,name,...} pages out of a Composio response."""
    if not isinstance(resp, dict):
        return []
    for shape in (resp.get("data"), resp.get("response"), resp):
        if isinstance(shape, dict):
            data = shape.get("data")
            if isinstance(data, list):
                return [p for p in data if isinstance(p, dict)]
            if isinstance(shape.get("pages"), list):
                return [p for p in shape["pages"] if isinstance(p, dict)]
    return []


# ── Per-platform publish flows ──────────────────────────────────────────────
async def _publish_linkedin(db, org_id, user_id, post, conn) -> Dict[str, Any]:
    return _result_envelope(await _exec(
        db, org_id, user_id, "LINKEDIN_CREATE_LINKED_IN_POST",
        {"commentary": _text_for(post, "linkedin") or post.get("text", "")},
    ), tool="LINKEDIN_CREATE_LINKED_IN_POST")


async def _publish_facebook(db, org_id, user_id, post, conn) -> Dict[str, Any]:
    """Facebook posts go to a specific Page, not the user's profile.
    Page ID is captured at activation time (or just-in-time below).
    """
    page_id = conn.get("facebook_page_id")
    if not page_id:
        page_id = await _resolve_facebook_page_id(db, org_id, user_id, conn)
        if not page_id:
            return _result_payload(False, tool="FACEBOOK_CREATE_POST",
                                   error="No Facebook Page found. You must connect a Page you admin.")
    img = _image_url(post)
    if img:
        # FB has dedicated photo+video endpoints. CREATE_PHOTO_POST
        # accepts page_id + url + caption.
        return _result_envelope(await _exec(
            db, org_id, user_id, "FACEBOOK_CREATE_PHOTO_POST",
            {
                "page_id": str(page_id),
                "url": img,
                "message": _text_for(post, "facebook") or post.get("text", ""),
                "published": True,
            },
        ), tool="FACEBOOK_CREATE_PHOTO_POST")
    return _result_envelope(await _exec(
        db, org_id, user_id, "FACEBOOK_CREATE_POST",
        {
            "page_id": str(page_id),
            "message": _text_for(post, "facebook") or post.get("text", ""),
            "published": True,
        },
    ), tool="FACEBOOK_CREATE_POST")


async def _resolve_instagram_user_id(
    db, org_id: str, user_id: str, conn: Dict[str, Any],
) -> Optional[str]:
    """Look up the user's IG Business Account ID and cache it on the
    connection row. Returns None if Composio can't resolve it (most
    common cause: IG account isn't Business/Creator linked to a FB
    Page the user admins).

    The CREATE_MEDIA step requires the *numeric* ID, not the 'me'
    alias — that was the bug we kept hitting.
    """
    try:
        res = await _exec(db, org_id, user_id, "INSTAGRAM_GET_USER_INFO", {})
    except Exception as exc:
        logger.warning("IG user info lookup failed: %s", exc)
        return None
    if not res.get("success"):
        return None
    info = res.get("response") or {}
    data = info.get("data") if isinstance(info, dict) and isinstance(info.get("data"), dict) else info
    ig_id = None
    username = None
    if isinstance(data, dict):
        ig_id = data.get("id") or data.get("user_id")
        username = data.get("username") or data.get("name")
    if ig_id:
        await db.composio_connections.update_one(
            {"connection_id": conn["connection_id"]},
            {"$set": {
                "instagram_user_id": str(ig_id),
                "instagram_username": username,
            }},
        )
    return str(ig_id) if ig_id else None


async def _publish_instagram(db, org_id, user_id, post, conn) -> Dict[str, Any]:
    """Instagram's modern flow is two Meta Graph API calls:
        1. POST_IG_USER_MEDIA       → returns container_id
        2. POST_IG_USER_MEDIA_PUBLISH → publishes the container

    Critical detail: step 1 REQUIRES the actual numeric IG Business
    Account ID — 'me' is NOT accepted (only the publish step accepts
    'me'). That was the silent-success bug. We cache the resolved ID
    on the connection row so subsequent publishes skip the lookup.
    """
    img = _image_url(post)
    if not img:
        return _result_payload(False, tool="INSTAGRAM_POST_IG_USER_MEDIA",
                               error="Instagram requires an image — generate or attach one first.")
    caption = _text_for(post, "instagram") or post.get("text", "")
    ig_user_id = conn.get("instagram_user_id")
    if not ig_user_id:
        ig_user_id = await _resolve_instagram_user_id(db, org_id, user_id, conn)
    if not ig_user_id:
        return _result_payload(
            False, tool="INSTAGRAM_GET_USER_INFO",
            error="Couldn't resolve Instagram Business Account ID. Confirm your IG account is "
                  "Business/Creator and linked to a Facebook Page you admin.",
        )
    step1 = await _exec(
        db, org_id, user_id, "INSTAGRAM_POST_IG_USER_MEDIA",
        {"ig_user_id": str(ig_user_id), "image_url": img, "caption": caption},
    )
    if not step1.get("success"):
        return _result_payload(False, tool="INSTAGRAM_POST_IG_USER_MEDIA",
                               error=step1.get("error") or "Container creation failed",
                               response=step1.get("response"))
    container_id = _extract_id(step1.get("response") or {})
    if not container_id:
        return _result_payload(False, tool="INSTAGRAM_POST_IG_USER_MEDIA",
                               error="No container ID returned from Instagram",
                               response=step1.get("response"))
    step2 = await _exec(
        db, org_id, user_id, "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH",
        {"ig_user_id": str(ig_user_id), "creation_id": str(container_id)},
    )
    return _result_envelope(step2, tool="INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH",
                             extra={"container_id": container_id})


async def _publish_reddit(db, org_id, user_id, post, conn) -> Dict[str, Any]:
    variants = post.get("variants") or {}
    rv = variants.get("reddit") or {}
    sub = rv.get("subreddit") or post.get("reddit_subreddit")
    if not sub:
        return _result_payload(False, tool="REDDIT_CREATE_REDDIT_POST",
                               error="Reddit post needs a `subreddit` — set it in the post variants.")
    text = _text_for(post, "reddit") or post.get("text", "")
    title = rv.get("title") or (text.splitlines()[0][:300] if text else "Untitled")
    body = rv.get("body") or text
    return _result_envelope(await _exec(
        db, org_id, user_id, "REDDIT_CREATE_REDDIT_POST",
        {"subreddit": sub.lstrip("r/").strip(), "title": title, "content": body},
    ), tool="REDDIT_CREATE_REDDIT_POST")


async def _publish_tiktok(db, org_id, user_id, post, conn) -> Dict[str, Any]:
    img = _image_url(post)
    if not img:
        return _result_payload(False, tool="TIKTOK_POST_PHOTO",
                               error="TikTok requires a photo or video.")
    return _result_envelope(await _exec(
        db, org_id, user_id, "TIKTOK_POST_PHOTO",
        {"media_url": img, "title": _text_for(post, "tiktok") or post.get("text", "")[:150]},
    ), tool="TIKTOK_POST_PHOTO")


_PUBLISHERS = {
    "linkedin":  _publish_linkedin,
    "facebook":  _publish_facebook,
    "instagram": _publish_instagram,
    "reddit":    _publish_reddit,
    "tiktok":    _publish_tiktok,
}


# ── Facebook Page resolution ────────────────────────────────────────────────
async def _resolve_facebook_page_id(
    db, org_id: str, user_id: str, conn: Dict[str, Any],
) -> Optional[str]:
    """Find the FB Page this connection manages and cache it on the row."""
    try:
        res = await _exec(db, org_id, user_id, "FACEBOOK_LIST_MANAGED_PAGES", {})
    except Exception as exc:
        logger.warning("FB page lookup failed: %s", exc)
        return None
    if not res.get("success"):
        return None
    pages = _flatten_pages(res.get("response"))
    if not pages:
        return None
    pick = pages[0]
    page_id = str(pick.get("id") or "")
    page_name = pick.get("name") or ""
    if page_id:
        await db.composio_connections.update_one(
            {"connection_id": conn["connection_id"]},
            {"$set": {
                "facebook_page_id": page_id,
                "facebook_page_name": page_name,
                "facebook_pages": pages[:10],
            }},
        )
    return page_id or None


async def discover_connection_metadata(
    db, *, org_id: str, user_id: str, toolkit: str,
) -> Dict[str, Any]:
    """Probe Composio for human-readable connection details (FB Page name,
    IG @handle) and persist them so the UI can show "Connected to AZ Pain
    Doctors (Page)" instead of just "Connected".
    """
    conn = await _active_connection(db, org_id, user_id, toolkit)
    if not conn:
        return {}
    out: Dict[str, Any] = {}
    if toolkit == "facebook":
        page_id = conn.get("facebook_page_id")
        if not page_id:
            page_id = await _resolve_facebook_page_id(db, org_id, user_id, conn)
        if page_id:
            conn = await _active_connection(db, org_id, user_id, toolkit) or conn
            out["facebook_page_id"] = page_id
            out["facebook_page_name"] = conn.get("facebook_page_name")
            out["display_label"] = conn.get("facebook_page_name") or f"Page {page_id}"
            out["pages"] = conn.get("facebook_pages") or []
    elif toolkit == "instagram":
        ig_user_id = conn.get("instagram_user_id")
        if not ig_user_id:
            ig_user_id = await _resolve_instagram_user_id(db, org_id, user_id, conn)
        if ig_user_id:
            # Re-read so we pick up the username that resolve cached.
            conn = await _active_connection(db, org_id, user_id, toolkit) or conn
            handle = conn.get("instagram_username")
            out["instagram_user_id"] = str(ig_user_id)
            out["instagram_username"] = handle
            out["display_label"] = f"@{handle}" if handle else f"IG {ig_user_id}"
    return out


# ── Public entry point ─────────────────────────────────────────────────────
async def publish_post(
    db,
    *,
    post: Dict[str, Any],
    actor_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Fan a post out to all of its requested platforms."""
    pid = post.get("id") or post.get("_id")
    org_id = post.get("org_id")
    user_id = post.get("created_by") or post.get("user_id") or actor_user_id
    if not (org_id and user_id):
        return {
            "status": "failed", "results": {}, "published_count": 0,
            "failed_count": 0, "error": "Post is missing org_id or user_id",
        }
    platforms = list(post.get("platforms") or [])
    if not platforms:
        return {"status": "skipped", "results": {}, "published_count": 0, "failed_count": 0}

    await db.social_posts.update_one(
        {"_id": post["_id"]} if "_id" in post else {"id": pid},
        {"$set": {"status": "publishing", "publish_started_at": datetime.now(timezone.utc)}},
    )

    results: Dict[str, Dict[str, Any]] = {}
    ok = 0
    bad = 0
    for platform in platforms:
        toolkit = PLATFORM_TOOLKITS.get(platform)
        if not toolkit:
            results[platform] = {"ok": False, "error": "Platform not supported for direct publishing yet"}
        else:
            conn = await _active_connection(db, org_id, user_id, toolkit)
            if not conn:
                results[platform] = {"ok": False, "error": f"No connected account for {toolkit}"}
            else:
                fn = _PUBLISHERS[platform]
                try:
                    results[platform] = await fn(db, org_id, user_id, post, conn)
                except Exception as exc:
                    logger.exception("Publish %s failed for post %s", platform, pid)
                    results[platform] = {"ok": False, "tool": "", "error": str(exc)[:300]}
        if results[platform].get("ok"):
            ok += 1
        else:
            bad += 1
            # Log Meta's actual rejection so production logs carry the
            # signal we need to diagnose silent failures. The frontend's
            # publish_results chip should already surface this, but logs
            # are the ground truth when the chip is missing for any reason.
            logger.warning(
                "social-publish FAIL: post=%s platform=%s tool=%s error=%s response=%s",
                pid, platform, results[platform].get("tool"),
                results[platform].get("error"),
                str(results[platform].get("response"))[:400],
            )
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
    logger.info("social-publish: post=%s status=%s ok=%d bad=%d", pid, final, ok, bad)
    return {
        "status": final, "results": results,
        "published_count": ok, "failed_count": bad,
    }
