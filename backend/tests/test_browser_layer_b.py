"""Direct Playwright-level tests for Mission Mode Layer B.

Tests the `browser_tools` primitives WITHOUT going through the LLM loop —
this locks in correctness of selector resolution, click matching, fill
behaviour, and submit wiring in isolation. A broken Chromium or SSRF
regression fails here, not in a 3-minute mission run.

Uses `asyncio.run()` to avoid pulling in pytest-asyncio as a dep.

Target page: `https://httpbin.org/forms/post` (a tiny HTML form that
echoes submitted values back as JSON) so every round-trip is verifiable
deterministically. If httpbin is unreachable the module skips.
"""

import asyncio
import uuid

import pytest
import requests

from browser_tools import (
    browse_url,
    browse_click,
    browse_fill,
    browse_submit,
    cleanup_mission_page,
    shutdown_browser,
)


HTTPBIN_FORM = "https://httpbin.org/forms/post"


def _httpbin_up() -> bool:
    try:
        return requests.get("https://httpbin.org/get", timeout=5).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _httpbin_up(),
                                reason="httpbin.org unreachable")


def _run(coro_factory):
    """Run `coro_factory()` in a fresh event loop AND tear the shared
    Chromium down afterwards. Each pytest test needs its own asyncio.run()
    for isolation, but our module-level `_browser` is bound to whichever
    loop launched it — so we have to close it at the end of each test
    before the next asyncio.run() tries to use it."""
    async def wrapped():
        try:
            await coro_factory()
        finally:
            await shutdown_browser()
    asyncio.run(wrapped())


def test_browse_url_sticky_session(tmp_path):
    """browse_url with a mission_id returns content AND leaves the sticky
    page open so Layer B calls operate on it."""
    mid = f"test-{uuid.uuid4().hex}"

    async def go():
        try:
            out = await browse_url(
                HTTPBIN_FORM, take_screenshot=True, upload_dir=tmp_path,
                mission_id=mid,
            )
            assert not out.get("error"), out
            assert out["final_url"].endswith("/forms/post")
            assert "Customer name" in out["text"] or "custname" in out["text"]
            assert out["screenshot"], "expected a screenshot filename"
        finally:
            await cleanup_mission_page(mid)
    _run(go)


def test_browse_fill_by_label_and_selector(tmp_path):
    """browse_fill resolves fields by label, by name attribute, and by a
    CSS selector — exercising 3 of the 4 resolution strategies."""
    mid = f"test-{uuid.uuid4().hex}"

    async def go():
        try:
            await browse_url(HTTPBIN_FORM, take_screenshot=False,
                             upload_dir=tmp_path, mission_id=mid)
            res = await browse_fill(
                mid,
                fields={
                    "Customer name": "Ada Lovelace",       # by label
                    "custtel": "+1-555-1234",              # by name attr
                    "input[name='custemail']": "a@b.com",  # by CSS selector
                    "Totally fake field xyz": "ignored",   # miss
                },
                upload_dir=tmp_path,
            )
            assert not res.get("error"), res
            filled_fields = {f["field"] for f in res.get("filled", [])}
            assert "Customer name" in filled_fields
            assert "custtel" in filled_fields
            assert "input[name='custemail']" in filled_fields
            assert "Totally fake field xyz" in res.get("not_found", [])
        finally:
            await cleanup_mission_page(mid)
    _run(go)


def test_browse_submit_roundtrip(tmp_path):
    """Full pipeline: navigate → fill → submit. httpbin echoes submitted
    form data into the response JSON, so we assert our values made it."""
    mid = f"test-{uuid.uuid4().hex}"

    async def go():
        try:
            await browse_url(HTTPBIN_FORM, take_screenshot=False,
                             upload_dir=tmp_path, mission_id=mid)
            fill_res = await browse_fill(
                mid,
                fields={
                    "Customer name": "Grace Hopper",
                    "custtel": "+1-555-0000",
                    "custemail": "grace@nasa.gov",
                    "comments": "A1 service, would book again.",
                },
                upload_dir=tmp_path,
            )
            assert not fill_res.get("error"), fill_res
            res = await browse_submit(mid, upload_dir=tmp_path)
            assert not res.get("error"), res
            body = res.get("text") or ""
            assert "Grace Hopper" in body, body[:300]
            assert "grace@nasa.gov" in body, body[:300]
            assert "would book again" in body
            assert res.get("final_url", "").endswith("/post")
        finally:
            await cleanup_mission_page(mid)
    _run(go)


def test_browse_click_by_text(tmp_path):
    """browse_click finds the submit button by its visible text ('Submit
    order' on the httpbin form) and triggers form submission."""
    mid = f"test-{uuid.uuid4().hex}"

    async def go():
        try:
            await browse_url(HTTPBIN_FORM, take_screenshot=False,
                             upload_dir=tmp_path, mission_id=mid)
            await browse_fill(
                mid,
                fields={"Customer name": "Click-by-text tester"},
                upload_dir=tmp_path,
            )
            res = await browse_click(mid, text="Submit order", upload_dir=tmp_path)
            assert not res.get("error"), res
            assert "Click-by-text tester" in (res.get("text") or "")
        finally:
            await cleanup_mission_page(mid)
    _run(go)


def test_browse_fill_without_navigation_errors(tmp_path):
    """browse_fill on a mission that hasn't browsed returns a helpful
    error, doesn't crash."""
    mid = f"test-{uuid.uuid4().hex}"

    async def go():
        try:
            res = await browse_fill(mid, fields={"a": "b"}, upload_dir=tmp_path)
            assert res.get("error"), res
            assert "No page is loaded" in res["error"]
        finally:
            await cleanup_mission_page(mid)
    _run(go)


def test_browse_url_ssrf_block(tmp_path):
    """SSRF protection rejects private-network URLs even via the sticky
    session path."""
    mid = f"test-{uuid.uuid4().hex}"

    async def go():
        try:
            res = await browse_url("http://127.0.0.1:1/", take_screenshot=False,
                                   upload_dir=tmp_path, mission_id=mid)
            assert res.get("error"), res
            assert "private" in res["error"].lower() or "local" in res["error"].lower()
        finally:
            await cleanup_mission_page(mid)
    _run(go)
