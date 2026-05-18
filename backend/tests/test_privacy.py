"""Tests for the GDPR privacy module — data export + right-to-erasure."""
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Fake Mongo replacement ─────────────────────────────────────────────────
class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def __aiter__(self):
        async def _gen():
            for d in self._docs:
                yield d
        return _gen()

    def sort(self, *a, **k):
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self


class _FakeColl:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find(self, query=None, projection=None):
        out = []
        for d in self.docs:
            if self._match(d, query or {}):
                out.append(d)
        return _FakeCursor(out)

    async def find_one(self, query=None, projection=None, **kwargs):
        for d in self.docs:
            if self._match(d, query or {}):
                return d
        return None

    async def count_documents(self, query=None):
        return sum(1 for d in self.docs if self._match(d, query or {}))

    async def insert_one(self, doc):
        self.docs.append(doc)
        return type("R", (), {"inserted_id": doc.get("_id") or "fake"})()

    async def update_one(self, *a, **k):
        return None

    async def update_many(self, *a, **k):
        return None

    async def delete_many(self, query):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not self._match(d, query)]
        return type("R", (), {"deleted_count": before - len(self.docs)})()

    async def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if self._match(d, query):
                self.docs.pop(i)
                return type("R", (), {"deleted_count": 1})()
        return type("R", (), {"deleted_count": 0})()

    @staticmethod
    def _match(doc, query):
        for k, v in (query or {}).items():
            if k.startswith("$"):
                continue
            if k == "_id":
                if str(doc.get("_id")) != str(v):
                    return False
                continue
            if isinstance(v, dict) and "$in" in v:
                if doc.get(k) not in v["$in"]:
                    return False
                continue
            if isinstance(v, dict) and "$lt" in v:
                if not (doc.get(k) is not None and doc.get(k) < v["$lt"]):
                    return False
                continue
            if isinstance(v, dict) and "$ne" in v:
                if doc.get(k) == v["$ne"]:
                    return False
                continue
            if isinstance(v, dict) and "$regex" in v:
                import re as _re
                if not _re.search(v["$regex"], str(doc.get(k) or "")):
                    return False
                continue
            if doc.get(k) != v:
                return False
        return True


class _FakeDB:
    def __init__(self):
        for name in (
            "users", "organizations", "org_members", "teams", "conversations",
            "messages", "documents", "missions", "audits", "social_posts",
            "user_memory", "composio_connections", "privacy_export_jobs",
            "invites", "auth_audit_log",
        ):
            setattr(self, name, _FakeColl())


# ── _find_blocking_owned_orgs ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_blocking_orgs_when_sole_owner_with_members():
    from privacy import _find_blocking_owned_orgs
    db = _FakeDB()
    # Setup: user is the sole owner of "Acme" which has 3 members; not blocking for "Solo" (1 member).
    db.org_members.docs = [
        {"user_id": "U1", "org_id": "A1", "role": "owner"},
        {"user_id": "U2", "org_id": "A1", "role": "admin"},
        {"user_id": "U3", "org_id": "A1", "role": "member"},
        {"user_id": "U1", "org_id": "S1", "role": "owner"},
    ]
    db.organizations.docs = [
        {"_id": "A1", "name": "Acme"},
        {"_id": "S1", "name": "Solo"},
    ]
    db.users.docs = [
        {"_id": "U2", "name": "Alice", "email": "alice@x.com"},
        {"_id": "U3", "name": "Bob", "email": "bob@x.com"},
    ]
    # Patch the ObjectId path the production code uses to look up users / orgs.
    with patch("privacy.ObjectId", side_effect=lambda s: s):
        blockers = await _find_blocking_owned_orgs(db, "U1")
    assert len(blockers) == 1
    b = blockers[0]
    assert b["org_id"] == "A1"
    assert b["member_count"] == 3
    # Admins should be ordered first.
    assert b["transfer_targets"][0]["role"] == "admin"
    assert b["transfer_targets"][0]["email"] == "alice@x.com"


