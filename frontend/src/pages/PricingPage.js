import React from "react";
import { Link } from "react-router-dom";
import {
  ArrowRight, Check, Sparkles, Flame, Gem, Hammer, Wrench,
  Megaphone, Globe, Smartphone, PenTool, Search, Mail, Bot,
  Compass, ShieldCheck, Zap,
} from "lucide-react";
import Logo from "../components/Logo";

/**
 * PricingPage — two-axis pricing.
 *
 * Axis 1: Platform subscription (Spark / Tempered / Refine) — clean SaaS.
 * Axis 2: SoulPrint Studio — productized done-for-you services delivered
 *         by the agency arm. Discounted for higher platform tiers so the
 *         platform subscription "earns back" on the first service order.
 *
 * Design intent: prospects shouldn't have to read paragraphs to feel the
 * conversion ladder. Three tier cards above, three flagship Studio cards
 * below, full menu beneath, and a 4-step ladder graphic showing how a
 * free audit becomes a $5k/mo client.
 */

const TIERS = [
  {
    id: "spark",
    name: "Spark",
    tagline: "For solo founders shipping alone",
    icon: Sparkles,
    monthly: 79,
    annual: 59,
    cta: "Start with Spark",
    href: "/auth?mode=register",
    perks: [
      "1 user · 1 organization · 3 teams",
      "Full 150+ agent catalogue",
      "1,500 agent messages / month",
      "10 autonomous mission runs / month",
      "30 free business audits / month",
      "Crucible: 120 min live remote browser / mo",
      "3 GB documents · 250 files",
      "2 social accounts · 3 GitHub repos",
      "Community + 48h email support",
    ],
    studioPerk: "ArcheForge Studios services available at retail",
    accent: "from-amber-400/30 to-orange-400/10",
  },
  {
    id: "tempered",
    name: "Temper",
    tagline: "For teams scaling without scaling headcount",
    icon: Flame,
    monthly: 299,
    annual: 249,
    cta: "Choose Temper",
    href: "/auth?mode=register",
    highlight: true,
    seatsLabel: "5 seats included · $40/seat after (max 20)",
    perks: [
      "5 seats · 3 orgs · unlimited teams",
      "Custom agent creation",
      "5,000 messages / month (pooled)",
      "75 mission runs / month",
      "200 business audits / month",
      "Crucible: 600 min + 3 saved browser profiles",
      "25 GB documents · unlimited files",
      "10 social accounts · unlimited GitHub repos",
      "Proof of Integrity (Bitcoin-anchored)",
      "White-label audit PDFs",
      "Priority support · 24h SLA",
    ],
    studioPerk: "15% off all ArcheForge Studios services + priority queue",
    accent: "from-orange-500/35 to-red-400/10",
  },
  {
    id: "refine",
    name: "Refine",
    tagline: "For agencies + enterprises running ON our platform",
    icon: Gem,
    monthly: null,            // "Custom"
    annual: null,
    startingAt: 1500,
    cta: "Talk to us",
    href: "mailto:hello@soulprintengine.ai?subject=Refine%20tier%20enquiry",
    perks: [
      "Unlimited seats · unlimited orgs · multi-workspace",
      "Custom agent personalities co-designed with our team",
      "Fair-use everything (100k msgs · 1k missions · 2k audits / mo)",
      "Crucible: unlimited + unlimited saved profiles",
      "Bring-your-own LLM key option",
      "White-label everything: domain, logo, client portal",
      "SSO/SAML · SCIM · DPA · custom data retention",
      "Dedicated infra option (your own cluster)",
      "Dedicated CSM + 4h SLA + QBR",
    ],
    studioPerk: "$2,000/mo ArcheForge credits included + 30% off everything after",
    accent: "from-violet-500/30 to-fuchsia-400/10",
  },
];

