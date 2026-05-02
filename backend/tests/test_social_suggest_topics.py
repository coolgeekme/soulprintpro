"""Backend tests for Social Queue — Suggest Topics feature.

Covers POST /api/orgs/{org_id}/social/suggest-topics which fuses the org's
Soul (mission/audience/tone/values) with live Perplexity news and asks
GPT-4.1-mini to synthesise N distinct postable topic ideas.

The real Emergent LLM key is used — each topic call is ~10-20s wall
time (Perplexity ~3-5s + LLM ~5-15s). Tests share a single happy-path
response via a module-scoped fixture to keep runtime reasonable.
"""
from __future__ import annotations

import os
import time
import uuid

import pytest
import requests


# ── config ─────────────────────────────────────────────────────────────────
def _load_backend_url() -> str:
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


BASE_URL = _load_backend_url()
assert BASE_URL, "REACT_APP_BACKEND_URL missing"
API = f"{BASE_URL}/api"

STANDING_EMAIL = "autopost-test-1777661963@example.com"
STANDING_PWD = "testtest123"
STANDING_ORG = "69f4f8199304a5a0df27f750"

VALID_ANGLES = {"news", "evergreen", "promotional", "community", "thought-leadership"}


# ── shared fixtures ────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def standing_ctx():
    """Log into the standing autopost smoke-test account (has Soul seeded)."""
    s = requests.Session()
    s.headers["Content-Type"] = "application/json"
    r = s.post(f"{API}/auth/login", json={
        "email": STANDING_EMAIL, "password": STANDING_PWD,
    }, timeout=30)
    assert r.status_code == 200, f"standing login failed: {r.status_code} {r.text[:300]}"
    data = r.json()
    token = data.get("access_token") or data.get("token")
    assert token, f"no token in login response: {data}"
    s.headers["Authorization"] = f"Bearer {token}"
    return {"session": s, "org_id": STANDING_ORG}


@pytest.fixture(scope="module")
def fresh_user_ctx():
    """Register a brand new user who has NO Soul — verifies graceful degradation."""
    s = requests.Session()
    s.headers["Content-Type"] = "application/json"
    email = f"TEST_topics_{uuid.uuid4().hex[:10]}@example.com"
    pwd = "StrongPass!234"
    r = s.post(f"{API}/auth/register", json={
        "email": email, "password": pwd, "name": "Topic Tester",
    }, timeout=30)
    assert r.status_code in (200, 201), f"register: {r.status_code} {r.text[:300]}"
    data = r.json()
    token = data.get("access_token") or data.get("token")
    assert token, f"no token in register response: {data}"
    s.headers["Authorization"] = f"Bearer {token}"

    # Ensure we have at least one org (none auto-created on register)
    r = s.get(f"{API}/orgs", timeout=20)
    assert r.status_code == 200, r.text
    orgs = r.json()
    if not orgs:
        c = s.post(f"{API}/orgs", json={"name": f"TEST_TopicsOrg_{uuid.uuid4().hex[:6]}"},
                   timeout=60)
        assert c.status_code in (200, 201), f"org create: {c.status_code} {c.text[:300]}"
        org_id = c.json().get("id") or c.json().get("_id")
    else:
        org_id = orgs[0]["id"]
    return {"session": s, "email": email, "org_id": org_id}


@pytest.fixture(scope="module")
def default_topics(standing_ctx):
    """Call suggest-topics once with defaults; reuse the response in shape tests."""
    s = standing_ctx["session"]
    org_id = standing_ctx["org_id"]
    t0 = time.time()
    r = s.post(f"{API}/orgs/{org_id}/social/suggest-topics", json={}, timeout=60)
    elapsed = time.time() - t0
    assert r.status_code == 200, (
        f"suggest-topics failed in {elapsed:.1f}s: {r.status_code} {r.text[:500]}"
    )
    body = r.json()
    assert "topics" in body, f"response missing 'topics' key: {body}"
    topics = body["topics"]
    assert isinstance(topics, list) and len(topics) > 0, f"no topics returned: {body}"
    return {"topics": topics, "elapsed": elapsed}


