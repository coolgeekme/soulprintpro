"""Tests for the new [[IMAGE]] block extractor and chat image pipeline."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artifacts import extract_image_blocks


def test_extracts_single_image_block():
    text = (
        "Here's the IG post for AZ Pain Doctors:\n\n"
        "[[IMAGE prompt=\"A flat-design Instagram post about back-pain relief, "
        "calm blue palette, hero text 'Same-day appointments'\" aspect=\"1:1\"]]\n\n"
        "Caption: Suffering from chronic back pain? Same-day appointments…"
    )
    cleaned, blocks = extract_image_blocks(text)
    assert len(blocks) == 1
    b = blocks[0]
    assert "flat-design Instagram post" in b["prompt"]
    assert b["aspect"] == "1:1"
    assert b["model"] == "nano-banana"
    # Cleaned text should NOT contain the raw block syntax.
    assert "[[IMAGE" not in cleaned
    assert "[[image" not in cleaned.lower()
    assert "Caption:" in cleaned


def test_extracts_multiple_image_blocks():
    text = (
        "Three creative variants:\n"
        "[[IMAGE prompt=\"Variant 1: minimalist\" aspect=\"1:1\"]]\n"
        "[[IMAGE prompt=\"Variant 2: bold typography\" aspect=\"1:1\"]]\n"
        "[[IMAGE prompt=\"Variant 3: photographic\" aspect=\"1:1\"]]\n"
        "Pick your favorite."
    )
    cleaned, blocks = extract_image_blocks(text)
    assert len(blocks) == 3
    assert "[[IMAGE" not in cleaned
    assert blocks[0]["prompt"].startswith("Variant 1")
    assert blocks[2]["prompt"].startswith("Variant 3")


def test_empty_prompt_is_dropped():
    text = "[[IMAGE prompt=\"\"]]\nWhoops empty"
    cleaned, blocks = extract_image_blocks(text)
    assert blocks == []
    assert "[[IMAGE" not in cleaned


def test_no_image_tag_returns_unchanged():
    text = "Plain text reply with no image block."
    cleaned, blocks = extract_image_blocks(text)
    assert blocks == []
    assert cleaned == text


def test_invalid_model_falls_back_to_nano_banana():
    text = '[[IMAGE prompt="x" model="dall-e-99"]]'
    cleaned, blocks = extract_image_blocks(text)
    assert len(blocks) == 1
    assert blocks[0]["model"] == "nano-banana"


def test_long_prompt_is_truncated():
    long_prompt = "x" * 5000
    text = f'[[IMAGE prompt="{long_prompt}"]]'
    _, blocks = extract_image_blocks(text)
    assert len(blocks) == 1
    assert len(blocks[0]["prompt"]) <= 1800
