"""Phase B: Mission Attachments — backend tests

Covers (HTTP to REACT_APP_BACKEND_URL):
  - POST /api/orgs/{org_id}/missions/uploads
      · accepts multipart 'files', returns {attachments: [...]}
      · rejects .exe (400)
      · rejects empty file (400)
      · rejects >25 MB file (400)
      · rejects >6 files in one call (400)
      · text_excerpt populated for txt/md, empty for image
  - DELETE /api/orgs/{org_id}/missions/uploads/{attachment_id}
      · succeeds for pending owner-uploaded record
      · 404 for non-existent / not-owned
      · 409 for already-consumed
  - POST /api/orgs/{org_id}/missions
      · attachment_ids resolved into mission.attachments
      · '[USER ATTACHMENTS]' block appended to goal
      · pending records flipped to consumed_by_mission_id
      · 400 for non-existent attachment_id
      · 400 for already-consumed attachment_id
      · serialize_mission emits attachments WITHOUT path / text_excerpt
  - read_attachment tool (handler-level)
      · no args -> listing
      · matching name -> full content
      · non-matching name -> error
      · no mission context -> error
  - Regression: missions without attachments still work; goal unchanged;
    serialize_mission returns empty attachments list.
"""
import io
import os
import asyncio
import uuid
import pytest
import requests


def _base_url() -> str:
    url = os.environ.get("REACT_APP_BACKEND_URL", "").strip()
    if not url:
        try:
            with open("/app/frontend/.env", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("REACT_APP_BACKEND_URL="):
                        url = line.split("=", 1)[1].strip()
                        break
        except OSError:
            pass
    return url.rstrip("/")


BASE_URL = _base_url()
assert BASE_URL, "REACT_APP_BACKEND_URL missing"
API = f"{BASE_URL}/api"


# ── HTTP fixture: fresh user + fresh org per module ─────────────────────────
@pytest.fixture(scope="module")
def ctx():
    s = requests.Session()
    s.headers["Content-Type"] = "application/json"
    email = f"TEST_missattach_{uuid.uuid4().hex[:10]}@example.com"
    pwd = "StrongPass!234"
    r = s.post(f"{API}/auth/register", json={
        "email": email, "password": pwd, "name": "Mission Attach Tester",
    }, timeout=30)
    assert r.status_code in (200, 201), r.text
    token = r.json().get("access_token") or r.json().get("token")
    s.headers["Authorization"] = f"Bearer {token}"
    rl = s.post(f"{API}/auth/login",
                json={"email": email, "password": pwd}, timeout=30)
    if rl.status_code == 200:
        tk2 = rl.json().get("access_token") or rl.json().get("token")
        if tk2:
            s.headers["Authorization"] = f"Bearer {tk2}"

    r = s.get(f"{API}/orgs", timeout=20)
    orgs = r.json() if r.status_code == 200 else []
    if orgs:
        org_id = orgs[0]["id"]
    else:
        c = s.post(f"{API}/orgs",
                   json={"name": f"TEST_MissAttachOrg_{uuid.uuid4().hex[:6]}"},
                   timeout=60)
        assert c.status_code in (200, 201), c.text
        org_id = c.json().get("id") or c.json().get("_id")
    yield {"s": s, "org_id": org_id, "email": email}


def _multipart_session(ctx):
    """Return a session that doesn't force JSON Content-Type."""
    s2 = requests.Session()
    s2.headers["Authorization"] = ctx["s"].headers["Authorization"]
    return s2


def _upload_one(ctx, name="brief.md", content=b"# Hello\nThis is a tiny brief."):
    s = _multipart_session(ctx)
    files = [("files", (name, io.BytesIO(content), "text/markdown"))]
    return s.post(f"{API}/orgs/{ctx['org_id']}/missions/uploads",
                  files=files, timeout=30)


# ── Upload happy path ──────────────────────────────────────────────────────
class TestUploadHappy:

    def test_upload_md_returns_attachment(self, ctx):
        r = _upload_one(ctx, "brief.md", b"# Title\nA short brief paragraph.")
        assert r.status_code == 200, r.text
        data = r.json()
        assert "attachments" in data and len(data["attachments"]) == 1
        a = data["attachments"][0]
        assert a["original_name"] == "brief.md"
        assert a["kind"] == "text"
        assert a["size"] > 0
        assert a["url"].startswith("/api/files/")
        # text excerpt populated for text-extractable
        assert "brief paragraph" in (a.get("text_excerpt") or "")

    def test_upload_image_has_empty_text_excerpt(self, ctx):
        s = _multipart_session(ctx)
        # 1x1 PNG (smallest valid)
        png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
               b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
               b"\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\x00\x00\x00\x03"
               b"\x00\x01\x83\x83\xb6\x82\x00\x00\x00\x00IEND\xaeB`\x82")
        files = [("files", ("pixel.png", io.BytesIO(png), "image/png"))]
        r = s.post(f"{API}/orgs/{ctx['org_id']}/missions/uploads",
                   files=files, timeout=30)
        assert r.status_code == 200, r.text
        a = r.json()["attachments"][0]
        assert a["kind"] == "image"
        assert (a.get("text_excerpt") or "") == ""
        # cleanup so it doesn't pollute later tests
        s2 = ctx["s"]
        s2.delete(f"{API}/orgs/{ctx['org_id']}/missions/uploads/{a['id']}",
                  timeout=15)


