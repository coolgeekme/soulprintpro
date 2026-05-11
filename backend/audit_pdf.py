"""Polished, brand-aligned PDF renderer for Business Audits.

Replaces the generic Markdown-to-PDF path for audits ONLY — other
artifact types (briefs, plans, reports) continue to use
`artifact_synthesis._render_pdf`.

Design goals:
  • **Cover page** — big brand name, hero score circle, prep date,
    Foundry footer
  • **Score band coloring** — green ≥80, amber ≥60, red <60. The
    score number, ring, and band label all reflect the same color
  • **Card-style sections** — strengths/issues/recommendations rendered
    as bordered tables with subtle backgrounds, not raw paragraphs
  • **Priority pills** — recommendations carry coloured HIGH / MED /
    LOW pills inline with the title
  • **"How SoulPrint helps" callouts** — orange accent inset card on
    every recommendation, mirroring the in-app design
  • **Section eyebrows** — uppercase tracking-widest micro-labels above
    each section, mirroring the SPA
  • **Branded header/footer** — every page after the cover gets a slim
    orange rule + page number
"""
from __future__ import annotations

import io
import math
from datetime import datetime, timezone
from typing import Iterable

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as _canvas
from reportlab.platypus import (
    BaseDocTemplate, Frame, NextPageTemplate, PageBreak, PageTemplate,
    Paragraph, Spacer, Table, TableStyle, KeepTogether, Flowable,
)


# ── Unicode-safe font registration ─────────────────────────────────────────
# Helvetica's default WinAnsi encoding can't render several characters the
# LLM regularly emits (non-breaking hyphen U+2011, ellipsis U+2026, some
# smart-quote variants). Register Bitstream Vera (ships with reportlab)
# as our body/bold/italic faces so unicode flows cleanly. Fall back to
# Helvetica if Vera files aren't on disk for any reason.
def _register_unicode_fonts() -> tuple[str, str, str]:
    try:
        import os as _os
        from reportlab.rl_config import T1SearchPath  # noqa: F401
        font_dir = _os.path.join(
            _os.path.dirname(__import__("reportlab").__file__), "fonts",
        )
        regular = _os.path.join(font_dir, "Vera.ttf")
        bold = _os.path.join(font_dir, "VeraBd.ttf")
        italic = _os.path.join(font_dir, "VeraIt.ttf")
        if not (_os.path.isfile(regular) and _os.path.isfile(bold)
                and _os.path.isfile(italic)):
            raise FileNotFoundError
        pdfmetrics.registerFont(TTFont("AuditBody", regular))
        pdfmetrics.registerFont(TTFont("AuditBody-Bold", bold))
        pdfmetrics.registerFont(TTFont("AuditBody-Italic", italic))
        from reportlab.pdfbase.pdfmetrics import registerFontFamily
        registerFontFamily(
            "AuditBody", normal="AuditBody",
            bold="AuditBody-Bold", italic="AuditBody-Italic",
            boldItalic="AuditBody-Bold",
        )
        return "AuditBody", "AuditBody-Bold", "AuditBody-Italic"
    except Exception:
        return "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"


FONT_REG, FONT_BOLD, FONT_ITALIC = _register_unicode_fonts()


# ── Brand tokens ────────────────────────────────────────────────────────────
BRAND_ORANGE = colors.HexColor("#F64000")
BRAND_ORANGE_SOFT = colors.HexColor("#fff7ed")
BRAND_ORANGE_BORDER = colors.HexColor("#fdba74")
INK = colors.HexColor("#0f0f0f")
INK_SOFT = colors.HexColor("#374151")
MUTED = colors.HexColor("#6b7280")
RULE = colors.HexColor("#e5e7eb")
CARD_BG = colors.HexColor("#fafafa")
GREEN = colors.HexColor("#10b981")
GREEN_SOFT = colors.HexColor("#ecfdf5")
GREEN_BORDER = colors.HexColor("#a7f3d0")
AMBER = colors.HexColor("#f59e0b")
AMBER_SOFT = colors.HexColor("#fffbeb")
RED = colors.HexColor("#ef4444")
RED_SOFT = colors.HexColor("#fef2f2")
RED_BORDER = colors.HexColor("#fecaca")


def _band(score: int):
    """Return (color, soft_bg, label) for the score band."""
    if score >= 80:
        return GREEN, GREEN_SOFT, "STRONG"
    if score >= 60:
        return AMBER, AMBER_SOFT, "NEEDS WORK"
    return RED, RED_SOFT, "URGENT"


