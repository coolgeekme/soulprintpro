"""Backend tests for Social Queue MVP (Generate & Queue path C).

Covers:
  - POST /api/orgs/{org_id}/social/draft    (LLM-backed; slow: 5-15s)
  - GET  /api/orgs/{org_id}/social/posts    (+ ?status filter, CSV)
  - PATCH /api/social/posts/{post_id}       (scheduled_for auto-flips draft→queued)
  - DELETE /api/social/posts/{post_id}
  - POST /api/conversations/{id}/messages/{msg_id}/queue-social
"""
from __future__ import annotations

import os
import time
import uuid

import pytest
import requests

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


BASE_URL = _load_backend_url()
assert BASE_URL, "REACT_APP_BACKEND_URL missing"

API = f"{BASE_URL}/api"


# ── shared fixtures ────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def user_ctx():
    """Register a fresh user, return {session, user, org_id}."""
    s = requests.Session()
    s.headers["Content-Type"] = "application/json"
    email = f"TEST_social_{uuid.uuid4().hex[:10]}@example.com"
    pwd = "StrongPass!234"
    r = s.post(f"{API}/auth/register", json={
        "email": email, "password": pwd, "name": "Social Tester",
    }, timeout=30)
    assert r.status_code in (200, 201), f"register failed: {r.status_code} {r.text}"
    data = r.json()
    token = data.get("access_token") or data.get("token")
    assert token, f"no access_token in register response: {data}"
    s.headers["Authorization"] = f"Bearer {token}"

    # Fetch orgs — if none, create a bare one (orgs aren't auto-created on register)
    r = s.get(f"{API}/orgs", timeout=20)
    assert r.status_code == 200, r.text
    orgs = r.json()
    if not orgs:
        # Pass no website to keep creation fast / avoid scrape latency
        c = s.post(f"{API}/orgs", json={"name": f"TEST_Org_{uuid.uuid4().hex[:6]}"},
                   timeout=60)
        assert c.status_code in (200, 201), f"org create failed: {c.status_code} {c.text[:300]}"
        org_id = c.json().get("id") or c.json().get("_id")
        assert org_id, f"bad org create resp: {c.json()}"
    else:
        org_id = orgs[0]["id"]
    return {"session": s, "user": data.get("user") or {}, "email": email, "org_id": org_id}


@pytest.fixture(scope="module")
def drafted_posts(user_ctx):
    """Generate 2 drafts once, reuse across tests to save LLM tokens/time."""
    s = user_ctx["session"]
    org_id = user_ctx["org_id"]
    body = {"prompt": "Announce our Summer Cold Brew launch with a friendly hook.",
            "platforms": ["twitter", "linkedin"], "count": 2}
    t0 = time.time()
    r = s.post(f"{API}/orgs/{org_id}/social/draft", json=body, timeout=120)
    elapsed = time.time() - t0
    assert r.status_code == 200, f"draft failed in {elapsed:.1f}s: {r.status_code} {r.text[:500]}"
    posts = r.json().get("posts", [])
    assert len(posts) >= 1, f"expected ≥1 drafted post, got {posts}"
    return posts


# ── 1. Draft endpoint ──────────────────────────────────────────────────────
class TestSocialDraft:
    def test_draft_success_returns_posts(self, user_ctx, drafted_posts):
        assert drafted_posts, "expected posts"
        p = drafted_posts[0]
        for field in ("id", "org_id", "agent_id", "agent_name", "status",
                      "platforms", "text", "hashtags"):
            assert field in p, f"missing {field} in {p}"
        assert p["status"] == "draft"
        assert p["text"].strip(), "drafted text is empty"
        assert p["platforms"], "drafted post has no platforms"
        assert p["org_id"] == user_ctx["org_id"]

    def test_draft_empty_prompt_422(self, user_ctx):
        r = user_ctx["session"].post(
            f"{API}/orgs/{user_ctx['org_id']}/social/draft",
            json={"prompt": "   ", "platforms": ["twitter"], "count": 1},
            timeout=30,
        )
        assert r.status_code == 422, f"expected 422 got {r.status_code}: {r.text}"

    def test_draft_empty_platforms_422(self, user_ctx):
        r = user_ctx["session"].post(
            f"{API}/orgs/{user_ctx['org_id']}/social/draft",
            json={"prompt": "something", "platforms": [], "count": 1},
            timeout=30,
        )
        assert r.status_code == 422, f"expected 422 got {r.status_code}: {r.text}"

    def test_draft_all_unsupported_platforms_422(self, user_ctx):
        r = user_ctx["session"].post(
            f"{API}/orgs/{user_ctx['org_id']}/social/draft",
            json={"prompt": "hi", "platforms": ["myspace", "orkut"], "count": 1},
            timeout=30,
        )
        assert r.status_code == 422


