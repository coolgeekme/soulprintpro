"""music_video.py — assemble a music video from one base image + one song.

Path A pipeline (Feb 2026):
  1. Probe the song's duration with `ffprobe`.
  2. Generate a single ~10s silent clip via Kie.ai's Kling 2.1 Master
     Image-to-Video, seeded with the user's base image + vibe prompt.
  3. ffmpeg-loop the clip to ≥ song length, mux the audio, trim to the
     exact song duration, output an H.264 + AAC MP4.

We keep this as a standalone module (not tucked inside missions.py)
because:
  • the ffmpeg subprocess work is heavy and easier to unit-test alone;
  • the same builder is reused by the agent tool AND the Social Queue
    HTTP endpoint;
  • mocking the kie.ai call + ffmpeg subprocess in isolation gives us
    deterministic tests that don't burn real credits.

The public entrypoint is `build_music_video_bytes(...)` which returns
the final MP4 bytes — saving to GridFS / disk is the caller's job
(matches the existing `_save_remote_url_to_uploads` + `save_artifacts`
patterns the rest of the app uses).
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("music_video")

# Kling 2.1 Master accepts only 5 or 10 second clip lengths.
# Always pick 10 — fewer Kie.ai round-trips per minute of song.
KLING_CLIP_LEN = 10

# Hard ceiling so a buggy / misreported song duration never spins us
# into a 100-minute ffmpeg render. Suno tracks top out around 8 min;
# 15 min is a safe upper bound for any reasonable music video.
MAX_OUTPUT_SECONDS = 60 * 15

# Minimum song duration we'll bother making a video for — covers
# Suno preview clips (5-10s) and the trivial-input edge case.
MIN_OUTPUT_SECONDS = 3.0


class MusicVideoError(RuntimeError):
    """Raised when any stage of the build pipeline fails. Caller can
    surface `.args[0]` directly to the user — messages are written to
    be human-readable."""


def _which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def ffmpeg_available() -> bool:
    return bool(_which("ffmpeg")) and bool(_which("ffprobe"))


async def _run_subprocess(args: list[str], *, timeout: float = 300.0) -> tuple[int, bytes, bytes]:
    """Run a subprocess, capturing stdout/stderr. Raises asyncio.TimeoutError
    on timeout (caller maps to MusicVideoError)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise
    return proc.returncode or 0, stdout or b"", stderr or b""


async def probe_audio_duration(audio_path: Path) -> float:
    """Return the audio file's duration in seconds. Uses ffprobe."""
    if not _which("ffprobe"):
        raise MusicVideoError("ffprobe is not installed on this server.")
    code, stdout, stderr = await _run_subprocess(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        timeout=60.0,
    )
    if code != 0:
        raise MusicVideoError(
            f"Couldn't read song duration ({stderr.decode('utf-8', 'ignore')[:200]})."
        )
    try:
        return float(stdout.decode("utf-8", "ignore").strip())
    except (ValueError, TypeError):
        raise MusicVideoError("Song file produced an unreadable duration.")


async def _download(url: str, dest: Path, *, timeout: float = 300.0) -> None:
    """Download a URL to disk. Raises MusicVideoError on any failure."""
    if not url:
        raise MusicVideoError("Missing source URL.")
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise MusicVideoError(f"Download failed: {exc}") from exc
    if resp.status_code != 200 or not resp.content:
        raise MusicVideoError(
            f"Download returned HTTP {resp.status_code} (url={url[:120]})"
        )
    dest.write_bytes(resp.content)
    if dest.stat().st_size < 1024:
        raise MusicVideoError("Downloaded file is suspiciously small (<1 KB).")


async def stitch_video_to_song(
    *, silent_video_path: Path, song_path: Path, output_path: Path,
    target_seconds: float,
) -> None:
    """ffmpeg pipeline:
      • `-stream_loop -1` repeats the silent video forever
      • `-i song`         attaches the audio
      • `-map 0:v -map 1:a` selects video from #0, audio from #1
      • `-t <secs>`       cuts the output at the song length
      • H.264 + AAC, faststart for browser playback
    """
    if not _which("ffmpeg"):
        raise MusicVideoError("ffmpeg is not installed on this server.")
    if target_seconds <= 0:
        raise MusicVideoError("Target output length must be > 0.")
    args = [
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", str(silent_video_path),
        "-i", str(song_path),
        "-map", "0:v:0", "-map", "1:a:0",
        "-t", f"{target_seconds:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-shortest",
        str(output_path),
    ]
    # Long timeout — encoding a 5-minute 1080p clip on a small VM
    # takes 30-90s. 10 minutes is the absolute upper bound.
    code, _stdout, stderr = await _run_subprocess(args, timeout=600.0)
    if code != 0 or not output_path.is_file() or output_path.stat().st_size < 1024:
        raise MusicVideoError(
            "ffmpeg failed to stitch the music video: "
            + stderr.decode("utf-8", "ignore")[-500:]
        )


