"""Regression: superadmin + staff-email users bypass every user-facing cap.

Feb 2026 — added `is_privileged_user` in staff.py and re-routed
billing / trial-cap / access-code / seat-policy / org-transfer gates
through it. This test locks in the invariant so future refactors
don't silently re-gate internal accounts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _install_env(monkeypatch, staff_emails=None, staff_domains=None):
    """Point staff.py at a controlled fixture set."""
    if staff_emails is None:
        staff_emails = "founder@archeforge.com"
    if staff_domains is None:
        staff_domains = "archeforge.com"
    monkeypatch.setenv("STAFF_EMAILS", staff_emails)
    monkeypatch.setenv("STAFF_DOMAINS", staff_domains)


def _reload_staff():
    import importlib
    import staff as staff_mod  # noqa: F401
    return importlib.reload(staff_mod)


def test_is_privileged_user_superadmin_flag_alone(monkeypatch):
    _install_env(monkeypatch, staff_emails="", staff_domains="")
    s = _reload_staff()
    assert s.is_privileged_user({"is_superadmin": True, "email": "x@example.com"}) is True


def test_is_privileged_user_staff_domain(monkeypatch):
    _install_env(monkeypatch)
    s = _reload_staff()
    assert s.is_privileged_user({"email": "anyone@archeforge.com"}) is True
    # Explicit override — same shape
    assert s.is_privileged_user({"email": "founder@archeforge.com"}) is True


def test_is_privileged_user_regular_user_denied(monkeypatch):
    _install_env(monkeypatch)
    s = _reload_staff()
    assert s.is_privileged_user({"email": "customer@example.com"}) is False
    assert s.is_privileged_user({}) is False
    assert s.is_privileged_user(None) is False


def test_resolve_user_plan_perpetual_for_superadmin(monkeypatch):
    _install_env(monkeypatch)
    _reload_staff()
    import importlib
    import trial_caps
    importlib.reload(trial_caps)
    d = trial_caps.resolve_user_plan({"is_superadmin": True, "email": "any@x.com"})
    assert d["tier"] == "perpetual"
    assert d["reason"] == "superadmin"


def test_resolve_user_plan_perpetual_for_staff_email(monkeypatch):
    _install_env(monkeypatch)
    _reload_staff()
    import importlib
    import trial_caps
    importlib.reload(trial_caps)
    d = trial_caps.resolve_user_plan({"email": "anyone@archeforge.com"})
    assert d["tier"] == "perpetual"
    assert d["reason"] == "staff"


def test_resolve_user_plan_expired_for_regular_user(monkeypatch):
    _install_env(monkeypatch)
    _reload_staff()
    import importlib
    import trial_caps
    importlib.reload(trial_caps)
    past = datetime.now(timezone.utc) - timedelta(days=60)
    d = trial_caps.resolve_user_plan({
        "email": "customer@example.com",
        "access_granted_at": past,
        "access_expires_at": past,
    })
    assert d["tier"] == "expired"


def test_access_state_superadmin_full(monkeypatch):
    _install_env(monkeypatch)
    _reload_staff()
    import importlib
    import access_codes
    importlib.reload(access_codes)
    st = access_codes.access_state({"is_superadmin": True, "email": "x@x.com"})
    assert st["mode"] == "full"
    assert st["reason"] == "superadmin"
    assert st["is_perpetual"] is True


def test_access_state_staff_email_full(monkeypatch):
    _install_env(monkeypatch)
    _reload_staff()
    import importlib
    import access_codes
    importlib.reload(access_codes)
    st = access_codes.access_state({"email": "founder@archeforge.com"})
    assert st["mode"] == "full"
    assert st["reason"] == "staff"
    assert st["is_perpetual"] is True


def test_user_has_access_regular_user_denied(monkeypatch):
    _install_env(monkeypatch)
    _reload_staff()
    import importlib
    import access_codes
    importlib.reload(access_codes)
    allowed, reason = access_codes._user_has_access({"email": "stranger@example.com"})
    assert allowed is False
    assert reason == "none"


def test_seat_warning_suppressed_for_privileged_owner(monkeypatch):
    _install_env(monkeypatch)
    _reload_staff()
    import importlib
    import seat_policy
    importlib.reload(seat_policy)
    # A status dict emitted by compute_seat_status when the owner is privileged.
    status = {
        "plan": "spark",
        "effective_plan": "refine",
        "seats_included": 10_000,
        "seats_used": 42,
        "over_cap": False,
        "next_invite_over_cap": False,
        "boost_source": "privileged",
    }
    assert seat_policy.warning_for_status(status) is None
