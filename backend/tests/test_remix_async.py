"""Test the remix-asset endpoint's background-task pattern.

Production bug Feb 2026: users hit a "network error" when remixing an
uploaded image asset because Nano Banana image-to-image generation
typically takes 30-90 seconds, exceeding production-ingress proxy
timeouts (usually 60-100s). The fix moves the Nano Banana call into a
background `asyncio.create_task` so the HTTP request returns immediately
with `image_status: "pending"`, and the SocialPage's existing polling
loop picks up the final image.

We assert via code inspection that:
  • The remix-asset route is decorated with status_code=202.
  • The route body contains `asyncio.create_task(` (background pattern).
  • The route persists image_status="pending" before kicking the task off.
This keeps the test fast + independent of the heavy router-init machinery.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


_SOCIAL_SRC = (Path(__file__).resolve().parent.parent / "social_posts.py").read_text()


def _extract_remix_handler() -> str:
    """Return the body of `remix_asset_into_cover` for static analysis."""
    m = re.search(
        r"async def remix_asset_into_cover\(.*?(?=^    @r\.|^def |\Z)",
        _SOCIAL_SRC, flags=re.DOTALL | re.MULTILINE,
    )
    assert m, "remix_asset_into_cover not found"
    return m.group(0)


def test_remix_endpoint_decorated_with_202():
    """The /remix-asset route must declare status_code=202 — returning
    202 (Accepted) is the HTTP-spec-correct response when work is
    deferred to a background task, AND it stops axios from logging the
    request as a generic 200 success when it's actually still pending."""
    # The decorator immediately precedes the function definition.
    pattern = re.compile(
        r"@r\.post\(\"/social/posts/\{post_id\}/remix-asset\",\s*status_code=202\)",
    )
    assert pattern.search(_SOCIAL_SRC), (
        "remix-asset route is missing status_code=202 — the FE expects "
        "this so it can show the right toast copy on a deferred response."
    )


def test_remix_handler_uses_background_task():
    """The handler must offload the Nano Banana call to
    `asyncio.create_task(...)` so the HTTP request returns immediately —
    fixing the production 'network error' caused by ingress-proxy
    timeouts on the 30-90s synchronous generation."""
    body = _extract_remix_handler()
    assert "asyncio.create_task" in body, (
        "Handler must spawn a background task for Nano Banana — without "
        "it the HTTP request blocks for 30-90s and ingress timeouts kill "
        "the response."
    )
    # The Nano Banana call should live INSIDE the background coroutine,
    # not at the top level of the handler.
    nano_idx = body.find("generate_image_via_nano_banana")
    task_idx = body.find("async def _run_remix_bg")
    create_idx = body.find("asyncio.create_task")
    assert task_idx >= 0 and nano_idx >= 0
    assert task_idx < nano_idx < create_idx, (
        "Nano Banana call should appear inside _run_remix_bg, BEFORE the "
        "create_task line that kicks the coroutine off."
    )


def test_remix_handler_marks_post_pending_synchronously():
    """Before spawning the background task, the handler must update the
    post to image_status='pending' so the SocialPage polling loop sees
    the spinner state immediately."""
    body = _extract_remix_handler()
    # The synchronous update_one happens BEFORE asyncio.create_task.
    pending_idx = body.find('"image_status": "pending"')
    task_idx = body.find("asyncio.create_task")
    assert pending_idx >= 0, "Handler must persist image_status='pending' synchronously."
    assert pending_idx < task_idx, (
        "image_status='pending' must be persisted BEFORE create_task — "
        "otherwise the FE polling loop has a race condition where the "
        "first poll fires before the spinner state is in the DB."
    )


def test_remix_handler_persists_user_prompt_synchronously():
    """User-typed prompt must be saved synchronously too — without it,
    the 'Edit prompt' affordance on the post would be blank if the user
    refreshes the page mid-remix."""
    body = _extract_remix_handler()
    assert '"image_prompt": prompt' in body, (
        "Handler must persist image_prompt synchronously for refresh-safety."
    )


def test_remix_handler_writes_terminal_status_on_failure():
    """When Nano Banana raises in the background task, the post must
    be flipped to image_status='error' with a user-visible error message
    so the FE polling loop can stop the spinner + show the error."""
    body = _extract_remix_handler()
    assert '"image_status": "error"' in body, (
        "Background task must set image_status='error' on failure."
    )
    assert "image_error" in body, (
        "Background task must persist image_error for FE display."
    )
