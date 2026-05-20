import React from "react";
import { Link } from "react-router-dom";
import Logo from "../components/Logo";
import {
  ArrowLeft, ArrowRight, ShieldCheck, Check, Hourglass, Mail, FileText,
} from "lucide-react";

/**
 * /legal/soc2-roadmap — public roadmap of SOC 2 Trust Service Criteria
 * controls. Lists each control with one of two states:
 *   • LIVE — operating today, evidence captured
 *   • IN PROGRESS — control designed, evidence-collection ramping up
 *
 * No target date is promised. The page is intentionally engineering-honest
 * so enterprise buyers and security reviewers see what's actually shipped
 * vs. what's in flight — without being able to hold us to a calendar date
 * we haven't committed to. When the Type I/II attestation lands, we'll
 * replace this page with the report link + auditor letter.
 */

const SECTIONS = [
  {
    title: "Security (CC) — Common Criteria",
    note: "Required for every SOC 2 audit.",
    controls: [
      { name: "TLS 1.2+ on every endpoint with HSTS preload", state: "live" },
      { name: "MongoDB Atlas AES-256 full-disk encryption at rest", state: "live" },
      { name: "Browser-session credential vault — Fernet (AES-128 + HMAC) keyed by env-only secret", state: "live" },
      { name: "Bcrypt password hashing (cost factor 12) + Firebase Google sign-in (JWKS-verified)", state: "live" },
      { name: "Brute-force protection — per-IP+email lockout after N failures", state: "live" },
      { name: "Per-org and per-team authorization checks on every data endpoint", state: "live" },
      { name: "Private Chief-of-Staff threads scoped to owner-user only", state: "live" },
      { name: "Security headers middleware (CSP, X-Frame-Options, Referrer-Policy)", state: "live" },
      { name: "Secrets in env only — no credentials in source control", state: "live" },
      { name: "Centralized superadmin action logging (actor, action, target, timestamp)", state: "live" },
      { name: "Proof-of-integrity — every artifact SHA-256 anchored on Bitcoin via OpenTimestamps", state: "live" },
      { name: "MFA-required for all administrator accounts", state: "in_progress" },
      { name: "Quarterly access reviews — formal sign-off log", state: "in_progress" },
      { name: "Vulnerability scanning in CI (Trivy / Snyk) — block on critical CVEs", state: "in_progress" },
      { name: "Dependency upgrade SLA (critical: 7d, high: 30d)", state: "in_progress" },
      { name: "Annual penetration test by external firm", state: "in_progress" },
    ],
  },
  {
    title: "Availability (A)",
    note: "Optional category — included to support enterprise uptime expectations.",
    controls: [
      { name: "Managed MongoDB Atlas with automated geo-replicated backups", state: "live" },
      { name: "Encrypted GridFS mirror for every uploaded file (durable through pod redeploys)", state: "live" },
      { name: "Supervisor-managed services with automatic restart on crash", state: "live" },
      { name: "External uptime monitoring + on-call rotation", state: "in_progress" },
      { name: "Documented Disaster Recovery (RPO/RTO) runbook + tabletop test", state: "in_progress" },
      { name: "Quarterly backup-restore drill with timed evidence", state: "in_progress" },
    ],
  },
  {
    title: "Confidentiality (C)",
    note: "Optional category — required by most enterprise security questionnaires.",
    controls: [
      { name: "Per-org data isolation enforced at the query layer", state: "live" },
      { name: "Capability-URL-gated file serving (unguessable, scoped to org)", state: "live" },
      { name: "Customer-managed encryption keys (BYOK) on Refine dedicated builds", state: "in_progress" },
      { name: "Data classification labels for every collection", state: "in_progress" },
      { name: "Data Loss Prevention scanning on outbound integrations", state: "in_progress" },
    ],
  },
  {
    title: "Processing Integrity (PI)",
    note: "Optional category — relevant because we run autonomous agents on customer data.",
    controls: [
      { name: "LLM call telemetry — every request logged with kind, model, tokens, latency, cost", state: "live" },
      { name: "Mission event log — append-only audit trail per mission (think / tool_call / tool_result / artifact)", state: "live" },
      { name: "Artifact tamper-evidence — Bitcoin-anchored SHA-256 proofs", state: "live" },
      { name: "Anti-hallucination — agents auto-fetch user-supplied URLs as ground truth before responding", state: "live" },
      { name: "Empty-promise detector + auto-retry forces same-turn artifact delivery", state: "live" },
      { name: "Formal change-management — PR review + deployment log linked to ticket", state: "in_progress" },
    ],
  },
  {
    title: "Privacy (P)",
    note: "Optional category — included for GDPR and B2B customer alignment.",
    controls: [
      { name: "Right-to-erasure via full account deletion (cascades orgs, teams, conversations, files)", state: "live" },
      { name: "Sub-processors disclosed publicly at /legal/sub-processors", state: "live" },
      { name: "Standard DPA available on request for every tier", state: "live" },
      { name: "Privacy policy + data retention windows published at /legal/privacy", state: "live" },
      { name: "Per-org data export (machine-readable JSON dump)", state: "in_progress" },
      { name: "Cookie-consent banner for marketing surfaces (EU/UK visitors)", state: "in_progress" },
    ],
  },
  {
    title: "Operational maturity & governance",
    note: "Cross-cutting controls auditors look for.",
    controls: [
      { name: "Incident response contact + 24h acknowledgement SLA (team@archeforge.com)", state: "live" },
      { name: "Sub-processor inventory maintained on a public page", state: "live" },
      { name: "Documented incident response playbook (detection, containment, eradication, recovery, post-mortem)", state: "in_progress" },
      { name: "Vendor risk assessment for every sub-processor (annual review)", state: "in_progress" },
      { name: "Employee onboarding security training + acknowledgement", state: "in_progress" },
      { name: "Background checks for engineers with production access", state: "in_progress" },
      { name: "Annual policy review + version control on policy docs", state: "in_progress" },
      { name: "Compliance automation platform — Vanta / Drata / Secureframe", state: "in_progress" },
    ],
  },
];


