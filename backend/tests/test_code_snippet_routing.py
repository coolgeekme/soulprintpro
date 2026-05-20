"""Regression: code / snippet / HTML-tag intents must NEVER route to image gen.

The user reported a "watermark HTML/CSS tag" request returning generated
images instead of a code snippet. The fix lives in two places:

1. `_DOC_INTENT_RE` in server.py — hard veto for any code-shaped ask
   (HTML, CSS, JS, regex, watermark tag, embed code, ...).
2. `_DRAFTER_SYSTEM` in missions.py — explicit CODE-SNIPPET RECIPE rule.

This test exercises (1) deterministically (no LLM roundtrip needed) and
also asserts (2) the drafter prompt mentions the recipe so the file
edit isn't accidentally reverted.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The functions live inside server.py which imports the world. We
# isolate the regex from a fresh import to avoid pulling Mongo/Motor.
from server import _quick_image_intent, _DOC_INTENT_RE, _IMAGE_KEYWORDS_RE
from missions import _DRAFTER_SYSTEM


CODE_LIKE_PROMPTS = [
    # The exact bug the user reported.
    "Give me an HTML watermark tag for the bottom-right corner.",
    "Generate a CSS snippet for a fixed-position watermark overlay.",
    "Write me a watermark HTML markup I can drop into my header.",
    # Other classic code-snippet asks.
    "Generate an HTML snippet for a hero section.",
    "Create a regex that matches a US zip code.",
    "Write a SQL query that selects active users in the last 7 days.",
    "Give me a JSON payload example for the /webhooks endpoint.",
    "Make me a Dockerfile for a FastAPI app.",
    "Design a React component that renders a pricing card.",
    "Write a curl command to POST to /api/login.",
    "Generate a meta tag for Open Graph image preview.",
    "Make a tracking pixel embed code.",
    "Generate a shell script that backs up Mongo nightly.",
    "Write a cron expression for every weekday at 9am.",
    "Build me a tiny JavaScript snippet that copies to clipboard.",
]


def test_quick_image_intent_vetoes_code_requests():
    """Every code-shaped prompt must return False (NOT image) from the
    deterministic pre-classifier, not None (which would defer to the
    LLM and could still misroute)."""
    failures = []
    for prompt in CODE_LIKE_PROMPTS:
        result = _quick_image_intent(prompt)
        if result is not False:
            failures.append((prompt, result))
    assert not failures, f"Code-shaped prompts that didn't get vetoed: {failures}"


def test_doc_intent_regex_matches_code_keywords():
    """Direct regex hit for each known code keyword."""
    code_keywords = [
        "watermark tag", "html snippet", "css snippet", "embed code",
        "meta tag", "tracking pixel", "react component", "regex",
        "sql query", "dockerfile", "cron expression", "shell script",
        "<div", "<span", "<script", "<style", "javascript", "typescript",
        ".html", ".css", ".js", ".ts", ".tsx", ".jsx", ".py", ".sh",
    ]
    for kw in code_keywords:
        assert _DOC_INTENT_RE.search(kw), f"_DOC_INTENT_RE missed code keyword: {kw!r}"


def test_genuine_image_prompts_still_match():
    """Sanity: real image asks must still trigger the image path."""
    image_prompts = [
        "Create an Instagram post about our launch.",
        "Design a hero illustration for the homepage.",
        "Generate a logo for our startup.",
        "Make me a thumbnail for the launch post.",
    ]
    for prompt in image_prompts:
        result = _quick_image_intent(prompt)
        assert result is True, f"Real image prompt should route to image gen: {prompt!r} → {result}"


def test_drafter_prompt_has_code_snippet_recipe():
    """Guard rail — the CODE-SNIPPET RECIPE block must stay in the
    drafter system prompt so the mission goal-rewriter routes code
    requests to source-file artifacts instead of images."""
    assert "CODE-SNIPPET RECIPE" in _DRAFTER_SYSTEM
    # Make sure the recipe specifically calls out the misroute we're
    # guarding against — "do NOT generate an image".
    assert "do NOT generate an image" in _DRAFTER_SYSTEM
