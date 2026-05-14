"""Live-polling endpoint test: GET /api/conversations/{id}/messages/since

Validates the new cursor-based incremental fetch the frontend polls every 3s:

  • Empty cursor → []
  • Garbage cursor → 400
  • Old cursor → all messages
  • Future cursor → []
  • Auth gating via load_conv_or_403 → 403 for non-team-member
  • Multi-user live update: A posts, B's /since cursor=last picks it up
  • Feedback parity: up_count / down_count / my_feedback fields present
  • Regression: /messages (full fetch) still works

Uses the standing superadmin reggie@coolgeek.me to mint a single-use access
code, registers a fresh secondary user with it, then orchestrates the
multi-user flow.
"""
import os
import time
import uuid
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # fallback to frontend/.env when pytest is run without env loaded
    try:
        with open("/app/frontend/.env") as fh:
            for line in fh:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                    break
    except Exception:
        pass

API = f"{BASE_URL}/api"

SUPERADMIN_EMAIL = "reggie@coolgeek.me"
SUPERADMIN_PASSWORD = "superadminpass123"


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── Session-scoped setup ─────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def admin_token():
    r = requests.post(f"{API}/auth/login",
                      json={"email": SUPERADMIN_EMAIL, "password": SUPERADMIN_PASSWORD},
                      timeout=15)
    assert r.status_code == 200, f"superadmin login failed: {r.status_code} {r.text}"
    tok = r.json().get("access_token")
    assert tok
    return tok


@pytest.fixture(scope="session")
def access_code(admin_token):
    """Mint a single-use access code via the superadmin admin route."""
    code_val = f"TESTPOLL{uuid.uuid4().hex[:8].upper()}"
    r = requests.post(f"{API}/admin/access-codes",
                      headers=_auth_headers(admin_token),
                      json={"code": code_val, "usage_type": "multi", "max_uses": 10,
                            "note": "messages/since polling test"},
                      timeout=15)
    assert r.status_code == 200, f"mint access code failed: {r.status_code} {r.text}"
    return r.json()["code"]


def _register(name: str, email: str, password: str, code: str):
    r = requests.post(f"{API}/auth/register",
                      json={"name": name, "email": email, "password": password,
                            "access_code": code},
                      timeout=15)
    assert r.status_code == 200, f"register failed for {email}: {r.status_code} {r.text}"
    return r.json()["access_token"], r.json()["id"]


@pytest.fixture(scope="session")
def user_a(access_code):
    email = f"TEST_polla_{uuid.uuid4().hex[:10]}@example.com"
    tok, uid = _register("Poll A", email, "testpass123", access_code)
    return {"token": tok, "id": uid, "email": email}


@pytest.fixture(scope="session")
def user_b(access_code):
    email = f"TEST_pollb_{uuid.uuid4().hex[:10]}@example.com"
    tok, uid = _register("Poll B", email, "testpass123", access_code)
    return {"token": tok, "id": uid, "email": email}


@pytest.fixture(scope="session")
def user_c(access_code):
    """An outsider — never invited to A's org. Used for the 403 test."""
    email = f"TEST_pollc_{uuid.uuid4().hex[:10]}@example.com"
    tok, uid = _register("Poll C", email, "testpass123", access_code)
    return {"token": tok, "id": uid, "email": email}