# ── 1. Happy path ───────────────────────────────────────────────────────────
class TestHappyPath:
    def test_default_returns_six_topics(self, default_topics):
        # Default count=6, angle="mix" — spec says 6 items.
        topics = default_topics["topics"]
        assert len(topics) == 6, f"expected 6 topics on default call, got {len(topics)}"

    def test_response_time_under_45s(self, default_topics):
        assert default_topics["elapsed"] < 45, (
            f"suggest-topics took {default_topics['elapsed']:.1f}s; spec says <45s"
        )

    def test_every_topic_has_required_keys(self, default_topics):
        required = {"id", "headline", "pitch", "angle", "tags",
                    "source_url", "source_title"}
        for i, t in enumerate(default_topics["topics"]):
            missing = required - set(t.keys())
            assert not missing, f"topic[{i}] missing keys {missing}: {t}"

    def test_id_is_uuid_string(self, default_topics):
        for t in default_topics["topics"]:
            assert isinstance(t["id"], str) and len(t["id"]) >= 32
            # Will raise if not a valid UUID.
            uuid.UUID(t["id"])

    def test_headlines_non_empty_strings(self, default_topics):
        for t in default_topics["topics"]:
            assert isinstance(t["headline"], str)
            hl = t["headline"].strip()
            assert hl, "empty headline"
            # Spec says 6-14 words; allow slack since LLM output varies.
            word_count = len(hl.split())
            assert 3 <= word_count <= 25, f"headline word count {word_count}: {hl!r}"

    def test_pitch_is_string(self, default_topics):
        for t in default_topics["topics"]:
            assert isinstance(t["pitch"], str)

    def test_angle_in_valid_set(self, default_topics):
        for t in default_topics["topics"]:
            assert t["angle"] in VALID_ANGLES, (
                f"angle {t['angle']!r} not in {VALID_ANGLES}"
            )

    def test_tags_are_list_without_hash_prefix(self, default_topics):
        for t in default_topics["topics"]:
            assert isinstance(t["tags"], list), f"tags not a list: {t['tags']!r}"
            # Spec says 2-4 but spec also slices up to 6; be lenient.
            assert 0 <= len(t["tags"]) <= 6, f"tag count out of bounds: {t['tags']}"
            for tag in t["tags"]:
                assert isinstance(tag, str)
                assert not tag.startswith("#"), f"tag has # prefix: {tag!r}"

    def test_source_url_and_title_are_strings(self, default_topics):
        for t in default_topics["topics"]:
            assert isinstance(t["source_url"], str)
            assert isinstance(t["source_title"], str)

    def test_ids_unique_across_batch(self, default_topics):
        ids = [t["id"] for t in default_topics["topics"]]
        assert len(set(ids)) == len(ids), f"duplicate ids in batch: {ids}"


# ── 2. Hallucinated URL stripping ──────────────────────────────────────────
class TestHallucinatedUrls:
    def test_source_url_either_empty_or_non_hallucinated(self, default_topics):
        """Every source_url must either be empty OR look like a real http URL
        (the server drops any URL not in Perplexity's citation set)."""
        for t in default_topics["topics"]:
            u = t["source_url"]
            if u:
                assert u.startswith("http://") or u.startswith("https://"), (
                    f"non-empty source_url is not http(s): {u!r}"
                )
                # If source_url non-empty, source_title may or may not be set,
                # but it should not contain placeholder text.
                assert "example.com" not in u.lower() or "TEST_" in u, (
                    f"source_url looks like a placeholder: {u!r}"
                )


# ── 3. Count bounds ─────────────────────────────────────────────────────────
class TestCountBounds:
    def test_count_3_returns_three(self, standing_ctx):
        r = standing_ctx["session"].post(
            f"{API}/orgs/{standing_ctx['org_id']}/social/suggest-topics",
            json={"count": 3}, timeout=60,
        )
        assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"
        topics = r.json().get("topics", [])
        assert len(topics) == 3, f"expected 3, got {len(topics)}"

    def test_count_10_returns_up_to_ten(self, standing_ctx):
        r = standing_ctx["session"].post(
            f"{API}/orgs/{standing_ctx['org_id']}/social/suggest-topics",
            json={"count": 10}, timeout=90,
        )
        assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"
        topics = r.json().get("topics", [])
        # "up to 10" — allow slack but it must be >6 (above the default) and ≤10.
        assert 6 <= len(topics) <= 10, f"expected 6-10 topics for count=10, got {len(topics)}"

    def test_count_below_minimum_422(self, standing_ctx):
        r = standing_ctx["session"].post(
            f"{API}/orgs/{standing_ctx['org_id']}/social/suggest-topics",
            json={"count": 2}, timeout=15,
        )
        assert r.status_code == 422, f"expected 422 for count=2, got {r.status_code}: {r.text[:200]}"

    def test_count_above_maximum_422(self, standing_ctx):
        r = standing_ctx["session"].post(
            f"{API}/orgs/{standing_ctx['org_id']}/social/suggest-topics",
            json={"count": 13}, timeout=15,
        )
        assert r.status_code == 422, f"expected 422 for count=13, got {r.status_code}: {r.text[:200]}"


