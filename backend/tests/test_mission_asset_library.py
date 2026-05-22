"""Regression: mission must use Asset Library images when user asks.

Bug the user reported (Feb 2026): mission goal said "Generate 7 IG posts
about Disciplined Hustle and use the images in the asset library and
remix them in the posts." The mission produced text-only posts and
ignored the asset library entirely.

Root cause: `draft_to_social_queue` didn't accept `asset_ids`, and there
was no `list_org_assets` tool for the lead to discover what was
available. The drafter system prompt also didn't teach the LLM to chain
those two tools.

This test covers four orthogonal regressions:
  1. `_has_asset_intent` deterministically matches user wording.
  2. `_ASSET_LIBRARY_DIRECTIVE` mentions the correct tools + flag names.
  3. `list_org_assets` Tool is registered with the right description.
  4. `draft_to_social_queue` Tool description mentions asset_ids/remix.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from missions import (  # noqa: E402
    _has_asset_intent,
    _ASSET_LIBRARY_DIRECTIVE,
    _DRAFTER_SYSTEM,
    build_tools,
)


# ── _has_asset_intent ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Generate 7 IG posts and use the images in the asset library",
        "Create 5 LinkedIn posts — remix my product photos",
        "Draft IG content and attach our team photo",
        "Write 3 tweets with our brand images",
        "Use the asset library to source images for the spring drop posts",
        "Remix my photos into 5 instagram posts",
        "Make 4 IG posts with our product shots",
        "Use my pictures from the asset library",
        "Draft posts and remix our images",
        "Use the photos in the library for the cover images",
        "Attach our brand images to each post",
    ],
)
def test_detector_catches_asset_intent(text):
    assert _has_asset_intent(text), (
        f"_has_asset_intent should return True for: {text!r}"
    )


@pytest.mark.parametrize(
    "text",
    [
        "Generate 7 IG posts about Disciplined Hustle",
        "Create 3 LinkedIn posts about our launch",
        "Audit my SEO and write a report",
        "Write a 1-page PDF brief",
        "",
        # "use the image" (singular, no library/our/my context) should not match
        "Use the image we discussed earlier",
    ],
)
def test_detector_ignores_non_asset_intent(text):
    assert not _has_asset_intent(text), (
        f"_has_asset_intent should return False for: {text!r}"
    )


# ── _ASSET_LIBRARY_DIRECTIVE shape ───────────────────────────────────


def test_directive_names_both_tools():
    d = _ASSET_LIBRARY_DIRECTIVE
    assert "list_org_assets" in d
    assert "draft_to_social_queue" in d
    assert "asset_ids" in d
    assert "remix" in d


# ── Drafter system prompt covers the asset-library variant ───────────


def test_drafter_prompt_includes_asset_library_variant():
    assert "ASSET-LIBRARY VARIANT" in _DRAFTER_SYSTEM
    assert "list_org_assets" in _DRAFTER_SYSTEM
    # Must instruct the LLM to chain the two calls and default remix=true
    assert "remix=true" in _DRAFTER_SYSTEM.lower() or "remix: true" in _DRAFTER_SYSTEM.lower()


# ── Tool registration ────────────────────────────────────────────────


def _build_minimal_tools():
    """Build tools with mock dependencies — we only inspect the Tool
    list, never invoke the async handlers."""

    class _DummyDB:
        def __getattr__(self, name):
            return _DummyDB()

        async def find_one(self, *_a, **_k):
            return None

    async def _stub(*_a, **_k):
        return {"results": []}

    return build_tools(
        db=_DummyDB(),
        docs_root=Path("/tmp"),
        UPLOAD_DIR=Path("/tmp"),
        AGENTS={"writer": {"name": "Writer", "division": "marketing"}},
        brave_search=_stub,
        perplexity_web_search=_stub,
        perplexity_available=lambda: False,
        generate_image_for=_stub,
        save_artifacts=_stub,
        LlmChat=object,
        UserMessage=object,
        EMERGENT_LLM_KEY="test-key",
    )


def test_list_org_assets_tool_registered():
    tools = _build_minimal_tools()
    by_name = {t.name: t for t in tools}
    assert "list_org_assets" in by_name, (
        "list_org_assets Tool must be in build_tools() output"
    )
    desc = by_name["list_org_assets"].description.lower()
    # Description must surface the discovery use case
    for needle in ("asset library", "draft_to_social_queue", "category", "kind", "asset_ids"):
        assert needle in desc, f"list_org_assets description missing: {needle!r}"


def test_draft_to_social_queue_mentions_asset_workflow():
    tools = _build_minimal_tools()
    by_name = {t.name: t for t in tools}
    desc = by_name["draft_to_social_queue"].description.lower()
    # Description must surface the asset_ids + remix workflow
    for needle in ("asset_ids", "list_org_assets", "remix"):
        assert needle in desc, (
            f"draft_to_social_queue description missing: {needle!r}"
        )


# ── End-to-end post-pass simulation ──────────────────────────────────


def _simulate_postpass(raw_input: str, polished: str) -> tuple[str, list]:
    """Mirror the post-pass logic inside draft_mission_goal so we can
    test the asset-library override without spinning the FastAPI app."""
    import re as _re
    from missions import (
        _has_social_intent, _has_asset_intent,
        _SOCIAL_QUEUE_DIRECTIVE,
    )
    fired = []
    if _has_social_intent(raw_input):
        if "draft_to_social_queue" not in polished or _re.search(
            r"\.(pdf|md|docx|doc|txt|rtf)\b|\bwrite_artifact\b",
            polished, _re.IGNORECASE,
        ):
            polished = polished.rstrip() + _SOCIAL_QUEUE_DIRECTIVE
            fired.append("social_queue_directive")

        if _has_asset_intent(raw_input):
            lists_assets = "list_org_assets" in polished
            passes_asset_ids = bool(_re.search(r"asset_ids\s*[:=]", polished, _re.IGNORECASE))
            if not (lists_assets and passes_asset_ids):
                polished = polished.rstrip() + _ASSET_LIBRARY_DIRECTIVE
                fired.append("asset_library_directive")
    return polished, fired


def test_postpass_appends_asset_directive_when_missing():
    raw = (
        "Generate 7 IG posts about Disciplined Hustle and use the images "
        "in the asset library and remix them"
    )
    polished = (
        "Use `draft_to_social_queue` with platforms=['instagram'], "
        "count=7. Definition of done: 7 drafts in the Queue."
    )
    out, fired = _simulate_postpass(raw, polished)
    assert "asset_library_directive" in fired
    assert _ASSET_LIBRARY_DIRECTIVE in out
    assert "list_org_assets" in out


def test_postpass_skips_asset_directive_when_already_correct():
    raw = "Make 3 IG posts and remix my product photos from the library"
    polished = (
        "Step 1: call `list_org_assets` with category='product'. "
        "Step 2: call `draft_to_social_queue` with platforms=['instagram'], "
        "count=3, asset_ids=[ids from step 1], remix=true."
    )
    out, fired = _simulate_postpass(raw, polished)
    assert "asset_library_directive" not in fired
    assert out == polished


def test_postpass_skips_asset_when_no_asset_intent():
    raw = "Generate 7 IG posts about Disciplined Hustle"
    polished = (
        "Use `draft_to_social_queue` with platforms=['instagram'], count=7."
    )
    out, fired = _simulate_postpass(raw, polished)
    assert "asset_library_directive" not in fired
    assert _ASSET_LIBRARY_DIRECTIVE not in out
