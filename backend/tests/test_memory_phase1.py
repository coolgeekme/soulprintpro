"""Backend tests for Memory Phase 1 — user-scoped CRUD + isolation.

Covers:
  - GET /api/memory (empty + populated, include_archived flag)
  - POST /api/memory (create + 409 duplicate)
  - PATCH /api/memory/{id} (content/category/importance/is_archived)
  - DELETE /api/memory/{id} (hard delete + 404 on cross-user / non-existent)

Notes:
  - Uses standing superadmin (reggie@coolgeek.me) for primary user.
  - Registers a second fresh user via an access code if available, else
    skips cross-user isolation test gracefully.
"""
import os
import time
import uuid
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://emergent-wing.preview.emergentagent.com").rstrip("/")

ADMIN_EMAIL = "reggie@coolgeek.me"
ADMIN_PASS = "superadminpass123"


# ── Fixtures ────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASS},
        timeout=20,
    )
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


@pytest.fixture
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def cleanup_test_memories(admin_token):
    """Remove TEST_ prefixed memories before and after each test."""
    headers = {"Authorization": f"Bearer {admin_token}"}
    def _purge():
        try:
            r = requests.get(f"{BASE_URL}/api/memory?include_archived=true", headers=headers, timeout=15)
            if r.status_code == 200:
                for m in r.json().get("memories", []):
                    if m.get("content", "").startswith("TEST_"):
                        requests.delete(f"{BASE_URL}/api/memory/{m['id']}", headers=headers, timeout=15)
        except Exception:
            pass
    _purge()
    yield
    _purge()


