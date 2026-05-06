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



# ─────────────────────────────────────────────────────────────────────
# UNIVERSAL KIE.AI MARKETPLACE CLIENT
# ─────────────────────────────────────────────────────────────────────
# The original Veo helpers above are kept intact for backward compat.
# Everything below is the new generic surface that powers image / video /
# music generation across the broader Kie.ai marketplace.
#
# Kie.ai's task pattern is consistent across endpoints:
#   POST /api/v1/<family>/generate  →  { code, data: { taskId } }
#   GET  /api/v1/<family>/record-info?taskId=…  →  { data: { successFlag, response: { resultUrls: [...] } } }
#
# We model each provider here so the rest of the app can call ONE
# function (`submit_kie_task` + `wait_for_kie_task`) regardless of which
# kie.ai model is selected.
# ─────────────────────────────────────────────────────────────────────

# Provider key → kie.ai endpoint family name. The endpoint family is the
# path segment used in /api/v1/<family>/generate and /record-info.
KIE_PROVIDERS: dict[str, dict] = {
    # ── Image models ────────────────────────────────────────────────
    # Flux Kontext — kie.ai's only Flux variants. Note the unusual
    # two-segment family path "flux/kontext"; submit/poll URLs become
    # /api/v1/flux/kontext/generate and /record-info.
    "flux-kontext-pro": {"family": "flux/kontext", "type": "image", "label": "Flux Kontext Pro"},
    "flux-kontext-max": {"family": "flux/kontext", "type": "image", "label": "Flux Kontext Max"},
    "imagen-4-ultra": {"family": "imagen",   "type": "image", "label": "Imagen 4 Ultra"},
    "ideogram-3":     {"family": "ideogram", "type": "image", "label": "Ideogram 3"},
    "seedream-5":     {"family": "seedream", "type": "image", "label": "Seedream 5"},
    "gpt-image-1-5":  {"family": "gpt-image", "type": "image", "label": "GPT Image 1.5"},
    # ── Video models ────────────────────────────────────────────────
    "kling-3":        {"family": "kling",    "type": "video", "label": "Kling 3"},
    "kling-3-pro":    {"family": "kling",    "type": "video", "label": "Kling 3 Pro",
                       "extra": {"mode": "pro"}},
    "wan-2-6":        {"family": "wan",      "type": "video", "label": "Wan 2.6"},
    "seedance-1-5":   {"family": "seedance", "type": "video", "label": "Seedance 1.5"},
    "sora-2":         {"family": "sora",     "type": "video", "label": "Sora 2"},
    "runway-aleph":   {"family": "runway",   "type": "video", "label": "Runway Aleph"},
    # ── Music / audio ───────────────────────────────────────────────
    "suno":           {"family": "generate", "type": "music", "label": "Suno",
                       "no_family_path": True},
}


# How long we wait for each task type. Music / video are slow.
KIE_TASK_TIMEOUTS = {
    "image":  120,
    "video":  600,
    "music":  300,
}


def kie_provider_info(model_key: str) -> Optional[dict]:
    """Resolve the marketplace metadata for a model key. Returns None if
    the model isn't known — caller should fall back to a default path
    (e.g. Nano Banana for image, Veo for video) so the user still gets
    something."""
    return KIE_PROVIDERS.get(model_key)


