"""Helpers for org-asset text extraction and prompt formatting."""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Iterable

log = logging.getLogger("assets")

TEXT_LIKE_EXTS = {"txt", "md", "json", "csv", "log"}
PDF_EXTS = {"pdf"}
DOCX_EXTS = {"docx"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif", "svg"}

MAX_TEXT_PER_ASSET = 4000  # chars — keeps system prompts trim


def is_text_extractable(ext: str) -> bool:
    e = ext.lower().lstrip(".")
    return e in TEXT_LIKE_EXTS or e in PDF_EXTS or e in DOCX_EXTS


def extract_text(path: Path, ext: str) -> str:
    """Best-effort text extraction; returns empty string on any failure."""
    e = ext.lower().lstrip(".")
    try:
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
        lines.append(f"- `{a['original_name']}` ({' · '.join(meta_bits)})")
        if a.get("preview"):
            lines.append(f"    excerpt: {a['preview']}")
    return "\n".join(lines)
