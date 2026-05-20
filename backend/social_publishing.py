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
import os
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


async def meta_target_status_for_user(
    db, org_id: str, user_id: str,
) -> Dict[str, Any]:
    """Return the FB / IG target-pin state for this user+org. Used by
    SocialPage to render the "Posting to: <Page>" hint + a Change link
    inline on each FB/IG post. Returns {} when no connection exists.
    """
    out: Dict[str, Any] = {}
    for toolkit in ("facebook", "instagram"):
        conn = await _active_connection(db, org_id, user_id, toolkit)
        if not conn:
            out[toolkit] = {"connected": False}
            continue
        out[toolkit] = {
            "connected": True,
            "connection_id": conn.get("connection_id"),
            "page_id": conn.get("facebook_page_id") or "",
            "page_name": conn.get("facebook_page_name") or "",
            "ig_user_id": conn.get("instagram_user_id") or "",
            "ig_username": conn.get("instagram_username") or "",
            "has_target": bool(
                conn.get("facebook_page_id")
                if toolkit == "facebook"
                else conn.get("instagram_user_id")
            ),
        }
    return out


# ── Helpers ─────────────────────────────────────────────────────────────────
def _text_for(post: Dict[str, Any], platform: str) -> str:
    variants = post.get("variants") or {}
    pv = variants.get(platform)
    if pv:
        return (pv.get("text") or pv.get("body") or "").strip()
    return (post.get("text") or "").strip()


def _public_origin() -> str:
    """Resolve the fully-qualified `https://host` to prefix relative
    media URLs with before handing them to third-party publishing APIs.

    Meta's Graph API (Instagram, Facebook) and TikTok's Content API
    both REQUIRE a public ``https://…`` URL on a routable hostname —
    they fetch the asset server-side; a relative ``/api/files/<file>``
    is rejected (or worse, silently fails container creation with a
    Graph API ``error.message="Invalid parameter"`` that's hard to
    correlate).

    Precedence:
      1. ``PUBLIC_ORIGIN_OVERRIDE`` — same env knob the audit-email
         flow uses; lets ops point publishing at a specific CDN host
         without touching ``FRONTEND_URL``.
      2. ``FRONTEND_URL`` — the canonical user-facing host. Already
         set in every env (preview + production).
    """
    origin = (os.environ.get("PUBLIC_ORIGIN_OVERRIDE") or "").strip()
    if not origin:
        origin = (os.environ.get("FRONTEND_URL") or "").strip()
    return origin.rstrip("/")


def _absolutize(url: Optional[str]) -> Optional[str]:
    """Promote a relative ``/api/files/...`` (or any other relative
    path) to an absolute ``https://host/api/files/...`` URL using the
    public origin. Pass-through for already-absolute URLs and ``None``.
    """
    if not url:
        return url
    if url.startswith(("http://", "https://")):
        return url
    origin = _public_origin()
    if not origin:
        # No origin configured — return as-is and let the publisher
        # surface the inevitable Meta "Invalid parameter" error to the
        # user. Better that than swallowing a mis-config silently.
        logger.warning(
            "No PUBLIC_ORIGIN_OVERRIDE / FRONTEND_URL set — relative "
            "media URL %s will be sent to a third-party API as-is and "
            "will almost certainly fail.", url,
        )
        return url
    if url.startswith("/"):
        return f"{origin}{url}"
    return f"{origin}/{url}"