const STUDIO_SERVICES = [
  {
    id: "ads-combined",
    name: "Paid Ads — Google + Meta",
    blurb: "We set up, launch, and manage campaigns end-to-end. AI strategy from your Engine, human hands on the keyboard.",
    scope: "Full setup + 30 days managed",
    retail: 3750, unit: "/mo",
    icon: Megaphone,
    flagship: true,
  },
  {
    id: "site-marketing",
    name: "Marketing Website — 10 pages + CMS",
    blurb: "Full brand site, headless CMS, agent-friendly content schema so the Engine can update it post-launch.",
    scope: "Design + build + launch",
    retail: 12500, unit: "flat",
    icon: Globe,
    flagship: true,
  },
  {
    id: "app-mvp",
    name: "Web App MVP — 4-week sprint",
    blurb: "Production-ready MVP. We scope with the Engine, ship with humans, hand you the keys.",
    scope: "4-week build sprint",
    retail: 22500, unit: "flat",
    icon: Smartphone,
    flagship: true,
  },
  {
    id: "ads-google",
    name: "Paid Ads — Google only",
    blurb: "Search, Performance Max, and remarketing. Weekly reporting.",
    scope: "Setup + 30 days managed",
    retail: 2500, unit: "/mo",
    icon: Megaphone,
  },
  {
    id: "ads-meta",
    name: "Paid Ads — Meta only",
    blurb: "Facebook + Instagram. Creative refresh every 14 days.",
    scope: "Setup + 30 days managed",
    retail: 2500, unit: "/mo",
    icon: Megaphone,
  },
  {
    id: "site-5",
    name: "Marketing Website — 5 pages",
    blurb: "Lean brand site. Webflow or React, your call.",
    scope: "Design + build",
    retail: 6500, unit: "flat",
    icon: Globe,
  },
  {
    id: "brand",
    name: "Brand Identity",
    blurb: "Logo, type, palette, guidelines, sticker pack. The whole identity.",
    scope: "2-week sprint",
    retail: 3500, unit: "flat",
    icon: PenTool,
  },
  {
    id: "social",
    name: "Content + Social Management",
    blurb: "8 posts/week on one platform. AI-drafted, human-edited.",
    scope: "Monthly retainer",
    retail: 1800, unit: "/mo",
    icon: Megaphone,
  },
  {
    id: "seo",
    name: "SEO Retainer",
    blurb: "Tech audit, content production, link building, monthly report.",
    scope: "Monthly retainer",
    retail: 2500, unit: "/mo",
    icon: Search,
  },
  {
    id: "email",
    name: "Email + Marketing Automation",
    blurb: "Lifecycle sequences, broadcast cadence, segmentation built into your stack.",
    scope: "Setup + 90 days nurture",
    retail: 3500, unit: "flat",
    icon: Mail,
  },
  {
    id: "agent-custom",
    name: "Custom Agent Build",
    blurb: "Bespoke agent personality, tools, training data, and KPIs — designed with our team.",
    scope: "Per agent",
    retail: 1500, unit: "flat",
    icon: Bot,
  },
  {
    id: "strategy",
    name: "Strategy Sprint",
    blurb: "2-week deep dive. Foundry plus our team produce your 90-day execution roadmap.",
    scope: "2-week engagement",
    retail: 2500, unit: "flat",
    icon: Compass,
    bestseller: true,
  },
];

const FAQ = [
  {
    q: "How does ArcheForge pricing actually work for Temper and Refine?",
    a: "Temper members get 15% off every productized ArcheForge service automatically and skip the queue. Refine members get $2,000/mo of ArcheForge credits applied to any service, and 30% off everything beyond that. Both discounts apply on the day you subscribe — no waiting period.",
  },
  {
    q: "Do I have to use ArcheForge? Can I just use the platform?",
    a: "Of course. Foundry stands on its own — most Spark customers never touch ArcheForge. Studios exists for when Foundry tells you what to do and you'd rather have us do it than do it yourself.",
  },
  {
    q: "What's Crucible?",
    a: "Crucible is Foundry's live remote browser layer. When an agent needs to log into a system, fill a form, or click through something a scraper can't, it opens a Chromium session and streams the screen back to you in real time. You can take over with a click, save the session as a saved login, and your agents reuse it from then on.",
  },
  {
    q: "Is there a free trial?",
    a: "Yes — every public business audit you generate at /get-audit includes a 24-hour access code to the full platform. No card required.",
  },
  {
    q: "Annual vs monthly?",
    a: "Annual is ~17% off (two months free) and locks in your seat count at sign-up rate. Monthly is month-to-month, cancel anytime.",
  },
];


function Money({ value }) {
  return <span className="font-medium tracking-tight">${value.toLocaleString()}</span>;
}