async def submit_kie_task(
    *, model_key: str, prompt: str,
    image_url: Optional[str] = None,
    image_base64: Optional[str] = None,
    extra: Optional[dict] = None,
) -> Optional[str]:
    """Generic Kie.ai task submitter. Body shape:
        { prompt, model, aspect_ratio?, image?, extra... }

    Returns ``taskId`` or None on failure. We log loudly so the calling
    code path can surface a clean toast to the user. Note: each kie.ai
    model family expects slightly different field names — we send a
    superset, and kie.ai ignores fields it doesn't recognise.
    """
    info = kie_provider_info(model_key)
    if not info:
        log.warning("Unknown kie.ai model: %s", model_key)
        return None
    key = _api_key()
    if not key or not prompt.strip():
        return None
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body: dict = {
        "prompt": prompt.strip()[:2000],
        "model": model_key,
        # Per-family pre-set sane defaults; caller's `extra` overrides.
        "enableTranslation": True,
    }
    # Flux Kontext expects `aspectRatio` (camelCase) and a default 1:1
    # square works for social-post covers. Caller can override via
    # ``extra={"aspectRatio": "16:9"}`` for landscape needs.
    if info.get("family") == "flux/kontext":
        body.setdefault("aspectRatio", "1:1")
        body.setdefault("outputFormat", "png")
    # Suno (kie.ai's only audio model) lives at the bare /api/v1/generate
    # path — there is no /api/v1/suno/generate endpoint. The body shape
    # is also different: it requires `customMode` (false = prompt-only,
    # true = style+title+lyrics control). For a generic "make me a song"
    # we default to customMode=false so the user gets something even
    # with no extra prompt direction.
    if info.get("family") == "generate":
        body.setdefault("customMode", False)
        # `instrumental` is required even in non-custom mode. Default
        # to instrumental=True for ambiguous prompts ("Generate a song")
        # since vocals require explicit lyrics direction to sound good.
        body.setdefault("instrumental", True)
        body.setdefault("model", "V5")
        body.setdefault(
            "callBackUrl",
            os.environ.get(
                "KIE_SUNO_CALLBACK_URL",
                "https://example.com/no-op-callback",
            ),
        )
        if body.get("model") == "suno":
            body["model"] = "V5"
    # Image input (for image-to-image / video-from-image flows)
    if image_url:
        body["image_url"] = image_url
        body["imageUrl"] = image_url
    if image_base64:
        body["image_base64"] = image_base64
    # Per-provider preset
    if info.get("extra"):
        body.update(info["extra"])
    if extra:
        body.update(extra)
    family = info["family"]
    # Suno: kie.ai exposes it at the bare `/api/v1/generate` (no family
    # segment), unlike every other model. Honour the `no_family_path`
    # marker to skip the segment.
    submit_url = (
        f"{KIE_BASE}/api/v1/{family}"
        if info.get("no_family_path")
        else f"{KIE_BASE}/api/v1/{family}/generate"
    )
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(submit_url, headers=headers, json=body)
        if resp.status_code != 200:
            log.warning("Kie %s submit %s: %s", family, resp.status_code, resp.text[:300])
            return None
        data = resp.json() or {}
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Kie %s submit error: %s", family, exc)
        return None
    if data.get("code") not in (200, "200", 0):
        log.warning("Kie %s submit non-OK body: %s", family, data)
        return None
    return ((data.get("data") or {}).get("taskId")
            or (data.get("data") or {}).get("task_id")
            or None)


async def get_kie_task_status(model_key: str, task_id: str) -> dict:
    """Generic record-info reader. Returns the ``data`` dict (or empty)."""
    info = kie_provider_info(model_key)
    if not info or not task_id:
        return {}
    key = _api_key()
    if not key:
        return {}
    family = info["family"]
    headers = {"Authorization": f"Bearer {key}"}
    # Suno status: same `no_family_path` rule — the endpoint is
    # /api/v1/generate/record-info (not /api/v1/suno/record-info).
    status_url = (
        f"{KIE_BASE}/api/v1/{family}/record-info"
    )
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.get(
                status_url, headers=headers, params={"taskId": task_id},
            )
        if resp.status_code != 200:
            log.warning("Kie %s status %s: %s", family, resp.status_code, resp.text[:300])
            return {}
        return (resp.json() or {}).get("data") or {}
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Kie %s status error: %s", family, exc)
        return {}