# ── Upload rejection paths ─────────────────────────────────────────────────
class TestUploadRejects:

    def test_rejects_exe(self, ctx):
        s = _multipart_session(ctx)
        files = [("files", ("malware.exe", io.BytesIO(b"MZ\x90\x00"),
                            "application/x-msdownload"))]
        r = s.post(f"{API}/orgs/{ctx['org_id']}/missions/uploads",
                   files=files, timeout=20)
        assert r.status_code == 400
        assert "Unsupported" in r.text or "unsupported" in r.text.lower()

    def test_rejects_empty(self, ctx):
        s = _multipart_session(ctx)
        files = [("files", ("blank.txt", io.BytesIO(b""), "text/plain"))]
        r = s.post(f"{API}/orgs/{ctx['org_id']}/missions/uploads",
                   files=files, timeout=20)
        assert r.status_code == 400
        assert "mpty" in r.text  # "Empty file"

    def test_rejects_oversize(self, ctx):
        s = _multipart_session(ctx)
        big = b"a" * (25 * 1024 * 1024 + 1)
        files = [("files", ("huge.txt", io.BytesIO(big), "text/plain"))]
        r = s.post(f"{API}/orgs/{ctx['org_id']}/missions/uploads",
                   files=files, timeout=120)
        assert r.status_code == 400
        assert "large" in r.text.lower() or "max" in r.text.lower()

    def test_rejects_more_than_six(self, ctx):
        s = _multipart_session(ctx)
        files = [
            ("files", (f"f{i}.txt", io.BytesIO(f"content {i}".encode()),
                       "text/plain"))
            for i in range(7)
        ]
        r = s.post(f"{API}/orgs/{ctx['org_id']}/missions/uploads",
                   files=files, timeout=30)
        assert r.status_code == 400
        assert "6" in r.text


# ── Delete pending attachment ──────────────────────────────────────────────
class TestDeletePending:

    def test_delete_owner_pending_succeeds(self, ctx):
        r = _upload_one(ctx, "todrop.md", b"# Drop me\ndata")
        att_id = r.json()["attachments"][0]["id"]
        d = ctx["s"].delete(
            f"{API}/orgs/{ctx['org_id']}/missions/uploads/{att_id}",
            timeout=20,
        )
        assert d.status_code == 200, d.text

    def test_delete_unknown_returns_404(self, ctx):
        d = ctx["s"].delete(
            f"{API}/orgs/{ctx['org_id']}/missions/uploads/{uuid.uuid4().hex}",
            timeout=20,
        )
        assert d.status_code == 404


