"""Binary artifact synthesis — Markdown → PDF / DOCX.

Agents emit rich-text artifacts using the [[ARTIFACT]] block where the
body is Markdown. When the requested extension is `.pdf` or `.docx` we
transcode the Markdown body into actual binary bytes here so the user
gets a properly-formatted document they can download and use — not a
`.pdf` with gibberish text or an empty file.

Keep this intentionally dependency-lite:
  • PDF  → reportlab (pure Python, no system libs)
  • DOCX → python-docx (already vendored)

Supports a pragmatic Markdown subset that covers 99% of business
deliverables (briefs, plans, checklists, SOPs, meeting notes, CSV-style
tables):
  # / ## / ###        → headings
  **bold** / *italic* → inline emphasis
  - item / 1. item    → bullet / numbered lists
  blank line          → paragraph break
  > quote             → indented paragraph
  ``` fenced ```       → monospace code block
  | a | b | c |       → basic tables
"""
from __future__ import annotations

import io
import re
from typing import List, Tuple

# ── Markdown → HTML (intermediate) ───────────────────────────────────────
# We use the `markdown` library for the heavy lifting (headings, lists,
# bold/italic, tables via the "tables" extension, code fences) and then
# adapt the HTML into each output format's flowable model.
try:
    import markdown as _md
    _HAVE_MD = True
except Exception:
    _HAVE_MD = False


def _md_to_html(body: str) -> str:
    if not _HAVE_MD:
        # Fall back to escaped paragraphs so we still produce readable output
        from html import escape
        paras = [p.strip() for p in re.split(r"\n{2,}", body or "") if p.strip()]
        return "".join(f"<p>{escape(p)}</p>" for p in paras)
    return _md.markdown(
        body or "",
        extensions=["tables", "fenced_code", "nl2br", "sane_lists"],
        output_format="html5",
    )


