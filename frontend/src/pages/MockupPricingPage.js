import React, { useMemo } from "react";
import { Link } from "react-router-dom";
import {
  ArrowRight, Check, Sparkles, Hammer, Gem, Flame,
  Megaphone, Globe, Smartphone, PenTool, Search, Mail, Bot,
  Compass, Wrench, ShieldCheck,
} from "lucide-react";
import "../styles/foundry-dark.css";

/**
 * MockupPricingPage — pure-mockup route at /mockup/pricing.
 *
 * Same industrial dark aesthetic as MockupHomePage. Surfaces the full
 * two-axis pricing story (platform tiers + ArcheForge Studios menu)
 * in a high-contrast layout that matches the brand artwork the user
 * just sent.
 */

const TIERS = [
  {
    id: "spark", name: "Spark", icon: Sparkles, visual: "spark",
    headline: "Ignite ideas",
    tagline: "For solo founders shipping alone",
    monthly: 79, annual: 59,
    perks: [
      "1 user · 1 organization · 3 teams",
      "Full 169-agent catalogue",
      "1,500 messages / month",
      "10 autonomous mission runs / month",
      "30 free business audits / month",
      "Crucible: 120 min live remote browser",
      "3 GB documents · 250 files",
      "Community + 48h email support",
    ],
    studioPerk: "ArcheForge services at retail",
    cta: "Start with Spark",
  },
  {
    id: "temper", name: "Temper", icon: Hammer, visual: "anvil",
    headline: "Strengthen with purpose",
    tagline: "For teams scaling without scaling headcount",
    monthly: 299, annual: 249,
    hot: true,
    seatsLabel: "5 seats included · $40/seat after (max 20)",
    perks: [
      "5 seats · 3 orgs · unlimited teams",
      "Custom agent creation",
      "5,000 messages / month (pooled)",
      "75 mission runs · 200 audits / month",
      "Crucible: 600 min + 3 saved browser profiles",
      "25 GB documents · unlimited files",
      "Bitcoin-anchored Proof of Integrity",
      "White-label audit PDFs · 24h support SLA",
    ],
    studioPerk: "15% off all ArcheForge services + priority queue",
    cta: "Choose Temper",
  },
  {
    id: "refine", name: "Refine", icon: Gem, visual: "ingot",
    headline: "Precision that leaves a mark",
    tagline: "For agencies + enterprise running ON Foundry",
    monthly: null, startingAt: 1500,
    perks: [
      "Unlimited seats · unlimited orgs · multi-workspace",
      "Co-designed custom agent personalities",
      "Fair-use limits (100k msgs · 1k missions / mo)",
      "Crucible: unlimited + unlimited saved profiles",
      "Bring-your-own LLM key option",
      "White-label everything: domain, logo, portal",
      "SSO/SAML · SCIM · DPA · custom retention",
      "Dedicated CSM + 4h SLA + QBR",
    ],
    studioPerk: "$2,000/mo ArcheForge credits + 30% off everything after",
    cta: "Talk to us",
  },
];

const STUDIO = [
  { name: "Paid Ads — Google + Meta", retail: 3750, unit: "/mo",
    icon: Megaphone, flagship: true,
    blurb: "End-to-end campaign management. AI strategy from Foundry, human hands on the keys." },
  { name: "Marketing Site (10 pages + CMS)", retail: 12500, unit: "flat",
    icon: Globe, flagship: true,
    blurb: "Full brand site with agent-friendly CMS so Foundry can update it post-launch." },
  { name: "Web App MVP", retail: 22500, unit: "flat",
    icon: Smartphone, flagship: true,
    blurb: "4-week production sprint. Scope with the agents, ship with ArcheForge." },
  { name: "Paid Ads — Google only", retail: 2500, unit: "/mo", icon: Megaphone,
    blurb: "Search + PMax + remarketing. Weekly reporting." },
  { name: "Paid Ads — Meta only", retail: 2500, unit: "/mo", icon: Megaphone,
    blurb: "FB + IG. Creative refresh every 14 days." },
  { name: "Marketing Site (5 pages)", retail: 6500, unit: "flat", icon: Globe,
    blurb: "Lean brand site, Webflow or React." },
  { name: "Brand Identity", retail: 3500, unit: "flat", icon: PenTool,
    blurb: "Logo + type + palette + guidelines + sticker pack." },
  { name: "SEO Retainer", retail: 2500, unit: "/mo", icon: Search,
    blurb: "Tech audit + content + link building + monthly report." },
  { name: "Email + Marketing Automation", retail: 3500, unit: "flat", icon: Mail,
    blurb: "Lifecycle sequences + broadcast cadence wired into your stack." },
  { name: "Strategy Sprint", retail: 2500, unit: "flat", icon: Compass, bestseller: true,
    blurb: "2-week deep dive. Foundry + ArcheForge produce your 90-day roadmap." },
  { name: "Custom Agent Build", retail: 1500, unit: "flat", icon: Bot,
    blurb: "Bespoke agent personality + tools + training data." },
  { name: "Content + Social Mgmt", retail: 1800, unit: "/mo", icon: Megaphone,
    blurb: "8 posts/week on one platform. AI-drafted, human-edited." },
];


