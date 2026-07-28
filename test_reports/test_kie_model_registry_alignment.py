#!/usr/bin/env python3
"""Focused regression verification for kie.ai frontend/backend model-key alignment.

This script intentionally tests only the reported bug:
frontend smart image routing previously selected `seedream-5-lite`, which the
backend rejected as an unknown Kie model. It verifies the current frontend router
selects a backend-accepted model and that all current frontend image keys pass
the `/generate-media` unknown-model guard.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests


ROOT = Path("/app")
FRONTEND = ROOT / "frontend"
BACKEND = ROOT / "backend"
BASE_URL = os.environ.get("TEST_BACKEND_URL", "http://localhost:8001")

EMAIL = "reggie@coolgeek.me"
PASSWORD = "superadminpass123"
AGENT_ID = "specialized-chief-of-staff"


def run_node_router() -> str:
    code = (
        "import('./src/lib/spe-sdk/model-router.js')"
        ".then(m=>console.log(m.selectImageModel('Design a flyer for our launch event').model.key))"
        ".catch(e=>{console.error(e); process.exit(1)})"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", code],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(f"router node command failed: {result.stderr}")
    return result.stdout.strip().splitlines()[-1]


def frontend_image_keys() -> list[str]:
    src = (FRONTEND / "src/lib/spe-sdk/model-registry.js").read_text()
    block = re.search(
        r"const\s+DEFAULT_IMAGE_MODELS\s*=\s*\[(.*?)\];", src, flags=re.S
    )
    if not block:
        raise RuntimeError("Could not locate DEFAULT_IMAGE_MODELS block")
    return re.findall(r"key:\s*'([^']+)'", block.group(1))


def frontend_broken_key_matches() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"seedream-5-lite": [], "flux-pro": []}
    for path in (FRONTEND / "src").rglob("*"):
        if not path.is_file() or path.suffix not in {".js", ".jsx", ".ts", ".tsx"}:
            continue
        text = path.read_text(errors="ignore")
        for key in out:
            if key in text:
                out[key].append(str(path.relative_to(ROOT)))
    return out


def backend_kie_provider_keys() -> set[str]:
    sys.path.insert(0, str(BACKEND))
    import kie_client  # type: ignore

    return set(kie_client.KIE_PROVIDERS.keys())


def request_json(session: requests.Session, method: str, path: str, **kwargs):
    resp = session.request(method, f"{BASE_URL}{path}", timeout=60, **kwargs)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text[:1000]}
    return resp, data


def setup_conversation(session: requests.Session) -> str:
    login_resp, login_data = request_json(
        session,
        "POST",
        "/api/auth/login",
        json={"email": EMAIL, "password": PASSWORD},
    )
    if login_resp.status_code != 200:
        raise RuntimeError(f"login failed {login_resp.status_code}: {login_data}")
    token = login_data.get("access_token")
    if token:
        session.headers.update({"Authorization": f"Bearer {token}"})

    suffix = int(time.time())
    org_resp, org_data = request_json(
        session,
        "POST",
        "/api/orgs",
        json={"name": f"KIE Alignment QA {suffix}", "website_url": "", "soul_description": ""},
    )
    if org_resp.status_code != 200:
        raise RuntimeError(f"org create failed {org_resp.status_code}: {org_data}")
    org_id = org_data["id"]

    team_resp, team_data = request_json(
        session,
        "POST",
        f"/api/orgs/{org_id}/teams",
        json={"name": f"KIE QA Team {suffix}", "description": "", "visibility": "open"},
    )
    if team_resp.status_code != 200:
        raise RuntimeError(f"team create failed {team_resp.status_code}: {team_data}")
    team_id = team_data["id"]

    conv_resp, conv_data = request_json(
        session,
        "POST",
        f"/api/teams/{team_id}/conversations",
        json={
            "title": "KIE model alignment QA",
            "type": "direct",
            "agent_ids": [AGENT_ID],
            "member_ids": [],
            "auto_respond": False,
            "smart_expand": False,
        },
    )
    if conv_resp.status_code != 200:
        raise RuntimeError(f"conversation create failed {conv_resp.status_code}: {conv_data}")
    return conv_data["id"]


def post_generate_media(session: requests.Session, conv_id: str, model: str) -> dict:
    resp, data = request_json(
        session,
        "POST",
        f"/api/conversations/{conv_id}/generate-media",
        json={
            "prompt": "Design a small QA flyer for a launch event",
            "model": model,
            "media_type": "image",
            "aspect_ratio": "1:1",
        },
    )
    text = json.dumps(data, default=str)
    return {
        "model": model,
        "status_code": resp.status_code,
        "response": data,
        "contains_unknown_model": "Unknown model" in text,
        "passed_unknown_guard": "Unknown model" not in text,
    }


def main() -> int:
    results: dict = {
        "base_url": BASE_URL,
        "router_model_for_flyer": None,
        "frontend_image_keys": [],
        "backend_kie_provider_keys": [],
        "registry_alignment": {},
        "broken_key_matches": {},
        "generate_media_results": [],
        "old_key_guard_results": [],
        "overall_pass": False,
    }

    router_model = run_node_router()
    image_keys = frontend_image_keys()
    backend_keys = backend_kie_provider_keys()
    broken_matches = frontend_broken_key_matches()

    results["router_model_for_flyer"] = router_model
    results["frontend_image_keys"] = image_keys
    results["backend_kie_provider_keys"] = sorted(backend_keys)
    results["registry_alignment"] = {
        key: (key in backend_keys or key == "nano-banana") for key in image_keys
    }
    results["broken_key_matches"] = broken_matches

    with requests.Session() as session:
        conv_id = setup_conversation(session)
        results["conversation_id"] = conv_id

        # First test the exact smart-router output from the reported flyer flow.
        results["generate_media_results"].append(
            post_generate_media(session, conv_id, router_model)
        )

        # Then enumerate all current frontend image keys. Skip duplicate router key.
        for key in image_keys:
            if key == router_model:
                continue
            results["generate_media_results"].append(
                post_generate_media(session, conv_id, key)
            )

        # Backend guard should still reject obsolete keys; the fix must be frontend alignment.
        for key in ["seedream-5-lite", "flux-pro"]:
            results["old_key_guard_results"].append(
                post_generate_media(session, conv_id, key)
            )

    required_known = {
        "seedream-5",
        "flux-kontext-pro",
        "flux-kontext-max",
        "imagen-4-ultra",
        "gpt-image-1-5",
        "ideogram-3",
        "nano-banana",
    }
    all_required_present = required_known.issubset(set(image_keys))
    no_broken_frontend_keys = all(not paths for paths in broken_matches.values())
    registry_aligned = all(results["registry_alignment"].values())
    generate_no_unknown = all(r["passed_unknown_guard"] for r in results["generate_media_results"])
    router_ok = router_model == "seedream-5"
    old_guard_ok = all(
        r["status_code"] == 400 and r["contains_unknown_model"]
        for r in results["old_key_guard_results"]
    )

    results["overall_pass"] = all(
        [
            router_ok,
            all_required_present,
            no_broken_frontend_keys,
            registry_aligned,
            generate_no_unknown,
            old_guard_ok,
        ]
    )
    results["assertions"] = {
        "router_ok_seedream_5": router_ok,
        "all_required_frontend_keys_present": all_required_present,
        "no_obsolete_frontend_key_matches": no_broken_frontend_keys,
        "frontend_keys_registered_or_special_case": registry_aligned,
        "generate_media_current_keys_no_unknown_model": generate_no_unknown,
        "obsolete_keys_still_rejected_as_unknown": old_guard_ok,
    }

    out_path = ROOT / "test_reports/kie_model_registry_alignment_results.json"
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(json.dumps(results, indent=2, default=str))
    return 0 if results["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())