# ── Start mission with attachments ─────────────────────────────────────────
class TestStartMissionWithAttachments:

    def test_attachment_ids_resolved_and_consumed(self, ctx):
        # upload 1 md
        r = _upload_one(ctx, "spec.md",
                        b"# Spec\nGoal: ship the feature\n- point A\n- point B")
        att = r.json()["attachments"][0]
        att_id = att["id"]

        # start a mission referencing it
        m = ctx["s"].post(
            f"{API}/orgs/{ctx['org_id']}/missions",
            json={
                "goal": "Summarise the attached spec.",
                "attachment_ids": [att_id],
            }, timeout=60,
        )
        assert m.status_code in (200, 201), m.text
        mission = m.json()
        mission_id = mission["id"]

        # serialize_mission shape: has attachments, no path/text_excerpt leaked
        assert isinstance(mission.get("attachments"), list)
        assert len(mission["attachments"]) == 1
        a = mission["attachments"][0]
        assert a["id"] == att_id
        assert a["original_name"] == "spec.md"
        assert a["kind"] == "text"
        assert a["url"].startswith("/api/files/")
        assert "text_excerpt" not in a
        assert "path" not in a
        assert "filename" not in a

        # second mission with same id must be rejected (already consumed)
        m2 = ctx["s"].post(
            f"{API}/orgs/{ctx['org_id']}/missions",
            json={"goal": "Reuse same attachment", "attachment_ids": [att_id]},
            timeout=30,
        )
        assert m2.status_code == 400
        assert "already" in m2.text.lower()

        # delete now-consumed attachment must 409 OR 404 (it's been consumed
        # so the (uploader_id, pending) lookup for delete may not match
        # if endpoint scopes by consumed_by_mission_id="").
        # Spec says 409 — verify.
        d = ctx["s"].delete(
            f"{API}/orgs/{ctx['org_id']}/missions/uploads/{att_id}",
            timeout=20,
        )
        # endpoint returns 409 ONLY if the find_one matched and rec had
        # consumed_by_mission_id set. The current code does match (it
        # looks by id+org+uploader, no consumed filter), so 409 expected.
        assert d.status_code == 409, d.text

        # cleanup mission so we don't pollute org listing
        ctx["s"].delete(f"{API}/orgs/{ctx['org_id']}/missions/{mission_id}",
                        timeout=15)

    def test_unknown_attachment_id_returns_400(self, ctx):
        bogus = uuid.uuid4().hex
        m = ctx["s"].post(
            f"{API}/orgs/{ctx['org_id']}/missions",
            json={"goal": "Test bogus attachment id.",
                  "attachment_ids": [bogus]},
            timeout=30,
        )
        assert m.status_code == 400
        body = m.text.lower()
        assert "not found" in body or "already consumed" in body

    def test_mission_without_attachments_works(self, ctx):
        m = ctx["s"].post(
            f"{API}/orgs/{ctx['org_id']}/missions",
            json={"goal": "Plain mission with no files."},
            timeout=60,
        )
        assert m.status_code in (200, 201), m.text
        body = m.json()
        # attachments key is present and is empty list (not missing)
        assert body.get("attachments") == []
        # goal is unmodified by [USER ATTACHMENTS]
        assert "[USER ATTACHMENTS" not in (body.get("goal") or "")
        ctx["s"].delete(f"{API}/orgs/{ctx['org_id']}/missions/{body['id']}",
                        timeout=15)


# ── read_attachment tool — handler-level unit tests ───────────────────────
class _StubMissions:
    """Stub for db.missions: holds one mission doc, returns it on find_one."""
    def __init__(self, doc):
        self.doc = doc

    async def find_one(self, q, proj=None):
        return self.doc


class _StubDB:
    def __init__(self, mission_doc):
        self.missions = _StubMissions(mission_doc)