def _extract_kie_urls(data: dict) -> list[str]:
    """Pull all usable result URLs from a kie.ai task payload. Image
    tasks may return multiple variants; we keep them all so the user can
    pick. Falls back to single-URL fields if `resultUrls` is missing.

    Different kie.ai families serialise results inconsistently:
      • Veo / Suno / Sora: ``response.resultUrls = [url, ...]``
      • Flux Kontext:       ``response.resultImageUrl = url`` (single)
      • Other image models: typically ``response.imageUrl`` or ``response.url``
    We probe all known shapes so callers never have to know which.
    """
    if not data:
        return []
    response = data.get("response") or {}
    urls = response.get("resultUrls") or []
    if isinstance(urls, list) and urls:
        return [u for u in urls if isinstance(u, str)]
    # Suno (kie.ai's audio generator) returns a `sunoData` array of
    # variant clip objects, each with multiple URL fields. We surface
    # the playable audioUrl for every variant so the UI can offer them
    # all (Suno typically returns 2 takes per request).
    suno_data = response.get("sunoData")
    if isinstance(suno_data, list) and suno_data:
        out = []
        for clip in suno_data:
            if isinstance(clip, dict):
                u = (
                    clip.get("audioUrl")
                    or clip.get("streamAudioUrl")
                    or clip.get("sourceAudioUrl")
                )
                if isinstance(u, str) and u:
                    out.append(u)
        if out:
            return out
    # Single-URL forms used by various kie.ai providers
    single = (
        response.get("resultImageUrl")     # Flux Kontext
        or response.get("imageUrl")        # Imagen / Ideogram / GPT Image
        or response.get("videoUrl")        # Some video providers
        or response.get("audioUrl")        # Suno (single-clip mode)
        or response.get("url")             # Generic
        or data.get("url")
    )
    return [single] if single else []


async def wait_for_kie_task(
    model_key: str, task_id: str,
    *, total_timeout: Optional[int] = None,
    poll_interval: float = POLL_INTERVAL,
) -> list[str]:
    """Poll the kie.ai task until done, returning a list of result URLs.
    Empty list on failure / timeout."""
    info = kie_provider_info(model_key)
    if not info or not task_id:
        return []
    if total_timeout is None:
        total_timeout = KIE_TASK_TIMEOUTS.get(info["type"], 300)
    elapsed = 0.0
    while elapsed < total_timeout:
        data = await get_kie_task_status(model_key, task_id)
        flag = data.get("successFlag")
        # Some kie families (Suno) populate the result payload before
        # setting `successFlag`, while others don't expose the flag at
        # all. Treat "URLs are ready" as the universal success marker
        # alongside the explicit flag, so we don't poll forever after
        # the audio is already playable.
        if flag == 1 or (flag is None and _extract_kie_urls(data)):
            return _extract_kie_urls(data)
        if flag in (2, 3):
            log.warning("Kie %s task failed: %s", info["family"], data)
            return []
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    log.warning("Kie %s task timed out after %ss (task_id=%s)",
                info["family"], total_timeout, task_id)
    return []


async def generate_via_kie(
    *, model_key: str, prompt: str,
    image_url: Optional[str] = None,
    image_base64: Optional[str] = None,
    extra: Optional[dict] = None,
    total_timeout: Optional[int] = None,
) -> list[str]:
    """End-to-end helper: submit + poll → final result URL list (image,
    video, or audio depending on the provider). Returns [] on failure."""
    task_id = await submit_kie_task(
        model_key=model_key, prompt=prompt,
        image_url=image_url, image_base64=image_base64, extra=extra,
    )
    if not task_id:
        return []
    return await wait_for_kie_task(model_key, task_id, total_timeout=total_timeout)


__all__ = [
    # Original Veo helpers (kept for backward compat)
    "submit_video", "wait_for_video", "generate_video", "get_video_status",
    "VIDEO_MODELS", "DEFAULT_VIDEO_MODEL",
    # New universal client
    "KIE_PROVIDERS", "KIE_TASK_TIMEOUTS", "kie_provider_info",
    "submit_kie_task", "get_kie_task_status", "wait_for_kie_task",
    "generate_via_kie",
]
