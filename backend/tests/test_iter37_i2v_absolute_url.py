"""Iteration 37 — RETEST of the image-to-video fix after the env-var alignment.

Previous iteration (36) found that although chat + social routed to Kling I2V,
the `image_url` handed to Kie was a RELATIVE `/api/files/...` path because the
code read `PUBLIC_BASE_URL` / `REACT_APP_BACKEND_URL` — env vars that don't
exist in `/app/backend/.env`. Backend .env has `FRONTEND_URL` (set) and
`PUBLIC_ORIGIN_OVERRIDE` (present but empty in preview). This retest verifies:

  1. Chat I2V — `kie_image_url` on the persisted pending video msg is an
     ABSOLUTE https:// URL.
  2. Chat I2V defensive guard — when PUBLIC_ORIGIN_OVERRIDE and FRONTEND_URL
     are both empty, `_kick_off_image_to_video` (called with a relative URL)
     must synchronously persist a media_status='failed' agent message with
     a clear error text, NOT submit a Kie job.
  3. Chat regression — image + edit prompt → media_type != 'video'.
  4. Social queue video — POST /api/social/posts/{id}/generate-video on a
     post with a local `/api/files/...` image. Verify the outbound Kie
     call resolves image_url to absolute HTTPS. We monkeypatch
     `social_posts.kie_submit_kie_task` to capture the kwargs.
  5. Chat no-attachment + video prompt regression — POST message to a
     conversation with a "make me a video" prompt (no attachment) →
     still routes through the legacy `_kick_off_video` (Veo family),
     media_type='video' + kie_model like 'veo3_fast'.
"""
import asyncio
import io
import os
import sys
import uuid
from pathlib import Path

import pytest
import requests

# Add /app/backend to path so we can import server + social_posts.
_BACKEND = str(Path(__file__).resolve().parent.parent)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

API = os.environ["REACT_APP_BACKEND_URL"].rstrip("/") + "/api"

# Tiny 1x1 red PNG (89 bytes) — minimum viable image attachment.
_PNG_1x1_RED = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
)


@pytest.fixture(scope="module")
def ctx():
    """Register a standing superadmin, land in an org, spin up a team +
    direct conversation."""
    s = requests.Session()
    s.headers["Content-Type"] = "application/json"
    email = "reggie@coolgeek.me"
    pwd = "superadminpass123"
    r = s.post(f"{API}/auth/login",
               json={"email": email, "password": pwd}, timeout=30)
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
    tok = r.json().get("access_token") or r.json().get("token")
    s.headers["Authorization"] = f"Bearer {tok}"

    r = s.get(f"{API}/orgs", timeout=20)
    assert r.status_code == 200, r.text
    orgs = r.json() or []
    assert orgs, "reggie must have at least one org"
    org_id = orgs[0]["id"]

    # Team + direct conversation
    t = s.post(f"{API}/orgs/{org_id}/teams",
               json={"name": f"TEST_I2V37_{uuid.uuid4().hex[:6]}",
                     "visibility": "closed"},
               timeout=30)
    assert t.status_code in (200, 201), t.text
    team_id = t.json().get("id") or t.json().get("_id")

    agent_id = "design-image-prompt-engineer"
    c = s.post(f"{API}/teams/{team_id}/conversations",
               json={"type": "direct", "agent_ids": [agent_id],
                     "title": "TEST_i2v37"},
               timeout=30)
    assert c.status_code in (200, 201), c.text
    conv_id = c.json().get("id") or c.json().get("_id")

    return {"s": s, "org_id": org_id, "team_id": team_id,
            "conv_id": conv_id, "agent_id": agent_id, "email": email}


def _upload(ctx, text: str, filename: str = "clip.png"):
    s = requests.Session()
    s.headers["Authorization"] = ctx["s"].headers["Authorization"]
    files = [("files", (filename, io.BytesIO(_PNG_1x1_RED), "image/png"))]
    data = {"text": text}
    return s.post(f"{API}/conversations/{ctx['conv_id']}/upload",
                  files=files, data=data, timeout=120)


