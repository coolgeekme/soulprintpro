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



# ─────────────────────────────────────────────────────────────────────────────
# Prompt-intent style derivation (Feb 2026 follow-up)
#
# Even after the explicit per-post override shipped, users still got
# brand-default images when they typed "draft me infographic posts" in
# the goal field without opening the override panel. The router now
# falls back to deriving an override from the brief itself when the
# caller didn't pass an explicit one.
# ─────────────────────────────────────────────────────────────────────────────
from social_posts import derive_style_override_from_prompt


def test_derive_style_override_infographic() -> None:
    """The exact phrasing the user reported should trigger an override."""
    out = derive_style_override_from_prompt("Draft 5 infographic posts about pricing")
    assert "infographic" in out.lower()
    assert "callout" in out.lower() or "data-viz" in out.lower()


def test_derive_style_override_infographics_plural_hyphenated() -> None:
    """Variations: plural + hyphen + uppercase all match."""
    for prompt in ("info-graphics on Q1 wins", "INFOGRAPHIC ideas", "draft info graphics"):
        assert derive_style_override_from_prompt(prompt), f"missed: {prompt!r}"


def test_derive_style_override_isometric_3d() -> None:
    out = derive_style_override_from_prompt("3D render of our product launch hero")
    assert out and "3d" in out.lower()


def test_derive_style_override_meme() -> None:
    out = derive_style_override_from_prompt("Make 3 memes about late-night standups")
    assert out and "meme" in out.lower()


def test_derive_style_override_watercolour_british_spelling() -> None:
    out = derive_style_override_from_prompt("watercolour botanical illustrations")
    assert out and "watercol" in out.lower()


def test_derive_style_override_quote_card() -> None:
    out = derive_style_override_from_prompt("Spin up 5 pull-quote cards from the founder's letter")
    assert out and "quote" in out.lower()


def test_derive_style_override_empty_when_no_cue() -> None:
    """Plain briefs with no style language stay empty so brand defaults win."""
    assert derive_style_override_from_prompt("share a customer win story") == ""
    assert derive_style_override_from_prompt("") == ""


def test_derive_style_override_first_match_wins() -> None:
    """Brief mentioning two cues picks the first (most specific) match in our list.

    Specifically: 'infographic' (specific) beats a vague 'illustration' tail."""
    out = derive_style_override_from_prompt(
        "Make infographic posts in a flat illustration aesthetic",
    )
    assert "callout" in out.lower() or "data-viz" in out.lower()


def test_serializer_exposes_image_style_override_source() -> None:
    """Frontend can tell whether the override was user-supplied or auto-derived."""
    p = {
        "_id": "abc",
        "org_id": "org-1",
        "image_style_override": "Flat infographic …",
        "image_style_override_source": "auto_prompt",
    }
    assert serialize_social_post(p)["image_style_override_source"] == "auto_prompt"


def test_serializer_defaults_source_to_empty() -> None:
    p = {"_id": "abc", "org_id": "org-1"}
    assert serialize_social_post(p)["image_style_override_source"] == ""
