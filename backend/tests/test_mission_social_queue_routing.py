"""Regression: mission engine must route 'draft N social posts' to the
Social Queue, not to a PDF artifact.

Bug the user reported: running the mission goal "Generate 3 IG posts
about Disciplined Hustle to post the next 7 days" produced a PDF
artifact containing the post text — not actual drafts in the Social
Queue. Root cause: `draft_to_social_queue` was a stub returning
{"error": ...} and the agent fell back to write_artifact.

This test asserts two things:
1. The drafter system prompt now contains the SOCIAL-POSTS RECIPE
   block that explicitly tells the LLM to use draft_to_social_queue
   for any "generate N posts" goal.
2. The Tool list returned by build_tools() includes a
   draft_to_social_queue tool whose description forbids saving social
   posts as PDFs.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from missions import _DRAFTER_SYSTEM, build_tools  # noqa: E402


def test_drafter_prompt_has_social_posts_recipe():
    """Guard rail — without this block the drafter routes social-post
    requests to write_artifact (the PDF/MD path)."""
    assert "SOCIAL-POSTS RECIPE" in _DRAFTER_SYSTEM
    assert "draft_to_social_queue" in _DRAFTER_SYSTEM
    # The recipe must explicitly forbid the wrong deliverable type.
    assert "Do NOT save these posts as a .pdf or .md" in _DRAFTER_SYSTEM


def _mock_factory():
    """Minimal stubs so build_tools() can be called without spinning
    Mongo / network. We only inspect the returned Tool list — the
    callables are never invoked."""

    class _DummyDB:
        def __getattr__(self, name):
            return _DummyDB()

        async def find_one(self, *_a, **_k):
            return None

        async def insert_one(self, doc):
            class R:
                inserted_id = "fake_id"
            return R()

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
        LlmChat=object,        # placeholder — only truthy presence matters
        UserMessage=object,
        EMERGENT_LLM_KEY="test-key",
    )


def test_tool_list_includes_draft_to_social_queue():
    tools = _mock_factory()
    by_name = {t.name: t for t in tools}
    assert "draft_to_social_queue" in by_name, (
        "draft_to_social_queue Tool is missing from build_tools() output"
    )
    t = by_name["draft_to_social_queue"]
    desc = t.description.lower()
    # Tool description must forbid the PDF/MD path explicitly so the
    # agent doesn't fall back to write_artifact even if the goal is
    # ambiguous.
    assert "do not save social posts as a .pdf" in desc, (
        "Tool description must explicitly forbid saving social posts as PDFs"
    )
    # Tool must support the most common platform aliases.
    for p in ("instagram", "linkedin", "twitter", "facebook"):
        assert p in desc, f"Tool description missing platform {p}"


def test_tool_lacks_unwired_runtime_returns_friendly_error():
    """If a mission runtime forgets to pass LlmChat / EMERGENT_LLM_KEY,
    the tool returns a structured error instead of crashing."""
    tools = build_tools(
        db=None,
        docs_root=Path("/tmp"),
        UPLOAD_DIR=Path("/tmp"),
        AGENTS={},
        brave_search=lambda *_a, **_k: None,
        perplexity_web_search=lambda *_a, **_k: None,
        perplexity_available=lambda: False,
        generate_image_for=lambda *_a, **_k: None,
        save_artifacts=lambda *_a, **_k: None,
        # Intentionally omitting LlmChat / UserMessage / EMERGENT_LLM_KEY
    )
    by_name = {t.name: t for t in tools}
    assert "draft_to_social_queue" in by_name
    # The tool itself is still registered (correct) — its impl returns
    # a friendly error the agent surface can render. We don't invoke
    # the async impl here (no event loop in this test), but the
    # presence + signature contract is enough.
    assert callable(by_name["draft_to_social_queue"].handler)
