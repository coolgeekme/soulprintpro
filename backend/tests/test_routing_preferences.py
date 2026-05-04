"""Regression for the per-type Dynamic Intelligence routing preferences.

Endpoints under test:
  • GET  /api/auth/me — must hydrate ``routing_preferences`` with all
    four keys defaulted to "auto" for users who have never set them.
  • PATCH /api/auth/me/preferences — must accept partial bodies (e.g.
    only `image`) and merge them onto the existing prefs without wiping
    the others. Round-trips back via /me.
  • The provider field must NOT appear in /api/media/models (we hide
    "kie" / "emergent" from end-users).
"""
import os
import uuid
import pytest
import pytest_asyncio
import httpx
from datetime import datetime, timezone


BACKEND_URL = "http://localhost:8001"


@pytest_asyncio.fixture
async def auth_token():
    """Register a fresh user and return their bearer token."""
    email = f"prefs-test-{uuid.uuid4().hex[:8]}@example.com"
    password = "preftesting123"
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=30) as client:
        r = await client.post("/api/auth/register", json={
            "email": email, "password": password, "name": "Prefs Test",
        })
        if r.status_code != 200:
            pytest.skip(f"registration unavailable: {r.text[:200]}")
        yield r.json()["access_token"]


@pytest.mark.asyncio
async def test_me_returns_default_routing_preferences(auth_token):
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=30) as client:
        r = await client.get("/api/auth/me",
                             headers={"Authorization": f"Bearer {auth_token}"})
    assert r.status_code == 200
    me = r.json()
    assert "routing_preferences" in me
    prefs = me["routing_preferences"]
    for k in ("chat", "image", "video", "music"):
        assert prefs.get(k) == "auto", f"default for {k} should be 'auto'"


@pytest.mark.asyncio
async def test_partial_patch_merges_without_wiping(auth_token):
    """PATCH /me/preferences with just one field must keep the others
    untouched (server merges via $set on dotted keys)."""
    h = {"Authorization": f"Bearer {auth_token}"}
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=30) as client:
        # Lock just video → kling-3
        r = await client.patch("/api/auth/me/preferences",
                               headers=h, json={"video": "kling-3"})
        assert r.status_code == 200, r.text
        prefs = r.json()["routing_preferences"]
        assert prefs["video"] == "kling-3"
        for k in ("chat", "image", "music"):
            assert prefs[k] == "auto"
        # Now lock image → flux-pro — video must stay locked
        r = await client.patch("/api/auth/me/preferences",
                               headers=h, json={"image": "flux-pro"})
        assert r.status_code == 200
        prefs = r.json()["routing_preferences"]
        assert prefs["image"] == "flux-pro"
        assert prefs["video"] == "kling-3"   # preserved
        assert prefs["chat"] == "auto"
        # Round-trip via /me
        r = await client.get("/api/auth/me", headers=h)
        prefs2 = r.json()["routing_preferences"]
        assert prefs2 == prefs


@pytest.mark.asyncio
async def test_patch_back_to_auto_clears_lock(auth_token):
    """Lock then revert → /me reflects the revert."""
    h = {"Authorization": f"Bearer {auth_token}"}
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=30) as client:
        await client.patch("/api/auth/me/preferences",
                           headers=h, json={"music": "suno"})
        await client.patch("/api/auth/me/preferences",
                           headers=h, json={"music": "auto"})
        r = await client.get("/api/auth/me", headers=h)
    assert r.status_code == 200
    assert r.json()["routing_preferences"]["music"] == "auto"


@pytest.mark.asyncio
async def test_media_models_no_provider_field(auth_token):
    """User-facing model list must NOT carry provider names — we want
    'Suno' or 'Veo 3.1', never 'kie' / 'emergent'."""
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=30) as client:
        r = await client.get("/api/media/models",
                             headers={"Authorization": f"Bearer {auth_token}"})
    assert r.status_code == 200
    rows = r.json()["models"]
    assert rows, "registry shouldn't be empty"
    for m in rows:
        assert "provider" not in m, f"row leaked provider: {m}"
        assert "key" in m and "label" in m and "type" in m
    # Sanity — coverage of the four content types we expose to users.
    types = {m["type"] for m in rows}
    assert {"chat", "image", "video", "music"}.issubset(types), (
        f"missing types in registry: {types}"
    )


@pytest.mark.asyncio
async def test_preferences_survive_logout_relogin(auth_token):
    """Preferences are stored on the user doc, so a fresh login on a
    new device (= new token) sees the same values."""
    h = {"Authorization": f"Bearer {auth_token}"}
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=30) as client:
        # Set prefs with token A
        await client.patch("/api/auth/me/preferences",
                           headers=h, json={"chat": "claude-sonnet-4-5",
                                            "video": "sora-2"})
        # Fetch the email so we can re-login
        me = (await client.get("/api/auth/me", headers=h)).json()
        email = me["email"]
        # Re-login (simulates fresh device or after logout)
        r = await client.post("/api/auth/login", json={
            "email": email, "password": "preftesting123",
        })
        assert r.status_code == 200
        new_token = r.json()["access_token"]
        # Read prefs through the NEW token — must be identical
        r2 = await client.get("/api/auth/me",
                              headers={"Authorization": f"Bearer {new_token}"})
        prefs = r2.json()["routing_preferences"]
        assert prefs["chat"] == "claude-sonnet-4-5"
        assert prefs["video"] == "sora-2"
        assert prefs["image"] == "auto"
        assert prefs["music"] == "auto"
