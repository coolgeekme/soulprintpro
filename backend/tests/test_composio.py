"""Backend tests for Composio integration (per-org tool connection mgmt).

Covers:
- Auth-config bootstrap (5 toolkits present in Mongo at boot)
- GET /toolkits — auth gates + payload shape
- POST /connect — happy path (returns shaped redirect_url), bad toolkit -> 400
- After connect, row in db.composio_connections with status=INITIATED,
  composio_user_id="{org}:{user}", and shows up in /toolkits.connections.
- DELETE /connections/{id} — happy path + cross-(org,user) 404
- POST /connections/{id}/refresh — happy path for own connection
- Per-org isolation (different orgs → different composio_user_id)
- Idempotency of ensure_auth_configs (calling twice stays at 5 rows)
"""
import os
import asyncio
import requests
import pytest

def _read_env_file(path, key):
    try:
        with open(path) as f:
            for line in f:
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        return None

BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or _read_env_file("/app/frontend/.env", "REACT_APP_BACKEND_URL") or "").rstrip("/")
assert BASE_URL, "REACT_APP_BACKEND_URL not configured"
SUPERADMIN_EMAIL = "reggie@coolgeek.me"
SUPERADMIN_PASSWORD = "superadminpass123"
REGGIE_ORG_ID = "69f6a9f568b5974dfcc2fcf8"

TOOLKIT_SLUGS = {"gmail", "googlecalendar", "slack", "notion", "hubspot"}


# ── Fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def s():
    return requests.Session()


@pytest.fixture(scope="module")
def auth(s):
    r = s.post(f"{BASE_URL}/api/auth/login", json={
        "email": SUPERADMIN_EMAIL, "password": SUPERADMIN_PASSWORD,
    })
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    data = r.json()
    token = data["access_token"]
    s.headers.update({"Authorization": f"Bearer {token}"})
    return {"user_id": data["id"], "token": token}


@pytest.fixture(scope="module")
def db():
    """Placeholder — actual motor client is created per-call inside _with_db
    to avoid 'Event loop is closed' when pytest creates fresh loops per test."""
    return None


def _run(coro):
    """Run a coroutine. Creates a fresh motor client inside so each run uses
    its own event loop (motor binds to the loop it was constructed on)."""
    return asyncio.run(coro)


def _with_db(async_fn):
    """Wrap an async function so it gets a fresh motor client bound to the
    current event loop. Returns the result of async_fn(db)."""
    async def runner():
        from motor.motor_asyncio import AsyncIOMotorClient
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        try:
            return await async_fn(client[os.environ["DB_NAME"]])
        finally:
            client.close()
    return asyncio.run(runner())


