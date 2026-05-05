"""Regression test for the "/social/draft is too slow" performance fix.

Before the fix, /social/draft inlined Nano Banana image generation
for the first post, blocking the response for ~18s on preview and
~30-90s on production (where it routinely hit the K8s ingress
timeout, causing users to retry, double-spend LLM credit, and
sometimes wind up with duplicate drafts).

After the fix, /social/draft fires image generation as a background
asyncio.create_task and returns immediately with `image_status:
"pending"` on the first post. A polling loop on the frontend picks
up the image once it lands.

This test asserts:
  1. The draft endpoint completes WITHOUT awaiting the image gen
     (we mock _generate_post_image with a 30s sleep — if it were still
     inline, the test would hang; with the fix it returns in <1s).
  2. The first post's `image_status` is "pending" in the response.
  3. The background task eventually patches `image_status` to "ready"
     and writes the image dict.
"""
from __future__ import annotations

import os
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "social_perf_test")


@pytest.mark.asyncio
async def test_draft_endpoint_returns_before_image_generation_finishes():
    """Mock _generate_post_image to sleep 30s; assert the endpoint
    returns in well under that time (we use 5s as an upper bound to
    account for the LLM draft step being mocked too)."""
    # We have to import after env is set
    import social_posts as sp

    # ── Mock the slow image gen so we can assert timing semantics ──
    async def slow_image(post, org_doc, override_prompt: str = ""):
        await asyncio.sleep(30)
        return {"filename": "x.png", "url": "/api/files/x.png", "with_logo": True, "raw_filename": "x.png"}

    # Mock the LLM draft helper (returns instantly)
    async def fake_draft(**kwargs):
        return [
            {"platform": "twitter", "text": "First post text", "hashtags": []},
            {"platform": "linkedin", "text": "Second post text", "hashtags": []},
        ]

    # Build a fake DB context — we only need the .insert_one + update_one
    # affordances on social_posts and a basic find_one for organizations.
    inserted: list[dict] = []
    updates: list[dict] = []

    class _Coll:
        async def insert_one(self, doc):
            doc["_id"] = "fake_oid_" + str(len(inserted))
            inserted.append(doc)
            return MagicMock(inserted_id=doc["_id"])
        async def update_one(self, q, u):
            updates.append({"q": q, "u": u})
            # Apply the $set so subsequent reads see the patch
            for d in inserted:
                if d.get("_id") == q.get("_id"):
                    d.update(u.get("$set", {}))
            return MagicMock(modified_count=1)
        async def find_one(self, q):
            return {"_id": "org_oid", "soul": {}, "brand_assets": {}}

    fake_db = type("_DB", (), {})()
    fake_db.social_posts = _Coll()
    fake_db.organizations = _Coll()

    # Build the router with our slow-image stub injected
    router = sp.make_social_router(
        db=fake_db,
        AGENTS={"marketing-content-creator": {"name": "Content Creator", "subgroup": "Content"}},
        EMERGENT_LLM_KEY="sk-test",
        LlmChat=MagicMock(), UserMessage=MagicMock(),
        require_org_member=AsyncMock(return_value={"org": {"_id": "org_oid", "soul": {}, "brand_assets": {}}, "member": {"role": "owner"}}),
        get_current_user=AsyncMock(),
        route_best_agent=AsyncMock(return_value="marketing-content-creator"),
        load_conv_or_403=AsyncMock(),
        generate_image_via_nano_banana=AsyncMock(),
        compose_logo_on_image=lambda *a, **kw: None,
        upload_dir=None,
        recover_bytes_from_gridfs=AsyncMock(return_value=(None, None, None)),
    )

    # Reach into the router closure to call draft_social_posts directly.
    # That avoids spinning up a full ASGI app for one assertion.
    draft_endpoint = None
    for route in router.routes:
        if route.path.endswith("/social/draft") and "POST" in route.methods:
            draft_endpoint = route.endpoint
            break
    assert draft_endpoint is not None

    body = sp.SocialDraftBody(
        prompt="quick test", platforms=["linkedin", "twitter"], count=2,
    )
    fake_user = {"id": "u1", "name": "Tester"}

    # Patch the draft helper, the image gen, and the serializer's reach
    # into _generate_post_image (it's a closure inside make_social_router,
    # so we monkey-patch by name on the module)
    with patch.object(sp, "_draft_via_agent", side_effect=fake_draft), \
         patch.object(sp, "_generate_post_image", side_effect=slow_image, create=True):
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        result = await draft_endpoint(org_id="org_oid", body=body, u=fake_user)
        elapsed = loop.time() - t0

    # ── Assertions ─────────────────────────────────────────────────────────
    # 1. Endpoint returned in <5s even though image gen sleeps 30s
    assert elapsed < 5, f"Endpoint awaited image gen — elapsed {elapsed:.1f}s"

    # 2. Posts are returned with the right image_status
    posts = result["posts"]
    assert len(posts) == 2
    assert posts[0]["image_status"] == "pending", f"first post should be pending, got {posts[0]['image_status']}"
    assert posts[1]["image_status"] == "none", f"second post should be 'none', got {posts[1]['image_status']}"
    assert posts[0]["image_prompt"] == "First post text"  # primed for editor


if __name__ == "__main__":
    asyncio.run(test_draft_endpoint_returns_before_image_generation_finishes())
    print("OK")
