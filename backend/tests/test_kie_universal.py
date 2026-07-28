"""Regression for the universal kie.ai marketplace client.

The original Veo helpers must keep working untouched. The new universal
client surfaces a generic submit + poll pattern across every kie.ai
model family — image, video, music. Tests cover:

  • Provider registry contains every model the SDK references.
  • `kie_provider_info()` correctly resolves known + unknown keys.
  • `submit_kie_task` builds the right URL + body and returns the
    task_id from the response, mocking the HTTP layer.
  • `wait_for_kie_task` polls until success and extracts result URLs.
  • Failure / timeout paths return empty list (not exceptions).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import kie_client


def test_provider_registry_covers_phase1_phase2_phase3():
    """Every model the SDK + frontend references must be in the
    provider registry. If we add a new model, the integration test
    forces us to register it here too."""
    expected_image = {"flux-kontext-pro", "flux-kontext-max",
                      "imagen-4-ultra", "ideogram-3",
                      "seedream-5", "gpt-image-1-5"}
    expected_video = {"kling-3", "kling-3-pro", "wan-2-6", "seedance-1-5",
                      "sora-2", "runway-aleph"}
    expected_music = {"suno"}
    keys = set(kie_client.KIE_PROVIDERS.keys())
    assert expected_image.issubset(keys), f"missing image models: {expected_image - keys}"
    assert expected_video.issubset(keys), f"missing video models: {expected_video - keys}"
    assert expected_music.issubset(keys), f"missing music models: {expected_music - keys}"
    # Type field set correctly
    for k, info in kie_client.KIE_PROVIDERS.items():
        assert info["type"] in {"image", "video", "music"}
        assert "label" in info
        assert "family" in info


def test_kie_provider_info_resolution():
    info = kie_client.kie_provider_info("kling-3")
    assert info and info["type"] == "video"
    assert info["family"] == "kling"
    assert kie_client.kie_provider_info("not-a-real-model") is None
    assert kie_client.kie_provider_info("") is None


@pytest.mark.asyncio
async def test_submit_kie_task_returns_task_id_on_success(monkeypatch):
    """Mock httpx.AsyncClient.post to return a 200 + the canonical kie body."""
    monkeypatch.setenv("KIE_API_KEY", "test-fake-key")
    fake_resp = MagicMock(status_code=200, text="ok")
    fake_resp.json.return_value = {"code": 200, "data": {"taskId": "task-abc"}}
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.post = AsyncMock(return_value=fake_resp)
    with patch.object(kie_client.httpx, "AsyncClient", return_value=fake_client):
        task_id = await kie_client.submit_kie_task(
            model_key="flux-kontext-pro",
            prompt="A cyberpunk neon street",
        )
    assert task_id == "task-abc"
    # Verify the URL used the right family
    fake_client.post.assert_awaited_once()
    call_args = fake_client.post.await_args
    assert "/api/v1/flux/kontext/generate" in call_args.args[0]
    sent_body = call_args.kwargs["json"]
    assert sent_body["prompt"] == "A cyberpunk neon street"
    assert sent_body["model"] == "flux-kontext-pro"


@pytest.mark.asyncio
async def test_submit_kie_task_handles_unknown_model():
    """Unknown model key → return None, don't raise."""
    out = await kie_client.submit_kie_task(model_key="not-real", prompt="hi")
    assert out is None


@pytest.mark.asyncio
async def test_wait_for_kie_task_extracts_urls_on_success(monkeypatch):
    """Polling success → return list of URLs from response.resultUrls."""
    monkeypatch.setenv("KIE_API_KEY", "test-fake-key")
    success_resp = MagicMock(status_code=200)
    success_resp.json.return_value = {
        "data": {
            "successFlag": 1,
            "response": {
                "resultUrls": [
                    "https://cdn.kie.ai/abc.png",
                    "https://cdn.kie.ai/def.png",
                ],
            },
        },
    }
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.get = AsyncMock(return_value=success_resp)
    with patch.object(kie_client.httpx, "AsyncClient", return_value=fake_client):
        urls = await kie_client.wait_for_kie_task(
            "flux-kontext-pro", "task-abc", total_timeout=5,
        )
    assert urls == ["https://cdn.kie.ai/abc.png", "https://cdn.kie.ai/def.png"]


@pytest.mark.asyncio
async def test_wait_for_kie_task_returns_empty_on_failure_flag(monkeypatch):
    """successFlag=2 means the task hard-failed → return []."""
    monkeypatch.setenv("KIE_API_KEY", "test-fake-key")
    fail_resp = MagicMock(status_code=200)
    fail_resp.json.return_value = {"data": {"successFlag": 2,
                                            "errorMsg": "model busy"}}
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.get = AsyncMock(return_value=fail_resp)
    with patch.object(kie_client.httpx, "AsyncClient", return_value=fake_client):
        urls = await kie_client.wait_for_kie_task(
            "kling-3", "task-bad", total_timeout=5,
        )
    assert urls == []


@pytest.mark.asyncio
async def test_extract_kie_urls_handles_missing_fields():
    """Defensive — extractor returns [] for empty / malformed payloads."""
    assert kie_client._extract_kie_urls({}) == []
    assert kie_client._extract_kie_urls({"response": {}}) == []
    assert kie_client._extract_kie_urls(
        {"response": {"audioUrl": "https://x/y.mp3"}}
    ) == ["https://x/y.mp3"]
    assert kie_client._extract_kie_urls({"url": "https://fallback.url"}) == ["https://fallback.url"]


def test_legacy_veo_helpers_still_exist():
    """The original Veo client surface MUST not regress — server.py imports
    these by name. If anyone removes them, this test fires."""
    assert hasattr(kie_client, "submit_video")
    assert hasattr(kie_client, "wait_for_video")
    assert hasattr(kie_client, "generate_video")
    assert hasattr(kie_client, "VIDEO_MODELS")
    assert kie_client.DEFAULT_VIDEO_MODEL == "veo3_fast"
