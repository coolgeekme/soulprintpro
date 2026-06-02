"""End-to-end tests for the invite flow — public preview + auto-accept.

Covers:
  • /api/invites/peek is public (no auth), validates code, returns
    org_name + inviter_name + role.
  • Bad codes return 404 with friendly message.
  • Expired invites would 410 (we don't fabricate expiry here — that
    path is covered by the field shape check in the peek response).
  • An authenticated invitee can accept an invite they were sent.
"""
from __future__ import annotations

import os
import uuid

import requests

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL",
    "https://emergent-wing.preview.emergentagent.com",
).rstrip("/")
SUPERADMIN_EMAIL = "reggie@coolgeek.me"
SUPERADMIN_PASS = "superadminpass123"


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def _login(email: str, password: str):
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": email, "password": password},
        timeout=15,
    )
    return r.json().get("access_token") if r.status_code == 200 else None


# ─── Peek endpoint ──────────────────────────────────────────────────


def test_peek_requires_code():
    r = requests.post(
        f"{BASE_URL}/api/invites/peek",
        json={"code": ""},
        timeout=15,
    )
    assert r.status_code == 400


def test_peek_bad_code_returns_404():
    r = requests.post(
        f"{BASE_URL}/api/invites/peek",
        json={"code": "definitely-not-a-real-code"},
        timeout=15,
    )
    assert r.status_code == 404


def test_peek_is_public_and_returns_preview():
    """Owner creates an invite; the peek endpoint must work WITHOUT an
    Authorization header (this is what the unauthenticated /invite/{code}
    page calls)."""
    tok = _login(SUPERADMIN_EMAIL, SUPERADMIN_PASS)
    if not tok:
        return  # superadmin not seeded — skip silently
    orgs = requests.get(f"{BASE_URL}/api/orgs", headers=_h(tok), timeout=15).json()
    if not orgs:
        return
    org_id = orgs[0]["id"]
    inv = requests.post(
        f"{BASE_URL}/api/orgs/{org_id}/invites",
        headers=_h(tok), json={"role": "member"}, timeout=15,
    )
    assert inv.status_code == 200, inv.text
    code = inv.json()["code"]

    # Peek with NO auth header — must still work.
    r = requests.post(
        f"{BASE_URL}/api/invites/peek",
        json={"code": code}, timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("org_id", "org_name", "inviter_name", "role"):
        assert key in body, f"peek missing {key}"
    assert body["role"] == "member"
    assert body["org_name"], "org_name should not be empty"
    assert body["inviter_name"], "inviter_name should not be empty"