async def generate_silent_clip_kling(
    *, image_url: str, vibe_prompt: str,
) -> str:
    """Submit a Kling 2.1 Master Image-to-Video task → return final mp4 URL.

    Always asks for the 10s tier (cheapest cost-per-second + fewer
    Kie.ai round-trips to cover a full song). Kept as a thin wrapper
    so tests can monkey-patch this single function."""
    from kie_client import kling_v21_master_image_to_video
    url = await kling_v21_master_image_to_video(
        image_url=image_url,
        prompt=vibe_prompt or "Smooth, cinematic motion, slow camera drift",
        duration=str(KLING_CLIP_LEN),
    )
    if not url:
        raise MusicVideoError(
            "Kling 2.1 Master refused or timed out. Try again, or "
            "rephrase the vibe prompt — extremely literal prompts work best."
        )
    return url


async def build_music_video_bytes(
    *, image_url: str, song_url: str,
    vibe_prompt: str = "",
    max_seconds: Optional[float] = None,
    workdir: Optional[Path] = None,
) -> bytes:
    """Full pipeline. Returns the final MP4 bytes.

    Args:
      image_url:   public URL to the base image (jpg/png; ≤10 MB per kie.ai limits)
      song_url:    public URL or local-path-resolvable URL to the audio
      vibe_prompt: optional cinematic direction for Kling
      max_seconds: cap on output length (default = song duration, no cap)
      workdir:     optional tempdir override (mostly for tests)

    Raises MusicVideoError on any failure.
    """
    if not ffmpeg_available():
        raise MusicVideoError("Server is missing ffmpeg / ffprobe.")
    if not image_url:
        raise MusicVideoError("Need a base image URL.")
    if not song_url:
        raise MusicVideoError("Need a song URL.")

    if workdir is None:
        work = Path(tempfile.mkdtemp(prefix="mv-"))
        cleanup = True
    else:
        work = Path(workdir)
        work.mkdir(parents=True, exist_ok=True)
        cleanup = False

    try:
        song_path = work / "song.mp3"
        silent_path = work / "silent.mp4"
        output_path = work / "out.mp4"

        # 1) Download the song first — cheapest call, fail fast if the
        #    URL is dead before we burn Kling credits.
        await _download(song_url, song_path)
        song_duration = await probe_audio_duration(song_path)
        if song_duration < MIN_OUTPUT_SECONDS:
            raise MusicVideoError(
                f"Song is too short ({song_duration:.1f}s) — need ≥ "
                f"{MIN_OUTPUT_SECONDS}s of audio to make a music video."
            )
        target = song_duration
        if max_seconds and max_seconds > 0:
            target = min(target, float(max_seconds))
        if target > MAX_OUTPUT_SECONDS:
            log.info("Music video target %ss exceeds cap %ss, clamping.",
                     target, MAX_OUTPUT_SECONDS)
            target = MAX_OUTPUT_SECONDS

        # 2) Generate ONE Kling clip — `-stream_loop -1` in ffmpeg
        #    handles the looping, so we don't pay for multiple Kling
        #    calls regardless of song length.
        silent_url = await generate_silent_clip_kling(
            image_url=image_url, vibe_prompt=vibe_prompt,
        )
        await _download(silent_url, silent_path)

        # 3) Stitch — ffmpeg loops the silent clip until song length.
        await stitch_video_to_song(
            silent_video_path=silent_path,
            song_path=song_path,
            output_path=output_path,
            target_seconds=target,
        )
        return output_path.read_bytes()
    finally:
        if cleanup:
            try:
                shutil.rmtree(work, ignore_errors=True)
            except OSError:
                pass


__all__ = [
    "MusicVideoError",
    "ffmpeg_available",
    "probe_audio_duration",
    "stitch_video_to_song",
    "generate_silent_clip_kling",
    "build_music_video_bytes",
    "MAX_OUTPUT_SECONDS",
    "MIN_OUTPUT_SECONDS",
    "KLING_CLIP_LEN",
]
