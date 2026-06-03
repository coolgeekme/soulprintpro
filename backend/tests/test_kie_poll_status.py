"""Regression tests for `wait_for_kie_task` polling logic across the
two kie.ai status conventions (new `status` string vs old numeric
`successFlag`).

Background: kie.ai's newer Suno API emits a string `status` field
(PENDING / TEXT_SUCCESS / FIRST_SUCCESS / SUCCESS / *_FAILED) and
NEVER sets the old numeric `successFlag`. Our previous code only
checked the numeric flag, which is why Suno requests were timing out
after the URL-extraction tightening — the poll loop had no real
"done" signal to read.

These tests pin the new contract:
  • status == "SUCCESS" → return URLs immediately
  • status contains "FAIL" → abort
  • status == "FIRST_SUCCESS" / "TEXT_SUCCESS" → keep waiting
  • successFlag still honoured for older families
  • streamAudioUrl-only payloads never short-circuit out
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import kie_client


def _suno_info():
    return {"family": "generate", "type": "music", "label": "Suno",
            "no_family_path": True}


@pytest.mark.asyncio
async def test_status_success_returns_immediately() -> None:
    """SUCCESS string → return URLs without further polling."""
    final = {
        "status": "SUCCESS",
        "response": {
            "sunoData": [
                {"audioUrl": "https://kie/a.mp3"},
                {"audioUrl": "https://kie/b.mp3"},
            ]
        },
    }
    with patch.object(kie_client, "kie_provider_info", return_value=_suno_info()), \
         patch.object(kie_client, "get_kie_task_status",
                       new=AsyncMock(return_value=final)):
        urls = await kie_client.wait_for_kie_task("suno", "tid-1",
                                                   total_timeout=5,
                                                   poll_interval=0.01)
    assert urls == ["https://kie/a.mp3", "https://kie/b.mp3"]


@pytest.mark.asyncio
async def test_status_failed_aborts_with_empty() -> None:
    """GENERATE_AUDIO_FAILED → return [] (poll loop exits)."""
    fail = {
        "status": "GENERATE_AUDIO_FAILED",
        "response": {"sunoData": []},
    }
    with patch.object(kie_client, "kie_provider_info", return_value=_suno_info()), \
         patch.object(kie_client, "get_kie_task_status",
                       new=AsyncMock(return_value=fail)):
        urls = await kie_client.wait_for_kie_task("suno", "tid-2",
                                                   total_timeout=5,
                                                   poll_interval=0.01)
    assert urls == []


@pytest.mark.asyncio
async def test_status_intermediate_keeps_polling_until_success() -> None:
    """FIRST_SUCCESS (one take ready, second still rendering) must
    NOT cause an early exit — we want both takes, not one."""
    sequence = [
        {"status": "PENDING", "response": {}},
        {"status": "TEXT_SUCCESS", "response": {}},
        {"status": "FIRST_SUCCESS", "response": {
            "sunoData": [{"audioUrl": "https://kie/a.mp3"},
                          {"streamAudioUrl": "https://kie/b-stream"}],
        }},
        {"status": "SUCCESS", "response": {
            "sunoData": [{"audioUrl": "https://kie/a.mp3"},
                          {"audioUrl": "https://kie/b.mp3"}],
        }},
    ]
    mock_status = AsyncMock(side_effect=sequence)
    with patch.object(kie_client, "kie_provider_info", return_value=_suno_info()), \
         patch.object(kie_client, "get_kie_task_status", new=mock_status):
        urls = await kie_client.wait_for_kie_task("suno", "tid-3",
                                                   total_timeout=5,
                                                   poll_interval=0.01)
    assert urls == ["https://kie/a.mp3", "https://kie/b.mp3"]


@pytest.mark.asyncio
async def test_status_success_accepts_stream_url_as_fallback() -> None:
    """When kie.ai signals SUCCESS but for some reason a clip only
    has `streamAudioUrl`, we still surface it — better than silently
    dropping the result. `_extract_kie_urls(require_complete=False)`
    (the default) handles this."""
    edge = {
        "status": "SUCCESS",
        "response": {
            "sunoData": [
                {"audioUrl": "https://kie/a.mp3"},
                {"streamAudioUrl": "https://kie/b-stream"},
            ]
        },
    }
    with patch.object(kie_client, "kie_provider_info", return_value=_suno_info()), \
         patch.object(kie_client, "get_kie_task_status",
                       new=AsyncMock(return_value=edge)):
        urls = await kie_client.wait_for_kie_task("suno", "tid-4",
                                                   total_timeout=5,
                                                   poll_interval=0.01)
    assert urls == ["https://kie/a.mp3", "https://kie/b-stream"]


@pytest.mark.asyncio
async def test_legacy_success_flag_one_still_works() -> None:
    """Older kie.ai families (image / video) still use numeric flag.
    Image task with `successFlag = 1` should exit immediately."""
    image_info = {"family": "kling", "type": "video", "label": "Kling"}
    final = {
        "successFlag": 1,
        "response": {"resultUrls": ["https://kie/img.png"]},
    }
    with patch.object(kie_client, "kie_provider_info", return_value=image_info), \
         patch.object(kie_client, "get_kie_task_status",
                       new=AsyncMock(return_value=final)):
        urls = await kie_client.wait_for_kie_task("kling-3", "tid-5",
                                                   total_timeout=5,
                                                   poll_interval=0.01)
    assert urls == ["https://kie/img.png"]


@pytest.mark.asyncio
async def test_legacy_success_flag_failed_aborts() -> None:
    """flag = 2 or 3 → fail-fast like before."""
    image_info = {"family": "kling", "type": "video", "label": "Kling"}
    fail = {"successFlag": 2, "response": {}}
    with patch.object(kie_client, "kie_provider_info", return_value=image_info), \
         patch.object(kie_client, "get_kie_task_status",
                       new=AsyncMock(return_value=fail)):
        urls = await kie_client.wait_for_kie_task("kling-3", "tid-6",
                                                   total_timeout=5,
                                                   poll_interval=0.01)
    assert urls == []


@pytest.mark.asyncio
async def test_no_status_no_flag_music_waits_for_audio_url() -> None:
    """Fallback path for older Suno responses with neither field:
    we must not exit on streamAudioUrl alone (the original bug).
    Sequence: stream-only → both audioUrls."""
    sequence = [
        {"response": {"sunoData": [
            {"streamAudioUrl": "https://kie/a-stream"},
            {"streamAudioUrl": "https://kie/b-stream"},
        ]}},
        {"response": {"sunoData": [
            {"audioUrl": "https://kie/a.mp3"},
            {"audioUrl": "https://kie/b.mp3"},
        ]}},
    ]
    mock_status = AsyncMock(side_effect=sequence)
    with patch.object(kie_client, "kie_provider_info", return_value=_suno_info()), \
         patch.object(kie_client, "get_kie_task_status", new=mock_status):
        urls = await kie_client.wait_for_kie_task("suno", "tid-7",
                                                   total_timeout=5,
                                                   poll_interval=0.01)
    assert urls == ["https://kie/a.mp3", "https://kie/b.mp3"]
