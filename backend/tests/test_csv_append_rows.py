"""Tests for the `csv_append_rows` mission tool.

Verifies the Feb 18, 2026 fix that lets list/lead-hunt missions
incrementally build a CSV across multiple turns instead of being
forced to emit one giant write_artifact call.

We construct the tool with a lightweight stub of `save_artifacts` that
records the CSV bytes it received, and drive the tool through 3 calls
(initial declare-columns batch + 2 incremental appends + a final
de-dupe replace). Then we assert the CSV grew correctly, mission state
persisted across calls, and the artifact dict carries `csv_row_count`
so the dataset gate can read it.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from missions import build_tools  # noqa: E402


class FakeCollection:
    """Minimal AsyncIOMotorCollection stub — just the calls we hit."""
    def __init__(self):
        self.docs: dict = {}
        self.deletes: list = []

    async def find_one(self, query, projection=None):
        # Match on `id` field (mission docs use a UUID `id`).
        mid = query.get("id")
        return self.docs.get(mid)

    async def update_one(self, query, update):
        mid = query.get("id")
        if mid not in self.docs:
            self.docs[mid] = {"id": mid}
        doc = self.docs[mid]
        # Apply $set with dotted keys
        for k, v in (update.get("$set") or {}).items():
            parts = k.split(".")
            cur = doc
            for p in parts[:-1]:
                cur = cur.setdefault(p, {})
            cur[parts[-1]] = v
        # Apply $push (artifacts)
        for k, v in (update.get("$push") or {}).items():
            doc.setdefault(k, []).append(v)
        # Apply $pull (remove artifacts matching a filter dict)
        for k, filt in (update.get("$pull") or {}).items():
            old = doc.get(k) or []
            doc[k] = [
                x for x in old
                if not all(x.get(fk) == fv for fk, fv in (filt or {}).items())
            ]

    async def delete_many(self, query):
        self.deletes.append(query)


class FakeDB:
    def __init__(self):
        self.missions = FakeCollection()
        self.documents = FakeCollection()
        self.media_files = FakeCollection()


@pytest.mark.asyncio
async def test_csv_append_rows_builds_growing_csv(tmp_path, monkeypatch):
    """Three sequential calls produce one growing CSV artifact, NOT three
    revisions in the org library."""
    db = FakeDB()
    mission_id = "test-mission-1"
    # Seed a mission doc the tool can find.
    db.missions.docs[mission_id] = {"id": mission_id}

    captured: list = []

    async def fake_save_artifacts(_db, blocks, **kwargs):
        # Record the content we got + simulate an attachment payload.
        for b in blocks:
            captured.append({
                "name": b["name"], "ext": b["ext"], "mime": b["mime"],
                "content": b["content"], "title": b["title"],
            })
        return [{
            "document_id": f"doc-{len(captured)}",
            "filename": f"saved-{len(captured)}.csv",
            "original_name": blocks[0]["name"],
            "mime": "text/csv",
            "url": f"/api/files/saved-{len(captured)}.csv",
            "size": len(blocks[0]["content"]),
        }]

    # Build the tool catalog with stubbed dependencies.
    tools = build_tools(
        db=db,
        docs_root=tmp_path,
        UPLOAD_DIR=tmp_path,
        AGENTS={},
        brave_search=AsyncMock(return_value=[]),
        perplexity_web_search=AsyncMock(return_value=[]),
        perplexity_available=lambda: False,
        generate_image_for=AsyncMock(),
        save_artifacts=fake_save_artifacts,
    )
    tool = next(t for t in tools if t.name == "csv_append_rows")

    ctx = {
        "mission_id": mission_id,
        "org_id": "org1",
        "lead_agent_id": "agent1",
        "lead_agent_name": "Test Agent",
        "created_by": "user1",
    }

    # Call 1 — declare columns + 3 rows.
    res1 = await tool.handler(ctx, {
        "name": "phoenix_leads.csv",
        "columns": ["Company", "City", "Source URL"],
        "rows": [
            {"Company": "Acme", "City": "Phoenix", "Source URL": "https://chamber.example/1"},
            {"Company": "Bravo", "City": "Phoenix", "Source URL": "https://chamber.example/2"},
            {"Company": "Charlie", "City": "Phoenix", "Source URL": "https://chamber.example/3"},
        ],
    })
    assert res1["ok"] is True
    assert res1["row_count"] == 3
    assert res1["columns"] == ["Company", "City", "Source URL"]
    assert res1["artifact"]["csv_row_count"] == 3
    # Header + 3 rows in the CSV.
    assert captured[-1]["content"].count("\n") >= 3

    # Call 2 — append 2 more rows, columns omitted (locked).
    res2 = await tool.handler(ctx, {
        "name": "phoenix_leads.csv",
        "rows": [
            {"Company": "Delta", "City": "Phoenix", "Source URL": "https://bbb.example/d"},
            {"Company": "Echo", "City": "Phoenix", "Source URL": "https://bbb.example/e"},
        ],
    })
    assert res2["ok"] is True
    assert res2["row_count"] == 5
    assert res2["artifact"]["csv_row_count"] == 5
    # CSV now has 5 data rows + 1 header.
    assert captured[-1]["content"].count("\n") >= 5

    # Call 3 — REPLACE (e.g. agent de-duped).
    res3 = await tool.handler(ctx, {
        "name": "phoenix_leads.csv",
        "replace": True,
        "rows": [
            {"Company": "Acme", "City": "Phoenix", "Source URL": "https://chamber.example/1"},
            {"Company": "Bravo", "City": "Phoenix", "Source URL": "https://chamber.example/2"},
        ],
    })
    assert res3["ok"] is True
    assert res3["row_count"] == 2
    assert res3["artifact"]["csv_row_count"] == 2


@pytest.mark.asyncio
async def test_csv_append_rows_first_call_requires_columns(tmp_path):
    db = FakeDB()
    db.missions.docs["m1"] = {"id": "m1"}

    async def noop_save(*a, **k):
        return [{"original_name": "x.csv"}]

    tools = build_tools(
        db=db, docs_root=tmp_path, UPLOAD_DIR=tmp_path, AGENTS={},
        brave_search=AsyncMock(return_value=[]),
        perplexity_web_search=AsyncMock(return_value=[]),
        perplexity_available=lambda: False,
        generate_image_for=AsyncMock(),
        save_artifacts=noop_save,
    )
    tool = next(t for t in tools if t.name == "csv_append_rows")
    ctx = {"mission_id": "m1", "org_id": "o1", "lead_agent_id": "a",
           "lead_agent_name": "A", "created_by": "u"}

    res = await tool.handler(ctx, {
        "name": "leads.csv",
        "rows": [{"X": "y"}],  # no columns
    })
    assert "error" in res
    assert "columns" in res["error"].lower()


@pytest.mark.asyncio
async def test_csv_append_rows_caps_per_call(tmp_path):
    """Per-call cap (25) protects against the agent dumping 200 fake rows in one shot."""
    from missions import DATASET_MAX_ROWS_PER_CALL

    db = FakeDB()
    db.missions.docs["m1"] = {"id": "m1"}

    async def noop_save(_db, blocks, **k):
        return [{
            "original_name": blocks[0]["name"],
            "filename": "saved.csv",
            "mime": "text/csv",
            "url": "/api/files/saved.csv",
            "size": 100,
            "document_id": "doc1",
        }]

    tools = build_tools(
        db=db, docs_root=tmp_path, UPLOAD_DIR=tmp_path, AGENTS={},
        brave_search=AsyncMock(return_value=[]),
        perplexity_web_search=AsyncMock(return_value=[]),
        perplexity_available=lambda: False,
        generate_image_for=AsyncMock(),
        save_artifacts=noop_save,
    )
    tool = next(t for t in tools if t.name == "csv_append_rows")
    ctx = {"mission_id": "m1", "org_id": "o1", "lead_agent_id": "a",
           "lead_agent_name": "A", "created_by": "u"}

    # Hand the tool 60 rows — it should keep DATASET_MAX_ROWS_PER_CALL.
    big_batch = [{"X": f"r{i}", "Y": f"v{i}"} for i in range(60)]
    res = await tool.handler(ctx, {
        "name": "huge.csv",
        "columns": ["X", "Y"],
        "rows": big_batch,
    })
    assert res["ok"] is True
    assert res["row_count"] == DATASET_MAX_ROWS_PER_CALL
    assert res["truncated_call"] is True


@pytest.mark.asyncio
async def test_csv_append_rows_pulls_previous_artifact(tmp_path):
    """Each successive call $pulls any prior artifact entry for the same
    `original_name`, so mission.artifacts only ever holds the latest CSV."""
    db = FakeDB()
    db.missions.docs["m1"] = {
        "id": "m1",
        # Pre-seeded with two stale artifact rows for the same CSV name.
        "artifacts": [
            {"original_name": "leads.csv", "csv_row_count": 2},
            {"original_name": "leads.csv", "csv_row_count": 4},
            {"original_name": "other.pdf", "size": 100},
        ],
    }

    async def fake_save(_db, blocks, **k):
        return [{
            "original_name": blocks[0]["name"],
            "filename": "fresh.csv",
            "mime": "text/csv",
            "url": "/api/files/fresh.csv",
            "size": len(blocks[0]["content"]),
            "document_id": "doc-fresh",
        }]

    tools = build_tools(
        db=db, docs_root=tmp_path, UPLOAD_DIR=tmp_path, AGENTS={},
        brave_search=AsyncMock(return_value=[]),
        perplexity_web_search=AsyncMock(return_value=[]),
        perplexity_available=lambda: False,
        generate_image_for=AsyncMock(),
        save_artifacts=fake_save,
    )
    tool = next(t for t in tools if t.name == "csv_append_rows")
    ctx = {"mission_id": "m1", "org_id": "o1", "lead_agent_id": "a",
           "lead_agent_name": "A", "created_by": "u"}

    await tool.handler(ctx, {
        "name": "leads.csv",
        "columns": ["A", "B"],
        "rows": [{"A": "1", "B": "2"}],
    })
    # The stale leads.csv entries should be gone; other.pdf preserved.
    arts = db.missions.docs["m1"].get("artifacts") or []
    leads_entries = [a for a in arts if a.get("original_name") == "leads.csv"]
    # AFTER the $pull happens we DON'T re-push automatically — the loop's
    # `if artifact:` block does that based on the tool's return. So we
    # just assert the pull cleaned the old ones.
    assert len(leads_entries) == 0
    assert any(a.get("original_name") == "other.pdf" for a in arts)