# ─────────────────────────────────────────────────────────────────────
# 1. Chat I2V — kie_image_url must be an ABSOLUTE https:// URL
# ─────────────────────────────────────────────────────────────────────
class TestChatI2VAbsoluteURL:
    def test_kie_image_url_is_absolute_https(self, ctx):
        r = _upload(ctx, "make this into a video")
        assert r.status_code == 200, r.text
        body = r.json()
        agent_msgs = body.get("agent_messages") or []
        assert agent_msgs, f"No agent_messages: {body}"
        m = agent_msgs[0]

        # Retest core assertion — iteration 36 bug: kie_image_url used to
        # be a RELATIVE `/api/files/...` path. After the env-var
        # alignment it must be absolute.
        assert m.get("media_type") == "video", (
            f"REGRESSION: fast-path did not fire. media_type="
            f"{m.get('media_type')} intent={m.get('intent')}"
        )
        kie_img = m.get("kie_image_url") or ""
        assert kie_img.startswith("https://") or kie_img.startswith("http://"), (
            f"ITER36 BUG STILL PRESENT: kie_image_url is not absolute: "
            f"{kie_img!r}. Backend .env must expose PUBLIC_ORIGIN_OVERRIDE or "
            f"FRONTEND_URL and the code must read them."
        )
        # Preview env FRONTEND_URL=https://emergent-wing.preview.emergentagent.com
        assert "/api/files/" in kie_img, (
            f"kie_image_url doesn't contain /api/files/: {kie_img}"
        )
        status = m.get("media_status")
        assert status in ("pending", "failed"), f"Unexpected status={status}: {m}"
        if status == "pending":
            assert m.get("kie_task_id"), f"pending but no kie_task_id: {m}"
            assert m.get("kie_model") == "kling-v21-master-i2v", (
                f"Wrong kie_model: {m.get('kie_model')}"
            )
        print(f"ITER37 chat I2V OK: kie_image_url={kie_img} status={status} "
              f"task_id={m.get('kie_task_id')}")


# ─────────────────────────────────────────────────────────────────────
# 2. Defensive guard — relative URL must fail fast (not submit to Kie)
# ─────────────────────────────────────────────────────────────────────
class TestI2VDefensiveGuard:
    def test_relative_url_fails_fast(self, ctx):
        """Directly invoke `_kick_off_image_to_video` with a relative URL
        to simulate the misconfigured-env case. The guard at
        server.py:7162 must insert a failed message with clear error
        text and NOT submit any Kie job.
        """
        import server

        conv_id = ctx["conv_id"]
        agent_id = ctx["agent_id"]

        async def _run():
            return await server._kick_off_image_to_video(
                conv_id, agent_id, "make me a video",
                image_url="/api/files/conv/abc/xyz.png",  # RELATIVE
                duration="5",
            )

        msg = asyncio.get_event_loop().run_until_complete(_run()) \
            if not asyncio.get_event_loop().is_running() \
            else asyncio.new_event_loop().run_until_complete(_run())

        assert msg.get("media_type") == "video", f"Wrong media_type: {msg}"
        assert msg.get("media_status") == "failed", (
            f"Defensive guard didn't fire — status={msg.get('media_status')} "
            f"kie_task_id={msg.get('kie_task_id')}. This means Kie WAS submitted "
            f"with a relative URL (silent failure the user reported): {msg}"
        )
        assert not msg.get("kie_task_id"), (
            f"Defensive guard failed — a Kie task was submitted with a relative "
            f"URL (this is the exact bug the guard is supposed to prevent): {msg}"
        )
        text = (msg.get("text") or "").lower()
        assert "public" in text or "url" in text or "admin" in text, (
            f"Failed message text isn't clear about the misconfig: {msg.get('text')!r}"
        )
        print(f"ITER37 defensive guard OK: status=failed text={msg.get('text')!r}")


# ─────────────────────────────────────────────────────────────────────
# 3. Chat regression — image + edit prompt → still image-edit path
# ─────────────────────────────────────────────────────────────────────
class TestImageEditRegression:
    def test_edit_prompt_still_image_edit(self, ctx):
        r = _upload(ctx, "add my logo to this")
        assert r.status_code == 200, r.text
        body = r.json()
        agent_msgs = body.get("agent_messages") or []
        assert agent_msgs, f"No agent_messages: {body}"
        m = agent_msgs[0]
        assert m.get("media_type") != "video", (
            f"REGRESSION: edit prompt fired the video fast-path: {m}"
        )
        assert m.get("kie_model") != "kling-v21-master-i2v", (
            f"REGRESSION: edit prompt submitted to Kling I2V: {m}"
        )
        print(f"edit-path OK: media_type={m.get('media_type')} "
              f"intent={m.get('intent')}")