function EmberField({ count = 22 }) {
  const dots = useMemo(() => Array.from({ length: count }, (_, i) => ({
    left: `${Math.random() * 100}%`,
    delay: `${Math.random() * 8}s`,
    duration: `${6 + Math.random() * 8}s`,
    scale: 0.6 + Math.random() * 1.2,
    hue: Math.random() > 0.5 ? "var(--fdy-ember)" : "var(--fdy-molten)",
  })), [count]);
  return (
    <div className="fdy-embers" aria-hidden>
      {dots.map((d, i) => (
        <span key={i} className="fdy-ember-dot"
              style={{
                left: d.left, bottom: "-10px",
                animationDelay: d.delay, animationDuration: d.duration,
                transform: `scale(${d.scale})`, background: d.hue,
              }} />
      ))}
    </div>
  );
}


function Wordmark() {
  return (
    <Link to="/mockup/home" className="block leading-none">
      <div className="fdy-wordmark">FOUNDRY</div>
      <div className="fdy-sub-wordmark">
        POWERED BY <span className="accent">SOULPRINT</span> ENGINE
      </div>
    </Link>
  );
}


function TierVisual({ kind }) {
  if (kind === "spark") {
    return (
      <div className="h-20 flex items-end justify-center relative mb-2">
        {Array.from({ length: 5 }).map((_, i) => (
          <span key={i} className="absolute w-px"
                style={{
                  left: `${35 + i * 7}%`, bottom: 0,
                  height: `${30 + Math.random() * 35}px`,
                  background: "linear-gradient(180deg, transparent, var(--fdy-ember), var(--fdy-molten))",
                  opacity: 0.85,
                }} />
        ))}
        <div className="absolute bottom-0 left-1/2 -translate-x-1/2 w-5 h-5 rounded-full"
             style={{
               background: "radial-gradient(circle, var(--fdy-ember), var(--fdy-molten) 60%, transparent)",
               filter: "blur(2px)",
             }} />
      </div>
    );
  }
  if (kind === "anvil") {
    return (
      <div className="h-20 flex items-end justify-center relative mb-2">
        <div className="relative">
          <div className="w-20 h-10 mx-auto"
               style={{
                 background: "linear-gradient(180deg, #2a2a30, #0d0d10)",
                 clipPath: "polygon(0 0, 100% 0, 90% 70%, 100% 100%, 0 100%, 10% 70%)",
               }} />
          <div className="absolute -top-1 left-1/2 -translate-x-1/2 w-14 h-1 rounded-full"
               style={{
                 background: "var(--fdy-molten)",
                 boxShadow: "0 0 14px var(--fdy-molten), 0 0 28px var(--fdy-ember)",
               }} />
        </div>
      </div>
    );
  }
  return (
    <div className="h-20 flex items-center justify-center relative mb-2">
      <div className="w-20 h-10"
           style={{
             background: "linear-gradient(135deg, #4a4a4f 0%, #2a2a30 60%, #1a1a1c 100%)",
             clipPath: "polygon(20% 0, 100% 0, 80% 100%, 0% 100%)",
             borderTop: "1px solid rgba(255,255,255,.18)",
           }} />
      <div className="absolute bottom-1/3 left-1/2 -translate-x-1/2 w-16 h-px"
           style={{
             background: "var(--fdy-molten)",
             boxShadow: "0 0 12px var(--fdy-molten)",
           }} />
    </div>
  );
}