# ── 2. List endpoint + filters ─────────────────────────────────────────────
class TestSocialList:
    def test_list_returns_array(self, user_ctx, drafted_posts):
        r = user_ctx["session"].get(f"{API}/orgs/{user_ctx['org_id']}/social/posts", timeout=20)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        ids = {p["id"] for p in data}
        for p in drafted_posts:
            assert p["id"] in ids, f"drafted post {p['id']} missing from list"

    def test_list_filter_status_draft(self, user_ctx, drafted_posts):
        r = user_ctx["session"].get(
            f"{API}/orgs/{user_ctx['org_id']}/social/posts", params={"status": "draft"},
            timeout=20,
        )
        assert r.status_code == 200
        data = r.json()
        assert all(p["status"] == "draft" for p in data), f"non-draft leaked: {data}"

    def test_list_filter_status_csv(self, user_ctx, drafted_posts):
        r = user_ctx["session"].get(
            f"{API}/orgs/{user_ctx['org_id']}/social/posts",
            params={"status": "draft,queued"}, timeout=20,
        )
        assert r.status_code == 200
        for p in r.json():
            assert p["status"] in ("draft", "queued")


# ── 3. Patch endpoint ──────────────────────────────────────────────────────
class TestSocialPatch:
    def test_patch_scheduled_for_auto_flips_to_queued(self, user_ctx, drafted_posts):
        pid = drafted_posts[0]["id"]
        future = "2099-01-01T10:00:00Z"
        r = user_ctx["session"].patch(f"{API}/social/posts/{pid}",
                                      json={"scheduled_for": future}, timeout=20)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "queued", f"expected queued, got {data['status']}"
        assert data["scheduled_for"], "scheduled_for not persisted"

        # Verify it's visible under ?status=queued
        r2 = user_ctx["session"].get(f"{API}/orgs/{user_ctx['org_id']}/social/posts",
                                     params={"status": "queued"}, timeout=20)
        assert any(p["id"] == pid for p in r2.json())

    def test_patch_invalid_iso_422(self, user_ctx, drafted_posts):
        pid = drafted_posts[0]["id"]
        r = user_ctx["session"].patch(f"{API}/social/posts/{pid}",
                                      json={"scheduled_for": "not-a-date"}, timeout=20)
        assert r.status_code == 422

    def test_patch_status_published_sets_published_at(self, user_ctx, drafted_posts):
        if len(drafted_posts) < 2:
            pytest.skip("need ≥2 drafts")
        pid = drafted_posts[1]["id"]
        r = user_ctx["session"].patch(f"{API}/social/posts/{pid}",
                                      json={"status": "published"}, timeout=20)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "published"
        assert data["published_at"], "published_at not set"

    def test_patch_text_and_platforms(self, user_ctx, drafted_posts):
        pid = drafted_posts[0]["id"]
        r = user_ctx["session"].patch(f"{API}/social/posts/{pid}",
                                      json={"text": "TEST_updated text",
                                            "platforms": ["bluesky", "threads"]},
                                      timeout=20)
        assert r.status_code == 200
        data = r.json()
        assert data["text"] == "TEST_updated text"
        assert set(data["platforms"]) == {"bluesky", "threads"}


