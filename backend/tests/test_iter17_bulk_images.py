"""Iteration 17 — Bulk-image fix + binary-artifact rejection.

Covers:
  - _safe_filename() refuses binary extensions (returns ("","") tuple)
  - extract_artifact_blocks() drops binary blocks AND adds a user-visible warning
  - detect_image_count() parses "Make 5 images" / "Generate 10 thumbnails" / etc.
  - _generate_bulk_images() inserts N agent messages with media_filename
  - Regression: legacy single-image asks still produce count=1
  - Regression: text-only chats don't trigger image intent
"""
from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from motor.motor_asyncio import AsyncIOMotorClient

# Module under test
import artifacts  # /app/backend/artifacts.py
import server   # /app/backend/server.py


@pytest.fixture
async def fresh_db():
    """Fresh motor client bound to the current test's event loop.
    Avoids 'event loop is closed' across tests since server.db was bound
    to the import-time loop."""
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    yield db
    client.close()


# ─────────────────────────────────────────────────────────────────────────────
# _safe_filename — binary extensions are refused
# ─────────────────────────────────────────────────────────────────────────────
class TestSafeFilename:
    def test_png_returns_empty_tuple(self):
        assert artifacts._safe_filename("hero.png") == ("", "")

    def test_pdf_returns_empty_tuple(self):
        assert artifacts._safe_filename("report.pdf") == ("", "")

    def test_mp4_returns_empty_tuple(self):
        assert artifacts._safe_filename("clip.mp4") == ("", "")

    def test_md_passes_through(self):
        name, ext = artifacts._safe_filename("brief.md")
        assert name == "brief.md"
        assert ext == "md"

    def test_csv_passes_through(self):
        name, ext = artifacts._safe_filename("data.csv")
        assert name == "data.csv"
        assert ext == "csv"

    def test_unknown_ext_coerced_to_txt(self):
        name, ext = artifacts._safe_filename("notes.foobar")
        assert ext == "txt"
        assert name.endswith(".txt")

    @pytest.mark.parametrize(
        "filename",
        ["a.jpg", "b.jpeg", "c.gif", "d.webp", "e.svg", "f.mp3", "g.docx",
         "h.xlsx", "i.zip", "j.mov", "k.heic", "l.avif"],
    )
    def test_all_binary_exts_refused(self, filename):
        assert artifacts._safe_filename(filename) == ("", "")


# ─────────────────────────────────────────────────────────────────────────────
# extract_artifact_blocks — binary blocks dropped + user-visible warning
# ─────────────────────────────────────────────────────────────────────────────
class TestExtractArtifactBlocks:
    def test_drops_binary_blocks_and_appends_warning(self):
        text = (
            "Here are your hero images.\n\n"
            '[[ARTIFACT name="hero1.png" mime="image/png"]]\n'
            "fakebase64data1\n"
            "[[/ARTIFACT]]\n\n"
            '[[ARTIFACT name="hero2.png" mime="image/png"]]\n'
            "fakebase64data2\n"
            "[[/ARTIFACT]]\n\n"
            '[[ARTIFACT name="brief.md" mime="text/markdown"]]\n'
            "# Brief\nthis is a real text artifact\n"
            "[[/ARTIFACT]]\n"
        )
        cleaned, blocks = artifacts.extract_artifact_blocks(text)
        # Only the .md block is saved
        assert len(blocks) == 1
        assert blocks[0]["name"] == "brief.md"
        assert blocks[0]["ext"] == "md"
        # Warning includes both rejected png filenames
        assert "I tried to emit binary file(s)" in cleaned
        assert "hero1.png" in cleaned
        assert "hero2.png" in cleaned
        # Warning emoji present
        assert "⚠️" in cleaned

    def test_no_warning_when_only_text_blocks(self):
        text = (
            "Plan attached.\n\n"
            '[[ARTIFACT name="plan.md"]]\n'
            "# Plan\nstep 1\n"
            "[[/ARTIFACT]]\n"
        )
        cleaned, blocks = artifacts.extract_artifact_blocks(text)
        assert len(blocks) == 1
        assert "binary file" not in cleaned

    def test_passthrough_when_no_artifact_blocks(self):
        cleaned, blocks = artifacts.extract_artifact_blocks("Just regular prose.")
        assert blocks == []
        assert cleaned == "Just regular prose."


# ─────────────────────────────────────────────────────────────────────────────
# detect_image_count — bulk parser
# ─────────────────────────────────────────────────────────────────────────────
class TestDetectImageCount:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Make 5 images", 5),
            ("Generate 10 thumbnails", 8),  # capped at MAX_BULK_IMAGES
            ("Create 3 variations of our logo", 3),
            ("design 100 thumbnails", 8),    # clamped
            ("I need an image for my homepage", 1),
            ("Hello there", 1),
            ("Create an Instagram post", 1),
            ("design a logo for my coffee shop", 1),
            ("What's the capital of France?", 1),
            ("give me 4 hero images", 4),
            ("render 2 banner options", 2),
        ],
    )
    def test_count_extraction(self, text, expected):
        assert server.detect_image_count(text) == expected

    def test_max_bulk_constant(self):
        # Ensure the hard ceiling really is 8.
        assert server.MAX_BULK_IMAGES == 8


