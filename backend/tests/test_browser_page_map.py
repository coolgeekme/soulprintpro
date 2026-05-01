"""Regression tests for the page-map (`affordances`) extraction.

The Mission loop's intelligence depends on `browse_url` returning a clear
description of what's clickable on the page (links, buttons, forms with
field labels). Without it the LLM would be navigating blind. These tests
hit `httpbin.org/forms/post` — a tiny HTML page with one well-known form
plus a single submit button — so changes to the JS page-map extractor
fail loudly here.
"""

import asyncio
import uuid
from pathlib import Path

import pytest
import requests

from browser_tools import browse_url, cleanup_mission_page, shutdown_browser


HTTPBIN_FORM = "https://httpbin.org/forms/post"


def _httpbin_up() -> bool:
    try:
        return requests.get("https://httpbin.org/get", timeout=5).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _httpbin_up(),
                                reason="httpbin.org unreachable")


def _run(coro_factory):
    async def wrapped():
        try:
            await coro_factory()
        finally:
            await shutdown_browser()
    asyncio.run(wrapped())


def test_page_map_includes_form_with_labeled_fields(tmp_path):
    """The httpbin order form has 4 named text fields, 3 size radios, and
    several topping checkboxes. We expect the page-map to surface ALL of
    them with their visible labels resolved."""
    mid = f"test-{uuid.uuid4().hex}"

    async def go():
        try:
            out = await browse_url(HTTPBIN_FORM, take_screenshot=False,
                                   upload_dir=tmp_path, mission_id=mid)
            assert not out.get("error"), out
            forms = out.get("forms") or []
            assert len(forms) >= 1, "expected at least one form"
            f = forms[0]
            field_names = {fld.get("name") for fld in f.get("fields") or []}
            assert {"custname", "custtel", "custemail"}.issubset(field_names), field_names
            # Check label resolution worked for the text fields
            labels_by_name = {
                fld.get("name"): (fld.get("label") or "")
                for fld in f.get("fields") or []
                if fld.get("type") in {"text", "email", "tel"}
            }
            assert "Customer name" in labels_by_name.get("custname", "")
            assert "E-mail" in labels_by_name.get("custemail", "")
            # Submit button text is captured
            assert (f.get("submit_text") or "").lower().startswith("submit")
            # Radios for size should be present with their visible labels
            radios = [fld for fld in f.get("fields") or [] if fld.get("type") == "radio"]
            radio_labels = {fld.get("label") for fld in radios}
            assert "Small" in radio_labels and "Medium" in radio_labels and "Large" in radio_labels
        finally:
            await cleanup_mission_page(mid)
    _run(go)


def test_page_map_surfaces_button_text(tmp_path):
    """The submit button on the httpbin form is plain `<button>Submit order
    </button>`. The buttons list should include it with its visible text."""
    mid = f"test-{uuid.uuid4().hex}"

    async def go():
        try:
            out = await browse_url(HTTPBIN_FORM, take_screenshot=False,
                                   upload_dir=tmp_path, mission_id=mid)
            assert not out.get("error"), out
            buttons = out.get("buttons") or []
            assert any("Submit order" in (b.get("text") or "") for b in buttons), buttons
        finally:
            await cleanup_mission_page(mid)
    _run(go)


def test_page_map_truncates_to_safe_caps(tmp_path):
    """example.com is the smallest live page on the public web — it has a
    single anchor and zero forms/buttons. Page-map should still produce
    a well-shaped (non-error) result."""
    mid = f"test-{uuid.uuid4().hex}"

    async def go():
        try:
            out = await browse_url("https://example.com/", take_screenshot=False,
                                   upload_dir=tmp_path, mission_id=mid)
            assert not out.get("error"), out
            assert isinstance(out.get("links"), list)
            assert isinstance(out.get("buttons"), list)
            assert isinstance(out.get("forms"), list)
            # iana "Learn more" is the canonical link on this page
            assert any("Learn more" in (l.get("text") or "") for l in out.get("links") or [])
        finally:
            await cleanup_mission_page(mid)
    _run(go)
