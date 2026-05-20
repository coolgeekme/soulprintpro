import React from "react";
import { Link } from "react-router-dom";
import {
  ShieldCheck, Lock, KeyRound, Eye, EyeOff, Anchor, FileLock,
  UserCheck, AlertTriangle, Check, X, ArrowRight, Mail,
  Fingerprint, Activity, Database, Wand2,
} from "lucide-react";
import Logo from "../components/Logo";

/**
 * /trust — the marketing-facing security page.
 *
 * Distinct from /security (which is the structured policy doc auto-rendered
 * from /api/security). This page is for non-technical buyers: founders,
 * CFOs, procurement, and security-skeptical org leaders. The narrative
 * arc:
 *
 *   1. Hero — "Built so business teams don't have to compromise."
 *   2. The four trust pillars (visual grid)
 *   3. Comparison table: typical AI platforms vs Foundry
 *   4. Differentiator deep-dives (Proof of Integrity, Credential Vault,
 *      Private Chief of Staff, Brute-force defence) with real concrete
 *      claims — every line is shippable today, no hand-waving.
 *   5. Encryption in transit + at rest
 *   6. Compliance posture
 *   7. CTA — talk to our security team / read the structured policy
 *
 * The visual language is intentionally heavier than the rest of the
 * marketing site: bigger glass cards, accent gradients, monospaced
 * proofs. We want a buyer scrolling this page to feel that we treat
 * security as a product feature, not paperwork.
 */
export default function TrustPage() {
  return (
    <div className="min-h-screen" data-testid="trust-page">
      {/* Public top bar — auth-free so prospects can read without
          ever signing in. */}
      <header className="border-b border-white/5 sticky top-0 z-30 backdrop-blur-md"
              style={{
                background: "color-mix(in srgb, var(--bg) 75%, transparent)",
                paddingTop: "calc(env(safe-area-inset-top, 0px))",
              }}>
        <div className="max-w-6xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between">
          <Link to="/" className="inline-flex items-center gap-2">
            <Logo variant="wordmark" className="h-6 w-auto" />
          </Link>
          <nav className="flex items-center gap-1">
            <Link to="/security" className="sp-btn sp-btn-ghost text-xs hidden sm:inline-flex"
                  data-testid="trust-policy-link">
              Technical policy
            </Link>
            <Link to="/about" className="sp-btn sp-btn-ghost text-xs">About</Link>
            <Link to="/auth" className="sp-btn sp-btn-primary text-xs">
              Get started <ArrowRight className="w-3 h-3" />
            </Link>
          </nav>
        </div>
      </header>

      <main>
        <Hero />
        <Pillars />
        <Comparison />
        <ProofOfIntegrityDeepDive />
        <CredentialVaultDeepDive />
        <PrivateCoSDeepDive />
        <BruteForceDeepDive />
        <Encryption />
        <Compliance />
        <FinalCTA />
      </main>

      <footer className="border-t border-white/5 mt-12">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 py-8 text-xs sp-subtle flex items-center justify-between flex-wrap gap-3">
          <div>© Foundry — security-first AI for business teams.</div>
          <div className="flex items-center gap-4">
            <Link to="/security" className="hover:text-white">Technical policy</Link>
            <Link to="/about" className="hover:text-white">About</Link>
            <a href="mailto:team@archeforge.com" className="hover:text-white">Contact</a>
          </div>
        </div>
      </footer>
    </div>
  );
}


// ── Sections ──────────────────────────────────────────────────────────────


