"""Backend tests for Autonomous Scheduled Social Posting (autopost) flow.

Covers:
  - POST /api/social/posts/{post_id}/schedule-autopost (success + validation)
  - DELETE /api/social/posts/{post_id}/schedule-autopost
  - POST /api/orgs/{org_id}/social/sessions (upsert + validation + no leak)
  - GET  /api/orgs/{org_id}/social/sessions
  - DELETE /api/orgs/{org_id}/social/sessions/{platform}
  - End-to-end fire (~90s schedule) — verify autopost_status maps to
    'failed' for fake cookies (login wall) and post is NOT marked published.

Uses a freshly-registered user + org per module run.
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timedelta, timezone

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


# ── fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def ctx():
    s = requests.Session()
    s.headers["Content-Type"] = "application/json"
    email = f"TEST_autopost_{uuid.uuid4().hex[:10]}@example.com"
    pwd = "StrongPass!234"
    r = s.post(f"{API}/auth/register", json={
        "email": email, "password": pwd, "name": "Autopost Tester",
    }, timeout=30)
    assert r.status_code in (200, 201), f"register failed: {r.status_code} {r.text}"
    data = r.json()
    token = data.get("access_token") or data.get("token")
    assert token, f"no access_token in register: {data}"
    s.headers["Authorization"] = f"Bearer {token}"

    # Re-login per the agent_to_agent_context_note quirk: ensures JWT sub
    # matches org_member.user_id for the org we're about to create.
    rl = s.post(f"{API}/auth/login", json={"email": email, "password": pwd}, timeout=30)
    if rl.status_code == 200:
        tk2 = rl.json().get("access_token") or rl.json().get("token")
        if tk2:
            s.headers["Authorization"] = f"Bearer {tk2}"

    r = s.get(f"{API}/orgs", timeout=20)
    assert r.status_code == 200, r.text
    orgs = r.json()
    if orgs:
        org_id = orgs[0]["id"]
    else:
        c = s.post(f"{API}/orgs",
                   json={"name": f"TEST_AutopostOrg_{uuid.uuid4().hex[:6]}"},
                   timeout=60)
        assert c.status_code in (200, 201), f"org create: {c.status_code} {c.text[:300]}"
        org_id = c.json().get("id") or c.json().get("_id")
    return {"s": s, "email": email, "org_id": org_id}


@pytest.fixture(scope="module")
def draft_post(ctx):
    """Create one minimal draft post directly (bypassing LLM) via the
    standard draft endpoint, then return its id."""
    s, org_id = ctx["s"], ctx["org_id"]
    # Try the LLM-backed draft endpoint with a tiny request (count=1).
    body = {"prompt": "TEST_ autopost smoke draft.",
            "platforms": ["twitter"], "count": 1}
    r = s.post(f"{API}/orgs/{org_id}/social/draft", json=body, timeout=120)
    assert r.status_code == 200, f"draft create: {r.status_code} {r.text[:400]}"
    j = r.json()
    drafts = j["posts"] if isinstance(j, dict) and "posts" in j else j
    assert isinstance(drafts, list) and drafts, f"no drafts: {j}"
    return drafts[0]


# ── Schedule-autopost endpoint validation ───────────────────────────────────
class TestScheduleAutopostValidation:
    def test_unsupported_platform_instagram_returns_422(self, ctx, draft_post):
        s = ctx["s"]
        pid = draft_post["id"]
        when = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        r = s.post(f"{API}/social/posts/{pid}/schedule-autopost",
                   json={"scheduled_for": when, "platform": "instagram"}, timeout=20)
        assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"

    def test_unsupported_platform_tiktok_returns_422(self, ctx, draft_post):
        s = ctx["s"]
        pid = draft_post["id"]
        when = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        r = s.post(f"{API}/social/posts/{pid}/schedule-autopost",
                   json={"scheduled_for": when, "platform": "tiktok"}, timeout=20)
        assert r.status_code == 422

    def test_past_scheduled_for_returns_422(self, ctx, draft_post):
        s = ctx["s"]
        pid = draft_post["id"]
        when = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        r = s.post(f"{API}/social/posts/{pid}/schedule-autopost",
                   json={"scheduled_for": when, "platform": "twitter"}, timeout=20)
        assert r.status_code == 422

    def test_bad_iso_returns_422(self, ctx, draft_post):
        s = ctx["s"]
        pid = draft_post["id"]
        r = s.post(f"{API}/social/posts/{pid}/schedule-autopost",
                   json={"scheduled_for": "not-a-date", "platform": "twitter"}, timeout=20)
        assert r.status_code == 422

    def test_unknown_post_id_returns_404(self, ctx):
        s = ctx["s"]
        when = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        # Valid ObjectId-shaped but not found
        fake = "0" * 24
        r = s.post(f"{API}/social/posts/{fake}/schedule-autopost",
                   json={"scheduled_for": when, "platform": "twitter"}, timeout=20)
        assert r.status_code == 404


class TestScheduleAutopostSuccess:
    def test_schedule_then_cancel(self, ctx, draft_post):
        s = ctx["s"]
        pid = draft_post["id"]
        when = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        r = s.post(f"{API}/social/posts/{pid}/schedule-autopost",
                   json={"scheduled_for": when, "platform": "linkedin"}, timeout=20)
        assert r.status_code == 200, r.text
        body = r.json()
        # Required fields per spec
        assert body.get("autopost_enabled") is True
        assert body.get("autopost_platform") == "linkedin"
        assert body.get("autopost_status") == "pending"
        assert "autopost_mission_id" in body
        assert body.get("autopost_mission_id") == ""
        assert body.get("scheduled_for")
        # Status flips draft -> queued
        assert body.get("status") == "queued"

        # Cancel
        rc = s.delete(f"{API}/social/posts/{pid}/schedule-autopost", timeout=20)
        assert rc.status_code == 200, rc.text
        cb = rc.json()
        assert cb.get("autopost_enabled") is False
        assert cb.get("autopost_status") == "cancelled"


# ── Sessions CRUD ──────────────────────────────────────────────────────────
class TestSocialSessions:
    def test_upsert_rejects_empty_storage_state(self, ctx):
        s, org_id = ctx["s"], ctx["org_id"]
        r = s.post(f"{API}/orgs/{org_id}/social/sessions", json={
            "platform": "twitter",
            "storage_state": {"cookies": [], "origins": []},
            "label": "empty"
        }, timeout=20)
        assert r.status_code == 422

    def test_upsert_rejects_bad_platform(self, ctx):
        s, org_id = ctx["s"], ctx["org_id"]
        r = s.post(f"{API}/orgs/{org_id}/social/sessions", json={
            "platform": "myspace",
            "storage_state": {"cookies": [{"name": "x", "value": "y"}]},
        }, timeout=20)
        assert r.status_code == 422

    def test_upsert_get_delete_no_leak(self, ctx):
        s, org_id = ctx["s"], ctx["org_id"]
        fake_cookies = [
            {"name": "auth_token", "value": "FAKE_SECRET_DO_NOT_LEAK",
             "domain": ".x.com", "path": "/", "secure": True, "httpOnly": True,
             "sameSite": "None", "expires": -1},
        ]
        body = {
            "platform": "twitter",
            "storage_state": {"cookies": fake_cookies, "origins": []},
            "label": "TEST_x",
        }
        r = s.post(f"{API}/orgs/{org_id}/social/sessions", json=body, timeout=20)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["platform"] == "twitter"
        assert d["cookies_count"] == 1
        assert d["origins_count"] == 0
        # Critical: raw cookies must NEVER appear
        assert "storage_state" not in d
        assert "FAKE_SECRET_DO_NOT_LEAK" not in r.text

        # Upsert again — should not duplicate
        r2 = s.post(f"{API}/orgs/{org_id}/social/sessions", json=body, timeout=20)
        assert r2.status_code == 200

        # GET listing
        rg = s.get(f"{API}/orgs/{org_id}/social/sessions", timeout=20)
        assert rg.status_code == 200
        sessions = rg.json()
        assert isinstance(sessions, list)
        twitters = [x for x in sessions if x["platform"] == "twitter"]
        assert len(twitters) == 1
        assert "FAKE_SECRET_DO_NOT_LEAK" not in rg.text

        # DELETE
        rd = s.delete(f"{API}/orgs/{org_id}/social/sessions/twitter", timeout=20)
        assert rd.status_code == 200
        rg2 = s.get(f"{API}/orgs/{org_id}/social/sessions", timeout=20)
        assert all(x["platform"] != "twitter" for x in rg2.json())


# ── End-to-end fire (slow: 90s + mission run) ───────────────────────────────
@pytest.mark.slow
class TestAutopostEndToEnd:
    def test_fake_cookies_lead_to_failed_not_published(self, ctx):
        """Schedule for ~75s, upload fake storage_state, wait, verify
        autopost_status='failed' and post.status NOT 'published'."""
        s, org_id = ctx["s"], ctx["org_id"]
        # Fresh draft for this dedicated test
        draft_resp = s.post(f"{API}/orgs/{org_id}/social/draft", json={
            "prompt": "TEST_ e2e autopost smoke.",
            "platforms": ["twitter"], "count": 1,
        }, timeout=120)
        assert draft_resp.status_code == 200
        dj = draft_resp.json()
        post = (dj["posts"] if isinstance(dj, dict) and "posts" in dj else dj)[0]
        pid = post["id"]

        # Upload fake cookies
        s.post(f"{API}/orgs/{org_id}/social/sessions", json={
            "platform": "twitter",
            "storage_state": {"cookies": [
                {"name": "auth_token", "value": "FAKE", "domain": ".x.com",
                 "path": "/", "secure": True, "httpOnly": True,
                 "sameSite": "None", "expires": -1}
            ], "origins": []},
            "label": "TEST_e2e",
        }, timeout=20)

        when = (datetime.now(timezone.utc) + timedelta(seconds=75)).isoformat()
        r = s.post(f"{API}/social/posts/{pid}/schedule-autopost",
                   json={"scheduled_for": when, "platform": "twitter"}, timeout=20)
        assert r.status_code == 200, r.text
        assert r.json()["autopost_status"] == "pending"

        # Poll up to 5 minutes
        deadline = time.time() + 300
        final_status = None
        post_status = None
        last = None
        while time.time() < deadline:
            time.sleep(15)
            g = s.get(f"{API}/orgs/{org_id}/social/posts", timeout=20)
            assert g.status_code == 200
            gj = g.json()
            posts_list = gj["posts"] if isinstance(gj, dict) and "posts" in gj else gj
            match = next((p for p in posts_list if p["id"] == pid), None)
            assert match
            last = match
            final_status = match.get("autopost_status")
            post_status = match.get("status")
            if final_status in ("posted", "failed", "cancelled"):
                break

        assert final_status in ("failed", "cancelled"), (
            f"expected failed/cancelled with fake cookies, got: {final_status}; "
            f"post={last}"
        )
        # Critical regression guard: must NOT be marked published.
        assert post_status != "published", (
            f"post should NOT be published on autopost failure; got {post_status}"
        )
        # Should have an error message recorded
        assert (last.get("autopost_error") or "").strip(), \
            f"expected non-empty autopost_error, got: {last.get('autopost_error')!r}"


# ── browser_tools.register_mission_storage_state unit ───────────────────────
class TestRegisterMissionStorageState:
    def test_registry_round_trip(self):
        from browser_tools import (
            register_mission_storage_state,
            _MISSION_STORAGE_STATE,
        )
        mid = f"TEST_mission_{uuid.uuid4().hex[:8]}"
        ss = {"cookies": [{"name": "k", "value": "v"}], "origins": []}
        register_mission_storage_state(mid, ss)
        assert _MISSION_STORAGE_STATE.get(mid) == ss
        # cleanup
        _MISSION_STORAGE_STATE.pop(mid, None)
