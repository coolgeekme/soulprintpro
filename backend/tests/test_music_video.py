"""Tests for the music_video assembly pipeline.

We mock the kie.ai network call so tests run offline and never burn
real credits. The ffmpeg subprocess IS exercised end-to-end — it's the
core of the feature and the assertions about song duration + output
existence wouldn't be meaningful with a stubbed encoder.

A pre-baked silent.mp4 + sine-wave .mp3 are generated on-the-fly with
ffmpeg's built-in synthesizers so the test stays self-contained.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import music_video

pytestmark = pytest.mark.skipif(
    not music_video.ffmpeg_available(),
    reason="ffmpeg/ffprobe not installed in this environment.",
)


@pytest.fixture(scope="module")
def tiny_inputs(tmp_path_factory):
    """Generate a tiny silent video + 6-second sine tone for testing."""
    work = tmp_path_factory.mktemp("mv-inputs")
    silent = work / "silent.mp4"
    song = work / "song.mp3"
    # 2-second 320x320 silent clip (color bars) — cheap to encode.
    asyncio.get_event_loop().run_until_complete(
        music_video._run_subprocess([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=320x320:d=2",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
            str(silent),
        ], timeout=60.0)
    )
    # 6-second 440Hz sine tone @ 96k mono — keeps the test fast.
    asyncio.get_event_loop().run_until_complete(
        music_video._run_subprocess([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
            "-ac", "1", "-b:a", "96k", str(song),
        ], timeout=60.0)
    )
    return {"silent": silent, "song": song, "work": work}


@pytest.mark.asyncio
async def test_probe_audio_duration_matches_expected(tiny_inputs) -> None:
    dur = await music_video.probe_audio_duration(tiny_inputs["song"])
    # ffmpeg's sine generator is sample-accurate; 6.0s ± 0.1
    assert 5.9 < dur < 6.1


@pytest.mark.asyncio
async def test_stitch_loops_to_target_length(tiny_inputs, tmp_path) -> None:
    out = tmp_path / "out.mp4"
    await music_video.stitch_video_to_song(
        silent_video_path=tiny_inputs["silent"],
        song_path=tiny_inputs["song"],
        output_path=out,
        target_seconds=6.0,
    )
    assert out.is_file()
    assert out.stat().st_size > 1024
    # Probe output to confirm length is ~6s (2s silent looped 3x)
    code, stdout, _ = await music_video._run_subprocess([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(out),
    ])
    assert code == 0
    dur = float(stdout.decode().strip())
    assert 5.5 < dur < 6.5


@pytest.mark.asyncio
async def test_build_music_video_bytes_end_to_end(tiny_inputs, tmp_path) -> None:
    """Mock the kie.ai call to return our pre-baked silent.mp4 via
    file:// URL — exercises the real download + ffmpeg pipeline."""
    silent_url = "https://example.com/fake-silent.mp4"
    song_url = "https://example.com/fake-song.mp3"
    silent_bytes = tiny_inputs["silent"].read_bytes()
    song_bytes = tiny_inputs["song"].read_bytes()

    async def fake_download(url, dest, *, timeout=300.0):
        if url == silent_url:
            dest.write_bytes(silent_bytes)
        elif url == song_url:
            dest.write_bytes(song_bytes)
        else:
            raise music_video.MusicVideoError(f"Unexpected URL: {url}")

    async def fake_kling(*, image_url, vibe_prompt):
        return silent_url

    with patch.object(music_video, "_download", new=fake_download), \
         patch.object(music_video, "generate_silent_clip_kling",
                       new=fake_kling):
        out_bytes = await music_video.build_music_video_bytes(
            image_url="https://example.com/cover.png",
            song_url=song_url,
            vibe_prompt="cinematic drift",
        )
    assert isinstance(out_bytes, bytes)
    assert len(out_bytes) > 4096  # real MP4 header + a few frames


@pytest.mark.asyncio
async def test_build_rejects_too_short_song(tmp_path) -> None:
    """A 0.5s 'song' is meaningless — fail fast with a clear message."""
    if not music_video.ffmpeg_available():
        pytest.skip("ffmpeg missing")
    short_song = tmp_path / "blip.mp3"
    code, _, _ = await music_video._run_subprocess([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "sine=frequency=440:duration=0.5",
        "-ac", "1", str(short_song),
    ])
    assert code == 0
    short_bytes = short_song.read_bytes()

    async def fake_dl(url, dest, *, timeout=300.0):
        dest.write_bytes(short_bytes)

    async def fake_kling(*, image_url, vibe_prompt):
        return "https://example.com/silent.mp4"

    with patch.object(music_video, "_download", new=fake_dl), \
         patch.object(music_video, "generate_silent_clip_kling",
                       new=fake_kling):
        with pytest.raises(music_video.MusicVideoError) as exc:
            await music_video.build_music_video_bytes(
                image_url="https://example.com/cover.png",
                song_url="https://example.com/short.mp3",
            )
    assert "too short" in str(exc.value).lower()
