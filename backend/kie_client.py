"""Kie.ai async client (video generation via Veo 3.1).

Submit + poll workflow. Returns a public CDN URL we can stream from the agent
message bubble.
"""
from __future__ import annotations
import asyncio
import logging
import os
from typing import Optional

import httpx

log = logging.getLogger("kie")

KIE_BASE = "https://api.kie.ai"
DEFAULT_TIMEOUT = 8.0  # per-request HTTP timeout
POLL_INTERVAL = 4.0
DEFAULT_TOTAL_TIMEOUT = 360  # generous — Veo can take 60-180s

VIDEO_MODELS = {"veo3_lite", "veo3_fast", "veo3"}
DEFAULT_VIDEO_MODEL = "veo3_fast"


def _api_key() -> Optional[str]:
    return os.environ.get("KIE_API_KEY") or None


async def submit_video(prompt: str, *, model: str = DEFAULT_VIDEO_MODEL,
                       aspect_ratio: str = "16:9",
                       resolution: str = "720p") -> Optional[str]:
    """Submit a Veo 3.1 generation. Returns task_id, or None on failure."""
    key = _api_key()
    if not key or not prompt.strip():
        return None
    if model not in VIDEO_MODELS:
        model = DEFAULT_VIDEO_MODEL
    if aspect_ratio not in ("16:9", "9:16", "Auto"):
        aspect_ratio = "16:9"
    if resolution not in ("720p", "1080p", "4k"):
        resolution = "720p"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {
        "prompt": prompt.strip()[:1500],
        "model": model,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "enableTranslation": True,
    }
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(f"{KIE_BASE}/api/v1/veo/generate",
                                     headers=headers, json=body)
        if resp.status_code != 200:
            log.warning("Kie submit_video %s: %s", resp.status_code, resp.text[:300])
            return None
        data = resp.json() or {}
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Kie submit_video error: %s", exc)
        return None
    if data.get("code") != 200:
        log.warning("Kie submit_video non-200 in body: %s", data)
        return None
    return ((data.get("data") or {}).get("taskId")) or None


async def get_video_status(task_id: str) -> dict:
    """Return raw record-info payload (with successFlag, response.resultUrls etc.)."""
    key = _api_key()
    if not key or not task_id:
        return {}
    headers = {"Authorization": f"Bearer {key}"}
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.get(f"{KIE_BASE}/api/v1/veo/record-info",
                                    headers=headers, params={"taskId": task_id})
        if resp.status_code != 200:
            log.warning("Kie status %s: %s", resp.status_code, resp.text[:300])
            return {}
        return (resp.json() or {}).get("data") or {}
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Kie status error: %s", exc)
        return {}


def _extract_video_url(data: dict) -> Optional[str]:
    """Pull the first usable video URL from the polling payload."""
    if not data:
        return None
    response = data.get("response") or {}
    urls = response.get("resultUrls") or []
    if isinstance(urls, list) and urls:
        return urls[0]
    return data.get("videoUrl") or data.get("url") or None


async def wait_for_video(task_id: str, *, total_timeout: int = DEFAULT_TOTAL_TIMEOUT,
                         poll_interval: float = POLL_INTERVAL) -> Optional[str]:
    """Poll the Veo task until completion. Returns video URL or None on
    failure / timeout. Caller should treat None as a soft failure."""
    if not task_id:
        return None
    elapsed = 0.0
    while elapsed < total_timeout:
        data = await get_video_status(task_id)
        # Status flags: successFlag 1 = success, 0 = pending, 2/3 = failed
        flag = data.get("successFlag")
        if flag == 1:
            return _extract_video_url(data)
        if flag in (2, 3):
            log.warning("Kie video failed: %s", data)
            return None
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    log.warning("Kie video timed out after %ss for task %s", total_timeout, task_id)
    return None


async def generate_video(prompt: str, *, model: str = DEFAULT_VIDEO_MODEL,
                         aspect_ratio: str = "16:9",
                         resolution: str = "720p",
                         total_timeout: int = DEFAULT_TOTAL_TIMEOUT) -> Optional[str]:
    """End-to-end helper: submit + poll → final video URL (or None)."""
    task_id = await submit_video(prompt, model=model, aspect_ratio=aspect_ratio,
                                 resolution=resolution)
    if not task_id:
        return None
    return await wait_for_video(task_id, total_timeout=total_timeout)