def _get_read_attachment_tool(mission_doc, upload_dir):
    """Build the tool catalog and pluck out read_attachment."""
    import sys
    sys.path.insert(0, "/app/backend")
    import missions as M  # noqa

    async def _noop(*a, **k):
        return {}

    tools = M.build_tools(
        db=_StubDB(mission_doc),
        docs_root=upload_dir,
        UPLOAD_DIR=upload_dir,
        AGENTS={"lead": {"name": "Lead"}},
        brave_search=_noop,
        perplexity_web_search=_noop,
        perplexity_available=lambda: False,
        generate_image_for=_noop,
        save_artifacts=_noop,
    )
    by_name = {t.name: t for t in tools}
    return by_name["read_attachment"]


class TestReadAttachmentTool:

    def test_no_mission_context_returns_error(self, tmp_path):
        tool = _get_read_attachment_tool(None, tmp_path)
        out = asyncio.get_event_loop().run_until_complete(
            tool.handler({}, {}))
        assert "error" in out
        assert "mission context" in out["error"].lower()

    def test_no_attachments_returns_error(self, tmp_path):
        tool = _get_read_attachment_tool({"attachments": []}, tmp_path)
        out = asyncio.get_event_loop().run_until_complete(
            tool.handler({"mission_id": "m1"}, {}))
        assert "error" in out
        assert "attach" in out["error"].lower()

    def test_no_args_returns_listing(self, tmp_path):
        att = {
            "id": "a1", "original_name": "brief.md", "kind": "text",
            "size": 42, "filename": "x.md",
            "text_excerpt": "Hello world preview text",
        }
        tool = _get_read_attachment_tool({"attachments": [att]}, tmp_path)
        out = asyncio.get_event_loop().run_until_complete(
            tool.handler({"mission_id": "m1"}, {}))
        assert "listing" in out
        assert len(out["listing"]) == 1
        assert out["listing"][0]["name"] == "brief.md"
        assert "hint" in out

    def test_match_by_name_returns_full_content(self, tmp_path):
        # write file to upload_dir so on-disk re-extract is possible
        fp = tmp_path / "story.txt"
        fp.write_text("Once upon a time, there was a deeply detailed story." * 20)
        att = {
            "id": "a1", "original_name": "story.txt", "kind": "text",
            "size": fp.stat().st_size, "filename": "story.txt",
            "text_excerpt": "Once upon a time, there was a deeply detailed story.",
            "mime": "text/plain",
        }
        tool = _get_read_attachment_tool({"attachments": [att]}, tmp_path)
        out = asyncio.get_event_loop().run_until_complete(
            tool.handler({"mission_id": "m1"}, {"name": "story"}))
        assert "content" in out
        assert "Once upon a time" in out["content"]
        assert out["name"] == "story.txt"

    def test_no_match_returns_error(self, tmp_path):
        att = {"id": "a1", "original_name": "brief.md", "kind": "text",
               "size": 5, "filename": "x.md", "text_excerpt": "hi"}
        tool = _get_read_attachment_tool({"attachments": [att]}, tmp_path)
        out = asyncio.get_event_loop().run_until_complete(
            tool.handler({"mission_id": "m1"}, {"name": "nonexistent"}))
        assert "error" in out
        assert "no matching" in out["error"].lower()


# ── displayMissionGoal frontend stripping mirrored as docstring check ──────
class TestSerializeAndGoalShape:

    def test_goal_includes_user_attachments_block(self, ctx):
        # upload + start a mission with attachment, fetch via list endpoint
        r = _upload_one(ctx, "specA.md",
                        b"# SpecA\nThis is the brief content.")
        att_id = r.json()["attachments"][0]["id"]
        m = ctx["s"].post(
            f"{API}/orgs/{ctx['org_id']}/missions",
            json={
                "goal": "Summarise the attached brief.",
                "attachment_ids": [att_id],
            }, timeout=60,
        )
        assert m.status_code in (200, 201), m.text
        mission = m.json()
        # The serialized goal should still contain the [USER ATTACHMENTS]
        # block — frontend's displayMissionGoal strips it for display.
        assert "[USER ATTACHMENTS" in (mission.get("goal") or "")
        # original user goal at the top
        assert mission["goal"].startswith("Summarise the attached brief.")
        ctx["s"].delete(f"{API}/orgs/{ctx['org_id']}/missions/{mission['id']}",
                        timeout=15)
