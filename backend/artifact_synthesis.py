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
from typing import List, Optional, Tuple

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


def _render_xlsx(body_md: str, title: str = "") -> bytes:
    """Markdown → XLSX spreadsheet via openpyxl.

    Strategy: extract every Markdown table from the body and lay them
    out as separate worksheets (Sheet1, Sheet2, …). Any prose around
    the tables lands on a "Notes" sheet so the agent's commentary
    isn't lost. If no tables are present, dump the whole body as
    paragraphs into Notes — keeps the contract "agent emits markdown,
    we produce a file" intact even for narrative content.

    Pragmatic header detection: the first row of each table is styled
    bold + autofilter-enabled so users get a usable spreadsheet, not
    a dump of cells.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    # Strip the auto-created default sheet — we'll add named ones.
    default = wb.active
    wb.remove(default)

    lines = (body_md or "").splitlines()
    n = len(lines)
    tables: List[List[List[str]]] = []
    prose: List[str] = []
    i = 0
    while i < n:
        line = lines[i]
        if "|" in line and i + 1 < n and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            tbl, consumed = _consume_table(lines, i)
            if tbl:
                tables.append(tbl)
                i += consumed
                continue
        # Everything else flows to prose (we keep paragraph breaks).
        prose.append(line)
        i += 1

    bold = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="2D3748", end_color="2D3748", fill_type="solid")
    center = Alignment(horizontal="left", vertical="top", wrap_text=True)

    for idx, tbl in enumerate(tables, start=1):
        ws = wb.create_sheet(title=f"Sheet{idx}")
        for row_idx, row in enumerate(tbl, start=1):
            for col_idx, cell in enumerate(row, start=1):
                c = ws.cell(row=row_idx, column=col_idx, value=str(cell or ""))
                c.alignment = center
                if row_idx == 1:
                    c.font = bold
                    c.fill = header_fill
        # Crude but effective autosize: cap at 60 chars to avoid
        # giant runaway columns on long URLs.
        for col_idx in range(1, len(tbl[0]) + 1):
            max_len = max(
                (len(str(r[col_idx - 1])) for r in tbl if col_idx - 1 < len(r)),
                default=10,
            )
            ws.column_dimensions[get_column_letter(col_idx)].width = min(60, max(12, max_len + 2))
        # Freeze the header row.
        ws.freeze_panes = "A2"
        if len(tbl) > 1:
            ws.auto_filter.ref = ws.dimensions

    # Always create a Notes sheet with the prose so commentary isn't lost.
    notes_text = "\n".join(prose).strip()
    if notes_text or not tables:
        notes_ws = wb.create_sheet(title="Notes", index=0)
        if title:
            t = notes_ws.cell(row=1, column=1, value=title.strip())
            t.font = Font(bold=True, size=14)
            row = 3
        else:
            row = 1
        for para in re.split(r"\n{2,}", notes_text or (body_md or "")):
            para = re.sub(r"\s+", " ", para).strip()
            if not para:
                continue
            c = notes_ws.cell(row=row, column=1, value=para)
            c.alignment = Alignment(wrap_text=True, vertical="top")
            row += 1
        notes_ws.column_dimensions["A"].width = 100

    # If we somehow ended up with zero sheets, add an empty placeholder.
    if not wb.sheetnames:
        wb.create_sheet(title="Sheet1")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _render_pptx(body_md: str, title: str = "") -> bytes:
    """Markdown → PPTX deck via python-pptx.

    Slide-structure rule: each top-level `# heading` becomes a new
    slide title; the following content (paragraphs, bullets) becomes
    the slide body. If the body has no `#` headings, we generate a
    single-content deck with the title slide + one body slide.

    Designed to produce a CLEAN deck out of the box (no template),
    relying on python-pptx defaults. Branding/template support is a
    Phase 2 follow-up.
    """
    from pptx import Presentation
    from pptx.util import Pt, Inches
    from pptx.enum.text import PP_ALIGN

    prs = Presentation()
    # 16:9 widescreen — modern default for any deck shown on a TV /
    # projector / Zoom share.
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    # Title slide
    title_layout = prs.slide_layouts[0]  # "Title Slide"
    slide = prs.slides.add_slide(title_layout)
    slide.shapes.title.text = title.strip() or "Untitled Deck"
    if len(slide.placeholders) > 1:
        slide.placeholders[1].text = (
            "Generated by Foundry — Document Generator agent"
        )

    # Body slides: chunk by `#` headings.
    lines = (body_md or "").splitlines()
    n = len(lines)
    i = 0
    current_title = ""
    current_body: List[str] = []

    def flush_slide():
        nonlocal current_title, current_body
        if not current_title and not current_body:
            return
        body_layout = prs.slide_layouts[1]  # "Title and Content"
        sl = prs.slides.add_slide(body_layout)
        sl.shapes.title.text = current_title or "Notes"
        if len(sl.placeholders) > 1:
            tf = sl.placeholders[1].text_frame
            tf.clear()
            tf.word_wrap = True
            first = True
            for line in current_body:
                stripped = line.strip()
                if not stripped:
                    continue
                # Convert "- item" / "* item" to bullets, otherwise
                # render as paragraphs.
                m = re.match(r"^\s*[-*+]\s+(.*)$", line)
                m_ol = re.match(r"^\s*\d+\.\s+(.*)$", line)
                if m:
                    txt = m.group(1)
                    lvl = 0
                elif m_ol:
                    txt = m_ol.group(1)
                    lvl = 0
                else:
                    txt = stripped
                    lvl = 0
                # Strip inline emphasis markers for clean slide text;
                # the slide layout handles emphasis via run formatting,
                # but parsing **bold**/*italic* into runs is a deep
                # rabbit hole — Phase 2.
                txt = re.sub(r"\*\*(.+?)\*\*", r"\1", txt)
                txt = re.sub(r"\*(.+?)\*", r"\1", txt)
                if first:
                    p = tf.paragraphs[0]
                    first = False
                else:
                    p = tf.add_paragraph()
                p.text = txt
                p.level = lvl
                for run in p.runs:
                    run.font.size = Pt(18)
        current_title = ""
        current_body = []

    while i < n:
        line = lines[i]
        m = re.match(r"^(#{1,3})\s+(.*)$", line.strip())
        if m:
            # A new heading flushes the previous slide.
            flush_slide()
            current_title = m.group(2).strip()
            i += 1
            continue
        current_body.append(line)
        i += 1
    flush_slide()

    # Safety: if zero body slides were produced, add one with the
    # whole body as a single block of text — at least the user has
    # *something*.
    if len(prs.slides) == 1:
        body_layout = prs.slide_layouts[1]
        sl = prs.slides.add_slide(body_layout)
        sl.shapes.title.text = "Content"
        if len(sl.placeholders) > 1:
            tf = sl.placeholders[1].text_frame
            tf.text = (body_md or "").strip()[:3000]
            tf.word_wrap = True

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _render_csv(body_md: str, title: str = "") -> bytes:
    """Markdown → CSV (UTF-8, BOM-prefixed for Excel friendliness).

    Picks the FIRST Markdown table in the body. If no table is found,
    falls back to a single-column CSV of paragraph lines so the agent's
    output isn't silently lost.
    """
    import csv
    lines = (body_md or "").splitlines()
    n = len(lines)
    chosen_table: Optional[List[List[str]]] = None
    for i, line in enumerate(lines):
        if "|" in line and i + 1 < n and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            tbl, _ = _consume_table(lines, i)
            if tbl:
                chosen_table = tbl
                break

    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    if chosen_table:
        for row in chosen_table:
            writer.writerow(row)
    else:
        for para in re.split(r"\n{2,}", body_md or ""):
            para = re.sub(r"\s+", " ", para).strip()
            if para:
                writer.writerow([para])
    # BOM so Excel auto-detects UTF-8 instead of mangling diacritics.
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


# ── Public API ────────────────────────────────────────────────────────────
SUPPORTED_BINARY_EXTS = {"pdf", "docx", "xlsx", "pptx", "csv"}

MIME_FOR = {
    "pdf":  "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "csv":  "text/csv",
}


def synthesize(body_md: str, ext: str, title: str = "") -> Tuple[bytes, str]:
    """Convert a Markdown body to binary bytes for the requested ext.
    Returns (bytes, mime). Raises ValueError for unsupported exts.

    Sync path — uses reportlab for PDF and python-docx for DOCX. Kept
    for backwards compatibility / when no async event loop is running.
    Prefer ``synthesize_async`` when an org_id is available — it
    routes PDFs through the branded Playwright renderer for a polished,
    business-grade document.
    """
    ext = (ext or "").lower().lstrip(".")
    if ext == "pdf":
        return _render_pdf(body_md, title=title), MIME_FOR["pdf"]
    if ext == "docx":
        return _render_docx(body_md, title=title), MIME_FOR["docx"]
    if ext == "xlsx":
        return _render_xlsx(body_md, title=title), MIME_FOR["xlsx"]
    if ext == "pptx":
        return _render_pptx(body_md, title=title), MIME_FOR["pptx"]
    if ext == "csv":
        return _render_csv(body_md, title=title), MIME_FOR["csv"]
    raise ValueError(f"Unsupported binary artifact extension: {ext}")


async def synthesize_async(
    body_md: str, ext: str, title: str = "", *,
    db=None, org_id: str = "",
) -> Tuple[bytes, str]:
    """Async synthesis with brand-aware rendering.

    For PDF: renders a branded HTML document via Playwright using the
    org's logo + primary colour + tagline. Falls back to the reportlab
    path if Playwright is unavailable or any step fails — never raises,
    always returns valid bytes.

    For DOCX / XLSX / PPTX / CSV: delegates to the sync renderers
    (no branded template variants yet — Phase 2 follow-up).
    """
    ext = (ext or "").lower().lstrip(".")
    if ext == "pdf":
        try:
            from pdf_branded import render_branded_pdf
            data = await render_branded_pdf(
                body_md=body_md, title=title, db=db, org_id=org_id,
            )
            if data and len(data) > 200:
                return data, MIME_FOR["pdf"]
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "Branded PDF render failed (%s) — using reportlab fallback", exc,
            )
        return _render_pdf(body_md, title=title), MIME_FOR["pdf"]
    if ext in SUPPORTED_BINARY_EXTS:
        return synthesize(body_md, ext, title=title)
    raise ValueError(f"Unsupported binary artifact extension: {ext}")
