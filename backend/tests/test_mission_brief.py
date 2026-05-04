"""Regression for the mission-brief auto-synthesis behaviour.

When a mission finishes with a meaningful summary but produces no
[[ARTIFACT]] blocks, the backend should auto-build a Markdown brief and
push it onto the mission's artifacts list AND insert a Documents row so
the user can find the result on (a) the Mission Detail page, (b) the
Org Documents library.
"""
import os
import uuid
import pytest
import pytest_asyncio
from datetime import datetime, timezone
from pathlib import Path
from motor.motor_asyncio import AsyncIOMotorClient

from missions import _synthesize_brief_artifact


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
async def test_mission_brief_auto_synthesis(db, tmp_path: Path):
    """Synthesizes a markdown brief, attaches it to the mission, and
    persists a documents row referencing the same file."""
    org_id = "test-org-" + uuid.uuid4().hex[:8]
    mission_id = "test-mission-" + uuid.uuid4().hex[:8]
    # Mission row needs to exist so update_one + $push has something to
    # update against.
    await db.missions.insert_one({
        "id": mission_id,
        "org_id": org_id,
        "goal": "Compile a market analysis of low-code platforms",
        "status": "running",
        "lead_agent_id": "specialized-chief-of-staff",
        "lead_agent_name": "Chief of Staff",
        "created_by": "test-user",
        "started_at": datetime.now(timezone.utc),
        "artifacts": [],
    })
    # Point DOCS_DIR at the pytest tmp dir so we don't pollute /app
    os.environ["DOCS_DIR"] = str(tmp_path)
    summary = (
        "We surveyed 6 low-code platforms — Bubble, Webflow, Glide, "
        "Retool, Softr, and OutSystems. Webflow leads on design polish; "
        "Bubble on logical flexibility; Retool on internal-tools speed. "
        "Recommend Bubble for the prototype with a Retool admin layer."
    )
    mission = await db.missions.find_one({"id": mission_id})
    try:
        await _synthesize_brief_artifact(
            db, mission_id=mission_id, mission=mission,
            summary_text=summary, iteration=12,
        )
        # Mission's artifacts array should now have one entry
        m2 = await db.missions.find_one({"id": mission_id})
        arts = m2.get("artifacts") or []
        assert len(arts) == 1, f"expected 1 artifact, got {arts!r}"
        a = arts[0]
        assert a["kind"] == "document"
        assert a["mime"] == "text/markdown"
        assert a["original_name"].startswith("mission-brief-")
        assert a.get("auto_synthesized") is True
        # Documents collection must carry a row pointing at the same file
        d = await db.documents.find_one({
            "org_id": org_id, "filename": a["filename"],
        })
        assert d is not None, "documents row missing — won't show in library"
        assert d["mime"] == "text/markdown"
        # File should exist on disk (in tmp_path because we overrode DOCS_DIR)
        on_disk = Path(tmp_path) / org_id / a["filename"]
        assert on_disk.exists(), f"brief file missing at {on_disk}"
        body = on_disk.read_text()
        assert "Mission Brief" in body
        assert "Outcome" in body
        assert "Bubble" in body, "summary text should round-trip into the brief"
    finally:
        # Cleanup
        await db.missions.delete_one({"id": mission_id})
        await db.mission_events.delete_many({"mission_id": mission_id})
        await db.documents.delete_many({"org_id": org_id})
