"""Business Audit prospecting tool — superadmin only.

Workflow:
  1. Superadmin POSTs business name + website (+ optional socials) to
     /api/admin/audits.
  2. Backend kicks off an asyncio background task that:
       a. Fetches the website (Playwright) for hero text + brand voice
       b. Brave-searches "{brand}", "{brand} reviews", "{brand} on
          [linkedin|twitter|...]", "{brand} vs", "{brand} competitors"
       c. Hands the gathered data + a strict JSON schema to an LLM
          (Claude Sonnet 4.5 via the Emergent universal key) which
          synthesises the audit
       d. Renders a polished PDF using the existing reportlab helper
          and persists it to GridFS
  3. While generating, the audit row carries `status: "generating"`.
     The admin panel polls /api/admin/audits/{id} every 5s and shows
     a progress badge; the public page renders a "Coming soon — your
     audit is being prepared" placeholder.
  4. Once status flips to "ready", the public `/audit/{slug}` page
     renders a teaser (score + 3 strengths + 3 issues, blurred) and
     a lead form (name, email, phone, company, role). On submit, the
     full body unlocks and the PDF download appears. The admin gets
     a Resend email notification with the lead's contact details.

Security model:
  • All admin endpoints are gated by `require_superadmin`.
  • Public endpoints expose ONLY the teaser fields and the slug
    (no database ids, no admin metadata, no lead list).
  • PDF download requires the lead_id returned from /lead — anyone
    with a lead_id can re-download (acceptable; that's the prospect's
    own contact card). No cross-audit leakage.
  • Rate-limited at the public /lead endpoint (in-memory cache; see
    `_RECENT_LEAD_IPS`). Genuine prevention belongs at the ingress
    layer; this is a courtesy speed-bump.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from bson import ObjectId
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from perplexity_client import perplexity_company_summary

logger = logging.getLogger(__name__)


# Patterns that signal "we got an interstitial / bot-challenge page,
# not the real site". Matched case-insensitively against the rendered
# text; if any hits AND the text is short, we treat the fetch as failed
# and route to the Perplexity fallback.
_BOT_CHALLENGE_MARKERS = (
    "robot challenge screen",
    "checking the site connection security",
    "checking your browser before accessing",
    "just a moment",
    "please verify you are human",
    "enable javascript and cookies",
    "cf-browser-verification",
    "/cdn-cgi/",
    "pardon our interruption",
    "access denied",
    "attention required",
    "ddos protection by",
    "this page requires cookies",
)


def _looks_blocked(text: str) -> bool:
    """Return True if the text looks like a Cloudflare / bot-challenge
    interstitial rather than real page content. We require BOTH a marker
    AND a short body so a long real page that happens to contain the
    word "cookies" doesn't trip the detector.
    """
    if not text:
        return True
    body = text.lower()
    if len(body) < 600 and any(m in body for m in _BOT_CHALLENGE_MARKERS):
        return True
    # Sites that returned only nav-chrome (very short) — not blocked
    # per se, but the LLM won't get useful signal from <300 chars.
    if len(body) < 300:
        return True
    return False


# ── Pydantic bodies ──────────────────────────────────────────────────────────
class AuditCreateBody(BaseModel):
    business_name: str = Field(..., min_length=1, max_length=200)
    website_url: str = Field(..., min_length=1, max_length=400)
    socials: Optional[dict] = None  # {linkedin?, twitter?, instagram?, facebook?, tiktok?, youtube?}
    hint: Optional[str] = Field(default="", max_length=600)


class LeadSubmitBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    email: EmailStr
    phone: str = Field(..., min_length=4, max_length=40)
    # Company / role were originally required. We now collect only the
    # essentials (name, email, phone) so prospects unlock the audit
    # faster — every extra field measurably drops conversion. Both
    # are kept on the schema as optional in case a future admin tool
    # wants to volunteer them.
    company: str = Field("", max_length=160)
    role: str = Field("", max_length=120)


class PublicAuditStartBody(BaseModel):
    """Body for the homepage self-serve audit form."""
    business_name: str = Field(..., min_length=1, max_length=200)
    website_url: str = Field(..., min_length=1, max_length=400)
    name: str = Field(..., min_length=1, max_length=120)
    email: EmailStr
    # Phone optional — the homepage form reduces to just the essentials
    # to maximise conversion. Admin can chase phone numbers separately.
    phone: str = Field("", max_length=40)
    company: str = Field("", max_length=160)
    role: str = Field("", max_length=120)
    hint: Optional[str] = Field(default="", max_length=600)


class InAppAuditBody(BaseModel):
    """Body for logged-in users running audits inside the app —
    org-scoped, no lead capture."""
    org_id: str = Field(..., min_length=1, max_length=64)
    business_name: str = Field(..., min_length=1, max_length=200)
    website_url: str = Field(..., min_length=1, max_length=400)
    socials: Optional[dict] = None
    hint: Optional[str] = Field(default="", max_length=600)


# ── Helpers ──────────────────────────────────────────────────────────────────
_SLUG_NOISE_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str, *, suffix_len: int = 5) -> str:
    base = _SLUG_NOISE_RE.sub("-", (name or "").strip().lower()).strip("-")[:48]
    if not base:
        base = "audit"
    # Always append a short random suffix so slugs are stable AND unique even
    # when two admins audit the same brand. Keeps URLs short + share-friendly.
    return f"{base}-{secrets.token_hex(suffix_len // 2 + 1)[:suffix_len]}"


def _normalise_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return u


# Best-effort in-memory throttle for the public /lead endpoint. 5 submits per
# IP per 60s is plenty for legit prospects and stops trivial bot floods. Real
# protection lives at the ingress layer.
_RECENT_LEAD_IPS: dict[str, list[float]] = {}


def _rate_limit_ok(ip: str, *, window: int = 60, limit: int = 5) -> bool:
    now = time.time()
    bucket = _RECENT_LEAD_IPS.setdefault(ip, [])
    # Drop expired
    bucket[:] = [t for t in bucket if now - t < window]
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


# ── Public Resend helper ─────────────────────────────────────────────────────
async def _send_resend_email(to: str, subject: str, html: str, text: str,
                             *, attachments: list[dict] | None = None) -> bool:
    """POST to Resend's /emails endpoint. Returns True on success.

    `attachments` supports Resend's native attachment shape:
    `[{"filename": "...", "content": "<base64>"}]`. Use `_attach_pdf()`
    to build it from raw bytes.
    """
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    sender = os.environ.get("RESEND_FROM", "").strip()
    if not api_key or not sender:
        logger.info("Resend not configured — skipping email to %s", to)
        return False
    payload = {
        "from": sender, "to": [to], "subject": subject,
        "html": html, "text": text,
    }
    if attachments:
        payload["attachments"] = attachments
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if resp.status_code >= 300:
            logger.warning("Resend send failed %s: %s", resp.status_code, resp.text[:300])
            return False
        return True
    except Exception as exc:
        logger.warning("Resend send exception: %s", exc)
        return False


def _attach_pdf(pdf_bytes: bytes, filename: str) -> dict:
    """Wrap raw PDF bytes into Resend's attachment shape (base64 content)."""
    import base64
    return {
        "filename": filename,
        "content": base64.b64encode(pdf_bytes).decode("ascii"),
    }


# ── Audit serializers ────────────────────────────────────────────────────────
def _full_serialize(a: dict) -> dict:
    """Admin view — everything in the row (excluding _id raw)."""
    if not a:
        return {}
    out = {k: v for k, v in a.items() if k != "_id"}
    out["id"] = str(a["_id"])
    if isinstance(out.get("created_at"), datetime):
        out["created_at"] = out["created_at"].isoformat()
    if isinstance(out.get("updated_at"), datetime):
        out["updated_at"] = out["updated_at"].isoformat()
    return out


def _public_teaser(a: dict) -> dict:
    """Public view — score + first 3 strengths + first 3 issues (no detail)."""
    return {
        "slug": a.get("slug"),
        "business_name": a.get("business_name"),
        "website_url": a.get("website_url"),
        "status": a.get("status", "ready"),
        "score": a.get("score"),
        "strengths_teaser": [s.get("headline") if isinstance(s, dict) else str(s)
                             for s in (a.get("strengths") or [])[:3]],
        "issues_teaser": [i.get("headline") if isinstance(i, dict) else str(i)
                          for i in (a.get("issues") or [])[:3]],
        # Cosmetic only — gives the public page a one-line headline.
        "executive_summary": (a.get("executive_summary") or "")[:280],
    }


def _public_full(a: dict) -> dict:
    """Public view AFTER lead capture — everything except admin metadata."""
    keys = (
        "slug", "business_name", "website_url", "status",
        "score", "strengths", "issues", "recommendations",
        "competitors", "brand_voice", "sections",
        "how_soulprint_helps",
        "executive_summary", "pdf_filename",
        "website_fetch_blocked",
        "tech_signals",
    )
    out = {k: a.get(k) for k in keys}
    return out


