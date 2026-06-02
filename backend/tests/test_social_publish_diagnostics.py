"""Tests for Instagram publish diagnostics on broken cover images.

User report (Jun 2026): clicking "Publish via Foundry" on AZ Pain Doctors
posts returned the generic "Publishing failed on 1 platform. instagram:
No container ID returned from Instagram". Investigation traced it to the
same root cause as the missing-image bug — Meta's IG Graph API server-
side fetches the cover URL; when our URL 404s (lost composite, pre-
GridFS-mirror) Meta returns 200 with an embedded error body, Composio
reports success, and we collapse the missing-id case into a generic
message that gives the user no actionable signal.

Two diagnostics shipped:
  1. Pre-flight HEAD on the media URL before handing it to Meta. Hard
     404/410 → return a specific "regenerate the cover" error.
  2. `_extract_meta_error` walks the response body for the embedded
     ``{"error": {"message": "..."}}`` so the failure self-diagnoses
     even when pre-flight passes.

These tests pin the helpers without standing up Composio or aiohttp.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from social_publishing import (
    _extract_meta_error,
    _media_url_is_reachable,
)


# ── _extract_meta_error ──────────────────────────────────────────────

def test_extract_meta_error_nested_error_message() -> None:
    """Meta's canonical shape — ``response.error.message``."""
    resp = {
        "response": {
            "error": {
                "message": "The Media URL is not accessible.",
                "type": "OAuthException",
                "code": 9004,
            }
        }
    }
    assert _extract_meta_error(resp) == "The Media URL is not accessible."


def test_extract_meta_error_root_level_error() -> None:
    """Some Composio versions hoist ``error`` to the top level."""
    resp = {"error": {"message": "Caption is too long (max 2200 chars)."}}
    assert _extract_meta_error(resp) == "Caption is too long (max 2200 chars)."


def test_extract_meta_error_data_shape() -> None:
    """And some put the wrapped Meta body under ``data``."""
    resp = {"data": {"error": {"error_user_msg": "Aspect ratio not supported"}}}
    assert _extract_meta_error(resp) == "Aspect ratio not supported"


def test_extract_meta_error_returns_none_on_clean_response() -> None:
    """If there's no error embedded, helper returns None and the caller
    falls back to the generic 'Meta gave no reason' message."""
    resp = {"data": {"id": "17841405822304914"}}
    assert _extract_meta_error(resp) is None


def test_extract_meta_error_ignores_ok_message() -> None:
    """A literal ``"message": "OK"`` shouldn't be surfaced as an error."""
    resp = {"data": {"message": "OK"}}
    assert _extract_meta_error(resp) is None


def test_extract_meta_error_handles_non_dict_input() -> None:
    assert _extract_meta_error(None) is None
    assert _extract_meta_error("a string") is None
    assert _extract_meta_error([1, 2, 3]) is None


# ── _media_url_is_reachable ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_media_url_reachable_returns_false_on_none() -> None:
    """No URL → not reachable, no status."""
    ok, status = await _media_url_is_reachable(None)
    assert ok is False
    assert status is None


@pytest.mark.asyncio
async def test_media_url_reachable_returns_true_on_2xx() -> None:
    """200 from the HEAD = the publisher can proceed."""

    class _FakeResp:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def head(self, url, allow_redirects=True): return _FakeResp()

    with patch("aiohttp.ClientSession", return_value=_FakeSession()):
        ok, status = await _media_url_is_reachable("https://example.com/img.png")
    assert ok is True
    assert status == 200


@pytest.mark.asyncio
async def test_media_url_reachable_returns_false_on_404() -> None:
    """The exact failure mode the user hit — composite file missing."""

    class _FakeResp:
        status = 404
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def head(self, url, allow_redirects=True): return _FakeResp()

    with patch("aiohttp.ClientSession", return_value=_FakeSession()):
        ok, status = await _media_url_is_reachable("https://example.com/gone.png")
    assert ok is False
    assert status == 404


@pytest.mark.asyncio
async def test_media_url_reachable_does_not_block_on_transient_errors() -> None:
    """Network errors / timeouts → return True so the publisher still
    tries. We don't want a flaky DNS resolver to fail every IG publish.
    """
    with patch("aiohttp.ClientSession", side_effect=RuntimeError("transient")):
        ok, status = await _media_url_is_reachable("https://flaky.example.com/x.png")
    assert ok is True
    assert status is None