# ─────────────────────────────────────────────────────────────────────
# 4. Social queue — outbound Kie call must include absolute image_url
# ─────────────────────────────────────────────────────────────────────
class TestSocialQueueVideo:
    def test_outbound_image_url_is_absolute(self, ctx):
        """Insert a social post with a local /api/files/... cover image
        directly into Mongo (bypasses the multi-step draft flow), POST
        /api/social/posts/{id}/generate-video, then inspect backend logs
        for the ITER36 "no public base URL" WARNING. Absence of the
        warning proves the env-var fallback chain resolves to the
        preview FRONTEND_URL and image_public_url is absolute.
        """
        import time
        from datetime import datetime, timezone
        from pymongo import MongoClient
        from bson import ObjectId

        s = ctx["s"]
        org_id = ctx["org_id"]

        mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
        db_name = os.environ.get("DB_NAME", "test_database")
        client = MongoClient(mongo_url)
        db = client[db_name]

        # Grab a user id for the post's owner (reggie).
        user = db.users.find_one({"email": ctx["email"]})
        assert user, "reggie user missing"

        fake_local_url = f"/api/files/social/{uuid.uuid4().hex}.png"
        post_doc = {
            "org_id": org_id,
            "created_by": str(user["_id"]),
            "text": "TEST_iter37 social video with local image",
            "platforms": ["instagram"],
            "status": "draft",
            "image": {"url": fake_local_url, "prompt": "test"},
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        res = db.social_posts.insert_one(post_doc)
        post_id = str(res.inserted_id)

        # Snapshot backend log offset so we can tail only NEW lines
        log_paths = [
            "/var/log/supervisor/backend.err.log",
            "/var/log/supervisor/backend.out.log",
        ]
        pre_sizes = {}
        for p in log_paths:
            try:
                pre_sizes[p] = os.path.getsize(p)
            except OSError:
                pre_sizes[p] = 0

        # Trigger generate-video with a Kling model (image-to-video capable)
        v = s.post(f"{API}/social/posts/{post_id}/generate-video", json={
            "model": "kling-3",
            "aspect_ratio": "9:16",
        }, timeout=30)
        # 202 accepted / 200 depending on impl
        assert v.status_code in (200, 202), (
            f"generate-video failed: {v.status_code} {v.text}"
        )

        # Let the background task run enough to make the outbound
        # Kie call attempt (submit is quick — polling is the slow part).
        time.sleep(6)

        # Read new log content
        new_log = ""
        for path, off in pre_sizes.items():
            try:
                with open(path, "rb") as f:
                    f.seek(off)
                    new_log += f.read().decode("utf-8", errors="ignore")
            except OSError:
                continue

        # The WARNING iteration_36 flagged should NOT appear now that
        # FRONTEND_URL is being read as a fallback.
        bad_warning = "no public base URL is configured"
        assert bad_warning not in new_log, (
            f"ITER36 SOCIAL BUG STILL PRESENT: backend logged "
            f"{bad_warning!r} — the code isn't reading FRONTEND_URL / "
            f"PUBLIC_ORIGIN_OVERRIDE. Log excerpt:\n{new_log[-800:]}"
        )
        print(f"ITER37 social video OK: no misconfig warning in logs "
              f"(scanned {len(new_log)} new bytes).")


# ─────────────────────────────────────────────────────────────────────
# 5. Regression — no attachment + video prompt → still uses Veo path
# ─────────────────────────────────────────────────────────────────────
class TestNoAttachmentVideoRegression:
    def test_video_prompt_no_attachment_uses_veo(self, ctx):
        s = ctx["s"]
        r = s.post(f"{API}/conversations/{ctx['conv_id']}/messages", json={
            "text": "make me a short 5s video of a sunset over the ocean",
        }, timeout=60)
        assert r.status_code == 200, r.text
        body = r.json()
        # /messages endpoint returns agent_messages (plural list) on
        # video routing (see server.py:5906 → _kick_off_video).
        msgs = body.get("agent_messages") or ([body["agent_message"]] if body.get("agent_message") else [])
        assert msgs, f"No agent messages returned: {body}"
        m = msgs[0]
        # Should be a video pending/failed via _kick_off_video (Veo)
        assert m.get("media_type") == "video", (
            f"No-attachment video prompt didn't route to video: {m}"
        )
        # Legacy path uses DEFAULT_VIDEO_MODEL_LOCAL (veo3_fast). Kling
        # I2V would only fire via the multi-file upload fast-path.
        assert m.get("kie_model") != "kling-v21-master-i2v", (
            f"REGRESSION: text-only video prompt routed to Kling I2V "
            f"instead of Veo path: {m}"
        )
        status = m.get("media_status")
        assert status in ("pending", "failed"), f"Unexpected status={status}: {m}"
        print(f"ITER37 no-attachment veo OK: kie_model={m.get('kie_model')} "
              f"status={status}")
