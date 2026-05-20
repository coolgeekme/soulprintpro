"""Error reporting → team@archeforge.com via Resend.

Fingerprints each failure, rate-limits identical fingerprints to once
per 30 minutes, and best-effort delivers via the shared Resend client.
Never raises — every caller is in a hot path (LLM failure, social
draft, image gen) and we MUST NOT double-fail by killing the response
handler with an email-send exception.

Usage:
    from error_reporter import report_error_async
    asyncio.create_task(report_error_async(
        kind="social_draft",
        org_id=org_id,
        user_email=u.get("email"),
        message=str(exc),
        exc=exc,
        extra={"prompt": body.prompt[:200], "platforms": platforms},
    ))
"""
from __future__ import annotations

import asyncio
import hashlib
import html as _html
import logging
import os
import socket
import traceback
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Per-process in-memory fingerprint cache. Keyed by sha256(kind+message).
# Values are the unix-second timestamp of the last send. The cache is
# bounded to FINGERPRINT_CACHE_MAX entries — when it overflows, the
# oldest half gets dropped (LRU-ish). 1024 entries × 30-min TTL is
# plenty for a busy production deploy and uses <1MB.
_RATE_LIMIT_SECONDS = 30 * 60
_FINGERPRINT_CACHE_MAX = 1024
_fingerprint_cache: dict[str, float] = {}


def _fingerprint(kind: str, message: str) -> str:
    """Stable hash of (kind, normalised message). We strip request IDs,
    UUIDs, and timestamps from the message before hashing so the same
    bug across multiple requests collapses to one fingerprint."""
    import re
    norm = re.sub(r"[0-9a-fA-F-]{12,}", "<id>", message or "")
    norm = re.sub(r"\d{4}-\d{2}-\d{2}T[\d:.+-]+", "<ts>", norm)
    norm = re.sub(r"\b\d{3,}\b", "<n>", norm)
    return hashlib.sha256(f"{kind}::{norm}".encode("utf-8", errors="ignore")).hexdigest()


