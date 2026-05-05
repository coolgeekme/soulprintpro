"""Regression test for the production "no logo available" bug.

Repro: a Kubernetes pod restart wipes /app/backend/uploads/{logo}.png
from ephemeral disk, but the org's brand_assets.primary_logo.filename
still points at it (and the bytes are safely mirrored to GridFS).

Pre-fix behaviour:
  * `_ensure_local` did not exist; the compose code path did
    `Path(upload_dir) / filename` and `if not is_file(): bail`.
  * User got "Your logo file is missing from storage — please re-upload"
    even though re-uploading would just overwrite a perfectly-good
    GridFS mirror with the same bytes.

Post-fix behaviour:
  * `_ensure_local(filename)` first checks disk; if missing, calls
    `recover_bytes_from_gridfs(filename)` and writes the bytes back.
  * Compose proceeds, logo gets applied, disk copy is restored for
    subsequent reads.

This test exercises `_ensure_local` in isolation so we don't need
the full router context.
"""
import os
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock
import pytest

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "logo_rehydrate_test")


@pytest.mark.asyncio
async def test_ensure_local_rehydrates_from_gridfs_when_disk_wiped():
    """Disk-wiped + GridFS-present → rehydrates and returns Path."""
    from social_posts import make_social_router

    with tempfile.TemporaryDirectory() as tmp:
        upload_dir = Path(tmp)
        target = upload_dir / "logo-abc.png"
        # Sentinel bytes — small but non-zero
        sentinel = b"\x89PNG\r\n\x1a\nFAKE-LOGO-BYTES"

        async def fake_recover(filename):
            assert filename == "logo-abc.png"
            return sentinel, "image/png", "primary-logo.png"

        # Build the router (we won't hit any endpoint — we just need to
        # reach into its closure for `_ensure_local`)
        router = make_social_router(
            db=None, AGENTS={}, EMERGENT_LLM_KEY="", LlmChat=None, UserMessage=None,
            require_org_member=AsyncMock(), get_current_user=AsyncMock(),
            route_best_agent=AsyncMock(), load_conv_or_403=AsyncMock(),
            generate_image_via_nano_banana=None, compose_logo_on_image=None,
            upload_dir=upload_dir, recover_bytes_from_gridfs=fake_recover,
        )
        # _ensure_local is closed-over inside the factory; reach via __closure__
        ensure_local = None
        for route in router.routes:
            for cell in (route.endpoint.__closure__ or ()):
                try: contents = cell.cell_contents
                except ValueError: continue
                if callable(contents) and getattr(contents, "__name__", "") == "_ensure_local":
                    ensure_local = contents
                    break
            if ensure_local: break
        assert ensure_local is not None, "Could not locate _ensure_local in router closure"

        # Disk is empty — should rehydrate
        assert not target.exists()
        result = await ensure_local("logo-abc.png")
        assert result == target
        assert target.exists(), "rehydrated bytes should land on disk"
        assert target.read_bytes() == sentinel

        # Second call — disk hit, no GridFS call needed
        called = {"n": 0}
        async def counted_recover(filename):
            called["n"] += 1
            return None, None, None
        router2 = make_social_router(
            db=None, AGENTS={}, EMERGENT_LLM_KEY="", LlmChat=None, UserMessage=None,
            require_org_member=AsyncMock(), get_current_user=AsyncMock(),
            route_best_agent=AsyncMock(), load_conv_or_403=AsyncMock(),
            generate_image_via_nano_banana=None, compose_logo_on_image=None,
            upload_dir=upload_dir, recover_bytes_from_gridfs=counted_recover,
        )
        ensure_local2 = None
        for route in router2.routes:
            for cell in (route.endpoint.__closure__ or ()):
                try: contents = cell.cell_contents
                except ValueError: continue
                if callable(contents) and getattr(contents, "__name__", "") == "_ensure_local":
                    ensure_local2 = contents
                    break
            if ensure_local2: break
        result2 = await ensure_local2("logo-abc.png")
        assert result2 == target
        assert called["n"] == 0, "should hit disk first, not GridFS"


@pytest.mark.asyncio
async def test_ensure_local_returns_none_when_truly_lost():
    """Disk wiped + GridFS empty → return None (file genuinely lost)."""
    from social_posts import make_social_router

    with tempfile.TemporaryDirectory() as tmp:
        upload_dir = Path(tmp)

        async def empty_recover(filename):
            return None, None, None

        router = make_social_router(
            db=None, AGENTS={}, EMERGENT_LLM_KEY="", LlmChat=None, UserMessage=None,
            require_org_member=AsyncMock(), get_current_user=AsyncMock(),
            route_best_agent=AsyncMock(), load_conv_or_403=AsyncMock(),
            generate_image_via_nano_banana=None, compose_logo_on_image=None,
            upload_dir=upload_dir, recover_bytes_from_gridfs=empty_recover,
        )
        ensure_local = None
        for route in router.routes:
            for cell in (route.endpoint.__closure__ or ()):
                try: contents = cell.cell_contents
                except ValueError: continue
                if callable(contents) and getattr(contents, "__name__", "") == "_ensure_local":
                    ensure_local = contents
                    break
            if ensure_local: break
        result = await ensure_local("logo-missing.png")
        assert result is None


if __name__ == "__main__":
    asyncio.run(test_ensure_local_rehydrates_from_gridfs_when_disk_wiped())
    asyncio.run(test_ensure_local_returns_none_when_truly_lost())
    print("OK")