function Hero() {
  return (
    <section className="max-w-6xl mx-auto px-4 sm:px-6 pt-16 sm:pt-24 pb-12">
      <div className="inline-flex items-center gap-1.5 sp-glass rounded-full px-3 py-1 text-[10px] uppercase tracking-widest sp-subtle mb-5">
        <ShieldCheck className="w-3 h-3" /> Trust, security & privacy
      </div>
      <h1 className="text-3xl sm:text-5xl lg:text-6xl font-medium tracking-tight leading-[1.05] max-w-4xl">
        Built so your team can move fast — and your security review can{" "}
        <span style={{
          background: "linear-gradient(120deg, rgba(251,146,60,1), rgba(74,222,128,1))",
          WebkitBackgroundClip: "text",
          WebkitTextFillColor: "transparent",
        }}>say yes</span>.
      </h1>
      <p className="mt-5 text-base sm:text-lg sp-subtle leading-relaxed max-w-2xl">
        A SOC 2 PDF tells you an auditor checked a list once. We took a
        different approach in parallel: every artifact your agents create on
        Foundry — every PDF, audit, brief, exported report — gets a SHA-256
        fingerprint anchored on the Bitcoin blockchain via OpenTimestamps
        within ~6 hours. So does a daily Merkle root over key business
        records (mission events, workspace invites used, org members joined,
        audit leads captured). Any third party can re-hash a record and
        verify the proof with the open-source <code className="text-[12px] px-1 sp-glass rounded">ots</code> CLI
        — no cooperation from us required.
      </p>
      <div className="mt-7 flex items-center gap-2 flex-wrap">
        <a href="#comparison" className="sp-btn sp-btn-primary text-sm" data-testid="trust-cta-compare">
          See how we compare <ArrowRight className="w-4 h-4" />
        </a>
        <Link to="/security" className="sp-btn sp-btn-ghost text-sm">
          Read the technical policy
        </Link>
        <a href="mailto:team@archeforge.com?subject=Security%20review%20request"
           className="sp-btn sp-btn-ghost text-sm" data-testid="trust-cta-talk">
          <Mail className="w-4 h-4" /> Talk to our security team
        </a>
      </div>
    </section>
  );
}