@pytest.fixture(scope="session")
def org_and_team(user_a, user_b):
    """User A creates an org + open team, invites B, B accepts."""
    # 1. Create org
    r = requests.post(f"{API}/orgs",
                      headers=_auth_headers(user_a["token"]),
                      json={"name": f"TEST_PollOrg_{uuid.uuid4().hex[:6]}"},
                      timeout=15)
    assert r.status_code in (200, 201), f"org create: {r.status_code} {r.text}"
    org = r.json()
    org_id = org.get("id") or org.get("_id")
    assert org_id

    # 2. Invite B
    r = requests.post(f"{API}/orgs/{org_id}/invites",
                      headers=_auth_headers(user_a["token"]),
                      json={"role": "member"},
                      timeout=15)
    assert r.status_code == 200, f"invite create: {r.status_code} {r.text}"
    code = r.json().get("code") or r.json().get("invite", {}).get("code")
    assert code, f"no invite code in response: {r.json()}"

    # 3. B accepts
    r = requests.post(f"{API}/invites/accept",
                      headers=_auth_headers(user_b["token"]),
                      json={"code": code},
                      timeout=15)
    assert r.status_code == 200, f"invite accept: {r.status_code} {r.text}"

    # 4. Create an open team in the org (A is owner so this works)
    r = requests.post(f"{API}/orgs/{org_id}/teams",
                      headers=_auth_headers(user_a["token"]),
                      json={"name": f"TEST_PollTeam_{uuid.uuid4().hex[:6]}",
                            "visibility": "open"},
                      timeout=15)
    assert r.status_code in (200, 201), f"team create: {r.status_code} {r.text}"
    team = r.json()
    team_id = team.get("id") or team.get("_id")
    assert team_id
    return {"org_id": org_id, "team_id": team_id}


@pytest.fixture(scope="session")
def conversation(user_a, org_and_team):
    """User A creates a direct conversation in the team."""
    r = requests.post(f"{API}/teams/{org_and_team['team_id']}/conversations",
                      headers=_auth_headers(user_a["token"]),
                      json={"title": "TEST poll conv",
                            "type": "direct",
                            "agent_ids": ["specialized-chief-of-staff"]},
                      timeout=15)
    assert r.status_code in (200, 201), f"conv create: {r.status_code} {r.text}"
    conv = r.json()
    cid = conv.get("id") or conv.get("_id")
    assert cid
    return cid


# ── Tests ────────────────────────────────────────────────────────────────────

class TestCursorEdgeCases:
    """Backend basic cursor parsing & query behavior."""

    def test_empty_cursor_returns_empty_list(self, user_a, conversation):
        r = requests.get(f"{API}/conversations/{conversation}/messages/since",
                         headers=_auth_headers(user_a["token"]),
                         params={"cursor": ""},
                         timeout=15)
        assert r.status_code == 200, r.text
        assert r.json() == []

    def test_missing_cursor_param_returns_empty_list(self, user_a, conversation):
        # default `cursor: str = ""` → same as empty
        r = requests.get(f"{API}/conversations/{conversation}/messages/since",
                         headers=_auth_headers(user_a["token"]),
                         timeout=15)
        assert r.status_code == 200, r.text
        assert r.json() == []

    def test_garbage_cursor_returns_400(self, user_a, conversation):
        r = requests.get(f"{API}/conversations/{conversation}/messages/since",
                         headers=_auth_headers(user_a["token"]),
                         params={"cursor": "not-a-timestamp"},
                         timeout=15)
        assert r.status_code == 400, r.text
        body = r.json()
        assert "detail" in body
        assert "cursor" in body["detail"].lower() or "iso" in body["detail"].lower()

    def test_future_cursor_returns_empty(self, user_a, conversation):
        r = requests.get(f"{API}/conversations/{conversation}/messages/since",
                         headers=_auth_headers(user_a["token"]),
                         params={"cursor": "2099-01-01T00:00:00+00:00"},
                         timeout=15)
        assert r.status_code == 200, r.text
        assert r.json() == []

    def test_old_cursor_returns_all_existing_messages(self, user_a, conversation):
        """After conv creation an initial agent seed message may exist;
        with a very old cursor /since must return >= what /messages returns."""
        full = requests.get(f"{API}/conversations/{conversation}/messages",
                            headers=_auth_headers(user_a["token"]),
                            timeout=15)
        assert full.status_code == 200, full.text
        full_msgs = full.json()
        since = requests.get(f"{API}/conversations/{conversation}/messages/since",
                             headers=_auth_headers(user_a["token"]),
                             params={"cursor": "2000-01-01T00:00:00+00:00"},
                             timeout=15)
        assert since.status_code == 200, since.text
        since_msgs = since.json()
        full_ids = {m["id"] for m in full_msgs if m.get("id")}
        since_ids = {m["id"] for m in since_msgs if m.get("id")}
        assert full_ids.issubset(since_ids), \
            f"/since with old cursor missing messages — full={full_ids} since={since_ids}"


