import React, { useState } from "react";
import { ShieldCheck, Anchor, Calendar, Rocket, Loader2, Check, X, Mail, ArrowRight, Wand2 } from "lucide-react";
import api from "../lib/api";

/**
 * AuditConversionCTA — bottom-of-audit conversion bar.
 *
 * Sits below `<AuditFullReport>` and turns the audit reader into a
 * subscriber/client. Three intentional paths, in increasing
 * commitment:
 *
 *   1. Try free for 24h (self-serve, email-verified)  — primary path
 *   2. Book a demo (Cal.com / Calendly embed)         — high-intent
 *   3. Take the audit & leave (PDF download)          — soft exit
 *
 * The trial flow is request-then-redeem: the visitor enters their
 * email, we mail them a code, they redeem when ready (the clock
 * starts at redeem, not at request — so a Friday-afternoon click
 * doesn't waste 80% of their trial). This intentionally collapses
 * email verification + clock-start into one action.
 *
 * Personalised hero — pulls one or two facts from THIS audit
 * (overall score, recommendation count) so the CTA feels written
 * for them, not generic.
 */
export default function AuditConversionCTA({ audit, leadId }) {
  const [phase, setPhase] = useState("idle"); // idle | sending | sent | error | bookDemo
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [demoOpen, setDemoOpen] = useState(false);
  const [pdfBusy, setPdfBusy] = useState(false);

  const score = Number(audit?.scorecard?.overall ?? audit?.overall_score ?? 0);
  const recCount = Array.isArray(audit?.recommendations)
    ? audit.recommendations.length : 0;
  const slug = audit?.slug || "";

  // Hero copy adapts to the audit's posture. Low scores get a
  // "we found N issues, want SPE Pro to draft fixes?" hook. High
  // scores get "you're ahead, here's how SPE Pro keeps you there".
  const isLowScore = score && score < 70;
  const heroLine = isLowScore
    ? `We found ${recCount || "several"} specific opportunit${recCount === 1 ? "y" : "ies"} on your site. Want SoulPrint Engine Pro to draft the top 3 fixes for you right now?`
    : recCount > 0
      ? `You're already ahead. SoulPrint Engine Pro keeps you there — drafting briefs, monitoring competitors, and turning your wins into content automatically.`
      : `Ready to put a team of AI agents to work on your business? Set up takes 5 minutes.`;

  const onRequest = async (e) => {
    e?.preventDefault?.();
    setError("");
    if (!email.trim()) {
      setError("Email is required");
      return;
    }
    setPhase("sending");
    try {
      await api.post("/trial/request-code", {
        email: email.trim(),
        name: name.trim(),
        source: slug ? `audit:${slug}` : "audit",
      });
      setPhase("sent");
    } catch (err) {
      // Already-registered isn't an error — surface it cleanly so the
      // visitor goes to login instead of bouncing.
      const detail = err?.response?.data?.detail || "";
      if (err?.response?.status === 200 && err?.response?.data?.already_registered) {
        setError("");
        setPhase("alreadyRegistered");
      } else if (detail) {
        setError(detail);
        setPhase("error");
      } else {
        setError("Could not request a trial code. Please try again.");
        setPhase("error");
      }
    }
  };

  /** Trigger a real PDF download from the third CTA card. Hits the
   *  same lead-gated endpoint the orange button uses, then opens the
   *  resulting absolute URL in a new tab so the browser's PDF viewer
   *  takes over. We do NOT scrollIntoView() the orange button (that
   *  was the previous behaviour and confused users — the click looked
   *  like a no-op on desktops where the orange button was already in
   *  view). If we don't have a leadId for some reason, fall back to
   *  scrolling so the user still has a path. */
  const onPdfDownload = async () => {
    if (!slug || !leadId) {
      const el = document.querySelector('[data-testid="audit-pdf-download"]');
      if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    if (pdfBusy) return;
    setPdfBusy(true);
    try {
      const { data } = await api.get(
        `/public/audits/${slug}/pdf?lead_id=${encodeURIComponent(leadId)}`,
      );
      if (data?.url) window.open(data.url, "_blank", "noopener,noreferrer");
    } catch (_) {
      // Best-effort fallback to scrolling so the user can hit the
      // orange button manually.
      const el = document.querySelector('[data-testid="audit-pdf-download"]');
      if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
    } finally {
      setPdfBusy(false);
    }
  };

  return (
    <section
      className="rounded-2xl p-6 sm:p-8 border mt-8"
      style={{
        background: "linear-gradient(135deg, rgba(251,146,60,0.10), rgba(74,222,128,0.04))",
        borderColor: "rgba(251,146,60,0.25)",
      }}
      data-testid="audit-conversion-cta"
    >
      {/* Personalised hero */}
      <div className="flex items-start gap-3 mb-6">
        <Wand2 className="w-5 h-5 text-orange-300 shrink-0 mt-1" />
        <div>
          <div className="text-[10px] uppercase tracking-widest text-orange-300/90 mb-1">
            What's next?
          </div>
          <h3 className="text-xl sm:text-2xl font-medium tracking-tight leading-[1.2]">
            {heroLine}
          </h3>
        </div>
      </div>

      {/* Three-path grid */}
      <div className="grid sm:grid-cols-3 gap-3">
        {/* PATH 1 — 24h trial, primary, accent box */}
        <div
          className="rounded-xl p-4 border flex flex-col"
          style={{
            background: "rgba(251,146,60,0.10)",
            borderColor: "rgba(251,146,60,0.35)",
          }}
          data-testid="cta-trial-card"
        >
          <div className="flex items-center gap-2 mb-2">
            <Rocket className="w-4 h-4 text-orange-300" />
            <div className="text-[10px] uppercase tracking-widest text-orange-300/90">
              Self-serve · 24h
            </div>
          </div>
          <div className="text-lg font-medium mb-1.5">Try free for 24 hours</div>
          <div className="text-sm sp-subtle mb-4 leading-relaxed">
            No credit card. Clock starts when you're ready — register, get a
            code by email, redeem when you have time to put us through our paces.
          </div>

          {phase === "sent" ? (
            <SuccessState email={email} />
          ) : phase === "alreadyRegistered" ? (
            <div className="mt-auto">
              <div className="text-sm sp-subtle leading-relaxed mb-3">
                That email already has an account. Sign in to see your trial status.
              </div>
              <a href="/auth?mode=login" className="sp-btn sp-btn-primary text-sm w-full">
                Sign in <ArrowRight className="w-3.5 h-3.5" />
              </a>
            </div>
          ) : (
            <form onSubmit={onRequest} className="mt-auto space-y-2" data-testid="cta-trial-form">
              <input
                type="text"
                placeholder="Your name (optional)"
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="sp-input text-sm w-full"
                data-testid="cta-trial-name"
              />
              <input
                type="email"
                placeholder="you@company.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                className="sp-input text-sm w-full"
                data-testid="cta-trial-email"
              />
              <button
                type="submit"
                disabled={phase === "sending"}
                className="sp-btn sp-btn-primary text-sm w-full disabled:opacity-60"
                data-testid="cta-trial-submit"
              >
                {phase === "sending"
                  ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Sending…</>
                  : <>Email me a code <ArrowRight className="w-3.5 h-3.5" /></>}
              </button>
              {error && <div className="text-xs text-red-400">{error}</div>}
            </form>
          )}
        </div>

        {/* PATH 2 — Book a demo */}
        <div className="sp-glass rounded-xl p-4 flex flex-col" data-testid="cta-demo-card">
          <div className="flex items-center gap-2 mb-2">
            <Calendar className="w-4 h-4 text-white/85" />
            <div className="text-[10px] uppercase tracking-widest sp-subtle">
              Live · 20 minutes
            </div>
          </div>
          <div className="text-lg font-medium mb-1.5">Book a demo</div>
          <div className="text-sm sp-subtle mb-4 leading-relaxed">
            Want to see SoulPrint Engine Pro operated by a human first?
            We'll set up your workspace live and run your first mission with you.
          </div>
          <button
            type="button"
            onClick={() => setDemoOpen(true)}
            className="sp-btn sp-btn-ghost text-sm w-full mt-auto"
            data-testid="cta-demo-btn"
          >
            <Calendar className="w-3.5 h-3.5" /> Pick a time
          </button>
        </div>

        {/* PATH 3 — Take the audit & leave (PDF download) */}
        <div className="sp-glass rounded-xl p-4 flex flex-col" data-testid="cta-pdf-card">
          <div className="flex items-center gap-2 mb-2">
            <Anchor className="w-4 h-4 text-white/85" />
            <div className="text-[10px] uppercase tracking-widest sp-subtle">
              Just the audit
            </div>
          </div>
          <div className="text-lg font-medium mb-1.5">Take it & run</div>
          <div className="text-sm sp-subtle mb-4 leading-relaxed">
            Download the full audit as a PDF. Tamper-evident — the file is
            cryptographically anchored on Bitcoin so you can prove its integrity later.
          </div>
          <button
            type="button"
            onClick={onPdfDownload}
            disabled={pdfBusy}
            className="sp-btn sp-btn-ghost text-sm w-full mt-auto disabled:opacity-50"
            data-testid="cta-pdf-btn"
          >
            {pdfBusy
              ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Preparing…</>
              : <><Anchor className="w-3.5 h-3.5" /> Download PDF</>}
          </button>
        </div>
      </div>

      {/* Trust strip */}
      <div className="mt-6 pt-5 border-t border-white/5 flex flex-wrap items-center gap-x-4 gap-y-2 text-[11px] sp-subtle">
        <TrustBadge icon={Anchor} label="Bitcoin-anchored proofs" />
        <TrustBadge icon={ShieldCheck} label="Encrypted credential vault" />
        <TrustBadge icon={X} label="Never trained on your data" cross />
        <a href="/trust" className="text-orange-300/90 hover:text-orange-200 underline-offset-2 hover:underline ml-auto inline-flex items-center gap-1">
          Full security policy <ArrowRight className="w-3 h-3" />
        </a>
      </div>

      {demoOpen && <DemoModal onClose={() => setDemoOpen(false)} source={slug ? `audit:${slug}` : "audit"} />}
    </section>
  );
}


function SuccessState({ email }) {
  return (
    <div
      className="rounded-lg p-3 border border-green-500/30 bg-green-500/8 mt-auto"
      data-testid="cta-trial-success"
    >
      <div className="flex items-start gap-2">
        <Check className="w-4 h-4 text-green-400 shrink-0 mt-0.5" />
        <div>
          <div className="text-base font-medium">Code sent!</div>
          <div className="text-sm sp-subtle leading-relaxed mt-1">
            We emailed a code to <b className="text-white/90">{email}</b>. Open
            it and click the button when you're ready — your 24-hour trial
            starts at that moment, not now.
          </div>
        </div>
      </div>
    </div>
  );
}


function TrustBadge({ icon: Icon, label, cross }) {
  return (
    <span className="inline-flex items-center gap-1">
      {cross
        ? <X className="w-3 h-3 text-red-400/80" />
        : <Icon className="w-3 h-3 text-green-400/80" />}
      {label}
    </span>
  );
}


function DemoModal({ onClose, source }) {
  // Cal.com / Calendly / zcal embed URL via env, with a baked-in
  // fallback to the founder's zcal so the demo CTA always has a
  // working iframe — even on deploys that don't set the env var.
  const calUrl = (process.env.REACT_APP_DEMO_BOOKING_URL
    || "https://zcal.co/reggie-archeforge/meeting").trim();
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: "rgba(0,0,0,0.6)" }}
      onClick={onClose}
      data-testid="demo-modal"
    >
      <div
        className="sp-glass-strong rounded-2xl w-full max-w-3xl max-h-[90vh] overflow-hidden flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-4 border-b border-white/10">
          <div className="flex items-center gap-2">
            <Calendar className="w-4 h-4 text-orange-300" />
            <div className="font-medium">Book a 20-minute demo</div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="sp-btn sp-btn-ghost !p-1.5"
            data-testid="demo-modal-close"
            aria-label="Close"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="flex-1 overflow-auto">
          {calUrl ? (
            <div className="relative h-[70vh]">
              <iframe
                src={calUrl}
                title="Book a demo"
                className="w-full h-full"
                style={{ border: 0 }}
              />
              <div className="absolute top-2 right-2">
                <a
                  href={calUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="sp-btn sp-btn-ghost text-[11px]"
                  data-testid="demo-modal-open-tab"
                >
                  Open in new tab
                </a>
              </div>
            </div>
          ) : (
            <div className="p-8 text-center">
              <Mail className="w-10 h-10 text-orange-300 mx-auto mb-4" />
              <div className="text-lg font-medium mb-2">Let's find a time</div>
              <p className="text-sm sp-subtle leading-relaxed max-w-md mx-auto mb-5">
                Email{" "}
                <a href={`mailto:team@archeforge.com?subject=Demo%20request%20-%20${encodeURIComponent(source || "audit")}`}
                   className="text-orange-300 hover:text-orange-200 underline-offset-2 hover:underline">
                  team@archeforge.com
                </a>
                {" "}with a couple of windows that work for you and we'll get a
                20-minute demo on the calendar within 24 hours.
              </p>
              <a
                href={`mailto:team@archeforge.com?subject=Demo%20request%20-%20${encodeURIComponent(source || "audit")}`}
                className="sp-btn sp-btn-primary text-sm"
                data-testid="demo-modal-mailto"
              >
                <Mail className="w-4 h-4" /> Open email
              </a>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
