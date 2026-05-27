"""Regression: IG Reels / TikTok video / YouTube Shorts auto-publish path.

User asked for the autopost worker to handle video covers for these
three platforms. We just rewrote the publishers in social_publishing.py
— this test pins the slug names, arg shapes, and platform→toolkit
mapping so a future refactor can't silently regress them.

We mock `composio.execute_tool` instead of doing real Composio calls
so the test is hermetic and fast.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import social_publishing as sp  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────


def _post(**overrides):
    base = {
        "_id": "post-1",
        "id": "post-1",
        "org_id": "org-1",
        "created_by": "user-1",
        "platforms": ["instagram"],
        "text": "Spring drop is here.",
        "hashtags": ["spring", "halfsalt"],
        "image": None,
        "video": None,
    }
    base.update(overrides)
    return base


def _video_post(platform: str, **overrides):
    return _post(
        platforms=[platform],
        video={
            "filename": "post.mp4",
            "url": "/api/files/post.mp4",
            "remote_url": "https://cdn.kie.ai/v/post.mp4",
            "model": "veo3_fast",
            "aspect_ratio": "9:16",
        },
        video_aspect_ratio="9:16",
        **overrides,
    )


# ── Platform registry ────────────────────────────────────────────────


def test_platform_toolkits_include_video_platforms():
    for p in ("instagram", "tiktok", "youtube", "facebook"):
        assert p in sp.PLATFORM_TOOLKITS, f"{p} must be in PLATFORM_TOOLKITS"
    # And the dispatch dict has matching keys
    for p in ("instagram", "tiktok", "youtube", "facebook"):
        assert p in sp._PUBLISHERS, f"{p} must be in _PUBLISHERS"


# ── _video_url / _platform_wants_video ───────────────────────────────


def test_video_url_prefers_remote_url():
    post = _video_post("tiktok")
    assert sp._video_url(post) == "https://cdn.kie.ai/v/post.mp4"


def test_video_url_falls_back_to_local_url():
    post = _post(
        platforms=["tiktok"],
        video={
            "filename": "post.mp4",
            "url": "/api/files/post.mp4",
        },
    )
    out = sp._video_url(post)
    assert out is not None and out.endswith("/api/files/post.mp4")
    assert out.startswith("http")  # absolutized


def test_platform_wants_video_only_when_video_present():
    assert sp._platform_wants_video(_video_post("tiktok"), "tiktok")
    assert not sp._platform_wants_video(_post(platforms=["tiktok"]), "tiktok")
    # Linkedin/twitter/reddit should NOT prefer video even when present
    assert not sp._platform_wants_video(_video_post("twitter"), "twitter")
    assert not sp._platform_wants_video(_video_post("linkedin"), "linkedin")


# ── Instagram Reels ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_instagram_reels_uses_video_url_and_media_type():
    post = _video_post("instagram", text="My new IG Reel about HalfSalt")
    conn = {"connection_id": "c-1", "instagram_user_id": "17841234567890"}
    captured: list[tuple[str, dict]] = []

    async def mock_execute(db, org_id, user_id, slug, args):
        captured.append((slug, args))
        if slug == "INSTAGRAM_POST_IG_USER_MEDIA":
            return {"success": True, "response": {"id": "container-123"}}
        if slug == "INSTAGRAM_GET_MEDIA_CONTAINER_STATUS":
            return {"success": True, "response": {"data": {"status_code": "FINISHED"}}}
        if slug == "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH":
            return {"success": True, "response": {"id": "ig-media-999"}}
        return {"success": False, "error": f"unexpected slug {slug}"}

    with patch.object(sp.composio, "execute_tool", new=AsyncMock(side_effect=mock_execute)):
        out = await sp._publish_instagram(None, "org-1", "user-1", post, conn)

    assert out["ok"], f"Expected ok, got {out!r}"
    assert out["tool"] == "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH"
    assert out.get("media_type") == "REELS"
    create = next(args for slug, args in captured if slug == "INSTAGRAM_POST_IG_USER_MEDIA")
    assert create["media_type"] == "REELS"
    assert create["video_url"] == "https://cdn.kie.ai/v/post.mp4"
    assert "image_url" not in create
    # We MUST poll container status before publishing the Reel
    assert any(slug == "INSTAGRAM_GET_MEDIA_CONTAINER_STATUS" for slug, _ in captured)


@pytest.mark.asyncio
async def test_instagram_image_post_unchanged_when_no_video():
    post = _post(
        platforms=["instagram"],
        image={"url": "/api/files/cover.png"},
    )
    conn = {"connection_id": "c-1", "instagram_user_id": "17841234567890"}
    captured: list[tuple[str, dict]] = []

    async def mock_execute(db, org_id, user_id, slug, args):
        captured.append((slug, args))
        if slug == "INSTAGRAM_POST_IG_USER_MEDIA":
            return {"success": True, "response": {"id": "ct-1"}}
        return {"success": True, "response": {"id": "pub-1"}}

    with patch.object(sp.composio, "execute_tool", new=AsyncMock(side_effect=mock_execute)):
        out = await sp._publish_instagram(None, "org-1", "user-1", post, conn)

    assert out["ok"]
    create = captured[0][1]
    assert "image_url" in create
    assert "video_url" not in create
    # No need to poll container for static images
    assert not any(slug == "INSTAGRAM_GET_MEDIA_CONTAINER_STATUS" for slug, _ in captured)


# ── TikTok video ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tiktok_video_uses_publish_video_with_public_url():
    post = _video_post("tiktok", text="HalfSalt drop · 60s edit")
    conn = {"connection_id": "c-tt"}
    captured: list[tuple[str, dict]] = []

    async def mock_execute(db, org_id, user_id, slug, args):
        captured.append((slug, args))
        return {"success": True, "response": {"publish_id": "tt-abc"}}

    with patch.object(sp.composio, "execute_tool", new=AsyncMock(side_effect=mock_execute)):
        out = await sp._publish_tiktok(None, "org-1", "user-1", post, conn)

    assert out["ok"]
    assert out["tool"] == "TIKTOK_PUBLISH_VIDEO"
    args = captured[0][1]
    assert args["video_url"] == "https://cdn.kie.ai/v/post.mp4"
    assert len(args["title"]) <= 150
    assert args["title"].startswith("HalfSalt drop")
    # Disclose flags must be present — TikTok rejects uploads without them
    for k in ("disable_comment", "disable_duet", "disable_stitch"):
        assert k in args, f"TikTok video args missing {k}"


@pytest.mark.asyncio
async def test_tiktok_falls_back_to_photo_when_no_video():
    post = _post(platforms=["tiktok"], image={"url": "/api/files/p.png"})
    conn = {"connection_id": "c-tt"}
    captured: list[tuple[str, dict]] = []

    async def mock_execute(db, org_id, user_id, slug, args):
        captured.append((slug, args))
        return {"success": True, "response": {"id": "x"}}

    with patch.object(sp.composio, "execute_tool", new=AsyncMock(side_effect=mock_execute)):
        out = await sp._publish_tiktok(None, "org-1", "user-1", post, conn)

    assert out["ok"]
    assert out["tool"] == "TIKTOK_POST_PHOTO"
    assert "media_url" in captured[0][1]


@pytest.mark.asyncio
async def test_tiktok_no_media_errors_clearly():
    post = _post(platforms=["tiktok"])
    conn = {"connection_id": "c-tt"}
    with patch.object(sp.composio, "execute_tool", new=AsyncMock()):
        out = await sp._publish_tiktok(None, "org-1", "user-1", post, conn)
    assert not out["ok"]
    assert "photo or video" in (out.get("error") or "")


# ── YouTube Shorts ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_youtube_upload_video_passes_title_description_tags():
    post = _video_post(
        "youtube",
        text="Headline of the Short\n\nLonger description with context.",
        hashtags=["spring", "#halfsalt"],
    )
    conn = {"connection_id": "c-yt"}
    captured: list[tuple[str, dict]] = []

    async def mock_execute(db, org_id, user_id, slug, args):
        captured.append((slug, args))
        return {"success": True, "response": {"id": "yt-vid-1"}}

    with patch.object(sp.composio, "execute_tool", new=AsyncMock(side_effect=mock_execute)):
        out = await sp._publish_youtube(None, "org-1", "user-1", post, conn)

    assert out["ok"], f"Expected ok, got {out!r}"
    assert out["tool"] == "YOUTUBE_UPLOAD_VIDEO"
    args = captured[0][1]
    assert args["video_url"] == "https://cdn.kie.ai/v/post.mp4"
    # Title is first non-empty line, capped at 100 chars
    assert args["title"] == "Headline of the Short"
    # Description carries the whole body
    assert "Longer description with context" in args["description"]
    # Hashtags lose their # prefix
    assert args["tags"] == ["spring", "halfsalt"]
    # Privacy defaults to public; made_for_kids defaults to False
    assert args["privacy_status"] == "public"
    assert args["made_for_kids"] is False
    # 9:16 → flagged as Short
    assert out.get("is_short") is True


@pytest.mark.asyncio
async def test_youtube_no_video_errors_clearly():
    post = _post(platforms=["youtube"])
    conn = {"connection_id": "c-yt"}
    with patch.object(sp.composio, "execute_tool", new=AsyncMock()):
        out = await sp._publish_youtube(None, "org-1", "user-1", post, conn)
    assert not out["ok"]
    assert "video" in (out.get("error") or "").lower()


@pytest.mark.asyncio
async def test_youtube_respects_privacy_override_from_variants():
    post = _video_post(
        "youtube",
        text="Private launch test",
        variants={"youtube": {"privacy": "unlisted"}},
    )
    conn = {"connection_id": "c-yt"}
    captured: list[tuple[str, dict]] = []

    async def mock_execute(db, org_id, user_id, slug, args):
        captured.append((slug, args))
        return {"success": True, "response": {"id": "x"}}

    with patch.object(sp.composio, "execute_tool", new=AsyncMock(side_effect=mock_execute)):
        await sp._publish_youtube(None, "org-1", "user-1", post, conn)
    assert captured[0][1]["privacy_status"] == "unlisted"


# ── Facebook video path ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_facebook_video_uses_create_video_post():
    """When a FB post has a video attached, we hit FACEBOOK_CREATE_VIDEO_POST
    instead of the photo / text endpoints."""
    post = _video_post("facebook", text="FB video promo")
    conn = {"connection_id": "c-fb", "facebook_page_id": "1234567890"}
    captured: list[tuple[str, dict]] = []

    async def mock_execute(db, org_id, user_id, slug, args):
        captured.append((slug, args))
        if slug.startswith("FACEBOOK_GET"):
            return {"success": True, "response": {"data": [{"id": "1234567890"}]}}
        return {"success": True, "response": {"id": "fb-vid-1"}}

    with patch.object(sp.composio, "execute_tool", new=AsyncMock(side_effect=mock_execute)):
        out = await sp._publish_facebook(None, "org-1", "user-1", post, conn)

    assert out["ok"], f"Expected ok, got {out!r}"
    assert out["tool"] == "FACEBOOK_CREATE_VIDEO_POST"
    args = next(a for s, a in captured if s == "FACEBOOK_CREATE_VIDEO_POST")
    assert args["url"] == "https://cdn.kie.ai/v/post.mp4"
    assert args["page_id"] == "1234567890"