@pytest.mark.asyncio
async def test_no_blockers_when_user_owns_only_solo_orgs():
    from privacy import _find_blocking_owned_orgs
    db = _FakeDB()
    db.org_members.docs = [
        {"user_id": "U1", "org_id": "S1", "role": "owner"},
    ]
    db.organizations.docs = [{"_id": "S1", "name": "Solo"}]
    with patch("privacy.ObjectId", side_effect=lambda s: s):
        blockers = await _find_blocking_owned_orgs(db, "U1")
    assert blockers == []


@pytest.mark.asyncio
async def test_no_blockers_when_other_owner_exists():
    from privacy import _find_blocking_owned_orgs
    db = _FakeDB()
    db.org_members.docs = [
        {"user_id": "U1", "org_id": "A1", "role": "owner"},
        {"user_id": "U2", "org_id": "A1", "role": "owner"},
        {"user_id": "U3", "org_id": "A1", "role": "member"},
    ]
    db.organizations.docs = [{"_id": "A1", "name": "Acme"}]
    with patch("privacy.ObjectId", side_effect=lambda s: s):
        blockers = await _find_blocking_owned_orgs(db, "U1")
    assert blockers == []


# ── _safe_slug ──────────────────────────────────────────────────────────────


def test_safe_slug_handles_special_chars():
    from privacy import _safe_slug
    assert _safe_slug("Acme Corp / Health Co") == "acme-corp-health-co"
    assert _safe_slug("") == "untitled"
    assert _safe_slug("a" * 100, max_len=10) == "a" * 10


# ── _build_readme ──────────────────────────────────────────────────────────


def test_build_readme_includes_email_and_orgs():
    from privacy import _build_readme
    txt = _build_readme(
        {"name": "Reggie", "email": "r@x.com"},
        [{"org_id": "A1", "name": "Acme", "role": "owner"}],
    )
    assert "r@x.com" in txt
    assert "Reggie" in txt
    assert "GDPR Article 15" in txt
    assert "Article 17" in txt or "right-to-be-forgotten" in txt.lower() or "request deletion" in txt.lower()


# ── _build_export_zip end-to-end ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_export_zip_basic(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVACY_EXPORT_DIR", str(tmp_path))
    # Force reimport so the module picks up the new env-var.
    import importlib, privacy
    importlib.reload(privacy)
    from privacy import _build_export_zip

    db = _FakeDB()
    db.users.docs = [{
        "_id": "U1", "name": "Reggie", "email": "r@x.com",
        "phone": "", "timezone": "UTC", "telegram_chat_id": 0,
    }]
    db.org_members.docs = [{"user_id": "U1", "org_id": "A1", "role": "owner"}]
    db.organizations.docs = [{"_id": "A1", "name": "Acme", "general_team_id": "T1"}]
    db.teams.docs = [{"_id": "T1", "org_id": "A1", "name": "General",
                       "soulprint": {"tone": "warm"}}]
    db.conversations.docs = [{
        "_id": "C1", "id": "C1", "owner_user_id": "U1",
        "team_id": "T1", "title": "My CoS",
    }]
    db.messages.docs = [
        {"_id": "M1", "conversation_id": "C1", "sender_id": "U1",
         "sender_type": "user", "text": "Hello"},
    ]
    db.documents.docs = [{
        "_id": "D1", "id": "D1", "org_id": "A1", "created_by": "U1",
        "original_name": "brief.pdf", "title": "Brief", "mime": "application/pdf",
    }]
    db.user_memory.docs = [{"_id": "MEM1", "user_id": "U1", "text": "loves Slack"}]

    with patch("privacy.ObjectId", side_effect=lambda s: s):
        zip_path = await _build_export_zip(db, "U1", "job1")

    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert "README.md" in names
        assert "account.json" in names
        assert "connections.json" in names
        assert "memory/user_memory.json" in names
        # Org folder uses slug + 6-char prefix.
        assert any("orgs/acme-" in n and "role.json" in n for n in names)
        assert any("soulprints.json" in n for n in names), "owner should get soulprints"
        # The user's CoS DM should be exported in full.
        assert any("conversations/direct/" in n for n in names)


# ── Single-use token semantics ─────────────────────────────────────────────


def test_export_token_format():
    from privacy import _make_token
    t1 = _make_token()
    t2 = _make_token()
    assert t1 != t2
    assert len(t1) >= 40  # urlsafe(32) → ≥43 chars after base64