# ── PDF synthesis (reportlab + platypus) ─────────────────────────────────
def _render_pdf(body_md: str, title: str = "") -> bytes:
    """Render a Markdown body as a clean business-style PDF."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem,
        Table, TableStyle, PageBreak, Preformatted,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=LETTER,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.8 * inch, bottomMargin=0.8 * inch,
        title=title or "Document",
    )

    styles = getSampleStyleSheet()
    body_style = ParagraphStyle(
        "Body", parent=styles["BodyText"],
        fontName="Helvetica", fontSize=10.5, leading=15,
        alignment=TA_LEFT, spaceAfter=8,
    )
    h1 = ParagraphStyle("H1", parent=styles["Heading1"],
                        fontName="Helvetica-Bold", fontSize=20, leading=24,
                        spaceBefore=0, spaceAfter=10, textColor=colors.HexColor("#1a1a22"))
    h2 = ParagraphStyle("H2", parent=styles["Heading2"],
                        fontName="Helvetica-Bold", fontSize=15, leading=20,
                        spaceBefore=12, spaceAfter=6, textColor=colors.HexColor("#1a1a22"))
    h3 = ParagraphStyle("H3", parent=styles["Heading3"],
                        fontName="Helvetica-Bold", fontSize=12, leading=16,
                        spaceBefore=8, spaceAfter=4, textColor=colors.HexColor("#333333"))
    code_style = ParagraphStyle("Code", parent=styles["Code"],
                                fontName="Courier", fontSize=9, leading=12,
                                backColor=colors.HexColor("#f4f4f4"),
                                borderPadding=6, spaceAfter=8)
    quote_style = ParagraphStyle("Quote", parent=body_style,
                                 leftIndent=18, textColor=colors.HexColor("#555555"),
                                 fontName="Helvetica-Oblique")

    story: List = []
    if title and title.strip():
        story.append(Paragraph(_escape_for_para(title), h1))
        story.append(Spacer(1, 6))

    # Walk the Markdown ourselves (simple, predictable) so we can map each
    # block to the right Platypus flowable. We accept the same subset the
    # docstring advertises.
    lines = (body_md or "").splitlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Fenced code block
        if stripped.startswith("```"):
            j = i + 1
            code_lines: List[str] = []
            while j < n and not lines[j].strip().startswith("```"):
                code_lines.append(lines[j])
                j += 1
            story.append(Preformatted("\n".join(code_lines), code_style))
            i = j + 1
            continue

        # Markdown table — detect a header line followed by a separator
        # line of dashes. We accept GFM-style pipes.
        if "|" in line and i + 1 < n and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            tbl, consumed = _consume_table(lines, i)
            if tbl:
                story.append(_render_table(tbl, body_style))
                story.append(Spacer(1, 6))
                i += consumed
                continue

        # Headings
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            text = _inline_to_para(m.group(2))
            style = h1 if level == 1 else h2 if level == 2 else h3
            story.append(Paragraph(text, style))
            i += 1
            continue

        # Blockquote
        if stripped.startswith(">"):
            quote_lines = []
            while i < n and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip().lstrip(">").strip())
                i += 1
            story.append(Paragraph(_inline_to_para(" ".join(quote_lines)), quote_style))
            continue

        # Unordered list
        if re.match(r"^\s*[-*+]\s+", line):
            items, consumed = _consume_list(lines, i, ordered=False)
            story.append(ListFlowable(
                [ListItem(Paragraph(_inline_to_para(t), body_style), leftIndent=12) for t in items],
                bulletType="bullet", bulletFontSize=8, leftIndent=12,
            ))
            story.append(Spacer(1, 4))
            i += consumed
            continue

        # Ordered list
        if re.match(r"^\s*\d+\.\s+", line):
            items, consumed = _consume_list(lines, i, ordered=True)
            story.append(ListFlowable(
                [ListItem(Paragraph(_inline_to_para(t), body_style), leftIndent=14) for t in items],
                bulletType="1", bulletFontSize=9, leftIndent=14,
            ))
            story.append(Spacer(1, 4))
            i += consumed
            continue

        # Blank line → small spacer (paragraph break)
        if not stripped:
            i += 1
            continue

        # Regular paragraph — consume consecutive non-empty, non-special lines
        para_lines = [line]
        i += 1
        while i < n:
            s2 = lines[i].strip()
            if not s2: break
            if s2.startswith(("#", "> ", "- ", "* ", "+ ", "```", "|")): break
            if re.match(r"^\d+\.\s+", s2): break
            para_lines.append(lines[i])
            i += 1
        story.append(Paragraph(_inline_to_para(" ".join(l.strip() for l in para_lines)), body_style))

    if not story:
        story.append(Paragraph("(empty)", body_style))

    # Page footer with the document title, left, and page number right.
    def _footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#999999"))
        if title:
            canvas.drawString(0.75 * inch, 0.45 * inch, title[:120])
        canvas.drawRightString(LETTER[0] - 0.75 * inch, 0.45 * inch, f"Page {_doc.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def _consume_list(lines: List[str], start: int, *, ordered: bool) -> Tuple[List[str], int]:
    items: List[str] = []
    i = start
    pattern = re.compile(r"^\s*\d+\.\s+(.*)$" if ordered else r"^\s*[-*+]\s+(.*)$")
    while i < len(lines):
        m = pattern.match(lines[i])
        if not m:
            break
        items.append(m.group(1).strip())
        i += 1
    return items, i - start


def _consume_table(lines: List[str], start: int) -> Tuple[List[List[str]], int]:
    """Parse a GFM-style table starting at `lines[start]` (header line).
    Returns (rows, lines_consumed). Row 0 is the header."""
    def _cells(s: str) -> List[str]:
        parts = [p.strip() for p in s.strip().strip("|").split("|")]
        return parts

    header = _cells(lines[start])
    # Skip separator line
    rows: List[List[str]] = [header]
    i = start + 2
    while i < len(lines):
        s = lines[i].strip()
        if not s or "|" not in s:
            break
        rows.append(_cells(s))
        i += 1
    if len(rows) < 2:
        return [], 0
    return rows, i - start


def _render_table(rows: List[List[str]], body_style):
    from reportlab.platypus import Table, TableStyle, Paragraph
    from reportlab.lib import colors
    data = [[Paragraph(_inline_to_para(c or ""), body_style) for c in r] for r in rows]
    t = Table(data, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f1ef")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1a1a22")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.HexColor("#cccccc")),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#dddddd")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _escape_for_para(s: str) -> str:
    """Escape the minimum for reportlab Paragraph (XML-ish). Keeps it safe
    before we inject our own <b> / <i> tags in `_inline_to_para`."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline_to_para(s: str) -> str:
    """Translate inline Markdown (**bold**, *italic*, `code`, [txt](url))
    into Platypus's mini-HTML so the Paragraph flowable renders styles."""
    # Escape XML first
    t = _escape_for_para(s or "")
    # Inline code: `x` → <font face="Courier">x</font>
    t = re.sub(r"`([^`]+)`", r'<font face="Courier">\1</font>', t)
    # Bold: **x** → <b>x</b>
    t = re.sub(r"\*\*([^\*]+)\*\*", r"<b>\1</b>", t)
    # Italic: *x* or _x_ → <i>x</i>
    t = re.sub(r"(?<![\w\*])\*([^\*]+)\*(?![\w\*])", r"<i>\1</i>", t)
    t = re.sub(r"(?<![\w_])_([^_]+)_(?![\w_])", r"<i>\1</i>", t)
    # Links: [text](url) → <link href="url">text</link>
    t = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        r'<link href="\2" color="#d83a00">\1</link>',
        t,
    )
    return t


