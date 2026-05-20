import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import api from "../lib/api";
import Logo from "../components/Logo";
import { Lock, ShieldCheck, KeyRound, Server, Users, Mail, ArrowRight, Loader2, Anchor } from "lucide-react";

/**
 * Public security trust page — no auth required.
 *
 * Pulls the structured policy doc from `/api/security` and renders a
 * scannable B2B trust page: encryption, authentication, authorization,
 * rate limiting, secrets handling, audit, incident response, and
 * compliance posture. Designed for prospective customers' security
 * review teams (the people who decide whether their company can use you).
 *
 * Source of truth is `backend/security.py:SECURITY_POLICY_DOC` so the
 * page never drifts from the actual implementation.
 */
export default function SecurityPage() {
  const [policy, setPolicy] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const { data } = await api.get("/security");
        if (alive) setPolicy(data);
      } catch (_) {
        // Non-fatal — render the static skeleton below.
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, []);

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <Loader2 className="w-5 h-5 animate-spin opacity-60" />
      </div>
    );
  }

  return (
    <div className="min-h-screen" data-testid="security-page">
      {/* Top bar — minimal, no auth-aware header so prospects can read
          this without ever signing up. */}
      <header className="border-b border-white/5 sticky top-0 z-30 backdrop-blur-md"
              style={{
                background: "color-mix(in srgb, var(--bg) 75%, transparent)",
                paddingTop: "calc(env(safe-area-inset-top, 0px))",
              }}>
        <div className="max-w-4xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between">
          <Link to="/" className="inline-flex items-center gap-2">
            <Logo variant="wordmark" className="h-6 w-auto" />
          </Link>
          <nav className="flex items-center gap-1">
            <Link to="/about" className="sp-btn sp-btn-ghost text-xs">About</Link>
            <Link to="/auth" className="sp-btn sp-btn-primary text-xs">
              Get started <ArrowRight className="w-3 h-3" />
            </Link>
          </nav>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-4 sm:px-6 py-10 sm:py-14">
        {/* Hero */}
        <div className="mb-10">
          <div className="inline-flex items-center gap-1.5 sp-glass rounded-full px-3 py-1 text-[10px] uppercase tracking-widest sp-subtle mb-4">
            <ShieldCheck className="w-3 h-3" /> Trust & Security
          </div>
          <h1 className="text-3xl sm:text-5xl font-medium tracking-tight leading-[1.05] mb-3">
            Built so business teams can sleep at night.
          </h1>
          <p className="text-base sm:text-lg sp-subtle leading-relaxed max-w-2xl">
            Every conversation, file, and mission your team runs on Foundry
            is protected by encryption in transit and at rest, strict per-org
            authorization, and a credential vault that even our admins can't read.
            Below is the current state of our security posture — kept in sync
            with the actual code by <code className="text-[12px] px-1 sp-glass rounded">/api/security</code>.
          </p>
          {policy?.last_reviewed && (
            <div className="mt-3 text-[11px] sp-subtle">
              Last reviewed: <b>{policy.last_reviewed}</b> · Version {policy.version}
            </div>
          )}
        </div>

        {/* Proof of Integrity — flagship tamper-evident feature. Powered
            by OpenTimestamps / Bitcoin. Shows up as a full-width section
            so prospective customers spot it before they scroll the rest. */}
        {policy?.proof_of_integrity && (
          <section className="rounded-2xl p-5 sm:p-6 mb-8 border"
                   style={{
                     background: "linear-gradient(135deg, rgba(251,146,60,0.10), rgba(251,146,60,0.02))",
                     borderColor: "rgba(251,146,60,0.25)",
                   }}>
            <div className="flex items-start gap-3 mb-3">
              <div className="w-10 h-10 rounded-xl flex items-center justify-center shrink-0"
                   style={{ background: "rgba(251,146,60,0.15)" }}>
                <Anchor className="w-5 h-5 text-orange-300" />
              </div>
              <div className="min-w-0">
                <div className="text-[10px] uppercase tracking-widest text-orange-300/90 mb-0.5">
                  Proof of Integrity · Bitcoin-anchored
                </div>
                <h2 className="text-xl sm:text-2xl font-medium tracking-tight">
                  Tamper-evident proof for every artifact your agents create.
                </h2>
              </div>
            </div>
            <div className="grid sm:grid-cols-2 gap-3 mt-4">
              <div className="sp-glass rounded-xl p-4">
                <div className="text-[10px] uppercase tracking-widest sp-subtle mb-1">Tier 1 · Per-file</div>
                <div className="text-sm sp-subtle leading-relaxed">
                  {policy.proof_of_integrity.tier1_per_file}
                </div>
              </div>
              <div className="sp-glass rounded-xl p-4">
                <div className="text-[10px] uppercase tracking-widest sp-subtle mb-1">Tier 2 · Daily Merkle root</div>
                <div className="text-sm sp-subtle leading-relaxed">
                  {policy.proof_of_integrity.tier2_daily_merkle}
                </div>
              </div>
            </div>
            <div className="grid sm:grid-cols-3 gap-2 mt-3 text-[11px] sp-subtle">
              <div><b className="text-white/85">Ledger:</b> {policy.proof_of_integrity.ledger}</div>
              <div><b className="text-white/85">Cost:</b> {policy.proof_of_integrity.cost_to_customer}</div>
              <div className="sm:col-span-1"><b className="text-white/85">Verify yourself:</b> any third party, any time</div>
            </div>
            <div className="mt-3 pt-3 border-t border-orange-300/15 text-[11px] sp-subtle leading-relaxed">
              <b className="text-white/85">How to verify independently:</b> {policy.proof_of_integrity.verifiability}
            </div>
          </section>
        )}

        {/* Cards */}
        <div className="grid sm:grid-cols-2 gap-3 mb-8">
          <Card icon={Lock} title="Encryption in transit">
            {policy?.encryption?.in_transit || "TLS 1.2+ on every endpoint."}
          </Card>
          <Card icon={Server} title="Encryption at rest">
            {policy?.encryption?.at_rest || "MongoDB Atlas full-disk AES-256."}
          </Card>
          <Card icon={KeyRound} title="Authentication">
            <ul className="list-disc pl-4 space-y-0.5 text-sm sp-subtle">
              {(policy?.authentication?.methods || []).map((m, i) => (
                <li key={i}>{m}</li>
              ))}
            </ul>
            {policy?.authentication?.brute_force_protection && (
              <div className="mt-2 text-xs sp-subtle leading-relaxed">
                {policy.authentication.brute_force_protection}
              </div>
            )}
          </Card>
          <Card icon={Users} title="Per-org authorization">
            {policy?.authorization?.model
              || "Every endpoint validates org/team membership before returning a row."}
            {policy?.authorization?.private_chief_of_staff && (
              <div className="mt-2 text-xs sp-subtle leading-relaxed">
                {policy.authorization.private_chief_of_staff}
              </div>
            )}
          </Card>
        </div>

        {/* Rate limiting */}
        <Section title="Rate limiting & abuse protection">
          <ul className="space-y-1.5 text-sm sp-subtle">
            {policy?.rate_limiting && Object.entries(policy.rate_limiting).map(([k, v]) => (
              <li key={k} className="flex gap-2">
                <span className="font-medium text-white/85 capitalize min-w-[140px]">
                  {k.replace(/_/g, " ")}:
                </span>
                <span className="leading-relaxed">{v}</span>
              </li>
            ))}
          </ul>
        </Section>

        {/* Secrets */}
        <Section title="Secrets & key management">
          <p className="text-sm sp-subtle leading-relaxed">
            {policy?.secrets?.policy
              || "All third-party API keys live in the backend env, never in source."}
          </p>
        </Section>

        {/* Data residency */}
        {policy?.data_residency && (
          <Section title="Data residency & infrastructure">
            <ul className="space-y-1.5 text-sm sp-subtle">
              {Object.entries(policy.data_residency).map(([k, v]) => (
                <li key={k}>
                  <span className="font-medium text-white/85 capitalize mr-1.5">
                    {k.replace(/_/g, " ")}:
                  </span>
                  {v}
                </li>
              ))}
            </ul>
          </Section>
        )}

        {/* Compliance posture */}
        {policy?.compliance_posture && (
          <Section title="Compliance posture">
            <ul className="space-y-2.5 text-sm sp-subtle">
              {Object.entries(policy.compliance_posture).map(([k, v]) => (
                <li key={k}>
                  <div className="text-[10px] uppercase tracking-widest text-white/70 mb-0.5">
                    {k.replace(/_/g, " ")}
                  </div>
                  <div className="leading-relaxed">{v}</div>
                </li>
              ))}
            </ul>
            <div className="mt-4 pt-4 border-t border-white/5">
              <Link to="/legal/soc2-roadmap"
                    className="inline-flex items-center gap-1.5 text-xs text-orange-300 hover:text-orange-200 underline-offset-2 hover:underline"
                    data-testid="security-soc2-roadmap-link">
                See the full SOC 2 control roadmap <ArrowRight className="w-3 h-3" />
              </Link>
            </div>
          </Section>
        )}

        {/* Incident response */}
        <Section title="Incident response">
          <p className="text-sm sp-subtle leading-relaxed mb-2">
            Found a vulnerability or have a question for our security team?
            Email us at{" "}
            <a href="mailto:team@archeforge.com"
               className="text-orange-300 hover:text-orange-200 underline-offset-2 hover:underline">
              <Mail className="w-3 h-3 inline mr-1" />
              {policy?.incident_response?.contact || "team@archeforge.com"}
            </a>
            . We aim to acknowledge within{" "}
            <b>{policy?.incident_response?.expected_response_hours || 24} hours</b>.
          </p>
        </Section>

        {/* Footer */}
        <div className="mt-10 pt-6 border-t border-white/5 flex items-center justify-between flex-wrap gap-3">
          <div className="text-xs sp-subtle">
            Want a deeper review? We're happy to walk security teams through
            our architecture diagram and answer SIG/CAIQ questionnaires.
          </div>
          <a href="mailto:team@archeforge.com?subject=Security%20review%20request"
             className="sp-btn sp-btn-primary text-xs"
             data-testid="security-contact-btn">
            <Mail className="w-3 h-3" /> Talk to security
          </a>
        </div>
      </main>
    </div>
  );
}


function Card({ icon: Icon, title, children }) {
  return (
    <div className="sp-glass rounded-2xl p-5">
      <div className="flex items-center gap-2 mb-2">
        <div className="w-7 h-7 rounded-lg sp-glass-strong flex items-center justify-center shrink-0">
          <Icon className="w-3.5 h-3.5 text-orange-300" />
        </div>
        <div className="text-sm font-medium">{title}</div>
      </div>
      <div className="text-sm sp-subtle leading-relaxed">{children}</div>
    </div>
  );
}


function Section({ title, children }) {
  return (
    <section className="sp-glass-strong rounded-2xl p-5 sm:p-6 mb-3">
      <div className="text-[10px] uppercase tracking-widest sp-subtle mb-2">{title}</div>
      {children}
    </section>
  );
}