function TierCard({ t }) {
  const Icon = t.icon;
  return (
    <div className={`fdy-card ${t.hot ? "fdy-card-hot" : ""} relative flex flex-col gap-4`}
         data-testid={`mockup-pricing-${t.id}`}>
      {t.hot && (
        <span className="absolute -top-3 left-1/2 -translate-x-1/2
                         text-[10px] uppercase tracking-widest px-3 py-1 rounded-full"
              style={{
                background: "linear-gradient(135deg, var(--fdy-molten), var(--fdy-molten-2))",
                color: "#1a0a02", fontWeight: 700,
              }}>
          Most teams pick this
        </span>
      )}
      <TierVisual kind={t.visual} />
      <div className="flex items-center gap-2">
        <Icon className="w-4 h-4 text-orange-400" />
        <span className="fdy-eyebrow">{t.name}</span>
      </div>
      <h3 className="fdy-display text-4xl fdy-molten-text leading-none">
        {t.headline}
      </h3>
      <p className="text-sm fdy-mute leading-relaxed">{t.tagline}</p>

      <div>
        {t.monthly != null ? (
          <>
            <div className="flex items-baseline gap-2">
              <span className="fdy-display text-5xl">${t.monthly}</span>
              <span className="text-xs fdy-mute">/ month</span>
            </div>
            <p className="text-[11px] fdy-mute mt-1">
              or ${t.annual}/mo billed annually (~17% off)
            </p>
            {t.seatsLabel && (
              <p className="text-[11px] fdy-mute mt-1 italic">{t.seatsLabel}</p>
            )}
          </>
        ) : (
          <>
            <div className="fdy-display text-5xl">Custom</div>
            <p className="text-[11px] fdy-mute mt-1">
              Starting at ${t.startingAt.toLocaleString()}/mo
            </p>
          </>
        )}
      </div>

      <ul className="space-y-2 border-t border-white/5 pt-4 flex-1">
        {t.perks.map((p) => (
          <li key={p} className="flex gap-2 text-sm leading-snug">
            <Check className="w-4 h-4 mt-0.5 flex-shrink-0 text-orange-400" />
            <span>{p}</span>
          </li>
        ))}
      </ul>

      <div className="fdy-card !p-3 text-xs flex gap-2"
           style={{ background: "rgba(255, 90, 24, .06)", border: "1px solid rgba(255,90,24,.18)" }}>
        <Wrench className="w-3 h-3 text-orange-400 mt-0.5 flex-shrink-0" />
        <span className="leading-relaxed">
          <span className="fdy-eyebrow !text-[9px] block mb-0.5">
            ArcheForge Studios
          </span>
          {t.studioPerk}
        </span>
      </div>

      <button
        className={`fdy-btn ${t.hot ? "fdy-btn-primary" : "fdy-btn-ghost"} w-full justify-center`}
      >
        {t.cta} <ArrowRight className="w-4 h-4" />
      </button>
    </div>
  );
}


function StudioCard({ s }) {
  const Icon = s.icon;
  return (
    <div className={`fdy-card ${s.flagship ? "ring-1 ring-orange-500/30" : ""}`}>
      <div className="flex items-start justify-between mb-3">
        <div className="w-9 h-9 rounded-xl flex items-center justify-center"
             style={{
               background: "linear-gradient(135deg, rgba(255,255,255,.05), rgba(255,255,255,.01))",
               border: "1px solid rgba(255,255,255,.08)",
             }}>
          <Icon className="w-4 h-4 text-orange-400" />
        </div>
        {s.bestseller && (
          <span className="text-[9px] uppercase tracking-widest text-amber-300
                           bg-amber-400/10 px-2 py-1 rounded-full">
            Bestseller
          </span>
        )}
      </div>
      <h4 className="fdy-display-condensed text-lg mb-2 leading-tight">{s.name}</h4>
      <p className="text-xs fdy-mute leading-relaxed mb-3">{s.blurb}</p>
      <div className="border-t border-white/5 pt-3 flex items-end justify-between">
        <div>
          <div className="fdy-display text-2xl fdy-molten-text">
            ${s.retail.toLocaleString()}
            <span className="text-xs fdy-mute ml-1 normal-case tracking-normal"
                  style={{ fontFamily: "Inter, sans-serif" }}>{s.unit}</span>
          </div>
        </div>
        <div className="text-right">
          <div className="fdy-eyebrow !text-[9px]">Temper / Refine</div>
          <div className="text-xs text-orange-300">-15% / -30%</div>
        </div>
      </div>
    </div>
  );
}


