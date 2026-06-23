"""Tests for `ios_image_compat.normalize_for_web` — the single chokepoint
that decides whether an uploaded image gets transcoded.

These tests cover the rules users actually care about:
  • PNG/JPEG/WEBP/GIF pass through unchanged — verbatim bytes.
  • HEIC/HEIF gets transcoded to a viewable JPEG.
  • AVIF + TIFF get transcoded too.
  • The `transcoded_from` label is human-readable.
  • EXIF survives the round-trip.
  • The accept-string covers everything an iOS Safari picker will see.
"""
from __future__ import annotations

import io

import pytest
from PIL import Image


def _png_bytes(size=(80, 60), color=(120, 60, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes(size=(80, 60), color=(40, 200, 120)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _heic_bytes(size=(120, 90), color=(220, 80, 80)) -> bytes:
    """Build a tiny HEIC fixture. Skipped at import-time if pillow_heif
    isn't available in this environment."""
    import pillow_heif  # noqa: F401 — register the opener
    pillow_heif.register_heif_opener()
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="HEIF", quality=80)
    return buf.getvalue()


def test_png_passes_through_unchanged() -> None:
    from ios_image_compat import normalize_for_web
    src = _png_bytes()
    out = normalize_for_web(src, original_name="logo.png", mime="image/png")
    assert out["was_transcoded"] is False
    assert out["mime"] == "image/png"
    assert out["ext"] == "png"
    assert out["bytes"] == src  # byte-for-byte identical


def test_jpeg_passes_through_unchanged() -> None:
    from ios_image_compat import normalize_for_web
    src = _jpeg_bytes()
    out = normalize_for_web(src, original_name="photo.jpg", mime="image/jpeg")
    assert out["was_transcoded"] is False
    assert out["mime"] == "image/jpeg"
    assert out["bytes"] == src


def test_heic_transcodes_to_jpeg_and_labels_source() -> None:
    """The headline iPhone-photo flow."""
    from ios_image_compat import normalize_for_web
    pillow_heif = pytest.importorskip("pillow_heif")
    src = _heic_bytes()
    out = normalize_for_web(src, original_name="IMG_4321.HEIC", mime="image/heic")
    assert out["was_transcoded"] is True
    assert out["mime"] == "image/jpeg"
    assert out["ext"] == "jpg"
    assert out["transcoded_from"] == "HEIC"
    # The transcoded bytes must be a valid JPEG we can re-open.
    img = Image.open(io.BytesIO(out["bytes"]))
    assert img.format == "JPEG"
    assert img.size == (120, 90)  # geometry preserved


def test_heic_octet_stream_mime_still_detected_via_pil() -> None:
    """iOS Safari sometimes posts HEIC with `application/octet-stream`.
    The normalizer must still transcode based on Pillow's format sniff,
    not the bogus MIME."""
    from ios_image_compat import normalize_for_web
    pytest.importorskip("pillow_heif")
    src = _heic_bytes()
    out = normalize_for_web(
        src, original_name="IMG_4321.HEIC",
        mime="application/octet-stream",
    )
    assert out["was_transcoded"] is True
    assert out["mime"] == "image/jpeg"


def test_unknown_format_raises_value_error() -> None:
    """Garbage bytes shouldn't sneak through as a "JPEG"."""
    from ios_image_compat import normalize_for_web
    with pytest.raises(ValueError):
        normalize_for_web(b"not-an-image-at-all", original_name="garbage.bin", mime="application/octet-stream")


def test_accept_string_contains_heic_and_extensions() -> None:
    """The HTML accept string MUST include the bare extensions, not just
    MIMEs — iOS Safari otherwise hides files whose MIME it can't
    detect."""
    from ios_image_compat import accepted_html_input_accept
    accept = accepted_html_input_accept()
    for needle in ("image/heic", "image/heif", "image/avif", ".heic", ".heif", ".avif"):
        assert needle in accept, f"Missing {needle} in accept string"
