"""Regression tests for the widened ALLOWED_FILENAME_RE in serve_file.

Bug: /api/files/{filename} used to reject Kie video filenames like
`kie-abcdef123456.mp4` with a 400 "Invalid filename" because the old regex
disallowed .mp4/.mp3 extensions AND required 14+ hex chars (kie- prefix uses
only 12 hex). The regex was widened to:

    ^([a-z]+-)?[a-f0-9]{8,64}\.(png|jpg|jpeg|webp|gif|pdf|mp4|mov|webm|mp3|wav|m4a|ogg|aac|flac)$

These tests seed physical files under UPLOAD_DIR + accompanying db docs so
the ownership check passes, then hit the public /api/files/ endpoint and
assert 200 + correct mime for video/image/pdf, and 400 for malicious names.
"""
import os
import secrets
import pathlib
import asyncio

import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]

UPLOAD_DIR = pathlib.Path("/app/backend/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Minimal MP4 (ftyp header only — enough for the server to serve it as bytes)
MINIMAL_MP4 = (
    b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41"
    + b"\x00" * 16
)
# Minimal PNG (1x1 transparent)
MINIMAL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
)
MINIMAL_PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@pytest.fixture(scope="module")
def db():
    client = MongoClient(MONGO_URL)
    return client[DB_NAME]


def _run(x):
    return x  # no-op; using sync pymongo


@pytest.fixture(scope="module")
def kie_video_file(db):
    """Seed a physical kie-{12hex}.mp4 + a messages doc so ownership check passes."""
    hex12 = secrets.token_hex(6)  # exactly 12 lowercase hex chars
    filename = f"kie-{hex12}.mp4"
    path = UPLOAD_DIR / filename
    path.write_bytes(MINIMAL_MP4)

    msg_id = f"TEST_msg_{hex12}"
    _run(db.messages.insert_one({
        "_id": msg_id,
        "conversation_id": f"TEST_conv_{hex12}",
        "role": "assistant",
        "content": "here is your video",
        "media_filename": filename,
        "media_url": f"/api/files/{filename}",
        "media_status": "ready",
        "media_type": "video",
    }))

    yield filename

    # Teardown
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    _run(db.messages.delete_one({"_id": msg_id}))


@pytest.fixture(scope="module")
def nano_banana_png(db):
    """Seed a 32-hex .png (nano-banana pattern) + messages doc."""
    hex32 = secrets.token_hex(16)  # 32 lowercase hex chars
    filename = f"{hex32}.png"
    path = UPLOAD_DIR / filename
    path.write_bytes(MINIMAL_PNG)

    msg_id = f"TEST_msg_{hex32}"
    _run(db.messages.insert_one({
        "_id": msg_id,
        "conversation_id": f"TEST_conv_{hex32}",
        "role": "assistant",
        "content": "here is your image",
        "media_filename": filename,
        "media_type": "image",
    }))

    yield filename

    try:
        path.unlink()
    except FileNotFoundError:
        pass
    _run(db.messages.delete_one({"_id": msg_id}))


@pytest.fixture(scope="module")
def audit_pdf(db):
    """Seed a 32-hex .pdf + business_audits doc so ownership check passes."""
    hex32 = secrets.token_hex(16)
    filename = f"{hex32}.pdf"
    path = UPLOAD_DIR / filename
    path.write_bytes(MINIMAL_PDF)

    audit_id = f"TEST_audit_{hex32}"
    _run(db.business_audits.insert_one({
        "_id": audit_id,
        "pdf_filename": filename,
    }))

    yield filename

    try:
        path.unlink()
    except FileNotFoundError:
        pass
    _run(db.business_audits.delete_one({"_id": audit_id}))


# ---------- Primary bug repro ----------

class TestKieVideoServe:
    def test_kie_12hex_mp4_returns_200_video_mime(self, kie_video_file):
        url = f"{BASE_URL}/api/files/{kie_video_file}"
        r = requests.get(url, timeout=15)
        assert r.status_code == 200, (
            f"expected 200 for {kie_video_file}, got {r.status_code}: {r.text[:200]}"
        )
        ct = r.headers.get("content-type", "")
        assert "video/mp4" in ct or "mp4" in ct, f"unexpected content-type: {ct}"
        assert r.content.startswith(b"\x00\x00\x00\x20ftyp"), "body is not the seeded mp4"

    def test_kie_prefix_mov_extension_accepted(self):
        # The regex should also accept `.mov`, `.webm`
        hex12 = secrets.token_hex(6)
        # We're only testing the regex gate (no seeded doc), so we expect
        # either 200, 404 (not linked), but NOT 400.
        for ext in ("mov", "webm", "mp3", "wav"):
            r = requests.get(f"{BASE_URL}/api/files/kie-{hex12}.{ext}", timeout=10)
            assert r.status_code != 400, f"regex rejected valid {ext}: {r.text[:200]}"


# ---------- Regressions: legacy paths still work ----------

class TestLegacyMediaStillWorks:
    def test_nano_banana_32hex_png_returns_200(self, nano_banana_png):
        r = requests.get(f"{BASE_URL}/api/files/{nano_banana_png}", timeout=15)
        assert r.status_code == 200, (
            f"nano-banana png {nano_banana_png} broken: "
            f"{r.status_code} {r.text[:200]}"
        )
        assert "image/png" in r.headers.get("content-type", "")

    def test_audit_pdf_32hex_returns_200(self, audit_pdf):
        r = requests.get(f"{BASE_URL}/api/files/{audit_pdf}", timeout=15)
        assert r.status_code == 200, (
            f"audit pdf {audit_pdf} broken: {r.status_code} {r.text[:200]}"
        )
        assert "pdf" in r.headers.get("content-type", "").lower()


# ---------- Regressions: malicious/invalid filenames still rejected ----------

class TestSecurityStillRejects:
    @pytest.mark.parametrize("bad", [
        "malware.exe",
        "kie-abcdef123456.exe",
        "abcdef123456.sh",
        "abc.png",                     # <8 hex
        "abcdef1.mp4",                 # 7 hex
        "GHIJKL123456.mp4",            # non-hex chars
        "kie-abcdef12345g.mp4",        # non-hex 'g'
        "kie-abcdef123456.mp4.exe",    # double extension
        "kie-abcdef123456",            # no extension
    ])
    def test_invalid_filename_returns_400(self, bad):
        r = requests.get(f"{BASE_URL}/api/files/{bad}", timeout=10)
        assert r.status_code == 400, (
            f"expected 400 for {bad!r}, got {r.status_code}: {r.text[:200]}"
        )

    def test_path_traversal_does_not_leak(self):
        # Path traversal — even though ingress may normalize the path away
        # from /api/files/, we must never return the real /etc/passwd bytes.
        r = requests.get(f"{BASE_URL}/api/files/..%2F..%2Fetc%2Fpasswd", timeout=10)
        assert r.status_code in (400, 404), f"traversal returned {r.status_code}"
        assert b"root:" not in r.content, "path traversal leaked /etc/passwd!"

    def test_min_hex_boundary_exactly_8_accepted_by_regex(self):
        # Boundary: 8 hex should pass the regex (may still 404 if unlinked)
        r = requests.get(f"{BASE_URL}/api/files/abcdef01.mp4", timeout=10)
        assert r.status_code != 400, "8-hex boundary wrongly rejected"

    def test_max_hex_boundary_65_rejected(self):
        # 65 hex should fail (max is 64)
        long_hex = "a" * 65
        r = requests.get(f"{BASE_URL}/api/files/{long_hex}.mp4", timeout=10)
        assert r.status_code == 400, "65-hex boundary wrongly accepted"
