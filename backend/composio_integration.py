"""Composio integration — managed agent-tool platform.

Why this module exists
----------------------
We wrap Composio's Python SDK behind a thin interface so that, if we ever
swap providers (Pipedream, Nango, custom MCP servers), only this file
changes. Routes and the agent loop import named helpers from here, never
the raw `composio` package.

Architecture
- Each (org_id, user_id) is mapped to a Composio `user_id` of the form
  `"<org_id>:<user_id>"`. Different orgs see different connection sets
  for the same Foundry user — mirrors how Memory isolates per org.
- Auth configs are CREATED ONCE per toolkit at startup (idempotent —
  we skip if one already exists for that toolkit). Auth config IDs are
  cached in Mongo so re-creation doesn't churn.
- Composio's "managed auth" means we don't have to register OAuth apps
  with Google / Slack / Notion / etc. Composio's own OAuth credentials
  do the heavy lifting; we just receive the connected_account.
- Per-call telemetry lands in `llm_calls` with `kind_label =
  "composio:<toolkit>:<tool_slug>"` so SpendDashboard rolls it up
  automatically with the rest of the cost picture.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Toolkits Foundry exposes in v1. Extend this list to onboard more apps.
# Order matters — surfaced top-to-bottom in the Connections UI.
SUPPORTED_TOOLKITS: List[Dict[str, str]] = [
    {"slug": "gmail", "name": "Gmail", "description": "Send and read email"},
    {"slug": "googlecalendar", "name": "Google Calendar", "description": "Manage events and invites"},
    {"slug": "slack", "name": "Slack", "description": "Post messages, search channels"},
    {"slug": "notion", "name": "Notion", "description": "Read and write docs, databases"},
    {"slug": "hubspot", "name": "HubSpot", "description": "Manage contacts, deals, companies"},
]
_TOOLKIT_META = {t["slug"]: t for t in SUPPORTED_TOOLKITS}

_COMPOSIO_API_KEY = os.environ.get("COMPOSIO_API_KEY", "").strip()


def is_available() -> bool:
    """Composio is wired iff the API key is set in env."""
    return bool(_COMPOSIO_API_KEY)


# ── Client singleton ─────────────────────────────────────────────────────────
_client = None


def _get_client():
    """Lazy singleton — avoids importing `composio` at module load when the
    key isn't configured (keeps Foundry boot fast on dev envs without it).
    """
    global _client
    if _client is not None:
        return _client
    if not is_available():
        raise RuntimeError("COMPOSIO_API_KEY is not set in environment.")
    from composio import Composio  # local import — heavy SDK
    _client = Composio(api_key=_COMPOSIO_API_KEY)
    return _client


# ── User ID mapping ─────────────────────────────────────────────────────────
def composio_user_id(org_id: str, user_id: str) -> str:
    """Build the per-org Composio user_id.

    Different orgs ⇒ different connection sets for the same Foundry user.
    This keeps OAuth tokens fully isolated across workspaces.
    """
    return f"{org_id}:{user_id}"


# ── Auth-config bootstrap ───────────────────────────────────────────────────
async def ensure_auth_configs(db) -> Dict[str, str]:
    """Create one composio-managed auth config per supported toolkit,
    keyed by toolkit slug. Idempotent: skips toolkits we've already seen.

    Returns the slug → auth_config_id map. Stored in the `composio_meta`
    collection so we don't re-create on every boot.
    """
    if not is_available():
        return {}
    existing = {
        row["toolkit"]: row["auth_config_id"]
        async for row in db.composio_auth_configs.find({}, {"_id": 0})
    }
    if all(t["slug"] in existing for t in SUPPORTED_TOOLKITS):
        return existing
    client = _get_client()
    out = dict(existing)
    for tk in SUPPORTED_TOOLKITS:
        slug = tk["slug"]
        if slug in out:
            continue
        try:
            ac = client.auth_configs.create(
                toolkit=slug,
                options={
                    "name": f"Foundry {tk['name']}",
                    "type": "use_composio_managed_auth",
                },
            )
            out[slug] = ac.id
            await db.composio_auth_configs.update_one(
                {"toolkit": slug},
                {"$set": {
                    "toolkit": slug,
                    "auth_config_id": ac.id,
                    "name": tk["name"],
                    "created_at": datetime.now(timezone.utc),
                }},
                upsert=True,
            )
            logger.info("Composio: bootstrapped auth_config for %s → %s", slug, ac.id)
        except Exception as exc:
            # One toolkit failing shouldn't prevent the rest. We'll
            # retry on next boot.
            logger.warning("Composio: auth_config create failed for %s: %s", slug, exc)
    return out


async def get_auth_config_id(db, toolkit: str) -> Optional[str]:
    row = await db.composio_auth_configs.find_one({"toolkit": toolkit}, {"_id": 0})
    return (row or {}).get("auth_config_id")


# ── Connections (per user, per toolkit) ─────────────────────────────────────
async def initiate_connection(
    db,
    org_id: str,
    user_id: str,
    toolkit: str,
    callback_url: str,
) -> Dict[str, Any]:
    """Kick off OAuth for a toolkit. Returns the Composio-hosted redirect URL.

    The user clicks through, authorises on the third-party site, and lands
    back at `callback_url`. We don't need to do anything special on the
    callback page — Composio persists the connection server-side. The
    frontend just re-fetches `list_connections` to refresh the UI.
    """
    if toolkit not in _TOOLKIT_META:
        raise ValueError(f"Toolkit '{toolkit}' is not supported by Foundry")
    ac_id = await get_auth_config_id(db, toolkit)
    if not ac_id:
        raise RuntimeError(f"No auth_config bootstrapped for {toolkit} yet.")
    client = _get_client()
    cu = composio_user_id(org_id, user_id)
    req = client.connected_accounts.link(
        user_id=cu,
        auth_config_id=ac_id,
        callback_url=callback_url,
        allow_multiple=False,
    )
    # Snapshot the pending connection so the UI can show "Connecting…"
    # immediately and detect orphans later.
    await db.composio_connections.update_one(
        {"connection_id": req.id},
        {"$set": {
            "connection_id": req.id,
            "org_id": org_id,
            "user_id": user_id,
            "composio_user_id": cu,
            "toolkit": toolkit,
            "auth_config_id": ac_id,
            "status": getattr(req, "status", "INITIATED"),
            "initiated_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
    return {
        "connection_id": req.id,
        "redirect_url": req.redirect_url,
        "status": getattr(req, "status", "INITIATED"),
    }


async def refresh_connection_status(db, connection_id: str) -> Dict[str, Any]:
    """Pull the latest status from Composio and persist it locally.

    Called when the user returns to our callback page so the UI can flip
    the toolkit card from "Connecting…" to "Connected" without waiting
    for the next page reload.
    """
    client = _get_client()
    acct = client.connected_accounts.get(nanoid=connection_id)
    status = getattr(acct, "status", None)
    updated = {
        "status": status,
        "last_synced_at": datetime.now(timezone.utc),
    }
    if status == "ACTIVE":
        updated["activated_at"] = datetime.now(timezone.utc)
    # Account label (e.g. the user's actual gmail address) for the UI.
    label = (
        getattr(acct, "params", None)
        and getattr(acct.params, "labels", None)
    )
    if label:
        updated["account_label"] = ",".join(label) if isinstance(label, list) else str(label)
    await db.composio_connections.update_one(
        {"connection_id": connection_id},
        {"$set": updated},
    )
    return {"connection_id": connection_id, **updated}


async def list_connections(db, org_id: str, user_id: str) -> List[Dict[str, Any]]:
    """List every connection for (org_id, user_id) — one row per toolkit."""
    cu = composio_user_id(org_id, user_id)
    rows = await db.composio_connections.find(
        {"composio_user_id": cu},
        {"_id": 0},
    ).sort("initiated_at", -1).to_list(200)
    # Hydrate with the human-readable name from SUPPORTED_TOOLKITS so the
    # UI doesn't need a second lookup.
    for r in rows:
        meta = _TOOLKIT_META.get(r.get("toolkit"))
        if meta:
            r["toolkit_name"] = meta["name"]
            r["toolkit_description"] = meta["description"]
        # Normalise dates to ISO so JSON serialisation is clean.
        for k in ("initiated_at", "activated_at", "last_synced_at"):
            v = r.get(k)
            if isinstance(v, datetime):
                r[k] = v.isoformat()
    return rows


async def disconnect_connection(db, org_id: str, user_id: str, connection_id: str) -> None:
    """Disconnect a Composio connection and remove the local row.

    Auth-gates against the (org_id, user_id) so members of other orgs
    cannot revoke each other's connections, even if they guess the id.
    """
    cu = composio_user_id(org_id, user_id)
    row = await db.composio_connections.find_one(
        {"connection_id": connection_id, "composio_user_id": cu},
    )
    if not row:
        raise ValueError("Connection not found for this user")
    client = _get_client()
    try:
        client.connected_accounts.delete(nanoid=connection_id)
    except Exception as exc:
        # Composio may have already revoked / never finished — proceed
        # to clear our local row anyway.
        logger.warning("Composio: remote disconnect failed (%s) — clearing local row", exc)
    await db.composio_connections.delete_one({"connection_id": connection_id})


# ── Tool fetch + execute (for the agent loop) ───────────────────────────────
async def get_tools_for_user(
    db,
    org_id: str,
    user_id: str,
    toolkit_filter: Optional[List[str]] = None,
    limit: int = 30,
) -> List[Dict[str, Any]]:
    """Return OpenAI-formatted tools for every toolkit this user has
    actively connected. If `toolkit_filter` is provided we narrow further.

    The returned list is ready to drop into `chat.completions.create(tools=...)`.
    """
    if not is_available():
        return []
    cu = composio_user_id(org_id, user_id)
    active = await db.composio_connections.find(
        {"composio_user_id": cu, "status": "ACTIVE"},
        {"_id": 0, "toolkit": 1},
    ).to_list(50)
    if not active:
        return []
    connected_toolkits = list({r["toolkit"] for r in active})
    if toolkit_filter:
        connected_toolkits = [t for t in connected_toolkits if t in toolkit_filter]
        if not connected_toolkits:
            return []
    client = _get_client()
    try:
        tools = client.tools.get(
            user_id=cu,
            toolkits=connected_toolkits,
            limit=limit,
        )
    except Exception as exc:
        logger.error("Composio: tools.get failed for %s: %s", cu, exc)
        return []
    # The SDK returns a list of dicts shaped for OpenAI's tool-calling
    # API. We just pass them through unchanged.
    return list(tools or [])


async def execute_tool(
    db,
    org_id: str,
    user_id: str,
    tool_slug: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Execute a single Composio tool call on behalf of a user.

    Records telemetry to `llm_calls` so SpendDashboard surfaces the call
    under `kind_label = "composio:<toolkit>:<slug>"`.
    """
    if not is_available():
        raise RuntimeError("Composio is not configured")
    cu = composio_user_id(org_id, user_id)
    client = _get_client()
    # Composio v3 requires an explicit toolkit version for *manual* execute
    # calls (the auto path inside .tools.get() handles versioning itself).
    # Look it up once per slug and reuse — version pins are stable.
    version: Optional[str] = None
    try:
        meta = client.tools.get_raw_composio_tool_by_slug(slug=tool_slug)
        version = getattr(meta, "version", None)
    except Exception as exc:
        logger.warning("Composio: version lookup failed for %s: %s", tool_slug, exc)
    started = datetime.now(timezone.utc)
    success = True
    err_msg: Optional[str] = None
    response: Any = None
    try:
        kwargs: Dict[str, Any] = {
            "slug": tool_slug,
            "arguments": arguments or {},
            "user_id": cu,
        }
        if version:
            kwargs["version"] = version
        result = client.tools.execute(**kwargs)
        # SDK returns a Pydantic-ish object; pull the dict form.
        if hasattr(result, "model_dump"):
            response = result.model_dump()
        elif hasattr(result, "to_dict"):
            response = result.to_dict()
        else:
            response = result
    except Exception as exc:
        success = False
        err_msg = str(exc)[:500]
        response = {"error": err_msg}
        logger.error("Composio: tools.execute failed for %s: %s", tool_slug, exc)
    finally:
        ended = datetime.now(timezone.utc)
        toolkit = tool_slug.split("_", 1)[0].lower() if "_" in tool_slug else "unknown"
        # Mirror the existing llm_calls telemetry shape so SpendDashboard
        # rolls it up automatically. cost_usd is set per-call estimate
        # ($0.00 by default — Composio bills us at the plan level, not
        # per-call, so we attribute zero unit cost here. The Spend page
        # already surfaces call volume independent of cost.)
        await db.llm_calls.insert_one({
            "org_id": org_id,
            "user_id": user_id,
            "kind_label": f"composio:{toolkit}:{tool_slug}",
            "model": "composio",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_usd": 0.0,
            "success": success,
            "duration_ms": int((ended - started).total_seconds() * 1000),
            "created_at": ended,
            "error": err_msg,
        })
    return {
        "success": success,
        "response": response,
        "error": err_msg,
    }


# ── Public catalogue ────────────────────────────────────────────────────────
def list_supported_toolkits() -> List[Dict[str, str]]:
    """Return the static v1 toolkit list for the UI's catalogue panel."""
    return list(SUPPORTED_TOOLKITS)