class TestAuthGating:
    def test_non_team_member_gets_403(self, user_c, conversation):
        r = requests.get(f"{API}/conversations/{conversation}/messages/since",
                         headers=_auth_headers(user_c["token"]),
                         params={"cursor": "2000-01-01T00:00:00+00:00"},
                         timeout=15)
        assert r.status_code in (403, 404), \
            f"expected 403 (or 404 if conv hidden), got {r.status_code}: {r.text}"

    def test_unauthenticated_request_rejected(self, conversation):
        r = requests.get(f"{API}/conversations/{conversation}/messages/since",
                         params={"cursor": "2000-01-01T00:00:00+00:00"},
                         timeout=15)
        assert r.status_code in (401, 403), \
            f"expected 401/403, got {r.status_code}: {r.text}"


class TestParityWithMessages:
    def test_feedback_fields_present(self, user_a, conversation):
        # Ensure at least one message exists by posting one (idempotent
        # for the test — a duplicate user message is harmless).
        full = requests.get(f"{API}/conversations/{conversation}/messages",
                            headers=_auth_headers(user_a["token"]),
                            timeout=15)
        assert full.status_code == 200
        if not full.json():
            post = requests.post(f"{API}/conversations/{conversation}/messages",
                                 headers=_auth_headers(user_a["token"]),
                                 json={"text": "parity probe"},
                                 timeout=120)
            assert post.status_code == 200, post.text
        r = requests.get(f"{API}/conversations/{conversation}/messages/since",
                         headers=_auth_headers(user_a["token"]),
                         params={"cursor": "2000-01-01T00:00:00+00:00"},
                         timeout=15)
        assert r.status_code == 200
        msgs = r.json()
        assert msgs, "expected /since to return existing messages"
        for m in msgs:
            assert "up_count" in m, f"missing up_count: {m.keys()}"
            assert "down_count" in m
            assert "my_feedback" in m
            assert isinstance(m["up_count"], int)
            assert isinstance(m["down_count"], int)

    def test_no_mongo_objectid_leakage(self, user_a, conversation):
        r = requests.get(f"{API}/conversations/{conversation}/messages/since",
                         headers=_auth_headers(user_a["token"]),
                         params={"cursor": "2000-01-01T00:00:00+00:00"},
                         timeout=15)
        assert r.status_code == 200
        for m in r.json():
            assert "_id" not in m, f"raw mongo _id leaked: {m}"


class TestRegressionFullFetch:
    def test_messages_endpoint_still_works(self, user_a, conversation):
        r = requests.get(f"{API}/conversations/{conversation}/messages",
                         headers=_auth_headers(user_a["token"]),
                         timeout=15)
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), list)


class TestMultiUserLiveUpdate:
    """The actual product win — user B sees user A's message via /since."""

    def test_b_sees_a_new_message_via_since(self, user_a, user_b, conversation):
        # Step 1: B loads conv to get baseline cursor (latest timestamp)
        baseline = requests.get(f"{API}/conversations/{conversation}/messages",
                                headers=_auth_headers(user_b["token"]),
                                timeout=15)
        assert baseline.status_code == 200, f"B can't read conv: {baseline.status_code} {baseline.text}"
        baseline_msgs = baseline.json()
        if baseline_msgs:
            cursor = max((m.get("ready_at") or m.get("timestamp") or "")
                         for m in baseline_msgs)
        else:
            cursor = "2000-01-01T00:00:00+00:00"
        assert cursor

        # Step 2: A posts a new message.  Send-message triggers an LLM call;
        # to keep the test fast & deterministic we don't wait for the agent
        # reply — we only need at least the user message to appear on /since.
        marker = f"TEST_marker_{uuid.uuid4().hex[:8]}"
        post = requests.post(f"{API}/conversations/{conversation}/messages",
                             headers=_auth_headers(user_a["token"]),
                             json={"text": f"hello team {marker}"},
                             timeout=120)
        assert post.status_code == 200, f"send: {post.status_code} {post.text}"

        # Step 3: B polls /since with their old cursor — must see A's msg.
        # Allow a beat for the insert to land + (optional) agent reply.
        seen_user = False
        last_resp = None
        for _ in range(6):
            time.sleep(2)
            r = requests.get(f"{API}/conversations/{conversation}/messages/since",
                             headers=_auth_headers(user_b["token"]),
                             params={"cursor": cursor},
                             timeout=15)
            assert r.status_code == 200, r.text
            last_resp = r.json()
            for m in last_resp:
                if marker in (m.get("text") or ""):
                    seen_user = True
                    break
            if seen_user:
                break
        assert seen_user, (
            f"B did not see A's marker message via /since. "
            f"cursor={cursor} last_response={last_resp}"
        )