export default function MockupPricingPage() {
  return (
    <div className="fdy-page">
      <EmberField />

      <header className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 py-6
                         flex items-center justify-between">
        <Wordmark />
        <nav className="hidden md:flex items-center gap-8 text-sm fdy-mute">
          <Link to="/mockup/home" className="hover:text-white">Home</Link>
          <Link to="/mockup/pricing" className="text-white">Pricing</Link>
          <Link to="/trust" className="hover:text-white">Trust</Link>
        </nav>
        <div className="flex items-center gap-2">
          <Link to="/auth" className="fdy-btn fdy-btn-ghost text-xs">Sign in</Link>
          <Link to="/auth?mode=register" className="fdy-btn fdy-btn-primary text-xs">
            Get started <ArrowRight className="w-3.5 h-3.5" />
          </Link>
        </div>
      </header>

      <section className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 pt-10 pb-12 text-center">
        <div className="fdy-eyebrow inline-flex items-center gap-2 mb-4">
          <Flame className="w-3 h-3 text-orange-400" /> Three temperatures
        </div>
        <h1 className="fdy-display text-5xl sm:text-6xl lg:text-7xl">
          Start with a <span className="fdy-molten-text">spark</span>.<br />
          Scale through <span className="fdy-molten-text">temper</span>.<br />
          Finish in <span className="fdy-molten-text">refine</span>.
        </h1>
        <p className="text-base fdy-mute mt-5 max-w-xl mx-auto leading-relaxed">
          A working crew of AI agents at every plan.
          When Foundry tells you what to do, <span className="text-white">ArcheForge Studios</span> ships it for you.
        </p>
      </section>

      {/* Tiers */}
      <section className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 pb-24">
        <div className="grid md:grid-cols-3 gap-6">
          {TIERS.map((t) => <TierCard key={t.id} t={t} />)}
        </div>
        <p className="text-[11px] fdy-mute text-center mt-6 italic">
          All plans include Bitcoin-anchored Proof of Integrity, brute-force lockout,
          CSP enforcement, encrypted credential vault. We never sell or train on your
          data — see <Link to="/trust" className="underline">Trust &amp; Security</Link>.
        </p>
      </section>

      <div className="max-w-7xl mx-auto px-6 md:px-10">
        <div className="fdy-rule" />
      </div>

      {/* ArcheForge Studios menu */}
      <section className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 py-24">
        <div className="text-center mb-14">
          <div className="fdy-eyebrow inline-flex items-center gap-2 mb-3">
            <Wrench className="w-3 h-3" /> Done for you
          </div>
          <h2 className="fdy-display text-5xl sm:text-6xl">
            We don't just sell software.<br />
            We'll <span className="fdy-molten-text">ship the work</span> for you.
          </h2>
          <p className="text-base fdy-mute mt-5 max-w-xl mx-auto leading-relaxed">
            ArcheForge Studios is our agency arm. Productized,
            fixed-price engagements for the things Foundry recommends
            but you don't have time to execute.
          </p>
        </div>

        <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {STUDIO.map((s) => <StudioCard key={s.name} s={s} />)}
        </div>

        <div className="fdy-card fdy-card-hot mt-8 p-6 flex flex-col sm:flex-row
                        items-start sm:items-center justify-between gap-4">
          <div>
            <h3 className="fdy-display-condensed text-2xl">Need something not on the menu?</h3>
            <p className="text-sm fdy-mute mt-1">
              Custom builds, fractional CMO/CTO, anything custom — all available.
            </p>
          </div>
          <a href="mailto:hello@foundryagents.ai" className="fdy-btn fdy-btn-primary">
            Tell us what you need <ArrowRight className="w-4 h-4" />
          </a>
        </div>
      </section>

      <footer className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 py-10 border-t border-white/5
                         flex items-center justify-between text-xs fdy-mute">
        <span>© {new Date().getFullYear()} Foundry · by Foundry</span>
        <span className="inline-flex items-center gap-2">
          <ShieldCheck className="w-3 h-3 text-orange-400" />
          foundryagents.ai
        </span>
      </footer>
    </div>
  );
}
