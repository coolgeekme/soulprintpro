"""Helpers for org-asset text extraction and prompt formatting."""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Iterable

log = logging.getLogger("assets")

TEXT_LIKE_EXTS = {
    # Plain text / data
    "txt", "md", "markdown", "rst",
    "json", "yaml", "yml", "toml",
    "csv", "tsv", "log",
    # Web & markup — extracted with tag-stripping so agent prompts get
    # the prose, not the angle brackets.
    "html", "htm", "xml", "rss", "atom", "svg",
    # Rich text
    "rtf",
    # Code (rare but useful when users drop a config or snippet)
    "py", "js", "ts", "css", "sql", "sh", "env",
}
PDF_EXTS = {"pdf"}
DOCX_EXTS = {"docx"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif", "svg"}

MAX_TEXT_PER_ASSET = 4000  # chars — keeps system prompts trim


def is_text_extractable(ext: str) -> bool:
    e = ext.lower().lstrip(".")
    return e in TEXT_LIKE_EXTS or e in PDF_EXTS or e in DOCX_EXTS


def extract_text(path: Path, ext: str) -> str:
    """Best-effort text extraction; returns empty string on any failure.

    For HTML/XML/SVG, strips tags so the prompt context contains the
    user-visible text, not the markup. RTF goes through a light
    `striprtf` pass when the library is available, falls back to a
    crude regex strip when it isn't.
    """
    e = ext.lower().lstrip(".")
    try:
        if e in {"html", "htm", "xml", "rss", "atom", "svg"}:
            raw = path.read_text(encoding="utf-8", errors="ignore")
            return _strip_html_like(raw)
        if e == "rtf":
            raw = path.read_text(encoding="utf-8", errors="ignore")
            try:
                from striprtf.striprtf import rtf_to_text  # type: ignore
                return rtf_to_text(raw)
            except Exception:
                # Crude fallback — drop control words and braces. Good
                # enough for the prompt context layer.
                import re
                no_groups = re.sub(r"[{}]", "", raw)
                no_ctrl = re.sub(r"\\[a-zA-Z]+-?\d*\s?", "", no_groups)
                return re.sub(r"\\[^a-zA-Z]", "", no_ctrl)
        if e in TEXT_LIKE_EXTS:
            return path.read_text(encoding="utf-8", errors="ignore")
        if e in PDF_EXTS:
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            chunks = []
            for page in reader.pages:
                try:
                    chunks.append(page.extract_text() or "")
                except Exception:
                    pass
            return "\n".join(chunks)
        if e in DOCX_EXTS:
            from docx import Document
            doc = Document(str(path))
            return "\n".join(p.text for p in doc.paragraphs if p.text)
    except Exception as exc:
        log.warning("Text extraction failed for %s: %s", path, exc)
    return ""


def _strip_html_like(raw: str) -> str:
    """Crude tag stripper — good enough for prompt context.

    Removes <script>/<style> blocks first (their contents are noise),
    then removes everything between angle brackets, then collapses
    whitespace. We deliberately don't pull in BeautifulSoup just for
    this — it's a 2 MB add to keep prompts trim.
    """
    import re
    import html as _html
    # Drop script / style entirely (content + tags). DOTALL so it
    # spans newlines.
    s = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1\s*>", "",
        raw, flags=re.IGNORECASE | re.DOTALL,
    )
    # Strip all remaining tags.
    s = re.sub(r"<[^>]+>", " ", s)
    # Decode HTML entities (&amp; → &, &#39; → ', etc.).
    s = _html.unescape(s)
    # Collapse whitespace.
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def trim(text: str, limit: int = MAX_TEXT_PER_ASSET) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[…truncated]"


def format_assets_for_prompt(assets: Iterable[dict]) -> str:
    """Compact rendering for injection into an agent system prompt."""
    rows = list(assets)
    if not rows:
        return ""
    lines = [
        "Organisation reference library — these assets belong to the user's "
        "organisation and you may cite them by name when relevant. Refer to "
        "filenames in `inline-code` so the UI can render them as chips."
    ]
    for a in rows:
        meta_bits = [a.get("kind", "file")]
        if a.get("description"):
            meta_bits.append(a["description"])
        # External links carry a `link_url` we surface so agents can
        # reference the actual document the user shared (Google Drive,
        # Notion, Figma, Dropbox, etc.).
        if a.get("kind") == "link" and a.get("link_url"):
            provider = a.get("link_provider") or "link"
            meta_bits.append(provider)
            lines.append(
                f"- `{a['original_name']}` ({' · '.join(meta_bits)}) → {a['link_url']}"
            )
        else:
            lines.append(f"- `{a['original_name']}` ({' · '.join(meta_bits)})")
        if a.get("preview"):
            lines.append(f"    excerpt: {a['preview']}")
    return "\n".join(lines)