# ─────────────────────────────────────────────────────────────────────────────
# detect_image_intent — text-only regression
# ─────────────────────────────────────────────────────────────────────────────
class TestImageIntentRegression:
    def test_text_only_chat_no_image_intent(self):
        # Pure-text question must NOT short-circuit to image generation via
        # the deterministic pre-classifier.
        assert server._quick_image_intent("What's the capital of France?") in (False, None)

    def test_obvious_image_ask_triggers(self):
        assert server._quick_image_intent("create an instagram post") is True


# ─────────────────────────────────────────────────────────────────────────────
# _generate_bulk_images — mocked image generator, real Mongo
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_generate_bulk_images_inserts_n_messages(fresh_db):
    """Patch nano-banana to return a deterministic tuple, then assert that
    exactly `count` agent messages get inserted with proper media_* fields
    and per-image '(i of n)' markers."""
    test_conv_id = f"TEST_conv_{uuid.uuid4().hex[:8]}"

    async def _fake_gen(prompt, sys_msg):
        # Pretend the model returned a real file + caption.
        return (f"img_{uuid.uuid4().hex[:8]}.png", "draft caption")

    try:
        with patch.object(server, "generate_image_via_nano_banana",
                          side_effect=_fake_gen):
            rendered = await server._generate_bulk_images(
                db=fresh_db,
                conv_id=test_conv_id,
                primary_agent_id="ada",
                primary_agent_name="Ada",
                user_text="create 3 hero images for our launch",
                sys_msg="test sys",
                count=3,
            )
        assert rendered == 3

        # Verify persistence — exactly 3 messages, each tagged with "(i of 3)"
        cursor = fresh_db.messages.find({"conversation_id": test_conv_id})
        msgs = []
        async for m in cursor:
            msgs.append(m)
        assert len(msgs) == 3
        texts = sorted([m["text"] for m in msgs])
        assert any("(1 of 3)" in t for t in texts)
        assert any("(2 of 3)" in t for t in texts)
        assert any("(3 of 3)" in t for t in texts)
        # Each must carry image media metadata
        for m in msgs:
            assert m["media_type"] == "image"
            assert m["media_filename"].endswith(".png")
            assert m["media_url"].startswith("/api/files/")
            assert m["sender_type"] == "agent"
    finally:
        # Cleanup
        await fresh_db.messages.delete_many({"conversation_id": test_conv_id})


@pytest.mark.asyncio
async def test_generate_bulk_images_count_one_no_variant_tag(fresh_db):
    """count=1 → single message, no '(1 of 1)' suffix."""
    test_conv_id = f"TEST_conv_{uuid.uuid4().hex[:8]}"

    async def _fake_gen(prompt, sys_msg):
        return (f"img_{uuid.uuid4().hex[:8]}.png", "single caption")

    try:
        with patch.object(server, "generate_image_via_nano_banana",
                          side_effect=_fake_gen):
            rendered = await server._generate_bulk_images(
                db=fresh_db,
                conv_id=test_conv_id,
                primary_agent_id="ada",
                primary_agent_name="Ada",
                user_text="design a logo",
                sys_msg="test sys",
                count=1,
            )
        assert rendered == 1
        msgs = [m async for m in fresh_db.messages.find({"conversation_id": test_conv_id})]
        assert len(msgs) == 1
        assert "of 1" not in msgs[0]["text"]
    finally:
        await fresh_db.messages.delete_many({"conversation_id": test_conv_id})


@pytest.mark.asyncio
async def test_generate_bulk_images_degrades_on_partial_failures(fresh_db):
    """If the image model raises mid-batch, the helper logs and continues
    so later asks still render. Verify partial success returns a count
    less than requested."""
    test_conv_id = f"TEST_conv_{uuid.uuid4().hex[:8]}"
    call_count = {"n": 0}

    async def _flaky_gen(prompt, sys_msg):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated quota exhaustion")
        return (f"img_{uuid.uuid4().hex[:8]}.png", "ok")

    try:
        with patch.object(server, "generate_image_via_nano_banana",
                          side_effect=_flaky_gen):
            rendered = await server._generate_bulk_images(
                db=fresh_db,
                conv_id=test_conv_id,
                primary_agent_id="ada",
                primary_agent_name="Ada",
                user_text="create 3 hero images",
                sys_msg="test sys",
                count=3,
            )
        # Two of three should have rendered (the 2nd failed)
        assert rendered == 2
        msgs = [m async for m in fresh_db.messages.find({"conversation_id": test_conv_id})]
        assert len(msgs) == 2
    finally:
        await fresh_db.messages.delete_many({"conversation_id": test_conv_id})
