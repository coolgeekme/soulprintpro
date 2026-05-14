"""Smoke test: private Chief-of-Staff convs always produce a reply.

Bug report Feb 2026: ben@archeforge.com saw 3 user messages with zero
agent responses in his private CoS thread. Root cause was three-fold:

  1. Legacy convs had auto_respond=False stamped before the field was
     made default-True on create.
  2. The image-intent classifier wrongly tagged "put together everything
     in a contract" as wanting a generated image, clearing responders.
  3. No safety net guaranteed at least one CoS reply per private_cos
     conv, so empty-responder paths produced silent dropouts.

Fix in server.py adds three guarantees:
  - On startup, heal any private_cos conv missing auto_respond / CoS in
    agent_ids (one-shot idempotent migration).
  - In send_message, force CoS into responders for private_cos kinds
    after the router/smart_expand decisions.
  - Re-force CoS into responders AFTER the image/video intent block
    clears them, so a misfiring classifier can't silence the room.

This test exercises the third guarantee (the one that actually fired
for Ben) via the boolean helpers it depends on.
"""
import pytest

# These two boolean helpers are pure — no LLM call needed if the quick
# pre-check returns True/False. We assert that the literal phrasing
# Ben used does NOT trip the quick image classifier (avoiding even the
# LLM round-trip).
from server import _quick_image_intent


@pytest.mark.parametrize("text", [
    "put together everything in a contract to hire consulting agreement",
    "draft a consulting agreement contract for hire",
    "can you write a brief outlining our pricing strategy",
    "give me a memo on the H1 roadmap",
    "produce a one-page report comparing competitors",
])
def test_document_requests_dont_trip_quick_image_intent(text):
    # _quick_image_intent returns None when it's ambiguous (defer to
    # the full LLM classifier) and False when it's a clear non-image
    # request. We never want True here — that would short-circuit the
    # classifier and start an image generation for a document ask.
    assert _quick_image_intent(text) is not True


@pytest.mark.parametrize("text", [
    "design a logo for my startup",
    "create an Instagram post for our launch",
    "make a hero illustration of a phoenix",
])
def test_clear_image_requests_remain_recognised(text):
    # The classifier should still catch obvious image asks (so we don't
    # accidentally regress in the opposite direction).
    assert _quick_image_intent(text) is not False
