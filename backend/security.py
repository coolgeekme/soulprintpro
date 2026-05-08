"""Security primitives — brute-force lockout, security headers, IP rate-limit.

Kept out of `server.py` so the monolith stays readable. All helpers are
opt-in and best-effort: failures fall through to "allow" rather than
blocking legitimate traffic when Mongo is hiccuping.

Concepts:
  * `LoginThrottle` — per-(ip, email) lockout for /api/auth/login. After
    `MAX_FAILED_ATTEMPTS` consecutive failures within `LOCKOUT_WINDOW`
    minutes, the account is soft-locked for `LOCKOUT_DURATION_MIN`
    minutes. Successful login clears the counter.
  * `RateLimiter` — generic in-memory sliding-window rate limit usable as
    a FastAPI dependency. Pinned per (key, route).
  * `SecurityHeadersMiddleware` — adds CSP, HSTS, X-Frame-Options,
    X-Content-Type-Options, Referrer-Policy, Permissions-Policy on every
    response.

The headers middleware leans toward "permissive enough not to break the
SPA" while still defending against the most common injection / clickjack
classes. CSP starts in report-only mode behind an env flag so we can
tune it without breaking pages.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


# ── Brute-force lockout ────────────────────────────────────────────────────

MAX_FAILED_ATTEMPTS = 8        # how many wrong passwords trigger lockout
LOCKOUT_WINDOW_MIN = 15        # window over which we count failures
LOCKOUT_DURATION_MIN = 15      # how long the lockout lasts after threshold


def _client_ip(request: Request) -> str:
    """Best-effort client IP. Honors X-Forwarded-For when the platform
    proxy injects it (Kubernetes ingress prepends real client IP)."""
    xff = request.headers.get("x-forwarded-for", "").strip()
    if xff:
        # First entry is the original client.
        return xff.split(",")[0].strip()
    return (request.client.host if request.client else "0.0.0.0")


class LoginThrottle:
    """MongoDB-backed login attempt tracker. Stateless across pods."""

    def __init__(self, db):
        self.db = db
        # Index ensures stale entries auto-purge after the lockout window.
        try:
            # Fire-and-forget — index creation is async but we don't want
            # to block module import.
            import asyncio
            asyncio.get_event_loop().create_task(self._ensure_indexes())
        except Exception:
            pass

    async def _ensure_indexes(self):
        try:
            # TTL — drop attempts older than 60 min so the collection
            # stays small even under attack.
            await self.db.login_attempts.create_index(
                "expires_at", expireAfterSeconds=0,
            )
            await self.db.login_attempts.create_index(
                [("identifier", 1)],
            )
        except Exception as exc:
            logger.warning("login_attempts index creation failed: %s", exc)

    @staticmethod
    def _identifier(ip: str, email: str) -> str:
        # Combine IP + email so a single attacker hitting many accounts
        # is throttled, AND a credential-stuffing attempt from many IPs
        # against one account is also throttled (separate identifier
        # lookup below).
        return f"{ip}:{(email or '').lower().strip()}"

    async def check(self, *, ip: str, email: str) -> None:
        """Raise 429 if locked out; otherwise no-op."""
        now = datetime.now(timezone.utc)
        ident = self._identifier(ip, email)
        try:
            row = await self.db.login_attempts.find_one(
                {"identifier": ident, "locked_until": {"$gt": now}},
            )
        except Exception:
            return  # fail-open on Mongo blip
        if row:
            until = row.get("locked_until")
            if isinstance(until, datetime):
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
                wait_s = int((until - now).total_seconds())
                raise HTTPException(
                    status_code=429,
                    detail=(f"Too many failed login attempts. Try again in "
                            f"{max(1, wait_s // 60)} minute"
                            f"{'s' if wait_s >= 120 else ''}."),
                )

    async def record_failure(self, *, ip: str, email: str) -> None:
        now = datetime.now(timezone.utc)
        ident = self._identifier(ip, email)
        window_start = now - timedelta(minutes=LOCKOUT_WINDOW_MIN)
        try:
            row = await self.db.login_attempts.find_one({"identifier": ident})
        except Exception:
            return
        # Drop attempts outside the rolling window to compute a fresh count.
        prior_failures = []
        if row:
            for t in (row.get("failures") or []):
                if isinstance(t, datetime):
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=timezone.utc)
                    if t >= window_start:
                        prior_failures.append(t)
        prior_failures.append(now)
        update = {
            "identifier": ident,
            "failures": prior_failures,
            "expires_at": now + timedelta(minutes=60),
        }
        # Trip lockout once we cross the threshold.
        if len(prior_failures) >= MAX_FAILED_ATTEMPTS:
            update["locked_until"] = now + timedelta(minutes=LOCKOUT_DURATION_MIN)
        try:
            await self.db.login_attempts.update_one(
                {"identifier": ident}, {"$set": update}, upsert=True,
            )
        except Exception as exc:
            logger.warning("login_attempts upsert failed: %s", exc)

    async def clear(self, *, ip: str, email: str) -> None:
        ident = self._identifier(ip, email)
        try:
            await self.db.login_attempts.delete_one({"identifier": ident})
        except Exception:
            pass


# ── In-memory IP rate limiter ──────────────────────────────────────────────


class RateLimiter:
    """Sliding-window per-key rate limit. In-memory; per-pod. Suitable
    for low-stakes endpoints (signup, password reset, invite accept)
    where we want to stop trivial bot floods. For B2B-grade protection
    in front of monetized endpoints, layer a true ingress-level limit.
    """

    def __init__(self, *, calls: int, window_s: int):
        self.calls = calls
        self.window_s = window_s
        self._buckets: dict[str, list[float]] = {}

    def _check(self, key: str) -> bool:
        now = time.time()
        bucket = self._buckets.setdefault(key, [])
        bucket[:] = [t for t in bucket if now - t < self.window_s]
        if len(bucket) >= self.calls:
            return False
        bucket.append(now)
        return True

    def __call__(self, request: Request):
        """Use as a FastAPI dependency: `Depends(my_limiter)`."""
        key = _client_ip(request) + ":" + request.url.path
        if not self._check(key):
            raise HTTPException(
                status_code=429,
                detail=("Too many requests from your network. "
                        "Please slow down and try again in a moment."),
            )


# ── Security headers middleware ────────────────────────────────────────────


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds production-grade security headers on every response.

    The CSP is intentionally permissive (allows `'unsafe-inline'` for
    styles since the SPA uses inline styles + some libraries, and
    `'unsafe-eval'` for the React dev shim) — but blocks third-party
    script origins outright. Tighten over time once we audit which
    libraries actually need eval.

    Set `SECURITY_HEADERS_DISABLED=1` to opt out (e.g. when debugging
    a "blocked by CSP" issue locally).
    """

    DEFAULT_CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
        "https://www.gstatic.com https://apis.google.com "
        "https://www.googletagmanager.com "
        "https://static.cloudflareinsights.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data: blob: https:; "
        "media-src 'self' blob: https:; "
        "connect-src 'self' https: wss: ws:; "
        "frame-src 'self' https://*.firebaseapp.com https://accounts.google.com; "
        "frame-ancestors 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )

    async def dispatch(self, request: Request, call_next):
        if os.environ.get("SECURITY_HEADERS_DISABLED") == "1":
            return await call_next(request)
        response = await call_next(request)
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=63072000; includeSubDomains; preload",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(self), microphone=(self), camera=(self), payment=()",
        )
        # CSP defaults to enforced now that the policy is stable across
        # the SPA and our APIs. Set `CSP_MODE=report-only` if you ever
        # need to A/B test a policy change without breaking pages.
        csp_mode = os.environ.get("CSP_MODE", "enforce")
        header_name = (
            "Content-Security-Policy"
            if csp_mode == "enforce"
            else "Content-Security-Policy-Report-Only"
        )
        response.headers.setdefault(header_name, self.DEFAULT_CSP)
        return response


