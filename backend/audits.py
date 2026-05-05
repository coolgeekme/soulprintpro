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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from bson import ObjectId
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

logger = logging.getLogger(__name__)


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
    company: str = Field(..., min_length=1, max_length=160)
    role: str = Field(..., min_length=1, max_length=120)


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
async def _send_resend_email(to: str, subject: str, html: str, text: str) -> bool:
    """POST to Resend's /emails endpoint. Returns True on success."""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    sender = os.environ.get("RESEND_FROM", "").strip()
    if not api_key or not sender:
        logger.info("Resend not configured — skipping admin notification")
        return False
    payload = {
        "from": sender, "to": [to], "subject": subject,
        "html": html, "text": text,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
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
        "executive_summary", "pdf_filename",
    )
    out = {k: a.get(k) for k in keys}
    return out


# ── LLM audit synthesis ──────────────────────────────────────────────────────
_AUDIT_SYSTEM_PROMPT = """You are SoulPrint Engine's Senior Brand & Growth Auditor — a hybrid of a McKinsey-style strategist, a senior conversion-rate optimiser, and a brand voice expert. You produce honest, prospect-grade audits that win business by being specific, useful, and slightly uncomfortable.

You will receive raw data the team has gathered:
  • The business's website rendered text (hero, copy, calls-to-action)
  • Brave Search results for the brand (recent mentions, news, citations)
  • Brave Search results for "{brand} reviews" (Google, Yelp, Trustpilot, BBB, etc.)
  • Brave Search results for "{brand} competitors" / "{brand} vs"
  • Optional social profile URLs the admin provided

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
    {"priority": "high|medium|low", "title": "Action verb + specific change", "detail": "What to do, why it works, how to measure success.", "estimated_impact": "1 sentence: the lift they should expect."}
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
  }
}

Rules:
  • If a data source is empty/missing, acknowledge it ("No public reviews surfaced in our scan…") rather than fabricating.
  • Cite specific copy, URLs, or themes when possible.
  • Be specific over diplomatic — this is a paid audit. Vague platitudes lose deals.
  • Score must reflect reality, not flattery.
  • Output ONLY the JSON, no preamble, no markdown fences.
"""


async def _synthesise_audit_via_llm(
    *, LlmChat, UserMessage, EMERGENT_LLM_KEY: str,
    business_name: str, website_url: str,
    website_text: str, brand_search: list, reviews_search: list,
    competitors_search: list, socials: dict, hint: str,
):
    """Call the audit synthesis model and parse its JSON.

    Returns ``(parsed_audit_dict, ModelChoice)`` — the model choice is
    persisted on the audit row so the admin can see (and the public page
    can display, if we ever surface it) which model produced the analysis.
    Raises on structural failure.
    """
    from dynamic_intelligence import pick_text_model, TASK_AUDIT_SYNTHESIS
    choice = pick_text_model(TASK_AUDIT_SYNTHESIS)

    user_text = json.dumps({
        "business_name": business_name,
        "website_url": website_url,
        "admin_hint": hint,
        "socials_provided": socials or {},
        "website_rendered_text_excerpt": (website_text or "")[:8000],
        "brave_brand_search": brand_search[:8],
        "brave_reviews_search": reviews_search[:8],
        "brave_competitors_search": competitors_search[:8],
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
    return data, choice


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
                f"{rec.get('detail','')}  \n_Expected impact: {rec.get('estimated_impact','')}_\n"
            )
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
    out.append("\n\n---\n_Prepared by SoulPrint Engine Pro._")
    return "".join(out)


# ── Router factory ───────────────────────────────────────────────────────────
def make_audits_router(
    *,
    db,
    get_current_user,
    require_superadmin,
    LlmChat,
    UserMessage,
    EMERGENT_LLM_KEY: str,
    brave_web_search,
    browse_url,
    persist_bytes,
    upload_dir: Path,
    render_pdf,
):
    """Two routers in one — admin (gated) and public.

    Wired into FastAPI by importing from server.py and including BOTH.
    Returns ``(admin_router, public_router)``.
    """
    admin_r = APIRouter(prefix="/api/admin")
    public_r = APIRouter(prefix="/api/public")

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
                if not url:
                    return ""
                try:
                    snap = await browse_url(url, take_screenshot=False)
                    return (snap or {}).get("text", "")
                except Exception as exc:
                    logger.warning("browse %s failed: %s", url, exc)
                    return ""

            website_text, brand_hits, reviews_hits, comp_hits = await asyncio.gather(
                _safe_browse(),
                _safe_brave(brand),
                _safe_brave(f"{brand} reviews"),
                _safe_brave(f"{brand} competitors OR alternatives"),
            )

            # Stage B — synthesise (Dynamic Intelligence picks the model
            # based on task signature; the choice + rationale is stamped
            # on the audit row for the admin's audit trail).
            data, model_choice = await _synthesise_audit_via_llm(
                LlmChat=LlmChat, UserMessage=UserMessage,
                EMERGENT_LLM_KEY=EMERGENT_LLM_KEY,
                business_name=brand,
                website_url=url,
                website_text=website_text,
                brand_search=brand_hits,
                reviews_search=reviews_hits,
                competitors_search=comp_hits,
                socials=a.get("socials") or {},
                hint=a.get("hint") or "",
            )

            # Stage C — render PDF (best-effort; non-blocking on failure)
            pdf_filename = ""
            try:
                md = _audit_to_markdown({**a, **data})
                pdf_bytes = render_pdf(md, title=f"Audit — {brand}")
                pdf_filename = f"audit-{audit_id}-{uuid.uuid4().hex[:8]}.pdf"
                await persist_bytes(
                    upload_dir / pdf_filename, pdf_bytes,
                    original_name=pdf_filename, mime="application/pdf",
                )
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
        asyncio.create_task(_run_audit(res.inserted_id))
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
        asyncio.create_task(_run_audit(oid))
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
        notify_to = os.environ.get("AUDIT_NOTIFY_EMAIL", "").strip() or "reggie@coolgeek.me"
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

    @public_r.get("/audits/{slug}/pdf")
    async def public_audit_pdf(slug: str, lead_id: str):
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
        # Return a redirect URL to the existing /api/files handler — that
        # already rehydrates from GridFS if disk got wiped.
        return {"url": f"/api/files/{a['pdf_filename']}",
                "filename": a["pdf_filename"]}

    return admin_r, public_r
