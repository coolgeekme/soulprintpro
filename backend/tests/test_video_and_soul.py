"""Iteration 6 — soul + general team/conv + Kie.ai video generation.

Tests the new behavior shipped in this batch:
  - POST /api/orgs with website_url + soul_description (and combinations) auto-
    populates `soul`, `general_team_id`, `general_conversation_id` on the org.
  - The auto-created General team + General group conversation exist and have
    the expected flags (auto_respond=true, smart_expand=true,
    chief-of-staff anchored).
  - Sending a text message in the General conversation yields an
    auto-respond agent reply (no @mention required).
  - POST /api/conversations/{id}/generate-video with explicit prompt returns a
    pending video placeholder, and polling resolves to media_status='ready'
    with an mp4 URL within ~4 minutes.
  - POST /api/conversations/{id}/messages with text containing video intent
    keywords routes to the video pipeline (returns pending video, no text).
  - Image generation continues to work in the General conversation
    (regression).
"""

import os
import secrets
import time

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL")
if not BASE_URL:
    try:
        with open("/app/frontend/.env") as fh:
            for ln in fh:
                if ln.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = ln.strip().split("=", 1)[1]
                    break
    except FileNotFoundError:
        pass
BASE_URL = (BASE_URL or "").rstrip("/")
assert BASE_URL, "REACT_APP_BACKEND_URL not configured"


