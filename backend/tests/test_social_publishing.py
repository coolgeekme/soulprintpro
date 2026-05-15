"""Backend tests for Foundry Social Queue → live publishing via Composio.

Covers:
- GET  /api/orgs/{org}/social/publishable-platforms (auth + shape)
- POST /api/social/posts/{id}/publish (auth, 404, idempotency, status flip)
- social_publishing.publish_post fan-out (no conn => failed, IG no-image, reddit no-subreddit)
- PATCH /api/social/posts/{id} accepts auto_publish + new status values
- promote_due_drafts scheduler hook (auto-publish=true vs false, idempotency)
- composio_integration: FEATURED_TOOLKITS contains 23 entries incl. social slugs
- DIVISION_TOOLKIT_DEFAULTS covers social toolkits for marketing/sales/content
"""
import os
import sys
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from bson import ObjectId

sys.path.insert(0, "/app/backend")


def _env(path, key):
    try:
        with open(path) as f:
            for line in f:
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        return None


BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL")
            or _env("/app/frontend/.env", "REACT_APP_BACKEND_URL") or "").rstrip("/")
MONGO_URL = os.environ.get("MONGO_URL") or _env("/app/backend/.env", "MONGO_URL")
DB_NAME = os.environ.get("DB_NAME") or _env("/app/backend/.env", "DB_NAME")
assert BASE_URL and MONGO_URL and DB_NAME, "env not configured"

SUPERADMIN_EMAIL = "reggie@coolgeek.me"
SUPERADMIN_PASSWORD = "superadminpass123"
REGGIE_ORG_ID = "69f6a9f568b5974dfcc2fcf8"
NON_MEMBER_EMAIL = "autopost-test-1777661963@example.com"
NON_MEMBER_PASSWORD = "testtest123"

SUPPORTED = {"linkedin", "facebook", "instagram", "reddit", "tiktok"}


def _run(async_fn_taking_db):
    """async_fn_taking_db: an `async def f(db)` taking the motor DB."""
    from motor.motor_asyncio import AsyncIOMotorClient
    async def runner():
        client = AsyncIOMotorClient(MONGO_URL)
        try:
            return await async_fn_taking_db(client[DB_NAME])
        finally:
            client.close()
    return asyncio.run(runner())


@pytest.fixture
def db():
    # Sentinel — tests should pass `_db` arg into closures via _run.
    return None


@pytest.fixture(scope="module")
def s():
    return requests.Session()


@pytest.fixture(scope="module")
def auth(s):
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": SUPERADMIN_EMAIL, "password": SUPERADMIN_PASSWORD})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    data = r.json()
    s.headers.update({"Authorization": f"Bearer {data['access_token']}"})
    return {"user_id": data["id"], "token": data["access_token"]}


@pytest.fixture(scope="module")
def non_member_session():
    s2 = requests.Session()
    r = s2.post(f"{BASE_URL}/api/auth/login",
                json={"email": NON_MEMBER_EMAIL, "password": NON_MEMBER_PASSWORD})
    assert r.status_code == 200
    data = r.json()
    s2.headers.update({"Authorization": f"Bearer {data['access_token']}"})
    return s2, data["id"]


@pytest.fixture
def cleanup_post_ids():
    ids = []
    yield ids
    from motor.motor_asyncio import AsyncIOMotorClient
    async def _clean(db):
        client = AsyncIOMotorClient(MONGO_URL)
        try:
            db_ = client[DB_NAME]
            for oid in ids:
                try:
                    await db_.social_posts.delete_one({"_id": ObjectId(oid)})
                except Exception:
                    pass
        finally:
            client.close()
    _run(_clean)


# ── composio_integration constants ──────────────────────────────────────────
class TestComposioConstants:
    def test_featured_toolkits_includes_social_slugs(self):
        from composio_integration import FEATURED_TOOLKITS
        slugs = {t["slug"] for t in FEATURED_TOOLKITS}
        assert len(FEATURED_TOOLKITS) >= 23, f"expected >=23, got {len(FEATURED_TOOLKITS)}"
        for needed in ("linkedin", "facebook", "instagram", "reddit"):
            assert needed in slugs, f"{needed} missing from FEATURED_TOOLKITS"

    def test_division_defaults_cover_social(self):
        from composio_integration import DIVISION_TOOLKIT_DEFAULTS
        keys = {k.lower() for k in DIVISION_TOOLKIT_DEFAULTS.keys()}
        # At least one of these divisions should default-include each social slug
        flat = set()
        for v in DIVISION_TOOLKIT_DEFAULTS.values():
            flat.update(v)
        # Marketing/sales/content auto-enable social toolkits
        for needed in ("linkedin", "facebook", "instagram", "reddit"):
            assert needed in flat, f"{needed} not in any DIVISION_TOOLKIT_DEFAULTS list"

    def test_supported_platforms(self):
        from social_publishing import supported_platforms, is_platform_supported
        assert set(supported_platforms()) == SUPPORTED
        assert is_platform_supported("linkedin")
        assert not is_platform_supported("youtube")