class TestReadyAtBranch:
    """The $or {ready_at: $gt} branch — exercised by patching a message
    in-place in Mongo so we don't need a real media generation roundtrip."""

    def test_ready_at_branch_picked_up(self, user_a, conversation):
        # Post a message so we have something to patch.
        post = requests.post(f"{API}/conversations/{conversation}/messages",
                             headers=_auth_headers(user_a["token"]),
                             json={"text": f"ready_at probe {uuid.uuid4().hex[:6]}"},
                             timeout=120)
        assert post.status_code == 200, post.text

        # Snapshot current state with a wide cursor.
        all_now = requests.get(f"{API}/conversations/{conversation}/messages",
                               headers=_auth_headers(user_a["token"]),
                               timeout=15)
        assert all_now.status_code == 200
        msgs = all_now.json()
        assert msgs, "no messages to test ready_at branch against"

        # Cursor strictly newer than every timestamp → empty without ready_at
        latest_ts = max((m.get("timestamp") or "") for m in msgs)
        # bump by 1 second by mutating the trailing seconds string;
        # simplest reliable bump: use the latest + " " is invalid, so we
        # just use latest_ts itself which is the *equality* boundary;
        # $gt means strictly greater, so the same value returns empty.
        r = requests.get(f"{API}/conversations/{conversation}/messages/since",
                         headers=_auth_headers(user_a["token"]),
                         params={"cursor": latest_ts},
                         timeout=15)
        assert r.status_code == 200
        # Now bump ready_at on the most-recent message directly in Mongo
        # to a future point, then re-poll with cursor=latest_ts.
        try:
            import asyncio
            from datetime import datetime, timezone, timedelta
            from motor.motor_asyncio import AsyncIOMotorClient
        except Exception as exc:
            pytest.skip(f"motor unavailable for direct DB poke: {exc}")
        mongo_url = os.environ.get("MONGO_URL")
        db_name = os.environ.get("DB_NAME")
        if not (mongo_url and db_name):
            pytest.skip("MONGO_URL/DB_NAME not set in test env")
        target_id = msgs[-1]["id"]
        future = datetime.now(timezone.utc) + timedelta(seconds=5)

        async def _patch():
            client = AsyncIOMotorClient(mongo_url)
            try:
                await client[db_name].messages.update_one(
                    {"id": target_id},
                    {"$set": {"ready_at": future}},
                )
            finally:
                client.close()

        asyncio.run(_patch())

        # Now /since with cursor = pre-patch latest_ts should include the
        # patched message via the ready_at branch.
        r2 = requests.get(f"{API}/conversations/{conversation}/messages/since",
                          headers=_auth_headers(user_a["token"]),
                          params={"cursor": latest_ts},
                          timeout=15)
        assert r2.status_code == 200, r2.text
        ids = [m["id"] for m in r2.json()]
        assert target_id in ids, (
            f"ready_at branch failed to surface patched message. "
            f"cursor={latest_ts} returned ids={ids}"
        )
