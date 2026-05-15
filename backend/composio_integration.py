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

# Toolkits Foundry exposes "above the fold" in the Connections UI. Order
# matters — these render first as cards. Picked to cover the 80%
# productivity/CRM/eng/marketing use cases for an agency-style B2B tool.
# Anything else lives in the Composio catalog and the user opens it via
# "Browse all".
FEATURED_TOOLKITS: List[Dict[str, str]] = [
    {"slug": "gmail", "name": "Gmail", "description": "Send and read email"},
    {"slug": "googlecalendar", "name": "Google Calendar", "description": "Manage events and invites"},
    {"slug": "slack", "name": "Slack", "description": "Post messages, search channels"},
    {"slug": "notion", "name": "Notion", "description": "Read and write docs, databases"},
    {"slug": "hubspot", "name": "HubSpot", "description": "Manage contacts, deals, companies"},
    {"slug": "linear", "name": "Linear", "description": "Track issues and sprints"},
    {"slug": "jira", "name": "Jira", "description": "Manage tickets across projects"},
    {"slug": "asana", "name": "Asana", "description": "Project & task management"},
    {"slug": "googledrive", "name": "Google Drive", "description": "Store and share files"},
    {"slug": "dropbox", "name": "Dropbox", "description": "Cloud file storage"},
    {"slug": "discord", "name": "Discord", "description": "Post to servers and channels"},
    {"slug": "salesforce", "name": "Salesforce", "description": "CRM for enterprise sales"},
    {"slug": "mailchimp", "name": "Mailchimp", "description": "Email marketing campaigns"},
    {"slug": "stripe", "name": "Stripe", "description": "Read payments and customers"},
    {"slug": "calendly", "name": "Calendly", "description": "Schedule meetings"},
    {"slug": "zoom", "name": "Zoom", "description": "Schedule and join meetings"},
    {"slug": "twitter", "name": "Twitter / X", "description": "Post and search tweets"},
    {"slug": "linkedin", "name": "LinkedIn", "description": "Post and manage profile"},
    {"slug": "airtable", "name": "Airtable", "description": "Database tables and views"},
    {"slug": "gitlab", "name": "GitLab", "description": "Repos, MRs, pipelines"},
]
# Back-compat: the old name is still referenced by routes. Keep it as an
# alias that mirrors the featured list.
SUPPORTED_TOOLKITS = FEATURED_TOOLKITS
_TOOLKIT_META = {t["slug"]: t for t in FEATURED_TOOLKITS}

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
    """Create one composio-managed auth config per FEATURED toolkit, keyed
    by toolkit slug. Idempotent: skips toolkits we've already seen.

    Returns the slug → auth_config_id map. Stored in the `composio_meta`
    collection so we don't re-create on every boot. Non-featured toolkits
    are bootstrapped lazily on first user Connect click via
    `ensure_auth_config_for`.
    """
    if not is_available():
        return {}
    existing = {
        row["toolkit"]: row["auth_config_id"]
        async for row in db.composio_auth_configs.find({}, {"_id": 0})
    }
    if all(t["slug"] in existing for t in FEATURED_TOOLKITS):
        return existing
    out = dict(existing)
    for tk in FEATURED_TOOLKITS:
        slug = tk["slug"]
        if slug in out:
            continue
        try:
            ac_id = await _create_one_auth_config(db, slug, tk["name"])
            if ac_id:
                out[slug] = ac_id
        except Exception as exc:
            # One toolkit failing shouldn't prevent the rest. We'll
            # retry on next boot.
            logger.warning("Composio: auth_config create failed for %s: %s", slug, exc)
    return out


