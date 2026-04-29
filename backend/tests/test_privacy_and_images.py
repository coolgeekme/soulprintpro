"""Backend tests for conversation privacy + image-generation tool (Nano Banana).

Covers:
  * Private conversations: user B in same org cannot see/read/write user A's
    direct conversation (list filter, GET conv, GET /messages, POST messages,
    POST /generate-image all 403/404).
  * Group conversations: including user B lets them see and post.
  * POST /api/conversations/{cid}/generate-image
      - happy path returns user_message(intent=image_request, text=🎨 …)
        and agent_message(media_type=image, media_url=/api/files/<fn>,
        media_filename matches ALLOWED_FILENAME_RE).
      - 400 on empty prompt.
      - 403/404 for non-member.
      - 400 on agent_id not in group conv, 400 on unknown agent_id.
  * GET /api/files/{filename}
      - 200 + image/* content-type for conversation member (owner).
      - 401 for anonymous.
      - 403 for authenticated non-member.
      - 400 for malformed filename.
      - 404 for valid-shape but non-existent filename.
  * Persistence: GET /messages includes the agent message with media_url and
    media_filename intact.
"""
import os
import re
import uuid
import pytest
import requests


BASE_URL = os.environ.get("REACT_APP_BACKEND_URL")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for ln in f:
            if ln.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = ln.strip().split("=", 1)[1]
                break
BASE_URL = (BASE_URL or "").rstrip("/")
assert BASE_URL, "REACT_APP_BACKEND_URL not configured"
API = f"{BASE_URL}/api"

ALLOWED_FILENAME_RE = re.compile(r"^[a-f0-9]{16,}\.(png|jpg|jpeg|webp|gif|pdf)$", re.IGNORECASE)
MEDIA_URL_RE = re.compile(r"^/api/files/[a-f0-9]{16,}\.(png|jpg|jpeg|webp|gif)$", re.IGNORECASE)


def _uniq(p="t"):
    return f"TEST_{p}_{uuid.uuid4().hex[:10]}"


def _sess():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


def _register(s, name=None):
    name = name or _uniq("user")
    email = f"{name.lower()}@test.dev"
    r = s.post(
        f"{API}/auth/register",
        json={"name": name, "email": email, "password": "password123"},
    )
    assert r.status_code == 200, r.text
    return r.json(), email


# ── Shared fixture: org with user A (creator) + user B (fellow member) ──────
@pytest.fixture(scope="module")
def two_user_ctx():
    # User A creates org + team, invites user B
    sA = _sess()
    uA, emailA = _register(sA)
    org = sA.post(f"{API}/orgs", json={"name": _uniq("org")}).json()
    assert "id" in org, org
    team = sA.post(
        f"{API}/orgs/{org['id']}/teams",
        json={"name": _uniq("team"), "description": "d"},
    ).json()
    assert "id" in team, team

    # Set a modest soulprint (helps image generation system prompt)
    sA.put(
        f"{API}/teams/{team['id']}/soulprint",
        json={
            "mission": "Delight SMBs",
            "tone": "warm, confident",
            "audience": "founders",
            "values": "clarity",
        },
    )

    # Create invite, user B accepts
    inv = sA.post(f"{API}/orgs/{org['id']}/invites", json={}).json()
    assert inv.get("code"), inv
    sB = _sess()
    uB, emailB = _register(sB)
    r = sB.post(f"{API}/invites/accept", json={"code": inv["code"]})
    assert r.status_code == 200, r.text

    return {
        "sA": sA, "uA": uA, "emailA": emailA,
        "sB": sB, "uB": uB, "emailB": emailB,
        "org_id": org["id"],
        "team_id": team["id"],
    }