# ── 4. Angle filter ────────────────────────────────────────────────────────
class TestAngleFilter:
    def test_invalid_angle_422(self, standing_ctx):
        r = standing_ctx["session"].post(
            f"{API}/orgs/{standing_ctx['org_id']}/social/suggest-topics",
            json={"angle": "spicy-take"}, timeout=15,
        )
        assert r.status_code == 422, (
            f"expected 422 for invalid angle, got {r.status_code}: {r.text[:200]}"
        )

    def test_empty_angle_accepted(self, standing_ctx):
        # Validates the Literal allows "" (mix). Small count=3 to speed up.
        r = standing_ctx["session"].post(
            f"{API}/orgs/{standing_ctx['org_id']}/social/suggest-topics",
            json={"angle": "", "count": 3}, timeout=60,
        )
        assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"

    def test_news_angle_accepted_and_returns_topics(self, standing_ctx):
        r = standing_ctx["session"].post(
            f"{API}/orgs/{standing_ctx['org_id']}/social/suggest-topics",
            json={"angle": "news", "count": 3}, timeout=60,
        )
        assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"
        topics = r.json().get("topics", [])
        assert len(topics) == 3
        # All topic.angle values still must be in the valid set.
        for t in topics:
            assert t["angle"] in VALID_ANGLES


# ── 5. Hint propagation ────────────────────────────────────────────────────
class TestHint:
    def test_hint_accepted_and_influences_output(self, standing_ctx):
        # Unique tokenable hint so we can detect whether the LLM absorbed it.
        marker = "whitepaper"
        r = standing_ctx["session"].post(
            f"{API}/orgs/{standing_ctx['org_id']}/social/suggest-topics",
            json={
                "count": 3,
                "hint": f"We're launching our annual {marker} next week, lean into that.",
            },
            timeout=60,
        )
        assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"
        topics = r.json().get("topics", [])
        assert len(topics) == 3
        # Lenient: check the hint marker appears somewhere in headlines/pitches/tags.
        blob = " ".join(
            f"{t.get('headline','')} {t.get('pitch','')} {' '.join(t.get('tags', []))}"
            for t in topics
        ).lower()
        assert marker in blob, (
            f"hint marker {marker!r} not referenced anywhere in 3-topic batch: {topics}"
        )


# ── 6. Graceful degradation (no Soul) ──────────────────────────────────────
class TestNoSoul:
    def test_org_without_soul_still_returns_topics(self, fresh_user_ctx):
        """A brand-new org has no Soul — suggester must fall back to company
        name only without raising an HTTPException."""
        r = fresh_user_ctx["session"].post(
            f"{API}/orgs/{fresh_user_ctx['org_id']}/social/suggest-topics",
            json={"count": 3}, timeout=60,
        )
        assert r.status_code == 200, (
            f"no-soul call failed: {r.status_code}: {r.text[:300]}"
        )
        topics = r.json().get("topics", [])
        assert len(topics) == 3, f"expected 3 topics (no-soul fallback), got {len(topics)}"
        for t in topics:
            assert t["headline"].strip()
            assert t["angle"] in VALID_ANGLES


# ── 7. Authorisation / errors ──────────────────────────────────────────────
class TestAuthorization:
    def test_unknown_org_403_or_404(self, standing_ctx):
        bogus = "000000000000000000000000"
        r = standing_ctx["session"].post(
            f"{API}/orgs/{bogus}/social/suggest-topics",
            json={}, timeout=15,
        )
        # require_org_member runs first — non-member returns 403; if the
        # guard allows through, find_one returns None → 404. Either is OK.
        assert r.status_code in (403, 404), (
            f"expected 403/404 for bogus org, got {r.status_code}: {r.text[:200]}"
        )

    def test_non_member_gets_403(self, fresh_user_ctx):
        # fresh_user is not a member of the standing org → 403.
        r = fresh_user_ctx["session"].post(
            f"{API}/orgs/{STANDING_ORG}/social/suggest-topics",
            json={}, timeout=15,
        )
        assert r.status_code == 403, (
            f"expected 403 for non-member, got {r.status_code}: {r.text[:200]}"
        )

    def test_unauthenticated_401(self, standing_ctx):
        # Fresh session with no auth header.
        r = requests.post(
            f"{API}/orgs/{STANDING_ORG}/social/suggest-topics",
            json={}, timeout=15,
        )
        assert r.status_code in (401, 403), (
            f"expected 401/403 unauthenticated, got {r.status_code}: {r.text[:200]}"
        )