function StatePill({ state }) {
  if (state === "live") {
    return (
      <span className="inline-flex items-center gap-1 text-[10px] font-medium uppercase tracking-widest px-2 py-0.5 rounded-full"
            style={{ background: "rgba(16,185,129,0.12)", color: "rgb(16,185,129)" }}>
        <Check className="w-2.5 h-2.5" /> Live
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 text-[10px] font-medium uppercase tracking-widest px-2 py-0.5 rounded-full"
          style={{ background: "rgba(251,146,60,0.12)", color: "rgb(251,146,60)" }}>
      <Hourglass className="w-2.5 h-2.5" /> In progress
    </span>
  );
}


export default function SOC2RoadmapPage() {
  const liveCount = SECTIONS.reduce(
    (n, s) => n + s.controls.filter((c) => c.state === "live").length,
    0,
  );
  const wipCount = SECTIONS.reduce(
    (n, s) => n + s.controls.filter((c) => c.state === "in_progress").length,
    0,
  );

  return (
    <div className="min-h-screen" data-testid="soc2-roadmap-page">
      {/* Top bar — auth-free, prospects can read without signing up. */}
      <header className="border-b border-white/5 sticky top-0 z-30 backdrop-blur-md"
              style={{
                background: "color-mix(in srgb, var(--bg) 75%, transparent)",
                paddingTop: "calc(env(safe-area-inset-top, 0px))",
              }}>
        <div className="max-w-3xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between">
          <Link to="/" className="inline-flex items-center gap-2" data-testid="soc2-brand">
            <Logo variant="wordmark" className="h-6 w-auto" />
          </Link>
          <nav className="flex items-center gap-1">
            <Link to="/security" className="sp-btn sp-btn-ghost text-xs" data-testid="soc2-back-security">
              <ArrowLeft className="w-3 h-3" /> Security
            </Link>
            <Link to="/auth" className="sp-btn sp-btn-primary text-xs">
              Get started <ArrowRight className="w-3 h-3" />
            </Link>
          </nav>
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-4 sm:px-6 py-10 sm:py-14">
        {/* Hero */}
        <div className="mb-10">
          <div className="inline-flex items-center gap-1.5 sp-glass rounded-full px-3 py-1 text-[10px] uppercase tracking-widest sp-subtle mb-4">
            <ShieldCheck className="w-3 h-3" /> SOC 2 Roadmap
          </div>
          <h1 className="text-3xl sm:text-5xl font-medium tracking-tight leading-[1.05] mb-4">
            What's already live. What's in flight.
          </h1>
          <p className="text-base sm:text-lg sp-subtle leading-relaxed">
            We publish a working list of SOC 2 controls so security reviewers
            don't have to take our word for it. Every control below is mapped
            to a Trust Service Criterion (Security, Availability, Confidentiality,
            Processing Integrity, Privacy). No target dates — we'll let the
            attestation letter speak for the timeline.
          </p>

          <div className="grid grid-cols-2 gap-3 mt-6 max-w-md">
            <div className="sp-glass rounded-xl px-4 py-3" data-testid="soc2-live-count">
              <div className="text-[10px] uppercase tracking-widest text-emerald-400/90 mb-0.5">Live today</div>
              <div className="text-2xl font-medium">{liveCount}</div>
              <div className="text-[11px] sp-subtle">controls operating with evidence</div>
            </div>
            <div className="sp-glass rounded-xl px-4 py-3" data-testid="soc2-wip-count">
              <div className="text-[10px] uppercase tracking-widest text-orange-300/90 mb-0.5">In progress</div>
              <div className="text-2xl font-medium">{wipCount}</div>
              <div className="text-[11px] sp-subtle">designed, evidence-collection ramping</div>
            </div>
          </div>
        </div>

        {/* Sections */}
        {SECTIONS.map((sec) => (
          <section key={sec.title} className="sp-glass-strong rounded-2xl p-5 sm:p-6 mb-3"
                   data-testid={`soc2-section-${sec.title.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`}>
            <div className="mb-3">
              <div className="text-base sm:text-lg font-medium">{sec.title}</div>
              <div className="text-[11px] sp-subtle mt-0.5">{sec.note}</div>
            </div>
            <ul className="space-y-2">
              {sec.controls.map((c, i) => (
                <li key={i} className="flex items-start gap-3 text-sm">
                  <div className="pt-0.5 shrink-0">
                    <StatePill state={c.state} />
                  </div>
                  <span className="sp-subtle leading-relaxed">{c.name}</span>
                </li>
              ))}
            </ul>
          </section>
        ))}

        {/* Refine tier callout */}
        <section className="rounded-2xl p-5 sm:p-6 mt-6 border"
                 style={{
                   background: "linear-gradient(135deg, rgba(168,85,247,0.10), rgba(168,85,247,0.02))",
                   borderColor: "rgba(168,85,247,0.25)",
                 }}
                 data-testid="soc2-refine-callout">
          <div className="text-[10px] uppercase tracking-widest text-violet-300/90 mb-1">
            Refine tier · dedicated deployment
          </div>
          <h2 className="text-xl sm:text-2xl font-medium tracking-tight mb-2">
            SOC 2 Type II — on the roadmap for the dedicated build.
          </h2>
          <p className="text-sm sp-subtle leading-relaxed">
            The shared platform's roadmap above is one stream of work. The
            other is the <b className="text-white/85">Refine reference build</b> — a
            dedicated single-tenant deployment template we operate
            separately. When we engage an auditor it will be scoped to this
            template; once the Type II attestation lands, every Refine
            customer inherits the report under NDA without paying for their
            own audit. Until then, Refine customers get the dedicated
            deployment with all controls listed above operating today.
          </p>
          <div className="mt-4 flex gap-2 flex-wrap">
            <Link to="/pricing" className="sp-btn sp-btn-primary text-xs">
              See Refine pricing <ArrowRight className="w-3 h-3" />
            </Link>
            <Link to="/security" className="sp-btn sp-btn-ghost text-xs">
              <FileText className="w-3 h-3" /> Security overview
            </Link>
          </div>
        </section>

        {/* Footer CTA */}
        <div className="mt-10 pt-6 border-t border-white/5 flex items-center justify-between flex-wrap gap-3">
          <div className="text-xs sp-subtle">
            Security review team needs a SIG / CAIQ questionnaire walked through?
          </div>
          <a href="mailto:team@archeforge.com?subject=SOC%202%20roadmap%20question"
             className="sp-btn sp-btn-primary text-xs"
             data-testid="soc2-contact-btn">
            <Mail className="w-3 h-3" /> Talk to security
          </a>
        </div>
      </main>
    </div>
  );
}
