"""Tests for org-logo injection into [[IMAGE]] block generation.

Verifies the Feb 2026 fix where Telegram-side image generation (and
chat-side too) now passes the org's brand_assets.primary_logo (and
alt_logo if present) as a reference image to Nano Banana so generated
visuals carry the brand mark.

The test patches `generate_image_via_nano_banana` to a stub that
captures the `input_image_paths` argument, drives
`_generate_image_attachments` against an in-memory mock DB, and asserts
the logo path made it through.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _png_bytes() -> bytes:
    from PIL import Image
    import io
    img = Image.new("RGBA", (64, 64), (10, 200, 50, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_image_generation_passes_org_logo_as_reference(tmp_path, monkeypatch):
    """The pipeline must read org.brand_assets.primary_logo.filename,
    resolve it on disk (UPLOAD_DIR), and pass that Path to Nano Banana
    via the ``input_image_paths`` kwarg.
    """
    # Late import so monkeypatched UPLOAD_DIR is honoured.
    import server  # noqa: E402

    # Redirect UPLOAD_DIR + DOCS_DIR to tmp so the test never touches
    # real artifacts.
    fake_upload = tmp_path / "uploads"
    fake_docs = tmp_path / "docs"
    fake_upload.mkdir()
    fake_docs.mkdir()
    monkeypatch.setattr(server, "UPLOAD_DIR", fake_upload)
    monkeypatch.setattr(server, "DOCS_DIR", fake_docs)

    # Write a fake brand logo on disk.
    logo_filename = f"{uuid.uuid4().hex}.png"
    (fake_upload / logo_filename).write_bytes(_png_bytes())

    org_id = "65f0123456789abcdef01234"
    fake_org_doc = {
        "_id": __import__("bson").ObjectId(org_id),
        "name": "AZ Pain Doctors",
        "brand_assets": {
            "primary_logo": {"filename": logo_filename, "url": f"/api/files/{logo_filename}"},
        },
    }

    # ── Build a tiny async DB stub with just the methods _generate_image_attachments uses ──
    class FakeCollection:
        def __init__(self, find_one_result=None):
            self._find_one_result = find_one_result
            self.inserted = []

        async def find_one(self, *args, **kwargs):
            return self._find_one_result

        async def insert_one(self, doc):
            self.inserted.append(doc)
            return type("X", (), {"inserted_id": "fake"})()

    class FakeDB:
        organizations = FakeCollection(find_one_result=fake_org_doc)
        documents = FakeCollection()
        media_files = FakeCollection()

    monkeypatch.setattr(server, "db", FakeDB())

    # Capture what gets passed to Nano Banana.
    captured = {}

    async def fake_nano(prompt, system_msg, input_image_paths=None):
        captured["prompt"] = prompt
        captured["system_msg"] = system_msg
        captured["input_image_paths"] = input_image_paths
        out_fname = f"{uuid.uuid4().hex}.png"
        (fake_upload / out_fname).write_bytes(_png_bytes())
        return out_fname, ""

    monkeypatch.setattr(server, "generate_image_via_nano_banana", fake_nano)

    # GridFS mirror should not be needed because disk file exists; stub
    # the helpers anyway in case the code path changes.
    async def _no_gridfs(*args, **kwargs):
        return None

    monkeypatch.setattr(server, "recover_bytes_from_gridfs", AsyncMock(return_value=(None, None, None)))

    # Drive the pipeline.
    image_blocks = [
        {"prompt": "An IG post about same-day appointments", "aspect": "1:1",
         "model": "nano-banana", "title": "Hero post"},
    ]
    conv = {"_id": "conv1", "team_id": "team1"}
    team = {"org_id": org_id, "name": "AZ Pain Team", "soulprint": {}}

    out = await server._generate_image_attachments(
        image_blocks,
        conv=conv, team=team, agent_id="specialized-chief-of-staff",
        message_id="msg1", created_by="user1",
    )

    # The Nano Banana stub must have received the logo path.
    paths = captured.get("input_image_paths")
    assert paths is not None, "logo path not passed to Nano Banana"
    assert any(p.name == logo_filename for p in paths), (
        f"primary_logo filename {logo_filename!r} not in input_image_paths={paths!r}"
    )

    # System message should include the BRAND LOGO directive so the
    # model knows the reference image IS the logo.
    sysmsg = captured.get("system_msg") or ""
    assert "BRAND LOGO" in sysmsg, f"system_msg missing brand-logo directive: {sysmsg!r}"
    assert "AZ Pain Doctors" in sysmsg, "brand name not in system_msg"

    # Pipeline should still return one attachment.
    assert len(out) == 1
    assert out[0]["kind"] == "image"


@pytest.mark.asyncio
async def test_image_generation_works_when_org_has_no_logo(tmp_path, monkeypatch):
    """No logo on the org → input_image_paths should be None (not [])
    so Nano Banana's text-only path runs without a stale empty list."""
    import server  # noqa: E402

    fake_upload = tmp_path / "uploads"
    fake_docs = tmp_path / "docs"
    fake_upload.mkdir()
    fake_docs.mkdir()
    monkeypatch.setattr(server, "UPLOAD_DIR", fake_upload)
    monkeypatch.setattr(server, "DOCS_DIR", fake_docs)

    org_id = "65f0123456789abcdef01234"
    fake_org_doc = {
        "_id": __import__("bson").ObjectId(org_id),
        "name": "No-Logo Co",
        # No brand_assets at all.
    }

    class FakeCollection:
        def __init__(self, find_one_result=None):
            self._find_one_result = find_one_result

        async def find_one(self, *args, **kwargs):
            return self._find_one_result

        async def insert_one(self, doc):
            return type("X", (), {"inserted_id": "fake"})()

    class FakeDB:
        organizations = FakeCollection(find_one_result=fake_org_doc)
        documents = FakeCollection()
        media_files = FakeCollection()

    monkeypatch.setattr(server, "db", FakeDB())

    captured = {}

    async def fake_nano(prompt, system_msg, input_image_paths=None):
        captured["input_image_paths"] = input_image_paths
        captured["system_msg"] = system_msg
        out_fname = f"{uuid.uuid4().hex}.png"
        (fake_upload / out_fname).write_bytes(_png_bytes())
        return out_fname, ""

    monkeypatch.setattr(server, "generate_image_via_nano_banana", fake_nano)
    monkeypatch.setattr(server, "recover_bytes_from_gridfs", AsyncMock(return_value=(None, None, None)))

    image_blocks = [{"prompt": "neutral test", "aspect": "1:1", "model": "nano-banana", "title": "x"}]
    conv = {"_id": "conv1", "team_id": "team1"}
    team = {"org_id": org_id, "name": "No-Logo Co", "soulprint": {}}

    out = await server._generate_image_attachments(
        image_blocks,
        conv=conv, team=team, agent_id="specialized-chief-of-staff",
        message_id="msg1", created_by="user1",
    )

    assert captured["input_image_paths"] is None, (
        f"expected None when no logo, got {captured['input_image_paths']!r}"
    )
    # No BRAND LOGO directive should be present.
    assert "BRAND LOGO" not in (captured["system_msg"] or "")
    assert len(out) == 1
