"""Tests for the staff override helper."""
from __future__ import annotations

import importlib
import os
import sys


def _reload_staff(monkeypatch, **env):
    """Reimport staff.py with fresh env so we can test overrides."""
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    sys.path.insert(0, "/app/backend")
    if "staff" in sys.modules:
        del sys.modules["staff"]
    return importlib.import_module("staff")


def test_default_staff_includes_reggie_and_archeforge(monkeypatch):
    s = _reload_staff(monkeypatch, STAFF_EMAILS=None, STAFF_DOMAINS=None)
    assert s.is_staff_email("reggie@coolgeek.me") is True
    assert s.is_staff_email("REGGIE@CoolGeek.me") is True       # case-insensitive
    assert s.is_staff_email("anyone@archeforge.com") is True
    assert s.is_staff_email("ANYONE@ArcheForge.com") is True
    assert s.is_staff_email("stranger@example.com") is False
    assert s.is_staff_email("") is False
    assert s.is_staff_email(None) is False
    # Subdomain MUST NOT match — staff is exact-domain only.
    assert s.is_staff_email("user@app.archeforge.com") is False


def test_env_overrides_take_precedence(monkeypatch):
    s = _reload_staff(
        monkeypatch,
        STAFF_EMAILS="founder@example.com",
        STAFF_DOMAINS="foundryagents.ai",
    )
    assert s.is_staff_email("founder@example.com") is True
    assert s.is_staff_email("anyone@foundryagents.ai") is True
    # Defaults are replaced, not merged — overriding the env wipes them.
    assert s.is_staff_email("reggie@coolgeek.me") is False
    assert s.is_staff_email("anyone@archeforge.com") is False


def test_leading_at_in_domain_env_is_stripped(monkeypatch):
    s = _reload_staff(monkeypatch, STAFF_DOMAINS="@archeforge.com,@foundryagents.ai")
    assert s.is_staff_email("anyone@archeforge.com") is True
    assert s.is_staff_email("anyone@foundryagents.ai") is True
