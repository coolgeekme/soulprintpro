"""image_resize.py — Batch image resize utility for ad/banner sizes.

The site's existing image flow (`generate_image_via_nano_banana`) only emits
ONE image per call and the underlying model can't honour exact pixel
dimensions like 728×90 or 160×600. So when a user uploads a hero image and
asks for it to be re-rendered at six standard ad slots, the agent stalls
because there's no tool that can deliver the deliverable.

This module provides a deterministic PIL-based batch resize:

  • ``resize_one(img_bytes, target_w, target_h, mode)`` → PNG bytes at
    *exact* target dimensions. Three modes (see below).
  • ``smart_resize_batch(img_bytes, sizes)`` → list of ``(filename, label,
    bytes, w, h, mode)`` tuples ready to be persisted as artifacts.

Modes:
  • ``fit``   — scale-to-contain + pad with averaged-edge colour. No
                content cropped. Best for product shots / hero images
                where the whole subject must remain visible.
  • ``fill``  — scale-to-cover + center-crop. Fills the canvas edge to
                edge with no padding. Best for backgrounds / textures.
  • ``stretch`` — brute force resize ignoring aspect ratio. Use only when
                the user explicitly wants distortion.

Default mode is ``fit`` — that's what users expect when re-purposing one
hero image across multiple ad slots: "show me my whole image at this size,
add a clean background where it doesn't fill."
"""
from __future__ import annotations

import logging
import re
from io import BytesIO
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


# Hard cap so a malicious or accidental request like 50000×50000 doesn't
# eat all our memory. 8K is plenty for any ad/banner use case.
MAX_DIMENSION = 8192


def _slugify(s: str, fallback: str = "size") -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (s or "").strip()).strip("-")
    return (s or fallback).lower()[:60]


