"""Integration tests for the addon checkout endpoint validation.

Focus areas (per review request):
  • /billing/summary returns addons block + addons_catalogue (auth)
  • /billing/addons/checkout body validation (422 on missing/short fields)
  • Unknown addon_id → 404
  • extra_seat plan restriction → 400 for non-spark users
  • 503 surfaced when STRIPE_API_KEY is the shadow/invalid value
"""
from __future__ import annotations

import os
import requests

BASE_URL = (
    os.environ.get("REACT_APP_BACKEND_URL")
    or "https://emergent-wing.preview.emergentagent.com"
).rstrip("/")
LOGIN_EMAIL = "reggie@coolgeek.me"
LOGIN_PASSWORD = "superadminpass123"


def _login_session() -> requests.Session | None:
    s = requests.Session()
    r = s.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": LOGIN_EMAIL, "password": LOGIN_PASSWORD},
        timeout=20,
    )
    if r.status_code != 200:
        return None
    data = r.json()
    token = data.get("access_token") or data.get("token")
    if token:
        s.headers.update({"Authorization": f"Bearer {token}"})
    return s


# ── /billing/summary structural assertions ──────────────────────────


def test_billing_summary_includes_addons_and_catalogue() -> None:
    s = _login_session()
    if s is None:
        import pytest
        pytest.skip("Login failed; cannot verify summary auth branch")
    r = s.get(f"{BASE_URL}/api/billing/summary", timeout=20)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "addons" in body, "addons counter block missing"
    expected = {
        "extra_orgs",
        "extra_seats",
        "credit_messages",
        "credit_browser_minutes",
        "credit_missions",
    }
    assert expected.issubset(set(body["addons"].keys())), body["addons"]
    assert "addons_catalogue" in body, "catalogue snapshot missing"
    assert isinstance(body["addons_catalogue"], list)
    assert len(body["addons_catalogue"]) == 5
    assert "orgs_limit" in body, "orgs_limit missing"


# ── /billing/addons/checkout validation ─────────────────────────────


def test_checkout_missing_addon_id_422() -> None:
    s = _login_session()
    if s is None:
        import pytest
        pytest.skip("Login failed")
    r = s.post(
        f"{BASE_URL}/api/billing/addons/checkout",
        json={
            "origin_url": "https://x.test",
            "consent_text": "I agree to the charge",
        },
        timeout=15,
    )
    assert r.status_code == 422, f"expected 422 got {r.status_code}: {r.text}"


def test_checkout_short_consent_text_422() -> None:
    s = _login_session()
    if s is None:
        import pytest
        pytest.skip("Login failed")
    r = s.post(
        f"{BASE_URL}/api/billing/addons/checkout",
        json={
            "addon_id": "message_pack",
            "origin_url": "https://x.test",
            "consent_text": "tooshort",
        },
        timeout=15,
    )
    assert r.status_code == 422, r.text


def test_checkout_unknown_addon_id_404() -> None:
    s = _login_session()
    if s is None:
        import pytest
        pytest.skip("Login failed")
    r = s.post(
        f"{BASE_URL}/api/billing/addons/checkout",
        json={
            "addon_id": "definitely_not_a_real_addon",
            "origin_url": "https://x.test",
            "consent_text": "I agree to the charge for this purchase",
        },
        timeout=15,
    )
    assert r.status_code == 404, f"expected 404 got {r.status_code}: {r.text}"


def test_checkout_extra_seat_plan_restriction_or_503() -> None:
    """Reggie is superadmin; plan != 'spark' likely → 400 plan_restrictions.
    Could also be 503 if seat lookup hits Stripe first depending on order.
    The implementation checks plan_restrictions BEFORE stripe (verified by
    reading billing.py:1224-1234), so we expect 400."""
    s = _login_session()
    if s is None:
        import pytest
        pytest.skip("Login failed")
    r = s.post(
        f"{BASE_URL}/api/billing/addons/checkout",
        json={
            "addon_id": "extra_seat",
            "origin_url": "https://x.test",
            "consent_text": "I agree to the recurring charge",
        },
        timeout=15,
    )
    # Accept both — depends on whether staff user is on spark plan or not
    assert r.status_code in (400, 503), f"unexpected {r.status_code}: {r.text}"


def test_checkout_message_pack_returns_503_in_preview() -> None:
    """STRIPE_API_KEY is the shadow placeholder in preview → 503 with
    friendly message."""
    s = _login_session()
    if s is None:
        import pytest
        pytest.skip("Login failed")
    r = s.post(
        f"{BASE_URL}/api/billing/addons/checkout",
        json={
            "addon_id": "message_pack",
            "origin_url": "https://x.test",
            "consent_text": "I agree to the one-time charge for this pack",
        },
        timeout=30,
    )
    assert r.status_code == 503, f"expected 503 got {r.status_code}: {r.text}"
    body = r.json()
    detail = (body.get("detail") or "").lower()
    # User-facing message — should mention configured / support / billing
    assert any(t in detail for t in ("configur", "support", "billing")), detail
