"""Backend Dynamic Intelligence — task-aware model selection for server-side LLM calls.

Mirrors the spirit of the frontend `spe-sdk/model-router.js` (which detects
chat / image / video / music intent on user messages) but for *backend*
tasks the user doesn't author themselves: audit synthesis, agent reply,
mission step planning, draft batches, summary rolling, etc.

Why this exists:
  • Different tasks have different optimal models. Dropping every call onto
    one model wastes money on the cheap ones and underpowers the hard ones.
  • Hardcoding a model per call site scatters routing decisions across the
    codebase and makes it impossible to flip the default without grepping.
  • Recording the chosen model + reason on the resulting artifact gives the
    admin an audit trail (especially valuable for the prospecting Audits
    feature where the rationale is part of the trust contract).

Design:
  • Single `pick_text_model(task_type, ...)` entry point.
  • Returns ``(provider, model, reason)`` so caller can pass straight into
    `LlmChat(...).with_model(provider, model)` and stamp `reason` on the
    output row.
  • Task signatures are explicit constants — no fuzzy auto-detection at
    this layer (the frontend SDK does that for user prompts; backend tasks
    are deterministic).
  • Falls back to a sensible default if the caller passes an unknown task,
    so adding a new task type in one site doesn't require updating this
    file in lockstep.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# ── Task constants ──────────────────────────────────────────────────────────
# String constants instead of an Enum so callers can pass plain task names
# without importing the type — keeps the call site one line.
TASK_AUDIT_SYNTHESIS = "audit_synthesis"
TASK_AGENT_REPLY = "agent_reply"
TASK_MISSION_PLANNER = "mission_planner"
TASK_SOCIAL_DRAFT = "social_draft"
TASK_TOPIC_SUGGEST = "topic_suggest"
TASK_SUMMARIZE = "summarize"
TASK_INTENT_CLASSIFY = "intent_classify"
TASK_IMAGE_PROMPT = "image_prompt_synthesis"


@dataclass(frozen=True)
class ModelChoice:
    provider: str   # "openai" | "anthropic" | "gemini"
    model: str      # provider-specific model id
    reason: str     # one-sentence justification recorded on the artifact


# ── Routing table ──────────────────────────────────────────────────────────
# The matrix is intentionally small + readable — one row per task. When the
# product gains a new model option (e.g. gpt-5.2 ships) update entries here
# in one place and the change cascades through every call site.
#
# Default model family is OpenAI (per Reggie's Feb 5 2026 directive to use
# the user-supplied OPENAI_API_KEY). The audit synthesis task uses gpt-5.1
# because:
#   • It needs to ingest 5+ data sources (rendered website text, brave
#     search results across brand / reviews / competitors), 8-15K tokens of
#     context.
#   • The output is strict JSON adhering to a schema with ~30 fields —
#     gpt-5.1's instruction-following on JSON is materially better than
#     gpt-4.1-mini.
#   • Synthesis quality directly drives whether prospects find the audit
#     useful — this is the wrong place to optimize for cost.
_TABLE: dict[str, ModelChoice] = {
    TASK_AUDIT_SYNTHESIS: ModelChoice(
        provider="openai",
        model="gpt-5.1",
        reason=(
            "Long-context multi-source synthesis with strict JSON output — "
            "gpt-5.1 best balances reasoning depth, schema compliance, and "
            "cost for the prospecting Audit pipeline."
        ),
    ),
    TASK_AGENT_REPLY: ModelChoice(
        provider="openai",
        model="gpt-5.1",
        reason="Conversational quality + tool-use reliability for live agent replies.",
    ),
    TASK_MISSION_PLANNER: ModelChoice(
        provider="openai",
        model="gpt-5.1",
        reason="Multi-step reasoning + tool-use for autonomous mission loops.",
    ),
    TASK_SOCIAL_DRAFT: ModelChoice(
        provider="openai",
        model="gpt-4.1-mini",
        reason="Short structured output, batch generation — gpt-4.1-mini is fast and cheap enough to draft 10+ posts/run.",
    ),
    TASK_TOPIC_SUGGEST: ModelChoice(
        provider="openai",
        model="gpt-4.1-mini",
        reason="Brief brainstorm output where speed > depth.",
    ),
    TASK_SUMMARIZE: ModelChoice(
        provider="openai",
        model="gpt-4.1-mini",
        reason="Compression task — gpt-4.1-mini handles rolling summaries at 1/10th the cost of gpt-5.1.",
    ),
    TASK_INTENT_CLASSIFY: ModelChoice(
        provider="openai",
        model="gpt-4.1-mini",
        reason="Sub-second classification doesn't need a frontier model.",
    ),
    TASK_IMAGE_PROMPT: ModelChoice(
        provider="openai",
        model="gpt-4.1-mini",
        reason="Short prompt-rewriting for image gen — keep it cheap and fast.",
    ),
}

# Sensible default if a caller passes an unknown task. Matches the audit
# pick because frontier-tier output is the safer default when we don't
# know what the task is.
_DEFAULT_CHOICE = ModelChoice(
    provider="openai", model="gpt-5.1",
    reason="Unknown task — falling back to the default frontier model.",
)


def pick_text_model(
    task_type: str,
    *,
    override: Optional[str] = None,
) -> ModelChoice:
    """Pick the optimal text-generation model for a backend task.

    Parameters
    ----------
    task_type : str
        One of the ``TASK_*`` constants in this module.
    override : Optional[str]
        If a caller wants to force a specific model (e.g. an admin set a
        per-feature override in the future), pass ``"openai/gpt-5.2"``
        and we'll honour it without consulting the table.

    Returns
    -------
    ModelChoice
        The chosen provider, model id, and a one-sentence rationale.
    """
    if override:
        # Allow either "openai/gpt-5.1" or just "gpt-5.1" (defaults to openai)
        if "/" in override:
            prov, mdl = override.split("/", 1)
        else:
            prov, mdl = "openai", override
        return ModelChoice(
            provider=prov.strip(), model=mdl.strip(),
            reason=f"Manual override → {prov}/{mdl}",
        )
    return _TABLE.get(task_type, _DEFAULT_CHOICE)