def _averaged_edge_colour(im) -> tuple[int, int, int]:
    """Sample the outer 2px border of the image and average it. Used as
    the pad fill so letterboxed ads blend with the source colour palette
    instead of an obvious white/black bar. Cheap (8 samples)."""
    w, h = im.size
    if w < 4 or h < 4:
        # Too small to sample a meaningful edge — punt to neutral grey.
        return (235, 235, 235)
    rgb = im.convert("RGB")
    samples = [
        rgb.getpixel((1, 1)),
        rgb.getpixel((w - 2, 1)),
        rgb.getpixel((1, h - 2)),
        rgb.getpixel((w - 2, h - 2)),
        rgb.getpixel((w // 2, 1)),
        rgb.getpixel((w // 2, h - 2)),
        rgb.getpixel((1, h // 2)),
        rgb.getpixel((w - 2, h // 2)),
    ]
    r = sum(p[0] for p in samples) // len(samples)
    g = sum(p[1] for p in samples) // len(samples)
    b = sum(p[2] for p in samples) // len(samples)
    return (r, g, b)


def resize_one(
    img_bytes: bytes,
    target_w: int,
    target_h: int,
    mode: str = "fit",
    *,
    pad_colour: Optional[tuple[int, int, int]] = None,
) -> bytes:
    """Return PNG bytes resized exactly to (target_w, target_h)."""
    from PIL import Image  # local import — Pillow is already a hard dep
    if target_w <= 0 or target_h <= 0:
        raise ValueError("target dimensions must be positive integers")
    if target_w > MAX_DIMENSION or target_h > MAX_DIMENSION:
        raise ValueError(
            f"target dimensions exceed the safety cap ({MAX_DIMENSION}px)"
        )
    src = Image.open(BytesIO(img_bytes))
    src.load()  # force decode now so EXIF auto-rotation can apply
    try:
        from PIL import ImageOps
        src = ImageOps.exif_transpose(src)
    except Exception:
        pass

    mode = (mode or "fit").lower()
    if mode == "stretch":
        out = src.resize((target_w, target_h), Image.LANCZOS)
    elif mode == "fill":
        # Cover the canvas edge-to-edge, then center-crop overflow.
        sw, sh = src.size
        scale = max(target_w / sw, target_h / sh)
        scaled = src.resize((max(1, round(sw * scale)), max(1, round(sh * scale))),
                            Image.LANCZOS)
        sw2, sh2 = scaled.size
        left = (sw2 - target_w) // 2
        top = (sh2 - target_h) // 2
        out = scaled.crop((left, top, left + target_w, top + target_h))
    else:
        # Default `fit` — scale to contain + pad. Choose pad colour from
        # the source's edge palette so it blends with the source.
        sw, sh = src.size
        scale = min(target_w / sw, target_h / sh)
        new_w = max(1, round(sw * scale))
        new_h = max(1, round(sh * scale))
        scaled = src.resize((new_w, new_h), Image.LANCZOS)
        if pad_colour is None:
            pad_colour = _averaged_edge_colour(src)
        canvas = Image.new("RGB", (target_w, target_h), pad_colour)
        offset = ((target_w - new_w) // 2, (target_h - new_h) // 2)
        # Preserve transparency where possible
        if scaled.mode == "RGBA":
            canvas.paste(scaled, offset, mask=scaled.split()[3])
        else:
            canvas.paste(scaled.convert("RGB"), offset)
        out = canvas

    # Always emit PNG — ad-tech accepts PNG everywhere and we need lossless
    # rescaling so the user can drop these straight into a creative server.
    if out.mode not in ("RGB", "RGBA"):
        out = out.convert("RGB")
    buf = BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def smart_resize_batch(
    img_bytes: bytes,
    sizes: Iterable[dict],
    *,
    base_name: str = "image",
) -> list[dict]:
    """Resize one source image into many target sizes.

    Each ``size`` dict accepts: ``label`` (free-form, defaults to e.g.
    "300x250"), ``width`` (int, required), ``height`` (int, required),
    ``mode`` (optional — fit | fill | stretch).

    Returns a list of dicts: ``{filename, label, width, height, mode,
    bytes, mime}``. Items that fail validation are skipped with an
    ``error`` field instead so the caller can surface partial failures
    without aborting the whole batch.
    """
    out: list[dict] = []
    if not img_bytes:
        return out
    for spec in sizes:
        try:
            w = int(spec.get("width") or 0)
            h = int(spec.get("height") or 0)
            mode = (spec.get("mode") or "fit").lower()
            label = (spec.get("label") or f"{w}x{h}").strip()
            slug = _slugify(label, fallback=f"{w}x{h}")
            filename = f"{_slugify(base_name)}-{slug}-{w}x{h}.png"
            png = resize_one(img_bytes, w, h, mode=mode)
            out.append({
                "filename": filename,
                "label": label,
                "width": w,
                "height": h,
                "mode": mode,
                "bytes": png,
                "mime": "image/png",
            })
        except Exception as exc:
            logger.warning("smart_resize_batch failed for %s: %s", spec, exc)
            out.append({
                "label": (spec.get("label") or f"{spec.get('width')}x{spec.get('height')}"),
                "width": spec.get("width"),
                "height": spec.get("height"),
                "error": str(exc)[:240],
            })
    return out


# Pre-baked ad slot dictionary — covers the 6 IAB sizes the user asked
# about plus a few common social slots. Agents can reference these names
# in prompts (e.g. "resize for IAB Wide Skyscraper") and we'll look up
# the dimensions automatically.
AD_SIZE_PRESETS: dict[str, tuple[int, int]] = {
    # IAB display
    "wide skyscraper":   (160, 600),
    "skyscraper":        (120, 600),
    "rectangle":         (300, 250),
    "medium rectangle":  (300, 250),
    "large rectangle":   (336, 280),
    "leaderboard":       (728, 90),
    "mobile leaderboard": (320, 50),
    "billboard":         (970, 250),
    "half-page":         (300, 600),
    "halfpage":          (300, 600),
    "half page":         (300, 600),
    "square":            (250, 250),
    # Social slots
    "instagram square":  (1080, 1080),
    "instagram story":   (1080, 1920),
    "twitter post":      (1600, 900),
    "linkedin post":     (1200, 627),
    "facebook post":     (1200, 630),
    "youtube thumbnail": (1280, 720),
}


def resolve_preset(name: str) -> Optional[tuple[int, int]]:
    """Look up a friendly slot name (case- and dash-insensitive)."""
    key = re.sub(r"[\s_\-]+", " ", (name or "").strip().lower())
    return AD_SIZE_PRESETS.get(key)
