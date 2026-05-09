import React, { useEffect, useState } from "react";
import { useParams, useSearchParams, Link } from "react-router-dom";
import {
  Loader2, Lock, ShieldCheck, ExternalLink, Download,
  TrendingUp, AlertTriangle, Sparkles, Phone, Mail,
} from "lucide-react";
import { toast } from "sonner";
import api from "../lib/api";
import AuditFullReport from "../components/AuditFullReport";
import AuditConversionCTA from "../components/AuditConversionCTA";

/**
 * PublicAuditPage — the prospect-facing landing page for a generated
 * business audit. Lives at /audit/:slug, NO AUTH REQUIRED.
 *
 * Two states:
 *   1. Pre-form: show score (big), executive summary, and TEASERS for
 *      strengths + issues. Detail copy is blurred and the recommendations
 *      / competitors / brand voice / sections sit behind a LOCKED card.
 *   2. Post-form: full report renders inline, PDF download CTA appears.
 *
 * Lead capture flow: name + email + phone + company + role → POST to
 * /api/public/audits/:slug/lead → backend sends Resend email to admin
 * + persists row to db.audit_leads + returns the full audit + lead_id.
 * lead_id is stored on this page's state and used to fetch the PDF.
 */
export default function PublicAuditPage() {
  const { slug } = useParams();
  const [params] = useSearchParams();
  const [teaser, setTeaser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [unlocked, setUnlocked] = useState(null); // {audit, leadId}

  // When the visitor arrives from the homepage self-serve flow
  // (`/audit/<slug>?lead_id=<id>`), they already gave us their email
  // a few seconds ago. Auto-unlock so we don't make them fill the
  // gate again. We re-fetch the full (post-gate) audit shape using
  // a tiny "unlock by lead id" endpoint that the backend exposes.
  // The teaser fetch still runs first as a sanity check / 404 guard.
  const presentedLeadId = params.get("lead_id");

  useEffect(() => {
    let cancelled = false;
    const fetchTeaser = async () => {
      try {
        const { data } = await api.get(`/public/audits/${slug}`);
        if (!cancelled) setTeaser(data);
      } catch (err) {
        if (!cancelled) {
          if (err?.response?.status === 404) setNotFound(true);
          else toast.error(err?.response?.data?.detail || "Failed to load audit");
        }
      } finally { if (!cancelled) setLoading(false); }
    };
    fetchTeaser();
    return () => { cancelled = true; };
  }, [slug]);

  // Once the teaser confirms the audit is ready AND we have a lead id
  // in the URL, fetch the full unlocked payload. We trust the backend
  // to validate that the lead id actually belongs to this audit.
  useEffect(() => {
    if (!presentedLeadId || !teaser || teaser.status !== "ready" || unlocked) return;
    let cancelled = false;
    (async () => {
      try {
        const { data } = await api.get(
          `/public/audits/${slug}/unlock?lead_id=${encodeURIComponent(presentedLeadId)}`,
        );
        if (!cancelled && data?.audit) {
          setUnlocked({ audit: data.audit, leadId: presentedLeadId });
        }
      } catch (_) { /* fall back to gate */ }
    })();
    return () => { cancelled = true; };
  }, [presentedLeadId, teaser, slug, unlocked]);

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <Loader2 className="w-8 h-8 animate-spin opacity-60" />
      </div>
    );
  }

  if (notFound) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center px-6 text-center">
        <div className="text-6xl mb-4 opacity-40">404</div>
        <h1 className="text-2xl font-medium mb-2">Audit not found</h1>
        <p className="sp-subtle text-sm">This link may have been deleted or never existed.</p>
        <Link to="/" className="mt-6 sp-btn sp-btn-ghost text-xs">Back to home</Link>
      </div>
    );
  }

  if (teaser?.status === "generating") {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center px-6 text-center max-w-md mx-auto">
        <Loader2 className="w-10 h-10 animate-spin opacity-50 mb-4" />
        <h1 className="text-2xl font-medium mb-2">Your audit is being prepared</h1>
        <p className="sp-subtle text-sm leading-relaxed">
          We're gathering data from {teaser?.business_name}'s website, social profiles, reviews, and competitive landscape — usually takes 3–10 minutes. Refresh this page in a few minutes.
        </p>
      </div>
    );
  }

  if (teaser?.status === "failed") {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center px-6 text-center max-w-md mx-auto">
        <AlertTriangle className="w-10 h-10 opacity-50 mb-4" />
        <h1 className="text-2xl font-medium mb-2">Audit unavailable</h1>
        <p className="sp-subtle text-sm">There was an issue generating this audit. The team has been notified.</p>
      </div>
    );
  }

  const showFull = !!unlocked;
  const audit = showFull ? unlocked.audit : teaser;

  return (
    <div className="min-h-screen sp-bg-grain">
      {/* Top bar — minimal branding, no app-shell nav. */}
      <header className="sp-glass-strong border-b border-white/5">
        <div className="max-w-5xl mx-auto px-6 py-3 flex items-center justify-between">
          <Link to="/" className="flex items-center gap-2">
            <Sparkles className="w-4 h-4 opacity-80" />
            <span className="text-sm tracking-tight font-medium">SoulPrint Engine Pro</span>
          </Link>
          <span className="text-[10px] uppercase tracking-widest sp-subtle">Business Audit</span>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-6 py-12 space-y-10" data-testid="public-audit-page">
        {/* ── Hero — score + business name ─────────────────────────── */}
        <section className="text-center space-y-3">
          <div className="text-[11px] uppercase tracking-widest sp-subtle">Business Audit for</div>
          <h1 className="text-4xl sm:text-5xl font-medium tracking-tight">{audit.business_name}</h1>
          {audit.website_url && (
            <a href={audit.website_url} target="_blank" rel="noopener noreferrer"
               className="inline-flex items-center gap-1 text-xs sp-subtle hover:text-orange-300">
              {audit.website_url} <ExternalLink className="w-3 h-3" />
            </a>
          )}
          {/* Score circle */}
          <div className="pt-6 flex flex-col items-center" data-testid="audit-score-card">
            <ScoreRing value={audit.score || 0} />
            <div className="text-xs sp-subtle mt-3 max-w-md leading-relaxed">
              {audit.executive_summary || audit.score_rationale || "Comprehensive audit of website, social presence, reviews, brand voice, and competitive position."}
            </div>
          </div>
        </section>

        {/* ── Teaser strengths + issues (always visible) ───────────── */}
        <div className="grid sm:grid-cols-2 gap-4">
          <TeaserCard
            icon={TrendingUp}
            title="Top strengths"
            items={(showFull ? audit.strengths : (teaser.strengths_teaser || []).map((h) => ({ headline: h }))) || []}
            tone="emerald"
            full={showFull}
          />
          <TeaserCard
            icon={AlertTriangle}
            title="Critical issues"
            items={(showFull ? audit.issues : (teaser.issues_teaser || []).map((h) => ({ headline: h }))) || []}
            tone="orange"
            full={showFull}
          />
        </div>

        {/* ── Gate or full report ──────────────────────────────────── */}
        {!showFull ? (
          <LeadGate slug={slug} onUnlock={(payload) => setUnlocked(payload)} />
        ) : (
          <>
            <AuditFullReport audit={audit} />
            {/* Conversion bar — three-path CTA after the prospect has
                read the full audit. Personalised hero, 24-hour trial
                request form, demo modal, PDF exit. */}
            <AuditConversionCTA audit={{ ...audit, slug }} />
            <PdfDownload slug={slug} leadId={unlocked.leadId} />
          </>
        )}

        <footer className="pt-12 text-center">
          <div className="text-[10px] uppercase tracking-widest sp-subtle">Audit prepared by</div>
          <div className="text-sm mt-1">SoulPrint Engine Pro</div>
        </footer>
      </main>
    </div>
  );
}


