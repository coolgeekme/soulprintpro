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
from datetime import datetime, timezone
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


__all__ = ["send_resend_email", "send_welcome_email"]


async def send_welcome_email(*, name: str, email: str, notify_team: bool = True) -> bool:
    """Send a friendly welcome email to a newly registered user AND
    (when `notify_team=True`) a notification copy to the Foundry team.
    Best-effort + never raises — the caller's registration flow MUST
    NOT break if the email provider is having a bad day.

    The "team" address is configurable via `WELCOME_NOTIFY_EMAIL` and
    defaults to `team@archeforge.com` (the canonical owner inbox).

    Pass `notify_team=False` when the new user is staff — there's no
    point pinging the founders about their own accounts.

    Idempotency:
      - Sender enforces nothing — duplicates are caller's responsibility.
        The register path only fires this once per inserted user_id, so
        in practice we won't double-send.
    """
    if not email:
        return False
    safe_name = (name or email.split("@")[0] or "there").strip()
    team_addr = (os.environ.get("WELCOME_NOTIFY_EMAIL") or "team@archeforge.com").strip()
    # Public origin for the in-email "Open dashboard" CTA. Env-driven so
    # the same code ships across preview / production / future domains
    # (foundryagents.ai, etc.) without a redeploy.
    public_origin = (
        os.environ.get("PUBLIC_ORIGIN_OVERRIDE")
        or "https://foundryagents.ai"
    ).rstrip("/")

    user_html = f"""
      <div style="font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                  color:#1a1a22;max-width:560px;margin:0 auto;line-height:1.55;padding:32px 24px;">
        <h1 style="font-family:'IBM Plex Serif',Georgia,serif;font-size:28px;
                   color:#F64000;margin:0 0 16px;line-height:1.15;">
          Welcome to Foundry, {safe_name}.
        </h1>
        <p style="font-size:15px;color:#2a2a35;">
          You now have access to a 169-agent team that already knows how
          to build inside your brand voice — strategists, copywriters,
          researchers, designers, engineers, the works.
        </p>
        <p style="font-size:15px;color:#2a2a35;">
          A great first step: paste your website (or just describe your
          business) into a fresh organization. We'll synthesize your
          Soul — mission, voice, audience, visual style — and your
          Chief of Staff agent will spin up a plan for what to ship next.
        </p>
        <p style="margin-top:28px;">
          <a href="{public_origin}/app"
             style="display:inline-block;background:linear-gradient(135deg,#F64000,#FF8E3A);
                    color:white;padding:12px 24px;border-radius:999px;
                    text-decoration:none;font-weight:600;font-size:14px;">
            Open your dashboard →
          </a>
        </p>
        <p style="font-size:13px;color:#888;margin-top:32px;">
          Questions? Reply to this email — a real human reads it.<br/>
          — The Foundry team
        </p>
      </div>
    """
    user_text = (
        f"Welcome to Foundry, {safe_name}.\n\n"
        f"You now have access to a 169-agent team that already knows how to build inside your brand voice.\n\n"
        f"Open your dashboard: {public_origin}/app\n\n"
        f"Questions? Reply to this email — a real human reads it.\n— The Foundry team"
    )
    user_ok = await send_resend_email(
        to=email,
        subject="Welcome to Foundry",
        html=user_html, text=user_text,
    )

    # Internal team notification — short, scannable, no marketing fluff.
    # Skipped when the new user is staff (`notify_team=False`).
    if notify_team:
        team_html = (
            f'<div style="font-family:Inter,Arial,sans-serif;font-size:14px;color:#1a1a22;">'
            f'<p><strong>New user signed up</strong></p>'
            f'<table style="border-collapse:collapse;font-size:14px;">'
            f'<tr><td style="padding:4px 12px 4px 0;color:#888;">Name</td><td>{safe_name}</td></tr>'
            f'<tr><td style="padding:4px 12px 4px 0;color:#888;">Email</td><td>{email}</td></tr>'
            f'<tr><td style="padding:4px 12px 4px 0;color:#888;">Time (UTC)</td>'
            f'<td>{datetime.now(timezone.utc).isoformat(timespec="seconds")}</td></tr>'
            f'</table></div>'
        )
        team_text = (
            f"New user signed up.\n"
            f"Name: {safe_name}\nEmail: {email}\n"
            f"Time (UTC): {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        )
        await send_resend_email(
            to=team_addr,
            subject=f"[Foundry] New signup — {safe_name} <{email}>",
            html=team_html, text=team_text,
        )

    return bool(user_ok)
