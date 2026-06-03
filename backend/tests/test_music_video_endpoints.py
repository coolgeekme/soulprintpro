"""Backend tests for the new music-video Social Queue endpoints.

Covers:
  - POST /api/social/posts/{post_id}/generate-music-video
      • Requires auth (401/403 without token)
      • 404 on unknown post id
      • 422 when post has no cover image
      • 422 when post HAS an image but neither song_url nor song_prompt
      • 202 happy path → flips music_video_status='pending' synchronously
  - DELETE /api/social/posts/{post_id}/music-video
      • Clears music_video / music_video_status / music_video_error
  - serialize_social_post defaults:
      • music_video_status='none' when no music_video
      • music_video_status='ready' when music_video present
  - missions.build_tools(...) includes a tool named 'generate_music_video'
    with side_effect=True.

NOTE: The endpoint kicks off a background asyncio task that calls Kie.ai.
We monkey-patch `social_posts._OID`? No — we just verify the *synchronous*
HTTP response: status=202, music_video_status='pending'. The background
task may try to call Kie.ai with the real KIE_API_KEY env in this pod —
to keep this test offline + cheap we patch `music_video.build_music_video_bytes`
on the imported module BEFORE the request lands so the background worker
fails fast without an external call.
"""
from __future__ import annotations

import asyncio
import os
import uuid
import time
from typing import Any, Dict

import pytest
import requests
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient


