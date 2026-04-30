"""Brand assets — per-organization logo, color palette, and tagline.

Stored on the `organization` document under the `brand_assets` field:

    {
      "primary_logo": {"filename": "abc.png", "url": "/api/files/abc.png"},
      "alt_logo":     {"filename": "def.png", "url": "/api/files/def.png"} or None,
      "brand_colors": ["#F64000", "#1B1B1B"],
      "tagline":      "Distil the Soul of every business."
    }

These get used by the social-image generator (`compose_logo_on_image`) so
every drafted post carries the org's identity in the lower-right corner
(default; user can pick any of the 5 anchor positions).
"""
from __future__ import annotations

import io
import logging
import re
import uuid
from pathlib import Path
from typing import Literal, Optional

from PIL import Image
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

LOGO_KIND = Literal["primary", "alt"]
LOGO_POSITION = Literal["top-left", "top-right", "bottom-left", "bottom-right", "center"]
DEFAULT_LOGO_POSITION: LOGO_POSITION = "bottom-right"

# Brand-color regex — accepts #abc, #aabbcc, #aabbccdd
_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


class BrandUpdateBody(BaseModel):
    brand_colors: Optional[list[str]] = Field(default=None, max_length=8)
    tagline: Optional[str] = None


def normalise_colors(colors: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for c in colors or []:
        c = (c or "").strip()
        if not c:
            continue
        if not _HEX_RE.match(c):
            continue
        if not c.startswith("#"):
            c = f"#{c}"
        c = c.upper()
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out[:8]


def serialize_brand(org: dict) -> dict:
    ba = (org or {}).get("brand_assets") or {}
    return {
        "primary_logo": ba.get("primary_logo") or None,
        "alt_logo":     ba.get("alt_logo") or None,
        "brand_colors": ba.get("brand_colors") or [],
        "tagline":      ba.get("tagline") or "",
    }


def save_logo_bytes(
    upload_dir: Path, original_name: str, content: bytes,
) -> dict:
    """Persist an uploaded logo to ``upload_dir`` and return the doc to store.

    Re-encodes via Pillow so we get a sensible PNG even when users paste in
    huge JPGs or sketchy WEBPs. Output is RGBA so transparency carries through
    to the social-image overlay.
    """
    if not content:
        raise ValueError("Empty file")
    try:
        with Image.open(io.BytesIO(content)) as src:
            src.load()
            rgba = src.convert("RGBA")
            # Cap to 1024×1024 — larger doesn't help and bloats the assets dir
            rgba.thumbnail((1024, 1024), Image.LANCZOS)
            buf = io.BytesIO()
            rgba.save(buf, format="PNG", optimize=True)
            blob = buf.getvalue()
    except Exception as exc:
        raise ValueError(f"Couldn't decode the image: {exc}") from exc

    fname = f"logo-{uuid.uuid4().hex}.png"
    (upload_dir / fname).write_bytes(blob)
    return {
        "filename": fname,
        "url": f"/api/files/{fname}",
        "original_name": (original_name or "logo")[:200],
    }


def compose_logo_on_image(
    base_path: Path,
    logo_path: Path,
    position: LOGO_POSITION = DEFAULT_LOGO_POSITION,
    *,
    out_dir: Optional[Path] = None,
    margin_pct: float = 0.04,
    logo_pct: float = 0.18,
) -> Path:
    """Composite a logo onto a base image at one of 5 anchor positions.

    Returns the *new* image path (we always write a fresh file rather than
    mutating the base) so the original generated image stays untouched and
    re-positioning the logo is cheap.

    `logo_pct` controls the logo's longest side as a fraction of the base
    image's longest side (default 18% — visible but unobtrusive). `margin_pct`
    is the gap between the logo and the image edges.
    """
    out_dir = out_dir or base_path.parent
    with Image.open(base_path) as base, Image.open(logo_path) as logo:
        base_rgba = base.convert("RGBA")
        logo_rgba = logo.convert("RGBA")
        bw, bh = base_rgba.size
        target = int(max(bw, bh) * logo_pct)
        # Scale the logo so its longest side == target while preserving aspect
        lw, lh = logo_rgba.size
        scale = target / max(lw, lh)
        new_logo = logo_rgba.resize((max(1, int(lw * scale)), max(1, int(lh * scale))), Image.LANCZOS)
        nlw, nlh = new_logo.size
        margin = int(max(bw, bh) * margin_pct)
        if position == "top-left":
            xy = (margin, margin)
        elif position == "top-right":
            xy = (bw - nlw - margin, margin)
        elif position == "bottom-left":
            xy = (margin, bh - nlh - margin)
        elif position == "center":
            xy = ((bw - nlw) // 2, (bh - nlh) // 2)
        else:  # bottom-right (default)
            xy = (bw - nlw - margin, bh - nlh - margin)
        composed = base_rgba.copy()
        composed.alpha_composite(new_logo, dest=xy)
        out_name = f"composed-{uuid.uuid4().hex}.png"
        out_path = out_dir / out_name
        composed.convert("RGB").save(out_path, format="PNG", optimize=True)
        return out_path