# ── LLM audit synthesis ──────────────────────────────────────────────────────
_AUDIT_SYSTEM_PROMPT = """You are Foundry's Senior Brand & Growth Auditor — a hybrid of a McKinsey-style strategist, a senior conversion-rate optimiser, and a brand voice expert. You produce honest, prospect-grade audits that win business by being specific, useful, and slightly uncomfortable.

You will receive raw data the team has gathered:
  • The business's website rendered text (hero, copy, calls-to-action)
  • Brave Search results for the brand (recent mentions, news, citations)
  • Brave Search results for "{brand} reviews" (Google, Yelp, Trustpilot, BBB, etc.)
  • Brave Search results for "{brand} competitors" / "{brand} vs"
  • Optional social profile URLs the admin provided
  • ``tech_signals`` — HARD DATA gathered by our own crawlers, NOT LLM judgement:
      - ``web_vitals`` — Google PageSpeed Insights v5 for both mobile + desktop:
        performance score 0-100, LCP (ms), INP (ms), CLS, TTFB (ms), FCP (ms),
        total-blocking-time (ms), speed-index (ms). Mobile is Google's
        primary ranking signal. Cite the actual numbers in findings —
        these are the most defensible signals in the entire audit.
      - ``tracking_pixels`` — list of installed analytics / ad / chat /
        marketing-automation tags actually found in the rendered HTML
        (GA4, GTM, Meta Pixel, LinkedIn Insight, TikTok, Hotjar, Clarity,
        Intercom, HubSpot, etc.) PLUS a ``gaps`` list calling out what's
        missing. Use these to make recommendations real and specific.
      - ``domain_reputation`` — SPF, DKIM (common selectors), DMARC presence
        + policy, plus an ``issues`` list. Email security is a high-priority
        finding most prospects don't know they have.
      - ``screenshots_captured`` — a boolean telling you whether the audit
        page will embed mobile / tablet / desktop screenshots. You don't
        need to describe them, but you SHOULD reference responsive-design
        observations when they're available.

═══════════════════════════════════════════════════════════════════════
PLATFORM CAPABILITY MAP — what Foundry can actually do
═══════════════════════════════════════════════════════════════════════
This audit is delivered through Foundry. EVERY
recommendation you make MUST include a ``soulprint_execution`` field
that explains — concretely, in 1–2 sentences — exactly how Foundry
helps the user act on the fix (which agent drafts it, which mission
researches it, which feature schedules it). Use only the capabilities
listed below; do NOT invent features that don't exist.

Be honest about the boundary: Foundry PRODUCES the work product
(drafts, briefs, plans, research, reports, monitoring); the user or
their team is the one who clicks publish, files paperwork, books the
meeting, etc. A recommendation like "redirect 301 your /old to /new"
isn't something Foundry literally does — but Foundry can DRAFT the
exact .htaccess block, EXPLAIN what to paste where, and MONITOR the
result. Frame it that way.

Foundry is a B2B platform (built by ArcheForge) that lets a company:

A. **Multi-agent Org Workspace** — every business creates an
   "Organization" with named Teams (Marketing, Sales, Ops, etc.).
   Each Team gets a roster of specialist AI agents (150+ catalogue:
   Brand Voice Engineer, Conversion Copywriter, SEO Strategist,
   Customer Insights Analyst, Reviews Therapist, Competitive
   Researcher, etc.) drawing on the org's Soul (mission, voice,
   audience, values).

B. **Brand Soul** — every org defines a "Soul" (mission, tone, audience,
   values). This is automatically prepended to every agent's system
   prompt so output stays on-brand without re-explaining.

C. **Mission Mode** — autonomous multi-step agent loops that run for
   hours or days. Used for: weekly competitive scans, review
   monitoring, content batch generation, lead-qualification scoring,
   brand-mention digests. The mission engine has access to web search
   (Brave + Perplexity), browser fetch (Playwright), PDF rendering,
   and the agent catalogue.

D. **Social Queue** — auto-drafts on-brand social posts for LinkedIn,
   Twitter, Instagram, etc. Each post gets an AI-generated cover image
   (Flux Kontext Pro) with the company logo composited in any corner.
   Posts can be scheduled, edited inline, or auto-published.

E. **Dynamic Intelligence Routing** — the platform auto-picks the
   best LLM for each task (gpt-5.1 for synthesis, gpt-4.1-mini for
   batch drafts, Claude / Gemini when stronger, Suno for music, Nano
   Banana for image edits). Means the user never has to think about
   model selection.

F. **Document Library + PDF/CSV Generation** — every artifact (briefs,
   one-pagers, comparison tables, plans, press releases) is rendered
   as a polished, branded PDF or CSV and stored in the org's Document
   Library, fully searchable.

G. **Real-time Web Search & Browser Tools** — agents can search the
   web (Brave / Perplexity) and fetch + render arbitrary URLs
   (Playwright) so they reason on FRESH data, not stale training cuts.

H. **Brand Assets + Logo Library** — uploaded logos / colors / fonts
   are auto-applied to every generated image, PDF, and social post.

I. **Org Mind Map** — visual brand architecture diagram showing how
   the Soul, Teams, agents, and recent missions connect.

J. **Image / Video / Music Generation** — covers, ads, animated
   explainers, jingles, voiceovers — all on-brand, all in one place.

K. **Custom Agents** — admins can author new specialist agents that
   inherit the org's Soul + sit in any Team's roster.

═══════════════════════════════════════════════════════════════════════
HOW TO MAP RECOMMENDATIONS TO PLATFORM CAPABILITIES
═══════════════════════════════════════════════════════════════════════
For every recommendation, choose the most direct path Foundry can help
with. Be specific — name the agent, the mission, or the feature. Be
honest about the boundary: Foundry DRAFTS, BRIEFS, RESEARCHES, PLANS,
and MONITORS; the user (or their team) is the one who hits publish,
files paperwork, books the meeting, makes the phone call. Words like
"drafts", "surfaces", "monitors", "briefs", "schedules a draft",
"prepares" are GOOD. Words like "executes", "ships", "publishes
automatically", "fixes for you" overpromise and should be avoided
unless the action genuinely happens autonomously inside Foundry
(e.g. social posts via the Social Queue with auto-publish on, which
the user explicitly opts into).

Examples of good ``soulprint_execution`` text:

  • "Foundry's **Conversion Copywriter agent** drafts a new hero
     using your Brand Soul + the top phrases from your G2 reviews
     (pulled in by the Reviews Listener mission). You review and
     paste into your CMS."

  • "Stand up a recurring **Reviews Monitor mission** that scans
     Google + Trustpilot weekly, pings your Slack when sentiment
     drops, and drafts a public response in your tone for you to
     approve and post."

  • "Use the **Social Queue** to keep a LinkedIn cadence ready —
     Foundry drafts 5 posts a week from your latest customer wins
     and generates the cover images. With auto-publish on you can
     have it post directly; otherwise it queues drafts for your
     30-second approval."

  • "Spin up a **'Pricing comparison' brief mission** — the
     Competitive Researcher agent scans 3 named competitors weekly
     and produces a PDF you can drop into sales decks."

═══════════════════════════════════════════════════════════════════════
PLATFORM NAMING — CRITICAL
═══════════════════════════════════════════════════════════════════════
The platform's name is **Foundry**. Always write "Foundry" in every
user-facing string — recommendations, executive summary, brand-voice
read, sections, the closing pitch, agent-citation examples, anywhere.
NEVER use the words "SoulPrint" or "SoulPrint Engine" anywhere in the
generated audit text. The field name ``soulprint_execution`` is an
internal schema key only — its VALUE must say "Foundry", never
"SoulPrint".

═══════════════════════════════════════════════════════════════════════
JSON SCHEMA
═══════════════════════════════════════════════════════════════════════

Synthesize a structured audit and return ONLY valid JSON matching this schema:

{
  "executive_summary": "2-3 sentence at-a-glance — concrete, no jargon",
  "score": 0-100,            // 0=disaster, 50=mediocre, 70=solid, 85+=excellent
  "score_rationale": "1 sentence explaining the score",
  "strengths": [             // exactly 3 — the BEST things they're doing
    {"headline": "Punchy 4-8 word title", "detail": "2-3 sentences with a concrete observation. Cite specific copy, page, or pattern."}
  ],
  "issues": [                // exactly 3 — the highest-impact problems
    {"headline": "4-8 word title", "detail": "2-3 sentences naming the problem and its likely impact on prospects."}
  ],
  "recommendations": [       // exactly 5-8 prioritized fixes
    {
      "priority": "high|medium|low",
      "title": "Action verb + specific change",
      "detail": "What to do, why it works, how to measure success.",
      "estimated_impact": "1 sentence: the lift they should expect.",
      "soulprint_execution": "1-2 sentences: how Foundry helps the user act on this — which agent or mission would brief, draft, plan, or research it. Be honest about the boundary: Foundry produces the playbook, copy, brief, or analysis; the user (or their team) is the one who hits publish, files the form, books the meeting, etc. Reference at least one named agent or mission. Never imply Foundry autonomously ships work to live customer-facing channels unless that's literally the case. Do NOT invent features."
    }
  ],
  "competitors": [           // exactly 3 — named direct competitors
    {"name": "Competitor name", "url": "domain.com or full URL", "edge": "1 sentence: what they do better than the audited business."}
  ],
  "brand_voice": {
    "stated_mission": "What they SAY they're about (quote/paraphrase from site)",
    "actual_voice": "How their copy + reviews actually FEEL — tone words",
    "gap": "Where the stated and actual diverge — be specific."
  },
  "sections": {
    "website_analysis": "Paragraph: what works, what doesn't, conversion path quality.",
    "social_presence": "Paragraph: which platforms, frequency hint, brand consistency. If you can't find them, say so plainly.",
    "reviews_summary": "Paragraph: sentiment + 2-3 specific recurring complaints or themes (cite review excerpts when available).",
    "competitive_context": "Paragraph: where they sit vs the 3 named competitors."
  },
  "how_soulprint_helps": "3-4 sentence closing pitch: tie the audit's findings together and explain how Foundry helps the user act on the recommended fixes — by drafting the copy, building the briefs, surfacing the research, and keeping the playbooks current. Be honest about the split: Foundry prepares the work, the user (or their team) ships it. Reference 2-3 specific Foundry features by name. End with a soft CTA inviting them to start with Foundry. Do NOT use marketing fluff — be specific and grounded in THEIR audit."
}

Rules:
  • If a data source is empty/missing, acknowledge it ("No public reviews surfaced in our scan…") rather than fabricating.
  • Whenever ``tech_signals`` provides a hard number (Core Web Vitals score, missing DMARC, no Meta Pixel detected, etc.) prefer it over LLM-judgement. Cite the actual number or fact verbatim — e.g. "Your mobile PageSpeed score is 38 with an LCP of 4,800 ms — search ranking and conversion both suffer." Hard data outranks vibe.
  • If ``tech_signals.web_vitals.available`` is true and the mobile performance score is below 60, you MUST add an issue with priority "high" titled with the exact score (e.g. "Mobile PageSpeed score: 38 — slow site costs conversions and rankings").
  • If ``tech_signals.tracking_pixels.gaps`` is non-empty, surface those gaps as findings. They're the difference between "you guess what works" and "you measure what works".
  • If ``tech_signals.domain_reputation.issues`` is non-empty, add a "Email & domain security" finding citing the missing record(s). Phishing impersonation and email deliverability are real revenue risks.
  • The input includes ``perplexity_company_summary`` — Perplexity Sonar's live web research on the business. Treat it as ADDITIVE CONTEXT (market position, what they actually do, recent mentions) — NOT as a substitute for verbatim on-page copy. When grading voice/tone/CTAs, cite the ``website_rendered_text_excerpt`` first; only fall back to the Perplexity summary when on-page text is empty or shows a bot-challenge wall.
  • The input ALSO includes ``website_fetch_blocked`` — a boolean. When TRUE, the site sits behind anti-bot protection (Cloudflare, DataDome, Akamai, PerimeterX) and our crawler could not read the real homepage. In that case:
       — You MUST NOT invent copy-level findings ("sparse hero", "weak CTA", "confusing nav") — you did not see the page.
       — Open the ``executive_summary`` with one sentence acknowledging the wall: "We could not crawl {website_url} directly — it's protected by anti-bot software — so this audit is built from Perplexity's live web research, third-party citations, and review signals."
       — Add ONE issue with priority "medium" titled "Anti-bot protection blocks crawlers (including ours)" explaining the conversion + SEO implication (some legitimate prospects, comparison shoppers, and AI search agents will be blocked too).
       — Lean on Perplexity + Brave brand/reviews search for positioning, voice signals, and competitive context.
       — Every other section (recommendations, competitors, brand voice) must STILL be specific and useful, just sourced from web research instead of homepage copy.
  • If ``website_rendered_text_excerpt`` is NOT empty but looks like a Cloudflare / bot-verification interstitial (e.g. "Verifying that you are not a robot", "Just a moment", "Checking your browser"), treat it the same as the blocked case above.
  • Cite specific copy, URLs, or themes when possible.
  • Be specific over diplomatic — this is a paid audit. Vague platitudes lose deals.
  • Score must reflect reality, not flattery.
  • EVERY recommendation MUST have a ``soulprint_execution`` field. Skipping it breaks the audit pipeline.
  • Output ONLY the JSON, no preamble, no markdown fences.
"""


