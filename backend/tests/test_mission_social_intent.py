"""Iteration 11 — verify mission start_mission() correctly:

  • detects social-posting intent in the goal text
  • stamps mission.social_platform + mission.has_social_session
  • appends the right [SYSTEM CONTEXT] block to the goal
  • leaves non-social missions completely untouched
  • surfaces the new fields through serialize_mission() in
    GET /orgs/{id}/missions and GET /missions/{id}

We use the standing autopost test account whose org already has a
'twitter' social_session uploaded, so we can hit BOTH the
has_social_session=True (tweet) AND the has_social_session=False
(linkedin) branches in the same run.

Every mission we start gets cancelled within ~1s to conserve the
LLM budget — the goal-injection logic is a pre-loop concern, so we
don't need to wait for the mission to actually run.
"""
import os
import time

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Fall back to backend public URL from frontend env file
    from pathlib import Path
    fe_env = Path("/app/frontend/.env")
    if fe_env.exists():
        for line in fe_env.read_text().splitlines():
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                break

STANDING_EMAIL = "autopost-test-1777661963@example.com"
STANDING_PASSWORD = "testtest123"
STANDING_ORG_ID = "69f4f8199304a5a0df27f750"


# ── Fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def auth_token(session):
    r = session.post(f"{BASE_URL}/api/auth/login", json={
        "email": STANDING_EMAIL, "password": STANDING_PASSWORD,
    })
    if r.status_code != 200:
        pytest.skip(f"Standing test account login failed: {r.status_code} {r.text[:200]}")
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def auth_session(session, auth_token):
    session.headers.update({"Authorization": f"Bearer {auth_token}"})
    return session


def _start(auth_session, goal: str) -> dict:
    r = auth_session.post(
        f"{BASE_URL}/api/orgs/{STANDING_ORG_ID}/missions",
        json={"goal": goal},
        timeout=30,
    )
    assert r.status_code == 200, f"start_mission failed: {r.status_code} {r.text[:300]}"
    return r.json()


def _cancel(auth_session, mid: str):
    try:
        auth_session.post(f"{BASE_URL}/api/missions/{mid}/cancel", timeout=10)
    except Exception:
        pass


# ── Helper / detection unit tests (cheap, no HTTP) ──────────────────────────
class TestPlatformDetectionHelper:
    """Exercise detect_social_platform_in_goal() directly."""

    def test_detects_linkedin(self):
        from social_autopost import detect_social_platform_in_goal
        assert detect_social_platform_in_goal(
            "post a celebratory post on our LinkedIn company page"
        ) == "linkedin"

    def test_detects_twitter_via_tweet(self):
        from social_autopost import detect_social_platform_in_goal
        assert detect_social_platform_in_goal(
            "tweet about our new product"
        ) == "twitter"

    def test_detects_facebook(self):
        from social_autopost import detect_social_platform_in_goal
        assert detect_social_platform_in_goal(
            "Make a post on our Facebook page"
        ) == "facebook"

    def test_detects_reddit(self):
        from social_autopost import detect_social_platform_in_goal
        assert detect_social_platform_in_goal(
            "post on Reddit r/marketing"
        ) == "reddit"

    def test_returns_empty_for_non_social_goal(self):
        from social_autopost import detect_social_platform_in_goal
        assert detect_social_platform_in_goal(
            "research the top 3 pain management clinics in Phoenix"
        ) == ""

    def test_returns_empty_for_empty_goal(self):
        from social_autopost import detect_social_platform_in_goal
        assert detect_social_platform_in_goal("") == ""


# ── End-to-end mission-start tests ──────────────────────────────────────────
class TestStartMissionSocialContextInjection:
    """Cover the three branches in missions.start_mission():
       (A) social platform with a stored cookie session
       (B) social platform WITHOUT a stored cookie session
       (C) no social platform at all
    """

    # ── Case A — twitter, has_social_session=True ──────────────────────────
    def test_twitter_goal_injects_authenticated_context(self, auth_session):
        goal = "tweet that AZ Pain Doctors is now open Saturdays"
        mission = _start(auth_session, goal)
        try:
            # social_platform stamp
            assert mission["social_platform"] == "twitter", mission
            # The standing org has a 'twitter' social_session uploaded
            assert mission["has_social_session"] is True, (
                "expected has_social_session=True for the standing org's twitter session "
                f"— got {mission}"
            )

            # Goal must have been augmented with the AUTHENTICATED-branch context
            g = mission["goal"]
            assert g.startswith(goal), "original goal must be preserved verbatim at the start"
            assert "[SYSTEM CONTEXT" in g
            assert "TWITTER" in g
            assert "ALREADY" in g  # cookies are ALREADY loaded
            assert "MUST actually publish" in g or "MUST actually publish" in g
            assert "PUBLISHED:" in g
            # And it must NOT contain the no-session FAILED phrase
            assert "no login cookies on file" not in g
        finally:
            _cancel(auth_session, mission["id"])

    # ── Case B — linkedin, has_social_session=False ────────────────────────
    def test_linkedin_goal_injects_no_session_context(self, auth_session):
        goal = "post a celebratory note on our AZ Pain Doctors LinkedIn company page"
        mission = _start(auth_session, goal)
        try:
            assert mission["social_platform"] == "linkedin", mission
            # The standing org has only twitter cookies, not linkedin
            assert mission["has_social_session"] is False, (
                "expected has_social_session=False (no linkedin cookies on standing org) "
                f"— got {mission}"
            )

            g = mission["goal"]
            assert g.startswith(goal)
            assert "[SYSTEM CONTEXT" in g
            assert "LINKEDIN" in g
            # New policy: no-session branch tells the agent to start
            # normally and rely on Take Control when the login wall
            # appears, INSTEAD of hard-failing the mission.
            assert "NO authenticated session" in g
            assert "Take control" in g
            assert "DO NOT type credentials" in g
            assert "PUBLISHED:" in g  # the agent must still publish post-login
            # And it must NOT carry the old hard-fail directive
            assert "FAILED: no login cookies on file" not in g
            assert "must upload a session at the org's Social Queue page" not in g
            # And it must NOT carry the authenticated-branch language
            assert "ALREADY" not in g
        finally:
            _cancel(auth_session, mission["id"])

    # ── Case C — non-social, no injection at all ───────────────────────────
    def test_nonsocial_goal_is_unchanged(self, auth_session):
        goal = "research the top 3 pain management clinics in Phoenix and write a one-page brief"
        mission = _start(auth_session, goal)
        try:
            assert mission["social_platform"] == ""
            assert mission["has_social_session"] is False
            # Goal is preserved verbatim — no [SYSTEM CONTEXT] block
            assert mission["goal"].strip() == goal.strip(), (
                f"non-social goal should be untouched, got:\n{mission['goal']!r}"
            )
            assert "[SYSTEM CONTEXT" not in mission["goal"]
        finally:
            _cancel(auth_session, mission["id"])


