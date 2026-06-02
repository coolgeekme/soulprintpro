"""Regression tests for the per-post visual-style override on Social Queue.

User report (Jun 2026): they asked for an infographic on a specific AZ Pain
Doctors post but the image generator produced the brand-default photo
aesthetic because the org's `brand_assets.visual_style` + `style_guide`
were silently winning over the user's per-post intent.

Fix:
  • New `image_style_override` field on `social_posts`
  • Settable at draft time via `POST /api/orgs/{id}/social/draft`
    body field `style_override`
  • Settable per-post via `POST /api/social/posts/{id}/generate-image`
    body fields `style_override` (string) and `clear_style_override` (bool)
  • `_generate_post_image` reads the per-post override and REPLACES the
    brand-default `Visual style:` + `Synthesized visual style:` segments
    with a hard directive ("HARD OVERRIDE — match exactly, ignore brand
    defaults"). Brand tone/audience/palette/logo stay in place.

These tests pin the contract without standing up an HTTP server — they
exercise the Pydantic body shapes and the serializer.
"""
from __future__ import annotations

from social_posts import SocialDraftBody, GenerateImageBody, serialize_social_post


def test_draft_body_accepts_style_override() -> None:
    body = SocialDraftBody(
        prompt="Draft 3 IG posts about minimally invasive pain relief",
        platforms=["instagram"],
        count=3,
        style_override="Flat infographic with bold typography, callout stats, no people",
    )
    assert body.style_override.startswith("Flat infographic")


def test_draft_body_style_override_optional() -> None:
    """Backward compat — old clients omitting the field still work."""
    body = SocialDraftBody(prompt="Draft", platforms=["twitter"], count=1)
    assert body.style_override == ""


def test_draft_body_style_override_max_length() -> None:
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SocialDraftBody(
            prompt="x", platforms=["twitter"], count=1,
            style_override="x" * 1001,
        )


def test_generate_image_body_accepts_style_override() -> None:
    body = GenerateImageBody(
        prompt="A nurse comforting a patient",
        style_override="3D isometric illustration with soft pastels",
    )
    assert body.style_override == "3D isometric illustration with soft pastels"
    assert body.clear_style_override is False


def test_generate_image_body_clear_override() -> None:
    body = GenerateImageBody(prompt="", clear_style_override=True)
    assert body.clear_style_override is True
    assert body.style_override is None


def test_generate_image_body_distinguishes_unset_from_empty() -> None:
    """Unset (None) means 'leave the persisted override alone'; empty
    string means 'set override to empty' (effectively cleared but
    explicit). The endpoint relies on this distinction so a regenerate
    call without `style_override` doesn't wipe an existing override.
    """
    body_unset = GenerateImageBody(prompt="x")
    assert body_unset.style_override is None

    body_explicit_empty = GenerateImageBody(prompt="x", style_override="")
    assert body_explicit_empty.style_override == ""


def test_serializer_exposes_style_override() -> None:
    """Frontend can read `image_style_override` straight off the post."""
    p = {
        "_id": "abc",
        "org_id": "org-1",
        "image_style_override": "Watercolor with botanical accents",
    }
    out = serialize_social_post(p)
    assert out["image_style_override"] == "Watercolor with botanical accents"


def test_serializer_defaults_style_override_to_empty() -> None:
    """Old posts without the field serialize cleanly (empty string)."""
    p = {"_id": "abc", "org_id": "org-1"}
    out = serialize_social_post(p)
    assert out["image_style_override"] == ""
