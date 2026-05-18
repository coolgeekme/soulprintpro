"""Tests for the relative→absolute URL promotion in social_publishing.

This bug caused IG container creation (and downstream publishing) to
fail on production because we shipped `/api/files/<file>` to Meta's
Graph API, which requires fully-qualified `https://...` URLs.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _set_origin(monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", "https://test.example.com")
    monkeypatch.delenv("PUBLIC_ORIGIN_OVERRIDE", raising=False)


def test_absolutize_relative_url():
    from social_publishing import _absolutize
    assert _absolutize("/api/files/foo.png") == "https://test.example.com/api/files/foo.png"


def test_absolutize_passes_through_absolute():
    from social_publishing import _absolutize
    assert _absolutize("https://cdn.x.com/y.png") == "https://cdn.x.com/y.png"
    assert _absolutize("http://localhost:8001/x.png") == "http://localhost:8001/x.png"


def test_absolutize_handles_none_and_empty():
    from social_publishing import _absolutize
    assert _absolutize(None) is None
    assert _absolutize("") == ""


def test_image_url_promotes_relative_path():
    from social_publishing import _image_url
    post = {"image": {"url": "/api/files/abc.png"}}
    assert _image_url(post) == "https://test.example.com/api/files/abc.png"


def test_image_url_prefers_public_url():
    from social_publishing import _image_url
    post = {"image": {"url": "/api/files/abc.png", "public_url": "https://cdn.x.com/abc.png"}}
    assert _image_url(post) == "https://cdn.x.com/abc.png"


def test_image_url_returns_none_when_missing():
    from social_publishing import _image_url
    assert _image_url({}) is None
    assert _image_url({"image": {}}) is None


def test_public_origin_override_wins(monkeypatch):
    monkeypatch.setenv("PUBLIC_ORIGIN_OVERRIDE", "https://custom-cdn.example.com")
    # Force a fresh import-time read by re-importing the module
    from social_publishing import _public_origin
    assert _public_origin() == "https://custom-cdn.example.com"


def test_absolutize_handles_trailing_and_leading_slashes(monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", "https://test.example.com/")
    from social_publishing import _absolutize
    # Trailing slash on origin must not produce a double slash.
    assert _absolutize("/api/files/foo.png") == "https://test.example.com/api/files/foo.png"
    # Path without leading slash also gets one inserted.
    assert _absolutize("api/files/foo.png") == "https://test.example.com/api/files/foo.png"