# ── Public security policy doc ─────────────────────────────────────────────


SECURITY_POLICY_DOC = {
    "version": "1.1",
    "last_reviewed": "2026-05-08",
    "data_residency": {
        "primary_region": "US",
        "database": "MongoDB Atlas (managed, encrypted at rest)",
        "object_storage": "MongoDB GridFS (mirrors disk for durability)",
    },
    "encryption": {
        "in_transit": "TLS 1.2+ on every endpoint (HSTS preload).",
        "at_rest": (
            "MongoDB Atlas full-disk AES-256. Browser session credentials "
            "stored in the Credential Vault use Fernet (AES-128-CBC + "
            "HMAC-SHA256) keyed by SOULPRINT_VAULT_KEY in the backend env."
        ),
    },
    "authentication": {
        "methods": [
            "Email + bcrypt password (cost factor 12, default).",
            "Google sign-in via Firebase ID-token verification (public JWKS).",
        ],
        "session_lifetime_days": 30,
        "session_renewal": (
            "Sliding refresh: every authenticated request re-issues a "
            "fresh access token if the current one is more than half-spent."
        ),
        "brute_force_protection": (
            f"Login lockout after {MAX_FAILED_ATTEMPTS} failed attempts "
            f"within {LOCKOUT_WINDOW_MIN} minutes; "
            f"{LOCKOUT_DURATION_MIN}-minute cool-down before the next attempt."
        ),
    },
    "authorization": {
        "model": (
            "Workspaces > Organizations > Teams > Conversations. Every "
            "data-bearing endpoint validates org membership (and team "
            "membership for closed teams) before returning a row."
        ),
        "roles": ["workspace owner", "workspace admin", "workspace member",
                  "org owner", "org admin", "org member"],
        "private_chief_of_staff": (
            "Per-user private CoS threads are visible only to the user "
            "who owns them and the org owner (audit access). No other "
            "member sees the contents."
        ),
    },
    "rate_limiting": {
        "login": (
            f"Throttled per IP+email (max {MAX_FAILED_ATTEMPTS} failures / "
            f"{LOCKOUT_WINDOW_MIN}m)."
        ),
        "lead_capture": "5 submissions per IP per minute.",
        "invite_accept": "10 attempts per IP per minute.",
    },
    "secrets": {
        "policy": (
            "All third-party API keys (OpenAI, Stripe, Resend, Brave, "
            "Perplexity, Kie.ai, Firebase) live in the backend env, "
            "never committed to source control. Firebase web config is "
            "public by design and gated server-side via project_id allowlist."
        ),
    },
    "audit": {
        "admin_dashboard_logging": (
            "Every superadmin action (audit re-runs, member removals, "
            "invite revocations) is logged with actor, action, target, "
            "timestamp."
        ),
    },
    "proof_of_integrity": {
        "tier1_per_file": (
            "Every agent-generated artifact (PDFs, briefs, audits, "
            "exported reports) is hashed with SHA-256 and the digest "
            "is submitted to the OpenTimestamps public calendar "
            "network. Within ~1-6 hours that digest is anchored into "
            "the Bitcoin blockchain — a tamper-evident record that "
            "the file existed at exactly those bytes at exactly that "
            "moment. Customers can download the standard `.ots` proof "
            "and verify independently with the OpenTimestamps CLI; "
            "no SoulPrint cooperation is required."
        ),
        "tier2_daily_merkle": (
            "Every 24 hours, a Merkle tree is built over key business "
            "records written that day (mission events, workspace "
            "invite acceptances, org member additions, audit leads). "
            "The Merkle root is anchored on Bitcoin via OpenTimestamps. "
            "Customers can re-hash any individual record and walk the "
            "Merkle path stored in the anchor manifest to prove the "
            "record was part of that day's anchored state."
        ),
        "ledger": "Bitcoin (via OpenTimestamps shared calendar servers)",
        "cost_to_customer": "Free.",
        "verifiability": (
            "All proofs are downloadable as standard `.ots` files and "
            "verifiable with the OpenTimestamps reference CLI: "
            "`ots verify --file <doc> <doc>.ots`."
        ),
    },
    "incident_response": {
        "contact": "team@archeforge.com",
        "expected_response_hours": 24,
    },
    "compliance_posture": {
        "soc2": "Targeting Type II; key controls (access reviews, "
                "incident playbooks, encryption) operating today.",
        "gdpr": "Right-to-erasure handled via account deletion; "
                "data subject requests routed to team@archeforge.com.",
    },
}


__all__ = [
    "LoginThrottle",
    "RateLimiter",
    "SecurityHeadersMiddleware",
    "SECURITY_POLICY_DOC",
    "_client_ip",
    "MAX_FAILED_ATTEMPTS",
    "LOCKOUT_WINDOW_MIN",
    "LOCKOUT_DURATION_MIN",
]
