"""Backend tests for Memory ORG ISOLATION (Phase 2 of memory roadmap).

Validates STRICT per-org isolation:
  - POST /api/memory requires org_id (422 without it)
  - POST with non-member org → 403
  - POST same content to two orgs → both succeed (per-org dedupe)
  - POST duplicate to same org → 409
  - GET ?org_id=A → only org-A memories
  - GET (no org_id) → only untagged
  - GET /api/memory/untagged → untagged only
  - PATCH org_id migrates a memory between orgs
  - PATCH with non-member org → 403

Uses reggie@coolgeek.me who has 2 orgs (Reggie Lab + SG Test).
"""
import os
import uuid
import pytest
import requests

BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or "https://emergent-wing.preview.emergentagent.com").rstrip("/")
ADMIN_EMAIL = "reggie@coolgeek.me"
ADMIN_PASS = "superadminpass123"


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"email": ADMIN_EMAIL, "password": ADMIN_PASS},
                      timeout=20)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def H(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def orgs(H):
    r = requests.get(f"{BASE_URL}/api/orgs", headers=H, timeout=15)
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list) and len(data) >= 2, f"need 2 orgs, got: {data}"
    return data


@pytest.fixture(scope="module")
def org_a(orgs):
    return orgs[0]["id"]


@pytest.fixture(scope="module")
def org_b(orgs):
    return orgs[1]["id"]


@pytest.fixture(autouse=True)
def cleanup(H, orgs):
    """Purge TEST_ prefixed memories before and after each test (all orgs + untagged)."""
    def _purge():
        try:
            for o in orgs:
                r = requests.get(f"{BASE_URL}/api/memory",
                                 params={"org_id": o["id"], "include_archived": "true"},
                                 headers=H, timeout=15)
                if r.status_code == 200:
                    for m in r.json().get("memories", []):
                        if m.get("content", "").startswith("TEST_"):
                            requests.delete(f"{BASE_URL}/api/memory/{m['id']}", headers=H, timeout=15)
            ru = requests.get(f"{BASE_URL}/api/memory/untagged", headers=H, timeout=15)
            if ru.status_code == 200:
                for m in ru.json().get("memories", []):
                    if m.get("content", "").startswith("TEST_"):
                        requests.delete(f"{BASE_URL}/api/memory/{m['id']}", headers=H, timeout=15)
        except Exception:
            pass
    _purge()
    yield
    _purge()


