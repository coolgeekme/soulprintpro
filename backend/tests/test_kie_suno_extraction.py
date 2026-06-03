"""Regression test for the Suno '0:00 / 0:00' bug (Jun 2026).

Bug: users hit `generate_music` via the Chief-of-Staff agent and got
back two audio players showing `0:00 / 0:00` with empty src — even
though "Generated 2 musics with Suno" rendered correctly. Root cause:

  Suno (via kie.ai) populates `streamAudioUrl` on each clip in
  `response.sunoData[]` IMMEDIATELY when the task is created — it's a
  chunked-streaming endpoint that serves audio as it's rendered. The
  final, full-MP3 `audioUrl` only lands a few seconds later. The poll
  loop's "URLs are ready → return early" short-circuit fired on the
  stream URLs, the frontend `<audio>` tag got a streaming endpoint
  with no Content-Length, and HTML5 audio defaults to "0:00 / 0:00"
  when it can't determine duration.

Fix: `_extract_kie_urls(data, require_complete=True)` refuses to
return a clip until its FINAL `audioUrl` has landed (not the stream
fallback). The poll loop passes `require_complete=True` for music
tasks only — image/video tasks keep the original behaviour because
their URLs ARE final when they appear.

These tests pin both pieces.
"""
from __future__ import annotations

from kie_client import _extract_kie_urls


# ── Mid-render: streamAudioUrl present, audioUrl missing ─────────────


def test_suno_midrender_returns_stream_url_without_require_complete() -> None:
    """When we're rendering the FINAL UI (caller knows the task is
    done), accept any URL we have so the user can at least click
    something. This path is exercised by post-task rendering."""
    mid_render = {
        "response": {
            "sunoData": [
                {"streamAudioUrl": "https://suno.kie/stream/clip-a"},
                {"streamAudioUrl": "https://suno.kie/stream/clip-b"},
            ]
        }
    }
    urls = _extract_kie_urls(mid_render, require_complete=False)
    assert urls == [
        "https://suno.kie/stream/clip-a",
        "https://suno.kie/stream/clip-b",
    ]


def test_suno_midrender_returns_empty_when_require_complete() -> None:
    """The exact bug: poll loop must NOT exit on streamAudioUrls
    alone. Returning [] keeps the loop spinning."""
    mid_render = {
        "response": {
            "sunoData": [
                {"streamAudioUrl": "https://suno.kie/stream/clip-a"},
                {"streamAudioUrl": "https://suno.kie/stream/clip-b"},
            ]
        }
    }
    urls = _extract_kie_urls(mid_render, require_complete=True)
    assert urls == []


# ── Final: audioUrl present on both clips ────────────────────────────


def test_suno_final_returns_audio_url_on_both() -> None:
    """When both takes have landed, return both final MP3 URLs."""
    final = {
        "response": {
            "sunoData": [
                {
                    "audioUrl": "https://suno.kie/final/clip-a.mp3",
                    "streamAudioUrl": "https://suno.kie/stream/clip-a",
                },
                {
                    "audioUrl": "https://suno.kie/final/clip-b.mp3",
                    "streamAudioUrl": "https://suno.kie/stream/clip-b",
                },
            ]
        }
    }
    urls = _extract_kie_urls(final, require_complete=True)
    assert urls == [
        "https://suno.kie/final/clip-a.mp3",
        "https://suno.kie/final/clip-b.mp3",
    ]


def test_suno_partial_returns_empty_when_require_complete() -> None:
    """Suno renders two takes. If only one is done, the loop must
    keep waiting — partial returns would only ever show one audio
    player in the chat bubble."""
    partial = {
        "response": {
            "sunoData": [
                {
                    "audioUrl": "https://suno.kie/final/clip-a.mp3",
                    "streamAudioUrl": "https://suno.kie/stream/clip-a",
                },
                {"streamAudioUrl": "https://suno.kie/stream/clip-b"},
            ]
        }
    }
    urls = _extract_kie_urls(partial, require_complete=True)
    assert urls == []


def test_suno_partial_allows_finished_clips_when_not_require_complete() -> None:
    """The rendering path (NOT the poll loop) still surfaces every
    URL it can find — so a clip that finished early still plays."""
    partial = {
        "response": {
            "sunoData": [
                {
                    "audioUrl": "https://suno.kie/final/clip-a.mp3",
                    "streamAudioUrl": "https://suno.kie/stream/clip-a",
                },
                {"streamAudioUrl": "https://suno.kie/stream/clip-b"},
            ]
        }
    }
    urls = _extract_kie_urls(partial, require_complete=False)
    assert "https://suno.kie/final/clip-a.mp3" in urls
    assert "https://suno.kie/stream/clip-b" in urls


# ── Non-Suno families are unchanged ──────────────────────────────────


def test_non_suno_extraction_unchanged_by_flag() -> None:
    """Image / video kie families don't have sunoData — they return
    `resultUrls` directly. The new flag has no effect on them."""
    image_data = {"response": {"resultUrls": ["https://kie/img.png"]}}
    assert _extract_kie_urls(image_data, require_complete=True) == ["https://kie/img.png"]
    assert _extract_kie_urls(image_data, require_complete=False) == ["https://kie/img.png"]


def test_single_audio_url_form_still_works() -> None:
    """Suno's single-clip mode (rare — usually you get 2 takes) puts
    the URL at `response.audioUrl`. Still surface it."""
    single = {"response": {"audioUrl": "https://kie/single.mp3"}}
    urls = _extract_kie_urls(single, require_complete=True)
    assert urls == ["https://kie/single.mp3"]


def test_empty_response_returns_empty() -> None:
    assert _extract_kie_urls({}) == []
    assert _extract_kie_urls({"response": {}}) == []
    assert _extract_kie_urls({"response": {"sunoData": []}}) == []
