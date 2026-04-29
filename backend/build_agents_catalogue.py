#!/usr/bin/env python3
"""
Build the agents catalogue JSON from a local clone of msitarzewski/agency-agents.

Usage:  python build_agents_catalogue.py /path/to/agency-agents [out.json]

Output: JSON list of agents with
  { id, division, name, description, tagline, emoji, color, accent, system }
where `system` is the full personality/process prompt (the markdown body after
the YAML frontmatter).
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

# Divisions we ship as agents. Skip meta/doc folders.
DIVISIONS = {
    "academic": "Academic",
    "design": "Design",
    "engineering": "Engineering",
    "finance": "Finance",
    "game-development": "Game Development",
    "marketing": "Marketing",
    "paid-media": "Paid Media",
    "product": "Product",
    "project-management": "Project Management",
    "sales": "Sales",
    "spatial-computing": "Spatial Computing",
    "specialized": "Specialized",
    "support": "Support",
    "testing": "Testing",
}

# Map agent-frontmatter colors to hex accents used by the UI gradient swatch.
COLOR_MAP = {
    "red":     "#f87171",
    "orange":  "#fb923c",
    "amber":   "#f59e0b",
    "yellow":  "#facc15",
    "lime":    "#a3e635",
    "green":   "#34d399",
    "emerald": "#34d399",
    "teal":    "#2dd4bf",
    "cyan":    "#22d3ee",
    "sky":     "#38bdf8",
    "blue":    "#60a5fa",
    "indigo":  "#818cf8",
    "violet":  "#a78bfa",
    "purple":  "#c084fc",
    "fuchsia": "#e879f9",
    "pink":    "#f472b6",
    "rose":    "#fb7185",
    "slate":   "#94a3b8",
    "gray":    "#9ca3af",
    "grey":    "#9ca3af",
    "zinc":    "#a1a1aa",
    "stone":   "#a8a29e",
    "neutral": "#a3a3a3",
    "white":   "#e5e7eb",
    "black":   "#52525b",
}

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)

def parse_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw, body = m.group(1), m.group(2)
    meta: dict = {}
    for line in raw.splitlines():
        if not line.strip() or ":" not in line:
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, body


def truncate_system(body: str, max_chars: int = 4500) -> str:
    """Keep prompts token-efficient. Cut at a section boundary when possible."""
    body = body.strip()
    if len(body) <= max_chars:
        return body
    cut = body.rfind("\n## ", 0, max_chars)
    if cut < max_chars // 2:
        cut = body.rfind("\n\n", 0, max_chars)
    if cut < 200:
        cut = max_chars
    return body[:cut].rstrip() + "\n\n[…]"


def title_case_id(stem: str, division: str) -> str:
    # filename → agent name fallback. e.g. "engineering-frontend-developer" → "Frontend Developer"
    prefix = division + "-"
    core = stem[len(prefix):] if stem.startswith(prefix) else stem
    words = [w.capitalize() for w in core.split("-") if w]
    return " ".join(words) or stem


def build(src: Path) -> list[dict]:
    agents: list[dict] = []
    seen_ids: set[str] = set()
    for folder, label in DIVISIONS.items():
        d = src / folder
        if not d.is_dir():
            continue
        for md in sorted(d.glob("*.md")):
            text = md.read_text(encoding="utf-8", errors="ignore")
            meta, body = parse_frontmatter(text)
            # Skip non-agent docs (strategy meta files have no frontmatter)
            if not meta or not meta.get("name"):
                # Fallback: still allow if the file is clearly an agent with a "# Foo Agent" heading
                if "Agent" not in text[:200]:
                    continue
            agent_id = md.stem.lower()
            if agent_id in seen_ids:
                continue
            seen_ids.add(agent_id)
            name = meta.get("name") or title_case_id(md.stem, folder)
            color_name = (meta.get("color") or "violet").lower().strip()
            accent = COLOR_MAP.get(color_name, "#a78bfa")
            emoji = meta.get("emoji") or ""
            vibe = meta.get("vibe") or ""
            description = meta.get("description") or vibe
            tagline = vibe or (description[:120] + ("…" if len(description) > 120 else ""))
            system = truncate_system(body)
            agents.append({
                "id": agent_id,
                "division": label,
                "division_key": folder,
                "name": name,
                "description": description,
                "tagline": tagline,
                "emoji": emoji,
                "color": color_name,
                "accent": accent,
                "system": system,
            })
    return agents


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1]).resolve()
    if not src.is_dir():
        print(f"Not a directory: {src}")
        return 2
    out = Path(sys.argv[2]) if len(sys.argv) >= 3 else Path(__file__).parent / "agents_catalogue.json"
    agents = build(src)
    out.write_text(json.dumps(agents, indent=2, ensure_ascii=False), encoding="utf-8")
    by_div: dict[str, int] = {}
    for a in agents:
        by_div[a["division"]] = by_div.get(a["division"], 0) + 1
    print(f"Wrote {len(agents)} agents to {out}")
    for div, n in sorted(by_div.items()):
        print(f"  {div:26s} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