def _create_direct_conv(ctx, agent_id="marketing-content-creator"):
    """User A creates a PRIVATE direct conv with a single agent (no human member)."""
    r = ctx["sA"].post(
        f"{API}/teams/{ctx['team_id']}/conversations",
        json={
            "type": "direct",
            "agent_ids": [agent_id],
            "member_ids": [],
            "title": _uniq("priv"),
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _create_group_conv(ctx, agent_ids=None, include_b=True):
    agent_ids = agent_ids or ["marketing-content-creator", "engineering-frontend-developer"]
    members = [ctx["uB"]["id"]] if include_b else []
    r = ctx["sA"].post(
        f"{API}/teams/{ctx['team_id']}/conversations",
        json={
            "type": "group",
            "agent_ids": agent_ids,
            "member_ids": members,
            "title": _uniq("grp"),
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


# ── Privacy ──────────────────────────────────────────────────────────────────
class TestPrivacy:
    def test_direct_conv_hidden_from_other_user(self, two_user_ctx):
        ctx = two_user_ctx
        conv = _create_direct_conv(ctx)
        cid = conv["id"]

        # 1. List — user A sees it, user B does not.
        rA = ctx["sA"].get(f"{API}/teams/{ctx['team_id']}/conversations")
        assert rA.status_code == 200
        assert any(c["id"] == cid for c in rA.json()), "creator should see own conv"

        rB = ctx["sB"].get(f"{API}/teams/{ctx['team_id']}/conversations")
        assert rB.status_code == 200
        assert not any(c["id"] == cid for c in rB.json()), (
            "non-member MUST NOT see private direct conv in list"
        )

        # 2. GET conv
        rB = ctx["sB"].get(f"{API}/conversations/{cid}")
        assert rB.status_code in (403, 404), rB.status_code

        # 3. GET /messages
        rB = ctx["sB"].get(f"{API}/conversations/{cid}/messages")
        assert rB.status_code in (403, 404)

        # 4. POST /messages
        rB = ctx["sB"].post(
            f"{API}/conversations/{cid}/messages", json={"text": "sneaky"}
        )
        assert rB.status_code in (403, 404)

    def test_other_user_cannot_generate_image_on_private_conv(self, two_user_ctx):
        ctx = two_user_ctx
        conv = _create_direct_conv(ctx)
        rB = ctx["sB"].post(
            f"{API}/conversations/{conv['id']}/generate-image",
            json={"prompt": "a cat in a spacesuit"},
        )
        assert rB.status_code in (403, 404), rB.status_code

    def test_group_member_can_see_and_read(self, two_user_ctx):
        ctx = two_user_ctx
        conv = _create_group_conv(ctx, include_b=True)
        cid = conv["id"]

        rB = ctx["sB"].get(f"{API}/teams/{ctx['team_id']}/conversations")
        assert rB.status_code == 200
        assert any(c["id"] == cid for c in rB.json()), (
            "group member B should see the group conv in their list"
        )

        rB = ctx["sB"].get(f"{API}/conversations/{cid}")
        assert rB.status_code == 200
        assert rB.json()["type"] == "group"

        rB = ctx["sB"].get(f"{API}/conversations/{cid}/messages")
        assert rB.status_code == 200


# ── Image generation ────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def generated_image(two_user_ctx):
    """Create a private direct conv and generate one image — reused by several tests."""
    ctx = two_user_ctx
    conv = _create_direct_conv(ctx, agent_id="marketing-content-creator")
    cid = conv["id"]
    r = ctx["sA"].post(
        f"{API}/conversations/{cid}/generate-image",
        json={"prompt": "A minimalist logo: a stylised acorn in deep teal, flat vector"},
        timeout=90,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    # Some models may fail — the endpoint returns ok:false with an error message
    # in that case. We skip image assertions only in that narrow window.
    if data.get("ok") is False:
        pytest.skip(f"Nano Banana returned no image this run: {data.get('agent_message', {}).get('text')}")
    return {"conv_id": cid, "data": data}


class TestImageGeneration:
    def test_happy_path_shape(self, generated_image):
        data = generated_image["data"]
        um = data["user_message"]
        am = data["agent_message"]

        # user_message shape
        assert um["intent"] == "image_request"
        assert um["text"].startswith("🎨 "), f"got: {um['text']!r}"
        assert um["sender_type"] == "user"

        # agent_message shape
        assert am["sender_type"] == "agent"
        assert am["media_type"] == "image"
        assert am["text"], "agent message text should be non-empty"
        assert ALLOWED_FILENAME_RE.match(am["media_filename"]), (
            f"bad media_filename: {am['media_filename']}"
        )
        assert MEDIA_URL_RE.match(am["media_url"]), f"bad media_url: {am['media_url']}"
        assert am["media_url"].endswith(am["media_filename"])

    def test_empty_prompt_returns_400(self, two_user_ctx):
        ctx = two_user_ctx
        conv = _create_direct_conv(ctx)
        r = ctx["sA"].post(
            f"{API}/conversations/{conv['id']}/generate-image",
            json={"prompt": "   "},
        )
        assert r.status_code == 400, r.text

    def test_unknown_agent_id_returns_400(self, two_user_ctx):
        ctx = two_user_ctx
        conv = _create_direct_conv(ctx)
        r = ctx["sA"].post(
            f"{API}/conversations/{conv['id']}/generate-image",
            json={"prompt": "a kitten", "agent_id": "nonsense-agent-id-xyz"},
        )
        assert r.status_code == 400, r.text

    def test_agent_not_in_group_returns_400(self, two_user_ctx):
        ctx = two_user_ctx
        conv = _create_group_conv(
            ctx,
            agent_ids=["marketing-content-creator", "engineering-frontend-developer"],
            include_b=True,
        )
        r = ctx["sA"].post(
            f"{API}/conversations/{conv['id']}/generate-image",
            json={"prompt": "a kitten", "agent_id": "finance-fpa-analyst"},
        )
        assert r.status_code == 400, r.text

    def test_messages_endpoint_preserves_media_fields(self, generated_image, two_user_ctx):
        ctx = two_user_ctx
        cid = generated_image["conv_id"]
        fname = generated_image["data"]["agent_message"]["media_filename"]
        r = ctx["sA"].get(f"{API}/conversations/{cid}/messages")
        assert r.status_code == 200
        msgs = r.json()
        am = next((m for m in msgs if m.get("media_filename") == fname), None)
        assert am is not None, "persisted agent message not found"
        assert am["media_type"] == "image"
        assert am["media_url"] == f"/api/files/{fname}"
        # user intro message also persisted
        ur = next((m for m in msgs if m.get("intent") == "image_request"), None)
        assert ur is not None and ur["text"].startswith("🎨 ")


# ── File serving access control ─────────────────────────────────────────────
class TestFileServing:
    def test_owner_can_fetch_image(self, generated_image, two_user_ctx):
        ctx = two_user_ctx
        fname = generated_image["data"]["agent_message"]["media_filename"]
        r = ctx["sA"].get(f"{API}/files/{fname}")
        assert r.status_code == 200, r.text
        ctype = r.headers.get("content-type", "")
        assert ctype.startswith("image/"), f"expected image/* got {ctype}"
        assert len(r.content) > 100  # non-empty file

    def test_anonymous_gets_401(self, generated_image):
        fname = generated_image["data"]["agent_message"]["media_filename"]
        r = requests.get(f"{API}/files/{fname}")  # fresh session, no cookie
        assert r.status_code == 401, r.status_code

    def test_non_member_gets_403(self, generated_image, two_user_ctx):
        ctx = two_user_ctx
        fname = generated_image["data"]["agent_message"]["media_filename"]
        r = ctx["sB"].get(f"{API}/files/{fname}")
        assert r.status_code == 403, r.status_code

    def test_malformed_filename_returns_400(self, two_user_ctx):
        ctx = two_user_ctx
        r = ctx["sA"].get(f"{API}/files/..%2Fetc%2Fpasswd")
        # FastAPI may normalise the URL; both 400 and 404 indicate rejection.
        assert r.status_code in (400, 404), r.status_code
        r = ctx["sA"].get(f"{API}/files/hello.txt")
        assert r.status_code == 400, r.status_code
        r = ctx["sA"].get(f"{API}/files/SHORT.png")  # <16 hex chars
        assert r.status_code == 400, r.status_code

    def test_nonexistent_valid_shape_returns_404(self, two_user_ctx):
        ctx = two_user_ctx
        # valid-shape hex but does not exist on disk / not linked to a message
        ghost = "deadbeefcafef00d" + "0" * 16 + ".png"
        r = ctx["sA"].get(f"{API}/files/{ghost}")
        assert r.status_code == 404, r.status_code