# ── 4. Delete endpoint ─────────────────────────────────────────────────────
class TestSocialDelete:
    def test_delete_removes_post(self, user_ctx):
        s = user_ctx["session"]
        # Create a quick manual post by drafting 1 (cheap; reuse LLM once more)
        r = s.post(f"{API}/orgs/{user_ctx['org_id']}/social/draft",
                   json={"prompt": "TEST_delete me please",
                         "platforms": ["twitter"], "count": 1}, timeout=120)
        assert r.status_code == 200, r.text
        pid = r.json()["posts"][0]["id"]

        # Delete
        d = s.delete(f"{API}/social/posts/{pid}", timeout=20)
        assert d.status_code == 200
        assert d.json().get("ok") is True

        # Confirm absence
        r2 = s.get(f"{API}/orgs/{user_ctx['org_id']}/social/posts", timeout=20)
        assert all(p["id"] != pid for p in r2.json())

        # Second delete → 404
        d2 = s.delete(f"{API}/social/posts/{pid}", timeout=20)
        assert d2.status_code == 404


# ── 5. Queue-from-chat endpoint ────────────────────────────────────────────
class TestQueueSocialFromChat:
    @pytest.fixture(scope="class")
    def chat_ctx(self, user_ctx):
        """Create a team + direct conversation + exchange one turn so we have
        both a user and an agent message to test the sender-type guard."""
        s = user_ctx["session"]
        org_id = user_ctx["org_id"]

        # List teams — registration usually creates a default team
        r = s.get(f"{API}/orgs/{org_id}/teams", timeout=20)
        if r.status_code != 200 or not r.json():
            pytest.skip(f"no teams; cannot exercise chat flow (status={r.status_code})")
        team_id = r.json()[0]["id"]

        # Start / open the org-wide direct conversation with an agent.
        agents = s.get(f"{API}/teams/{team_id}/agents", timeout=20)
        if agents.status_code != 200 or not agents.json():
            pytest.skip(f"team agents unavailable: {agents.status_code} {agents.text[:200]}")
        agent_id = agents.json()[0]["id"]

        r = s.post(f"{API}/teams/{team_id}/conversations",
                   json={"agent_ids": [agent_id], "title": "TEST_social"},
                   timeout=30)
        if r.status_code not in (200, 201):
            pytest.skip(f"cannot create conv: {r.status_code} {r.text[:200]}")
        conv = r.json()
        conv_id = conv.get("id") or conv.get("conversation", {}).get("id")
        if not conv_id:
            pytest.skip(f"unexpected conv shape: {conv}")

        # Send user message; expect agent reply (LLM 5-15s)
        r = s.post(f"{API}/conversations/{conv_id}/messages",
                   json={"text": "Tweet about our new product launch in one sentence."},
                   timeout=120)
        if r.status_code not in (200, 201):
            pytest.skip(f"send-message failed: {r.status_code} {r.text[:200]}")

        # Poll for agent reply
        agent_msg_id, user_msg_id = "", ""
        for _ in range(20):
            mr = s.get(f"{API}/conversations/{conv_id}/messages", timeout=20)
            if mr.status_code == 200:
                for m in mr.json():
                    if m.get("sender_type") == "agent" and not agent_msg_id:
                        agent_msg_id = m["id"]
                    if m.get("sender_type") == "user" and not user_msg_id:
                        user_msg_id = m["id"]
                if agent_msg_id and user_msg_id:
                    break
            time.sleep(2)
        if not agent_msg_id:
            pytest.skip("agent reply never arrived")
        return {"conv_id": conv_id, "agent_msg_id": agent_msg_id,
                "user_msg_id": user_msg_id}

    def test_queue_agent_reply_ok(self, user_ctx, chat_ctx):
        s = user_ctx["session"]
        r = s.post(f"{API}/conversations/{chat_ctx['conv_id']}/messages/"
                   f"{chat_ctx['agent_msg_id']}/queue-social", timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "draft"
        assert data["source_message_id"] == chat_ctx["agent_msg_id"]
        assert "twitter" in data["platforms"]

    def test_queue_user_message_rejected(self, user_ctx, chat_ctx):
        if not chat_ctx.get("user_msg_id"):
            pytest.skip("no user msg captured")
        s = user_ctx["session"]
        r = s.post(f"{API}/conversations/{chat_ctx['conv_id']}/messages/"
                   f"{chat_ctx['user_msg_id']}/queue-social", timeout=20)
        assert r.status_code == 400, f"expected 400 got {r.status_code}: {r.text}"
