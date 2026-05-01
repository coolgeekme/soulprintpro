"""Tests for iteration_12: friendlier cookie upload for autopost.

Covers (all via HTTP to REACT_APP_BACKEND_URL):
  - Unit tests for ``parse_cookies_txt`` and ``normalise_session_upload``
  - POST /api/orgs/{org_id}/social/sessions
        · legacy shape: {platform, storage_state, label}         (regression)
        · new shape:    {platform, cookies_text=<netscape>, label}
        · new shape:    {platform, cookies_text=<raw JSON array>, label}
        · rejects empty + garbage cookies_text with 422 + human message
  - POST /api/orgs/{org_id}/social/sessions/{platform}/test
        · with body.cookies_text (FAKE cookies → ok=False for twitter)
        · with empty body → falls back to persisted session
        · rejects unknown platform with 422
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest
import requests

# Make `backend/` importable so we can unit-test the parser helpers.
sys.path.insert(0, "/app/backend")

from social_autopost import (  # noqa: E402
    normalise_session_upload,
    parse_cookies_txt,
)


def _base_url() -> str:
    url = os.environ.get("REACT_APP_BACKEND_URL", "").strip()
    if not url:
        try:
            with open("/app/frontend/.env", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("REACT_APP_BACKEND_URL="):
                        url = line.split("=", 1)[1].strip()
                        break
        except OSError:
            pass
    return url.rstrip("/")


BASE_URL = _base_url()
assert BASE_URL, "REACT_APP_BACKEND_URL missing"
API = f"{BASE_URL}/api"


NETSCAPE_SAMPLE = (
    "# Netscape HTTP Cookie File\n"
    "# comment – should be ignored\n"
    "\n"
    ".x.com\tTRUE\t/\tTRUE\t1780000000\tauth_token\tfake-token-abc\n"
    "#HttpOnly_.x.com\tTRUE\t/\tTRUE\t1780000000\tct0\tfake-csrf\n"
)

RAW_JSON_ARRAY = (
    '[{"name":"auth_token","value":"arr-val","domain":".x.com","path":"/",'
    '"secure":true,"httpOnly":true},'
    '{"name":"ct0","value":"arr-csrf","domain":".x.com","path":"/"}]'
)


# ── Unit tests (no HTTP) ──────────────────────────────────────────────────
class TestParseCookiesTxt:
    def test_parses_tab_separated_rows(self):
        cookies = parse_cookies_txt(NETSCAPE_SAMPLE)
        assert len(cookies) == 2
        c1 = cookies[0]
        assert c1["name"] == "auth_token"
        assert c1["value"] == "fake-token-abc"
        assert c1["domain"] == ".x.com"
        assert c1["path"] == "/"
        assert c1["secure"] is True
        assert c1["httpOnly"] is False
        assert c1["sameSite"] == "Lax"
        # expiration converted to numeric and preserved
        assert c1["expires"] == 1780000000

    def test_http_only_prefix_sets_http_only_true(self):
        cookies = parse_cookies_txt(NETSCAPE_SAMPLE)
        ct0 = next(c for c in cookies if c["name"] == "ct0")
        assert ct0["httpOnly"] is True
        assert ct0["domain"] == ".x.com"

    def test_ignores_comment_and_blank_lines(self):
        text = "# a comment\n\n# another\n"
        assert parse_cookies_txt(text) == []

    def test_bad_expiration_falls_back_to_session_cookie(self):
        bad = ".x.com\tTRUE\t/\tTRUE\tNOT_A_NUMBER\tfoo\tbar\n"
        cookies = parse_cookies_txt(bad)
        assert len(cookies) == 1
        assert cookies[0]["expires"] == -1


class TestNormaliseSessionUpload:
    def test_empty_string_rejected(self):
        with pytest.raises(ValueError) as exc:
            normalise_session_upload("")
        assert "empty" in str(exc.value).lower()

    def test_garbage_rejected_with_helpful_message(self):
        with pytest.raises(ValueError) as exc:
            normalise_session_upload("this is just garbage, not cookies")
        msg = str(exc.value)
        assert "storage_state" in msg
        assert "cookies.txt" in msg.lower() or "netscape" in msg.lower() or "JSON" in msg

    def test_accepts_storage_state_json(self):
        import json
        payload = json.dumps({"cookies": [
            {"name": "a", "value": "b", "domain": ".x.com"},
        ], "origins": []})
        out = normalise_session_upload(payload)
        assert isinstance(out, dict)
        assert out["cookies"][0]["name"] == "a"
        assert out["origins"] == []

    def test_accepts_raw_json_array(self):
        out = normalise_session_upload(RAW_JSON_ARRAY)
        assert len(out["cookies"]) == 2
        assert any(c["name"] == "auth_token" for c in out["cookies"])

    def test_accepts_netscape(self):
        out = normalise_session_upload(NETSCAPE_SAMPLE)
        assert len(out["cookies"]) == 2
        names = {c["name"] for c in out["cookies"]}
        assert names == {"auth_token", "ct0"}


# ── HTTP integration tests ────────────────────────────────────────────────
@pytest.fixture(scope="module")
def ctx():
    s = requests.Session()
    s.headers["Content-Type"] = "application/json"
    email = f"TEST_sessupload_{uuid.uuid4().hex[:10]}@example.com"
    pwd = "StrongPass!234"
    r = s.post(f"{API}/auth/register", json={
        "email": email, "password": pwd, "name": "Session Upload Tester",
    }, timeout=30)
    assert r.status_code in (200, 201), r.text
    token = r.json().get("access_token") or r.json().get("token")
    s.headers["Authorization"] = f"Bearer {token}"
    # Re-login so JWT sub aligns with org_member (matches existing pattern).
    rl = s.post(f"{API}/auth/login", json={"email": email, "password": pwd}, timeout=30)
    if rl.status_code == 200:
        tk2 = rl.json().get("access_token") or rl.json().get("token")
        if tk2:
            s.headers["Authorization"] = f"Bearer {tk2}"

    r = s.get(f"{API}/orgs", timeout=20)
    orgs = r.json()
    if orgs:
        org_id = orgs[0]["id"]
    else:
        c = s.post(f"{API}/orgs",
                   json={"name": f"TEST_SessUploadOrg_{uuid.uuid4().hex[:6]}"},
                   timeout=60)
        assert c.status_code in (200, 201), c.text
        org_id = c.json().get("id") or c.json().get("_id")
    yield {"s": s, "org_id": org_id}
    # Teardown: delete any sessions we created to not pollute the org.
    for plat in ("twitter", "linkedin"):
        try:
            s.delete(f"{API}/orgs/{org_id}/social/sessions/{plat}", timeout=10)
        except Exception:
            pass


class TestUpsertSessionVariants:
    def test_legacy_storage_state_shape_still_works(self, ctx):
        """Regression: the iteration_11 path (storage_state dict) must
        keep working verbatim."""
        s, org_id = ctx["s"], ctx["org_id"]
        body = {
            "platform": "linkedin",
            "storage_state": {"cookies": [
                {"name": "li_at", "value": "LEGACY_FAKE",
                 "domain": ".linkedin.com", "path": "/",
                 "secure": True, "httpOnly": True, "sameSite": "None",
                 "expires": -1},
            ], "origins": []},
            "label": "TEST_legacy",
        }
        r = s.post(f"{API}/orgs/{org_id}/social/sessions", json=body, timeout=20)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["platform"] == "linkedin"
        assert d["cookies_count"] == 1
        assert "storage_state" not in d
        assert "LEGACY_FAKE" not in r.text
        # Cleanup
        s.delete(f"{API}/orgs/{org_id}/social/sessions/linkedin", timeout=10)

    def test_new_cookies_text_netscape(self, ctx):
        s, org_id = ctx["s"], ctx["org_id"]
        body = {
            "platform": "twitter",
            "cookies_text": NETSCAPE_SAMPLE,
            "label": "TEST_netscape",
        }
        r = s.post(f"{API}/orgs/{org_id}/social/sessions", json=body, timeout=20)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["platform"] == "twitter"
        assert d["cookies_count"] == 2
        assert d["origins_count"] == 0
        # Listing should reflect this session but never leak raw cookies.
        rg = s.get(f"{API}/orgs/{org_id}/social/sessions", timeout=20)
        assert rg.status_code == 200
        sessions = rg.json()
        row = next(x for x in sessions if x["platform"] == "twitter")
        assert row["cookies_count"] == 2
        assert "fake-token-abc" not in rg.text
        assert "fake-csrf" not in rg.text

    def test_new_cookies_text_raw_json_array(self, ctx):
        s, org_id = ctx["s"], ctx["org_id"]
        body = {
            "platform": "twitter",
            "cookies_text": RAW_JSON_ARRAY,
            "label": "TEST_raw_json_array",
        }
        r = s.post(f"{API}/orgs/{org_id}/social/sessions", json=body, timeout=20)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["cookies_count"] == 2

    def test_empty_cookies_text_rejected(self, ctx):
        s, org_id = ctx["s"], ctx["org_id"]
        r = s.post(f"{API}/orgs/{org_id}/social/sessions", json={
            "platform": "twitter", "cookies_text": "", "label": "TEST_empty",
        }, timeout=20)
        assert r.status_code == 422, r.text
        # Cleaned-up human message
        assert "empty" in r.text.lower() or "cookies" in r.text.lower()

    def test_garbage_cookies_text_rejected_with_format_hint(self, ctx):
        s, org_id = ctx["s"], ctx["org_id"]
        r = s.post(f"{API}/orgs/{org_id}/social/sessions", json={
            "platform": "twitter", "cookies_text": "this is utter nonsense",
        }, timeout=20)
        assert r.status_code == 422, r.text
        body = r.text
        # Surfaces at least one of the three format names so user knows what to try.
        assert ("storage_state" in body) or ("cookies.txt" in body) or ("JSON" in body)

    def test_no_body_shape_rejected(self, ctx):
        """Neither storage_state nor cookies_text → 422."""
        s, org_id = ctx["s"], ctx["org_id"]
        r = s.post(f"{API}/orgs/{org_id}/social/sessions", json={
            "platform": "twitter", "label": "TEST_no_body",
        }, timeout=20)
        assert r.status_code == 422


class TestSessionTestEndpoint:
    def test_unsupported_platform_rejected(self, ctx):
        s, org_id = ctx["s"], ctx["org_id"]
        r = s.post(f"{API}/orgs/{org_id}/social/sessions/myspace/test",
                   json={"cookies_text": NETSCAPE_SAMPLE}, timeout=90)
        assert r.status_code == 422

    def test_empty_body_no_persisted_session_returns_404(self, ctx):
        """Request body empty + no stored cookies for that platform → 404."""
        s, org_id = ctx["s"], ctx["org_id"]
        # Make sure there's no reddit session on this test org.
        s.delete(f"{API}/orgs/{org_id}/social/sessions/reddit", timeout=10)
        r = s.post(f"{API}/orgs/{org_id}/social/sessions/reddit/test",
                   json={}, timeout=60)
        assert r.status_code == 404, r.text

    def test_fake_twitter_cookies_returns_ok_false(self, ctx):
        """Heuristic: x.com should redirect away from /home with fake cookies."""
        s, org_id = ctx["s"], ctx["org_id"]
        r = s.post(f"{API}/orgs/{org_id}/social/sessions/twitter/test",
                   json={"cookies_text": NETSCAPE_SAMPLE}, timeout=120)
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body.keys()) >= {"ok", "message", "final_url"}
        assert body["ok"] is False
        # Message should reference the login-check outcome.
        assert "couldn't confirm" in body["message"].lower() \
            or "could not confirm" in body["message"].lower() \
            or "logged in" in body["message"].lower() \
            or "log in" in body["message"].lower()

    def test_empty_body_falls_back_to_persisted_session(self, ctx):
        """After a cookies_text upload on twitter, POST to /test with no
        body should reuse the persisted storage_state (expected ok=False)."""
        s, org_id = ctx["s"], ctx["org_id"]
        # Ensure a twitter session exists
        s.post(f"{API}/orgs/{org_id}/social/sessions", json={
            "platform": "twitter", "cookies_text": NETSCAPE_SAMPLE,
            "label": "TEST_persisted",
        }, timeout=20)
        r = s.post(f"{API}/orgs/{org_id}/social/sessions/twitter/test",
                   json={}, timeout=120)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "ok" in body and "message" in body and "final_url" in body
        assert isinstance(body["ok"], bool)