# ── publishable-platforms endpoint ──────────────────────────────────────────
class TestPublishablePlatformsEndpoint:
    def test_returns_supported_and_connected_shape(self, s, auth):
        r = s.get(f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/social/publishable-platforms")
        assert r.status_code == 200, r.text
        data = r.json()
        assert "supported" in data and "connected" in data
        assert set(data["supported"]) == SUPPORTED
        # connected dict should at minimum have keys for all supported
        assert set(data["connected"].keys()) >= SUPPORTED
        # All bool
        for k, v in data["connected"].items():
            assert isinstance(v, bool)

    def test_non_member_blocked(self, non_member_session):
        sess, _ = non_member_session
        r = sess.get(f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/social/publishable-platforms")
        assert r.status_code in (403, 404), f"expected 403/404, got {r.status_code}"


# ── POST /publish endpoint ──────────────────────────────────────────────────
class TestPublishEndpoint:
    def _insert_draft(self, db, auth, *, platforms, image=None, text="hello world",
                      extras=None) -> str:
        async def _ins(db):
            doc = {
                "org_id": REGGIE_ORG_ID,
                "team_id": "",
                "status": "draft",
                "platforms": platforms,
                "text": text,
                "hashtags": [],
                "image": image,
                "created_at": datetime.now(timezone.utc),
                "created_by": auth["user_id"],
            }
            if extras:
                doc.update(extras)
            res = await db.social_posts.insert_one(doc)
            return str(res.inserted_id)
        return _run(_ins)

    def test_404_for_unknown_post(self, s, auth):
        r = s.post(f"{BASE_URL}/api/social/posts/000000000000000000000000/publish")
        assert r.status_code == 404
        # malformed id also 404
        r = s.post(f"{BASE_URL}/api/social/posts/not-an-id/publish")
        assert r.status_code == 404

    def test_publish_flips_status_to_publishing(self, s, auth, db, cleanup_post_ids):
        pid = self._insert_draft(db, auth, platforms=["linkedin"])
        cleanup_post_ids.append(pid)
        r = s.post(f"{BASE_URL}/api/social/posts/{pid}/publish")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "publishing"
        assert data["post_id"] == pid

    def test_publish_idempotent_in_flight(self, s, auth, db, cleanup_post_ids):
        # Insert a post already in "publishing" state
        async def _ins(db):
            doc = {
                "org_id": REGGIE_ORG_ID, "status": "publishing",
                "platforms": ["linkedin"], "text": "x",
                "created_at": datetime.now(timezone.utc),
                "created_by": auth["user_id"],
            }
            res = await db.social_posts.insert_one(doc)
            return str(res.inserted_id)
        pid = _run(_ins)
        cleanup_post_ids.append(pid)
        r = s.post(f"{BASE_URL}/api/social/posts/{pid}/publish")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "publishing"
        assert "Already in flight" in (data.get("message") or "")

    def test_publish_no_connections_yields_failed(self, s, auth, db, cleanup_post_ids):
        # Ensure no ACTIVE connections (Reggie has none in preview env)
        pid = self._insert_draft(db, auth, platforms=["linkedin", "facebook", "reddit"],
                                  extras={"reddit_subreddit": "test"})
        cleanup_post_ids.append(pid)
        r = s.post(f"{BASE_URL}/api/social/posts/{pid}/publish")
        assert r.status_code == 200
        # Wait for background task to write final state
        import time
        final = None
        for _ in range(30):
            time.sleep(0.5)
            async def _fetch(db):
                return await db.social_posts.find_one({"_id": ObjectId(pid)})
            doc = _run(_fetch)
            if doc.get("status") in ("failed", "partial", "published"):
                final = doc
                break
        assert final is not None, "publish_post never finalized"
        assert final["status"] == "failed", f"expected failed, got {final['status']}"
        assert final.get("failed_count", 0) >= 1
        assert final.get("published_count", 0) == 0
        results = final.get("publish_results") or {}
        for plat in ("linkedin", "facebook", "reddit"):
            assert plat in results
            assert results[plat]["ok"] is False
            assert "No connected account" in results[plat]["error"]


# ── social_publishing.publish_post unit tests ───────────────────────────────
class TestPublishPostUnit:
    """Direct calls into the module to verify builder error paths."""

    def test_no_connections_failed(self, auth, db, cleanup_post_ids):
        async def _go(db):
            doc = {
                "_id": ObjectId(),
                "org_id": REGGIE_ORG_ID,
                "platforms": ["linkedin", "facebook"],
                "text": "hi",
                "created_by": auth["user_id"],
            }
            await db.social_posts.insert_one(doc)
            cleanup_post_ids.append(str(doc["_id"]))
            from social_publishing import publish_post
            res = await publish_post(db, post=doc, actor_user_id=auth["user_id"])
            assert res["status"] == "failed"
            assert res["failed_count"] == 2
            assert res["published_count"] == 0
            for plat in ("linkedin", "facebook"):
                assert res["results"][plat]["ok"] is False
                assert "No connected account" in res["results"][plat]["error"]
        _run(_go)

    def test_instagram_no_image_error(self, db, auth, cleanup_post_ids):
        async def _go(db):
            # Insert a fake ACTIVE instagram connection so builder runs
            import composio_integration as ci
            cu = ci.composio_user_id(REGGIE_ORG_ID, auth["user_id"])
            conn_id = f"TEST_conn_{uuid.uuid4().hex}"
            await db.composio_connections.insert_one({
                "composio_user_id": cu,
                "toolkit": "instagram",
                "status": "ACTIVE",
                "connection_id": conn_id,
                "org_id": REGGIE_ORG_ID,
                "user_id": auth["user_id"],
            })
            try:
                doc = {
                    "_id": ObjectId(),
                    "org_id": REGGIE_ORG_ID,
                    "platforms": ["instagram"],
                    "text": "no image here",
                    "image": None,
                    "created_by": auth["user_id"],
                }
                await db.social_posts.insert_one(doc)
                cleanup_post_ids.append(str(doc["_id"]))
                from social_publishing import publish_post
                res = await publish_post(db, post=doc, actor_user_id=auth["user_id"])
                ig = res["results"]["instagram"]
                assert ig["ok"] is False
                assert "image" in ig["error"].lower()
            finally:
                await db.composio_connections.delete_one({"connection_id": conn_id})
        _run(_go)

    def test_reddit_no_subreddit_error(self, db, auth, cleanup_post_ids):
        async def _go(db):
            import composio_integration as ci
            cu = ci.composio_user_id(REGGIE_ORG_ID, auth["user_id"])
            conn_id = f"TEST_conn_{uuid.uuid4().hex}"
            await db.composio_connections.insert_one({
                "composio_user_id": cu,
                "toolkit": "reddit",
                "status": "ACTIVE",
                "connection_id": conn_id,
                "org_id": REGGIE_ORG_ID,
                "user_id": auth["user_id"],
            })
            try:
                doc = {
                    "_id": ObjectId(),
                    "org_id": REGGIE_ORG_ID,
                    "platforms": ["reddit"],
                    "text": "no subreddit specified",
                    "created_by": auth["user_id"],
                    # no reddit_subreddit, no variants.reddit.subreddit
                }
                await db.social_posts.insert_one(doc)
                cleanup_post_ids.append(str(doc["_id"]))
                from social_publishing import publish_post
                res = await publish_post(db, post=doc, actor_user_id=auth["user_id"])
                rd = res["results"]["reddit"]
                assert rd["ok"] is False
                assert "subreddit" in rd["error"].lower()
            finally:
                await db.composio_connections.delete_one({"connection_id": conn_id})
        _run(_go)

    def test_ig_failure_doesnt_block_other_platforms(self, db, auth, cleanup_post_ids):
        """IG raises 'requires image' but LinkedIn (no conn) returns its own
        error — they are independent failures, neither crashes the other."""
        async def _go(db):
            import composio_integration as ci
            cu = ci.composio_user_id(REGGIE_ORG_ID, auth["user_id"])
            conn_id = f"TEST_conn_{uuid.uuid4().hex}"
            await db.composio_connections.insert_one({
                "composio_user_id": cu, "toolkit": "instagram",
                "status": "ACTIVE", "connection_id": conn_id,
                "org_id": REGGIE_ORG_ID, "user_id": auth["user_id"],
            })
            try:
                doc = {
                    "_id": ObjectId(), "org_id": REGGIE_ORG_ID,
                    "platforms": ["instagram", "linkedin"],
                    "text": "mix", "image": None,
                    "created_by": auth["user_id"],
                }
                await db.social_posts.insert_one(doc)
                cleanup_post_ids.append(str(doc["_id"]))
                from social_publishing import publish_post
                res = await publish_post(db, post=doc, actor_user_id=auth["user_id"])
                assert "instagram" in res["results"] and "linkedin" in res["results"]
                assert res["results"]["instagram"]["ok"] is False
                assert "image" in res["results"]["instagram"]["error"].lower()
                assert res["results"]["linkedin"]["ok"] is False
                assert "No connected account" in res["results"]["linkedin"]["error"]
                assert res["status"] == "failed"
                assert res["failed_count"] == 2
            finally:
                await db.composio_connections.delete_one({"connection_id": conn_id})
        _run(_go)


# ── PATCH endpoint accepts auto_publish + new status values ────────────────
class TestPatchSocialPost:
    def test_auto_publish_roundtrip(self, s, auth, db, cleanup_post_ids):
        async def _ins(db):
            doc = {
                "org_id": REGGIE_ORG_ID, "status": "draft",
                "platforms": ["linkedin"], "text": "rt",
                "created_at": datetime.now(timezone.utc),
                "created_by": auth["user_id"],
            }
            r = await db.social_posts.insert_one(doc)
            return str(r.inserted_id)
        pid = _run(_ins)
        cleanup_post_ids.append(pid)
        # default should be true per serializer
        r = s.patch(f"{BASE_URL}/api/social/posts/{pid}", json={"auto_publish": False})
        assert r.status_code == 200, r.text
        assert r.json()["auto_publish"] is False
        r = s.patch(f"{BASE_URL}/api/social/posts/{pid}", json={"auto_publish": True})
        assert r.status_code == 200
        assert r.json()["auto_publish"] is True

    @pytest.mark.parametrize("status", ["publishing", "partial", "failed"])
    def test_new_status_values_accepted(self, s, auth, db, cleanup_post_ids, status):
        async def _ins(db):
            doc = {
                "org_id": REGGIE_ORG_ID, "status": "draft",
                "platforms": ["linkedin"], "text": "st",
                "created_at": datetime.now(timezone.utc),
                "created_by": auth["user_id"],
            }
            r = await db.social_posts.insert_one(doc)
            return str(r.inserted_id)
        pid = _run(_ins)
        cleanup_post_ids.append(pid)
        r = s.patch(f"{BASE_URL}/api/social/posts/{pid}", json={"status": status})
        assert r.status_code == 200, f"{status}: {r.status_code} {r.text}"
        assert r.json()["status"] == status


# ── promote_due_drafts scheduler hook ───────────────────────────────────────
class TestPromoteDueDrafts:
    def test_auto_publish_true_marks_ready_and_fires(self, db, auth, cleanup_post_ids):
        async def _go(db):
            past = datetime.now(timezone.utc) - timedelta(minutes=5)
            doc = {
                "org_id": REGGIE_ORG_ID, "status": "queued",
                "platforms": ["linkedin"],
                "scheduled_for": past, "auto_publish": True,
                "text": "due", "created_by": auth["user_id"],
                "created_at": past,
            }
            r = await db.social_posts.insert_one(doc)
            pid = str(r.inserted_id)
            cleanup_post_ids.append(pid)
            from social_posts import promote_due_drafts
            n = await promote_due_drafts(db=db)
            assert n >= 1, f"expected >=1 promoted, got {n}"
            fresh = await db.social_posts.find_one({"_id": r.inserted_id})
            assert "ready_at" in fresh
            # status should transition to publishing (then failed once bg done)
            # we just verify ready_at exists and the auto-publish path was taken
            # Wait briefly for bg task
            import asyncio as _a
            await _a.sleep(1.5)
            again = await db.social_posts.find_one({"_id": r.inserted_id})
            assert again["status"] in ("publishing", "failed", "partial", "published")
            # Idempotency: re-running returns 0 because ready_at filter excludes
            n2 = await promote_due_drafts(db=db)
            # n2 counts other posts too; just ensure THIS post isn't re-picked
            still = await db.social_posts.find_one({"_id": r.inserted_id})
            # ready_at was set on first pass, never overwritten
            assert still["ready_at"] == fresh["ready_at"]
        _run(_go)

    def test_auto_publish_false_marks_ready_only(self, db, auth, cleanup_post_ids):
        async def _go(db):
            past = datetime.now(timezone.utc) - timedelta(minutes=5)
            doc = {
                "org_id": REGGIE_ORG_ID, "status": "queued",
                "platforms": ["linkedin"],
                "scheduled_for": past, "auto_publish": False,
                "text": "due-manual", "created_by": auth["user_id"],
                "created_at": past,
            }
            r = await db.social_posts.insert_one(doc)
            pid = str(r.inserted_id)
            cleanup_post_ids.append(pid)
            from social_posts import promote_due_drafts
            await promote_due_drafts(db=db)
            fresh = await db.social_posts.find_one({"_id": r.inserted_id})
            assert "ready_at" in fresh
            # status NOT flipped to publishing — still queued
            assert fresh["status"] == "queued"
            # No publish_started_at set
            assert fresh.get("publish_started_at") is None
        _run(_go)
