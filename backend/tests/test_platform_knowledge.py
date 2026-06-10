"""Regression: the chat layer injects authoritative Foundry knowledge
when (and only when) the user is asking about the platform.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from platform_knowledge import (  # noqa: E402
    has_foundry_question, get_foundry_context_block, reload_knowledge,
)


# Make sure the canonical file got loaded — most tests below assume it.
@pytest.fixture(scope="module", autouse=True)
def _ensure_loaded():
    ok = reload_knowledge()
    assert ok, "Foundry knowledge .md must exist at /app/backend/knowledge/foundry.md"


# ── Intent detector — positives ──────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "What is Foundry?",
        "what's the foundry platform",
        "Tell me about Foundry",
        "Tell me about the Foundry platform",
        "Explain the Foundry",
        "How does Foundry work?",
        "What can foundry do for me?",
        "What can this platform do?",
        "What can this app do?",
        "what does this platform do",
        "Tell me about this app",
        "Who built this?",
        "Who built Foundry?",
        "Who made this platform?",
        "Who is behind Foundry?",
        "What are your features?",
        "list your features",
        "show me the features",
        "what are the platform's capabilities",
        "How do I get started?",
        "Getting started with Foundry",
        "Foundry platform overview",
    ],
)
def test_detector_catches_foundry_questions(text):
    assert has_foundry_question(text), (
        f"has_foundry_question should match: {text!r}"
    )


# ── Intent detector — negatives ──────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        # Unrelated business chatter
        "Schedule a meeting with our investor for Friday",
        "Generate 7 IG posts about Disciplined Hustle",
        "Write me a one-page audit report",
        "What's our brand tone again?",
        # Empty / blank
        "",
        "   ",
        # "Foundry" as part of a business name, not a platform question
        "I had lunch at The Foundry yesterday",
        # Adjacent but not actually a platform question
        "How does the social queue scheduler work internally?",
    ],
)
def test_detector_ignores_non_foundry_questions(text):
    assert not has_foundry_question(text), (
        f"has_foundry_question should NOT match: {text!r}"
    )


# ── Context block shape ──────────────────────────────────────────────


def test_context_block_returns_none_for_non_foundry_question():
    assert get_foundry_context_block("Generate 5 IG posts") is None
    assert get_foundry_context_block("") is None
    assert get_foundry_context_block(None) is None


def test_context_block_includes_authority_header():
    """The header tells the agent the block is authoritative and to
    refuse to fabricate. Without it the LLM can drift into "based on my
    general knowledge of B2B SaaS…" territory."""
    block = get_foundry_context_block("What is Foundry?")
    assert block is not None
    assert "PRODUCT KNOWLEDGE: Foundry" in block
    assert "AUTHORITATIVE" in block
    assert "DO NOT fabricate" in block


def test_context_block_includes_key_features():
    """Spot-check that the loaded .md covers the major features the
    user wants the agent to be able to answer about."""
    block = get_foundry_context_block("Tell me about Foundry")
    assert block is not None
    # The features the user listed when greenlighting this build
    for needle in (
        "Soul",                    # SoulPrint
        "Mission",                 # Mission Drafter / Crucible
        "Social Queue",
        "Asset Library",
        "Nano Banana",             # image gen
        "Kie.ai",                  # video gen
        "Veo",                     # video model name
        "Composio",                # publishing
        "Telegram",                # chief of staff bot
        "OpenTimestamps",          # audit anchoring
        "SOC 2",                   # compliance posture
    ):
        assert needle in block, f"Knowledge file missing: {needle!r}"


def test_context_block_states_what_foundry_is_not():
    """The agent should be able to gracefully decline fabricating
    features. The .md has a "What Foundry is *not*" section for this."""
    block = get_foundry_context_block("What is Foundry?")
    assert "is *not*" in block or "is not" in block.lower()


