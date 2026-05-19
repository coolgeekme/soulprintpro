"""Tests for the Composio resilience fixes shipped Feb 19, 2026.

Production bug: when `list_connections` raised (transient Composio
5xx, a malformed row in Mongo, etc.) the ENTIRE Connections panel
blanked out — including the "+ Add" toolkit catalog. Users saw the
panel header + intro copy with no toolkit cards, with no actionable
recovery path.

Fix: `composio_list_toolkits` now wraps `list_connections` in
try/except, returns featured toolkits regardless, and surfaces a
non-fatal `connections_error` flag the FE renders as an inline
"Retry now" banner.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.mark.asyncio
async def test_toolkits_endpoint_returns_catalog_when_connections_blows_up():
    """End-to-end: even if list_connections raises, the endpoint still
    returns the featured toolkit catalog so the user can click + Add."""
    # We test the route handler directly to avoid spinning the full app.
    from server import composio_list_toolkits  # noqa: E402

    fake_user = {"id": "u-1"}
    featured_sample = [
        {"slug": "facebook", "name": "Facebook"},
        {"slug": "instagram", "name": "Instagram"},
        {"slug": "gmail", "name": "Gmail"},
    ]

    with patch("server.require_org_member", AsyncMock(return_value=None)):
        with patch("composio_integration.is_available", lambda: True):
            with patch("composio_integration.list_featured_toolkits",
                       lambda: featured_sample):
                with patch(
                    "composio_integration.list_connections",
                    AsyncMock(side_effect=RuntimeError("Composio 502")),
                ):
                    result = await composio_list_toolkits("org-1", fake_user)

    # ALL three featured toolkits returned, even with connections crashing.
    assert result["available"] is True
    assert len(result["toolkits"]) == 3
    assert {t["slug"] for t in result["toolkits"]} == {"facebook", "instagram", "gmail"}
    # connections is an empty list, NOT None — keeps FE .map() safe.
    assert result["connections"] == []
    # The non-fatal error flag is set so the FE can show the inline banner.
    assert result["connections_error"]
    assert "Refresh" in result["connections_error"] or "moment" in result["connections_error"]


@pytest.mark.asyncio
async def test_toolkits_endpoint_clean_path_omits_error_flag():
    """Happy path: list_connections succeeds → no `connections_error`."""
    from server import composio_list_toolkits  # noqa: E402

    fake_user = {"id": "u-1"}
    fake_connections = [
        {"connection_id": "ig-1", "toolkit": "instagram", "status": "ACTIVE"},
    ]

    with patch("server.require_org_member", AsyncMock(return_value=None)):
        with patch("composio_integration.is_available", lambda: True):
            with patch("composio_integration.list_featured_toolkits",
                       lambda: [{"slug": "instagram", "name": "Instagram"}]):
                with patch(
                    "composio_integration.list_connections",
                    AsyncMock(return_value=fake_connections),
                ):
                    result = await composio_list_toolkits("org-1", fake_user)

    assert result["available"] is True
    assert len(result["connections"]) == 1
    assert result["connections_error"] is None


@pytest.mark.asyncio
async def test_toolkits_endpoint_composio_unavailable_returns_empty_safely():
    """When `is_available` is False (no COMPOSIO_API_KEY set), we still
    return a valid shape with empty arrays so the FE doesn't crash."""
    from server import composio_list_toolkits  # noqa: E402

    fake_user = {"id": "u-1"}
    with patch("server.require_org_member", AsyncMock(return_value=None)):
        with patch("composio_integration.is_available", lambda: False):
            result = await composio_list_toolkits("org-1", fake_user)
    assert result["available"] is False
    assert result["toolkits"] == []
    assert result["connections"] == []
