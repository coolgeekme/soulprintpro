"""Shared email delivery utilities (Resend).

Keeps the Resend HTTP client in one place so every feature (audits,
workspace invites, password resets, etc.) uses the same envelope + the
same env-var contract (``RESEND_API_KEY`` + ``RESEND_FROM``).

Fails SILENTLY when Resend is unconfigured — callers get back False so
they can still persist the invite/audit/lead and show a "copy link"
fallback in the UI without blocking the user flow.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


async def send_resend_email(
    *, to: str, subject: str, html: str, text: str,
    reply_to: Optional[str] = None,
) -> bool:
    """POST to Resend's /emails endpoint. Returns True on success."""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    sender = os.environ.get("RESEND_FROM", "").strip()
    if not api_key or not sender:
        logger.info("Resend not configured — skipping email to %s", to)
        return False
    payload: dict = {
        "from": sender,
        "to": [to],
        "subject": subject,
        "html": html,
        "text": text,
    }
    if reply_to:
        payload["reply_to"] = reply_to
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        ok = resp.status_code < 300
        try:
            from external_api_costs import log_external_call_fire_and_forget
            log_external_call_fire_and_forget(
                provider="resend.email", kind_label="transactional",
                calls=1, success=ok,
                error=None if ok else f"HTTP {resp.status_code}",
            )
        except Exception:
            pass
        if not ok:
            logger.warning(
                "Resend send failed %s: %s", resp.status_code, resp.text[:300],
            )
            return False
        return True
    except Exception as exc:
        try:
            from external_api_costs import log_external_call_fire_and_forget
            log_external_call_fire_and_forget(
                provider="resend.email", kind_label="transactional",
                calls=1, success=False, error=str(exc)[:200],
            )
        except Exception:
            pass
        logger.warning("Resend send exception: %s", exc)
        return False


__all__ = ["send_resend_email"]