function Pillars() {
  const pillars = [
    {
      icon: Lock,
      title: "Encrypted everywhere",
      blurb: "TLS 1.2+ with HSTS preload in transit. AES-256 at rest. Browser session credentials separately encrypted with Fernet (AES-128-CBC + HMAC-SHA256) — even our admins can't decrypt them.",
    },
    {
      icon: Anchor,
      title: "Tamper-evident",
      blurb: "Every artifact your agents create is hashed and anchored on the Bitcoin blockchain via OpenTimestamps. You can prove a document hasn't been altered — without trusting us.",
      accent: true,
    },
    {
      icon: UserCheck,
      title: "Per-org isolation by default",
      blurb: "Workspace > Organization > Team > Conversation. Every endpoint validates membership before returning a row. Closed teams are invisible to non-members. Period.",
    },
    {
      icon: EyeOff,
      title: "Privacy that means it",
      blurb: "Your private Chief of Staff threads are visible only to you and your org owner (for audit). No other member, no other org, no agent training. We never train on your data.",
    },
  ];
  return (
    <section className="max-w-6xl mx-auto px-4 sm:px-6 py-10 sm:py-14">
      <h2 className="text-2xl sm:text-3xl font-medium tracking-tight mb-2">
        Four security pillars, four real implementations.
      </h2>
      <p className="sp-subtle text-sm sm:text-base mb-7 max-w-2xl">
        Not "we take security seriously." Here's exactly what we do — and how
        you can verify it yourself.
      </p>
      <div className="grid sm:grid-cols-2 gap-3">
        {pillars.map((p) => (
          <div
            key={p.title}
            className={`rounded-2xl p-5 sm:p-6 ${p.accent ? "border" : "sp-glass"}`}
            style={p.accent ? {
              background: "linear-gradient(135deg, rgba(251,146,60,0.10), rgba(251,146,60,0.02))",
              borderColor: "rgba(251,146,60,0.25)",
            } : undefined}
          >
            <div className="flex items-start gap-3">
              <div className="w-10 h-10 rounded-xl flex items-center justify-center shrink-0"
                   style={{ background: p.accent ? "rgba(251,146,60,0.15)" : undefined }}>
                <p.icon className={`w-4 h-4 ${p.accent ? "text-orange-300" : "text-white/85"}`} />
              </div>
              <div className="flex-1">
                <div className="font-medium text-base mb-1">{p.title}</div>
                <div className="text-sm sp-subtle leading-relaxed">{p.blurb}</div>
              </div>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}


function Comparison() {
  // Be honest. "Most AI platforms" is a fair shorthand for the set of
  // major B2B AI tools we benchmark against. Every column we claim a
  // checkmark is shippable today — see the deep-dives below for the
  // exact implementation.
  const rows = [
    { feature: "Encryption in transit (TLS 1.2+)", us: true, them: true },
    { feature: "Encryption at rest (AES-256)", us: true, them: true },
    { feature: "Per-org / per-team data isolation", us: true, them: "partial" },
    { feature: "Brute-force lockout on login", us: true, them: "partial" },
    { feature: "Credential vault encrypted so even admins can't read it", us: true, them: false },
    { feature: "Private Chief of Staff threads (audit-only owner access)", us: true, them: false },
    { feature: "Bitcoin-anchored proof of integrity for every artifact", us: true, them: false },
    { feature: "Public, share-safe verify URLs anyone can check", us: true, them: false },
    { feature: "Daily Merkle anchor of audit records", us: true, them: false },
    { feature: "We never train on your data", us: true, them: "partial" },
    { feature: "Public, JSON-queryable security policy", us: true, them: false },
  ];
  return (
    <section id="comparison" className="max-w-6xl mx-auto px-4 sm:px-6 py-10 sm:py-14">
      <h2 className="text-2xl sm:text-3xl font-medium tracking-tight mb-2">
        How we compare to "most AI platforms".
      </h2>
      <p className="sp-subtle text-sm sm:text-base mb-7 max-w-2xl">
        The features below aren't roadmap items — they're shipped, in
        production, verifiable today.
      </p>
      <div className="sp-glass-strong rounded-2xl overflow-hidden">
        <div className="grid grid-cols-[1fr_120px_120px] gap-px text-sm">
          <div className="bg-black/30 px-4 sm:px-5 py-3 text-[10px] uppercase tracking-widest sp-subtle">
            Feature
          </div>
          <div className="bg-black/30 px-3 py-3 text-[10px] uppercase tracking-widest sp-subtle text-center">
            Most platforms
          </div>
          <div className="bg-black/30 px-3 py-3 text-[10px] uppercase tracking-widest text-center font-medium"
               style={{ color: "rgba(251,146,60,1)" }}>
            Foundry
          </div>
          {rows.map((r, i) => (
            <React.Fragment key={i}>
              <div className="px-4 sm:px-5 py-3 border-t border-white/5">
                {r.feature}
              </div>
              <Cell value={r.them} compact />
              <Cell value={r.us} highlight />
            </React.Fragment>
          ))}
        </div>
      </div>
    </section>
  );
}


function Cell({ value, highlight, compact }) {
  let content;
  if (value === true) {
    content = (
      <div className="inline-flex items-center justify-center w-7 h-7 rounded-full"
           style={{
             background: highlight ? "rgba(74,222,128,0.15)" : "rgba(255,255,255,0.05)",
             color: highlight ? "rgba(74,222,128,1)" : "rgba(255,255,255,0.6)",
           }}>
        <Check className="w-3.5 h-3.5" />
      </div>
    );
  } else if (value === false) {
    content = (
      <div className="inline-flex items-center justify-center w-7 h-7 rounded-full bg-red-500/10 text-red-400/80">
        <X className="w-3.5 h-3.5" />
      </div>
    );
  } else {
    // partial / "sometimes"
    content = (
      <div className="text-[10px] uppercase tracking-widest sp-subtle">
        Varies
      </div>
    );
  }
  return (
    <div className={`px-3 py-3 border-t border-white/5 flex items-center justify-center ${compact ? "" : ""}`}>
      {content}
    </div>
  );
}


function ProofOfIntegrityDeepDive() {
  return (
    <Deep
      eyebrow="Proof of Integrity · Bitcoin-anchored"
      icon={Anchor}
      title="Cryptographic proof. No vendor lock-in."
      body={
        <>
          <p>
            Every PDF, brief, and audit your agents produce gets its SHA-256
            hash submitted to the OpenTimestamps public calendar network.
            Within 1-6 hours that hash is anchored into a Bitcoin block —
            permanent, immutable, verifiable by anyone with a copy of the
            file and the standard <code className="px-1 sp-glass rounded">ots</code> CLI.
          </p>
          <p>
            <span className="text-white/85 font-medium">What this kills:</span>{" "}
            "Did SoulPrint silently edit our audit?" "Did someone tamper with
            our compliance report?" "Can we prove this brief existed on
            Mar 12?" Yes. Yes. And yes.
          </p>
          <p>
            <span className="text-white/85 font-medium">Vendor-lock-free:</span>{" "}
            Anyone — your auditor, your customer, a journalist — can hit a
            public verify URL like <code className="px-1 sp-glass rounded">/v/&lt;file_id&gt;</code>{" "}
            and confirm the proof without ever logging into SoulPrint. Even
            if we disappeared tomorrow, your proofs would still be valid on
            Bitcoin forever.
          </p>
        </>
      }
      stat={{ value: "0¢", label: "Cost to your team. Forever." }}
      ctas={[
        { to: "/security", label: "Read the technical policy", primary: false },
      ]}
      accent="orange"
    />
  );
}


function CredentialVaultDeepDive() {
  return (
    <Deep
      eyebrow="Credential Vault · Encrypted at the field level"
      icon={FileLock}
      title="Saved logins our own admins can't read."
      body={
        <>
          <p>
            When an agent logs into a third-party site on your behalf — your
            CRM, your social inbox, your supplier portal — the cookies and
            session state are encrypted with{" "}
            <span className="text-white/85 font-medium">Fernet</span>{" "}
            (AES-128-CBC + HMAC-SHA256) before they ever touch the database.
          </p>
          <p>
            The decryption key (<code className="px-1 sp-glass rounded">SOULPRINT_VAULT_KEY</code>)
            lives in our backend env, separate from the database. Even a full
            DB dump would yield ciphertext only. Even our infra team can't
            read your saved logins.
          </p>
          <p>
            <span className="text-white/85 font-medium">Bonus:</span>{" "}
            credentials persist across our redeploys (so your agent's
            existing logged-in session keeps working) — but only because the
            encrypted blob survives, never because we copy plaintext anywhere.
          </p>
        </>
      }
      stat={{ value: "AES‑128", label: "Vault encryption (CBC + HMAC)." }}
      accent="violet"
    />
  );
}


function PrivateCoSDeepDive() {
  return (
    <Deep
      eyebrow="Privacy boundaries · Per-user threads"
      icon={EyeOff}
      title="Your private Chief of Staff is actually private."
      body={
        <>
          <p>
            Inside every organization, each user gets a{" "}
            <span className="text-white/85 font-medium">private Chief of Staff thread</span>
            {" "}— a personal AI co-pilot for thinking out loud, drafting
            sensitive memos, working through people problems. These threads
            are visible only to:
          </p>
          <ul className="space-y-1.5 mt-1">
            <li className="flex gap-2"><Check className="w-4 h-4 text-green-400 shrink-0 mt-0.5" /><span>You — the person who started the thread</span></li>
            <li className="flex gap-2"><Check className="w-4 h-4 text-green-400 shrink-0 mt-0.5" /><span>Your org owner — for audit moderation only, never for browsing</span></li>
            <li className="flex gap-2"><X className="w-4 h-4 text-red-400 shrink-0 mt-0.5" /><span>Other org members — never. Not even admins.</span></li>
            <li className="flex gap-2"><X className="w-4 h-4 text-red-400 shrink-0 mt-0.5" /><span>Other organizations — never. Workspaces are hard isolation boundaries.</span></li>
          </ul>
          <p className="mt-3">
            <span className="text-white/85 font-medium">Closed teams</span>{" "}
            extend the same boundary to team-level conversations: the Mind
            Map, activity feed, and document library all filter out content
            from teams a user isn't a member of.
          </p>
        </>
      }
      stat={{ value: "0", label: "Other members can ever see your private thread." }}
      accent="green"
    />
  );
}


function BruteForceDeepDive() {
  return (
    <Deep
      eyebrow="Account protection · Built-in"
      icon={AlertTriangle}
      title="Brute-force lockout, rate limits, and CSP — turned on, today."
      body={
        <>
          <p>
            Login is throttled per (IP × email): 8 failed password attempts
            in 15 minutes triggers a 15-minute lockout with a clear
            "try again in N minutes" message. Successful login wipes the
            counter.
          </p>
          <p>
            Lead capture, account registration, and invite acceptance all sit
            behind sliding-window rate limits — so a bot trying to enumerate
            accounts or harvest invite codes hits a wall fast.
          </p>
          <p>
            Every response carries{" "}
            <span className="text-white/85 font-medium">HSTS preload</span>,{" "}
            <span className="text-white/85 font-medium">X-Frame-Options</span>,{" "}
            <span className="text-white/85 font-medium">X-Content-Type-Options</span>,
            a{" "}
            <span className="text-white/85 font-medium">strict CSP</span>,
            and a{" "}
            <span className="text-white/85 font-medium">Permissions-Policy</span>{" "}
            that locks geolocation / mic / camera to first-party only and
            disables payment APIs entirely.
          </p>
        </>
      }
      stat={{ value: "8 / 15m", label: "Failed attempts → 15-minute lockout." }}
      accent="amber"
    />
  );
}


function Encryption() {
  return (
    <section className="max-w-6xl mx-auto px-4 sm:px-6 py-10 sm:py-14">
      <h2 className="text-2xl sm:text-3xl font-medium tracking-tight mb-2">
        Encryption — boring but rigorous.
      </h2>
      <p className="sp-subtle text-sm sm:text-base mb-7 max-w-2xl">
        Three layers, three different keys, three different blast radiuses.
      </p>
      <div className="grid sm:grid-cols-3 gap-3">
        <Pill
          icon={Database}
          eyebrow="At rest"
          title="MongoDB Atlas · AES-256"
          body="Full-disk encryption on the managed database. Mirrored to encrypted GridFS for object storage."
        />
        <Pill
          icon={Activity}
          eyebrow="In transit"
          title="TLS 1.2+ · HSTS preload"
          body="Every endpoint, every WebSocket, every API call. HSTS with includeSubDomains and preload, two-year max-age."
        />
        <Pill
          icon={Fingerprint}
          eyebrow="In the vault"
          title="Fernet · AES-128 + HMAC"
          body="Browser session credentials are encrypted independently with a backend-only key before they ever hit the DB."
        />
      </div>
    </section>
  );
}


function Compliance() {
  return (
    <section className="max-w-6xl mx-auto px-4 sm:px-6 py-10 sm:py-14">
      <div className="sp-glass-strong rounded-2xl p-6 sm:p-8">
        <div className="flex items-start gap-3 mb-4">
          <Wand2 className="w-5 h-5 text-orange-300 shrink-0 mt-1" />
          <div>
            <div className="text-[10px] uppercase tracking-widest sp-subtle mb-1">Compliance posture</div>
            <h2 className="text-xl sm:text-2xl font-medium tracking-tight">
              Honest about where we are. Not where we wish we were.
            </h2>
          </div>
        </div>
        <div className="grid sm:grid-cols-2 gap-4 mt-4 text-sm sp-subtle leading-relaxed">
          <div>
            <div className="text-white/85 font-medium mb-1">SOC 2 — preparing for audit</div>
            Encryption (TLS 1.2+ everywhere, AES-256 at rest), per-org
            authorization, brute-force lockout, security headers, and
            superadmin action logging are <b className="text-white/85">operating today</b>.
            Documented incident-response playbook, quarterly access reviews,
            and formal change-management evidence are <b className="text-white/85">in progress</b>.
            We haven't engaged an auditor yet — when we do we'll say so.{" "}
            <a href="/legal/soc2-roadmap"
               className="text-orange-300 hover:text-orange-200 underline-offset-2 hover:underline">
              See the full control roadmap →
            </a>
          </div>
          <div>
            <div className="text-white/85 font-medium mb-1">GDPR — Right-to-erasure honored</div>
            Account deletion purges your data. Data subject requests routed
            to{" "}
            <a href="mailto:team@archeforge.com" className="text-orange-300 hover:text-orange-200 underline-offset-2 hover:underline">
              team@archeforge.com
            </a>
            {" "}with a 24-hour ack target.
          </div>
          <div>
            <div className="text-white/85 font-medium mb-1">No training on your data</div>
            We never use your conversations, files, or business records to
            train models — ours, OpenAI's, Anthropic's, anyone's. Period.
          </div>
          <div>
            <div className="text-white/85 font-medium mb-1">Open questions welcome</div>
            We answer SIG / CAIQ questionnaires. We do customer security
            reviews. We share architecture diagrams. Just{" "}
            <a href="mailto:team@archeforge.com?subject=Security%20review%20request"
               className="text-orange-300 hover:text-orange-200 underline-offset-2 hover:underline">
              email us
            </a>
            .
          </div>
        </div>
      </div>
    </section>
  );
}


function FinalCTA() {
  return (
    <section className="max-w-6xl mx-auto px-4 sm:px-6 py-12 sm:py-16">
      <div className="rounded-2xl p-8 sm:p-12 border text-center"
           style={{
             background: "linear-gradient(135deg, rgba(251,146,60,0.10), rgba(74,222,128,0.04))",
             borderColor: "rgba(251,146,60,0.25)",
           }}>
        <ShieldCheck className="w-10 h-10 text-orange-300 mx-auto mb-4" />
        <h2 className="text-2xl sm:text-4xl font-medium tracking-tight max-w-2xl mx-auto leading-[1.1]">
          Ready to put your security team in the driver's seat?
        </h2>
        <p className="sp-subtle text-sm sm:text-base mt-3 max-w-xl mx-auto leading-relaxed">
          Bring your toughest questions. We'll bring the architecture
          diagrams, the SIG, the encryption key flows, and answers — not
          marketing copy.
        </p>
        <div className="mt-6 flex items-center justify-center gap-2 flex-wrap">
          <a href="mailto:team@archeforge.com?subject=Security%20review%20request"
             className="sp-btn sp-btn-primary text-sm"
             data-testid="trust-final-cta-talk">
            <Mail className="w-4 h-4" /> Talk to security
          </a>
          <Link to="/auth?mode=register" className="sp-btn sp-btn-ghost text-sm"
                data-testid="trust-final-cta-start">
            Start a workspace <ArrowRight className="w-4 h-4" />
          </Link>
          <Link to="/security" className="sp-btn sp-btn-ghost text-sm">
            Read the technical policy
          </Link>
        </div>
      </div>
    </section>
  );
}


// ── Reusable building blocks ──────────────────────────────────────────────


function Deep({ eyebrow, icon: Icon, title, body, stat, ctas, accent }) {
  const accentColor = {
    orange: "rgba(251,146,60,1)",
    green: "rgba(74,222,128,1)",
    violet: "rgba(167,139,250,1)",
    amber: "rgba(251,191,36,1)",
  }[accent || "orange"];
  const accentBg = {
    orange: "rgba(251,146,60,0.10)",
    green: "rgba(74,222,128,0.10)",
    violet: "rgba(167,139,250,0.10)",
    amber: "rgba(251,191,36,0.10)",
  }[accent || "orange"];
  return (
    <section className="max-w-6xl mx-auto px-4 sm:px-6 py-8">
      <div className="sp-glass-strong rounded-2xl p-6 sm:p-8 grid sm:grid-cols-[1.4fr_1fr] gap-6 sm:gap-10 items-start">
        <div>
          <div className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[10px] uppercase tracking-widest mb-3"
               style={{ background: accentBg, color: accentColor }}>
            <Icon className="w-3 h-3" /> {eyebrow}
          </div>
          <h2 className="text-2xl sm:text-3xl font-medium tracking-tight leading-[1.15] mb-3">
            {title}
          </h2>
          <div className="text-sm sm:text-base sp-subtle leading-relaxed space-y-3">
            {body}
          </div>
          {ctas && ctas.length > 0 && (
            <div className="flex items-center gap-2 mt-5 flex-wrap">
              {ctas.map((c) => (
                <Link key={c.to} to={c.to}
                      className={`sp-btn ${c.primary ? "sp-btn-primary" : "sp-btn-ghost"} text-sm`}>
                  {c.label}
                </Link>
              ))}
            </div>
          )}
        </div>
        {stat && (
          <div className="rounded-xl p-5 sm:p-6 border"
               style={{ background: accentBg, borderColor: accentColor + "33" }}>
            <div className="text-4xl sm:text-5xl font-semibold tracking-tight"
                 style={{ color: accentColor }}>
              {stat.value}
            </div>
            <div className="text-xs sp-subtle mt-2 leading-relaxed">{stat.label}</div>
          </div>
        )}
      </div>
    </section>
  );
}


function Pill({ icon: Icon, eyebrow, title, body }) {
  return (
    <div className="sp-glass rounded-2xl p-5">
      <div className="flex items-center gap-2 mb-2">
        <div className="w-8 h-8 rounded-lg sp-glass-strong flex items-center justify-center">
          <Icon className="w-4 h-4 text-orange-300" />
        </div>
        <div className="text-[10px] uppercase tracking-widest sp-subtle">{eyebrow}</div>
      </div>
      <div className="text-sm font-medium mb-1">{title}</div>
      <div className="text-xs sp-subtle leading-relaxed">{body}</div>
    </div>
  );
}