def _priority_band(priority: str):
    p = (priority or "").lower()
    if p == "high":
        return RED, RED_SOFT, RED_BORDER
    if p == "medium":
        return AMBER, AMBER_SOFT, colors.HexColor("#fde68a")
    return MUTED, colors.HexColor("#f3f4f6"), RULE


# ── Inline-emphasis escaping (reportlab Paragraph flavour) ──────────────────
# Map a handful of Unicode codepoints that even Vera's TTF doesn't
# always glyph for, to ASCII-safe equivalents the renderer is sure to
# draw. Without this, U+2011 (non-breaking hyphen) shows up as a "tofu"
# (☐) box mid-word.
_UNICODE_FALLBACKS = {
    "\u2010": "-",   # HYPHEN
    "\u2011": "-",   # NON-BREAKING HYPHEN
    "\u2012": "-",   # FIGURE DASH
    "\u2212": "-",   # MINUS SIGN
    "\u00ad": "",    # SOFT HYPHEN (invisible)
    "\u200b": "",    # ZERO-WIDTH SPACE
    "\u200c": "",    # ZWNJ
    "\u200d": "",    # ZWJ
    "\ufeff": "",    # BOM
    "\u00a0": " ",   # NBSP — let layout breathe
}


def _sanitize_text(text: str) -> str:
    if not text:
        return ""
    s = str(text)
    for k, v in _UNICODE_FALLBACKS.items():
        if k in s:
            s = s.replace(k, v)
    return s


