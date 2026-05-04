"""Regression tests for the Firebase ID-token verification module.

These cover the safe-by-default behaviours that protect the legacy auth
flow: a forged Firebase-shaped token must NEVER pass verification, an
unconfigured backend must reject Firebase tokens cleanly, and the
issuer-sniff helper must correctly distinguish Firebase tokens from
our legacy HS256 tokens.
"""
import importlib
import os
import sys

import jwt as pyjwt
import pytest


@pytest.fixture(scope="module")
def fb():
    """Import firebase_auth fresh each test module run so env-var
    overrides take effect."""
    sys.modules.pop("firebase_auth", None)
    return importlib.import_module("firebase_auth")


def _make_token(claims: dict, secret: str = "x", alg: str = "HS256") -> str:
    return pyjwt.encode(claims, secret, algorithm=alg)


# ── Issuer sniff ──────────────────────────────────────────────────────


def test_looks_like_firebase_true_for_firebase_iss(fb):
    tok = _make_token({"iss": "https://securetoken.google.com/some-project", "sub": "u"})
    assert fb._looks_like_firebase_token(tok) is True


def test_looks_like_firebase_false_for_legacy_token(fb):
    # Our legacy HS256 tokens have `sub` but no `iss` claim.
    tok = _make_token({"sub": "user_id", "type": "access"})
    assert fb._looks_like_firebase_token(tok) is False


def test_looks_like_firebase_false_for_other_iss(fb):
    tok = _make_token({"iss": "https://accounts.google.com", "sub": "u"})
    assert fb._looks_like_firebase_token(tok) is False


def test_looks_like_firebase_false_for_garbage(fb):
    assert fb._looks_like_firebase_token("not.a.jwt") is False
    assert fb._looks_like_firebase_token("") is False


# ── Verification rejects forged tokens ────────────────────────────────


def test_verify_rejects_forged_token_when_configured(fb, monkeypatch):
    """A Firebase-shaped token signed with a random key must NOT verify
    even though its `iss` and `aud` claims look correct."""
    monkeypatch.setattr(fb, "FIREBASE_PROJECT_ID", "soulprint-test")
    forged = _make_token({
        "iss": "https://securetoken.google.com/soulprint-test",
        "aud": "soulprint-test",
        "sub": "fake_uid",
        "exp": 9999999999,
        "iat": 1700000000,
    }, secret="random-key", alg="HS256")
    with pytest.raises(ValueError):
        fb.verify_firebase_id_token(forged)


def test_verify_errors_when_unconfigured(fb, monkeypatch):
    monkeypatch.setattr(fb, "FIREBASE_PROJECT_ID", "")
    with pytest.raises(ValueError, match="not configured"):
        fb.verify_firebase_id_token("anything")


def test_firebase_enabled_reflects_project_id(fb, monkeypatch):
    monkeypatch.setattr(fb, "FIREBASE_PROJECT_ID", "")
    assert fb.firebase_enabled() is False
    monkeypatch.setattr(fb, "FIREBASE_PROJECT_ID", "soulprint-b9431")
    assert fb.firebase_enabled() is True


# ── Provider extraction ───────────────────────────────────────────────


def test_extract_provider_google(fb):
    claims = {"firebase": {"sign_in_provider": "google.com"}}
    assert fb._extract_provider(claims) == "google.com"


def test_extract_provider_password(fb):
    claims = {"firebase": {"sign_in_provider": "password"}}
    assert fb._extract_provider(claims) == "password"


def test_extract_provider_unknown_when_missing(fb):
    assert fb._extract_provider({}) in ("unknown", "password")


# ── Mongo upsert linking logic ────────────────────────────────────────


class _FakeUsers:
    """Tiny in-memory stand-in for the Mongo `users` collection just
    rich enough to drive `upsert_user_for_firebase_claims`."""
    def __init__(self):
        self.docs = []
        self._next = 1

    async def find_one(self, q):
        for d in self.docs:
            if all(d.get(k) == v for k, v in q.items()):
                return d
        return None

    async def update_one(self, q, update):
        for d in self.docs:
            if all(d.get(k) == v for k, v in q.items()):
                d.update(update.get("$set", {}))
                return
        return None

    async def insert_one(self, doc):
        from bson import ObjectId
        doc["_id"] = ObjectId.from_datetime(__import__("datetime").datetime.now())
        self.docs.append(doc)
        class _R:
            inserted_id = doc["_id"]
        return _R()


class _FakeDB:
    def __init__(self):
        self.users = _FakeUsers()


@pytest.mark.asyncio
async def test_upsert_links_existing_email(fb):
    """Existing legacy user with matching email gets the Firebase UID
    attached — preserving their Mongo id, role, and history."""
    from bson import ObjectId
    db = _FakeDB()
    legacy_id = ObjectId()
    db.users.docs.append({
        "_id": legacy_id,
        "email": "reggie@coolgeek.me",
        "name": "Reggie",
        "is_superadmin": True,
    })
    claims = {
        "sub": "firebase-uid-123",
        "email": "reggie@coolgeek.me",
        "name": "Reggie via Google",
        "firebase": {"sign_in_provider": "google.com"},
    }
    out = await fb.upsert_user_for_firebase_claims(db, claims)
    assert out["id"] == str(legacy_id)
    assert out["email"] == "reggie@coolgeek.me"
    assert out["is_superadmin"] is True
    # Doc was annotated with the firebase_uid for next time
    linked = await db.users.find_one({"_id": legacy_id})
    assert linked["firebase_uid"] == "firebase-uid-123"
    assert linked["firebase_provider"] == "google.com"


@pytest.mark.asyncio
async def test_upsert_returns_existing_firebase_uid(fb):
    from bson import ObjectId
    db = _FakeDB()
    legacy_id = ObjectId()
    db.users.docs.append({
        "_id": legacy_id,
        "email": "x@y.com",
        "name": "X",
        "firebase_uid": "u-1",
    })
    out = await fb.upsert_user_for_firebase_claims(
        db, {"sub": "u-1", "email": "x@y.com", "name": "X"},
    )
    assert out["id"] == str(legacy_id)


@pytest.mark.asyncio
async def test_upsert_creates_fresh_user_when_no_match(fb):
    db = _FakeDB()
    out = await fb.upsert_user_for_firebase_claims(
        db, {"sub": "u-new", "email": "new@example.com", "name": "Newbie"},
    )
    assert out["email"] == "new@example.com"
    assert out["name"] == "Newbie"
    assert out["is_superadmin"] is False
    assert len(db.users.docs) == 1
    assert db.users.docs[0]["firebase_uid"] == "u-new"
    assert db.users.docs[0]["password_hash"] == ""  # Firebase-only


@pytest.mark.asyncio
async def test_upsert_promotes_superadmin_via_env(fb, monkeypatch):
    monkeypatch.setenv("SUPERADMIN_EMAILS", "boss@example.com,vip@example.com")
    db = _FakeDB()
    out = await fb.upsert_user_for_firebase_claims(
        db, {"sub": "u", "email": "boss@example.com", "name": "Boss"},
    )
    assert out["is_superadmin"] is True


@pytest.mark.asyncio
async def test_upsert_raises_when_no_uid(fb):
    db = _FakeDB()
    with pytest.raises(ValueError, match="subject"):
        await fb.upsert_user_for_firebase_claims(db, {"email": "x@y.com"})