# ── DOCX synthesis (python-docx) ──────────────────────────────────────────
def _render_docx(body_md: str, title: str = "") -> bytes:
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor

    d = Document()
    # Tight page margins for a modern business look
    for section in d.sections:
        section.left_margin = Inches(0.75)
        section.right_margin = Inches(0.75)
        section.top_margin = Inches(0.8)
        section.bottom_margin = Inches(0.8)

    if title and title.strip():
        d.add_heading(title.strip(), level=0)

    lines = (body_md or "").splitlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            j = i + 1
            code_lines: List[str] = []
            while j < n and not lines[j].strip().startswith("```"):
                code_lines.append(lines[j])
                j += 1
            p = d.add_paragraph()
            run = p.add_run("\n".join(code_lines))
            run.font.name = "Courier New"
            run.font.size = Pt(9)
            i = j + 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            level = min(len(m.group(1)), 5)
            d.add_heading(m.group(2).strip(), level=level)
            i += 1
            continue

        if "|" in line and i + 1 < n and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            tbl, consumed = _consume_table(lines, i)
            if tbl:
                _write_docx_table(d, tbl)
                i += consumed
                continue

        if stripped.startswith(">"):
            quote_lines = []
            while i < n and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip().lstrip(">").strip())
                i += 1
            p = d.add_paragraph(" ".join(quote_lines), style="Intense Quote")
            continue

        if re.match(r"^\s*[-*+]\s+", line):
            items, consumed = _consume_list(lines, i, ordered=False)
            for it in items:
                p = d.add_paragraph(style="List Bullet")
                _write_docx_inline(p, it)
            i += consumed
            continue

        if re.match(r"^\s*\d+\.\s+", line):
            items, consumed = _consume_list(lines, i, ordered=True)
            for it in items:
                p = d.add_paragraph(style="List Number")
                _write_docx_inline(p, it)
            i += consumed
            continue

        if not stripped:
            i += 1
            continue

        para_lines = [line]
        i += 1
        while i < n:
            s2 = lines[i].strip()
            if not s2: break
            if s2.startswith(("#", "> ", "- ", "* ", "+ ", "```", "|")): break
            if re.match(r"^\d+\.\s+", s2): break
            para_lines.append(lines[i])
            i += 1
        p = d.add_paragraph()
        _write_docx_inline(p, " ".join(l.strip() for l in para_lines))

    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _write_docx_table(doc, rows: List[List[str]]) -> None:
    from docx.shared import Pt
    ncols = max(len(r) for r in rows) or 1
    table = doc.add_table(rows=len(rows), cols=ncols)
    table.style = "Light Grid Accent 1"
    for ri, row in enumerate(rows):
        cells = table.rows[ri].cells
        for ci in range(ncols):
            val = row[ci] if ci < len(row) else ""
            cells[ci].text = ""
            p = cells[ci].paragraphs[0]
            _write_docx_inline(p, val)
            if ri == 0:
                for run in p.runs:
                    run.bold = True


_BOLD_RE = re.compile(r"\*\*([^\*]+)\*\*")
_ITAL_RE = re.compile(r"(?<![\w\*])\*([^\*]+)\*(?![\w\*])")
_UND_ITAL_RE = re.compile(r"(?<![\w_])_([^_]+)_(?![\w_])")
_CODE_RE = re.compile(r"`([^`]+)`")


def _write_docx_inline(paragraph, text: str) -> None:
    """Approximate inline Markdown in a python-docx paragraph — parse a
    small set of inline tokens and add styled runs accordingly."""
    # Normalize links [text](url) → text (url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text or "")

    # Tokenise: we walk through the string and split on inline markers in
    # a specific priority: code > bold > italic. For simplicity we do
    # three sequential passes, each producing a list of (text, style)
    # tuples that the next pass re-splits.
    runs: List[Tuple[str, dict]] = [(text, {})]

    def _split(runs, regex, style_key):
        out: List[Tuple[str, dict]] = []
        for txt, sty in runs:
            if style_key in sty:
                out.append((txt, sty))
                continue
            last = 0
            for m in regex.finditer(txt):
                if m.start() > last:
                    out.append((txt[last:m.start()], dict(sty)))
                out.append((m.group(1), {**sty, style_key: True}))
                last = m.end()
            if last < len(txt):
                out.append((txt[last:], dict(sty)))
        return out

    runs = _split(runs, _CODE_RE, "code")
    runs = _split(runs, _BOLD_RE, "bold")
    runs = _split(runs, _ITAL_RE, "italic")
    runs = _split(runs, _UND_ITAL_RE, "italic")

    from docx.shared import Pt
    for txt, sty in runs:
        if not txt:
            continue
        r = paragraph.add_run(txt)
        if sty.get("bold"):   r.bold = True
        if sty.get("italic"): r.italic = True
        if sty.get("code"):
            r.font.name = "Courier New"
            r.font.size = Pt(9.5)


# ── Public API ────────────────────────────────────────────────────────────
SUPPORTED_BINARY_EXTS = {"pdf", "docx"}

MIME_FOR = {
    "pdf":  "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def synthesize(body_md: str, ext: str, title: str = "") -> Tuple[bytes, str]:
    """Convert a Markdown body to binary bytes for the requested ext.
    Returns (bytes, mime). Raises ValueError for unsupported exts."""
    ext = (ext or "").lower().lstrip(".")
    if ext == "pdf":
        return _render_pdf(body_md, title=title), MIME_FOR["pdf"]
    if ext == "docx":
        return _render_docx(body_md, title=title), MIME_FOR["docx"]
    raise ValueError(f"Unsupported binary artifact extension: {ext}")