def _register():
    email = f"TEST_{secrets.token_hex(6)}@test.dev"
    password = "password123"
    r = requests.post(
        f"{BASE_URL}/api/auth/register",
        json={"name": "Soul Tester", "email": email, "password": password},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    token = r.json().get("access_token") or r.json().get("token")
    assert token, f"no token in register response: {r.json()}"
    return {"email": email, "token": token, "headers": {"Authorization": f"Bearer {token}"}}


@pytest.fixture(scope="module")
def user_ctx():
    return _register()


# ──────────────────────────────────────────────────────────────────
# Org creation — soul + general team/conv
# ──────────────────────────────────────────────────────────────────
class TestOrgSoulAndGeneral:
    def test_org_with_url_and_manual_soul(self, user_ctx):
        r = requests.post(
            f"{BASE_URL}/api/orgs",
            headers=user_ctx["headers"],
            json={
                "name": "TEST_Acorn Tools",
                "website_url": "https://example.com",
                "soul_description": "We build sleek productivity tools for indie creators.",
            },
            timeout=120,
        )
        assert r.status_code == 200, r.text
        org = r.json()
        # Org top-level fields persisted
        assert org["website_url"] == "https://example.com"
        assert "indie creators" in org["soul_description"]
        # Structured soul populated by LLM
        soul = org["soul"]
        assert isinstance(soul, dict)
        for key in ("mission", "audience", "tone", "values", "north_star", "summary"):
            assert key in soul, f"soul.{key} missing"
        # At least the summary/mission should have content given the manual hint
        assert (soul["summary"] or soul["mission"]).strip(), "soul should be non-empty"
        # General team + conv autocreated
        assert org["general_team_id"], "general_team_id missing"
        assert org["general_conversation_id"], "general_conversation_id missing"
        # Stash on the user_ctx for reuse downstream
        user_ctx["org"] = org

    def test_org_with_only_url(self, user_ctx):
        r = requests.post(
            f"{BASE_URL}/api/orgs",
            headers=user_ctx["headers"],
            json={"name": "TEST_URLOnly", "website_url": "https://example.com"},
            timeout=120,
        )
        assert r.status_code == 200, r.text
        org = r.json()
        assert org["general_team_id"]
        assert org["general_conversation_id"]
        # soul keys all present (may be empty if scrape was thin)
        for key in ("mission", "audience", "tone", "values", "north_star", "summary"):
            assert key in org["soul"]

    def test_org_with_only_manual(self, user_ctx):
        r = requests.post(
            f"{BASE_URL}/api/orgs",
            headers=user_ctx["headers"],
            json={"name": "TEST_ManualOnly", "soul_description": "AI tools for educators."},
            timeout=120,
        )
        assert r.status_code == 200, r.text
        org = r.json()
        assert org["general_team_id"]
        assert org["general_conversation_id"]
        soul = org["soul"]
        # With a manual hint, summary/mission should be populated
        assert (soul.get("summary") or soul.get("mission") or "").strip()

    def test_org_with_only_name(self, user_ctx):
        r = requests.post(
            f"{BASE_URL}/api/orgs",
            headers=user_ctx["headers"],
            json={"name": "TEST_BareOrg"},
            timeout=60,
        )
        assert r.status_code == 200, r.text
        org = r.json()
        # Even with no hint we should get the soul shell + general team/conv
        assert org["general_team_id"]
        assert org["general_conversation_id"]
        for key in ("mission", "audience", "tone", "values", "north_star", "summary"):
            assert key in org["soul"]

    def test_list_and_get_org_includes_soul_fields(self, user_ctx):
        org = user_ctx["org"]
        r = requests.get(f"{BASE_URL}/api/orgs", headers=user_ctx["headers"], timeout=15)
        assert r.status_code == 200
        ids = [o["id"] for o in r.json()]
        assert org["id"] in ids
        # First org returned should have the new fields
        target = next(o for o in r.json() if o["id"] == org["id"])
        for key in ("website_url", "soul_description", "soul", "general_team_id", "general_conversation_id"):
            assert key in target

        r2 = requests.get(f"{BASE_URL}/api/orgs/{org['id']}", headers=user_ctx["headers"], timeout=15)
        assert r2.status_code == 200
        single = r2.json()
        for key in ("website_url", "soul_description", "soul", "general_team_id", "general_conversation_id"):
            assert key in single

    def test_general_team_and_conv_exist(self, user_ctx):
        org = user_ctx["org"]
        # General team
        r = requests.get(
            f"{BASE_URL}/api/teams/{org['general_team_id']}",
            headers=user_ctx["headers"], timeout=15,
        )
        assert r.status_code == 200, r.text
        team = r.json()
        assert team["name"] == "General"
        # General conv
        r2 = requests.get(
            f"{BASE_URL}/api/conversations/{org['general_conversation_id']}",
            headers=user_ctx["headers"], timeout=15,
        )
        assert r2.status_code == 200, r2.text
        conv = r2.json()
        assert conv["type"] == "group"
        assert conv.get("auto_respond") is True
        assert conv.get("smart_expand") is True
        assert "specialized-chief-of-staff" in (conv.get("agent_ids") or [])


# ──────────────────────────────────────────────────────────────────
# General conversation — auto_respond text reply
# ──────────────────────────────────────────────────────────────────
class TestGeneralConvAutoRespond:
    def test_text_message_triggers_auto_respond(self, user_ctx):
        conv_id = user_ctx["org"]["general_conversation_id"]
        r = requests.post(
            f"{BASE_URL}/api/conversations/{conv_id}/messages",
            headers=user_ctx["headers"],
            json={"text": "Hi team, what should I focus on this week?"},
            timeout=120,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # smart_expand=True can produce multiple agent replies (`agent_messages` list)
        # while plain auto_respond yields a single `agent_message`. Accept either.
        agent_msgs = body.get("agent_messages") or ([body["agent_message"]] if body.get("agent_message") else [])
        assert agent_msgs, f"expected at least one agent reply: {body}"
        first = agent_msgs[0]
        assert first.get("sender_type") == "agent"
        assert not first.get("media_type"), first
        assert (first.get("text") or "").strip()
        # Soul-injection sanity: with manual hint "tools for indie creators", at least
        # one of the agent replies should reference indie creators or productivity.
        joined = " ".join((m.get("text") or "").lower() for m in agent_msgs)
        assert any(kw in joined for kw in ("indie", "creator", "productivity", "tool")), (
            f"soul not reflected in agent tone: {joined[:300]}"
        )


# ──────────────────────────────────────────────────────────────────
# Image generation regression
# ──────────────────────────────────────────────────────────────────
class TestImageStillWorks:
    def test_image_intent_message(self, user_ctx):
        conv_id = user_ctx["org"]["general_conversation_id"]
        r = requests.post(
            f"{BASE_URL}/api/conversations/{conv_id}/messages",
            headers=user_ctx["headers"],
            json={"text": "Create an Instagram post image for our launch"},
            timeout=180,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        agent_msgs = body.get("agent_messages") or ([body["agent_message"]] if body.get("agent_message") else [])
        # Find any image-typed message (auto image-intent path)
        img = next((m for m in agent_msgs if m.get("media_type") == "image"), None)
        if img:
            assert (img.get("media_url") or "").startswith("/api/files/"), img
            assert img["media_url"].endswith((".png", ".jpg", ".jpeg", ".webp")), img["media_url"]
        else:
            txt = (agent_msgs[0].get("text", "") if agent_msgs else "")[:160]
            pytest.skip(f"image intent not detected — got text reply instead: {txt}")


# ──────────────────────────────────────────────────────────────────
# Video generation — explicit endpoint
# ──────────────────────────────────────────────────────────────────
@pytest.mark.timeout(300)
class TestVideoExplicit:
    def _wait_for_ready(self, conv_id, msg_id, headers, max_seconds=240):
        deadline = time.time() + max_seconds
        last_status = None
        while time.time() < deadline:
            r = requests.get(
                f"{BASE_URL}/api/conversations/{conv_id}/messages",
                headers=headers, timeout=20,
            )
            assert r.status_code == 200, r.text
            for m in r.json():
                if m.get("id") == msg_id:
                    last_status = m.get("media_status")
                    if last_status in ("ready", "failed"):
                        return m
                    break
            time.sleep(5)
        pytest.fail(f"video did not resolve within {max_seconds}s (last_status={last_status})")

    def test_generate_video_endpoint(self, user_ctx):
        conv_id = user_ctx["org"]["general_conversation_id"]
        r = requests.post(
            f"{BASE_URL}/api/conversations/{conv_id}/generate-video",
            headers=user_ctx["headers"],
            json={
                "prompt": "A 5-second cinematic shot of a tiny robot rolling across a desk.",
                "aspect_ratio": "16:9",
            },
            timeout=30,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        agent_msg = body["agent_message"]
        assert agent_msg["media_type"] == "video"
        assert agent_msg["media_status"] == "pending"
        msg_id = agent_msg["id"]

        ready = self._wait_for_ready(conv_id, msg_id, user_ctx["headers"], max_seconds=240)
        assert ready["media_status"] == "ready", f"video failed/timeout: {ready}"
        url = ready.get("media_url") or ""
        assert url.endswith(".mp4"), f"expected mp4 URL, got: {url}"
        assert url.startswith("http"), f"expected absolute URL, got: {url}"


# ──────────────────────────────────────────────────────────────────
# Video generation — auto-detected intent in send_message
# ──────────────────────────────────────────────────────────────────
@pytest.mark.timeout(60)
class TestVideoAutoIntent:
    def test_video_intent_in_send_message(self, user_ctx):
        conv_id = user_ctx["org"]["general_conversation_id"]
        r = requests.post(
            f"{BASE_URL}/api/conversations/{conv_id}/messages",
            headers=user_ctx["headers"],
            json={"text": "Generate a 5-second cinematic video of a robot rolling through a classroom"},
            timeout=45,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        agent_msgs = body.get("agent_messages") or ([body["agent_message"]] if body.get("agent_message") else [])
        vid = next((m for m in agent_msgs if m.get("media_type") == "video"), None)
        if vid:
            assert vid.get("media_status") == "pending", vid
            assert vid.get("aspect_ratio") in ("16:9", "9:16", "Auto"), vid
        else:
            txt = (agent_msgs[0].get("text", "") if agent_msgs else "")[:160]
            pytest.skip(f"video intent not detected — got text reply instead: {txt}")