def test_context_block_includes_multiple_sources():
    """Multi-source loader: the injected block should carry SECTION
    headers for each .md it pulled in, so the agent can cite where an
    answer came from (e.g. "per FOUNDRY_FEATURES.md…")."""
    block = get_foundry_context_block("Tell me about Foundry")
    assert block is not None
    # The bundle MUST surface at least the headline doc + the
    # canonical FAQ. Other files (whitepaper, SOC2, ABOUT) may or
    # may not ship in slim builds, so we assert on the two we always
    # carry.
    assert "── SOURCE: backend/knowledge/foundry.md ──" in block
    assert "── SOURCE: docs/FOUNDRY_FEATURES.md ──" in block


def test_context_block_includes_features_doc_content():
    """The FEATURES.md file describes capabilities the user explicitly
    wants chat answers to be sourced from — spot-check that its content
    actually landed in the bundle (not just the header)."""
    block = get_foundry_context_block("What are your features?")
    assert block is not None
    # Pull anchors from the actual FOUNDRY_FEATURES.md so the test
    # tracks the source-of-truth instead of guessing strings.
    from pathlib import Path
    features_md = (
        Path(__file__).resolve().parents[2]
        / "docs" / "FOUNDRY_FEATURES.md"
    )
    assert features_md.is_file()
    # Find a non-empty line that's clearly distinctive (length > 30,
    # has at least one alpha word) — proves the body, not just the
    # header, made it into the bundle.
    distinctive = next(
        (ln.strip() for ln in features_md.read_text().splitlines()
         if len(ln.strip()) > 50 and not ln.strip().startswith("#")),
        None,
    )
    assert distinctive, "FOUNDRY_FEATURES.md has no body content to anchor on"
    assert distinctive in block, (
        "Body of FOUNDRY_FEATURES.md not present in the injected block — "
        "the loader is dropping content."
    )


def test_context_block_excludes_dev_internal_files():
    """memory/PRD.md, memory/CHANGELOG.md, memory/test_credentials.md
    are dev-internal and MUST NOT leak into agent prompts (they contain
    seed creds, agent roadmap, and unimplemented features)."""
    block = get_foundry_context_block("Tell me about Foundry")
    assert block is not None
    for forbidden in ("PRD.md", "CHANGELOG.md", "test_credentials.md"):
        assert f"── SOURCE: memory/{forbidden} ──" not in block, (
            f"Dev-internal file {forbidden!r} leaked into agent prompt"
        )


# ── End-to-end via build_system_prompt ──────────────────────────────


def test_build_system_prompt_injects_when_asked():
    """When `latest_user_text` matches a Foundry question, the system
    prompt MUST include the authority block. When it doesn't match,
    the block MUST NOT appear (zero token cost on every other turn)."""
    import importlib
    server = importlib.import_module("server")
    AGENTS = server.AGENTS
    agent_id = next(iter(AGENTS))  # any registered agent
    team = {"name": "Test Team", "org_id": "x", "soulprint": {}}

    with_q = server.build_system_prompt(
        agent_id, team, current_user_name="Test User",
        latest_user_text="What can Foundry do for my business?",
    )
    assert "PRODUCT KNOWLEDGE: Foundry" in with_q
    assert "Nano Banana" in with_q  # confirms .md content landed

    no_q = server.build_system_prompt(
        agent_id, team, current_user_name="Test User",
        latest_user_text="Generate 7 IG posts about pistachios.",
    )
    assert "PRODUCT KNOWLEDGE: Foundry" not in no_q


def test_build_system_prompt_handles_none_user_text():
    """Backwards-compat — early callsites may not pass latest_user_text."""
    import importlib
    server = importlib.import_module("server")
    AGENTS = server.AGENTS
    agent_id = next(iter(AGENTS))
    team = {"name": "T", "org_id": "x", "soulprint": {}}
    out = server.build_system_prompt(agent_id, team)
    assert "PRODUCT KNOWLEDGE: Foundry" not in out