async def _synthesise_audit_via_llm(
    *, LlmChat, UserMessage, EMERGENT_LLM_KEY: str,
    business_name: str, website_url: str,
    website_text: str, brand_search: list, reviews_search: list,
    competitors_search: list, socials: dict, hint: str,
    perplexity_summary: str = "",
    website_blocked: bool = False,
    tech_signals: Optional[dict] = None,
):
    """Call the audit synthesis model and parse its JSON.

    Returns ``(parsed_audit_dict, ModelChoice)`` — the model choice is
    persisted on the audit row so the admin can see (and the public page
    can display, if we ever surface it) which model produced the analysis.
    Raises on structural failure.
    """
    from dynamic_intelligence import pick_text_model, TASK_AUDIT_SYNTHESIS
    choice = pick_text_model(TASK_AUDIT_SYNTHESIS)

    # Trim the screenshots / pixel evidence so the prompt stays compact —
    # the LLM only needs the SUMMARY of each tech signal, not the raw URLs.
    ts = tech_signals or {}
    compact_signals = {
        "web_vitals": ts.get("web_vitals") or {"available": False},
        "tracking_pixels": ts.get("tracking_pixels") or {"available": False},
        "domain_reputation": {
            k: v for k, v in (ts.get("domain_reputation") or {}).items()
            if k != "mx"   # the MX list isn't useful to the analyst
        },
        # Screenshots are linked into the report by the UI/PDF directly;
        # we don't need the LLM to "describe" them.
        "screenshots_captured": bool(
            (ts.get("screenshots") or {}).get("available")
        ),
    }

    user_text = json.dumps({
        "business_name": business_name,
        "website_url": website_url,
        "admin_hint": hint,
        "socials_provided": socials or {},
        "website_rendered_text_excerpt": (website_text or "")[:8000],
        # When the on-page fetch was blocked by anti-bot protection, the
        # excerpt above is intentionally empty. The synthesizer must NOT
        # invent "weak copy / sparse hero" findings in that case — it
        # leans on Perplexity for positioning and uses Brave snippets for
        # voice signals. The flag below tells it exactly that.
        "website_fetch_blocked": bool(website_blocked),
        # Perplexity Sonar summary — independent of the page scrape, so
        # it's useful even when Playwright succeeded (real-time market
        # context, customer mentions, positioning) and CRITICAL when the
        # site cloaks bots. The synthesizer treats this as background
        # context, not as a substitute for verbatim on-page copy.
        "perplexity_company_summary": (perplexity_summary or "")[:6000],
        "brave_brand_search": brand_search[:8],
        "brave_reviews_search": reviews_search[:8],
        "brave_competitors_search": competitors_search[:8],
        # Hard-data signals — actual numbers from PageSpeed Insights,
        # actual DNS records, actual tracking pixels in their DOM.
        # These are precisely the signals a chat agent CAN'T produce,
        # and they should drive the highest-priority recommendations.
        "tech_signals": compact_signals,
    }, ensure_ascii=False)

    chat = LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=f"audit-{uuid.uuid4().hex[:12]}",
        system_message=_AUDIT_SYSTEM_PROMPT,
    ).with_model(choice.provider, choice.model)
    raw = await chat.send_message(UserMessage(text=user_text))
    text = (raw or "").strip()
    # Strip accidental ```json fences
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text, flags=re.I | re.M)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Audit LLM returned non-JSON: {exc}") from exc
    data = _scrub_platform_name(data)
    return data, choice


