"""Regression: asset library accepts the expanded file-type allowlist.

User asked that `.htm`, `.svg`, `.html`, `.xml`, `.rtf`, plus the usual
spreadsheet / slide / config / archive formats all upload cleanly to
the Asset Library and that HTML-shaped files have their tags stripped
for the agent prompt context.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from assets import (  # noqa: E402
    TEXT_LIKE_EXTS, IMAGE_EXTS, is_text_extractable, extract_text,
    _strip_html_like,
)


# ── Allowlist coverage ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "ext",
    # Common, real-world types users have asked to upload
    [
        "html", "htm", "xml", "svg", "rtf",
        "txt", "md", "csv", "json", "yaml", "yml", "toml",
        "log", "rss", "atom",
        "py", "js", "ts", "css", "sql", "sh", "env",
    ],
)
def test_text_like_includes_web_and_data_formats(ext):
    assert ext in TEXT_LIKE_EXTS or ext in IMAGE_EXTS, (
        f"{ext!r} should be uploadable as text-like or image"
    )


@pytest.mark.parametrize(
    "ext",
    ["html", "htm", "xml", "rtf"],
)
def test_text_extractable_for_web_and_rtf(ext):
    assert is_text_extractable(ext), (
        f"is_text_extractable should return True for .{ext}"
    )


# ── ALLOWED_ASSET_EXTS server-side allowlist ─────────────────────────


def test_server_allowlist_includes_expanded_types():
    """Make sure the upload-endpoint allowlist matches the user-facing
    accept= list. This is the gate users hit when posting to
    `/api/orgs/{id}/assets` — a missing entry here = 400."""
    # Import lazily because server.py is heavy and pulls Composio etc.
    import importlib
    mod = importlib.import_module("server")
    allowlist = mod.ALLOWED_ASSET_EXTS
    must_have = {
        # Web pages
        "html", "htm", "xml", "rss", "atom", "svg",
        # Spreadsheets / slides
        "xlsx", "xls", "pptx", "ppt",
        # Rich text
        "rtf",
        # Data formats
        "yaml", "yml", "toml", "tsv", "markdown",
        # Archives
        "tar", "gz", "tgz", "7z",
        # Image variants
        "bmp", "tiff", "heic", "avif", "ico",
        # Audio / video extras
        "aac", "ogg", "flac", "mkv", "avi",
    }
    missing = must_have - allowlist
    assert not missing, f"Server allowlist missing: {sorted(missing)}"


def test_server_kind_for_ext_classifies_new_types():
    import importlib
    mod = importlib.import_module("server")
    kind_for_ext = mod.kind_for_ext
    assert kind_for_ext("htm")  == "web"
    assert kind_for_ext("html") == "web"
    assert kind_for_ext("xml")  == "web"
    assert kind_for_ext("svg")  == "image"
    assert kind_for_ext("rtf")  == "doc"
    assert kind_for_ext("xlsx") == "spreadsheet"
    assert kind_for_ext("pptx") == "slides"
    assert kind_for_ext("tar")  == "archive"
    assert kind_for_ext("heic") == "image"
    assert kind_for_ext("mkv")  == "video"
    assert kind_for_ext("aac")  == "audio"


# ── HTML tag stripping ───────────────────────────────────────────────


def test_strip_html_removes_script_and_style_blocks():
    html = (
        "<html><head><title>Brand</title>"
        "<script>alert('noise')</script>"
        "<style>.x{color:red}</style></head>"
        "<body><h1>Hello</h1><p>World</p></body></html>"
    )
    out = _strip_html_like(html)
    assert "alert" not in out
    assert "color:red" not in out
    assert "Hello" in out
    assert "World" in out


def test_strip_html_decodes_entities():
    html = "<p>We make &amp; sell pistachios &#8212; the best.</p>"
    out = _strip_html_like(html)
    assert "&amp;" not in out
    assert "&" in out
    # "&#8212;" → "—" (em-dash)
    assert "—" in out


def test_strip_html_collapses_whitespace():
    html = "<p>One</p>\n\n\n\n<p>Two</p>"
    out = _strip_html_like(html)
    # No 4-newline runs in the output
    assert "\n\n\n" not in out


# ── extract_text dispatches to the right backend ─────────────────────


def test_extract_text_for_html(tmp_path):
    p = tmp_path / "page.htm"
    p.write_text(
        "<html><body><h1>Heading</h1>"
        "<p>Lorem <strong>ipsum</strong> dolor.</p></body></html>",
        encoding="utf-8",
    )
    text = extract_text(p, "htm")
    assert "Heading" in text
    assert "Lorem" in text
    assert "ipsum" in text
    assert "<strong>" not in text


def test_extract_text_for_xml(tmp_path):
    p = tmp_path / "feed.xml"
    p.write_text(
        "<?xml version='1.0'?><rss><channel><title>News</title>"
        "<item><title>First</title></item></channel></rss>",
        encoding="utf-8",
    )
    text = extract_text(p, "xml")
    assert "News" in text
    assert "First" in text
    assert "<title>" not in text


def test_extract_text_handles_unreadable_gracefully(tmp_path):
    """Non-existent file returns empty string instead of raising."""
    p = tmp_path / "missing.html"
    text = extract_text(p, "html")
    assert text == ""
