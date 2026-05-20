"""Tests for the social-draft resilience hardening shipped Feb 19, 2026.

Production bug: users hit "Failed to generate" intermittently when
generating posts in the Social Queue. The `/orgs/{org_id}/social/draft`
endpoint was single-shot against `gpt-4.1-mini` with no timeout guard
and no fallback model — when OpenAI was slow/rate-limited or when the
LLM run drifted past the production-ingress proxy timeout, the request
died and the FE surfaced a generic "Failed" toast.

This test verifies the resilience layer via code inspection:
  • per-attempt asyncio.wait_for timeout
  • multi-attempt retry list (primary → retry → faster fallback)
  • TimeoutError → user-facing "Timeout" message
  • exhausted retries → 503 (so the FE retry-on-503 loop kicks in)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


_SOCIAL_SRC = (Path(__file__).resolve().parent.parent / "social_posts.py").read_text()


def _extract_draft_helper() -> str:
    """Return the body of `_draft_via_agent` for static analysis."""
    m = re.search(
        r"async def _draft_via_agent\(.*?(?=^async def |^def [a-zA-Z_]|\Z)",
        _SOCIAL_SRC, flags=re.DOTALL | re.MULTILINE,
    )
    assert m, "_draft_via_agent not found"
    return m.group(0)


def test_draft_helper_has_per_attempt_timeout():
    """Every LLM attempt must be wrapped in asyncio.wait_for so a slow
    OpenAI response can't drift past the production-ingress 60-100s cap.
    """
    body = _extract_draft_helper()
    assert "asyncio.wait_for" in body, (
        "_draft_via_agent must wrap chat.send_message in asyncio.wait_for "
        "to fail-fast before the production-ingress timeout."
    )
    # Timeout should be < the typical ingress cap (60-100s).
    # Literal timeout values live in the _MODEL_ATTEMPTS tuples, e.g.
    # `("openai", "gpt-4.1-mini", 50.0)`. Pull them out.
    timeouts = [float(x) for x in re.findall(r",\s*(\d+(?:\.\d+)?)\)", body)]
    assert any(0 < t <= 60 for t in timeouts), (
        f"Expected at least one timeout <= 60s in _MODEL_ATTEMPTS; got {timeouts}"
    )


def test_draft_helper_retries_with_fallback_model():
    """The retry list must include a second model so a slow / rate-
    limited primary model doesn't fail the whole request."""
    body = _extract_draft_helper()
    assert "_MODEL_ATTEMPTS" in body, "Helper must declare _MODEL_ATTEMPTS list."
    # The list must contain >= 2 entries and reference at least 2
    # different models (primary + fallback).
    model_refs = re.findall(r'"(gpt-[a-z0-9.\-]+)"', body)
    assert len(set(model_refs)) >= 2, (
        f"Expected >= 2 distinct fallback models; got {model_refs}"
    )


def test_draft_helper_raises_503_on_total_failure():
    """When every attempt fails, helper must raise HTTPException(503),
    NOT 502. The FE retry-on-503 loop relies on the specific status."""
    body = _extract_draft_helper()
    # The status code shows up as the first arg to HTTPException.
    matches = re.findall(r"HTTPException\(\s*(\d+)", body)
    assert "503" in matches, (
        f"Helper must raise 503 when retries exhausted; statuses seen: {matches}"
    )


def test_draft_helper_distinguishes_timeout_in_error():
    """Timeout failures should surface with a user-facing "Timeout"
    string so the FE toast tells the user it's a speed issue (retry
    will help) vs an LLM error (refine the prompt)."""
    body = _extract_draft_helper()
    assert "asyncio.TimeoutError" in body
    assert re.search(r"['\"]Timeout", body), (
        "Helper must distinguish TimeoutError with a 'Timeout'-prefixed message."
    )


def test_route_handler_returns_503_not_502_on_failure():
    """The draft route handler must propagate 503 (not 502) on LLM
    errors so the FE's retry-on-503 loop catches it. 502s would cause
    a hard fail with no auto-retry."""
    m = re.search(
        r"async def draft_social_posts\(.*?(?=^    @r\.|^def |\Z)",
        _SOCIAL_SRC, flags=re.DOTALL | re.MULTILINE,
    )
    assert m
    handler = m.group(0)
    statuses = re.findall(r"HTTPException\(\s*(\d+)", handler)
    assert "503" in statuses
    # Don't allow 502 on the LLM-failure path (we kept 422 for input
    # validation, which is fine).
    assert "502" not in statuses, (
        f"Draft handler still uses 502 somewhere; statuses: {statuses}. "
        "Use 503 so the FE retry-on-503 loop activates."
    )