# ── Defensive platform-name scrub ───────────────────────────────────────────
# The audit synthesis prompt explicitly tells the LLM to write "Foundry",
# but some models (especially older Gemini / Claude variants) still leak
# the legacy "SoulPrint" / "SoulPrint Engine" name from their training
# data when they cite the platform in recommendations or the closing
# pitch. We sweep every user-visible string field after JSON parse and
# replace the legacy name with "Foundry" so users never see it.
# Schema-internal keys like ``soulprint_execution`` / ``how_soulprint_helps``
# are deliberately left untouched — they're persisted by name and renaming
# would orphan every existing audit row.
def _scrub_platform_name(node):
    if isinstance(node, dict):
        return {k: _scrub_platform_name(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_scrub_platform_name(x) for x in node]
    if isinstance(node, str):
        # Order matters — strip the longer marketing form before the
        # bare brand so "SoulPrint Engine" doesn't degrade to
        # "Foundry Engine". Possessive forms (curly or straight quote)
        # are preserved as "Foundry's" to keep recommendation copy
        # natural ("Have Foundry's Copywriter agent draft …").
        out = re.sub(r"SoulPrint\s+Engine", "Foundry", node, flags=re.I)
        out = re.sub(r"SoulPrint['\u2019]s", "Foundry's", out, flags=re.I)
        out = re.sub(r"SoulPrints?", "Foundry", out, flags=re.I)
        return out
    return node


# ── PDF rendering (uses existing reportlab helper) ───────────────────────────
def _audit_to_markdown(a: dict) -> str:
    """Render audit dict → Markdown body the existing PDF renderer handles."""
    name = a.get("business_name") or "Audit"
    score = a.get("score", "—")
    rationale = a.get("score_rationale", "")
    summary = a.get("executive_summary", "")
    bv = a.get("brand_voice") or {}
    secs = a.get("sections") or {}
    out = []
    out.append(f"# Business Audit — {name}\n")
    out.append(f"**Health Score:** {score} / 100 — {rationale}\n")
    if summary:
        out.append(f"\n_{summary}_\n")
    out.append("\n## Strengths\n")
    for s in (a.get("strengths") or []):
        if isinstance(s, dict):
            out.append(f"\n**{s.get('headline','')}**  \n{s.get('detail','')}")
    out.append("\n\n## Issues\n")
    for i in (a.get("issues") or []):
        if isinstance(i, dict):
            out.append(f"\n**{i.get('headline','')}**  \n{i.get('detail','')}")
    out.append("\n\n## Prioritized Recommendations\n")
    for idx, rec in enumerate(a.get("recommendations") or [], 1):
        if isinstance(rec, dict):
            out.append(
                f"\n**{idx}. [{rec.get('priority','').upper()}] {rec.get('title','')}**  \n"
                f"{rec.get('detail','')}  \n"
                f"_Expected impact: {rec.get('estimated_impact','')}_\n"
            )
            # Execution hook — explicitly tied to a Foundry
            # Pro feature so the prospect sees the natural next step.
            spe = rec.get("soulprint_execution") or ""
            if spe:
                out.append(f"\n**↳ How Foundry can help:** {spe}\n")
    out.append("\n## Brand Voice Read\n")
    out.append(f"\n- **Stated mission:** {bv.get('stated_mission','')}")
    out.append(f"\n- **Actual voice:** {bv.get('actual_voice','')}")
    out.append(f"\n- **Gap:** {bv.get('gap','')}")
    out.append("\n\n## Website Analysis\n")
    out.append(f"\n{secs.get('website_analysis','')}")
    out.append("\n\n## Social Presence\n")
    out.append(f"\n{secs.get('social_presence','')}")
    out.append("\n\n## Reviews Summary\n")
    out.append(f"\n{secs.get('reviews_summary','')}")
    out.append("\n\n## Competitive Context\n")
    out.append(f"\n{secs.get('competitive_context','')}")
    out.append("\n\n## Direct Competitors\n")
    for c in (a.get("competitors") or []):
        if isinstance(c, dict):
            out.append(f"\n- **{c.get('name','')}** ({c.get('url','')}) — {c.get('edge','')}")
    # Closing pitch — ties the audit to the platform and prompts the
    # prospect toward the natural next step (booking a Foundry setup).
    helps = a.get("how_soulprint_helps") or ""
    if helps:
        out.append("\n\n## Tackle These Fixes With Foundry\n")
        out.append(f"\n{helps}\n")
    out.append("\n\n---\n_Prepared by Foundry._")
    return "".join(out)


# ── Router factory ───────────────────────────────────────────────────────────
def make_audits_router(
    *,
    db,
    get_current_user,
    require_superadmin,
    require_org_member,
    LlmChat,
    UserMessage,
    EMERGENT_LLM_KEY: str,
    brave_web_search,
    browse_url,
    persist_bytes,
    upload_dir: Path,
    render_pdf,
):
    """Three routers — admin (gated), public (lead-capture), in-app (auth + org).

    Wired into FastAPI by importing from server.py and including all three.
    Returns ``(admin_router, public_router, app_router)``.
    """
    admin_r = APIRouter(prefix="/api/admin")
    public_r = APIRouter(prefix="/api/public")
    app_r = APIRouter(prefix="/api/audits")

    # ── Audit generation pipeline ──────────────────────────────────────────
    async def _run_audit(audit_id):
        try:
            a = await db.business_audits.find_one({"_id": audit_id})
            if not a:
                return
            url = _normalise_url(a.get("website_url", ""))
            brand = a.get("business_name", "")

            # Stage A — gather (parallel)
            async def _safe_brave(q: str):
                try:
                    return await brave_web_search(q, count=8)
                except Exception as exc:
                    logger.warning("brave search %r failed: %s", q, exc)
                    return []

            async def _safe_browse():
                """Fetch on-page text via Playwright. Returns
                ``(text, blocked)`` — when the site is behind anti-bot
                protection we surface that as a separate flag and
                deliberately blank the text so the synthesizer prompt's
                "fall back to Perplexity" branch fires instead of trying
                to grade the audit from a 180-character interstitial.
                """
                if not url:
                    return "", False
                try:
                    snap = await browse_url(url, take_screenshot=False)
                except Exception as exc:
                    logger.warning("browse %s failed: %s", url, exc)
                    return "", False
                text = (snap or {}).get("text", "") or ""
                # `browse_url` flips `bot_challenge` when its own sniffer
                # finds challenge markers. We also re-check with our
                # local heuristic so audits get a unified "blocked"
                # signal even if upstream missed a marker.
                blocked = bool((snap or {}).get("bot_challenge")) or _looks_blocked(text)
                if blocked:
                    return "", True
                return text, False

            async def _safe_perplexity():
                """Always-on Perplexity Sonar summary. Independent of
                the on-page scrape: provides market context + customer
                mentions + real-time positioning even when Playwright
                succeeds, AND is the only available signal when the site
                cloaks bots.
                """
                if not url and not brand:
                    return ""
                try:
                    return await perplexity_company_summary(brand, url) or ""
                except Exception as exc:
                    logger.warning("Perplexity summary failed for %s: %s", url, exc)
                    return ""

            # All five fetches run in parallel — total wall time is the
            # slowest one, not the sum. Adds four hard-data signals
            # alongside the LLM-grade inputs: Core Web Vitals, multi-
            # viewport screenshots, tracking-pixel inventory, and email
            # domain reputation. These are precisely the signals a chat
            # agent CAN'T produce in a 30-second response, and they're
            # what justifies the audit as a paid deliverable.
            from audit_signals import (
                fetch_web_vitals, capture_viewport_screenshots,
                inventory_tracking_pixels, domain_reputation,
            )

            # Per-stage timeouts so a single hung collector can't eat
            # the entire watchdog budget. Each ceiling is generous
            # vs typical wall time but tight enough to leave room for
            # the synthesis LLM pass that follows.
            async def _bounded(coro, ceiling: float, name: str):
                try:
                    return await asyncio.wait_for(coro, timeout=ceiling)
                except asyncio.TimeoutError:
                    logger.info(
                        "audit signal '%s' exceeded %.1fs — dropping", name, ceiling
                    )
                    return {"available": False, "error": f"{name} timed out after {ceiling}s"}

            async def _no_url(_msg: str):
                return {"available": False, "error": _msg}

            (
                browse_result, perplexity_text,
                brand_hits, reviews_hits, comp_hits,
                web_vitals, screenshots, domain_rep,
            ) = await asyncio.gather(
                _safe_browse(),
                _safe_perplexity(),
                _safe_brave(brand),
                _safe_brave(f"{brand} reviews"),
                _safe_brave(f"{brand} competitors OR alternatives"),
                _bounded(fetch_web_vitals(url), 20.0, "web_vitals") if url else _no_url("no url"),
                _bounded(
                    capture_viewport_screenshots(
                        url, persist_bytes=persist_bytes,
                        upload_dir=upload_dir, audit_id=str(audit_id),
                    ), 35.0, "viewport_screenshots",
                ) if url else _no_url("no url"),
                _bounded(domain_reputation(url), 10.0, "domain_reputation") if url else _no_url("no url"),
            )
            website_text, website_blocked = browse_result
            # Pixel inventory works off the rendered HTML, so it only fires
            # when the browse succeeded. We fetch the raw HTML once via
            # httpx (much cheaper than a second Playwright run) — that's
            # enough since pixels are inserted as `<script src=...>` tags
            # that are present in the static HTML.
            pixel_inventory: Dict[str, Any]
            if website_text and not website_blocked:
                try:
                    import httpx
                    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
                        r = await c.get(url, headers={"User-Agent": "Mozilla/5.0 (Foundry Audit)"})
                        pixel_inventory = inventory_tracking_pixels(r.text)
                except Exception as exc:
                    logger.info("pixel inventory fetch failed for %s: %s", url, exc)
                    pixel_inventory = {"available": False, "error": str(exc)[:200]}
            else:
                pixel_inventory = {"available": False, "error": "site fetch blocked or empty"}

            # If the on-page text was an anti-bot wall (`_safe_browse`
            # already blanked it) or otherwise looks thin, log it and
            # stamp the audit row so the UI can render an honest
            # "Site behind anti-bot protection — analysis based on web
            # research" banner instead of pretending the LLM read the
            # real homepage.
            if website_blocked or _looks_blocked(website_text):
                logger.info(
                    "Audit fetch for %s blocked / thin (%d chars). "
                    "Perplexity summary len=%d will carry the audit.",
                    url, len(website_text), len(perplexity_text or ""),
                )
                try:
                    await db.business_audits.update_one(
                        {"_id": audit_id},
                        {"$set": {"website_fetch_blocked": bool(website_blocked)}},
                    )
                except Exception:
                    pass

            # Stage B — synthesise (Dynamic Intelligence picks the model
            # based on task signature; the choice + rationale is stamped
            # on the audit row for the admin's audit trail).
            tech_signals = {
                "web_vitals": web_vitals,
                "screenshots": screenshots,
                "tracking_pixels": pixel_inventory,
                "domain_reputation": domain_rep,
            }
            data, model_choice = await _synthesise_audit_via_llm(
                LlmChat=LlmChat, UserMessage=UserMessage,
                EMERGENT_LLM_KEY=EMERGENT_LLM_KEY,
                business_name=brand,
                website_url=url,
                website_text=website_text,
                perplexity_summary=perplexity_text,
                website_blocked=website_blocked,
                brand_search=brand_hits,
                reviews_search=reviews_hits,
                competitors_search=comp_hits,
                tech_signals=tech_signals,
                socials=a.get("socials") or {},
                hint=a.get("hint") or "",
            )

            # Stage C — render PDF (best-effort; non-blocking on failure).
            # Uses the dedicated, brand-aligned audit renderer which
            # consumes the structured audit dict directly (cover page,
            # score ring, priority pills, "How Foundry helps" cards).
            pdf_filename = ""
            pdf_bytes_for_email = b""
            try:
                from audit_pdf import render_audit_pdf
                pdf_bytes = render_audit_pdf({**a, **data})
                pdf_filename = f"audit-{audit_id}-{uuid.uuid4().hex[:8]}.pdf"
                await persist_bytes(
                    upload_dir / pdf_filename, pdf_bytes,
                    original_name=pdf_filename, mime="application/pdf",
                )
                pdf_bytes_for_email = pdf_bytes
            except Exception as exc:
                logger.warning("Audit PDF render failed for %s: %s", audit_id, exc)

            await db.business_audits.update_one(
                {"_id": audit_id},
                {"$set": {
                    "status": "ready",
                    "score": int(data.get("score") or 0),
                    "score_rationale": data.get("score_rationale", ""),
                    "executive_summary": data.get("executive_summary", ""),
                    "strengths": data.get("strengths", []),
                    "issues": data.get("issues", []),
                    "recommendations": data.get("recommendations", []),
                    "competitors": data.get("competitors", []),
                    "brand_voice": data.get("brand_voice", {}),
                    "sections": data.get("sections", {}),
                    "how_soulprint_helps": data.get("how_soulprint_helps", ""),
                    "tech_signals": tech_signals,
                    "pdf_filename": pdf_filename,
                    # Dynamic Intelligence audit trail — admin can see
                    # which model produced this report and why.
                    "model_used": f"{model_choice.provider}/{model_choice.model}",
                    "model_routing_reason": model_choice.reason,
                    "updated_at": datetime.now(timezone.utc),
                }},
            )
            logger.info(
                "Audit %s ready (score=%s, model=%s/%s)",
                audit_id, data.get("score"), model_choice.provider, model_choice.model,
            )
            # Email the audit to the visitor (homepage self-serve flow only).
            # Admin-created rows have created_via=None or "admin" and skip
            # this — those audits are for outbound prospecting, not
            # transactional. Best-effort: failures here don't roll back
            # the audit doc.
            if a.get("created_via") == "public":
                try:
                    await _email_visitor_audit_ready(
                        audit={**a, **data, "_id": audit_id, "slug": a.get("slug")},
                        pdf_bytes=pdf_bytes_for_email,
                        pdf_filename=pdf_filename,
                    )
                except Exception as exc:
                    logger.warning(
                        "visitor audit email failed for %s: %s",
                        audit_id, exc,
                    )
        except Exception as exc:
            logger.exception("Audit generation failed for %s", audit_id)
            await db.business_audits.update_one(
                {"_id": audit_id},
                {"$set": {
                    "status": "failed",
                    "error": str(exc)[:600],
                    "updated_at": datetime.now(timezone.utc),
                }},
            )

    # ── Watchdog wrapper: cap any single run at 6 minutes ───────────────
    # Without this, a wedged Playwright fetch or unresponsive LLM call
    # can leave the audit stuck at "generating" indefinitely. We hard-
    # stop after 360 s and mark the row failed so the UI shows a clear
    # retry affordance instead of a forever-spinning state.
    #
    # Budget breakdown for a healthy audit (all stages parallelized):
    #   • Playwright site fetch       ~15-30 s
    #   • Perplexity Sonar Pro        ~20-60 s
    #   • Brave Search (x3)           ~3-6 s (parallel)
    #   • Web Vitals (PSI mobile+desk)~10-18 s (parallel)
    #   • Multi-viewport screenshots  ~15-20 s (parallel after this fix)
    #   • Domain reputation DNS       ~2-3 s (parallel after this fix)
    #   • Tracking-pixel HTML fetch   ~3-5 s
    #   • Synthesis LLM call          ~30-90 s
    #   • PDF render + persist        ~3-8 s
    # Worst-case parallel-stage wall time + serial LLM ≈ 120-180 s.
    # 360 s gives 2× headroom for slow real-world sites.
    AUDIT_MAX_RUNTIME_SEC = 360

    async def _run_audit_guarded(audit_id):
        try:
            await asyncio.wait_for(
                _run_audit(audit_id), timeout=AUDIT_MAX_RUNTIME_SEC,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Audit %s exceeded %ds — marking failed",
                audit_id, AUDIT_MAX_RUNTIME_SEC,
            )
            try:
                await db.business_audits.update_one(
                    {"_id": audit_id},
                    {"$set": {
                        "status": "failed",
                        "error": (
                            f"Audit exceeded {AUDIT_MAX_RUNTIME_SEC}s. "
                            "This usually means the target site was unreachable "
                            "or one of our research APIs timed out. Try again."
                        ),
                        "updated_at": datetime.now(timezone.utc),
                    }},
                )
            except Exception:
                pass
        except Exception:
            # _run_audit handles its own exception → no-op here.
            pass

    # ── One-time backfill: rename "SoulPrint" → "Foundry" in existing
    # audit documents. The audit synthesizer used to leak the legacy
    # brand name into recommendations / the closing pitch; the prompt +
    # post-process scrub now prevent that for NEW audits, but older
    # rows are still in the DB. We sweep them on boot. Cheap (only
    # touches docs that actually contain the legacy string) and safe
    # to re-run — once scrubbed, the query returns nothing.
    async def backfill_platform_name():
        try:
            cur = db.business_audits.find(
                {"$text": {"$search": "SoulPrint"}},
                {"_id": 1},
            ) if False else db.business_audits.find(
                # Fall back to a regex scan since there's no text index
                # on business_audits. The pattern matches the legacy
                # marketing name in any string field of the document
                # serialized as JSON. We rescan the doc's content with
                # `_scrub_platform_name` to mutate in place.
                {"$or": [
                    {"executive_summary": {"$regex": "SoulPrint", "$options": "i"}},
                    {"how_soulprint_helps": {"$regex": "SoulPrint", "$options": "i"}},
                    {"recommendations.soulprint_execution": {"$regex": "SoulPrint", "$options": "i"}},
                    {"recommendations.detail": {"$regex": "SoulPrint", "$options": "i"}},
                    {"strengths.detail": {"$regex": "SoulPrint", "$options": "i"}},
                    {"issues.detail": {"$regex": "SoulPrint", "$options": "i"}},
                    {"sections.website_analysis": {"$regex": "SoulPrint", "$options": "i"}},
                    {"sections.social_presence": {"$regex": "SoulPrint", "$options": "i"}},
                    {"sections.reviews_summary": {"$regex": "SoulPrint", "$options": "i"}},
                    {"sections.competitive_context": {"$regex": "SoulPrint", "$options": "i"}},
                    {"brand_voice.stated_mission": {"$regex": "SoulPrint", "$options": "i"}},
                    {"brand_voice.actual_voice": {"$regex": "SoulPrint", "$options": "i"}},
                    {"brand_voice.gap": {"$regex": "SoulPrint", "$options": "i"}},
                    {"score_rationale": {"$regex": "SoulPrint", "$options": "i"}},
                ]},
            )
            n = 0
            async for row in cur:
                full = await db.business_audits.find_one({"_id": row["_id"]})
                if not full:
                    continue
                # The scrub mutates user-visible string values in place
                # but ignores schema-internal keys. Skip the few admin
                # bookkeeping fields by patching only the audit payload.
                patch = {}
                for key in ("executive_summary", "score_rationale",
                            "how_soulprint_helps"):
                    v = full.get(key)
                    if isinstance(v, str):
                        patch[key] = _scrub_platform_name(v)
                for key in ("strengths", "issues", "recommendations",
                            "competitors", "brand_voice", "sections"):
                    v = full.get(key)
                    if v is not None:
                        patch[key] = _scrub_platform_name(v)
                # Apply only when something actually changed — keeps the
                # mtime stable for unaffected fields.
                changed = {k: v for k, v in patch.items() if v != full.get(k)}
                if changed:
                    await db.business_audits.update_one(
                        {"_id": row["_id"]}, {"$set": changed},
                    )
                    n += 1
            if n:
                logger.info("Backfilled platform-name in %d audits", n)
        except Exception as exc:
            logger.warning("Audit name-backfill skipped: %s", exc)

    # ── Orphan recovery: re-kick any audit stuck at "generating" on
    # startup. This catches rows orphaned by a redeploy or crash that
    # killed the asyncio task mid-flight. We use a 7-minute grace
    # window — just over the 6-min watchdog — so freshly-started or
    # legitimately-slow audits aren't picked up.
    async def recover_orphans():
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=7)
            n = 0
            cur = db.business_audits.find({
                "status": "generating",
                "$or": [
                    {"updated_at": {"$lt": cutoff}},
                    {"updated_at": {"$exists": False}, "created_at": {"$lt": cutoff}},
                ],
            }, {"_id": 1})
            async for a in cur:
                logger.info("Re-kicking orphaned audit %s", a["_id"])
                asyncio.create_task(_run_audit_guarded(a["_id"]))
                n += 1
            if n:
                logger.info("Recovered %d orphaned audits", n)
        except Exception as exc:
            logger.warning("Orphan recovery failed: %s", exc)

    # ── Admin endpoints ────────────────────────────────────────────────────
    @admin_r.post("/audits")
    async def create_audit(body: AuditCreateBody, u: dict = Depends(require_superadmin)):
        slug = _slugify(body.business_name)
        # Avoid the (very unlikely) slug collision
        for _ in range(3):
            if not await db.business_audits.find_one({"slug": slug}):
                break
            slug = _slugify(body.business_name)
        doc = {
            "slug": slug,
            "business_name": body.business_name.strip(),
            "website_url": _normalise_url(body.website_url),
            "socials": body.socials or {},
            "hint": (body.hint or "").strip(),
            "status": "generating",
            "score": None,
            "lead_count": 0,
            "view_count": 0,
            "pdf_filename": "",
            "created_by": u["id"],
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        res = await db.business_audits.insert_one(doc)
        doc["_id"] = res.inserted_id
        # Fire-and-forget the heavy pipeline
        asyncio.create_task(_run_audit_guarded(res.inserted_id))
        return _full_serialize(doc)

    @admin_r.get("/audits")
    async def list_audits(_: dict = Depends(require_superadmin)):
        cur = db.business_audits.find({}).sort("created_at", -1).limit(200)
        return [_full_serialize(a) async for a in cur]

    @admin_r.get("/audits/{audit_id}")
    async def get_audit(audit_id: str, _: dict = Depends(require_superadmin)):
        try:
            a = await db.business_audits.find_one({"_id": ObjectId(audit_id)})
        except Exception as exc:
            raise HTTPException(400, "Invalid audit id") from exc
        if not a:
            raise HTTPException(404, "Audit not found")
        return _full_serialize(a)

    @admin_r.get("/audits/{audit_id}/pdf")
    async def admin_audit_pdf(
        audit_id: str, request: Request,
        _: dict = Depends(require_superadmin),
    ):
        """Admin-only PDF download — no lead-id required, no public-page
        gating. Returns an absolute URL to the persisted file so the
        admin can click straight through from any deploy / domain.
        """
        try:
            a = await db.business_audits.find_one({"_id": ObjectId(audit_id)})
        except Exception as exc:
            raise HTTPException(400, "Invalid audit id") from exc
        if not a:
            raise HTTPException(404, "Audit not found")
        if not a.get("pdf_filename"):
            raise HTTPException(404, "PDF not yet generated for this audit")
        try:
            from workspaces import _public_origin as _origin_helper
            origin = _origin_helper(request)
        except Exception:
            origin = ""
        path = f"/api/files/{a['pdf_filename']}"
        return {
            "url": f"{origin.rstrip('/')}{path}" if origin else path,
            "filename": a["pdf_filename"],
        }

    @admin_r.delete("/audits/{audit_id}")
    async def delete_audit(audit_id: str, _: dict = Depends(require_superadmin)):
        try:
            await db.business_audits.delete_one({"_id": ObjectId(audit_id)})
            await db.audit_leads.delete_many({"audit_id": audit_id})
        except Exception as exc:
            raise HTTPException(400, "Invalid audit id") from exc
        return {"ok": True}

    @admin_r.post("/audits/{audit_id}/regenerate")
    async def regenerate_audit(audit_id: str, _: dict = Depends(require_superadmin)):
        try:
            oid = ObjectId(audit_id)
        except Exception as exc:
            raise HTTPException(400, "Invalid audit id") from exc
        if not await db.business_audits.find_one({"_id": oid}):
            raise HTTPException(404, "Audit not found")
        await db.business_audits.update_one(
            {"_id": oid},
            {"$set": {"status": "generating", "updated_at": datetime.now(timezone.utc), "error": ""}},
        )
        asyncio.create_task(_run_audit_guarded(oid))
        return {"ok": True}

    @admin_r.get("/audits/{audit_id}/leads")
    async def list_audit_leads(audit_id: str, _: dict = Depends(require_superadmin)):
        cur = db.audit_leads.find({"audit_id": audit_id}).sort("submitted_at", -1).limit(500)
        out = []
        async for lead in cur:
            d = {k: v for k, v in lead.items() if k != "_id"}
            d["id"] = str(lead["_id"])
            if isinstance(d.get("submitted_at"), datetime):
                d["submitted_at"] = d["submitted_at"].isoformat()
            out.append(d)
        return out

    # ── Public endpoints (no auth) ─────────────────────────────────────────
    async def _email_visitor_audit_ready(*, audit: dict, pdf_bytes: bytes,
                                         pdf_filename: str) -> None:
        """Send the visitor a "your audit is ready" email with the PDF
        attached AND a magic-link to view the rich version on the web.

        Resolves the recipient by walking the audit_leads rows for this
        audit (slug-keyed). If multiple leads were captured, we email
        the FIRST one (the homepage-form submitter — captured_via:
        'public_start') because they're the original requester. Other
        leads (lead-gate captures from after-the-fact unlocks) get the
        admin-side prospecting touch instead.
        """
        slug = audit.get("slug") or ""
        if not slug:
            return
        primary = await db.audit_leads.find_one(
            {"audit_slug": slug, "captured_via": "public_start"},
            sort=[("submitted_at", 1)],
        )
        if not primary:
            # Fall back to any lead on this audit if the primary tag
            # isn't on the doc (older rows pre-tagging).
            primary = await db.audit_leads.find_one(
                {"audit_slug": slug},
                sort=[("submitted_at", 1)],
            )
        if not primary or not primary.get("email"):
            return
        to_email = primary["email"]
        first_name = (primary.get("name") or "").split(" ")[0] or "there"
        biz = audit.get("business_name") or "your business"
        score = audit.get("score")
        score_line = f"{score}/100" if isinstance(score, int) else "—"
        # Magic-link URL: /audit/<slug>?lead_id=<id> — the auto-unlock
        # path we shipped earlier, so the visitor lands on the full
        # audit without re-filling the lead-gate.
        # Use FRONTEND_URL (with PUBLIC_ORIGIN_OVERRIDE precedence)
        # because there's no Request in scope here.
        origin = (os.environ.get("PUBLIC_ORIGIN_OVERRIDE") or "").strip()
        if not origin:
            origin = (os.environ.get("FRONTEND_URL") or "").strip()
        origin = origin.rstrip("/")
        view_url = f"{origin}/audit/{slug}?lead_id={primary['_id']}" if origin else f"/audit/{slug}"
        subject = f"Your Foundry audit for {biz} is ready"
        text = (
            f"Hi {first_name},\n\n"
            f"Your business audit for {biz} is ready. The PDF is attached "
            f"to this email — print it, share it with your team, save it "
            f"for later.\n\n"
            f"Health score: {score_line}\n\n"
            f"Want the rich, clickable version with live links to your "
            f"competitors' pages and detailed recommendations? Open the "
            f"web version here:\n\n{view_url}\n\n"
            f"Questions or want help acting on what you found? Reply to "
            f"this email or grab a 20-minute slot at "
            f"https://zcal.co/reggie-archeforge.\n\n"
            f"— The Foundry team\n"
        )
        # Pre-escape user-controlled values for the HTML view.
        from html import escape as _esc
        biz_h = _esc(biz)
        first_h = _esc(first_name)
        view_h = _esc(view_url)
        score_h = _esc(score_line)
        html = f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#fafafa;font-family:-apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif;color:#0f0f0f;">
  <div style="max-width:560px;margin:0 auto;padding:32px 24px;">
    <div style="background:#F64000;height:4px;border-radius:2px;width:48px;margin-bottom:24px;"></div>
    <h1 style="font-size:22px;font-weight:600;margin:0 0 8px 0;">Your audit is ready, {first_h}.</h1>
    <p style="font-size:15px;line-height:1.6;color:#374151;margin:0 0 20px 0;">
      We finished the comprehensive read of <b style="color:#0f0f0f;">{biz_h}</b> — website, social, reviews, brand voice, and competitive landscape. The full PDF is attached to this email.
    </p>
    <div style="background:#fff7ed;border:1px solid #fdba74;border-radius:12px;padding:16px 20px;margin:0 0 24px 0;">
      <div style="font-size:11px;letter-spacing:0.1em;text-transform:uppercase;color:#F64000;font-weight:600;margin-bottom:4px;">Health score</div>
      <div style="font-size:32px;font-weight:700;color:#0f0f0f;line-height:1;">{score_h}</div>
    </div>
    <a href="{view_h}" style="display:inline-block;background:#F64000;color:#fff;text-decoration:none;padding:12px 22px;border-radius:8px;font-weight:600;font-size:15px;margin-bottom:20px;">
      Open the rich web version →
    </a>
    <p style="font-size:13px;line-height:1.6;color:#6b7280;margin:24px 0 0 0;">
      The web version has the live competitor links, the per-recommendation "How Foundry helps you act on this" hooks, and the brand voice gap analysis with the full text intact. The PDF is the portable mirror.
    </p>
    <hr style="border:none;border-top:1px solid #e5e7eb;margin:32px 0;" />
    <p style="font-size:13px;line-height:1.6;color:#6b7280;margin:0;">
      Questions, or want help acting on what you found? Just reply to this email — we read every one — or grab a 20-minute slot at <a href="https://zcal.co/reggie-archeforge" style="color:#F64000;">zcal.co/reggie-archeforge</a>.
    </p>
    <p style="font-size:11px;color:#9ca3af;margin:32px 0 0 0;">Foundry · Tamper-evident · Bitcoin-anchored proof of integrity</p>
  </div>
</body></html>"""
        attachments: list[dict] = []
        if pdf_bytes and pdf_filename:
            attachments.append(_attach_pdf(pdf_bytes, pdf_filename))
        ok = await _send_resend_email(
            to_email, subject, html, text, attachments=attachments,
        )
        if ok:
            # Persist the timestamp so we don't double-send on a
            # regenerate. Future re-runs will see this and skip.
            await db.business_audits.update_one(
                {"_id": audit.get("_id")},
                {"$set": {"visitor_email_sent_at": datetime.now(timezone.utc),
                          "visitor_email_to": to_email}},
            )
            logger.info("Visitor audit email sent: slug=%s to=%s", slug, to_email)
        else:
            logger.warning("Visitor audit email FAILED: slug=%s to=%s", slug, to_email)

    async def _notify_new_lead(*, a: dict, lead: dict) -> None:
        """Fire the team@archeforge.com lead-notification email. Used
        by both the public /lead endpoint and the homepage self-serve
        audit-start endpoint so we don't lose lead visibility for the
        new flow."""
        notify_to = os.environ.get("AUDIT_NOTIFY_EMAIL", "").strip() or "team@archeforge.com"
        slug = a.get("slug") or lead.get("audit_slug", "")
        subject = f"🔥 New audit lead: {lead.get('name','')} from {lead.get('company','') or '—'}"
        text = (
            f"You have a new audit lead.\n\n"
            f"Audit: {a.get('business_name')} (/{slug})\n\n"
            f"Name:    {lead.get('name','')}\n"
            f"Email:   {lead.get('email','')}\n"
            f"Phone:   {lead.get('phone','') or '—'}\n"
            f"Company: {lead.get('company','') or '—'}\n"
            f"Role:    {lead.get('role','') or '—'}\n"
            f"Captured via: {lead.get('captured_via','lead_gate')}\n"
            f"Submitted: {(lead.get('submitted_at') or datetime.now(timezone.utc)).isoformat()}\n"
        )
        html = (
            f"<h2>🔥 New audit lead</h2>"
            f"<p>Audit: <b>{a.get('business_name')}</b> "
            f"(<code>/{slug}</code>)</p>"
            f"<table cellpadding='6' style='border-collapse:collapse;'>"
            f"<tr><td><b>Name</b></td><td>{lead.get('name','')}</td></tr>"
            f"<tr><td><b>Email</b></td><td><a href='mailto:{lead.get('email','')}'>{lead.get('email','')}</a></td></tr>"
            f"<tr><td><b>Phone</b></td><td>{lead.get('phone','') or '—'}</td></tr>"
            f"<tr><td><b>Company</b></td><td>{lead.get('company','') or '—'}</td></tr>"
            f"<tr><td><b>Role</b></td><td>{lead.get('role','') or '—'}</td></tr>"
            f"<tr><td><b>Captured via</b></td><td>{lead.get('captured_via','lead_gate')}</td></tr>"
            f"</table>"
        )
        try:
            await _send_resend_email(notify_to, subject, html, text)
        except Exception:
            pass

    @public_r.post("/audits/start")
    async def public_start_audit(
        body: PublicAuditStartBody, request: Request,
    ):
        """Self-serve audit creation from the public homepage.

        One form, two outcomes: a brand-new audit row enters the
        generation queue AND the requester's email is captured as a
        lead at the same moment — so they don't have to fill the
        lead-gate again when they land on `/audit/<slug>`. The
        tighter funnel is worth the slight asymmetry with admin-
        created audits (which don't capture a lead automatically).

        Anti-abuse: per-IP rate-limit (3/hour, separately-keyed from
        the lead-submit limiter so the two flows don't interact);
        URL hostname must be a real public domain after normalisation;
        every public-created row carries `created_via: "public"` so
        the admin dashboard can filter / spotlight them.
        """
        ip = (request.client.host if request.client else "0.0.0.0")
        if not _rate_limit_ok(ip, window=3600, limit=10):
            raise HTTPException(
                429,
                "We limit new audits to 10 per hour per network to keep "
                "abuse low. Please try again in a bit, or email "
                "team@archeforge.com if you need more right now.",
            )
        # Normalise + sanity check the URL.
        normalised_url = _normalise_url(body.website_url)
        if not normalised_url:
            raise HTTPException(400, "That doesn't look like a valid URL.")
        # Build the slug. Ensure uniqueness with the same retry the
        # admin path uses.
        slug = _slugify(body.business_name)
        for _ in range(3):
            if not await db.business_audits.find_one({"slug": slug}):
                break
            slug = _slugify(body.business_name)
        now = datetime.now(timezone.utc)
        doc = {
            "slug": slug,
            "business_name": body.business_name.strip(),
            "website_url": normalised_url,
            "socials": {},
            "hint": (body.hint or "").strip(),
            "status": "generating",
            "score": None,
            "lead_count": 1,            # we're capturing the requester
            "view_count": 0,
            "pdf_filename": "",
            "created_by": None,         # no auth on this path
            "created_by_email": body.email.lower().strip(),
            "created_via": "public",
            "created_at": now,
            "updated_at": now,
        }
        res = await db.business_audits.insert_one(doc)
        # Capture the requester as a lead immediately — same shape as
        # the existing /lead endpoint so the admin "Leads" modal
        # renders both kinds identically.
        lead_doc = {
            "audit_id": str(res.inserted_id),
            "audit_slug": slug,
            "name": body.name.strip(),
            "email": body.email.lower().strip(),
            "phone": (body.phone or "").strip(),
            "company": (body.company or "").strip(),
            "role": (body.role or "").strip(),
            "submitted_at": now,
            "ip": ip,
            "user_agent": (request.headers.get("user-agent") or "")[:240],
            "captured_via": "public_start",
        }
        lead_res = await db.audit_leads.insert_one(lead_doc)
        # Fire-and-forget the heavy generation pipeline.
        asyncio.create_task(_run_audit_guarded(res.inserted_id))
        # Kick off the standard "new lead" admin notification email so
        # we don't lose the operational visibility we already built.
        try:
            await _notify_new_lead(
                a={"_id": res.inserted_id, "business_name": body.business_name,
                   "slug": slug},
                lead=lead_doc,
            )
        except Exception:
            pass
        return {
            "ok": True,
            "slug": slug,
            "audit_id": str(res.inserted_id),
            "lead_id": str(lead_res.inserted_id),
        }

    @public_r.get("/audits/{slug}")
    async def public_get_audit_teaser(slug: str):
        a = await db.business_audits.find_one({"slug": slug})
        if not a:
            raise HTTPException(404, "Audit not found")
        # Track public view counts (best-effort, non-blocking)
        await db.business_audits.update_one(
            {"_id": a["_id"]}, {"$inc": {"view_count": 1}},
        )
        return _public_teaser(a)

    @public_r.post("/audits/{slug}/lead")
    async def public_submit_lead(slug: str, body: LeadSubmitBody, request: Request):
        ip = (request.client.host if request.client else "0.0.0.0")
        if not _rate_limit_ok(ip):
            raise HTTPException(
                429, "Too many submissions from your network. Please try again in a minute.",
            )
        a = await db.business_audits.find_one({"slug": slug})
        if not a:
            raise HTTPException(404, "Audit not found")
        if a.get("status") != "ready":
            raise HTTPException(409, "This audit isn't ready yet — please check back soon.")

        lead_doc = {
            "audit_id": str(a["_id"]),
            "audit_slug": slug,
            "name": body.name.strip(),
            "email": body.email.lower(),
            "phone": body.phone.strip(),
            "company": body.company.strip(),
            "role": body.role.strip(),
            "submitted_at": datetime.now(timezone.utc),
            "ip": ip,
            "user_agent": (request.headers.get("user-agent") or "")[:240],
        }
        res = await db.audit_leads.insert_one(lead_doc)
        await db.business_audits.update_one(
            {"_id": a["_id"]}, {"$inc": {"lead_count": 1}},
        )

        # Fire-and-forget admin notification email
        notify_to = os.environ.get("AUDIT_NOTIFY_EMAIL", "").strip() or "team@archeforge.com"
        subject = f"🔥 New audit lead: {body.name} from {body.company}"
        text = (
            f"You have a new audit lead.\n\n"
            f"Audit: {a.get('business_name')} (/{slug})\n\n"
            f"Name:    {body.name}\n"
            f"Email:   {body.email}\n"
            f"Phone:   {body.phone}\n"
            f"Company: {body.company}\n"
            f"Role:    {body.role}\n"
            f"Submitted: {lead_doc['submitted_at'].isoformat()}\n"
        )
        html = (
            f"<h2>🔥 New audit lead</h2>"
            f"<p>Audit: <b>{a.get('business_name')}</b> "
            f"(<code>/{slug}</code>)</p>"
            f"<table cellpadding='6' style='border-collapse:collapse;'>"
            f"<tr><td><b>Name</b></td><td>{body.name}</td></tr>"
            f"<tr><td><b>Email</b></td><td><a href='mailto:{body.email}'>{body.email}</a></td></tr>"
            f"<tr><td><b>Phone</b></td><td>{body.phone}</td></tr>"
            f"<tr><td><b>Company</b></td><td>{body.company}</td></tr>"
            f"<tr><td><b>Role</b></td><td>{body.role}</td></tr>"
            f"</table>"
            f"<p style='color:#888;font-size:12px;'>Submitted {lead_doc['submitted_at'].isoformat()}</p>"
        )
        asyncio.create_task(_send_resend_email(notify_to, subject, html, text))

        return {
            "ok": True,
            "lead_id": str(res.inserted_id),
            "audit": _public_full(a),
        }

    @public_r.get("/audits/{slug}/unlock")
    async def public_audit_unlock(slug: str, lead_id: str):
        """Auto-unlock the full audit body when the visitor arrives from
        the homepage self-serve flow with `?lead_id=...` in the URL.

        Validates the lead_id was captured against THIS slug — so a
        leaked lead id can't be used to peek at a different audit.
        Returns the same `_public_full(audit)` shape the lead-gate POST
        returns, keeping the PublicAuditPage's render path identical
        whether the visitor unlocked via gate-form or via redirect.
        """
        try:
            lead = await db.audit_leads.find_one(
                {"_id": ObjectId(lead_id), "audit_slug": slug},
            )
        except Exception as exc:
            raise HTTPException(400, "Invalid lead id") from exc
        if not lead:
            raise HTTPException(403, "Lead not associated with this audit.")
        a = await db.business_audits.find_one({"slug": slug})
        if not a:
            raise HTTPException(404, "Audit not found")
        if a.get("status") != "ready":
            # Don't 4xx here — a still-generating audit is a normal state
            # the page handles via the teaser route. Simply tell the
            # caller it isn't ready yet.
            return {"ok": False, "status": a.get("status", "generating")}
        return {"ok": True, "audit": _public_full(a)}

    @public_r.get("/audits/{slug}/pdf")
    async def public_audit_pdf(slug: str, lead_id: str, request: Request):
        # Validate the lead belongs to this audit (gates the download).
        try:
            lead = await db.audit_leads.find_one({"_id": ObjectId(lead_id), "audit_slug": slug})
        except Exception as exc:
            raise HTTPException(400, "Invalid lead id") from exc
        if not lead:
            raise HTTPException(403, "Submit the form to download.")
        a = await db.business_audits.find_one({"slug": slug})
        if not a or not a.get("pdf_filename"):
            raise HTTPException(404, "PDF not available")
        # Return an ABSOLUTE URL so the front-end can navigate to it
        # cleanly even when the SPA is hosted on a custom domain that
        # doesn't proxy `/api/*` to this backend (e.g. a marketing
        # subdomain that splits API and front-end). Falls back to the
        # relative path when no origin is detectable.
        try:
            from workspaces import _public_origin as _origin_helper
            origin = _origin_helper(request)
        except Exception:
            origin = ""
        path = f"/api/files/{a['pdf_filename']}"
        return {
            "url": f"{origin.rstrip('/')}{path}" if origin else path,
            "filename": a["pdf_filename"],
        }

    # ── In-app endpoints (authenticated users running audits from
    # inside Foundry — no lead capture, scoped to user's orgs) ────────
    @app_r.post("")
    async def create_in_app_audit(
        body: InAppAuditBody, u: dict = Depends(get_current_user),
    ):
        """Authenticated users running audits inside the app — no lead
        capture, attributed to the user + org, counts against the org's
        monthly audit usage."""
        await require_org_member(body.org_id, u["id"])
        slug = _slugify(body.business_name)
        for _ in range(3):
            if not await db.business_audits.find_one({"slug": slug}):
                break
            slug = _slugify(body.business_name)
        now = datetime.now(timezone.utc)
        doc = {
            "slug": slug,
            "business_name": body.business_name.strip(),
            "website_url": _normalise_url(body.website_url),
            "socials": body.socials or {},
            "hint": (body.hint or "").strip(),
            "status": "generating",
            "score": None,
            "lead_count": 0,
            "view_count": 0,
            "pdf_filename": "",
            "created_by": u["id"],
            "created_by_email": (u.get("email") or "").lower(),
            "org_id": body.org_id,
            "created_via": "in_app",
            "created_at": now,
            "updated_at": now,
        }
        res = await db.business_audits.insert_one(doc)
        doc["_id"] = res.inserted_id
        asyncio.create_task(_run_audit_guarded(res.inserted_id))
        return _full_serialize(doc)

    @app_r.get("")
    async def list_in_app_audits(
        org_id: str,
        limit: int = 50,
        u: dict = Depends(get_current_user),
    ):
        """List audits run in this org (any seat — surfaces every member's
        history so teams see what each other has already analysed)."""
        await require_org_member(org_id, u["id"])
        # Cap to keep payloads bounded; pagination can come later.
        limit = max(1, min(int(limit or 50), 200))
        cur = db.business_audits.find(
            {"org_id": org_id}
        ).sort("created_at", -1).limit(limit)
        return [_full_serialize(a) async for a in cur]

    @app_r.get("/{audit_id}")
    async def get_in_app_audit(
        audit_id: str, u: dict = Depends(get_current_user),
    ):
        try:
            a = await db.business_audits.find_one({"_id": ObjectId(audit_id)})
        except Exception as exc:
            raise HTTPException(400, "Invalid audit id") from exc
        if not a:
            raise HTTPException(404, "Audit not found")
        # Org-membership check unless this is the creator
        org_id = a.get("org_id")
        if org_id:
            await require_org_member(org_id, u["id"])
        elif a.get("created_by") != u["id"]:
            raise HTTPException(403, "Not your audit")
        return _full_serialize(a)

    @app_r.get("/{audit_id}/pdf")
    async def in_app_audit_pdf(
        audit_id: str, request: Request,
        u: dict = Depends(get_current_user),
    ):
        try:
            a = await db.business_audits.find_one({"_id": ObjectId(audit_id)})
        except Exception as exc:
            raise HTTPException(400, "Invalid audit id") from exc
        if not a:
            raise HTTPException(404, "Audit not found")
        org_id = a.get("org_id")
        if org_id:
            await require_org_member(org_id, u["id"])
        elif a.get("created_by") != u["id"]:
            raise HTTPException(403, "Not your audit")
        if not a.get("pdf_filename"):
            raise HTTPException(404, "PDF not yet generated")
        try:
            from workspaces import _public_origin as _origin_helper
            origin = _origin_helper(request)
        except Exception:
            origin = ""
        path = f"/api/files/{a['pdf_filename']}"
        return {
            "url": f"{origin.rstrip('/')}{path}" if origin else path,
            "filename": a["pdf_filename"],
        }

    @app_r.get("/by-slug/{slug}")
    async def get_audit_by_slug(
        slug: str, u: dict = Depends(get_current_user),
    ):
        """Authenticated, ungated view of an audit by its slug.

        Used by the in-app "View" button on /app/audits so creators
        + every member of the audit's org get the FULL audit (same
        view an admin sees) without having to fill out the public
        lead-capture form.

        Returns 404 if the audit isn't found OR if the caller isn't
        a member of the audit's org (we don't disclose existence to
        prevent slug enumeration by random authed users)."""
        a = await db.business_audits.find_one({"slug": slug})
        if not a:
            raise HTTPException(404, "Audit not found")
        org_id = a.get("org_id")
        allowed = False
        if org_id:
            try:
                await require_org_member(org_id, u["id"])
                allowed = True
            except HTTPException:
                allowed = False
        if not allowed and a.get("created_by") == u["id"]:
            allowed = True
        # Superadmin bypass — supports the admin path so we don't have
        # to duplicate this endpoint for the admin panel later.
        if not allowed and bool(u.get("is_superadmin")):
            allowed = True
        if not allowed:
            # Don't leak existence to enumeration attempts.
            raise HTTPException(404, "Audit not found")
        return _full_serialize(a)

    @app_r.delete("/{audit_id}")
    async def delete_in_app_audit(
        audit_id: str, u: dict = Depends(get_current_user),
    ):
        """Delete an audit + its PDF file. Org members can delete any
        audit in their org. Superadmin always allowed."""
        try:
            oid = ObjectId(audit_id)
            a = await db.business_audits.find_one({"_id": oid})
        except Exception as exc:
            raise HTTPException(400, "Invalid audit id") from exc
        if not a:
            raise HTTPException(404, "Audit not found")
        org_id = a.get("org_id")
        allowed = False
        if bool(u.get("is_superadmin")):
            allowed = True
        elif org_id:
            try:
                await require_org_member(org_id, u["id"])
                allowed = True
            except HTTPException:
                allowed = False
        elif a.get("created_by") == u["id"]:
            allowed = True
        if not allowed:
            raise HTTPException(403, "Not your audit")
        # Best-effort: remove PDF file from disk if present.
        pdf_filename = a.get("pdf_filename")
        if pdf_filename:
            try:
                pdf_path = upload_dir / pdf_filename
                if pdf_path.is_file():
                    pdf_path.unlink()
            except Exception as exc:
                logger.warning(
                    "Could not unlink audit PDF %s: %s", pdf_filename, exc,
                )
        # Also drop any captured leads — they're tied to this slug only.
        try:
            await db.audit_leads.delete_many({"audit_slug": a.get("slug")})
        except Exception:
            pass
        await db.business_audits.delete_one({"_id": oid})
        return {"ok": True}

    @app_r.post("/{audit_id}/retry")
    async def retry_in_app_audit(
        audit_id: str, u: dict = Depends(get_current_user),
    ):
        """Re-run an audit that's stuck at 'generating' or marked 'failed'.
        Resets the row's status + timestamps and kicks the pipeline."""
        try:
            oid = ObjectId(audit_id)
            a = await db.business_audits.find_one({"_id": oid})
        except Exception as exc:
            raise HTTPException(400, "Invalid audit id") from exc
        if not a:
            raise HTTPException(404, "Audit not found")
        org_id = a.get("org_id")
        if org_id:
            await require_org_member(org_id, u["id"])
        elif a.get("created_by") != u["id"]:
            raise HTTPException(403, "Not your audit")
        if a.get("status") == "ready":
            raise HTTPException(400, "Audit already completed")
        await db.business_audits.update_one(
            {"_id": oid},
            {"$set": {
                "status": "generating",
                "error": None,
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        asyncio.create_task(_run_audit_guarded(oid))
        a = await db.business_audits.find_one({"_id": oid})
        return _full_serialize(a)

    return admin_r, public_r, app_r, recover_orphans, backfill_platform_name
