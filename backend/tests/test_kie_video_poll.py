"""Regression tests for `wait_for_video` (the Veo-specific poll loop).

Kie.ai's Veo endpoint migrated from numeric `successFlag` to the
string `status` convention without changing the URL path. The poll
loop had to grow the same dual-signal logic as the universal
marketplace path; without it the loop spun until the 5-min timeout
and `generate_video` mission tool returned "Video generation failed
or timed out" for every Veo request in production (Jun 2026).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import kie_client


@pytest.mark.asyncio
async def test_veo_status_success_returns_immediately() -> None:
    """Canonical happy path: `status == "SUCCESS"` → return URL."""
    payload = {
        "status": "SUCCESS",
        "response": {"resultUrls": ["https://kie/video.mp4"]},
    }
    with patch.object(kie_client, "get_video_status",
                       new=AsyncMock(return_value=payload)):
        url = await kie_client.wait_for_video("tid-1",
                                              total_timeout=5,
                                              poll_interval=0.01)
    assert url == "https://kie/video.mp4"


@pytest.mark.asyncio
async def test_veo_status_failed_returns_none() -> None:
    """`*_FAILED` → abort poll, return None."""
    payload = {"status": "GENERATE_VIDEO_FAILED", "response": {}}
    with patch.object(kie_client, "get_video_status",
                       new=AsyncMock(return_value=payload)):
        url = await kie_client.wait_for_video("tid-2",
                                              total_timeout=5,
                                              poll_interval=0.01)
    assert url is None


@pytest.mark.asyncio
async def test_veo_pending_then_success_keeps_polling() -> None:
    """PENDING / GENERATING states must NOT short-circuit the loop."""
    sequence = [
        {"status": "PENDING", "response": {}},
        {"status": "GENERATING", "response": {}},
        {"status": "SUCCESS",
         "response": {"resultUrls": ["https://kie/ready.mp4"]}},
    ]
    with patch.object(kie_client, "get_video_status",
                       new=AsyncMock(side_effect=sequence)):
        url = await kie_client.wait_for_video("tid-3",
                                              total_timeout=5,
                                              poll_interval=0.01)
    assert url == "https://kie/ready.mp4"


@pytest.mark.asyncio
async def test_veo_legacy_success_flag_still_works() -> None:
    """For backward compat with kie.ai families that haven't migrated
    to the string convention, the numeric flag still has to work."""
    payload = {
        "successFlag": 1,
        "response": {"resultUrls": ["https://kie/legacy.mp4"]},
    }
    with patch.object(kie_client, "get_video_status",
                       new=AsyncMock(return_value=payload)):
        url = await kie_client.wait_for_video("tid-4",
                                              total_timeout=5,
                                              poll_interval=0.01)
    assert url == "https://kie/legacy.mp4"


@pytest.mark.asyncio
async def test_veo_legacy_success_flag_failed_returns_none() -> None:
    payload = {"successFlag": 2, "response": {}}
    with patch.object(kie_client, "get_video_status",
                       new=AsyncMock(return_value=payload)):
        url = await kie_client.wait_for_video("tid-5",
                                              total_timeout=5,
                                              poll_interval=0.01)
    assert url is None
