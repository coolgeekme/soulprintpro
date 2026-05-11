import React, { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Wand2, Loader2, ArrowRight, Globe, ShieldCheck, Anchor, Mail } from "lucide-react";
import api from "../lib/api";
import Logo from "../components/Logo";

/**
 * /get-audit — public self-serve audit creator.
 *
 * One short form: business name, website, name, email, optional phone.
 * Submit → backend creates the audit row + captures the requester as
 * a lead in one shot, fires off the generation pipeline, and returns
 * `{slug, lead_id}`. We redirect the visitor to
 * `/audit/<slug>?lead_id=<id>` which auto-unlocks the full audit so
 * they don't have to re-enter their email.
 *
 * Why a dedicated page (not a modal): scrolling to read the form,
 * adjusting the URL, getting an error all work better with full
 * routing. Modals lose state on accidental backdrop clicks.
 */
export default function GetAuditPage() {
  const nav = useNavigate();
  const [businessName, setBusinessName] = useState("");
  const [websiteUrl, setWebsiteUrl] = useState("");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [hint, setHint] = useState("");
  const [phase, setPhase] = useState("idle");  // idle | submitting | done | error
  const [error, setError] = useState("");

  const onSubmit = async (e) => {
    e.preventDefault();
    setError("");
    if (!businessName.trim() || !websiteUrl.trim()
        || !name.trim() || !email.trim()) {
      setError("Business name, website, your name, and email are required.");
      return;
    }
    setPhase("submitting");
    try {
      const { data } = await api.post("/public/audits/start", {
        business_name: businessName.trim(),
        website_url: websiteUrl.trim(),
        name: name.trim(),
        email: email.trim(),
        phone: phone.trim(),
        hint: hint.trim(),
      });
      setPhase("done");
      // Redirect to the public audit page, pre-unlocked. The audit is
      // still generating in the background — the page polls until
      // status = "ready" and renders progressively.
      setTimeout(() => {
        nav(`/audit/${data.slug}?lead_id=${data.lead_id}`);
      }, 600);
    } catch (err) {
      // Surface a useful message regardless of whether the server
      // returns a string detail (HTTPException), a Pydantic 422 array,
      // or no body at all (network/CORS hiccup). Prevents the generic
      // fallback from swallowing the real reason.
      const r = err?.response;
      let msg = "";
      const d = r?.data?.detail;
      if (typeof d === "string") {
        msg = d;
      } else if (Array.isArray(d) && d.length) {
        msg = d.map((e) => e?.msg || "").filter(Boolean).join("; ");
      } else if (r?.status === 429) {
        msg = "Too many audits started recently from this network — please try again in a few minutes.";
      } else if (r?.status >= 500) {
        msg = "Our servers are having a moment. Please try again in a few seconds.";
      } else if (!r) {
        msg = "We couldn't reach the audit service. Check your connection and try again.";
      }
      setError(msg
        || "We couldn't start that audit. Please double-check the URL and try again.");
      setPhase("error");
    }
  };

  return (
    <div className="min-h-screen flex flex-col" data-testid="get-audit-page">
      {/* Top bar */}
      <header className="border-b border-white/5">
        <div className="max-w-3xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between">
          <Link to="/" className="inline-flex items-center gap-2">
            <Logo variant="wordmark" className="h-6 w-auto" />
          </Link>
          <Link to="/auth?mode=login" className="sp-btn sp-btn-ghost text-xs">
            Sign in
          </Link>
        </div>
      </header>

      <main className="flex-1 flex items-center justify-center px-4 sm:px-6 py-10 sm:py-14">
        <div className="w-full max-w-2xl">
          <div className="text-center mb-7">
            <div className="inline-flex items-center gap-1.5 sp-glass rounded-full px-3 py-1 text-[10px] uppercase tracking-widest sp-subtle mb-4">
              <Wand2 className="w-3 h-3" /> Free business audit
            </div>
            <h1 className="text-3xl sm:text-4xl font-medium tracking-tight leading-[1.1]">
              Drop your URL. Get a personalised audit in <span style={{
                background: "linear-gradient(120deg, rgba(251,146,60,1), rgba(74,222,128,1))",
                WebkitBackgroundClip: "text",
                WebkitTextFillColor: "transparent",
              }}>~60 seconds</span>.
            </h1>
            <p className="text-sm sm:text-base sp-subtle leading-relaxed mt-3 max-w-xl mx-auto">
              Our agents will scan your site, dig through your reviews and
              competitor signal, and write you a prioritised list of fixes
              — every one mapped to a specific Foundry feature
              that helps you act on it. No credit card. No fluff.
            </p>
          </div>

          {phase === "done" ? (
            <div
              className="rounded-2xl p-6 border text-center"
              style={{
                background: "linear-gradient(135deg, rgba(74,222,128,0.10), rgba(74,222,128,0.02))",
                borderColor: "rgba(74,222,128,0.3)",
              }}
              data-testid="get-audit-done"
            >
              <Loader2 className="w-6 h-6 text-green-300 animate-spin mx-auto mb-3" />
              <div className="text-lg font-medium">Audit started!</div>
              <p className="text-sm sp-subtle mt-2 leading-relaxed">
                Taking you to your live audit page — generation usually
                takes about a minute.
              </p>
            </div>
          ) : (
            <form
              onSubmit={onSubmit}
              className="sp-glass-strong rounded-2xl p-5 sm:p-6 space-y-3"
              data-testid="get-audit-form"
            >
              <div className="grid sm:grid-cols-2 gap-3">
                <Field label="Business name">
                  <input
                    type="text"
                    value={businessName}
                    onChange={(e) => setBusinessName(e.target.value)}
                    placeholder="Acme Co"
                    className="sp-input"
                    data-testid="get-audit-business"
                    required
                    autoFocus
                  />
                </Field>
                <Field label="Website URL">
                  <div className="relative">
                    <Globe className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 sp-subtle" />
                    <input
                      type="text"
                      value={websiteUrl}
                      onChange={(e) => setWebsiteUrl(e.target.value)}
                      placeholder="acme.com"
                      className="sp-input pl-8"
                      data-testid="get-audit-url"
                      required
                    />
                  </div>
                </Field>
              </div>

              <div className="grid sm:grid-cols-2 gap-3">
                <Field label="Your name">
                  <input
                    type="text"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="Alex Kim"
                    className="sp-input"
                    data-testid="get-audit-name"
                    required
                  />
                </Field>
                <Field label="Work email" hint="We'll send the audit summary here.">
                  <input
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="alex@acme.com"
                    className="sp-input"
                    data-testid="get-audit-email"
                    required
                  />
                </Field>
              </div>

              <Field label="Phone (optional)">
                <input
                  type="tel"
                  value={phone}
                  onChange={(e) => setPhone(e.target.value)}
                  placeholder="+1 555 0100"
                  className="sp-input"
                  data-testid="get-audit-phone"
                />
              </Field>

              <Field label="What should we focus on? (optional)" hint="One sentence is plenty — e.g. 'we're losing trial-to-paid conversion'.">
                <textarea
                  value={hint}
                  onChange={(e) => setHint(e.target.value)}
                  rows={2}
                  className="sp-input resize-none"
                  placeholder="Anything specific you'd like the audit to dig into?"
                  maxLength={600}
                  data-testid="get-audit-hint"
                />
              </Field>

              <button
                type="submit"
                disabled={phase === "submitting"}
                className="sp-btn sp-btn-primary text-sm w-full mt-2 disabled:opacity-60"
                data-testid="get-audit-submit"
              >
                {phase === "submitting"
                  ? <><Loader2 className="w-4 h-4 animate-spin" /> Starting your audit…</>
                  : <>Generate my audit <ArrowRight className="w-4 h-4" /></>}
              </button>

              {error && (
                <div className="text-[12px] text-red-400 leading-relaxed pt-1"
                     data-testid="get-audit-error">
                  {error}
                </div>
              )}

              <div className="text-[10px] sp-subtle text-center pt-3 border-t border-white/5 leading-relaxed">
                By submitting you agree to receive your audit and occasional
                follow-ups. Unsubscribe anytime — see our{" "}
                <Link to="/trust" className="text-orange-300 hover:text-orange-200 underline-offset-2 hover:underline">
                  privacy posture
                </Link>
                .
              </div>
            </form>
          )}

          {/* Trust strip */}
          <div className="flex items-center justify-center gap-x-4 gap-y-1.5 flex-wrap text-[11px] sp-subtle mt-6">
            <span className="inline-flex items-center gap-1">
              <Anchor className="w-3 h-3 text-green-400/80" /> Bitcoin-anchored proofs
            </span>
            <span className="inline-flex items-center gap-1">
              <ShieldCheck className="w-3 h-3 text-green-400/80" /> Encrypted vault
            </span>
            <span className="inline-flex items-center gap-1">
              <Mail className="w-3 h-3 text-green-400/80" /> Never sold to anyone
            </span>
          </div>
        </div>
      </main>
    </div>
  );
}


function Field({ label, hint, children }) {
  return (
    <label className="block">
      <div className="text-[10px] uppercase tracking-widest sp-subtle mb-1">{label}</div>
      {children}
      {hint && <div className="text-[10px] sp-subtle mt-1 leading-relaxed">{hint}</div>}
    </label>
  );
}