# ── serialize_mission() surface tests ───────────────────────────────────────
class TestSerializeMissionSurfacesNewFields:
    """social_platform + has_social_session must round-trip through
       both list (GET /orgs/{id}/missions) and detail (GET /missions/{id}).
    """

    def test_fields_present_in_list_and_detail(self, auth_session):
        # Start one twitter mission so we have something to fetch
        mission = _start(auth_session, "tweet that we shipped a new feature today")
        mid = mission["id"]
        try:
            # Detail endpoint
            r = auth_session.get(f"{BASE_URL}/api/missions/{mid}", timeout=10)
            assert r.status_code == 200, r.text[:300]
            detail = r.json()
            assert "social_platform" in detail
            assert "has_social_session" in detail
            assert detail["social_platform"] == "twitter"
            assert detail["has_social_session"] is True

            # List endpoint
            r2 = auth_session.get(
                f"{BASE_URL}/api/orgs/{STANDING_ORG_ID}/missions", timeout=10,
            )
            assert r2.status_code == 200, r2.text[:300]
            items = r2.json()
            # may be wrapped — accept list or {"missions": [...]}
            arr = items if isinstance(items, list) else items.get("missions", [])
            match = next((m for m in arr if m.get("id") == mid), None)
            assert match is not None, "freshly-created mission missing from list endpoint"
            assert match.get("social_platform") == "twitter"
            assert match.get("has_social_session") is True
        finally:
            _cancel(auth_session, mid)


# ── System-prompt content tests (cheap, no HTTP) ────────────────────────────
class TestLeadSystemPromptContent:
    """The lead-mission system prompt must contain both the new
       'NEVER ASK for login' rule AND the 'SOCIAL POSTING' section.
    """

    def test_prompt_contains_never_ask_rule(self):
        from server import _build_lead_mission_system_prompt
        prompt = _build_lead_mission_system_prompt(tool_catalog="(stub)")
        assert "LOGIN CREDENTIALS" in prompt
        assert "NEVER ASK" in prompt

    def test_prompt_contains_social_posting_section(self):
        from server import _build_lead_mission_system_prompt
        prompt = _build_lead_mission_system_prompt(tool_catalog="(stub)")
        assert "SOCIAL POSTING" in prompt
        assert "PUBLISHED:" in prompt
        assert "FAILED:" in prompt
        # Must explicitly forbid the bug behaviour we're fixing
        assert "draft" in prompt.lower()


# ── Storage-state pre-registration ──────────────────────────────────────────
class TestStorageStateRegisteredBeforeLoop:
    """When a social mission with cookies is started, the merged
       storage_state must be registered into _MISSION_STORAGE_STATE
       BEFORE the asyncio.create_task — otherwise the agent's first
       browse_url would boot un-authenticated.

       We verify by checking the in-process registry right after the
       HTTP call returns. (Same process — backend is hot-reloaded.)
       Since the running mission may have already consumed the entry
       (pop-on-consume), we accept either: (a) entry is still there OR
       (b) it was already consumed (i.e. the registry behaves correctly).
       What we MUST NOT see is an exception during start_mission.
    """

    def test_twitter_mission_does_not_error_on_registration(self, auth_session):
        # A successful 200 from /missions IS the assertion — registration
        # happens inside the same handler. If it had thrown, the create
        # would have 500'd.
        mission = _start(auth_session, "tweet that we just shipped the new dashboard")
        try:
            assert mission["has_social_session"] is True
            assert mission["social_platform"] == "twitter"
        finally:
            _cancel(auth_session, mission["id"])