def _should_send(fp: str) -> bool:
    """30-min dedupe gate. Also evicts old entries when cache is full."""
    now = datetime.now(timezone.utc).timestamp()
    last = _fingerprint_cache.get(fp, 0.0)
    if now - last < _RATE_LIMIT_SECONDS:
        return False
    _fingerprint_cache[fp] = now
    if len(_fingerprint_cache) > _FINGERPRINT_CACHE_MAX:
        # Drop oldest half. Cheap with dict items + sort.
        for stale_fp, _ in sorted(_fingerprint_cache.items(), key=lambda kv: kv[1])[: _FINGERPRINT_CACHE_MAX // 2]:
            _fingerprint_cache.pop(stale_fp, None)
    return True


def _tail_backend_log(lines: int = 5) -> str:
    """Best-effort: return the last N lines of the backend log so the
    email gives the team enough context to triage without SSHing into
    the pod. Reads from the same supervisor file the agent uses for
    debugging. Returns '' if the file isn't accessible."""
    candidates = [
        "/var/log/supervisor/backend.err.log",
        "/var/log/supervisor/backend.out.log",
    ]
    for path in candidates:
        try:
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as f:
                # Read last ~16KB to comfortably cover N lines of stack traces.
                try:
                    f.seek(-16 * 1024, 2)
                except OSError:
                    f.seek(0)
                data = f.read().decode("utf-8", errors="replace")
            tail = data.splitlines()[-lines:]
            return "\n".join(tail)
        except Exception:
            continue
    return ""


async def report_error_async(
    *,
    kind: str,
    message: str,
    org_id: Optional[str] = None,
    org_name: Optional[str] = None,
    user_id: Optional[str] = None,
    user_email: Optional[str] = None,
    exc: Optional[BaseException] = None,
    extra: Optional[dict[str, Any]] = None,
    link: Optional[str] = None,
) -> bool:
    """Fire-and-forget error email to team@archeforge.com (configurable).

    Returns True if the email was actually sent, False if rate-limited
    or Resend is unconfigured. NEVER raises — every failure is
    swallowed + logged so this can be slot into a hot path without
    fear of cascading the original problem.
    """
    try:
        # The destination is env-driven so we can route to a different
        # mailbox per deploy without a code change. Defaults to the
        # canonical owner inbox the user specified.
        to_addr = (os.environ.get("ERROR_NOTIFY_EMAIL") or "team@archeforge.com").strip()
        if not to_addr:
            return False
        fp = _fingerprint(kind, message)
        if not _should_send(fp):
            logger.info("Error report suppressed (rate-limit) for fp=%s kind=%s", fp[:8], kind)
            return False

        # Compose the email body. Plain text is included as a
        # fallback for clients that strip HTML.
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        env_tag = (
            os.environ.get("DEPLOY_ENV")
            or ("production" if "foundryagents" in (os.environ.get("PUBLIC_ORIGIN_OVERRIDE") or "") else "preview")
        )
        host = socket.gethostname()
        tb = (
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            if exc is not None else ""
        )
        tail = _tail_backend_log(lines=5)
        # Compact key/value table for the human reader.
        rows = [
            ("Kind", kind),
            ("Time (UTC)", ts),
            ("Environment", env_tag),
            ("Host", host),
            ("Org", f"{org_name or '—'} ({org_id or '—'})"),
            ("User", f"{user_email or '—'} ({user_id or '—'})"),
            ("Message", message or "—"),
        ]
        if link:
            rows.append(("Link", link))
        if extra:
            for k, v in (extra or {}).items():
                rows.append((str(k), str(v)[:500]))

        rows_html = "".join(
            f'<tr><td style="padding:4px 12px 4px 0;color:#888;vertical-align:top;">{_html.escape(k)}</td>'
            f'<td style="vertical-align:top;">{_html.escape(v)}</td></tr>'
            for k, v in rows
        )
        tb_html = (
            f'<details style="margin-top:18px;"><summary style="cursor:pointer;color:#888;">Stack trace</summary>'
            f'<pre style="background:#0e0e10;color:#f0f0f0;padding:12px;'
            f'border-radius:6px;font-size:12px;overflow:auto;">{_html.escape(tb)}</pre></details>'
            if tb else ""
        )
        log_html = (
            f'<details style="margin-top:12px;" open><summary style="cursor:pointer;color:#888;">Last 5 backend log lines</summary>'
            f'<pre style="background:#0e0e10;color:#f0f0f0;padding:12px;'
            f'border-radius:6px;font-size:12px;overflow:auto;">{_html.escape(tail)}</pre></details>'
            if tail else ""
        )
        body_html = (
            '<div style="font-family:Inter,Arial,sans-serif;font-size:14px;color:#1a1a22;max-width:680px;">'
            f'<p style="margin:0 0 12px;"><strong>{_html.escape(kind)}</strong> failure '
            f'<span style="color:#888;">({_html.escape(env_tag)})</span></p>'
            f'<table style="border-collapse:collapse;font-size:14px;">{rows_html}</table>'
            f'{log_html}{tb_html}</div>'
        )
        body_text = (
            f"[{env_tag}] {kind} failure\n\n"
            + "\n".join(f"{k}: {v}" for k, v in rows)
            + (f"\n\nLast 5 backend log lines:\n{tail}" if tail else "")
            + (f"\n\nStack trace:\n{tb}" if tb else "")
        )
        try:
            from email_service import send_resend_email
            ok = await send_resend_email(
                to=to_addr,
                subject=f"[Foundry · {env_tag}] {kind} failure — {(message or '')[:60]}",
                html=body_html, text=body_text,
            )
            if ok:
                logger.info("Error report dispatched to %s fp=%s kind=%s", to_addr, fp[:8], kind)
            return bool(ok)
        except Exception as exc_send:
            logger.warning("Error report send failed: %s", exc_send)
            return False
    except Exception as exc_outer:
        # Bulletproof: the reporter itself is never allowed to crash.
        logger.warning("Error reporter blew up: %s", exc_outer)
        return False


def fire_and_forget(**kwargs) -> None:
    """Synchronous-friendly wrapper. Schedules ``report_error_async`` as
    a background task without awaiting it. Use from sync code paths or
    when the caller doesn't care about the email outcome."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(report_error_async(**kwargs))
    except RuntimeError:
        # No running loop — silently drop. We won't spin up a fresh
        # loop for an email; the caller's context is too unusual.
        pass


__all__ = ["report_error_async", "fire_and_forget"]
