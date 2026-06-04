"""Pod-restart resilience for chat-message video generation.

The `_kick_off_video` helper (server.py) must:
  1. Submit the Kie.ai task SYNCHRONOUSLY before returning, so we have a
     `taskId` to persist on the placeholder message.
  2. Stamp `kie_task_id` + `kie_model` on the inserted message.
  3. Fire the background poll task only after persistence.

Without (1)+(2), a pod restart kills the background asyncio task and
the placeholder message stays `media_status="pending"` forever — the
production bug the user reported ("keeps replying it's still generating").
"""
import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest


class _FakeCollection:
    def __init__(self, sink):
        self._sink = sink

    async def insert_one(self, doc):
        self._sink.append({**doc})
        return type("R", (), {"inserted_id": "fake"})()

    async def update_one(self, *_a, **_k):
        return type("R", (), {"modified_count": 1})()


class _FakeDB:
    def __init__(self, sink):
        self._sink = sink
        self._messages = _FakeCollection(sink)

    @property
    def messages(self):
        return self._messages


@pytest.mark.asyncio
async def test_kick_off_video_persists_kie_task_id(monkeypatch):
    import server

    fake_task_id = f"task-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(server, "kie_submit_video", AsyncMock(return_value=fake_task_id))

    async def _hang_forever(*_a, **_k):
        await asyncio.sleep(3600)
    monkeypatch.setattr(server, "kie_wait_for_video", _hang_forever)
    monkeypatch.setattr(server, "kie_generate_video", _hang_forever)

    sink: list[dict] = []
    monkeypatch.setattr(server, "db", _FakeDB(sink))

    test_agent_id = next(iter(server.AGENTS.keys()))

    msg = await server._kick_off_video(
        conv_id=f"conv-test-{uuid.uuid4().hex[:8]}",
        agent_id=test_agent_id,
        prompt="a test cinematic shot of a forest at dawn",
        aspect_ratio="16:9",
    )

    server.kie_submit_video.assert_awaited_once()
    assert msg["media_status"] == "pending"
    assert msg["kie_task_id"] == fake_task_id
    assert msg["kie_model"] == server.DEFAULT_VIDEO_MODEL_LOCAL
    assert msg["media_type"] == "video"
    assert "media_url" not in msg

    assert len(sink) == 1
    assert sink[0]["kie_task_id"] == fake_task_id
    assert sink[0]["media_status"] == "pending"

    # Cancel background poll task so teardown is clean.
    for t in asyncio.all_tasks():
        if t is not asyncio.current_task():
            t.cancel()


@pytest.mark.asyncio
async def test_kick_off_video_fails_fast_on_submit_failure(monkeypatch):
    """If Kie submit returns None, the placeholder lands as `failed`
    immediately so the user sees a real error instead of a spinner that
    never resolves."""
    import server

    monkeypatch.setattr(server, "kie_submit_video", AsyncMock(return_value=None))

    sink: list[dict] = []
    monkeypatch.setattr(server, "db", _FakeDB(sink))

    test_agent_id = next(iter(server.AGENTS.keys()))

    msg = await server._kick_off_video(
        conv_id=f"conv-test-{uuid.uuid4().hex[:8]}",
        agent_id=test_agent_id,
        prompt="another test prompt",
        aspect_ratio="9:16",
    )

    assert msg["media_status"] == "failed", msg
    assert "kie_task_id" not in msg
    assert len(sink) == 1
    assert sink[0]["media_status"] == "failed"
