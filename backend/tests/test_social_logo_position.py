"""Backend tests for PATCH /api/social/posts/{id}/logo-position (iter_13).

Bug being verified: previously the endpoint silently swallowed every failure
mode (missing raw_filename, missing logo file on disk, missing primary_logo
on the org, compose helper missing) and returned 200, so the frontend showed
"Logo applied" success even though nothing changed.

Now the endpoint:
  - returns descriptive 422 errors for every failure path
  - falls back to image.filename as the base for legacy posts (where
    raw_filename was never persisted) when with_logo=False
  - guards against double-branding (with_logo=True without raw_filename)
"""
from __future__ import annotations

import asyncio
import io
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests
from bson import ObjectId
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

# ── Env / constants ─────────────────────────────────────────────────────────
load_dotenv("/app/backend/.env", override=False)

BACKEND_URL = os.environ.get("REACT_APP_BACKEND_URL", "").strip()
if not BACKEND_URL:
    with open("/app/frontend/.env", "r", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BACKEND_URL = line.split("=", 1)[1].strip()
                break
BASE_URL = BACKEND_URL.rstrip("/")
API = f"{BASE_URL}/api"
UPLOAD_DIR = Path("/app/backend/uploads")
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]


# ── Helpers ─────────────────────────────────────────────────────────────────
def _png_bytes(color=(220, 40, 90, 255), size=(256, 256)) -> bytes:
    from PIL import Image
    img = Image.new("RGBA", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _put_disk_image(name_prefix: str, color=(80, 120, 200, 255)) -> str:
    fname = f"{name_prefix}-{uuid.uuid4().hex[:10]}.png"
    (UPLOAD_DIR / fname).write_bytes(_png_bytes(color, (300, 300)))
    return fname


def _run(coro_factory):
    """Run async work in a fresh loop with a fresh motor client.

    Motor binds AsyncIOMotorClient to the loop where IO first runs, so we
    can't reuse a module-scoped client across per-test event loops. The
    factory receives the live `db` and returns the coroutine to await.
    """
    loop = asyncio.new_event_loop()
    try:
        client = AsyncIOMotorClient(MONGO_URL, io_loop=loop)
        try:
            return loop.run_until_complete(coro_factory(client[DB_NAME]))
        finally:
            client.close()
    finally:
        loop.close()


async def _insert_post_async(db, *, org_id: str, image: dict | None,
                             logo_position: str = "bottom-right") -> str:
    doc = {
        "org_id": org_id, "team_id": "", "conversation_id": "",
        "source_message_id": "",
        "agent_id": "marketing-social-media-strategist",
        "agent_name": "Test Agent",
        "status": "draft", "platforms": ["twitter"],
        "text": "TEST_logopos draft", "hashtags": [], "link_url": "",
        "media": [], "image": image, "logo_position": logo_position,
        "scheduled_for": None, "published_at": None,
        "created_at": datetime.now(timezone.utc),
        "created_by": "TEST_logopos_user",
    }
    res = await db.social_posts.insert_one(doc)
    return str(res.inserted_id)


def _insert_post(org_id: str, image: dict | None) -> str:
    return _run(lambda db: _insert_post_async(db, org_id=org_id, image=image))


def _patch_pos(s, pid, position):
    return s.patch(f"{API}/social/posts/{pid}/logo-position",
                   params={"position": position}, timeout=30)


def _detail(r):
    try:
        return r.json().get("detail", "")
    except Exception:
        return r.text


# ── Fixture: register user + create org + upload logo ───────────────────────
@pytest.fixture(scope="module")
def ctx():
    s = requests.Session()
    s.headers["Content-Type"] = "application/json"
    email = f"TEST_logopos_{uuid.uuid4().hex[:10]}@example.com"
    pwd = "StrongPass!234"

    r = s.post(f"{API}/auth/register",
               json={"email": email, "password": pwd, "name": "LogoPos Tester"},
               timeout=30)
    assert r.status_code in (200, 201), r.text[:300]
    token = r.json().get("access_token") or r.json().get("token")
    s.headers["Authorization"] = f"Bearer {token}"

    r = s.get(f"{API}/orgs", timeout=20)
    orgs = r.json()
    if not orgs:
        c = s.post(f"{API}/orgs",
                   json={"name": f"TEST_LogoPosOrg_{uuid.uuid4().hex[:6]}"},
                   timeout=60)
        assert c.status_code in (200, 201), c.text[:300]
        org_id = c.json().get("id")
    else:
        org_id = orgs[0]["id"]

    headers = {k: v for k, v in s.headers.items() if k.lower() != "content-type"}
    r = requests.post(
        f"{API}/orgs/{org_id}/brand/logo",
        params={"kind": "primary"},
        files={"file": ("test_logo.png",
                        _png_bytes((20, 200, 120, 255), (96, 96)), "image/png")},
        headers=headers, timeout=30,
    )
    assert r.status_code == 200, r.text[:300]
    logo_filename = r.json()["primary_logo"]["filename"]
    assert (UPLOAD_DIR / logo_filename).is_file()

    return {"session": s, "org_id": org_id, "logo_filename": logo_filename}


# ── 1. Happy path — modern post with raw_filename ───────────────────────────
class TestHappyPath:
    def test_modern_post_recomposites(self, ctx):
        raw_fn = _put_disk_image("rawbase")
        composed_seed = _put_disk_image("composed-seed")
        image = {
            "filename": composed_seed,
            "url": f"/api/files/{composed_seed}",
            "with_logo": True, "raw_filename": raw_fn,
        }
        pid = _insert_post(ctx["org_id"], image)
        r = _patch_pos(ctx["session"], pid, "top-left")
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        img = data.get("image") or {}
        assert img.get("with_logo") is True, img
        assert img.get("filename", "").startswith("composed-"), img
        assert img.get("raw_filename") == raw_fn
        assert (UPLOAD_DIR / img["filename"]).is_file()
        assert data.get("logo_position") == "top-left"


# ── 2. Legacy fallback — no raw_filename, with_logo=False ───────────────────
class TestLegacyFallback:
    def test_legacy_post_uses_filename_as_base(self, ctx):
        base_fn = _put_disk_image("legacybase", (200, 80, 40, 255))
        image = {
            "filename": base_fn, "url": f"/api/files/{base_fn}",
            "with_logo": False,
            # NB: NO raw_filename key (legacy post)
        }
        pid = _insert_post(ctx["org_id"], image)
        r = _patch_pos(ctx["session"], pid, "bottom-right")
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        img = data.get("image") or {}
        assert img.get("with_logo") is True, img
        assert img.get("filename", "").startswith("composed-"), img
        assert img.get("raw_filename") == base_fn, img
        assert (UPLOAD_DIR / img["filename"]).is_file()


# ── 3. Error paths ──────────────────────────────────────────────────────────
EXPECT_DOUBLE_BRAND = (
    "Can't re-apply the logo — this image is already composited "
    "and the un-composited source is no longer on file."
)
EXPECT_NO_LOGO = (
    "Your organization has no primary logo uploaded. Go to the "
    "Org page and upload one, then try again."
)
EXPECT_LOGO_MISSING = (
    "Your logo file is missing from storage — please re-upload "
    "the primary logo on the Org page."
)
EXPECT_BASE_MISSING = (
    "The original image file is missing from storage. Click "
    "'Regenerate' to produce a fresh image."
)
EXPECT_NO_IMAGE = (
    "This post has no generated image yet — use 'Generate cover "
    "image' first, then position the logo."
)


class TestErrorPaths:
    def test_double_brand_guard(self, ctx):
        already = _put_disk_image("composed-locked")
        image = {
            "filename": already, "url": f"/api/files/{already}",
            "with_logo": True,  # composited but no raw_filename
        }
        pid = _insert_post(ctx["org_id"], image)
        r = _patch_pos(ctx["session"], pid, "top-right")
        assert r.status_code == 422, r.text[:300]
        assert _detail(r).startswith(EXPECT_DOUBLE_BRAND), _detail(r)

    def test_no_image_on_post(self, ctx):
        pid = _insert_post(ctx["org_id"], None)
        r = _patch_pos(ctx["session"], pid, "center")
        assert r.status_code == 422, r.text[:300]
        assert _detail(r) == EXPECT_NO_IMAGE, _detail(r)

    def test_no_primary_logo_on_org(self, ctx):
        org_id = ctx["org_id"]

        async def _snapshot(db):
            org = await db.organizations.find_one({"_id": ObjectId(org_id)})
            return (org.get("brand_assets") or {}).get("primary_logo")

        original = _run(_snapshot)
        assert original, "fixture should have set primary_logo"

        async def _clear(db):
            await db.organizations.update_one(
                {"_id": ObjectId(org_id)},
                {"$unset": {"brand_assets.primary_logo": ""}},
            )

        async def _restore(db):
            await db.organizations.update_one(
                {"_id": ObjectId(org_id)},
                {"$set": {"brand_assets.primary_logo": original}},
            )

        _run(_clear)
        try:
            raw_fn = _put_disk_image("nologobase")
            image = {"filename": raw_fn, "url": f"/api/files/{raw_fn}",
                     "with_logo": False, "raw_filename": raw_fn}
            pid = _insert_post(org_id, image)
            r = _patch_pos(ctx["session"], pid, "bottom-left")
            assert r.status_code == 422, r.text[:300]
            assert _detail(r) == EXPECT_NO_LOGO, _detail(r)
        finally:
            _run(_restore)

    def test_logo_file_missing_on_disk(self, ctx):
        org_id = ctx["org_id"]

        async def _snapshot(db):
            org = await db.organizations.find_one({"_id": ObjectId(org_id)})
            return (org.get("brand_assets") or {}).get("primary_logo")

        original = _run(_snapshot)
        ghost = {**(original or {}),
                 "filename": f"GHOST_{uuid.uuid4().hex[:8]}.png"}

        async def _set_ghost(db):
            await db.organizations.update_one(
                {"_id": ObjectId(org_id)},
                {"$set": {"brand_assets.primary_logo": ghost}},
            )

        async def _restore(db):
            await db.organizations.update_one(
                {"_id": ObjectId(org_id)},
                {"$set": {"brand_assets.primary_logo": original}},
            )

        _run(_set_ghost)
        try:
            raw_fn = _put_disk_image("ghostlogobase")
            image = {"filename": raw_fn, "url": f"/api/files/{raw_fn}",
                     "with_logo": False, "raw_filename": raw_fn}
            pid = _insert_post(org_id, image)
            r = _patch_pos(ctx["session"], pid, "bottom-left")
            assert r.status_code == 422, r.text[:300]
            assert _detail(r) == EXPECT_LOGO_MISSING, _detail(r)
        finally:
            _run(_restore)

    def test_base_file_missing_on_disk(self, ctx):
        ghost = f"GHOSTBASE_{uuid.uuid4().hex[:8]}.png"
        image = {"filename": ghost, "url": f"/api/files/{ghost}",
                 "with_logo": False, "raw_filename": ghost}
        pid = _insert_post(ctx["org_id"], image)
        r = _patch_pos(ctx["session"], pid, "top-left")
        assert r.status_code == 422, r.text[:300]
        assert _detail(r) == EXPECT_BASE_MISSING, _detail(r)

    def test_invalid_position(self, ctx):
        raw_fn = _put_disk_image("invposbase")
        image = {"filename": raw_fn, "url": f"/api/files/{raw_fn}",
                 "with_logo": False, "raw_filename": raw_fn}
        pid = _insert_post(ctx["org_id"], image)
        r = _patch_pos(ctx["session"], pid, "foo")
        assert r.status_code == 422, r.text[:300]
        assert _detail(r) == "Invalid position", _detail(r)


# ── 4. Regression — every anchor re-composites ──────────────────────────────
class TestPositionRegression:
    @pytest.mark.parametrize("position",
                             ["top-left", "top-right", "bottom-left",
                              "bottom-right", "center"])
    def test_each_anchor_recomposites(self, ctx, position):
        raw_fn = _put_disk_image(f"reg-{position}")
        image = {"filename": raw_fn, "url": f"/api/files/{raw_fn}",
                 "with_logo": False, "raw_filename": raw_fn}
        pid = _insert_post(ctx["org_id"], image)
        r = _patch_pos(ctx["session"], pid, position)
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        assert data.get("logo_position") == position
        img = data.get("image") or {}
        assert img.get("with_logo") is True
        assert img.get("filename", "").startswith("composed-")
        assert (UPLOAD_DIR / img["filename"]).is_file()
