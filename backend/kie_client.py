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
    failure / timeout. Caller should treat None as a soft failure.

    Per the official Veo3.1 docs (Jun 2026):
      • ``data.successFlag`` is the canonical signal: 0=generating,
        1=success, 2=failed (before completion), 3=generation_failed
        (upstream provider failed).
      • The result URL lives at ``data.response.resultUrls[0]``.
    The string ``data.status`` is also honoured as a fallback in case
    kie.ai migrates Veo to the Suno-style status convention later.
    """
    if not task_id:
        return None
    elapsed = 0.0
    data: dict = {}
    while elapsed < total_timeout:
        data = await get_video_status(task_id)
        # Newer string status — defensive fallback only.
        raw_status = data.get("status")
        status = raw_status.upper() if isinstance(raw_status, str) else ""
        if status == "SUCCESS":
            return _extract_video_url(data)
        if status and status not in ("PENDING", "GENERATING", "PROCESSING", ""):
            log.warning("Kie video failed (status=%s, errorCode=%s, msg=%s)",
                        status, data.get("errorCode"), data.get("errorMessage"))
            return None
        # Canonical numeric flag.
        flag = data.get("successFlag")
        if flag == 1:
            return _extract_video_url(data)
        if flag in (2, 3):
            log.warning("Kie video failed (flag=%s, errorCode=%s, msg=%s)",
                        flag, data.get("errorCode"), data.get("errorMessage"))
            return None
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    log.warning("Kie video timed out after %ss (task_id=%s, last_status=%s, last_flag=%s)",
                total_timeout, task_id,
                data.get("status"), data.get("successFlag"))
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


def _extract_kie_urls(data: dict, *, require_complete: bool = False) -> list[str]:
    """Pull all usable result URLs from a kie.ai task payload. Image
    tasks may return multiple variants; we keep them all so the user can
    pick. Falls back to single-URL fields if `resultUrls` is missing.

    Different kie.ai families serialise results inconsistently:
      • Veo / Suno / Sora: ``response.resultUrls = [url, ...]``
      • Flux Kontext:       ``response.resultImageUrl = url`` (single)
      • Other image models: typically ``response.imageUrl`` or ``response.url``
    We probe all known shapes so callers never have to know which.

    `require_complete=True` (set by the poll-loop early-exit for Suno):
    refuses to return a clip's streaming URL as a final result. Suno
    populates `streamAudioUrl` while the track is still being rendered
    — that endpoint serves chunked audio with no Content-Length, so
    HTML5 `<audio>` displays "0:00 / 0:00" and the user thinks the
    track failed. The real, playable `audioUrl` lands a few seconds
    later. Wait for it.
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
                # Prefer the FINAL `audioUrl` (a real .mp3 with
                # Content-Length headers). Fall back to streamAudioUrl
                # / sourceAudioUrl only when `require_complete=False`
                # (e.g. final result-rendering, not poll-loop probing).
                final_url = clip.get("audioUrl")
                if isinstance(final_url, str) and final_url:
                    out.append(final_url)
                elif not require_complete:
                    fallback = (
                        clip.get("streamAudioUrl")
                        or clip.get("sourceAudioUrl")
                    )
                    if isinstance(fallback, str) and fallback:
                        out.append(fallback)
        # If `require_complete=True` and ANY clip is still missing the
        # final URL, return [] so the poll loop keeps waiting instead
        # of returning a partial result.
        if require_complete and len(out) < len(suno_data):
            return []
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
    Empty list on failure / timeout.

    Kie.ai uses two parallel status conventions depending on model
    family (confirmed Jun 2026 against the live docs at
    https://docs.kie.ai):
      • **Suno** (music): ``data.status`` is a string. Documented
        values:
            ``PENDING``           - waiting to be processed
            ``TEXT_SUCCESS``      - lyrics done, audio still rendering
            ``FIRST_SUCCESS``     - first track ready, second still cooking
            ``SUCCESS``           - all tracks ready (return URLs)
            ``CREATE_TASK_FAILED`` - task submission failed
            ``GENERATE_AUDIO_FAILED`` - render failed
            ``CALLBACK_EXCEPTION`` - kie.ai callback machinery exploded
            ``SENSITIVE_WORD_ERROR`` - prompt rejected by content filter
        We treat *every* status that isn't SUCCESS / PENDING /
        TEXT_SUCCESS / FIRST_SUCCESS as a terminal failure — important
        because the original substring-match-on-"FAIL" missed
        CALLBACK_EXCEPTION and SENSITIVE_WORD_ERROR, spinning to
        timeout.
      • **Veo + numeric families** (image / video): ``data.successFlag``
        is 0 (generating) / 1 (success) / 2 or 3 (failed).
    """
    info = kie_provider_info(model_key)
    if not info or not task_id:
        return []
    if total_timeout is None:
        total_timeout = KIE_TASK_TIMEOUTS.get(info["type"], 300)
    is_music = info.get("type") == "music"
    SUNO_TERMINAL_OK = {"SUCCESS"}
    SUNO_IN_PROGRESS = {"PENDING", "TEXT_SUCCESS", "FIRST_SUCCESS", ""}
    elapsed = 0.0
    while elapsed < total_timeout:
        data = await get_kie_task_status(model_key, task_id)
        # Newer string status (canonical for Suno).
        raw_status = data.get("status")
        status = raw_status.upper() if isinstance(raw_status, str) else ""
        if status in SUNO_TERMINAL_OK:
            return _extract_kie_urls(data)
        if status and status not in SUNO_IN_PROGRESS:
            # Any non-SUCCESS / non-in-progress string is a terminal
            # failure — covers FAILED, CALLBACK_EXCEPTION,
            # SENSITIVE_WORD_ERROR, plus any future failure codes
            # kie.ai adds without an SDK update.
            log.warning("Kie %s task failed (status=%s, errorCode=%s, msg=%s)",
                        info["family"], status,
                        data.get("errorCode"), data.get("errorMessage"))
            return []
        # Older numeric flag (Veo / image families).
        flag = data.get("successFlag")
        if flag == 1:
            return _extract_kie_urls(data)
        if flag in (2, 3):
            log.warning("Kie %s task failed (flag=%s, errorCode=%s, msg=%s)",
                        info["family"], flag,
                        data.get("errorCode"), data.get("errorMessage"))
            return []
        # Fallback: NEITHER status string NOR numeric flag is present.
        # For music, only accept URLs when ALL clips have the final
        # `audioUrl` (never trust `streamAudioUrl` alone — that's the
        # chunked-streaming endpoint and yields '0:00 / 0:00' in HTML5
        # audio). For image / video, URLs are always final when they
        # appear.
        if status == "" and flag is None:
            urls = _extract_kie_urls(data, require_complete=is_music)
            if urls:
                return urls
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    log.warning("Kie %s task timed out after %ss (task_id=%s, last_status=%s, last_flag=%s)",
                info["family"], total_timeout, task_id,
                data.get("status") if 'data' in locals() else None,
                data.get("successFlag") if 'data' in locals() else None)
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
    urls = await wait_for_kie_task(model_key, task_id, total_timeout=total_timeout)
    # Per-call cost log so the admin dashboard reflects every kie.ai
    # generation. We map model_key → provider rate, defaulting to the
    # generic image/video bucket if unknown.
    try:
        from external_api_costs import log_external_call_fire_and_forget
        # Provider routing: kie.suno, kie.veo3_fast, kie.veo3, ... else
        # kie.image.default / kie.video.default depending on the model.
        prov = f"kie.{model_key}"
        VIDEO_KEYS = {"veo3", "veo3_fast", "veo3_lite", "sora-2",
                      "kling-3", "kling-3-pro", "wan-2-6", "seedance-1-5",
                      "runway-aleph"}
        if model_key == "suno":
            prov = "kie.suno"
        elif model_key in VIDEO_KEYS and prov not in {
            f"kie.{k}" for k in ("veo3", "veo3_fast", "veo3_lite", "sora-2",
                                  "kling-3", "kling-3-pro")
        }:
            prov = "kie.video.default"
        elif model_key not in {"suno"} and model_key not in VIDEO_KEYS:
            prov = "kie.image.default"
        # Estimate seconds for video calls based on the duration the
        # caller asked for (defaults to 8s — Veo3's standard length).
        seconds = 0.0
        if "video" in prov or model_key in VIDEO_KEYS:
            seconds = float((extra or {}).get("duration") or (extra or {}).get("seconds") or 8)
        log_external_call_fire_and_forget(
            provider=prov, kind_label=f"kie:{model_key}",
            calls=1, seconds=seconds,
            success=bool(urls),
            extra={"model_key": model_key, "task_id": task_id},
        )
    except Exception:
        pass
    return urls


# ─────────────────────────────────────────────────────────────────────
# KIE.AI UNIFIED JOBS API ("Market" models)
# ─────────────────────────────────────────────────────────────────────
# Newer kie.ai models (Kling V2.1 Master, Wan 2.7, Seedream 5, etc.)
# expose a UNIFIED submit/poll surface at
#   POST /api/v1/jobs/createTask
#   GET  /api/v1/jobs/recordInfo?taskId=…
# instead of the per-family /api/v1/<family>/generate path used above.
#
# Request body shape (Market):
#   { "model": "kling/v2-1-master-image-to-video",
#     "callBackUrl": "...",  # optional
#     "input": { ...model-specific args... } }
#
# Response shape (recordInfo data field):
#   { "taskId": "...", "model": "...",
#     "state": "waiting"|"queuing"|"generating"|"success"|"fail",
#     "resultJson": "{\"resultUrls\":[\"...\"]}",   # only on success
#     "failCode": "...", "failMsg": "..." }
#
# We keep this surface separate from `submit_kie_task` because the
# request/response shapes are genuinely different and conflating them
# leads to silent failures.
# ─────────────────────────────────────────────────────────────────────

import json

KIE_JOBS_SUBMIT_URL = f"{KIE_BASE}/api/v1/jobs/createTask"
KIE_JOBS_STATUS_URL = f"{KIE_BASE}/api/v1/jobs/recordInfo"

# Terminal-success / in-progress states for the Market jobs API.
KIE_JOB_OK = {"success"}
KIE_JOB_IN_PROGRESS = {"waiting", "queuing", "generating", ""}
# Anything not in OK or IN_PROGRESS is a terminal failure
# (canonical "fail", plus any future failure code kie.ai introduces).


async def submit_kie_job(
    *, model: str, input_params: dict,
    callback_url: Optional[str] = None,
) -> Optional[str]:
    """Submit a Market-style task at /api/v1/jobs/createTask.

    Returns ``taskId`` or None on failure. ``model`` is the literal
    string kie.ai expects (e.g. ``"kling/v2-1-master-image-to-video"``).
    ``input_params`` is the inner ``input`` object — caller is
    responsible for the exact keys each model needs (the docs surface
    differs per family).
    """
    key = _api_key()
    if not key or not model or not isinstance(input_params, dict):
        return None
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body: dict = {"model": model, "input": input_params}
    cb = callback_url or os.environ.get("KIE_JOBS_CALLBACK_URL")
    if cb:
        body["callBackUrl"] = cb
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(KIE_JOBS_SUBMIT_URL, headers=headers, json=body)
        if resp.status_code != 200:
            log.warning("Kie jobs submit %s: %s", resp.status_code, resp.text[:400])
            return None
        data = resp.json() or {}
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Kie jobs submit error: %s", exc)
        return None
    if data.get("code") not in (200, "200", 0):
        log.warning("Kie jobs submit non-OK body: %s", data)
        return None
    return ((data.get("data") or {}).get("taskId")
            or (data.get("data") or {}).get("task_id")
            or None)


async def get_kie_job_state(task_id: str) -> dict:
    """Return the ``data`` object from /jobs/recordInfo (or {})."""
    key = _api_key()
    if not key or not task_id:
        return {}
    headers = {"Authorization": f"Bearer {key}"}
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.get(KIE_JOBS_STATUS_URL,
                                    headers=headers,
                                    params={"taskId": task_id})
        if resp.status_code != 200:
            log.warning("Kie jobs status %s: %s", resp.status_code, resp.text[:300])
            return {}
        return (resp.json() or {}).get("data") or {}
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Kie jobs status error: %s", exc)
        return {}


def _extract_jobs_urls(data: dict) -> list[str]:
    """Parse ``resultJson`` (a JSON STRING) → list of result URLs.
    The Market API serializes results as a string so the surface stays
    polymorphic across image / video / audio. We tolerate missing /
    malformed payloads silently — caller treats [] as failure."""
    raw = data.get("resultJson")
    if not raw or not isinstance(raw, str):
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    urls = parsed.get("resultUrls") if isinstance(parsed, dict) else None
    if isinstance(urls, list):
        return [u for u in urls if isinstance(u, str) and u]
    return []


async def wait_for_kie_job(
    task_id: str, *, total_timeout: int = 600,
    poll_interval: float = POLL_INTERVAL,
) -> list[str]:
    """Poll /jobs/recordInfo until success/fail. Returns list of result
    URLs (empty on failure / timeout). Pure-state-machine logic — no
    backoff curve, kie.ai's poll quota is generous and a constant
    4s tick is plenty."""
    if not task_id:
        return []
    elapsed = 0.0
    data: dict = {}
    while elapsed < total_timeout:
        data = await get_kie_job_state(task_id)
        raw_state = data.get("state")
        state = raw_state.lower() if isinstance(raw_state, str) else ""
        if state in KIE_JOB_OK:
            return _extract_jobs_urls(data)
        if state and state not in KIE_JOB_IN_PROGRESS:
            log.warning("Kie job failed (state=%s, failCode=%s, failMsg=%s)",
                        state, data.get("failCode"), data.get("failMsg"))
            return []
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    log.warning("Kie job timed out after %ss (task_id=%s, last_state=%s)",
                total_timeout, task_id, data.get("state"))
    return []


async def kling_v21_master_image_to_video(
    *, image_url: str, prompt: str, duration: str = "10",
    negative_prompt: Optional[str] = None,
    cfg_scale: Optional[float] = None,
    total_timeout: int = 600,
) -> Optional[str]:
    """End-to-end: Kling 2.1 Master Image-to-Video → final mp4 URL.

    `duration` must be one of "5" or "10" (kie.ai accepts only those
    two values for the Master tier). Returns the mp4 URL or None.
    """
    if duration not in ("5", "10"):
        duration = "10"
    input_params: dict = {
        "prompt": (prompt or "").strip()[:5000] or "smooth cinematic motion",
        "image_url": image_url,
        "duration": duration,
    }
    if negative_prompt:
        input_params["negative_prompt"] = negative_prompt[:500]
    if cfg_scale is not None:
        try:
            input_params["cfg_scale"] = max(0.0, min(1.0, float(cfg_scale)))
        except (TypeError, ValueError):
            pass
    task_id = await submit_kie_job(
        model="kling/v2-1-master-image-to-video",
        input_params=input_params,
    )
    if not task_id:
        return None
    urls = await wait_for_kie_job(task_id, total_timeout=total_timeout)
    # Per-call cost log (Kling 2.1 Master is the priciest tier)
    try:
        from external_api_costs import log_external_call_fire_and_forget
        log_external_call_fire_and_forget(
            provider="kie.kling-v21-master-i2v",
            kind_label="kie:kling-v21-master-i2v",
            calls=1, seconds=float(duration),
            success=bool(urls),
            extra={"task_id": task_id, "duration": duration},
        )
    except Exception:
        pass
    return urls[0] if urls else None


__all__ = [
    # Original Veo helpers (kept for backward compat)
    "submit_video", "wait_for_video", "generate_video", "get_video_status",
    "VIDEO_MODELS", "DEFAULT_VIDEO_MODEL",
    # Universal /<family>/ marketplace client
    "KIE_PROVIDERS", "KIE_TASK_TIMEOUTS", "kie_provider_info",
    "submit_kie_task", "get_kie_task_status", "wait_for_kie_task",
    "generate_via_kie",
    # Unified /jobs/ API (Market models: Kling 2.1 Master, Wan 2.7, …)
    "submit_kie_job", "get_kie_job_state", "wait_for_kie_job",
    "kling_v21_master_image_to_video",
]
