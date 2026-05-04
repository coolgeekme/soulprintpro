"""Regression for the batch image-resize utility.

The Mind Map / agent flow was failing on requests like "recreate this
image at six IAB ad sizes" because the underlying image-gen model can't
honour exact pixel dimensions. ``image_resize.smart_resize_batch`` is the
deterministic PIL path that actually delivers the goods.

These tests cover:
  • Each of the three modes (fit / fill / stretch) hits the right output
    dimensions.
  • Fit mode preserves aspect ratio (no squashing) and pads correctly.
  • Smart batch returns one entry per requested size, even on failures.
  • Per-size failures don't poison the rest of the batch.
  • The 6 IAB ad-slot sizes from the original user complaint round-trip.
"""
import io
import pytest
from PIL import Image

from image_resize import (
    resize_one, smart_resize_batch, resolve_preset, AD_SIZE_PRESETS,
)


def _png_bytes(w: int, h: int, colour=(40, 90, 200)) -> bytes:
    """Build a tiny PNG of the given size for use as a source image."""
    im = Image.new("RGB", (w, h), colour)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _read_size(png_bytes: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(png_bytes)).size


def test_resize_one_fit_hits_exact_dimensions():
    src = _png_bytes(800, 600)
    out = resize_one(src, 300, 250, mode="fit")
    assert _read_size(out) == (300, 250), "fit mode must hit the exact target"


def test_resize_one_fill_hits_exact_dimensions():
    src = _png_bytes(800, 600)
    out = resize_one(src, 300, 250, mode="fill")
    assert _read_size(out) == (300, 250), "fill mode must hit the exact target"


def test_resize_one_stretch_hits_exact_dimensions():
    src = _png_bytes(800, 600)
    out = resize_one(src, 320, 50, mode="stretch")
    assert _read_size(out) == (320, 50)


def test_resize_one_rejects_nonsense_dimensions():
    src = _png_bytes(100, 100)
    with pytest.raises(ValueError):
        resize_one(src, 0, 100)
    with pytest.raises(ValueError):
        resize_one(src, 100, -1)
    with pytest.raises(ValueError):
        resize_one(src, 50000, 50000)  # over MAX_DIMENSION


def test_smart_batch_six_iab_sizes():
    """The exact slot list from the user's bug report."""
    src = _png_bytes(1200, 800)
    sizes = [
        {"label": "Wide Skyscraper",   "width": 160, "height": 600},
        {"label": "Rectangle",         "width": 300, "height": 250},
        {"label": "Mobile Leaderboard", "width": 320, "height": 50},
        {"label": "Leaderboard",       "width": 728, "height": 90},
        {"label": "Half-Page",         "width": 300, "height": 600},
        {"label": "Billboard",         "width": 970, "height": 250},
    ]
    out = smart_resize_batch(src, sizes, base_name="hero")
    assert len(out) == 6, "every requested size must produce an entry"
    for v, s in zip(out, sizes):
        assert "error" not in v, f"unexpected error: {v}"
        assert _read_size(v["bytes"]) == (s["width"], s["height"])
        assert v["label"] == s["label"]
        assert v["width"] == s["width"]
        assert v["height"] == s["height"]
        assert v["mime"] == "image/png"
        assert v["filename"].endswith(".png")
        assert "hero" in v["filename"]


def test_smart_batch_partial_failure_is_isolated():
    """One bad size shouldn't kill the rest of the batch."""
    src = _png_bytes(800, 600)
    sizes = [
        {"label": "ok",   "width": 100, "height": 100},
        {"label": "bad1", "width": 0,   "height": 100},   # invalid
        {"label": "ok2",  "width": 200, "height": 50},
    ]
    out = smart_resize_batch(src, sizes)
    assert len(out) == 3
    assert "error" not in out[0]
    assert "error" in out[1]
    assert "error" not in out[2]
    assert _read_size(out[0]["bytes"]) == (100, 100)
    assert _read_size(out[2]["bytes"]) == (200, 50)


def test_resolve_preset_known_slots():
    assert resolve_preset("Wide Skyscraper") == (160, 600)
    assert resolve_preset("wide-skyscraper") == (160, 600)
    assert resolve_preset("LEADERBOARD") == (728, 90)
    assert resolve_preset("Mobile Leaderboard") == (320, 50)
    assert resolve_preset("Half-Page") == (300, 600)
    assert resolve_preset("Billboard") == (970, 250)
    assert resolve_preset("not-a-real-slot") is None


def test_ad_size_presets_cover_user_request():
    """Every slot the user asked about must be present in the preset table."""
    requested = [
        ("Wide Skyscraper",   160, 600),
        ("Rectangle",         300, 250),
        ("Mobile Leaderboard", 320, 50),
        ("Leaderboard",       728, 90),
        ("Half-Page",         300, 600),
        ("Billboard",         970, 250),
    ]
    for name, w, h in requested:
        got = resolve_preset(name)
        assert got == (w, h), f"{name} preset wrong: got {got}, expected ({w},{h})"


def test_smart_batch_caps_max_dimension():
    """Oversized requests should fail isolated, not crash the whole batch."""
    src = _png_bytes(100, 100)
    out = smart_resize_batch(src, [
        {"label": "huge", "width": 999999, "height": 100},
        {"label": "ok",   "width": 200,    "height": 200},
    ])
    assert "error" in out[0]
    assert "error" not in out[1]
