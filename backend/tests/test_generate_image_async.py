"""Tests for the generate-image background-task pattern.

Production bug Feb 2026: "Generation failed" toast on production when
clicking Regenerate on a post. Root cause same family as the remix
network-error fix — `/social/posts/{id}/generate-image` was blocking
on Nano Banana for 30-90s, exceeding production-ingress proxy timeouts.

Code-shape tests (fast, no router-init boilerplate) verify:
  • Route decorated with status_code=202.
  • Body offloads Nano Banana to asyncio.create_task.
  • Post is flipped to image_status='pending' synchronously before
    the task is scheduled.
  • Background task writes a terminal error status on failure so the
    polling loop can stop the spinner and show the error.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_SOCIAL_SRC = (Path(__file__).resolve().parent.parent / "social_posts.py").read_text()


def _extract_generate_handler() -> str:
    """Return the body of `generate_post_image` for static analysis."""
    m = re.search(
        r"async def generate_post_image\(.*?(?=^    @r\.|^def |\Z)",
        _SOCIAL_SRC, flags=re.DOTALL | re.MULTILINE,
    )
    assert m, "generate_post_image not found"
    return m.group(0)


def test_generate_image_endpoint_returns_202():
    """Route declares status_code=202 — required so the FE can disambiguate
    'background task started' from 'image actually returned in body'."""
    pattern = re.compile(
        r'@r\.post\("/social/posts/\{post_id\}/generate-image",\s*status_code=202\)',
    )
    assert pattern.search(_SOCIAL_SRC), (
        "generate-image route missing status_code=202 — FE relies on this "
        "to show 'Generating image — 30-90s' toast vs the legacy success toast."
    )


def test_generate_image_handler_uses_background_task():
    """The Nano Banana call must be inside asyncio.create_task, not at the
    top level — otherwise production-ingress proxy timeouts kill the
    request before the image returns."""
    body = _extract_generate_handler()
    assert "asyncio.create_task" in body, (
        "Generate-image must offload Nano Banana to a background task — "
        "blocking inline causes the production 'Generation failed' error."
    )
    nano_idx = body.find("_generate_post_image")
    task_idx = body.find("async def _run_generate_bg")
    create_idx = body.find("asyncio.create_task")
    assert task_idx >= 0 and nano_idx >= 0 and create_idx >= 0
    assert task_idx < nano_idx < create_idx, (
        "Nano Banana call should live INSIDE _run_generate_bg, before the "
        "create_task line that schedules the coroutine."
    )


def test_generate_image_marks_post_pending_synchronously():
    """The post must be flipped to image_status='pending' BEFORE the
    background task is scheduled — otherwise the polling loop's first
    tick races against the DB write."""
    body = _extract_generate_handler()
    pending_idx = body.find('"image_status": "pending"')
    task_idx = body.find("asyncio.create_task")
    assert pending_idx >= 0, "Handler must persist image_status='pending'."
    assert pending_idx < task_idx, (
        "image_status='pending' must be written BEFORE create_task to avoid "
        "a polling-loop race condition."
    )


def test_generate_image_persists_prompt_synchronously():
    """User-typed prompt (override) must be saved synchronously so the
    Edit-prompt editor isn't blank on refresh mid-generation."""
    body = _extract_generate_handler()
    assert "image_prompt" in body, (
        "Handler must persist image_prompt synchronously for refresh-safety."
    )
    # Verify the synchronous update happens before the task.
    prompt_persist_idx = body.find('"image_prompt": resolved_body')
    task_idx = body.find("asyncio.create_task")
    if prompt_persist_idx >= 0:
        assert prompt_persist_idx < task_idx, (
            "image_prompt persistence must happen synchronously, before task launch."
        )


def test_generate_image_writes_error_status_on_failure():
    """Background task must flip image_status='error' on failure so the
    polling loop can stop spinning and show the error toast."""
    body = _extract_generate_handler()
    assert '"image_status": "error"' in body, (
        "Background task must set image_status='error' on failure."
    )
    assert "image_error" in body, (
        "Background task must persist image_error for the FE to display."
    )