# ── POST validation ─────────────────────────────────────────────────────
class TestPostValidation:
    def test_post_without_org_id_returns_422(self, H):
        r = requests.post(f"{BASE_URL}/api/memory", headers=H,
                          json={"content": "TEST_no_org_id"}, timeout=15)
        assert r.status_code == 422, f"expected 422 got {r.status_code}: {r.text}"

    def test_post_with_non_member_org_returns_403(self, H):
        # Use a syntactically-valid ObjectId that user is NOT a member of
        fake_org = "0123456789abcdef01234567"
        r = requests.post(f"{BASE_URL}/api/memory", headers=H,
                          json={"content": f"TEST_unauth {uuid.uuid4().hex[:6]}",
                                "org_id": fake_org}, timeout=15)
        assert r.status_code == 403, f"expected 403 got {r.status_code}: {r.text}"

    def test_post_with_invalid_org_id_returns_400(self, H):
        r = requests.post(f"{BASE_URL}/api/memory", headers=H,
                          json={"content": "TEST_bad", "org_id": "not-an-objectid"},
                          timeout=15)
        assert r.status_code == 400

    def test_post_with_valid_org_returns_200_and_stamps_org(self, H, org_a):
        content = f"TEST_valid_a {uuid.uuid4().hex[:6]}"
        r = requests.post(f"{BASE_URL}/api/memory", headers=H,
                          json={"content": content, "org_id": org_a}, timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["org_id"] == org_a
        assert d["content"] == content


# ── Per-org dedupe scoping ──────────────────────────────────────────────
class TestPerOrgDedupe:
    def test_same_content_two_orgs_both_succeed(self, H, org_a, org_b):
        content = f"TEST_same_two_orgs {uuid.uuid4().hex[:6]}"
        r1 = requests.post(f"{BASE_URL}/api/memory", headers=H,
                           json={"content": content, "org_id": org_a}, timeout=15)
        assert r1.status_code == 200, r1.text
        r2 = requests.post(f"{BASE_URL}/api/memory", headers=H,
                           json={"content": content, "org_id": org_b}, timeout=15)
        assert r2.status_code == 200, r2.text
        assert r1.json()["id"] != r2.json()["id"]
        assert r1.json()["org_id"] == org_a
        assert r2.json()["org_id"] == org_b

    def test_duplicate_same_org_returns_409(self, H, org_a):
        content = f"TEST_dup_same_org {uuid.uuid4().hex[:6]}"
        r1 = requests.post(f"{BASE_URL}/api/memory", headers=H,
                           json={"content": content, "org_id": org_a}, timeout=15)
        assert r1.status_code == 200
        r2 = requests.post(f"{BASE_URL}/api/memory", headers=H,
                           json={"content": content, "org_id": org_a}, timeout=15)
        assert r2.status_code == 409


# ── GET scoping ─────────────────────────────────────────────────────────
class TestGetScoping:
    def test_get_with_org_returns_only_that_org(self, H, org_a, org_b):
        ca = f"TEST_only_a {uuid.uuid4().hex[:6]}"
        cb = f"TEST_only_b {uuid.uuid4().hex[:6]}"
        requests.post(f"{BASE_URL}/api/memory", headers=H,
                      json={"content": ca, "org_id": org_a}, timeout=15)
        requests.post(f"{BASE_URL}/api/memory", headers=H,
                      json={"content": cb, "org_id": org_b}, timeout=15)

        ra = requests.get(f"{BASE_URL}/api/memory", params={"org_id": org_a},
                          headers=H, timeout=15)
        assert ra.status_code == 200
        contents_a = [m["content"] for m in ra.json()["memories"]]
        assert ca in contents_a
        assert cb not in contents_a
        # All returned should have org_id == org_a
        for m in ra.json()["memories"]:
            assert m["org_id"] == org_a, f"leak: {m}"

    def test_get_no_org_returns_untagged_only(self, H, org_a):
        # Create a tagged memory
        ctag = f"TEST_tagged_should_not_appear {uuid.uuid4().hex[:6]}"
        requests.post(f"{BASE_URL}/api/memory", headers=H,
                      json={"content": ctag, "org_id": org_a}, timeout=15)
        # GET without org_id
        r = requests.get(f"{BASE_URL}/api/memory", headers=H, timeout=15)
        assert r.status_code == 200
        for m in r.json()["memories"]:
            assert m.get("org_id") is None, f"non-untagged leaked into no-org GET: {m}"

    def test_untagged_endpoint_returns_only_untagged(self, H):
        r = requests.get(f"{BASE_URL}/api/memory/untagged", headers=H, timeout=15)
        assert r.status_code == 200
        for m in r.json()["memories"]:
            assert m.get("org_id") is None

    def test_get_non_member_org_returns_403(self, H):
        r = requests.get(f"{BASE_URL}/api/memory",
                         params={"org_id": "0123456789abcdef01234567"},
                         headers=H, timeout=15)
        assert r.status_code == 403


# ── PATCH migration ─────────────────────────────────────────────────────
class TestPatchMigration:
    def test_patch_org_id_migrates(self, H, org_a, org_b):
        content = f"TEST_to_migrate {uuid.uuid4().hex[:6]}"
        c = requests.post(f"{BASE_URL}/api/memory", headers=H,
                          json={"content": content, "org_id": org_a}, timeout=15)
        assert c.status_code == 200
        mid = c.json()["id"]

        # Migrate from A → B
        p = requests.patch(f"{BASE_URL}/api/memory/{mid}", headers=H,
                           json={"org_id": org_b}, timeout=15)
        assert p.status_code == 200, p.text

        # GET ?org_id=A should not contain it
        ra = requests.get(f"{BASE_URL}/api/memory", params={"org_id": org_a},
                          headers=H, timeout=15)
        assert mid not in [m["id"] for m in ra.json()["memories"]]

        # GET ?org_id=B should contain it
        rb = requests.get(f"{BASE_URL}/api/memory", params={"org_id": org_b},
                          headers=H, timeout=15)
        ids_b = [m["id"] for m in rb.json()["memories"]]
        assert mid in ids_b, f"migrated memory not found in org_b"

    def test_patch_non_member_org_returns_403(self, H, org_a):
        content = f"TEST_patch_unauth {uuid.uuid4().hex[:6]}"
        c = requests.post(f"{BASE_URL}/api/memory", headers=H,
                          json={"content": content, "org_id": org_a}, timeout=15)
        mid = c.json()["id"]
        r = requests.patch(f"{BASE_URL}/api/memory/{mid}", headers=H,
                           json={"org_id": "0123456789abcdef01234567"}, timeout=15)
        assert r.status_code == 403


# ── Chat-loop injection isolation (the CRITICAL test) ───────────────────
class TestChatLoopOrgIsolation:
    """The whole point: a fact captured in Org A must NEVER appear in
    Org B's agent reply."""

    def _find_general_team(self, H, org_id):
        r = requests.get(f"{BASE_URL}/api/orgs/{org_id}/teams", headers=H, timeout=15)
        if r.status_code != 200:
            return None
        teams = r.json() if isinstance(r.json(), list) else r.json().get("teams", [])
        # Prefer is_general=True (the org's default team)
        for t in teams:
            if t.get("is_general"):
                return t["id"]
        for t in teams:
            if "general" in (t.get("name", "") or "").lower():
                return t["id"]
        return teams[0]["id"] if teams else None

    def test_fintech_fact_only_in_org_a(self, H, org_a, org_b):
        # Seed a distinctive fact in Org A only
        marker = f"testfintech{uuid.uuid4().hex[:8]}"
        secret = f"User runs a {marker} agency in Austin."
        c = requests.post(f"{BASE_URL}/api/memory", headers=H,
                          json={"content": secret, "org_id": org_a,
                                "category": "work", "importance": "high"}, timeout=15)
        assert c.status_code == 200, c.text
        mid = c.json()["id"]
        try:
            team_a = self._find_general_team(H, org_a)
            team_b = self._find_general_team(H, org_b)
            if not team_a or not team_b:
                pytest.skip(f"no team found in one org (a={team_a}, b={team_b})")

            def _ask(team_id):
                r = requests.post(f"{BASE_URL}/api/teams/{team_id}/start", headers=H,
                                  json={"text": "What kind of agency do I run? Mention any specifics you remember about my business."},
                                  timeout=120)
                assert r.status_code == 200, f"smart_start failed: {r.status_code} {r.text[:300]}"
                d = r.json()
                # The agent reply lives under agent_message.text (or similar)
                am = d.get("agent_message") or d.get("agent_reply") or {}
                if isinstance(am, dict):
                    return (am.get("text") or am.get("content") or "").lower()
                return str(am).lower()

            reply_a = _ask(team_a)
            reply_b = _ask(team_b)

            # Reply A should reference the secret marker OR "fintech"
            assert (marker in reply_a) or ("fintech" in reply_a), \
                f"Org A reply did NOT mention fintech/marker. Reply: {reply_a[:600]}"
            # Reply B must NOT
            assert (marker not in reply_b) and ("fintech" not in reply_b), \
                f"LEAK! Org B reply mentioned fintech/marker. Reply: {reply_b[:600]}"
        finally:
            requests.delete(f"{BASE_URL}/api/memory/{mid}", headers=H, timeout=15)
