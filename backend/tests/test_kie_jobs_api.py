"""Regression tests for the new /api/v1/jobs/createTask + /jobs/recordInfo
helpers in kie_client. Older kie.ai families use the
/api/v1/<family>/generate path covered by test_kie_universal.py; the
new Market models (Kling V2.1 Master, Wan 2.7, Seedream 5, …) all live
behind the unified jobs/* surface.

We mock at the `get_kie_job_state` boundary so we don't need to spin up
a fake HTTP server — the network layer is exercised separately by the
existing test_kie_universal suite.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import kie_client


@pytest.mark.asyncio
async def test_jobs_success_extracts_first_result_url() -> None:
    """`state == "success"` → parse resultJson → return list of URLs."""
    payload = {
        "state": "success",
        "resultJson": '{"resultUrls":["https://cdn.kie/ok.mp4"]}',
    }
    with patch.object(kie_client, "get_kie_job_state",
                       new=AsyncMock(return_value=payload)):
        urls = await kie_client.wait_for_kie_job(
            "tid", total_timeout=2, poll_interval=0.01,
        )
    assert urls == ["https://cdn.kie/ok.mp4"]


@pytest.mark.asyncio
async def test_jobs_fail_state_returns_empty_immediately() -> None:
    """`state == "fail"` short-circuits — no further polling."""
    calls = []

    async def _fake(_tid):
        calls.append(_tid)
        return {"state": "fail", "failCode": "501", "failMsg": "boom"}

    with patch.object(kie_client, "get_kie_job_state", new=_fake):
        urls = await kie_client.wait_for_kie_job(
            "tid", total_timeout=2, poll_interval=0.01,
        )
    assert urls == []
    # Single poll → terminal — not 50.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_jobs_in_progress_then_success() -> None:
    """`waiting` → `generating` → `success` must keep polling, not bail."""
    sequence = [
        {"state": "waiting", "resultJson": ""},
        {"state": "queuing", "resultJson": ""},
        {"state": "generating", "resultJson": ""},
        {"state": "success",
         "resultJson": '{"resultUrls":["https://cdn.kie/ready.mp4"]}'},
    ]
    with patch.object(kie_client, "get_kie_job_state",
                       new=AsyncMock(side_effect=sequence)):
        urls = await kie_client.wait_for_kie_job(
            "tid", total_timeout=5, poll_interval=0.01,
        )
    assert urls == ["https://cdn.kie/ready.mp4"]


@pytest.mark.asyncio
async def test_jobs_malformed_resultjson_does_not_crash() -> None:
    """If resultJson is non-JSON we return empty rather than throwing."""
    payload = {"state": "success", "resultJson": "not json at all"}
    with patch.object(kie_client, "get_kie_job_state",
                       new=AsyncMock(return_value=payload)):
        urls = await kie_client.wait_for_kie_job(
            "tid", total_timeout=2, poll_interval=0.01,
        )
    assert urls == []


@pytest.mark.asyncio
async def test_extract_jobs_urls_handles_missing_resultjson() -> None:
    assert kie_client._extract_jobs_urls({}) == []
    assert kie_client._extract_jobs_urls({"resultJson": None}) == []
    assert kie_client._extract_jobs_urls({"resultJson": "{}"}) == []
    assert kie_client._extract_jobs_urls(
        {"resultJson": '{"resultUrls":[]}'}
    ) == []
    assert kie_client._extract_jobs_urls(
        {"resultJson": '{"resultUrls":["a","b"]}'}
    ) == ["a", "b"]


@pytest.mark.asyncio
async def test_kling_v21_helper_clamps_duration() -> None:
    """Caller passes "999" → we coerce to "10" before submission.
    kie.ai will 422 anything not in {5, 10} for the Master tier."""
    seen_params: dict = {}

    async def _fake_submit(*, model, input_params, callback_url=None):
        seen_params.update(input_params)
        seen_params["__model__"] = model
        return "tid-clip"

    async def _fake_wait(_tid, *, total_timeout=600, poll_interval=4.0):
        return ["https://cdn.kie/silent.mp4"]

    with patch.object(kie_client, "submit_kie_job", new=_fake_submit), \
         patch.object(kie_client, "wait_for_kie_job", new=_fake_wait):
        url = await kie_client.kling_v21_master_image_to_video(
            image_url="https://img/cover.png", prompt="vibe",
            duration="999",
        )
    assert url == "https://cdn.kie/silent.mp4"
    assert seen_params["duration"] == "10"
    assert seen_params["__model__"] == "kling/v2-1-master-image-to-video"


@pytest.mark.asyncio
async def test_kling_v21_helper_returns_none_when_submit_fails() -> None:
    with patch.object(kie_client, "submit_kie_job",
                       new=AsyncMock(return_value=None)):
        url = await kie_client.kling_v21_master_image_to_video(
            image_url="https://img/cover.png", prompt="vibe",
        )
    assert url is None