# ── 1. Auth gating on /toolkits ─────────────────────────────────────────────
class TestAuthGating:
    def test_unauthenticated_returns_401(self):
        r = requests.get(f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/composio/toolkits")
        assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"

    def test_non_member_returns_403(self, s, auth, db):
        # Use the standing autopost test account — it lives in a different org
        # (Autopost Org), so trying to read Reggie Lab's composio toolkits
        # should hit the org-membership gate and return 403.
        login = requests.post(f"{BASE_URL}/api/auth/login", json={
            "email": "autopost-test-1777661963@example.com",
            "password": "testtest123",
        })
        if login.status_code != 200:
            pytest.skip(f"autopost user login unavailable ({login.status_code})")
        stranger_token = login.json()["access_token"]
        r = requests.get(
            f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/composio/toolkits",
            headers={"Authorization": f"Bearer {stranger_token}"},
        )
        assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"


# ── 2. List toolkits — payload shape ────────────────────────────────────────
class TestListToolkits:
    def test_returns_available_and_5_toolkits(self, s, auth):
        r = s.get(f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/composio/toolkits")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["available"] is True
        slugs = {t["slug"] for t in data["toolkits"]}
        assert slugs == TOOLKIT_SLUGS, f"toolkits mismatch: {slugs}"
        assert isinstance(data["connections"], list)


# ── 3. Auth-configs bootstrapped on startup ─────────────────────────────────
class TestAuthConfigsBootstrap:
    def test_five_auth_configs_in_db(self, db):
        async def _q(d):
            return await d.composio_auth_configs.find({}, {"_id": 0}).to_list(20)
        rows = _with_db(_q)
        slugs = {r["toolkit"] for r in rows}
        assert TOOLKIT_SLUGS.issubset(slugs), f"missing toolkits: {TOOLKIT_SLUGS - slugs}"
        for r in rows:
            assert r.get("auth_config_id"), f"missing auth_config_id for {r['toolkit']}"

    def test_ensure_auth_configs_is_idempotent(self, db):
        """Calling ensure_auth_configs again must not duplicate rows."""
        import sys
        sys.path.insert(0, "/app/backend")
        from composio_integration import ensure_auth_configs

        async def _check(d):
            before = await d.composio_auth_configs.count_documents({})
            await ensure_auth_configs(d)
            after = await d.composio_auth_configs.count_documents({})
            return before, after
        before, after = _with_db(_check)
        assert before == after, f"row count changed {before} -> {after}"
        assert after >= 5


# ── 4. Connect + persistence ────────────────────────────────────────────────
class TestConnect:
    def test_bad_toolkit_returns_400(self, s, auth):
        r = s.post(
            f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/composio/connect",
            json={"toolkit": "nonexistent"},
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"

    def test_gmail_connect_returns_shaped_redirect_and_persists(self, s, auth, db):
        r = s.post(
            f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/composio/connect",
            json={"toolkit": "gmail"},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert "connection_id" in data and data["connection_id"]
        assert data["status"] in ("INITIATED", "INITIALIZING", "ACTIVE", "PENDING")
        assert data["redirect_url"].startswith("https://"), data["redirect_url"]
        # Composio hosted link
        assert "composio" in data["redirect_url"].lower() or "backend.composio" in data["redirect_url"].lower()
        # Stash for later tests
        TestConnect.conn_id = data["connection_id"]

        # Row persisted with proper composio_user_id
        async def _q(d):
            return await d.composio_connections.find_one(
                {"connection_id": data["connection_id"]}, {"_id": 0}
            )
        row = _with_db(_q)
        assert row is not None, "no row persisted in db.composio_connections"
        assert row["org_id"] == REGGIE_ORG_ID
        assert row["user_id"] == auth["user_id"]
        assert row["composio_user_id"] == f"{REGGIE_ORG_ID}:{auth['user_id']}"
        assert row["toolkit"] == "gmail"
        assert row["status"] in ("INITIATED", "INITIALIZING", "PENDING", "ACTIVE")

    def test_connection_shows_up_in_list_with_hydrated_name(self, s, auth):
        r = s.get(f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/composio/toolkits")
        assert r.status_code == 200, r.text
        conns = r.json()["connections"]
        gmail = next((c for c in conns if c["toolkit"] == "gmail"), None)
        assert gmail is not None, f"gmail connection not listed: {conns}"
        assert gmail.get("toolkit_name") == "Gmail"


# ── 5. Refresh + Disconnect ─────────────────────────────────────────────────
class TestRefreshAndDisconnect:
    def test_refresh_own_connection(self, s, auth):
        conn_id = getattr(TestConnect, "conn_id", None)
        if not conn_id:
            pytest.skip("no connection from previous test")
        r = s.post(f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/composio/connections/{conn_id}/refresh")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["connection_id"] == conn_id
        assert "status" in data

    def test_disconnect_cross_user_returns_404(self, s, auth):
        """A connection ID that doesn't belong to caller (org,user) -> 404."""
        bogus = "fakenanoid_xxxxxxxxxx"
        r = s.delete(f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/composio/connections/{bogus}")
        assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text}"

    def test_disconnect_own_connection_removes_row(self, s, auth, db):
        conn_id = getattr(TestConnect, "conn_id", None)
        if not conn_id:
            pytest.skip("no connection from previous test")
        r = s.delete(f"{BASE_URL}/api/orgs/{REGGIE_ORG_ID}/composio/connections/{conn_id}")
        assert r.status_code == 200, r.text
        assert r.json().get("status") == "disconnected"

        async def _q(d):
            return await d.composio_connections.find_one({"connection_id": conn_id})
        row = _with_db(_q)
        assert row is None, "row not removed from db.composio_connections"


# ── 6. Per-org isolation ────────────────────────────────────────────────────
class TestPerOrgIsolation:
    def test_composio_user_id_differs_per_org(self):
        import sys
        sys.path.insert(0, "/app/backend")
        from composio_integration import composio_user_id
        u = "user123"
        assert composio_user_id("orgA", u) == f"orgA:{u}"
        assert composio_user_id("orgB", u) == f"orgB:{u}"
        assert composio_user_id("orgA", u) != composio_user_id("orgB", u)
