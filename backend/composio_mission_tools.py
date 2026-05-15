"""Composio tools wrapped for the Mission loop.

Missions use a string-name-based tool catalogue (see `missions.py:Tool`),
not OpenAI's native tool_calls. So for each (toolkit, tool) pair the
user has connected we synthesise one `Tool` whose handler shells out to
`composio.execute_tool`. The mission LLM sees these alongside its
existing search/browser/artifact tools and picks one per turn.

Budget rails:
- Hard cap of MAX_TOOL_CALLS_PER_MISSION total Composio calls per mission
- Soft per-platform cap of MAX_PER_PLATFORM (resets per mission)
- Per-call telemetry lands in `llm_calls` with
  kind_label="mission_tool:composio:<toolkit>:<slug>" so SpendDashboard
  can split mission tool cost out of normal Composio spend.

Tool name shape:
- "gmail.send_email"  — short, LLM-friendly form
- Internally maps back to the Composio slug GMAIL_SEND_EMAIL.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import composio_integration as composio

logger = logging.getLogger(__name__)

# Per-mission ceiling. Missions chain dozens of steps, so 3/turn (chat) is
# too tight; 20 total + 5-per-platform keeps a runaway loop bounded
# without blocking realistic multi-step workflows.
MAX_TOOL_CALLS_PER_MISSION = 20
MAX_PER_PLATFORM = 5

# Per-mission counters, keyed by mission_id. Cleared at mission start by
# `reset_budget`. We deliberately use a process-global dict instead of
# a Mongo doc because the counter is purely intra-run guidance — if the
# process crashes mid-mission, the partial count is moot anyway.
_BUDGETS: Dict[str, Dict[str, int]] = {}


def reset_budget(mission_id: str) -> None:
    _BUDGETS[mission_id] = {"_total": 0}


def _bump_budget(mission_id: str, toolkit: str) -> Dict[str, int]:
    b = _BUDGETS.setdefault(mission_id, {"_total": 0})
    b["_total"] = b.get("_total", 0) + 1
    b[toolkit] = b.get(toolkit, 0) + 1
    return b


def _is_over_budget(mission_id: str, toolkit: str) -> Optional[str]:
    b = _BUDGETS.get(mission_id) or {}
    if b.get("_total", 0) >= MAX_TOOL_CALLS_PER_MISSION:
        return f"Mission tool-call budget reached ({MAX_TOOL_CALLS_PER_MISSION} total)."
    if b.get(toolkit, 0) >= MAX_PER_PLATFORM:
        return f"Per-platform budget reached for {toolkit} ({MAX_PER_PLATFORM} calls)."
    return None


def _short_name(slug: str) -> str:
    """GMAIL_SEND_EMAIL → gmail.send_email"""
    parts = slug.split("_", 1)
    if len(parts) != 2:
        return slug.lower()
    return f"{parts[0].lower()}.{parts[1].lower()}"


def _slug_for(short_name: str) -> str:
    """gmail.send_email → GMAIL_SEND_EMAIL"""
    parts = short_name.split(".", 1)
    if len(parts) != 2:
        return short_name.upper()
    return f"{parts[0].upper()}_{parts[1].upper()}"


def _summarise(arg_schema: Dict[str, Any]) -> str:
    """Build a 1-line JSON args hint for the LLM from the input schema."""
    props = (arg_schema or {}).get("properties", {}) or {}
    req = set((arg_schema or {}).get("required", []) or [])
    if not props:
        return "{}"
    sample: Dict[str, str] = {}
    for k in list(props.keys())[:5]:
        if k in req or len(sample) < 3:
            sample[k] = f"<{props[k].get('type','any')}>"
    return json.dumps(sample)


async def build_composio_mission_tools(
    db,
    *,
    org_id: str,
    user_id: str,
    mission_id: str,
    lead_agent_id: Optional[str] = None,
    Tool=None,  # noqa: N803 — caller passes the missions.Tool class
) -> List[Any]:
    """Return a list of `Tool` instances exposing every Composio action
    the user has actively connected. Empty list when:
        - COMPOSIO_API_KEY isn't configured
        - the user has zero ACTIVE connections
        - the mission's lead agent has tools_enabled=False
    """
    if Tool is None:
        return []
    if not composio.is_available():
        return []
    # Gate on the lead agent's tools_enabled toggle so users keep the
    # per-agent control they already see in AgentToolPrefsPanel. Default
    # is ON (missing row or any non-False value); only an explicit
    # `tools_enabled=False` disables Composio tools for missions led by
    # this agent.
    if lead_agent_id:
        row = await db.agent_tool_prefs.find_one(
            {"org_id": org_id, "agent_id": lead_agent_id},
            {"_id": 0, "tools_enabled": 1},
        )
        if row is not None and row.get("tools_enabled") is False:
            return []
    # Pull the OpenAI-formatted tools list from Composio (they include the
    # input schema we need for our args_hint). Limit to 30 — beyond that
    # the LLM gets confused choosing among too many options.
    tools = await composio.get_tools_for_user(db, org_id, user_id, limit=30)
    if not tools:
        return []

    reset_budget(mission_id)
    out: List[Any] = []
    for spec in tools:
        # `tools.get(...)` returns OpenAI-shape entries:
        # {type:"function", function:{name, description, parameters}}
        fn = (spec or {}).get("function") or {}
        slug = fn.get("name") or ""
        if not slug:
            continue
        toolkit = slug.split("_", 1)[0].lower()
        short_name = _short_name(slug)
        desc = (fn.get("description") or "").strip()
        if len(desc) > 240:
            desc = desc[:237] + "…"
        # Prefix every tool description with its toolkit so the LLM
        # naturally groups them when summarising the catalog.
        desc = f"[{toolkit}] {desc}"
        args_hint = _summarise(fn.get("parameters", {}))

        def _make_handler(_slug: str, _toolkit: str):
            async def _handler(ctx: Dict[str, Any], args: Dict[str, Any]) -> Any:
                mid = ctx.get("mission_id") or mission_id
                gate = _is_over_budget(mid, _toolkit)
                if gate:
                    return {"error": gate}
                # Bump the budget BEFORE executing — even a failed call
                # costs Composio quota AND should backpressure runaway
                # loops (e.g. expired auth that would otherwise be
                # retried forever).
                _bump_budget(mid, _toolkit)
                res = await composio.execute_tool(
                    db,
                    ctx.get("org_id") or org_id,
                    ctx.get("user_id") or user_id,
                    _slug,
                    args or {},
                )
                # Re-tag the most recent telemetry row from this user
                # for this tool as mission-sourced. Use find_one_and_update
                # because update_one(sort=...) is pymongo 4.7+ and we're
                # on 4.5 — the sort= kwarg silently disappears there.
                try:
                    await db.llm_calls.find_one_and_update(
                        {
                            "org_id": ctx.get("org_id") or org_id,
                            "user_id": ctx.get("user_id") or user_id,
                            "kind_label": f"composio:{_toolkit}:{_slug}",
                        },
                        {"$set": {
                            "mission_id": mid,
                            "kind_label": f"mission_tool:composio:{_toolkit}:{_slug}",
                        }},
                        sort=[("created_at", -1)],
                    )
                except Exception as exc:
                    # Telemetry is nice-to-have; never let it break the
                    # mission step.
                    logger.warning("mission telemetry tag failed: %s", exc)
                return {
                    "ok": bool(res.get("success")),
                    "response": res.get("response"),
                    "error": res.get("error"),
                }
            return _handler

        out.append(Tool(
            name=short_name,
            description=desc,
            args_hint=args_hint,
            handler=_make_handler(slug, toolkit),
            side_effect=True,
        ))
    logger.info(
        "Composio mission tools: org=%s user=%s n=%d (cap=%d total / %d per-platform)",
        org_id, user_id, len(out), MAX_TOOL_CALLS_PER_MISSION, MAX_PER_PLATFORM,
    )
    return out


def remaining_budget(mission_id: str) -> Dict[str, int]:
    """For diagnostics / future UI surface."""
    return dict(_BUDGETS.get(mission_id) or {})
