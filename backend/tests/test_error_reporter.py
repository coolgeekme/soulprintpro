"""Regression suite for error_reporter — Resend dispatch + fingerprint
dedupe + bulletproof error swallow.

Confirms:
- The reporter dispatches via send_resend_email with sane payload.
- A duplicate fingerprint inside the rate-limit window is suppressed.
- The reporter never raises — even when the email sender crashes.
- _fingerprint() collapses ID-noisy messages into the same hash so
  the same bug across N requests is throttled to ONE email.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from error_reporter import (  # noqa: E402
    _fingerprint,
    _fingerprint_cache,
    _should_send,
    report_error_async,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    """Each test starts with a clean fingerprint cache."""
    _fingerprint_cache.clear()
    yield
    _fingerprint_cache.clear()


def test_fingerprint_collapses_request_ids():
    """Two failure messages that differ only in a request UUID must
    collapse to the same fingerprint."""
    a = _fingerprint("social_draft", "Failed for req=550e8400-e29b-41d4-a716-446655440000")
    b = _fingerprint("social_draft", "Failed for req=12345678-90ab-cdef-1234-567890abcdef")
    assert a == b


def test_fingerprint_collapses_timestamps_and_numbers():
    a = _fingerprint("img_gen", "Crash at 2026-02-09T11:22:33+00:00 after 4711 ms")
    b = _fingerprint("img_gen", "Crash at 2026-02-10T22:00:00+00:00 after 9999 ms")
    assert a == b


def test_fingerprint_differs_by_kind():
    a = _fingerprint("social_draft", "Boom")
    b = _fingerprint("image_gen", "Boom")
    assert a != b


def test_should_send_respects_rate_limit():
    fp = _fingerprint("k", "msg")
    assert _should_send(fp) is True   # first call → fire
    assert _should_send(fp) is False  # immediate second call → suppressed


@pytest.mark.asyncio
async def test_report_error_dispatches_via_resend():
    """Happy path: send_resend_email is awaited with HTML + text body."""
    sent = {}

    async def fake_send(*, to, subject, html, text):
        sent.update(to=to, subject=subject, html=html, text=text)
        return True

    # patch the module the function uses (`email_service.send_resend_email`)
    with patch("email_service.send_resend_email", new=fake_send):
        ok = await report_error_async(
            kind="social_draft",
            message="LLM timeout after 30s",
            org_id="org-1", org_name="Test Org",
            user_email="bob@example.com",
            extra={"prompt": "make a tweet"},
        )

    assert ok is True
    assert sent["to"] == "team@archeforge.com"
    assert "social_draft" in sent["subject"]
    assert "LLM timeout" in sent["text"]
    assert "Test Org" in sent["html"]
    assert "bob@example.com" in sent["text"]


@pytest.mark.asyncio
async def test_duplicate_within_window_is_suppressed():
    """The reporter is rate-limited by fingerprint to 1 send / 30 min."""
    calls = 0

    async def fake_send(**kw):
        nonlocal calls
        calls += 1
        return True

    with patch("email_service.send_resend_email", new=fake_send):
        ok1 = await report_error_async(kind="x", message="same error happened")
        ok2 = await report_error_async(kind="x", message="same error happened")

    assert ok1 is True
    assert ok2 is False
    assert calls == 1


@pytest.mark.asyncio
async def test_reporter_swallows_send_exceptions():
    """If send_resend_email blows up, the reporter must return False —
    never propagate the exception to the caller's hot path."""

    async def angry_send(**kw):
        raise RuntimeError("Resend API down")

    with patch("email_service.send_resend_email", new=angry_send):
        ok = await report_error_async(kind="x", message="boom")

    assert ok is False  # didn't send, didn't crash


@pytest.mark.asyncio
async def test_reporter_swallows_outer_exceptions():
    """Even if something inside the body builder crashes, no raise."""
    # Pass an `exc` whose __traceback__ access blows up. The reporter
    # should still finish gracefully.
    class BadExc(Exception):
        @property
        def __traceback__(self):
            raise RuntimeError("nope")

    # Just confirm calling it with an unhealthy exception object doesn't
    # propagate. We don't patch send because the reporter should handle
    # the formatting failure first.
    ok = await report_error_async(kind="x", message="boom", exc=BadExc("bad"))
    # Either it sent (returning True) or it bailed (False). The hard
    # contract is: no raise.
    assert isinstance(ok, bool)
