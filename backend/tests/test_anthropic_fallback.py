"""Regression for the Anthropic-direct fallback helper.

We don't hit the live API in unit tests — the fallback wrapper is a
small piece of routing logic, so we test that:

  • Claude-prefixed model ids trigger the fallback path on transient errors
  • Non-Claude model ids re-raise the original error (no fallback attempt)
  • Permanent errors (401, 400) re-raise (no fallback attempt)
  • If both primary and fallback fail, the ORIGINAL error is re-raised so
    callers' existing error-handling/toasts keep working unchanged
  • The helper successfully routes a transient error to a working fallback

The transient/permanent classifier and ``_is_claude_model`` helpers are
also exercised directly because they're string-matched and easy to break.
"""
import pytest
from unittest.mock import AsyncMock, patch

from anthropic_fallback import (
    send_with_fallback, _is_claude_model, _is_transient,
)


def test_is_claude_model_matches_all_known_aliases():
    assert _is_claude_model("claude-sonnet-4-5")
    assert _is_claude_model("claude-opus-4-5")
    assert _is_claude_model("claude-haiku-4-5")
    assert _is_claude_model("Claude-3-5-Sonnet")  # case insensitive
    assert not _is_claude_model("gpt-4.1-mini")
    assert not _is_claude_model("gemini-2.5-flash-image-preview")
    assert not _is_claude_model("")
    assert not _is_claude_model(None)


def test_transient_classifier():
    # Transient
    assert _is_transient(Exception("Got HTTP 429 from upstream"))
    assert _is_transient(Exception("read timeout after 30s"))
    assert _is_transient(Exception("503 Service Unavailable"))
    assert _is_transient(Exception("RemoteProtocolError"))
    assert _is_transient(Exception("Anthropic overloaded — please retry"))
    # Permanent
    assert not _is_transient(Exception("401 Unauthorized"))
    assert not _is_transient(Exception("Invalid API key"))
    assert not _is_transient(Exception("400 Bad Request: invalid request"))
    assert not _is_transient(Exception("404 not found"))
    # Unknown — default to NOT transient (don't burn quota on guesses)
    assert not _is_transient(Exception("something completely unexpected"))


@pytest.mark.asyncio
async def test_fallback_fires_on_claude_transient():
    """Primary fails 429 on a Claude model → fallback returns happy path."""
    chat = AsyncMock()
    chat.send_message = AsyncMock(side_effect=Exception("HTTP 429 rate limit"))
    user_msg = object()
    with patch("anthropic_fallback._direct_anthropic_call",
               new=AsyncMock(return_value="from-fallback")):
        result = await send_with_fallback(
            chat, user_msg, model="claude-sonnet-4-5",
            system_prompt="be brief", prompt_text="hi",
        )
    assert result == "from-fallback"
    chat.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_fallback_for_non_claude_models():
    """Non-Claude model → fallback path must NOT be attempted."""
    chat = AsyncMock()
    chat.send_message = AsyncMock(side_effect=Exception("HTTP 429 rate limit"))
    user_msg = object()
    with patch("anthropic_fallback._direct_anthropic_call",
               new=AsyncMock(return_value="should-not-be-called")) as mock_fb:
        with pytest.raises(Exception, match="429"):
            await send_with_fallback(
                chat, user_msg, model="gpt-4.1-mini",
                system_prompt="x", prompt_text="y",
            )
    mock_fb.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_fallback_for_permanent_errors():
    """Auth failure on Claude → re-raise without fallback (key won't fix it)."""
    chat = AsyncMock()
    chat.send_message = AsyncMock(side_effect=Exception("401 Unauthorized — bad key"))
    user_msg = object()
    with patch("anthropic_fallback._direct_anthropic_call",
               new=AsyncMock()) as mock_fb:
        with pytest.raises(Exception, match="401"):
            await send_with_fallback(
                chat, user_msg, model="claude-sonnet-4-5",
                system_prompt="x", prompt_text="y",
            )
    mock_fb.assert_not_awaited()


@pytest.mark.asyncio
async def test_double_failure_reraises_original():
    """Primary 429 + fallback also fails → re-raise the PRIMARY error so
    callers' error messaging is unchanged from pre-fallback days."""
    chat = AsyncMock()
    chat.send_message = AsyncMock(side_effect=Exception("HTTP 429 primary"))
    user_msg = object()
    with patch("anthropic_fallback._direct_anthropic_call",
               new=AsyncMock(side_effect=Exception("Anthropic also down"))):
        with pytest.raises(Exception, match="primary"):
            await send_with_fallback(
                chat, user_msg, model="claude-sonnet-4-5",
                system_prompt="x", prompt_text="y",
            )


@pytest.mark.asyncio
async def test_happy_path_no_fallback_needed():
    """Primary succeeds → fallback is never touched."""
    chat = AsyncMock()
    chat.send_message = AsyncMock(return_value="primary-ok")
    user_msg = object()
    with patch("anthropic_fallback._direct_anthropic_call",
               new=AsyncMock()) as mock_fb:
        result = await send_with_fallback(
            chat, user_msg, model="claude-sonnet-4-5",
            system_prompt="x", prompt_text="y",
        )
    assert result == "primary-ok"
    mock_fb.assert_not_awaited()
