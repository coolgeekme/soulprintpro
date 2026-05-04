"""Regression tests for the Mind Map access control.

Owner-only by default. Owner can grant non-owner members. Anyone else
hits a 403 friendly explanation.
"""
import os
import pytest
import pytest_asyncio
from datetime import datetime, timezone
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

from server import _can_view_mind_map


@pytest_asyncio.fixture
async def db():
    url = os.environ.get("MONGO_URL")
    name = os.environ.get("DB_NAME") or "test_db"
    if not url:
        pytest.skip("MONGO_URL not configured")
    client = AsyncIOMotorClient(url)
    yield client[name]
    client.close()


@pytest.mark.asyncio
async def test_owner_always_has_mind_map_access():
    org = {"owner_id": "u1"}
    assert _can_view_mind_map(org, "u1", role="owner") is True
    # Even without a role hint, owner_id match wins
    assert _can_view_mind_map(org, "u1") is True


@pytest.mark.asyncio
async def test_non_owner_member_denied_by_default():
    org = {"owner_id": "u1"}
    assert _can_view_mind_map(org, "u2", role="member") is False
    assert _can_view_mind_map(org, "u3", role="admin") is False


@pytest.mark.asyncio
async def test_explicitly_granted_member_allowed():
    org = {"owner_id": "u1", "mind_map_access": ["u2", "u3"]}
    assert _can_view_mind_map(org, "u2", role="member") is True
    assert _can_view_mind_map(org, "u3", role="admin") is True
    assert _can_view_mind_map(org, "u4", role="member") is False


@pytest.mark.asyncio
async def test_anonymous_or_missing_org_denied():
    assert _can_view_mind_map({}, "u1") is False
    assert _can_view_mind_map(None, "u1") is False
    assert _can_view_mind_map({"owner_id": "u1"}, "") is False


@pytest.mark.asyncio
async def test_mind_map_access_list_handles_string_ids_consistently():
    # ObjectIds may be stored as strings or as bson ObjectId; helper must
    # handle both consistently.
    org = {"owner_id": "abc", "mind_map_access": [ObjectId() for _ in range(2)]}
    org["mind_map_access"].append("explicit-string-id")
    assert _can_view_mind_map(org, "explicit-string-id") is True
