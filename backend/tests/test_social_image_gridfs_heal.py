"""Regression test for the Social Queue composed-image disappearance bug.

Bug history (Jun 2026): users reported that scheduled Social Queue posts
showed images yesterday but not today. Root cause: `compose_logo_on_image`
wrote the composited cover to disk but never mirrored it to GridFS. When
the production pod redeployed (or hit ephemeral-storage GC) the disk file
vanished and `/api/files/<composed>.png` 404'd with no recovery path.

Fix (two-part):
  1. Forward — `_mirror_composed_to_gridfs` runs after every
     `compose_logo_on_image` call so the composite IS mirrored.
  2. Heal-on-read — `serve_file` (in server.py) detects a missing
     composite, looks up the social_posts doc by `image.filename`,
     recovers `raw_filename` from GridFS, re-composites the org's
     current primary logo on the fly, persists the new composite,
     and rewrites the post.

This test pins the helper logic without standing up a full HTTP server
— it exercises the recompose-from-raw path with mocked DB + GridFS.
"""
from __future__ import annotations

import io
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image

from brand import compose_logo_on_image


@pytest.fixture
def tmp_uploads(tmp_path: Path) -> Path:
    d = tmp_path / "uploads"
    d.mkdir()
    return d


def _png_bytes(size=(64, 64), color=(200, 100, 50, 255)) -> bytes:
    """Build a tiny in-memory PNG so tests don't need network or disk fixtures."""
    img = Image.new("RGBA", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_compose_logo_returns_new_file(tmp_uploads: Path) -> None:
    """Baseline — the underlying compose helper does write a new file.

    Confirms the test fixture works before we move on to the heal path.
    """
    base = tmp_uploads / "raw-base.png"
    logo = tmp_uploads / "primary-logo.png"
    base.write_bytes(_png_bytes(size=(256, 256), color=(20, 30, 40, 255)))
    logo.write_bytes(_png_bytes(size=(64, 64), color=(255, 255, 0, 255)))

    composed = compose_logo_on_image(base, logo, position="bottom-right",
                                     out_dir=tmp_uploads)
    assert composed.is_file()
    assert composed.name.startswith("composed-")
    # Base/logo untouched
    assert base.is_file() and logo.is_file()


@pytest.mark.asyncio
async def test_heal_recomposites_from_raw_when_composite_lost(tmp_uploads: Path) -> None:
    """End-to-end of the heal flow with mocked DB + GridFS.

    Simulates: composite filename is missing on disk AND in GridFS, but
    `raw_filename` IS in GridFS, plus the org has a primary logo whose
    bytes ARE in GridFS. The heal helper should:
      1. Recover raw bytes
      2. Recover logo bytes
      3. Re-composite to a NEW file
      4. Persist + update the post
    """
    raw_name = f"{uuid.uuid4().hex}.png"
    logo_name = f"logo-{uuid.uuid4().hex}.png"
    composed_name = f"composed-{uuid.uuid4().hex}.png"
    raw_bytes = _png_bytes(size=(512, 512), color=(15, 25, 35, 255))
    logo_bytes = _png_bytes(size=(96, 96), color=(255, 200, 0, 255))

    # Mock the GridFS recovery so the requested filename misses but the
    # raw + logo names hit.
    async def fake_recover(name):
        if name == raw_name:
            return raw_bytes, "image/png", raw_name
        if name == logo_name:
            return logo_bytes, "image/png", logo_name
        return None, None, None

    sp_doc = {
        "_id": "post-1",
        "org_id": "5f5d3c4b2a1e9f8a7b6c5d4e",  # valid 24-char hex
        "image": {
            "filename": composed_name,
            "raw_filename": raw_name,
            "with_logo": True,
        },
        "logo_position": "bottom-right",
    }

    org_doc = {
        "_id": "5f5d3c4b2a1e9f8a7b6c5d4e",
        "brand_assets": {"primary_logo": {"filename": logo_name}},
    }

    # We import the heal helper from server.py — this is the public
    # symbol the serve_file fallback uses.
    with patch("server.UPLOAD_DIR", tmp_uploads), \
         patch("server.recover_bytes_from_gridfs", side_effect=fake_recover), \
         patch("server.persist_bytes", new=AsyncMock(return_value="gridfs-id-1")), \
         patch("server.db") as mock_db:
        mock_db.organizations.find_one = AsyncMock(return_value=org_doc)
        mock_db.social_posts.update_one = AsyncMock()
        from server import _heal_social_post_image  # imported inside patch ctx
        result = await _heal_social_post_image(sp_doc, composed_name)

    assert result is not None, "heal helper returned None — should have recomposed"
    healed_path, mime = result
    assert healed_path.is_file()
    assert mime == "image/png"
    # The healed file is a fresh composite (different name from the lost one)
    assert healed_path.name != composed_name
    assert healed_path.name.startswith("composed-")
    # Raw rehydrated to disk for future use
    assert (tmp_uploads / raw_name).is_file()


@pytest.mark.asyncio
async def test_heal_returns_raw_when_no_logo_available(tmp_uploads: Path) -> None:
    """When the org has no primary logo, heal serves the raw bytes under
    the composed URL — better to show an un-branded image than 404.
    """
    raw_name = f"{uuid.uuid4().hex}.png"
    composed_name = f"composed-{uuid.uuid4().hex}.png"
    raw_bytes = _png_bytes()

    async def fake_recover(name):
        if name == raw_name:
            return raw_bytes, "image/png", raw_name
        return None, None, None

    sp_doc = {
        "_id": "post-2",
        "org_id": "5f5d3c4b2a1e9f8a7b6c5d4e",
        "image": {"filename": composed_name, "raw_filename": raw_name},
    }
    org_doc = {"_id": "5f5d3c4b2a1e9f8a7b6c5d4e", "brand_assets": {}}

    with patch("server.UPLOAD_DIR", tmp_uploads), \
         patch("server.recover_bytes_from_gridfs", side_effect=fake_recover), \
         patch("server.persist_bytes", new=AsyncMock()), \
         patch("server.db") as mock_db:
        mock_db.organizations.find_one = AsyncMock(return_value=org_doc)
        mock_db.social_posts.update_one = AsyncMock()
        from server import _heal_social_post_image
        result = await _heal_social_post_image(sp_doc, composed_name)

    assert result is not None
    healed_path, mime = result
    # Falls back to serving the raw image
    assert healed_path.name == raw_name
    assert mime == "image/png"


@pytest.mark.asyncio
async def test_heal_returns_none_when_raw_also_gone(tmp_uploads: Path) -> None:
    """If both composite AND raw are gone from GridFS, the post is
    genuinely unrecoverable and the helper signals 404.
    """
    composed_name = f"composed-{uuid.uuid4().hex}.png"
    raw_name = f"{uuid.uuid4().hex}.png"
    sp_doc = {
        "_id": "post-3",
        "org_id": "5f5d3c4b2a1e9f8a7b6c5d4e",
        "image": {"filename": composed_name, "raw_filename": raw_name},
    }

    async def fake_recover(_name):
        return None, None, None

    with patch("server.UPLOAD_DIR", tmp_uploads), \
         patch("server.recover_bytes_from_gridfs", side_effect=fake_recover), \
         patch("server.persist_bytes", new=AsyncMock()), \
         patch("server.db"):
        from server import _heal_social_post_image
        result = await _heal_social_post_image(sp_doc, composed_name)

    assert result is None