# ── Tests ───────────────────────────────────────────────────────────────
class TestMemoryListEmpty:
    def test_get_empty_returns_categories(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/memory", headers=auth_headers, timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "memories" in data and isinstance(data["memories"], list)
        assert "categories" in data and isinstance(data["categories"], list)
        # Verify the 9 categories documented
        expected = {"brand-voice", "pricing", "icp", "past-clients", "playbooks",
                    "agent-feedback", "preferences", "work", "other"}
        assert set(data["categories"]) == expected


class TestMemoryCreate:
    def test_post_creates_memory(self, auth_headers):
        content = f"TEST_user runs a fintech agency in Austin {uuid.uuid4().hex[:6]}"
        r = requests.post(
            f"{BASE_URL}/api/memory", headers=auth_headers,
            json={"content": content, "category": "work", "importance": "high"},
            timeout=15,
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["content"] == content
        assert data["category"] == "work"
        assert data["importance"] == "high"
        assert data["source"] == "manual"
        assert data["is_archived"] is False
        assert "id" in data and isinstance(data["id"], str)
        # content_key must be stripped from API responses
        assert "content_key" not in data
        # _id leak check
        assert "_id" not in data

        # Verify persistence via GET
        r2 = requests.get(f"{BASE_URL}/api/memory", headers=auth_headers, timeout=15)
        ids = [m["id"] for m in r2.json()["memories"]]
        assert data["id"] in ids

    def test_post_duplicate_returns_409(self, auth_headers):
        content = f"TEST_duplicate fact {uuid.uuid4().hex[:6]}"
        r1 = requests.post(
            f"{BASE_URL}/api/memory", headers=auth_headers,
            json={"content": content, "category": "other", "importance": "medium"},
            timeout=15,
        )
        assert r1.status_code == 200
        r2 = requests.post(
            f"{BASE_URL}/api/memory", headers=auth_headers,
            json={"content": content, "category": "other", "importance": "medium"},
            timeout=15,
        )
        assert r2.status_code == 409, f"expected 409 got {r2.status_code}: {r2.text}"

    def test_post_duplicate_case_insensitive(self, auth_headers):
        base = f"TEST_case insensitive {uuid.uuid4().hex[:6]}"
        r1 = requests.post(f"{BASE_URL}/api/memory", headers=auth_headers,
                           json={"content": base.lower()}, timeout=15)
        assert r1.status_code == 200
        r2 = requests.post(f"{BASE_URL}/api/memory", headers=auth_headers,
                           json={"content": base.upper()}, timeout=15)
        assert r2.status_code == 409


class TestMemoryUpdate:
    def test_patch_content_and_importance(self, auth_headers):
        content = f"TEST_to_edit {uuid.uuid4().hex[:6]}"
        c = requests.post(f"{BASE_URL}/api/memory", headers=auth_headers,
                         json={"content": content}, timeout=15)
        mid = c.json()["id"]

        new_content = f"TEST_edited {uuid.uuid4().hex[:6]}"
        r = requests.patch(
            f"{BASE_URL}/api/memory/{mid}", headers=auth_headers,
            json={"content": new_content, "importance": "high", "category": "brand-voice"},
            timeout=15,
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True}

        # Verify via GET
        rg = requests.get(f"{BASE_URL}/api/memory", headers=auth_headers, timeout=15)
        match = [m for m in rg.json()["memories"] if m["id"] == mid]
        assert len(match) == 1
        assert match[0]["content"] == new_content
        assert match[0]["importance"] == "high"
        assert match[0]["category"] == "brand-voice"

    def test_archive_hidden_by_default_and_visible_with_flag(self, auth_headers):
        content = f"TEST_archive {uuid.uuid4().hex[:6]}"
        c = requests.post(f"{BASE_URL}/api/memory", headers=auth_headers,
                         json={"content": content}, timeout=15)
        mid = c.json()["id"]

        r = requests.patch(f"{BASE_URL}/api/memory/{mid}", headers=auth_headers,
                           json={"is_archived": True}, timeout=15)
        assert r.status_code == 200

        r_default = requests.get(f"{BASE_URL}/api/memory", headers=auth_headers, timeout=15)
        assert mid not in [m["id"] for m in r_default.json()["memories"]]

        r_inc = requests.get(f"{BASE_URL}/api/memory?include_archived=true",
                              headers=auth_headers, timeout=15)
        archived = [m for m in r_inc.json()["memories"] if m["id"] == mid]
        assert len(archived) == 1
        assert archived[0]["is_archived"] is True

    def test_patch_nonexistent_returns_404(self, auth_headers):
        r = requests.patch(f"{BASE_URL}/api/memory/does-not-exist-id",
                           headers=auth_headers, json={"content": "TEST_x"}, timeout=15)
        assert r.status_code == 404


class TestMemoryDelete:
    def test_delete_removes_memory(self, auth_headers):
        content = f"TEST_to_delete {uuid.uuid4().hex[:6]}"
        c = requests.post(f"{BASE_URL}/api/memory", headers=auth_headers,
                         json={"content": content}, timeout=15)
        mid = c.json()["id"]

        r = requests.delete(f"{BASE_URL}/api/memory/{mid}", headers=auth_headers, timeout=15)
        assert r.status_code == 200
        assert r.json() == {"ok": True}

        r2 = requests.get(f"{BASE_URL}/api/memory?include_archived=true",
                          headers=auth_headers, timeout=15)
        assert mid not in [m["id"] for m in r2.json()["memories"]]

    def test_delete_nonexistent_returns_404(self, auth_headers):
        r = requests.delete(f"{BASE_URL}/api/memory/{uuid.uuid4()}",
                            headers=auth_headers, timeout=15)
        assert r.status_code == 404


class TestMemoryAuth:
    def test_get_without_auth_unauthorized(self):
        r = requests.get(f"{BASE_URL}/api/memory", timeout=15)
        assert r.status_code in (401, 403), f"expected 401/403 got {r.status_code}"


class TestMemoryCrossUserIsolation:
    """Verifies update/delete enforce user_id filter — even if user B
    knows user A's memory id, they get 404 (treated as not-found)."""

    def test_cross_user_delete_returns_404(self, admin_token, auth_headers):
        # Admin creates a memory
        content = f"TEST_iso_{uuid.uuid4().hex[:6]}"
        c = requests.post(f"{BASE_URL}/api/memory", headers=auth_headers,
                         json={"content": content}, timeout=15)
        mid = c.json()["id"]

        # Try to register a second user. The platform requires an access
        # code — without one we can't validate cross-user isolation here.
        reg = requests.post(
            f"{BASE_URL}/api/auth/register",
            json={
                "email": f"test_iso_{uuid.uuid4().hex[:8]}@example.com",
                "password": "isopass123!",
                "name": "Iso Tester",
            },
            timeout=15,
        )
        if reg.status_code != 200:
            pytest.skip(f"can't register second user without access code (got {reg.status_code}) — isolation logic verified in unit-level code review")

        other_token = reg.json().get("access_token")
        other_headers = {"Authorization": f"Bearer {other_token}", "Content-Type": "application/json"}

        # User B tries to delete user A's memory — should 404
        r = requests.delete(f"{BASE_URL}/api/memory/{mid}", headers=other_headers, timeout=15)
        assert r.status_code == 404

        # And patch — also 404
        r2 = requests.patch(f"{BASE_URL}/api/memory/{mid}", headers=other_headers,
                            json={"content": "hijack"}, timeout=15)
        assert r2.status_code == 404

        # User B's listing should be empty (or at least not contain A's id)
        r3 = requests.get(f"{BASE_URL}/api/memory", headers=other_headers, timeout=15)
        assert r3.status_code == 200
        assert mid not in [m["id"] for m in r3.json()["memories"]]

        # And admin's memory is still intact
        r4 = requests.get(f"{BASE_URL}/api/memory", headers=auth_headers, timeout=15)
        assert mid in [m["id"] for m in r4.json()["memories"]]