function ScoreRing({ value }) {
  const v = Math.max(0, Math.min(100, Number(value) || 0));
  // Color reflects the actual band — green / amber / red — not just orange.
  const color = v >= 80 ? "#10b981" : v >= 60 ? "#f59e0b" : "#ef4444";
  const C = 2 * Math.PI * 56;
  const offset = C * (1 - v / 100);
  return (
    <div className="relative w-40 h-40" data-testid="audit-score-ring">
      <svg viewBox="0 0 128 128" className="w-full h-full -rotate-90">
        <circle cx="64" cy="64" r="56" stroke="rgba(255,255,255,0.08)" strokeWidth="8" fill="none"/>
        <circle cx="64" cy="64" r="56" stroke={color} strokeWidth="8" fill="none"
                strokeDasharray={C} strokeDashoffset={offset}
                strokeLinecap="round"
                style={{ transition: "stroke-dashoffset 1.2s cubic-bezier(.2,.6,.2,1)" }} />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <div className="text-5xl font-medium tabular-nums">{v}</div>
        <div className="text-[10px] uppercase tracking-widest sp-subtle mt-1">Health Score</div>
      </div>
    </div>
  );
}


function TeaserCard({ icon: Icon, title, items, tone, full }) {
  const accent = tone === "emerald" ? "border-emerald-500/30" : "border-orange-500/30";
  return (
    <div className={`sp-glass rounded-2xl p-5 border ${accent}`}>
      <div className="flex items-center gap-2 mb-3">
        <Icon className="w-4 h-4 opacity-80" />
        <div className="text-[11px] uppercase tracking-widest sp-subtle">{title}</div>
      </div>
      {items.length === 0 ? (
        <div className="text-xs sp-subtle">—</div>
      ) : (
        <ul className="space-y-3">
          {items.map((it, i) => (
            <li key={i}>
              <div className="font-medium text-sm">{it.headline}</div>
              {full && it.detail && (
                <div className="text-xs sp-subtle mt-1 leading-relaxed">{it.detail}</div>
              )}
              {!full && (
                <div className="text-xs sp-subtle italic mt-1 select-none" style={{ filter: "blur(3px)" }} aria-hidden>
                  Detailed insight unlocked after a 30-second sign-up below.
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}


function LeadGate({ slug, onUnlock }) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (!name.trim() || !email.trim() || !phone.trim()) {
      toast.error("Name, email and phone are required");
      return;
    }
    setSubmitting(true);
    try {
      const { data } = await api.post(`/public/audits/${slug}/lead`, {
        name: name.trim(), email: email.trim(), phone: phone.trim(),
      });
      onUnlock({ audit: data.audit, leadId: data.lead_id });
      toast.success("Unlocked — your full report is below.");
      // Smooth scroll the user past the form so they immediately see the report
      setTimeout(() => window.scrollTo({ top: window.innerHeight, behavior: "smooth" }), 200);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Submission failed — try again.");
    } finally { setSubmitting(false); }
  };

  return (
    <section className="sp-glass-strong rounded-2xl p-6 sm:p-8 border border-orange-500/20" data-testid="audit-lead-gate">
      <div className="flex items-center gap-2 mb-2">
        <Lock className="w-4 h-4 text-orange-300" />
        <div className="text-[11px] uppercase tracking-widest text-orange-300/90">Unlock the full report</div>
      </div>
      <h2 className="text-2xl font-medium tracking-tight mb-2">See every recommendation, named competitor, and the full PDF.</h2>
      <p className="sp-subtle text-sm mb-5 max-w-2xl leading-relaxed">
        Your full audit includes the prioritized fix list, the 3 named direct competitors and where they're winning, the brand voice gap analysis, and a polished PDF you can save or share. Takes 30 seconds.
      </p>
      <form onSubmit={submit} className="grid sm:grid-cols-2 gap-3">
        <Field label="Full name" value={name} onChange={setName} placeholder="Jane Doe" testid="lead-name" />
        <Field label="Work email" type="email" value={email} onChange={setEmail} placeholder="jane@company.com" testid="lead-email" />
        <div className="sm:col-span-2">
          <Field label="Phone" type="tel" value={phone} onChange={setPhone} placeholder="(555) 123-4567" testid="lead-phone" />
        </div>
        <button
          type="submit"
          disabled={submitting}
          className="sm:col-span-2 sp-btn sp-btn-primary text-sm justify-center disabled:opacity-50"
          data-testid="lead-submit"
        >
          {submitting ? <Loader2 className="w-4 h-4 animate-spin" /> : <ShieldCheck className="w-4 h-4" />}
          {submitting ? "Unlocking…" : "Unlock the full audit"}
        </button>
        <div className="sm:col-span-2 text-[10px] sp-subtle text-center">
          We respect your inbox — no marketing spam, no third-party sharing. Just the audit.
        </div>
      </form>
    </section>
  );
}


function Field({ label, value, onChange, placeholder, type = "text", testid }) {
  return (
    <label className="block">
      <div className="text-[11px] uppercase tracking-widest sp-subtle mb-1">{label}</div>
      <input
        type={type} required value={value} onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full rounded-lg bg-black/30 border border-white/10 px-3 py-2.5 text-sm focus:outline-none focus:border-orange-500/50"
        data-testid={testid}
      />
    </label>
  );
}


// FullReport, SectionHeader, SectionParagraph were extracted to
// `components/AuditFullReport.js` so the admin "View report" modal can
// reuse the same layout without depending on a public-page concept.


function PdfDownload({ slug, leadId }) {
  const [busy, setBusy] = useState(false);
  const onDownload = async () => {
    setBusy(true);
    try {
      const { data } = await api.get(`/public/audits/${slug}/pdf?lead_id=${encodeURIComponent(leadId)}`);
      // Trigger a same-tab navigation to the file URL — browser handles the
      // PDF inline-or-download depending on the user's preference.
      const a = document.createElement("a");
      a.href = data.url;
      a.download = data.filename || `audit-${slug}.pdf`;
      a.target = "_blank";
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "PDF not available");
    } finally { setBusy(false); }
  };
  return (
    <div className="text-center py-6">
      <button
        type="button" onClick={onDownload} disabled={busy}
        className="sp-btn sp-btn-primary text-sm justify-center disabled:opacity-50"
        data-testid="audit-pdf-download"
      >
        {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
        {busy ? "Preparing PDF…" : "Download the full PDF report"}
      </button>
      <div className="text-[10px] sp-subtle mt-2 inline-flex items-center gap-3">
        <span className="inline-flex items-center gap-1"><Mail className="w-3 h-3" /> emailed to {leadId ? "you" : "—"}</span>
        <span className="inline-flex items-center gap-1"><Phone className="w-3 h-3" /> we'll be in touch shortly</span>
      </div>
    </div>
  );
}
