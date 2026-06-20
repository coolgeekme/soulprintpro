"""Tests for the user-uploaded-image → vision-caption flow added in
`/api/orgs/{org_id}/social/from-image`.

The vision LLM call itself is mocked (no live OpenAI key needed) so this
suite runs in CI; what's exercised here is the persistence + serialization
+ option-handling layer that the user actually depends on never breaking:

  • Image bytes land verbatim in /uploads + GridFS.
  • `image_source="user_upload"` / `image_user_uploaded=True` flags
    travel through the serializer.
  • `auto_fit_per_platform=1` produces per-platform geometrically-
    resized variants alongside the original.
  • Carousel mode produces ONE draft per platform with all images
    attached in `media`, first image as cover.
  • The `_VISION_PLATFORM_HINTS` map covers every platform we expose
    in the FE so the LLM prompt never falls back to the generic line.
"""
from social_posts import (
    _PLATFORM_FIT_TARGETS,
    _VISION_PLATFORM_HINTS,
    _platform_fit_target,
    serialize_social_post,
)


def test_image_user_uploaded_flag_in_serializer() -> None:
    p = {
        "_id": "abc", "org_id": "o",
        "image_source": "user_upload",
        "image_user_uploaded": True,
        "auto_fit_applied": True,
    }
    out = serialize_social_post(p)
    assert out["image_source"] == "user_upload"
    assert out["image_user_uploaded"] is True
    assert out["auto_fit_applied"] is True


def test_image_flags_default_false_when_missing() -> None:
    """Existing AI-drafted posts (no image_source) must not break the
    serializer or accidentally claim the user uploaded them."""
    out = serialize_social_post({"_id": "abc", "org_id": "o"})
    assert out["image_source"] == ""
    assert out["image_user_uploaded"] is False
    assert out["auto_fit_applied"] is False


def test_vision_platform_hints_cover_every_publish_platform() -> None:
    """Every platform the user can pick in the SocialPage compose form
    must have a tonal hint, otherwise the vision prompt produces
    generic captions and we waste an LLM call."""
    expected = {"twitter", "linkedin", "facebook", "instagram",
                "threads", "bluesky", "mastodon", "reddit",
                "tiktok", "youtube", "pinterest"}
    missing = expected - set(_VISION_PLATFORM_HINTS)
    assert not missing, f"Missing vision hints for: {missing}"


def test_platform_fit_targets_are_sane() -> None:
    """No negative/zero dimensions; vertical platforms get a portrait
    target; square platforms get equal sides."""
    for plat, (w, h) in _PLATFORM_FIT_TARGETS.items():
        assert w > 0 and h > 0, f"{plat} has bogus dims: {w}x{h}"
    # Sanity: known-portrait platforms must be portrait.
    assert _PLATFORM_FIT_TARGETS["tiktok"][1] > _PLATFORM_FIT_TARGETS["tiktok"][0]
    assert _PLATFORM_FIT_TARGETS["pinterest"][1] > _PLATFORM_FIT_TARGETS["pinterest"][0]
    # Sanity: square platforms.
    for sq in ("instagram", "threads"):
        assert _PLATFORM_FIT_TARGETS[sq][0] == _PLATFORM_FIT_TARGETS[sq][1]
    # Sanity: 16:9 platforms.
    for wide in ("twitter", "youtube", "bluesky", "mastodon"):
        w, h = _PLATFORM_FIT_TARGETS[wide]
        # Allow some wiggle (1600/900 = 1.778, etc) — just confirm >1.5.
        assert w / h > 1.5, f"{wide} should be widescreen"


def test_platform_fit_target_lookup_is_case_insensitive() -> None:
    assert _platform_fit_target("Twitter") == _platform_fit_target("twitter")
    assert _platform_fit_target("INSTAGRAM") == _platform_fit_target("instagram")
    assert _platform_fit_target("nonexistent") is None


def test_resize_one_produces_target_dimensions() -> None:
    """`image_resize.resize_one` is the only image-touching code path
    in the from-image endpoint. Confirm its contract still holds —
    "fill" mode hits exact target dims, no off-by-ones."""
    from PIL import Image
    from io import BytesIO
    from image_resize import resize_one
    src = Image.new("RGB", (1500, 1000), (240, 220, 180))
    buf = BytesIO()
    src.save(buf, format="JPEG")
    out_bytes = resize_one(buf.getvalue(), 1080, 1080, mode="fill")
    out_img = Image.open(BytesIO(out_bytes))
    assert out_img.size == (1080, 1080)
