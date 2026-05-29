"""Regression: `_public_origin` must NOT leak internal-deploy hostnames
into OAuth redirect URIs / email links.

Bug history (Feb 2026): user clicked "Connect GitHub" on production
(foundryagents.ai). Our backend sent GitHub a `redirect_uri` of
`https://emergent-wing.emergent.host/auth/github/callback` — the inner
Kubernetes deploy URL — instead of the public domain. GitHub rejected
it because that URL wasn't registered on the OAuth app.

Fix: `_public_origin` now skips any X-Forwarded-Host / Host / Origin
value matching a known internal-deploy suffix and falls through to the
next signal. PUBLIC_ORIGIN_OVERRIDE remains the operator escape hatch.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import workspaces as ws  # noqa: E402


def _req(**headers):
    r = MagicMock()
    # MagicMock's `.headers.get(...)` returns another MagicMock by
    # default — replace with a real dict so missing keys return None.
    r.headers = {k.lower(): v for k, v in headers.items()}
    # MagicMock dict shim — .get(key, default=None)
    h = r.headers
    class HeadersShim:
        def get(self, key, default=None):
            return h.get(key.lower(), default)
    r.headers = HeadersShim()
    return r


# ── PUBLIC_ORIGIN_OVERRIDE wins ──────────────────────────────────────


def test_override_wins_over_forwarded(monkeypatch):
    monkeypatch.setenv("PUBLIC_ORIGIN_OVERRIDE", "https://foundryagents.ai")
    out = ws._public_origin(_req(**{
        "X-Forwarded-Host": "emergent-wing.emergent.host",
        "X-Forwarded-Proto": "https",
    }))
    assert out == "https://foundryagents.ai"


def test_override_must_be_http_or_https(monkeypatch):
    """Reject `javascript:` and other malformed overrides."""
    monkeypatch.setenv("PUBLIC_ORIGIN_OVERRIDE", "javascript:alert(1)")
    out = ws._public_origin(_req(**{
        "Host": "foundryagents.ai",
    }))
    # Override rejected → falls through to Host header
    assert out == "https://foundryagents.ai"


# ── Internal-host suffix detection ───────────────────────────────────


@pytest.mark.parametrize(
    "host",
    [
        "emergent-wing.emergent.host",
        "podname.cluster.local",
        "emergent-wing.preview.emergentagent.com",
        "service.namespace.svc",
    ],
)
def test_internal_host_detection_positive(host):
    assert ws._is_internal_host(host)


@pytest.mark.parametrize(
    "host",
    [
        "foundryagents.ai",
        "www.foundryagents.ai",
        "app.foundryagents.ai",
        "",  # empty
    ],
)
def test_internal_host_detection_negative(host):
    assert not ws._is_internal_host(host)


# ── X-Forwarded-Host chain walk ──────────────────────────────────────


def test_skips_internal_forwarded_host(monkeypatch):
    """Production case: X-Forwarded-Host is the inner emergent host,
    but the Origin header is set to the real public domain. We MUST
    fall through to Origin instead of leaking the inner host."""
    monkeypatch.delenv("PUBLIC_ORIGIN_OVERRIDE", raising=False)
    out = ws._public_origin(_req(**{
        "X-Forwarded-Host": "emergent-wing.emergent.host",
        "X-Forwarded-Proto": "https",
        "Origin": "https://foundryagents.ai",
        "Host": "emergent-wing.emergent.host",
    }))
    assert out == "https://foundryagents.ai"


def test_walks_forwarded_chain_for_first_public_host(monkeypatch):
    """Comma-separated chains: first non-internal entry wins. Real
    production header may look like:
       X-Forwarded-Host: emergent-wing.emergent.host, foundryagents.ai
    when an internal proxy sits in front. We must take the first
    non-internal one, not the first.
    """
    monkeypatch.delenv("PUBLIC_ORIGIN_OVERRIDE", raising=False)
    out = ws._public_origin(_req(**{
        "X-Forwarded-Host": "emergent-wing.emergent.host, foundryagents.ai",
        "X-Forwarded-Proto": "https",
    }))
    assert out == "https://foundryagents.ai"


def test_normal_forwarded_host_works_when_not_internal(monkeypatch):
    """Don't regress the happy path: a public X-Forwarded-Host is honoured."""
    monkeypatch.delenv("PUBLIC_ORIGIN_OVERRIDE", raising=False)
    out = ws._public_origin(_req(**{
        "X-Forwarded-Host": "foundryagents.ai",
        "X-Forwarded-Proto": "https",
    }))
    assert out == "https://foundryagents.ai"


def test_falls_to_origin_when_all_forwarded_hosts_internal(monkeypatch):
    monkeypatch.delenv("PUBLIC_ORIGIN_OVERRIDE", raising=False)
    out = ws._public_origin(_req(**{
        "X-Forwarded-Host": "a.emergent.host, b.cluster.local",
        "Origin": "https://foundryagents.ai/app/dashboard",
    }))
    assert out == "https://foundryagents.ai"


def test_internal_host_header_does_not_leak(monkeypatch):
    """Final-fallback path: Host header is the inner deploy host but
    nothing else is set. We must NOT return that — we'd rather fall
    through to FRONTEND_URL (or empty) than leak the internal URL
    into an OAuth redirect.
    """
    monkeypatch.delenv("PUBLIC_ORIGIN_OVERRIDE", raising=False)
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    out = ws._public_origin(_req(**{
        "Host": "emergent-wing.emergent.host",
    }))
    # No fallback configured → empty string, NOT the leaked internal host.
    assert "emergent.host" not in out
    assert out == ""


def test_origin_header_rejected_if_internal(monkeypatch):
    monkeypatch.delenv("PUBLIC_ORIGIN_OVERRIDE", raising=False)
    monkeypatch.setenv("FRONTEND_URL", "https://foundryagents.ai")
    out = ws._public_origin(_req(**{
        "Origin": "https://emergent-wing.emergent.host",
    }))
    # Falls through to FRONTEND_URL, never returns the internal Origin.
    assert "emergent.host" not in out
    assert out == "https://foundryagents.ai"


# ── Scheme handling ──────────────────────────────────────────────────


def test_honours_forwarded_proto_http(monkeypatch):
    monkeypatch.delenv("PUBLIC_ORIGIN_OVERRIDE", raising=False)
    out = ws._public_origin(_req(**{
        "X-Forwarded-Host": "localhost:3000",
        "X-Forwarded-Proto": "http",
    }))
    assert out == "http://localhost:3000"


def test_defaults_to_https_when_proto_missing(monkeypatch):
    monkeypatch.delenv("PUBLIC_ORIGIN_OVERRIDE", raising=False)
    out = ws._public_origin(_req(**{
        "X-Forwarded-Host": "foundryagents.ai",
    }))
    assert out == "https://foundryagents.ai"
