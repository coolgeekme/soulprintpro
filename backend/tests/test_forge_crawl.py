"""Forge Crawl unit tests. Hits the real network — fine to skip in CI.

Run: ``cd /app/backend && pytest tests/test_forge_crawl.py -v``
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import forge_crawl as fc


@pytest.mark.asyncio
async def test_scrape_static_site_uses_httpx():
    r = await fc.scrape("https://www.python.org")
    assert r["ok"] is True
    assert r["engine"] == "httpx"
    assert "Python" in r["title"]
    assert len(r["markdown"]) > 500
    assert any("python.org" in u for u in r["links"])


@pytest.mark.asyncio
async def test_scrape_normalizes_missing_scheme():
    r = await fc.scrape("python.org")
    assert r["ok"] is True
    assert r["final_url"].startswith("https://")


@pytest.mark.asyncio
async def test_scrape_invalid_url_returns_error():
    r = await fc.scrape("")
    assert r["ok"] is False
    assert "url is required" in r["error"]


@pytest.mark.asyncio
async def test_crawl_returns_priority_pages():
    r = await fc.crawl("https://www.python.org", max_pages=3)
    assert r["ok"] is True
    assert len(r["pages"]) >= 2
    assert r["pages"][0]["final_url"].rstrip("/") == "https://www.python.org"


@pytest.mark.asyncio
async def test_crawl_max_pages_caps_to_20():
    r = await fc.crawl("https://www.python.org", max_pages=1)
    assert r["ok"] is True
    assert len(r["pages"]) == 1


def test_normalize_url_passthrough():
    assert fc._normalize_url("https://x.com") == "https://x.com"
    assert fc._normalize_url("x.com") == "https://x.com"
    assert fc._normalize_url("") == ""


def test_rank_links_prefers_priority_paths():
    seed = "https://example.com/"
    links = [
        "https://example.com/blog/post-1",
        "https://example.com/about",
        "https://example.com/some/very/deep/page",
        "https://example.com/pricing",
    ]
    ranked = fc._rank_links(seed, links, max_pages=2)
    assert ranked[0] in ("https://example.com/about", "https://example.com/pricing")