async def _create_one_auth_config(db, slug: str, display_name: str) -> Optional[str]:
    """Create exactly one composio-managed auth config and persist it."""
    client = _get_client()
    try:
        ac = client.auth_configs.create(
            toolkit=slug,
            options={
                "name": f"Foundry {display_name}",
                "type": "use_composio_managed_auth",
            },
        )
    except Exception as exc:
        logger.warning("Composio: auth_config create failed for %s: %s", slug, exc)
        return None
    await db.composio_auth_configs.update_one(
        {"toolkit": slug},
        {"$set": {
            "toolkit": slug,
            "auth_config_id": ac.id,
            "name": display_name,
            "created_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
    logger.info("Composio: bootstrapped auth_config for %s → %s", slug, ac.id)
    return ac.id


async def ensure_auth_config_for(db, toolkit: str) -> Optional[str]:
    """Lazy bootstrap: return existing auth_config_id for `toolkit`, or
    create one (composio-managed) if absent. Used when a user clicks
    Connect on a non-featured toolkit from the "Browse all" dialog.
    """
    row = await db.composio_auth_configs.find_one({"toolkit": toolkit}, {"_id": 0})
    if row and row.get("auth_config_id"):
        return row["auth_config_id"]
    # Look up the human name from Composio's catalogue so the auth_config
    # gets a readable label in their dashboard.
    display = toolkit
    try:
        info = await _resolve_toolkit_metadata(toolkit)
        if info:
            display = info.get("name") or toolkit
    except Exception:
        pass
    return await _create_one_auth_config(db, toolkit, display)


# Cache of {slug: {slug,name,description,logo}} for the full Composio
# catalog. Refreshed lazily; the catalog rarely changes so we just
# fetch once per process boot.
_CATALOG_CACHE: Dict[str, Dict[str, str]] = {}


async def list_all_toolkits() -> List[Dict[str, str]]:
    """Return the full Composio toolkit catalog (~250 entries) for the
    "Browse all" picker. Cached in-process after the first call.
    """
    if _CATALOG_CACHE:
        return list(_CATALOG_CACHE.values())
    if not is_available():
        return []
    client = _get_client()
    out: List[Dict[str, str]] = []
    cursor: Optional[str] = None
    try:
        while True:
            kwargs: Dict[str, Any] = {"limit": 100}
            if cursor:
                kwargs["cursor"] = cursor
            page = client.toolkits.list(**kwargs)
            items = getattr(page, "items", None) or []
            for t in items:
                slug = getattr(t, "slug", None)
                if not slug:
                    continue
                out.append({
                    "slug": slug,
                    "name": getattr(t, "name", slug) or slug,
                    "description": (
                        getattr(t, "meta", None)
                        and getattr(t.meta, "description", "")
                    ) or "",
                    "logo": (
                        getattr(t, "meta", None)
                        and getattr(t.meta, "logo", "")
                    ) or "",
                })
            cursor = getattr(page, "next_cursor", None)
            if not cursor or len(out) > 500:  # hard cap
                break
    except Exception as exc:
        logger.warning("Composio: toolkit catalog fetch failed: %s", exc)
        return []
    for t in out:
        _CATALOG_CACHE[t["slug"]] = t
    return out


async def _resolve_toolkit_metadata(slug: str) -> Optional[Dict[str, str]]:
    """Lookup one toolkit's display info from the cached catalog (or fetch)."""
    if slug in _CATALOG_CACHE:
        return _CATALOG_CACHE[slug]
    await list_all_toolkits()
    return _CATALOG_CACHE.get(slug)


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
    nickname: Optional[str] = None,
) -> Dict[str, Any]:
    """Kick off OAuth for a toolkit. Returns the Composio-hosted redirect URL.

    Multi-account: `allow_multiple=True` so the same user can connect
    Gmail #1 (Work) and Gmail #2 (Personal) under the same Foundry user.
    `nickname` is shown in the UI to distinguish accounts at a glance.
    Non-featured toolkits are auto-bootstrapped on first use.
    """
    # Lazy auth-config bootstrap for non-featured toolkits.
    ac_id = await get_auth_config_id(db, toolkit)
    if not ac_id:
        ac_id = await ensure_auth_config_for(db, toolkit)
    if not ac_id:
        raise ValueError(f"Toolkit '{toolkit}' is not available")
    client = _get_client()
    cu = composio_user_id(org_id, user_id)
    req = client.connected_accounts.link(
        user_id=cu,
        auth_config_id=ac_id,
        callback_url=callback_url,
        allow_multiple=True,
        alias=(nickname or None),
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
            "nickname": (nickname or "").strip() or None,
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

    Side-effect: when a connection transitions to ACTIVE for the first
    time, we auto-enable Composio tools for every agent in this org
    whose division matches the toolkit (e.g. connect Gmail → enable
    Sales / Support / HR agents). Agents the user has previously
    set explicitly (ON or OFF) are left alone.
    """
    client = _get_client()
    acct = client.connected_accounts.get(nanoid=connection_id)
    status = getattr(acct, "status", None)
    existing = await db.composio_connections.find_one(
        {"connection_id": connection_id}, {"_id": 0},
    )
    was_active = (existing or {}).get("status") == "ACTIVE"
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
    result = {"connection_id": connection_id, **updated}
    # First-time activation: auto-enable matching agents.
    if status == "ACTIVE" and not was_active and existing:
        try:
            applied = await _auto_enable_agents_for_toolkit(
                db,
                org_id=existing["org_id"],
                toolkit=existing["toolkit"],
                actor_user_id=existing.get("user_id"),
            )
            result["auto_enabled_agents"] = applied
        except Exception as exc:
            logger.warning("Composio: auto-enable on activation failed: %s", exc)
    return result


async def _auto_enable_agents_for_toolkit(
    db,
    *,
    org_id: str,
    toolkit: str,
    actor_user_id: Optional[str],
) -> int:
    """When a toolkit goes ACTIVE for the first time in an org, flip
    `tools_enabled=true` for every agent in this org whose division
    maps to this toolkit AND that doesn't already have an explicit
    pref (ON or OFF). Respects user overrides — once anyone has touched
    the agent's toggle, we never re-flip it automatically.

    Returns the number of agents we just enabled. Cross-imports are
    confined here so the AGENTS catalog stays owned by server.py.
    """
    from server import AGENTS  # local import — avoids circular at module load
    divisions = divisions_for_toolkit(toolkit)
    if not divisions:
        return 0
    div_set = set(divisions)
    candidate_ids: List[str] = []
    for aid, meta in AGENTS.items():
        if meta.get("org_id") and meta.get("org_id") != org_id:
            continue
        dk = (meta.get("division_key") or "").lower()
        if dk in div_set:
            candidate_ids.append(aid)
    if not candidate_ids:
        return 0
    # Filter out agents the user has already set explicitly.
    existing_rows = await db.agent_tool_prefs.find(
        {"org_id": org_id, "agent_id": {"$in": candidate_ids}},
        {"_id": 0, "agent_id": 1},
    ).to_list(2000)
    already_set = {r["agent_id"] for r in existing_rows}
    to_enable = [a for a in candidate_ids if a not in already_set]
    if not to_enable:
        return 0
    now = datetime.now(timezone.utc)
    for aid in to_enable:
        await db.agent_tool_prefs.update_one(
            {"org_id": org_id, "agent_id": aid},
            {"$set": {
                "org_id": org_id,
                "agent_id": aid,
                "tools_enabled": True,
                "updated_at": now,
                "updated_by": actor_user_id,
                "auto_enabled_by_toolkit": toolkit,
            }},
            upsert=True,
        )
    logger.info(
        "Composio: auto-enabled %d agents in org %s for toolkit %s",
        len(to_enable), org_id, toolkit,
    )
    return len(to_enable)


async def list_connections(db, org_id: str, user_id: str) -> List[Dict[str, Any]]:
    """List every connection for (org_id, user_id). With multi-account
    enabled there can be many rows per toolkit."""
    cu = composio_user_id(org_id, user_id)
    rows = await db.composio_connections.find(
        {"composio_user_id": cu},
        {"_id": 0},
    ).sort("initiated_at", -1).to_list(500)
    # Hydrate with the human-readable name. Featured toolkits come from
    # our static list; non-featured fall back to the cached catalog,
    # finally to the slug itself if Composio's catalog isn't available.
    for r in rows:
        slug = r.get("toolkit") or ""
        meta = _TOOLKIT_META.get(slug)
        if meta:
            r["toolkit_name"] = meta["name"]
            r["toolkit_description"] = meta["description"]
        else:
            cat = _CATALOG_CACHE.get(slug)
            r["toolkit_name"] = (cat or {}).get("name") or slug
            r["toolkit_description"] = (cat or {}).get("description") or ""
        # Normalise dates to ISO so JSON serialisation is clean.
        for k in ("initiated_at", "activated_at", "last_synced_at"):
            v = r.get(k)
            if isinstance(v, datetime):
                r[k] = v.isoformat()
    return rows


async def set_connection_nickname(
    db, org_id: str, user_id: str, connection_id: str, nickname: str,
) -> Dict[str, Any]:
    """Rename a connection's local nickname. Auth-gated to (org, user)."""
    cu = composio_user_id(org_id, user_id)
    nickname = (nickname or "").strip()[:60]
    res = await db.composio_connections.update_one(
        {"connection_id": connection_id, "composio_user_id": cu},
        {"$set": {"nickname": nickname or None}},
    )
    if res.matched_count == 0:
        raise ValueError("Connection not found for this user")
    return {"connection_id": connection_id, "nickname": nickname or None}


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
def list_featured_toolkits() -> List[Dict[str, str]]:
    """Return the static featured toolkit list for the Connections panel."""
    return list(FEATURED_TOOLKITS)


# Back-compat alias used by older route code.
def list_supported_toolkits() -> List[Dict[str, str]]:
    return list_featured_toolkits()


# Division → recommended toolkit slugs. Drives the "smart defaults" UX
# in AgentToolPrefsPanel: each agent's `division_key` resolves to a
# short list of toolkits that make sense for that role. Anything not
# listed here falls back to no recommendations.
DIVISION_TOOLKIT_DEFAULTS: Dict[str, List[str]] = {
    "engineering": ["github", "linear", "jira", "gitlab", "slack"],
    "marketing":   ["mailchimp", "twitter", "linkedin", "hubspot", "googledrive"],
    "sales":       ["hubspot", "salesforce", "gmail", "googlecalendar", "calendly"],
    "operations":  ["googlecalendar", "slack", "notion", "googledrive", "asana"],
    "creative":    ["notion", "googledrive", "dropbox", "twitter"],
    "content":     ["notion", "googledrive", "twitter", "linkedin"],
    "product":     ["linear", "notion", "jira", "asana"],
    "design":      ["notion", "googledrive", "dropbox"],
    "support":     ["slack", "gmail", "notion", "hubspot"],
    "finance":     ["stripe", "googledrive", "notion"],
    "hr":          ["gmail", "googlecalendar", "notion", "slack"],
    "legal":       ["googledrive", "notion", "gmail"],
}


def recommended_toolkits_for_division(division_key: str) -> List[str]:
    """Return the slug list for a division, or [] if unknown."""
    if not division_key:
        return []
    return list(DIVISION_TOOLKIT_DEFAULTS.get(division_key.lower(), []))


# Reverse index: toolkit slug → divisions it makes sense for. Used by the
# auto-enable flow when a new connection goes ACTIVE — we sweep all
# agents in those divisions and flip their tools_enabled to True (unless
# the user has previously set it explicitly).
_TOOLKIT_TO_DIVISIONS: Dict[str, List[str]] = {}
for _div, _slugs in DIVISION_TOOLKIT_DEFAULTS.items():
    for _slug in _slugs:
        _TOOLKIT_TO_DIVISIONS.setdefault(_slug, []).append(_div)


def divisions_for_toolkit(toolkit: str) -> List[str]:
    return list(_TOOLKIT_TO_DIVISIONS.get((toolkit or "").lower(), []))
