"""Composio tool-calling subturn — Phase 2 of the Composio integration.

`LlmChat` from emergentintegrations does NOT expose a `tools=` parameter,
so when an agent has Composio tools enabled we bypass it and call OpenAI
directly with `chat.completions.create(..., tools=[...])`. The result is
the SAME shape (text reply) but we get native function calling.

Budget: max 3 tool calls per turn (configurable). After 3 the model is
told to wrap up.

Persistence: every tool_call + tool_result is persisted as a
`sender_type="tool"` message inside the conversation so the timeline
captures exactly what happened. The frontend renders these inline as
"Agent is calling Gmail.send_email…" indicators.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from openai import AsyncOpenAI

import composio_integration as composio
from llm_logging import _CALL_CTX, estimate_cost  # type: ignore

logger = logging.getLogger(__name__)

MAX_TOOL_CALLS_PER_TURN = 3
MODEL = "gpt-4.1-mini"


def _openai_client() -> AsyncOpenAI:
    """Build a one-shot AsyncOpenAI client.

    Prefers the user-provided `OPENAI_API_KEY`. Falls back to
    `EMERGENT_LLM_KEY` so the budget meter keeps working when the user
    hasn't set their own key.
    """
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        key = (os.environ.get("EMERGENT_LLM_KEY") or "").strip()
    if not key:
        raise RuntimeError("No OpenAI key available for tool-calling subturn.")
    return AsyncOpenAI(api_key=key)


async def _record_llm_call(db, *, latency_ms: int, prompt_tokens: int,
                           completion_tokens: int, ok: bool, error: str = "") -> None:
    """Mirror what llm_logging records for LlmChat, but for the raw OpenAI
    call used in the tool subturn. Surfaces in SpendDashboard with
    kind_label='agent_reply:composio' so we can split tool turns out from
    normal chat spend.
    """
    ctx = _CALL_CTX.get() or {}
    label = ctx.get("kind_label", "")
    if label and ":composio" not in label:
        label = f"{label}:composio"
    elif not label:
        label = "agent_reply:composio"
    doc = {
        "provider": "openai",
        "model": MODEL,
        "kind": "chat",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cost_usd": round(estimate_cost(MODEL, prompt_tokens, completion_tokens), 6),
        "latency_ms": latency_ms,
        "ok": ok,
        "error": error,
        "kind_label": label,
        "org_id": ctx.get("org_id"),
        "user_id": ctx.get("user_id"),
        "conversation_id": ctx.get("conversation_id"),
        "agent_id": ctx.get("agent_id"),
        "created_at": datetime.now(timezone.utc),
    }
    try:
        asyncio.create_task(db.llm_calls.insert_one(doc))
    except Exception as exc:
        logger.warning("composio LLM telemetry failed: %s", exc)


async def is_agent_tools_enabled(db, org_id: str, agent_id: str) -> bool:
    """True iff the org admin has flipped the per-agent tools toggle ON.

    Default is OFF — agents must be explicitly opted-in per workspace to
    prevent surprise tool usage.
    """
    row = await db.agent_tool_prefs.find_one(
        {"org_id": org_id, "agent_id": agent_id},
        {"_id": 0, "tools_enabled": 1},
    )
    return bool((row or {}).get("tools_enabled"))


async def _persist_tool_message(
    db,
    *,
    conv_id: str,
    agent_id: str,
    agent_name: str,
    tool_slug: str,
    arguments: Dict[str, Any],
    status: str,
    result: Any,
    error: Optional[str],
) -> None:
    """Write one tool-call audit message into the conversation timeline."""
    toolkit = tool_slug.split("_", 1)[0].lower() if "_" in tool_slug else "tool"
    # Friendly label: GMAIL_SEND_EMAIL → "Gmail · send email"
    parts = tool_slug.split("_", 1)
    action_pretty = (parts[1] if len(parts) > 1 else tool_slug).replace("_", " ").lower()
    label = f"{toolkit.capitalize()} · {action_pretty}"
    text = f"{agent_name} called **{label}**" + (
        f" — {error}" if error else ""
    )
    await db.messages.insert_one({
        "id": str(uuid.uuid4()),
        "conversation_id": conv_id,
        "sender_type": "tool",
        "sender_id": None,
        "sender_name": agent_name,
        "agent_id": agent_id,
        "text": text,
        "tool_slug": tool_slug,
        "tool_toolkit": toolkit,
        "tool_label": label,
        "tool_arguments": arguments,
        "tool_status": status,  # "ok" or "error"
        "tool_result": _summarise_result(result),
        "tool_error": error,
        "timestamp": datetime.now(timezone.utc),
    })


def _summarise_result(result: Any) -> str:
    """Compact a Composio response so the timeline message stays cheap.

    Tool responses can be large (e.g. full email bodies). We keep a 400-char
    preview — the full thing is still in `llm_calls` if you need it.
    """
    if result is None:
        return ""
    try:
        s = json.dumps(result, default=str)
    except Exception:
        s = str(result)
    if len(s) > 400:
        return s[:400] + "…"
    return s


async def run_with_tools(
    db,
    *,
    org_id: str,
    user_id: str,
    agent_id: str,
    agent_name: str,
    conv_id: str,
    system_prompt: str,
    user_prompt: str,
    max_calls: int = MAX_TOOL_CALLS_PER_TURN,
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Run a single conversation turn with Composio tool calling.

    Returns
    -------
    (text, tool_messages)
        ``text`` is the final assistant reply or ``None`` if we couldn't
        even start (no Composio key, no connections, no tools resolved).
        ``tool_messages`` is the list of tool-audit messages we already
        persisted so the caller can include them in the agent_messages
        response payload for live rendering on the frontend.

    Falling back to the existing LlmChat flow is the caller's job — we
    return ``(None, [])`` to signal "didn't apply, do your thing."
    """
    if not composio.is_available():
        return None, []
    try:
        tools = await composio.get_tools_for_user(db, org_id, user_id, limit=40)
    except Exception as exc:
        logger.warning("Composio tools fetch failed for agent loop: %s", exc)
        return None, []
    if not tools:
        return None, []

    # Tell the agent what accounts it has so it can name them in replies
    # ("I'll send this from your Work Gmail"). With allow_multiple=True
    # the same toolkit can have several connections, so we surface the
    # nicknames Composio uses to disambiguate.
    try:
        all_conns = await composio.list_connections(db, org_id, user_id)
        active = [c for c in all_conns if c.get("status") == "ACTIVE"]
        if active:
            by_kit: Dict[str, List[str]] = {}
            for c in active:
                label = c.get("nickname") or c.get("account_label") or "default"
                by_kit.setdefault(c.get("toolkit_name") or c.get("toolkit"), []).append(label)
            lines = []
            for kit, labels in by_kit.items():
                if len(labels) == 1:
                    lines.append(f"- {kit}: {labels[0]}")
                else:
                    lines.append(f"- {kit}: {len(labels)} accounts ({', '.join(labels)})")
            system_prompt = (
                f"{system_prompt}\n\nCONNECTED ACCOUNTS available to you for tool calls:\n"
                + "\n".join(lines)
                + "\n\nWhen you take an action, briefly tell the user which account "
                "you're using (e.g. 'sending from your Work Gmail'). If the user has "
                "multiple accounts for the same tool and the right one is ambiguous, "
                "ASK before acting — don't guess on destructive actions like sending email."
            )
    except Exception as exc:
        logger.warning("Composio: account-context inject failed: %s", exc)

    client = _openai_client()
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    tool_audit: List[Dict[str, Any]] = []
    calls_used = 0

    # The model gets to call tools up to `max_calls` times in a single
    # turn. After that we force a text-only reply.
    for iteration in range(max_calls + 1):
        t0 = time.perf_counter()
        try:
            resp = await client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=tools if calls_used < max_calls else None,
                tool_choice="auto" if calls_used < max_calls else None,
                temperature=0.7,
            )
        except Exception as exc:
            logger.exception("OpenAI tool-loop iteration %d failed: %s", iteration, exc)
            await _record_llm_call(
                db, latency_ms=int((time.perf_counter() - t0) * 1000),
                prompt_tokens=0, completion_tokens=0,
                ok=False, error=str(exc)[:300],
            )
            # Give the normal LlmChat a chance to recover.
            if iteration == 0:
                return None, []
            return "I hit an issue while using your connected tools — try again, or disable tools for this agent.", tool_audit
        # Record telemetry for this OpenAI call so SpendDashboard sees it.
        usage = getattr(resp, "usage", None)
        await _record_llm_call(
            db, latency_ms=int((time.perf_counter() - t0) * 1000),
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            ok=True,
        )

        choice = resp.choices[0].message
        tool_calls = getattr(choice, "tool_calls", None) or []
        if not tool_calls:
            # Plain text reply — we're done.
            return (choice.content or "").strip(), tool_audit

        # Append the assistant's tool-call message so we can attach
        # `role: "tool"` results next.
        messages.append({
            "role": "assistant",
            "content": choice.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ],
        })

        for tc in tool_calls:
            if calls_used >= max_calls:
                # Drain remaining tool_calls with a "skipped" tool result
                # so the next LLM turn doesn't think they're still pending.
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps({"skipped": "max tool calls per turn reached"}),
                })
                continue
            calls_used += 1
            tool_slug = tc.function.name
            try:
                arguments = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}

            res = await composio.execute_tool(
                db, org_id, user_id, tool_slug, arguments,
            )
            status = "ok" if res.get("success") else "error"
            err = res.get("error")
            await _persist_tool_message(
                db,
                conv_id=conv_id,
                agent_id=agent_id,
                agent_name=agent_name,
                tool_slug=tool_slug,
                arguments=arguments,
                status=status,
                result=res.get("response"),
                error=err,
            )
            tool_audit.append({
                "tool_slug": tool_slug,
                "status": status,
                "error": err,
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(res.get("response") or {"error": err or "unknown"}, default=str)[:4000],
            })

    # Exhausted iterations without a clean text reply — synthesise one.
    return "I ran the tools you asked for — see the results above.", tool_audit
