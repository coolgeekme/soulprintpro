"""Tests for the org-agnostic Telegram routing redesign + Personal
Concierge tool expansion.

Network / Composio tools that require live OAuth (Gmail / Calendar)
are mocked so the tests are deterministic.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import personal_concierge as pc
import telegram_inbound as ti


# ── Fake Mongo replacement ─────────────────────────────────────────────────
class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)
        self._limit = None

    def sort(self, *args, **kwargs):
        return self

    def limit(self, n):
        self._limit = n
        return self

    def __aiter__(self):
        async def _gen():
            for d in self._docs[: (self._limit or len(self._docs))]:
                yield d
        return _gen()


class _FakeColl:
    def __init__(self, docs):
        self.docs = list(docs)

    def find(self, query, projection=None):
        return _FakeCursor(self.docs)

    async def find_one(self, query, projection=None):
        for d in self.docs:
            for k, v in query.items():
                if k == "_id":
                    continue
                if d.get(k) != v:
                    break
            else:
                return d
        return None

    async def count_documents(self, query):
        return 0

    async def insert_one(self, doc):
        self.docs.append(doc)
        return None

    async def update_one(self, *a, **k):
        return None

    async def delete_one(self, *a, **k):
        return None


class _FakeDB:
    def __init__(self):
        self.org_members = _FakeColl([])
        self.organizations = _FakeColl([])
        self.missions = _FakeColl([])
        self.social_posts = _FakeColl([])
        self.composio_connections = _FakeColl([])
        self.documents = _FakeColl([])
        self.telegram_sticky_org = _FakeColl([])
        self.telegram_disambig_state = _FakeColl([])
        self.telegram_inbound_log = _FakeColl([])
        self.users = _FakeColl([])


# ── Routing tests ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_default_routes_to_personal_concierge():
    """A plain message with no /org and no inline org name should NOT
    bind to an org. Even if the user has only one org."""
    db = _FakeDB()
    # Single org — old routing would've auto-bound; new routing must not.
    with patch.object(ti, "_user_orgs", AsyncMock(return_value=[
        {"id": "abc123", "name": "Reggie Lab"},
    ])):
        org, body, reason = await ti.detect_org(
            db, user_id="u1", chat_id=42, text="what's on my agenda today",
        )
    assert org is None
    assert reason == "personal_default"


@pytest.mark.asyncio
async def test_slash_org_routes_explicitly():
    """`/org <name> body` should route to that org."""
    db = _FakeDB()
    with patch.object(ti, "_user_orgs", AsyncMock(return_value=[
        {"id": "abc123", "name": "Reggie Lab"},
        {"id": "def456", "name": "Acme Corp"},
    ])):
        org, body, reason = await ti.detect_org(
            db, user_id="u1", chat_id=42,
            text="/org Acme Corp send the agenda to John",
        )
    assert org is not None
    assert org["name"] == "Acme Corp"
    assert reason == "slash"
    assert "send the agenda" in body


@pytest.mark.asyncio
async def test_inline_mention_routes_explicitly():
    """Inline mention of an exact org name routes to it."""
    db = _FakeDB()
    with patch.object(ti, "_user_orgs", AsyncMock(return_value=[
        {"id": "abc123", "name": "Reggie Lab"},
        {"id": "def456", "name": "Acme Corp"},
    ])):
        org, body, reason = await ti.detect_org(
            db, user_id="u1", chat_id=42,
            text="In Acme Corp, draft a reply to that contract email",
        )
    assert org is not None
    assert org["name"] == "Acme Corp"
    assert reason == "inline_match"


@pytest.mark.asyncio
async def test_no_sticky_carry_over():
    """Sticky context is NO LONGER consulted — even if a sticky row
    exists, a plain message routes to Personal Concierge."""
    db = _FakeDB()
    # Simulate a stale sticky in Mongo (shouldn't matter).
    db.telegram_sticky_org.docs.append({
        "chat_id": 42, "org_id": "abc123", "org_name": "Reggie Lab",
        "at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    })
    with patch.object(ti, "_user_orgs", AsyncMock(return_value=[
        {"id": "abc123", "name": "Reggie Lab"},
        {"id": "def456", "name": "Acme Corp"},
    ])):
        org, body, reason = await ti.detect_org(
            db, user_id="u1", chat_id=42, text="quick question — what time is it",
        )
    assert org is None
    assert reason == "personal_default"


# ── Personal Concierge tool schema sanity ─────────────────────────────────
def test_personal_concierge_exposes_new_tools():
    names = {t["function"]["name"] for t in pc.TOOL_SCHEMAS}
    assert "get_my_calendar" in names
    assert "get_my_inbox" in names
    assert "read_email" in names
    assert "search_my_docs" in names
    assert "list_orgs" in names
    assert "get_orgs_overview" in names
    assert "punt_to_org" in names


@pytest.mark.asyncio
async def test_search_my_docs_returns_results_across_orgs():
    db = _FakeDB()
    db.documents.docs.extend([
        {"id": "d1", "org_id": "abc", "original_name": "Acme Brief.pdf",
         "title": "Acme Q1 Brief", "preview": "Strategy doc for Acme",
         "kind": "doc", "mime": "application/pdf"},
        {"id": "d2", "org_id": "def", "original_name": "Pricing.md",
         "title": "Pricing", "preview": "Acme pricing tiers", "kind": "doc"},
    ])
    with patch.object(pc, "_user_orgs", AsyncMock(return_value=[
        {"id": "abc", "name": "Reggie Lab"},
        {"id": "def", "name": "Acme Corp"},
    ])):
        out = await pc._search_docs(db, "u1", query="Acme")
    assert len(out["results"]) == 2
    names = sorted(r["org_name"] for r in out["results"])
    assert names == ["Acme Corp", "Reggie Lab"]


@pytest.mark.asyncio
async def test_search_my_docs_requires_query():
    db = _FakeDB()
    out = await pc._search_docs(db, "u1", query="")
    assert "error" in out