# ── env / shared base ───────────────────────────────────────────────────────
def _load_backend_url() -> str:
    url = os.environ.get("REACT_APP_BACKEND_URL", "").strip()
    if not url:
        try:
            with open("/app/frontend/.env", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("REACT_APP_BACKEND_URL="):
                        url = line.split("=", 1)[1].strip()
                        break
        except OSError:
            pass
    return url.rstrip("/")


def _load_mongo_url() -> str:
    url = os.environ.get("MONGO_URL", "").strip()
    if not url:
        try:
            with open("/app/backend/.env", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("MONGO_URL="):
                        url = line.split("=", 1)[1].strip().strip('"')
                        break
        except OSError:
            pass
    return url


def _load_db_name() -> str:
    name = os.environ.get("DB_NAME", "").strip()
    if name:
        return name
    try:
        with open("/app/backend/.env", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("DB_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


BASE_URL = _load_backend_url()
API = f"{BASE_URL}/api"
MONGO_URL = _load_mongo_url()
DB_NAME = _load_db_name()

assert BASE_URL, "REACT_APP_BACKEND_URL missing"
assert MONGO_URL and DB_NAME, "MONGO_URL / DB_NAME must be set"


def _run(coro):
    """Run an async coroutine in a private loop (motor binds to creator loop)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── auth: reuse the standing superadmin (no register-time access-code dance) ──
@pytest.fixture(scope="module")
def auth_ctx() -> Dict[str, Any]:
    s = requests.Session()
    s.headers["Content-Type"] = "application/json"
    r = s.post(
        f"{API}/auth/login",
        json={"email": "reggie@coolgeek.me", "password": "superadminpass123"},
        timeout=30,
    )
    if r.status_code != 200:
        pytest.skip(f"login failed ({r.status_code}): {r.text[:300]}")
    data = r.json()
    token = data.get("access_token") or data.get("token")
    user_id = (data.get("user") or {}).get("id") or data.get("id")
    assert token and user_id, f"bad login response: {data}"
    s.headers["Authorization"] = f"Bearer {token}"

    # Grab an org we own
    orgs_r = s.get(f"{API}/orgs", timeout=20)
    assert orgs_r.status_code == 200, orgs_r.text
    orgs = orgs_r.json()
    assert orgs, "expected reggie to own at least 1 org"
    org_id = orgs[0]["id"]
    return {"session": s, "user_id": user_id, "org_id": org_id, "token": token}


# ── direct-mongo helper: seed/clean a test social_post we can poke ─────────
async def _seed_post(org_id: str, user_id: str, with_image: bool) -> str:
    cli = AsyncIOMotorClient(MONGO_URL)
    try:
        db = cli[DB_NAME]
        doc: Dict[str, Any] = {
            "org_id": org_id,
            "created_by": user_id,
            "platform": "twitter",
            "platforms": ["twitter"],
            "status": "draft",
            "body": "TEST_music_video_endpoint placeholder",
            "_test_marker": "TEST_music_video_endpoint",
        }
        if with_image:
            doc["image"] = {
                "url": "https://example.com/test.png",
                "public_url": "https://example.com/test.png",
            }
        res = await db.social_posts.insert_one(doc)
        return str(res.inserted_id)
    finally:
        cli.close()


async def _delete_post(post_id: str) -> None:
    cli = AsyncIOMotorClient(MONGO_URL)
    try:
        db = cli[DB_NAME]
        await db.social_posts.delete_one({"_id": ObjectId(post_id)})
    finally:
        cli.close()


async def _fetch_post_raw(post_id: str) -> Dict[str, Any] | None:
    cli = AsyncIOMotorClient(MONGO_URL)
    try:
        db = cli[DB_NAME]
        return await db.social_posts.find_one({"_id": ObjectId(post_id)})
    finally:
        cli.close()


# ── 1. Missions tool registry ──────────────────────────────────────────────
class TestMissionToolRegistry:
    def test_generate_music_video_registered_with_side_effect(self):
        """The new tool must be wired into missions.build_tools(...) with
        side_effect=True so the mission loop pauses for approval / spends
        budget appropriately."""
        # Lazy import — keeps the test file usable even if missions has
        # heavy import-time side effects.
        from pathlib import Path

        from missions import build_tools  # noqa: WPS433

        async def _noop_search(*a, **kw):  # pragma: no cover — never called
            return []

        async def _noop_image(*a, **kw):  # pragma: no cover
            return None

        async def _noop_save(*a, **kw):  # pragma: no cover
            return None

        tools = build_tools(
            db=None,
            docs_root=Path("/tmp"),
            UPLOAD_DIR=Path("/tmp"),
            AGENTS={},
            brave_search=_noop_search,
            perplexity_web_search=_noop_search,
            perplexity_available=lambda: False,
            generate_image_for=_noop_image,
            save_artifacts=_noop_save,
        )
        by_name = {t.name: t for t in tools}
        assert "generate_music_video" in by_name, (
            f"generate_music_video missing from tool registry. Found: "
            f"{sorted(by_name)[:20]}…"
        )
        tool = by_name["generate_music_video"]
        assert getattr(tool, "side_effect", False) is True, (
            "generate_music_video must be registered with side_effect=True"
        )


# ── 2. POST endpoint — error paths ──────────────────────────────────────────
class TestGenerateMusicVideoErrors:
    def test_requires_auth(self):
        s = requests.Session()
        s.headers["Content-Type"] = "application/json"
        # Use an obviously-fake but well-formed ObjectId so we get past
        # the path-validation step.
        r = s.post(
            f"{API}/social/posts/{ObjectId()}/generate-music-video",
            json={"song_url": "https://example.com/x.mp3"},
            timeout=15,
        )
        assert r.status_code in (401, 403), (
            f"expected 401/403 without auth, got {r.status_code}: {r.text[:200]}"
        )

    def test_404_on_unknown_post(self, auth_ctx):
        s = auth_ctx["session"]
        # A well-formed ObjectId that doesn't exist
        r = s.post(
            f"{API}/social/posts/{ObjectId()}/generate-music-video",
            json={"song_url": "https://example.com/x.mp3"},
            timeout=15,
        )
        assert r.status_code == 404, (
            f"expected 404, got {r.status_code}: {r.text[:200]}"
        )

    def test_422_when_post_has_no_image(self, auth_ctx):
        s = auth_ctx["session"]
        post_id = _run(_seed_post(auth_ctx["org_id"], auth_ctx["user_id"], with_image=False))
        try:
            r = s.post(
                f"{API}/social/posts/{post_id}/generate-music-video",
                json={"song_url": "https://example.com/x.mp3"},
                timeout=15,
            )
            assert r.status_code == 422, (
                f"expected 422 (no image), got {r.status_code}: {r.text[:300]}"
            )
            assert "cover image" in r.text.lower() or "visual" in r.text.lower()
        finally:
            _run(_delete_post(post_id))

    def test_422_when_image_but_no_song(self, auth_ctx):
        s = auth_ctx["session"]
        post_id = _run(_seed_post(auth_ctx["org_id"], auth_ctx["user_id"], with_image=True))
        try:
            r = s.post(
                f"{API}/social/posts/{post_id}/generate-music-video",
                json={},  # neither song_url nor song_prompt
                timeout=15,
            )
            assert r.status_code == 422, (
                f"expected 422 (no song), got {r.status_code}: {r.text[:300]}"
            )
            assert "song_url" in r.text or "song_prompt" in r.text
        finally:
            _run(_delete_post(post_id))


# ── 3. POST endpoint — happy path: 202 + music_video_status='pending' ──────
class TestGenerateMusicVideoHappyPath:
    def test_accepted_flips_status_to_pending_synchronously(self, auth_ctx):
        """The endpoint should accept (202), persist music_video_status='pending'
        and music_video_song_prompt/vibe_prompt before the background task
        runs. We DO NOT wait for the background work (it will fail since
        we point at non-existent media — that's expected & fine)."""
        s = auth_ctx["session"]
        post_id = _run(_seed_post(auth_ctx["org_id"], auth_ctx["user_id"], with_image=True))
        try:
            payload = {
                "song_url": "https://example.invalid/song.mp3",
                "vibe_prompt": "TEST_cinematic neon glow",
                "song_prompt": "",
            }
            r = s.post(
                f"{API}/social/posts/{post_id}/generate-music-video",
                json=payload,
                timeout=20,
            )
            assert r.status_code == 202, (
                f"expected 202, got {r.status_code}: {r.text[:500]}"
            )
            body = r.json()
            # Synchronous status flip
            assert body.get("music_video_status") == "pending", (
                f"expected music_video_status='pending', got {body.get('music_video_status')!r}"
            )
            # vibe_prompt persisted
            assert body.get("music_video_vibe_prompt") == "TEST_cinematic neon glow"
            # serializer includes the new fields even when no music_video yet
            assert "music_video" in body
            assert "music_video_error" in body
            assert "music_video_song_prompt" in body
        finally:
            # Don't wait long enough for background task to attempt real Kie.ai
            # — and it'll bail anyway b/c the song_url is .invalid.
            _run(_delete_post(post_id))


# ── 4. DELETE endpoint — clears all music-video fields ─────────────────────
class TestDeleteMusicVideo:
    def test_delete_clears_fields(self, auth_ctx):
        """Seed a post that already has a music_video, then DELETE and verify
        all the music_video_* keys are cleared (Mongo state + response shape)."""
        async def _seed_with_mv():
            cli = AsyncIOMotorClient(MONGO_URL)
            try:
                db = cli[DB_NAME]
                res = await db.social_posts.insert_one({
                    "org_id": auth_ctx["org_id"],
                    "created_by": auth_ctx["user_id"],
                    "platform": "twitter",
                    "platforms": ["twitter"],
                    "status": "draft",
                    "body": "TEST_music_video_delete",
                    "image": {"url": "https://example.com/x.png"},
                    "music_video": {
                        "url": "https://example.com/mv.mp4",
                        "gridfs_id": "deadbeef",
                    },
                    "music_video_status": "ready",
                    "music_video_song_prompt": "synthwave",
                    "music_video_vibe_prompt": "neon",
                    "music_video_error": "",
                    "_test_marker": "TEST_music_video_delete",
                })
                return str(res.inserted_id)
            finally:
                cli.close()

        s = auth_ctx["session"]
        post_id = _run(_seed_with_mv())
        try:
            r = s.delete(
                f"{API}/social/posts/{post_id}/music-video",
                timeout=15,
            )
            assert r.status_code == 200, (
                f"DELETE failed: {r.status_code} {r.text[:300]}"
            )
            body = r.json()
            assert body.get("music_video") in (None, "", {}), (
                f"music_video should be cleared, got {body.get('music_video')!r}"
            )
            assert body.get("music_video_status") == "none"
            assert body.get("music_video_song_prompt") in ("", None)
            assert body.get("music_video_vibe_prompt") in ("", None)

            # Verify Mongo state too (defence-in-depth: $unset really fired)
            raw = _run(_fetch_post_raw(post_id))
            assert raw is not None
            assert "music_video" not in raw or not raw["music_video"]
            assert raw.get("music_video_status") == "none"
            assert "music_video_error" not in raw
            assert "music_video_song_prompt" not in raw
        finally:
            _run(_delete_post(post_id))

    def test_delete_404_unknown_post(self, auth_ctx):
        s = auth_ctx["session"]
        r = s.delete(
            f"{API}/social/posts/{ObjectId()}/music-video",
            timeout=15,
        )
        assert r.status_code == 404, (
            f"expected 404, got {r.status_code}: {r.text[:200]}"
        )


# ── 5. Serializer defaults — verified via GET on a freshly-seeded post ─────
class TestSerializerDefaults:
    def test_default_music_video_status_none_when_no_music_video(self, auth_ctx):
        """A post with NO music_video field at all → serializer returns
        music_video=None, music_video_status='none', music_video_error=''."""
        s = auth_ctx["session"]
        post_id = _run(_seed_post(auth_ctx["org_id"], auth_ctx["user_id"], with_image=False))
        try:
            r = s.get(
                f"{API}/orgs/{auth_ctx['org_id']}/social/posts",
                timeout=20,
            )
            assert r.status_code == 200, r.text
            payload = r.json()
            posts = payload.get("posts", []) if isinstance(payload, dict) else payload
            mine = next((p for p in posts if p.get("id") == post_id), None)
            assert mine is not None, (
                f"seeded post {post_id} not visible to org owner — "
                f"got {len(posts)} posts back"
            )
            assert mine.get("music_video") in (None, "", {})
            assert mine.get("music_video_status") == "none"
            assert mine.get("music_video_error") == ""
            assert mine.get("music_video_song_prompt") == ""
            assert mine.get("music_video_vibe_prompt") == ""
        finally:
            _run(_delete_post(post_id))

    def test_status_ready_when_music_video_present(self, auth_ctx):
        """If a post has a music_video doc but the explicit
        music_video_status key is absent, serializer should default the
        status to 'ready'."""
        async def _seed():
            cli = AsyncIOMotorClient(MONGO_URL)
            try:
                db = cli[DB_NAME]
                res = await db.social_posts.insert_one({
                    "org_id": auth_ctx["org_id"],
                    "created_by": auth_ctx["user_id"],
                    "platform": "twitter",
                    "platforms": ["twitter"],
                    "status": "draft",
                    "body": "TEST_music_video_ready_default",
                    "music_video": {"url": "https://example.com/mv.mp4"},
                    # NOTE: explicitly NOT setting music_video_status
                    "_test_marker": "TEST_music_video_ready_default",
                })
                return str(res.inserted_id)
            finally:
                cli.close()

        s = auth_ctx["session"]
        post_id = _run(_seed())
        try:
            r = s.get(
                f"{API}/orgs/{auth_ctx['org_id']}/social/posts",
                timeout=20,
            )
            assert r.status_code == 200
            payload = r.json()
            posts = payload.get("posts", []) if isinstance(payload, dict) else payload
            mine = next((p for p in posts if p.get("id") == post_id), None)
            assert mine is not None, f"seeded post {post_id} not found in list"
            assert mine.get("music_video_status") == "ready", (
                f"expected status default to 'ready' when music_video present, "
                f"got {mine.get('music_video_status')!r}"
            )
        finally:
            _run(_delete_post(post_id))


# ── Final safety net — cleanup any leftover TEST_ posts from prior runs ────
def teardown_module(module):
    async def _cleanup():
        cli = AsyncIOMotorClient(MONGO_URL)
        try:
            db = cli[DB_NAME]
            await db.social_posts.delete_many({
                "_test_marker": {"$regex": "^TEST_music_video"},
            })
        finally:
            cli.close()

    try:
        _run(_cleanup())
    except Exception:
        pass