function TierCard({ tier }) {
  const Icon = tier.icon;
  return (
    <div
      className={`relative sp-glass-strong rounded-3xl p-6 sm:p-8 flex flex-col gap-5 ${
        tier.highlight ? "ring-2 ring-orange-400/40 sp-fade-up" : "sp-fade-up"
      }`}
      data-testid={`pricing-tier-${tier.id}`}
    >
      <div
        className={`absolute inset-0 -z-10 rounded-3xl bg-gradient-to-br ${tier.accent} opacity-50 pointer-events-none`}
      />
      {tier.highlight && (
        <span className="absolute -top-3 left-1/2 -translate-x-1/2 text-[10px] uppercase tracking-widest bg-orange-500 text-white px-3 py-1 rounded-full">
          Most teams pick this
        </span>
      )}
      <div className="flex items-center gap-2">
        <Icon className="w-5 h-5" />
        <h3 className="sp-display text-3xl">{tier.name}</h3>
      </div>
      <p className="text-sm sp-subtle leading-relaxed">{tier.tagline}</p>

      <div>
        {tier.monthly != null ? (
          <>
            <div className="flex items-baseline gap-2">
              <Money value={tier.monthly} />
              <span className="text-xs sp-subtle">/ month</span>
            </div>
            <p className="text-[11px] sp-subtle mt-1">
              or <Money value={tier.annual} />/mo billed annually (~17% off)
            </p>
            {tier.seatsLabel && (
              <p className="text-[11px] sp-subtle mt-1 italic">{tier.seatsLabel}</p>
            )}
          </>
        ) : (
          <>
            <div className="flex items-baseline gap-2">
              <span className="sp-display text-3xl">Custom</span>
            </div>
            <p className="text-[11px] sp-subtle mt-1">
              Starting at <Money value={tier.startingAt} />/mo
            </p>
          </>
        )}
      </div>

      <div className="border-t border-white/10 pt-4 flex-1">
        <ul className="space-y-2">
          {tier.perks.map((p) => (
            <li key={p} className="flex gap-2 text-sm leading-snug">
              <Check className="w-4 h-4 text-emerald-400 mt-0.5 flex-shrink-0" />
              <span>{p}</span>
            </li>
          ))}
        </ul>
      </div>

      <div className="sp-glass rounded-xl p-3 text-xs flex gap-2">
        <Wrench className="w-3.5 h-3.5 text-orange-300 mt-0.5 flex-shrink-0" />
        <span className="leading-relaxed">
          <span className="uppercase tracking-widest text-[9px] sp-subtle block mb-0.5">
            ArcheForge perks
          </span>
          {tier.studioPerk}
        </span>
      </div>

      <Link
        to={tier.href}
        className={`sp-btn ${tier.highlight ? "sp-btn-primary" : "sp-btn-ghost"} justify-center text-sm`}
        data-testid={`pricing-cta-${tier.id}`}
      >
        {tier.cta} <ArrowRight className="w-4 h-4" />
      </Link>
    </div>
  );
}