def _image_url(post: Dict[str, Any]) -> Optional[str]:
    """Return the absolute, https-prefixed image URL for `post`.

    Stored URLs are relative paths like ``/api/files/<filename>`` —
    Meta/TikTok publishers can't fetch those, so every publisher
    pulls through this helper before passing it to the third-party
    API. Prefer ``public_url`` (explicitly authoritative) over ``url``
    when both are present.
    """
    img = post.get("image") or {}
    raw = img.get("public_url") or img.get("url") or None
    return _absolutize(raw)


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
    """Publish to a Facebook Page. Requires a user-pinned Page target.
    `_resolve_facebook_page_id` now returns None if no target was
    selected — we surface that as an actionable error instead of
    silently posting to whatever Page Composio listed first.
    """
    page_id = await _resolve_facebook_page_id(db, org_id, user_id, conn)
    if not page_id:
        return _result_payload(
            False, tool="FACEBOOK_CREATE_POST",
            error=(
                "No Facebook Page selected for this org yet. Open the "
                "Connections panel → Facebook → Change target, and pick "
                "which Page this org should post to."
            ),
        )
    """Facebook posts go to a specific Page, not the user's profile.
    Page ID is the user-pinned target resolved above.
    """
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
    # Prefer the user-pinned IG Business Account (set via the Page picker
    # — see select_meta_target). Fall back to the legacy auto-resolve
    # ONLY if no pin exists, and tell the user how to fix it if even
    # that fails.
    ig_user_id = conn.get("instagram_user_id")
    if not ig_user_id:
        ig_user_id = await _resolve_instagram_user_id(db, org_id, user_id, conn)
    if not ig_user_id:
        return _result_payload(
            False, tool="INSTAGRAM_GET_USER_INFO",
            error=(
                "No Instagram Business Account selected for this org. "
                "Open the Connections panel → Facebook → Change target, "
                "and pick a Page that has a linked IG Business/Creator "
                "account. Composio publishes IG through the linked "
                "Page's access token — there's no IG-without-Page path."
            ),
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
async def list_available_meta_targets(
    db, org_id: str, user_id: str, conn: Dict[str, Any],
) -> Dict[str, Any]:
    """Return every FB Page this connection can manage, plus the linked
    Instagram Business account attached to each Page (when one exists).

    Shape:
        {
          "pages": [
            {
              "page_id": "1234",
              "page_name": "AZ Pain Doctors",
              "category": "Medical Clinic",
              "linked_instagram": {
                "ig_user_id": "1789…",
                "username": "azpaindocs",
              } | None,
            }, …
          ],
          "selected_page_id": "<currently pinned page or '' >",
        }

    This is the canonical source the picker UI uses. Composio's
    `FACEBOOK_LIST_MANAGED_PAGES` returns the bare Page list; to discover
    each Page's linked IG account we issue a follow-up
    `FACEBOOK_GET_PAGE_DETAILS` per page (light call). Results are NOT
    persisted here — they're freshly fetched each time the user opens the
    picker so they always see the live state.
    """
    out: Dict[str, Any] = {"pages": [], "selected_page_id": conn.get("facebook_page_id") or ""}
    try:
        res = await _exec(db, org_id, user_id, "FACEBOOK_LIST_MANAGED_PAGES", {})
    except Exception as exc:
        logger.warning("FB list pages failed: %s", exc)
        return out
    if not res.get("success"):
        out["error"] = res.get("error") or "Couldn't list your Facebook Pages."
        return out
    pages = _flatten_pages(res.get("response")) or []
    enriched: List[Dict[str, Any]] = []
    for p in pages[:25]:  # 25 ceiling is generous; most users have <5
        pid = str(p.get("id") or p.get("page_id") or "")
        if not pid:
            continue
        item: Dict[str, Any] = {
            "page_id": pid,
            "page_name": p.get("name") or p.get("page_name") or "Unnamed Page",
            "category": p.get("category") or "",
            "linked_instagram": None,
        }
        # Per-page IG lookup. Composio's typical surface is one of:
        # FACEBOOK_GET_PAGE_DETAILS, FACEBOOK_GET_PAGE_INFORMATION,
        # INSTAGRAM_GET_BUSINESS_ACCOUNT. We try each in order and
        # accept the first that returns an IG-linked payload. Failures
        # are silent — picker still renders, just without IG info.
        ig_info = await _try_ig_for_page(db, org_id, user_id, pid)
        if ig_info:
            item["linked_instagram"] = ig_info
        enriched.append(item)
    out["pages"] = enriched
    return out


async def _try_ig_for_page(
    db, org_id: str, user_id: str, page_id: str,
) -> Optional[Dict[str, Any]]:
    """Best-effort lookup of the IG Business account linked to one FB Page.

    Composio Meta surface exposes a few variants; we attempt them in
    priority order and return as soon as one yields an instagram id.

    NOTE: Meta's Graph API only returns ``instagram_business_account``
    when you explicitly request the field. Each candidate below passes
    a ``fields`` argument naming the IG fields we care about. We try
    both a comma-string and a list shape because Composio's tool
    wrappers vary by toolkit version on how they accept ``fields``.
    """
    ig_field_csv = "instagram_business_account{id,username,name,profile_picture_url},connected_instagram_account{id,username,name,profile_picture_url},name,id"
    ig_field_list = [
        "instagram_business_account",
        "connected_instagram_account",
        "name",
        "id",
    ]
    candidates = [
        ("FACEBOOK_GET_PAGE_DETAILS",     {"page_id": page_id, "fields": ig_field_csv}),
        ("FACEBOOK_GET_PAGE_DETAILS",     {"page_id": page_id, "fields": ig_field_list}),
        ("FACEBOOK_GET_PAGE_INFORMATION", {"page_id": page_id, "fields": ig_field_csv}),
        ("FACEBOOK_GET_PAGE_INFORMATION", {"page_id": page_id, "fields": ig_field_list}),
        ("FACEBOOK_GET_PAGE",             {"page_id": page_id, "fields": ig_field_csv}),
        # Bare calls as a last resort — some Composio wrappers always
        # include the IG fields regardless of the ``fields`` argument
        # and reject unknown args with 400.
        ("FACEBOOK_GET_PAGE_DETAILS",     {"page_id": page_id}),
        ("FACEBOOK_GET_PAGE_INFORMATION", {"page_id": page_id}),
        ("FACEBOOK_GET_PAGE",             {"page_id": page_id}),
    ]
    for tool, args in candidates:
        try:
            res = await _exec(db, org_id, user_id, tool, args)
        except Exception:
            continue
        if not res.get("success"):
            continue
        payload = res.get("response") or {}
        data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
        if not isinstance(data, dict):
            continue
        ig = (
            data.get("instagram_business_account")
            or data.get("connected_instagram_account")
        )
        if isinstance(ig, dict):
            ig_id = str(ig.get("id") or ig.get("ig_user_id") or "")
            username = ig.get("username") or ig.get("name") or ""
            if ig_id:
                logger.info(
                    "FB→IG link resolved for page %s via %s: ig_id=%s username=%s",
                    page_id, tool, ig_id, username,
                )
                return {"ig_user_id": ig_id, "username": username}
    # Final fallback: ask Composio's Instagram side directly. If the
    # user's IG connection exists and knows which FB Page it's behind,
    # we can reverse-resolve without needing the FB Page response.
    try:
        res = await _exec(
            db, org_id, user_id, "INSTAGRAM_GET_USER_INFO",
            {"fields": "id,username,name,profile_picture_url"},
        )
        if res.get("success"):
            payload = res.get("response") or {}
            data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
            if isinstance(data, dict):
                ig_id = str(data.get("id") or data.get("ig_user_id") or "")
                username = data.get("username") or data.get("name") or ""
                if ig_id:
                    logger.info(
                        "FB→IG link reverse-resolved via INSTAGRAM_GET_USER_INFO: ig_id=%s username=%s",
                        ig_id, username,
                    )
                    return {"ig_user_id": ig_id, "username": username}
    except Exception:
        pass
    logger.info(
        "FB→IG link lookup exhausted all candidates for page %s — no linked IG found",
        page_id,
    )
    return None


async def select_meta_target(
    db, org_id: str, user_id: str, conn: Dict[str, Any], page_id: str,
) -> Dict[str, Any]:
    """Pin a single Page (and its linked IG account) as the active
    publishing target for this connection. Used by the picker UI.

    We refresh the Page list at selection time to validate the choice
    and to capture the live page_name + linked IG (which may have
    changed since the user last connected).
    """
    targets = await list_available_meta_targets(db, org_id, user_id, conn)
    pages = targets.get("pages") or []
    match = next((p for p in pages if str(p.get("page_id")) == str(page_id)), None)
    if not match:
        raise ValueError("That Page is not in your Facebook account or you don't manage it.")
    update: Dict[str, Any] = {
        "facebook_page_id": str(match["page_id"]),
        "facebook_page_name": match.get("page_name") or "",
        "facebook_pages": pages[:25],
        "target_selected_at": datetime.now(timezone.utc),
        "target_selected_by": user_id,
    }
    ig = match.get("linked_instagram") or {}
    if ig.get("ig_user_id"):
        update["instagram_user_id"] = str(ig["ig_user_id"])
        update["instagram_username"] = ig.get("username") or ""
        update["instagram_linked_page_id"] = str(match["page_id"])
    else:
        # User picked a Page with no linked IG — clear any stale IG cache
        # so we don't accidentally publish to an old IG account.
        update["instagram_user_id"] = ""
        update["instagram_username"] = ""
        update["instagram_linked_page_id"] = ""
    await db.composio_connections.update_one(
        {"connection_id": conn["connection_id"]},
        {"$set": update},
    )
    # Mirror the IG selection onto the user's instagram connection
    # row (if one exists separately) so the IG publisher picks up the
    # same target without a second click.
    if ig.get("ig_user_id"):
        await db.composio_connections.update_one(
            {
                "composio_user_id": conn.get("composio_user_id"),
                "toolkit": "instagram",
                "status": "ACTIVE",
            },
            {"$set": {
                "instagram_user_id": str(ig["ig_user_id"]),
                "instagram_username": ig.get("username") or "",
                "instagram_linked_page_id": str(match["page_id"]),
                "target_selected_at": datetime.now(timezone.utc),
                "target_selected_by": user_id,
            }},
        )
    return {
        "selected_page": match,
        "instagram": ig if ig.get("ig_user_id") else None,
    }


async def _resolve_facebook_page_id(
    db, org_id: str, user_id: str, conn: Dict[str, Any],
) -> Optional[str]:
    """Resolve the FB Page for a publish call.

    Strict policy (Feb 2026): a user-selected `facebook_page_id` is
    REQUIRED. No more silent `pages[0]` fallback — that produced posts
    landing on the wrong Page with no way for the user to diagnose.
    Returns None when no target has been selected; the publisher uses
    that to surface a clear "select a Page first" error.
    """
    pinned = conn.get("facebook_page_id")
    return str(pinned) if pinned else None


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
