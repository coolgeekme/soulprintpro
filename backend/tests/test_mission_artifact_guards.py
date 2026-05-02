"""Phase A: Mission UX hardening tests.

Covers:
  1. artifacts.BINARY_EXTS exists and is disjoint from ALLOWED_EXTS.
  2. write_artifact tool rejects binary extensions (.png/.mp4/.pdf/.docx) BEFORE
     touching disk and returns a tool-specific error.
  3. write_artifact tool ACCEPTS allowed text extensions and persists them.
  4. Mission tool registry exposes write_artifact + generate_image with the
     updated descriptions.
  5. _build_lead_mission_system_prompt contains the new "DO NOT ASK FOR
     ACKNOWLEDGEMENT", "DO NOT ASK FOR A STATUS UPDATE" rules and the new
     "ARTIFACTS — TEXT ONLY" section.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

# Make /app/backend importable
sys.path.insert(0, "/app/backend")


# ── 1. artifacts module: BINARY_EXTS  ─────────────────────────────────────
class TestBinaryExtensionsConstant:
    def test_binary_exts_present_and_sized(self):
        from artifacts import BINARY_EXTS, ALLOWED_EXTS

        assert isinstance(BINARY_EXTS, set)
        # Spec says ~42 entries covering images/video/audio/binary-docs/archives
        assert len(BINARY_EXTS) >= 35
        # Spot-check key members
        for ext in ("png", "jpg", "jpeg", "webp", "gif", "svg",
                    "mp4", "mov", "mp3", "wav",
                    "pdf", "docx", "xlsx", "pptx",
                    "zip", "tar", "gz"):
            assert ext in BINARY_EXTS, f"{ext} missing from BINARY_EXTS"

    def test_binary_and_allowed_disjoint(self):
        from artifacts import BINARY_EXTS, ALLOWED_EXTS
        overlap = BINARY_EXTS & ALLOWED_EXTS
        assert overlap == set(), f"BINARY_EXTS overlaps ALLOWED_EXTS: {overlap}"


# ── 2,3,4. Mission tools: write_artifact handler  ─────────────────────────
def _build_tools_with_fakes():
    """Build the mission Tool list with stub deps so we can exercise the
    write_artifact handler without spinning up the full server.
    Returns: dict of tool_name -> Tool instance.
    """
    import missions
    from missions import build_tools

    # Fake DB & save_artifacts: capture calls, never touch real Mongo.
    saved: list[dict] = []

    class _Coll:
        async def find_one(self, *a, **kw):
            return None

        async def insert_one(self, *a, **kw):
            return None

        def find(self, *a, **kw):
            class _Cur:
                def sort(self, *a, **kw): return self
                def limit(self, *a, **kw): return self
                def __aiter__(self): return self
                async def __anext__(self): raise StopAsyncIteration
            return _Cur()

    class _DB:
        documents = _Coll()
        org_uploaded_files = _Coll()

    async def _save_artifacts_stub(db, blocks, **kw):
        # Mimic the real shape just enough for the handler to succeed.
        out = []
        for b in blocks:
            saved.append({"name": b["name"], "ext": b["ext"], "content": b["content"]})
            out.append({
                "document_id": "doc-stub-" + b["name"],
                "original_name": b["name"],
                "mime": b["mime"],
                "url": f"/api/files/docs/org/{b['name']}",
                "size": len(b["content"]),
            })
        return out

    async def _gen_image_stub(*a, **kw):
        return ("img.png", "stub caption", "nano-banana")

    async def _brave_stub(*a, **kw): return []
    async def _ppx_stub(*a, **kw): return {"text": "", "sources": []}

    tools = build_tools(
        db=_DB(),
        docs_root=Path("/tmp/sp-test-docs"),
        UPLOAD_DIR=Path("/tmp/sp-test-uploads"),
        AGENTS={},
        brave_search=_brave_stub,
        perplexity_web_search=_ppx_stub,
        perplexity_available=lambda: False,
        generate_image_for=_gen_image_stub,
        save_artifacts=_save_artifacts_stub,
    )
    return {t.name: t for t in tools}, saved


@pytest.fixture
def mission_tools():
    return _build_tools_with_fakes()


def _ctx():
    return {
        "org_id": "org-test",
        "mission_id": "mission-test",
        "lead_agent_id": "agent-test",
        "lead_agent_name": "Test Agent",
        "created_by": "user-test",
    }


class TestWriteArtifactBinaryRejection:
    def _call(self, tools, args):
        handler = tools["write_artifact"].handler
        return asyncio.get_event_loop().run_until_complete(handler(_ctx(), args))

    def test_png_rejected_with_image_specific_message(self, mission_tools):
        tools, saved = mission_tools
        result = self._call(tools, {"name": "diagram.png", "content": "anything"})
        assert "error" in result
        msg = result["error"]
        assert "TEXT-ONLY" in msg
        assert ".png" in msg
        assert "generate_image" in msg
        # Must NOT have persisted anything
        assert saved == []

    @pytest.mark.parametrize("ext", ["jpg", "jpeg", "webp", "gif"])
    def test_other_image_exts_rejected_with_image_message(self, mission_tools, ext):
        tools, saved = mission_tools
        result = self._call(tools, {"name": f"pic.{ext}", "content": "x"})
        assert "error" in result
        assert "generate_image" in result["error"]
        assert saved == []

    @pytest.mark.parametrize("ext", ["mp4", "pdf", "docx", "xlsx", "zip", "mp3"])
    def test_non_image_binary_rejected_with_generic_message(self, mission_tools, ext):
        tools, saved = mission_tools
        result = self._call(tools, {"name": f"thing.{ext}", "content": "x"})
        assert "error" in result
        msg = result["error"]
        assert f".{ext}" in msg
        assert "binary" in msg.lower()
        # Generic non-image binary message lists allowed text exts
        assert ".md" in msg and ".csv" in msg
        assert saved == []


class TestWriteArtifactAcceptsTextExts:
    def _call(self, tools, args):
        handler = tools["write_artifact"].handler
        return asyncio.get_event_loop().run_until_complete(handler(_ctx(), args))

    @pytest.mark.parametrize("ext", [
        "md", "csv", "json", "html", "ics", "sh", "py", "js", "sql",
        "txt", "xml", "yaml",
    ])
    def test_accepts_text_extension(self, mission_tools, ext):
        tools, saved = mission_tools
        result = self._call(tools, {
            "name": f"out.{ext}",
            "content": f"hello {ext}",
        })
        assert result.get("ok") is True, f"expected ok for .{ext}, got {result}"
        assert result["artifact"]["original_name"].endswith(f".{ext}")
        assert any(s["ext"] == ext for s in saved)


# ── 4. Tool registry descriptions  ────────────────────────────────────────
class TestToolRegistryDescriptions:
    def test_write_artifact_description_warns_text_only(self, mission_tools):
        tools, _ = mission_tools
        wa = tools["write_artifact"]
        desc = wa.description
        # Spec says "TEXT-ONLY" or "NOT for images" language
        assert ("TEXT-ONLY" in desc) or ("NOT for images" in desc) or ("TEXT" in desc.upper())
        # Should call out that binaries are rejected / point to generate_image
        assert "generate_image" in desc

    def test_generate_image_description_mentions_png_and_warning(self, mission_tools):
        tools, _ = mission_tools
        gi = tools["generate_image"]
        desc = gi.description
        assert "PNG" in desc.upper()
        # Warns against using write_artifact for images
        assert "write_artifact" in desc


# ── 5. Lead mission system prompt content  ────────────────────────────────
class TestLeadMissionSystemPromptText:
    def test_prompt_contains_new_rules_and_artifact_section(self):
        # We import via server module path; this re-runs server import.
        # If imports are heavy, the test still completes since server is
        # already loaded by the running supervisor process — we just import
        # the function name.
        import importlib
        server = importlib.import_module("server")
        prompt = server._build_lead_mission_system_prompt("(stub catalog)")
        assert isinstance(prompt, str) and len(prompt) > 500

        # New ASK_USER rules
        assert "DO NOT ASK FOR ACKNOWLEDGEMENT" in prompt
        assert "DO NOT ASK FOR A STATUS UPDATE" in prompt
        # Banned phrase samples should be quoted. The prompt is built via
        # multi-line Python string concatenation which introduces extra
        # whitespace at line boundaries — normalise whitespace before
        # checking so we're testing intent, not formatting.
        import re
        flat = re.sub(r"\s+", " ", prompt)
        for banned in ("Shall I continue?", "Want me to proceed?",
                       "Let me know if this looks good"):
            assert banned in flat, f"banned phrase not listed: {banned}"

        # New ARTIFACTS — TEXT ONLY section
        assert "ARTIFACTS" in prompt
        assert "TEXT ONLY" in prompt or "TEXT-ONLY" in prompt
        assert "generate_image" in prompt
        # Allowed extensions listed
        for ext in (".md", ".csv", ".json", ".html", ".ics"):
            assert ext in prompt