function StudioCard({ s }) {
  const Icon = s.icon;
  return (
    <div
      className={`sp-glass rounded-2xl p-5 flex flex-col gap-3 ${
        s.flagship ? "ring-1 ring-white/15" : ""
      }`}
      data-testid={`studio-card-${s.id}`}
    >
      <div className="flex items-start justify-between">
        <div className="w-9 h-9 rounded-xl sp-glass-strong flex items-center justify-center">
          <Icon className="w-4 h-4" />
        </div>
        {s.bestseller && (
          <span className="text-[9px] uppercase tracking-widest text-amber-300 bg-amber-400/10 px-2 py-1 rounded-full">
            Bestseller
          </span>
        )}
      </div>
      <h4 className="sp-display text-lg leading-snug">{s.name}</h4>
      <p className="text-xs sp-subtle leading-relaxed flex-1">{s.blurb}</p>
      <div className="border-t border-white/10 pt-3 flex items-end justify-between gap-2">
        <div>
          <div className="text-[10px] uppercase tracking-widest sp-subtle">
            {s.scope}
          </div>
          <div className="text-base font-medium tracking-tight">
            ${s.retail.toLocaleString()}
            <span className="text-xs sp-subtle ml-1">{s.unit}</span>
          </div>
        </div>
        <div className="text-right">
          <div className="text-[10px] uppercase tracking-widest sp-subtle">
            Tempered / Refine
          </div>
          <div className="text-xs">
            <span className="text-orange-300">
              -15% / -30%
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

const LADDER = [
  { step: 1, icon: Search, name: "Free Audit",
    blurb: "Drop a URL on the homepage. Get a real audit in 60 seconds, no card.",
    cta: "Try it free", href: "/get-audit" },
  { step: 2, icon: Sparkles, name: "Spark",
    blurb: "$79/mo. Foundry tells you what to do — you ship it yourself.",
    cta: null },
  { step: 3, icon: Flame, name: "Temper",
    blurb: "$299/mo. Your team is using it. Some things they hand to ArcheForge Studios.",
    cta: null },
  { step: 4, icon: Gem, name: "Refine + ArcheForge retainer",
    blurb: "Custom. Foundry plans, ArcheForge executes. End-to-end.",
    cta: null },
];


export default function PricingPage() {
  return (
    <div className="relative min-h-screen overflow-x-hidden">
      <div className="pointer-events-none fixed inset-0 -z-10">
        <div className="sp-aurora" />
      </div>

      {/* Nav */}
      <header className="relative z-10 flex items-center justify-between px-6 md:px-10 py-6">
        <Link to="/" className="flex items-center gap-2" data-testid="pricing-brand">
          <Logo variant="wordmark" className="h-9 w-auto" />
        </Link>
        <nav className="hidden md:flex items-center gap-8 text-sm sp-subtle">
          <Link to="/about" className="hover:text-white transition-colors">About</Link>
          <Link to="/pricing" className="text-white">Pricing</Link>
          <Link to="/trust" className="hover:text-white transition-colors">Trust</Link>
        </nav>
        <div className="flex items-center gap-2">
          <Link to="/auth" className="sp-btn sp-btn-ghost text-sm" data-testid="pricing-nav-signin">
            Sign in
          </Link>
          <Link to="/auth?mode=register" className="sp-btn sp-btn-primary text-sm">
            Get started <ArrowRight className="w-4 h-4" />
          </Link>
        </div>
      </header>

      {/* Hero */}
      <section className="relative z-10 max-w-6xl mx-auto px-6 md:px-10 pt-10 pb-16 text-center sp-fade-up">
        <span className="inline-flex items-center gap-2 text-[10px] uppercase tracking-widest sp-glass rounded-full px-3 py-1 mb-6">
          <Hammer className="w-3 h-3" /> The foundry
        </span>
        <h1 className="sp-display text-4xl sm:text-5xl lg:text-6xl max-w-3xl mx-auto leading-tight">
          Start with a <span className="text-amber-300">Spark</span>.
          Scale through <span className="text-orange-300">Temper</span>.
          Finish in <span className="text-violet-200">Refine</span>.
        </h1>
        <p className="text-base sm:text-lg sp-subtle mt-5 max-w-2xl mx-auto leading-relaxed">
          A working crew of AI agents at every plan. And when Foundry tells you what to do,{" "}
          <span className="text-white font-medium">ArcheForge Studios</span> will do it for you.
        </p>
      </section>

      {/* Platform tiers */}
      <section className="relative z-10 max-w-6xl mx-auto px-6 md:px-10 pb-20"
               data-testid="pricing-tiers-section">
        <div className="grid md:grid-cols-3 gap-5 lg:gap-6">
          {TIERS.map((t) => <TierCard key={t.id} tier={t} />)}
        </div>
        <p className="text-[11px] sp-subtle text-center mt-6 italic">
          All plans include OpenTimestamps proof-of-integrity, brute-force lockout,
          CSP enforcement, and encrypted credential vault. We never sell or train on
          your data — see <Link to="/trust" className="underline">Trust &amp; Security</Link>.
        </p>
      </section>

      {/* ArcheForge Studios */}
      <section className="relative z-10 max-w-6xl mx-auto px-6 md:px-10 py-20 border-t border-white/10"
               data-testid="studio-section">
        <div className="text-center mb-12">
          <span className="inline-flex items-center gap-2 text-[10px] uppercase tracking-widest sp-glass rounded-full px-3 py-1 mb-4">
            <Wrench className="w-3 h-3" /> Done for you
          </span>
          <h2 className="sp-display text-3xl sm:text-4xl lg:text-5xl max-w-3xl mx-auto leading-tight">
            We don't just sell software.
            <br />
            We'll <span className="text-orange-300">ship the work</span> for you.
          </h2>
          <p className="text-base sp-subtle mt-4 max-w-2xl mx-auto leading-relaxed">
            ArcheForge Studios is our agency arm. Productized, fixed-price
            engagements for the things Foundry recommends but you don't have
            time to execute. Temper members save 15% on every line item.
            Refine members get $2k/mo of credits and 30% off everything
            beyond that.
          </p>
        </div>

        <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-4">
          {STUDIO_SERVICES.map((s) => <StudioCard key={s.id} s={s} />)}
        </div>

        <div className="sp-glass-strong rounded-2xl p-6 mt-8 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
          <div>
            <h3 className="sp-display text-xl">Need something not on the ArcheForge menu?</h3>
            <p className="text-sm sp-subtle mt-1">
              Custom builds, integrations, fractional CMO/CTO retainers — all available.
            </p>
          </div>
          <a
            href="mailto:hello@archeforge.studio?subject=Custom%20ArcheForge%20engagement"
            className="sp-btn sp-btn-primary text-sm"
            data-testid="studio-custom-cta"
          >
            Tell us what you need <ArrowRight className="w-4 h-4" />
          </a>
        </div>
      </section>

      {/* Conversion ladder */}
      <section className="relative z-10 max-w-6xl mx-auto px-6 md:px-10 py-20 border-t border-white/10"
               data-testid="pricing-ladder">
        <div className="text-center mb-10">
          <span className="inline-flex items-center gap-2 text-[10px] uppercase tracking-widest sp-glass rounded-full px-3 py-1 mb-4">
            <Zap className="w-3 h-3" /> How it works together
          </span>
          <h2 className="sp-display text-3xl sm:text-4xl max-w-3xl mx-auto leading-tight">
            One foundry. Four temperatures.
          </h2>
        </div>
        <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-3">
          {LADDER.map((l, i) => {
            const Icon = l.icon;
            return (
              <div key={l.step}
                   className="sp-glass rounded-2xl p-5 flex flex-col gap-3">
                <div className="flex items-center justify-between">
                  <span className="text-[10px] uppercase tracking-widest sp-subtle">
                    Step {l.step}
                  </span>
                  <Icon className="w-4 h-4 sp-subtle" />
                </div>
                <h4 className="sp-display text-xl">{l.name}</h4>
                <p className="text-xs sp-subtle leading-relaxed flex-1">{l.blurb}</p>
                {l.cta && (
                  <Link to={l.href} className="sp-btn sp-btn-ghost text-xs justify-center">
                    {l.cta} <ArrowRight className="w-3 h-3" />
                  </Link>
                )}
                {i < LADDER.length - 1 && (
                  <div className="hidden lg:block absolute -right-3 top-1/2 -translate-y-1/2">
                    <ArrowRight className="w-4 h-4 sp-subtle" />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </section>

      {/* FAQ */}
      <section className="relative z-10 max-w-3xl mx-auto px-6 md:px-10 py-20 border-t border-white/10">
        <h2 className="sp-display text-3xl sm:text-4xl text-center mb-10">
          Common questions
        </h2>
        <div className="space-y-3">
          {FAQ.map((f, i) => (
            <details key={i} className="sp-glass rounded-2xl p-5 group">
              <summary className="cursor-pointer flex items-start justify-between gap-3 list-none">
                <span className="sp-display text-base">{f.q}</span>
                <ArrowRight className="w-4 h-4 sp-subtle transition-transform group-open:rotate-90 mt-1 flex-shrink-0" />
              </summary>
              <p className="text-sm sp-subtle mt-3 leading-relaxed">{f.a}</p>
            </details>
          ))}
        </div>
      </section>

      {/* Final CTA */}
      <section className="relative z-10 max-w-4xl mx-auto px-6 md:px-10 py-20">
        <div className="sp-glass-strong rounded-3xl p-10 md:p-14 text-center flex flex-col items-center gap-5">
          <ShieldCheck className="w-7 h-7 text-amber-300" />
          <h3 className="sp-display text-3xl sm:text-4xl max-w-2xl">
            Light the spark. Temper the work. Refine the outcome.
          </h3>
          <p className="sp-subtle text-sm max-w-xl">
            14-day money-back on Spark. 30-day money-back on Tempered. Refine
            ships with a dedicated CSM from day one.
          </p>
          <div className="flex gap-2">
            <Link to="/get-audit" className="sp-btn sp-btn-ghost"
                  data-testid="pricing-final-audit">
              Try a free audit
            </Link>
            <Link to="/auth?mode=register" className="sp-btn sp-btn-primary"
                  data-testid="pricing-final-cta">
              Start with Spark <ArrowRight className="w-4 h-4" />
            </Link>
          </div>
        </div>
      </section>

      <footer className="relative z-10 max-w-6xl mx-auto px-6 md:px-10 py-10 text-center text-xs sp-subtle">
        Foundry · ArcheForge Studios · est. 2026
      </footer>
    </div>
  );
}