def _para_escape(text: str) -> str:
    """Escape user text for reportlab Paragraph. Translates **bold** and
    *italic* markdown idioms to <b>/<i> tags, since the LLM tends to
    leak markdown emphasis into structured fields."""
    if not text:
        return ""
    s = (
        _sanitize_text(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    # Convert **bold** and *italic*. Order matters — bold first.
    import re as _re
    s = _re.sub(r"\*\*([^\*]+)\*\*", r"<b>\1</b>", s)
    s = _re.sub(r"(?<!\*)\*([^\*]+)\*(?!\*)", r"<i>\1</i>", s)
    # Markdown line-break trailing two spaces
    s = s.replace("  \n", "<br/>")
    return s


# ── Score ring flowable ─────────────────────────────────────────────────────
class ScoreRing(Flowable):
    """A circular score visual sized to fit on the cover page."""

    def __init__(self, score: int, size: float = 2.4 * inch):
        super().__init__()
        self.score = max(0, min(100, int(score or 0)))
        self.size = size
        self.width = size
        self.height = size

    def draw(self):
        c = self.canv
        col, soft, label = _band(self.score)
        cx = cy = self.size / 2.0
        r = self.size / 2.0 - 8
        # Background ring
        c.setStrokeColor(RULE)
        c.setLineWidth(8)
        c.circle(cx, cy, r, stroke=1, fill=0)
        # Coloured arc — reportlab arcs need a path
        c.setStrokeColor(col)
        c.setLineCap(1)  # round cap
        sweep = 360 * (self.score / 100.0)
        # Start at the top (90°) and go clockwise.
        path = c.beginPath()
        # Approximate the arc with line segments — small enough that
        # the eye sees a smooth curve (200 pts for the full sweep).
        steps = max(8, int(sweep / 1.8))
        for i in range(steps + 1):
            theta = math.radians(90 - (sweep * i / max(1, steps)))
            x = cx + r * math.cos(theta)
            y = cy + r * math.sin(theta)
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        c.drawPath(path, stroke=1, fill=0)
        # Big number
        c.setFillColor(INK)
        c.setFont(FONT_BOLD, 56)
        num = str(self.score)
        tw = c.stringWidth(num, FONT_BOLD, 56)
        c.drawString(cx - tw / 2.0, cy - 12, num)
        # "Health Score" label
        c.setFillColor(MUTED)
        c.setFont(FONT_REG, 8)
        sub = "HEALTH SCORE"
        tw = c.stringWidth(sub, FONT_REG, 8)
        c.drawString(cx - tw / 2.0, cy - 32, sub)
        # Band label below
        c.setFillColor(col)
        c.setFont(FONT_BOLD, 9)
        tw = c.stringWidth(label, FONT_BOLD, 9)
        c.drawString(cx - tw / 2.0, cy - 48, label)


# ── Horizontal rule flowable (orange brand accent) ──────────────────────────
class BrandRule(Flowable):
    def __init__(self, width: float = 2 * inch, color=BRAND_ORANGE,
                 thickness: float = 2.0):
        super().__init__()
        self.width = width
        self.height = thickness + 2
        self.color = color
        self.thickness = thickness

    def draw(self):
        c = self.canv
        c.setStrokeColor(self.color)
        c.setLineWidth(self.thickness)
        c.line(0, self.height / 2, self.width, self.height / 2)


# ── Page templates: cover (no header/footer) and content (with chrome) ──────
def _cover_page(canvas: _canvas.Canvas, doc):
    """Cover page chrome — minimal, clean."""
    canvas.saveState()
    # Top band (subtle orange)
    canvas.setFillColor(BRAND_ORANGE)
    canvas.rect(0, LETTER[1] - 6, LETTER[0], 6, stroke=0, fill=1)
    # Bottom slim line
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(0.75 * inch, 0.6 * inch, LETTER[0] - 0.75 * inch, 0.6 * inch)
    # Footer — left-aligned brand, right-aligned date
    canvas.setFillColor(MUTED)
    canvas.setFont(FONT_REG, 8)
    canvas.drawString(0.75 * inch, 0.42 * inch, "Foundry · Business Audit")
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    canvas.drawRightString(LETTER[0] - 0.75 * inch, 0.42 * inch, today)
    canvas.restoreState()


def _content_page(canvas: _canvas.Canvas, doc):
    """Header + footer for every content page after the cover."""
    canvas.saveState()
    # Header — slim brand bar
    canvas.setFillColor(BRAND_ORANGE)
    canvas.rect(0.75 * inch, LETTER[1] - 0.55 * inch, 0.4 * inch, 2, stroke=0, fill=1)
    canvas.setFillColor(INK)
    canvas.setFont(FONT_BOLD, 9)
    canvas.drawString(1.25 * inch, LETTER[1] - 0.55 * inch, "Foundry")
    canvas.setFillColor(MUTED)
    canvas.setFont(FONT_REG, 8)
    canvas.drawString(1.25 * inch, LETTER[1] - 0.7 * inch, "Business Audit")
    # Right side — business name (passed via doc attr)
    biz = getattr(doc, "_audit_brand", "")
    if biz:
        canvas.setFillColor(INK_SOFT)
        canvas.setFont(FONT_REG, 9)
        canvas.drawRightString(LETTER[0] - 0.75 * inch, LETTER[1] - 0.55 * inch, biz)
    # Footer — page number
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(0.75 * inch, 0.6 * inch, LETTER[0] - 0.75 * inch, 0.6 * inch)
    canvas.setFillColor(MUTED)
    canvas.setFont(FONT_REG, 8)
    canvas.drawString(0.75 * inch, 0.42 * inch, "Tamper-evident · Bitcoin-anchored proof of integrity")
    canvas.drawRightString(LETTER[0] - 0.75 * inch, 0.42 * inch, f"Page {doc.page}")
    canvas.restoreState()


# ── Card builders ────────────────────────────────────────────────────────────
def _eyebrow(text: str) -> Paragraph:
    style = ParagraphStyle(
        "Eyebrow", fontName=FONT_BOLD, fontSize=8,
        textColor=BRAND_ORANGE, leading=10, spaceAfter=4,
    )
    return Paragraph(text.upper(), style)


def _h1(text: str) -> Paragraph:
    style = ParagraphStyle(
        "H1", fontName=FONT_BOLD, fontSize=28,
        textColor=INK, leading=32, spaceAfter=4,
    )
    return Paragraph(_para_escape(text), style)


def _h2(text: str) -> Paragraph:
    style = ParagraphStyle(
        "H2", fontName=FONT_BOLD, fontSize=15,
        textColor=INK, leading=19, spaceBefore=10, spaceAfter=6,
    )
    return Paragraph(_para_escape(text), style)


def _h3(text: str, color=INK) -> Paragraph:
    style = ParagraphStyle(
        "H3", fontName=FONT_BOLD, fontSize=11.5,
        textColor=color, leading=15, spaceAfter=2,
    )
    return Paragraph(_para_escape(text), style)


def _body(text: str, color=INK_SOFT, size: float = 10) -> Paragraph:
    style = ParagraphStyle(
        "Body", fontName=FONT_REG, fontSize=size,
        textColor=color, leading=size * 1.45, spaceAfter=4,
    )
    return Paragraph(_para_escape(text or ""), style)


def _muted(text: str, italic: bool = False) -> Paragraph:
    style = ParagraphStyle(
        "Muted",
        fontName=FONT_ITALIC if italic else FONT_REG,
        fontSize=9, textColor=MUTED, leading=12, spaceAfter=2,
    )
    return Paragraph(_para_escape(text or ""), style)


def _card_table(content: list, *, bg=CARD_BG, border=RULE,
                pad: float = 12, content_width: float = 7 * inch) -> Table:
    """Wrap a list of flowables in a single-cell bordered card."""
    t = Table([[content]], colWidths=[content_width])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("BOX", (0, 0), (-1, -1), 0.6, border),
        ("LEFTPADDING", (0, 0), (-1, -1), pad),
        ("RIGHTPADDING", (0, 0), (-1, -1), pad),
        ("TOPPADDING", (0, 0), (-1, -1), pad),
        ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return t


def _priority_pill_paragraph(priority: str) -> Paragraph:
    """A small inline pill rendered via a Paragraph with background
    using HTML-style font tags (reportlab supports bgcolor)."""
    fg, bg, _ = _priority_band(priority)
    label = (priority or "—").upper()
    # Use reportlab-supported markup. We embed background via a font
    # backColor trick using a wrapper Paragraph in a small Table cell —
    # simpler to just style with bold uppercase text and a coloured
    # square unicode bullet to mirror the in-app pill.
    style = ParagraphStyle(
        "Pill", fontName=FONT_BOLD, fontSize=8,
        textColor=fg, leading=10, alignment=TA_LEFT,
    )
    return Paragraph(f"● {label}", style)


def _priority_pill_cell(priority: str) -> Table:
    """A small filled rectangle pill — coloured bg + matching text.
    Returned as a flowable (Table) so we can place it inline with the
    title via an outer Table row."""
    fg, bg, border = _priority_band(priority)
    label = (priority or "—").upper()
    inner_style = ParagraphStyle(
        "PillInner", fontName=FONT_BOLD, fontSize=7.5,
        textColor=fg, leading=9, alignment=TA_CENTER,
    )
    p = Paragraph(label, inner_style)
    t = Table([[p]], colWidths=[0.85 * inch], rowHeights=[0.21 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("BOX", (0, 0), (-1, -1), 0.5, border),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


# ── Main entry point ────────────────────────────────────────────────────────
def render_audit_pdf(audit: dict) -> bytes:
    """Render an audit dict to polished PDF bytes."""
    buf = io.BytesIO()
    doc = BaseDocTemplate(
        buf, pagesize=LETTER,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.95 * inch, bottomMargin=0.85 * inch,
        title=f"Business Audit — {audit.get('business_name','Audit')}",
        author="Foundry",
    )
    doc._audit_brand = audit.get("business_name") or ""

    # Cover frame uses the same usable area but no top/bottom chrome
    cover_frame = Frame(
        0.75 * inch, 0.7 * inch,
        LETTER[0] - 1.5 * inch, LETTER[1] - 1.4 * inch,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        id="cover",
    )
    content_frame = Frame(
        0.75 * inch, 0.7 * inch,
        LETTER[0] - 1.5 * inch, LETTER[1] - 1.55 * inch,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        id="content",
    )
    doc.addPageTemplates([
        PageTemplate(id="Cover", frames=[cover_frame], onPage=_cover_page),
        PageTemplate(id="Content", frames=[content_frame], onPage=_content_page),
    ])

    story: list = []
    _build_cover(story, audit)
    story.append(NextPageTemplate("Content"))
    story.append(PageBreak())
    _build_summary(story, audit)
    _build_strengths_issues(story, audit)
    _build_recommendations(story, audit)
    _build_brand_voice(story, audit)
    _build_sections(story, audit)
    _build_competitors(story, audit)
    _build_closing(story, audit)

    doc.build(story)
    return buf.getvalue()


def _build_cover(story: list, audit: dict):
    name = audit.get("business_name") or "Audit"
    url = audit.get("website_url") or ""
    score = int(audit.get("score") or 0)
    summary = audit.get("executive_summary") or audit.get("score_rationale") or ""

    story.append(Spacer(1, 1.2 * inch))
    story.append(_eyebrow("Business Audit Prepared For"))
    story.append(_h1(name))
    if url:
        story.append(_body(url, color=MUTED, size=10))
    story.append(Spacer(1, 0.45 * inch))
    # Centered score ring
    ring = ScoreRing(score, size=2.4 * inch)
    centered = Table([[ring]], colWidths=[7 * inch])
    centered.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER")]))
    story.append(centered)
    story.append(Spacer(1, 0.3 * inch))
    if summary:
        # Centered summary
        style = ParagraphStyle(
            "CoverSummary", fontName=FONT_REG, fontSize=11,
            textColor=INK_SOFT, leading=16, alignment=TA_CENTER,
        )
        story.append(Paragraph(_para_escape(summary), style))
    story.append(Spacer(1, 0.4 * inch))
    # Centered brand rule
    rule_table = Table([[BrandRule(width=1.6 * inch)]], colWidths=[7 * inch])
    rule_table.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER")]))
    story.append(rule_table)
    story.append(Spacer(1, 0.15 * inch))
    style = ParagraphStyle(
        "CoverFootline", fontName=FONT_REG, fontSize=9,
        textColor=MUTED, leading=12, alignment=TA_CENTER,
    )
    story.append(Paragraph(
        "Prepared by Foundry · "
        "Comprehensive read of website, social presence, reviews, "
        "brand voice, and competitive position.",
        style,
    ))


def _build_summary(story: list, audit: dict):
    rationale = audit.get("score_rationale") or ""
    if not rationale:
        return
    story.append(_eyebrow("Why this score"))
    story.append(_card_table([_body(rationale)], bg=BRAND_ORANGE_SOFT,
                             border=BRAND_ORANGE_BORDER))
    story.append(Spacer(1, 0.18 * inch))


def _build_strengths_issues(story: list, audit: dict):
    strengths = [s for s in (audit.get("strengths") or []) if isinstance(s, dict)]
    issues = [i for i in (audit.get("issues") or []) if isinstance(i, dict)]
    if not strengths and not issues:
        return

    def _list_card(title: str, items: list, accent, soft, border):
        flow: list = [
            _h3(title.upper(), color=accent),
            Spacer(1, 4),
        ]
        for it in items:
            flow.append(_h3(it.get("headline", ""), color=INK))
            if it.get("detail"):
                flow.append(_body(it.get("detail"), color=INK_SOFT, size=9.5))
            flow.append(Spacer(1, 4))
        return _card_table(flow, bg=soft, border=border, content_width=3.4 * inch)

    cells = []
    if strengths:
        cells.append(_list_card("Top Strengths", strengths,
                                GREEN, GREEN_SOFT, GREEN_BORDER))
    else:
        cells.append("")
    if issues:
        cells.append(_list_card("Critical Issues", issues,
                                RED, RED_SOFT, RED_BORDER))
    else:
        cells.append("")

    grid = Table([cells], colWidths=[3.5 * inch, 3.5 * inch])
    grid.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(grid)
    story.append(Spacer(1, 0.2 * inch))


def _build_recommendations(story: list, audit: dict):
    recs = [r for r in (audit.get("recommendations") or []) if isinstance(r, dict)]
    if not recs:
        return
    story.append(_eyebrow("Prioritized Recommendations"))
    story.append(_h2("The highest-leverage fixes"))
    story.append(_muted(
        "Each recommendation is paired with a concrete way SoulPrint Engine "
        "Pro helps you act on it — drafting, research, monitoring, or "
        "preparing the deliverable for you to ship.",
        italic=True,
    ))
    story.append(Spacer(1, 0.1 * inch))

    for idx, rec in enumerate(recs, 1):
        block: list = []
        # Title row: "1. Title"  +  PILL right-aligned
        title_p = _h3(f"{idx}. {rec.get('title','')}", color=INK)
        pill = _priority_pill_cell(rec.get("priority"))
        head = Table([[title_p, pill]],
                     colWidths=[5.45 * inch, 1.05 * inch])
        head.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        block.append(head)
        if rec.get("detail"):
            block.append(_body(rec.get("detail")))
        if rec.get("estimated_impact"):
            block.append(_muted(f"Expected impact: {rec.get('estimated_impact')}",
                                italic=True))
        spe = rec.get("soulprint_execution") or ""
        if spe:
            block.append(Spacer(1, 4))
            spe_inner = [
                Paragraph(
                    "HOW SOULPRINT ENGINE HELPS YOU ACT ON THIS",
                    ParagraphStyle(
                        "SpeLabel", fontName=FONT_BOLD, fontSize=7.5,
                        textColor=BRAND_ORANGE, leading=10, spaceAfter=2,
                    ),
                ),
                _body(spe, color=INK_SOFT, size=9.5),
            ]
            block.append(_card_table(spe_inner, bg=BRAND_ORANGE_SOFT,
                                     border=BRAND_ORANGE_BORDER, pad=10,
                                     content_width=6.5 * inch))
        story.append(KeepTogether(_card_table(
            block, bg=colors.white, border=RULE, pad=12,
        )))
        story.append(Spacer(1, 0.12 * inch))


def _build_brand_voice(story: list, audit: dict):
    bv = audit.get("brand_voice") or {}
    if not (bv.get("stated_mission") or bv.get("actual_voice") or bv.get("gap")):
        return
    story.append(_eyebrow("Brand Voice Read"))
    story.append(_h2("Stated mission vs. how the market hears it"))
    rows = [
        ("Stated mission", bv.get("stated_mission", "")),
        ("Actual voice", bv.get("actual_voice", "")),
        ("The gap", bv.get("gap", "")),
    ]
    flow: list = []
    for label, text in rows:
        if not text:
            continue
        flow.append(Paragraph(
            f"<b>{label}.</b> {_para_escape(text)}",
            ParagraphStyle(
                "BVRow", fontName=FONT_REG, fontSize=10,
                textColor=INK_SOFT, leading=15, spaceAfter=6,
            ),
        ))
    story.append(_card_table(flow, bg=CARD_BG, border=RULE))
    story.append(Spacer(1, 0.18 * inch))


def _build_sections(story: list, audit: dict):
    secs = audit.get("sections") or {}
    pairs = [
        ("Website analysis", secs.get("website_analysis")),
        ("Social presence", secs.get("social_presence")),
        ("Reviews summary", secs.get("reviews_summary")),
        ("Competitive context", secs.get("competitive_context")),
    ]
    pairs = [(t, b) for t, b in pairs if (b or "").strip()]
    if not pairs:
        return
    story.append(_eyebrow("Deeper Analysis"))
    story.append(_h2("Section-by-section read"))
    # Two-column grid
    cells = []
    for title, body in pairs:
        inner = [
            Paragraph(
                title.upper(),
                ParagraphStyle(
                    "SecLabel", fontName=FONT_BOLD, fontSize=8,
                    textColor=MUTED, leading=10, spaceAfter=4,
                ),
            ),
            _body(body, color=INK_SOFT, size=9.5),
        ]
        cells.append(_card_table(inner, bg=colors.white, border=RULE,
                                 content_width=3.4 * inch))
    # Pad to even number for grid layout
    if len(cells) % 2:
        cells.append("")
    rows = [cells[i:i + 2] for i in range(0, len(cells), 2)]
    grid = Table(rows, colWidths=[3.5 * inch, 3.5 * inch])
    grid.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(grid)
    story.append(Spacer(1, 0.18 * inch))


def _build_competitors(story: list, audit: dict):
    comps = [c for c in (audit.get("competitors") or []) if isinstance(c, dict)]
    if not comps:
        return
    story.append(_eyebrow("Competitive Landscape"))
    story.append(_h2("Direct competitors"))
    story.append(_muted("Three companies playing the same game — and where they're ahead.",
                        italic=True))
    story.append(Spacer(1, 0.1 * inch))
    for c in comps[:6]:
        flow = [
            _h3(c.get("name") or "—"),
        ]
        if c.get("url"):
            flow.append(_muted(c.get("url")))
        if c.get("edge"):
            flow.append(_body(c.get("edge"), size=9.5))
        story.append(_card_table(flow, bg=colors.white, border=RULE, pad=10))
        story.append(Spacer(1, 6))
    story.append(Spacer(1, 0.12 * inch))


def _build_closing(story: list, audit: dict):
    helps = (audit.get("how_soulprint_helps") or "").strip()
    if not helps:
        return
    story.append(Spacer(1, 0.1 * inch))
    inner = [
        Paragraph(
            "TACKLE THESE WITH SOULPRINT ENGINE PRO",
            ParagraphStyle(
                "ClosingEyebrow", fontName=FONT_BOLD, fontSize=9,
                textColor=BRAND_ORANGE, leading=12, spaceAfter=8,
            ),
        ),
        _body(helps, color=INK, size=11),
        Spacer(1, 8),
        Paragraph(
            "<b>Start free at pro.soulprintengine.ai</b>",
            ParagraphStyle(
                "ClosingCta", fontName=FONT_BOLD, fontSize=11,
                textColor=BRAND_ORANGE, leading=14,
            ),
        ),
    ]
    story.append(_card_table(inner, bg=BRAND_ORANGE_SOFT,
                             border=BRAND_ORANGE_BORDER, pad=18))


__all__ = ["render_audit_pdf"]
