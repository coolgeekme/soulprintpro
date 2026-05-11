import React from "react";
import { Link } from "react-router-dom";
import {
  ArrowRight, Check, Wrench, ShieldCheck, Hexagon,
  Megaphone, Globe, Smartphone, PenTool, Search, Mail, Bot, Compass,
} from "lucide-react";
import "../styles/foundry-chrome.css";

/**
 * MockupPricingV2Page — `/mockup/pricing-v2`.
 *
 * Same Spark/Temper/Refine + ArcheForge story as the v1 pricing mockup,
 * but rendered in the chrome-sigil aesthetic. Tier cards pull each sigil
 * out of the v2 brand sheet via background-position so the platform's
 * three "states" stay visually fused to the source artwork.
 */

const SIGIL_IMG = "https://customer-assets.emergentagent.com/job_emergent-wing/artifacts/pn78udxc_Foundry%20v2.png";

const TIERS = [
  {
    id: "spark",
    name: "Spark",
    headline: "Ignite ideas",
    tagline: "For solo founders shipping alone",
    monthly: 80, annual: 66,
    glowClass: "chr-tier-glow-spark",
    badgeClass: "chr-text-copper",
    sigilOffset: "0%",
    cta: "Start with Spark",
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
  },
  {
    id: "temper",
    name: "Temper",
    headline: "Strengthen with purpose",
    tagline: "For teams scaling without scaling headcount",
    monthly: 300, annual: 250,
    hot: true,
    glowClass: "chr-tier-glow-temper",
    badgeClass: "chr-text-steel",
    sigilOffset: "50%",
    cta: "Choose Temper",
    seatsLabel: "5 seats included · $40/seat after (max 20)",
    perks: [
      "5 seats · 3 orgs · unlimited teams",
      "Custom agent creation",
      "5,000 messages / month (pooled)",
      "75 missions · 200 audits / month",
      "Crucible: 600 min + 3 saved profiles",
      "25 GB documents · unlimited files",
      "Bitcoin-anchored Proof of Integrity",
      "White-label audit PDFs · 24h SLA",
    ],
    studioPerk: "15% off all ArcheForge services + priority queue",
  },
  {
    id: "refine",
    name: "Refine",
    headline: "Precision that leaves a mark",
    tagline: "For agencies + enterprise running ON Foundry",
    monthly: null, startingAt: 1500,
    glowClass: "chr-tier-glow-refine",
    badgeClass: "chr-text",
    sigilOffset: "100%",
    cta: "Talk to us",
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
  },
];

const STUDIO = [
  { name: "Paid Ads — Google + Meta", retail: 3750, unit: "/mo",
    icon: Megaphone, flagship: true,
    blurb: "End-to-end campaign management. AI strategy from Foundry, human hands on keys." },
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


function Wordmark() {
  return (
    <Link to="/mockup/home-v2" className="block leading-none">
      <div className="chr-wordmark">FOUNDRY</div>
      <div className="chr-sub-wordmark">
        POWERED BY <span className="accent">SOULPRINT</span> ENGINE
      </div>
    </Link>
  );
}


function TierSigil({ offset = "50%" }) {
  return (
    <div className="chr-sigil-mount">
      <div
        className="w-32 h-32"
        style={{
          backgroundImage: `url('${SIGIL_IMG}')`,
          backgroundRepeat: "no-repeat",
          backgroundPosition: `${offset} 35%`,
          backgroundSize: "300% auto",
          filter: "drop-shadow(0 0 12px rgba(95,168,214,.25))",
        }}
      />
    </div>
  );
}


function TierCard({ t }) {
  return (
    <div
      className={`chr-card ${t.hot ? "chr-card-steel" : ""} ${t.glowClass}
                  relative flex flex-col gap-4`}
      data-testid={`mockup-v2-pricing-${t.id}`}
    >
      {t.hot && (
        <span className="absolute top-3 right-3 text-[9px] uppercase tracking-widest
                         px-2.5 py-1 chr-display-tight"
              style={{
                background: "linear-gradient(135deg, var(--chr-steel), var(--chr-steel-d))",
                color: "#001628",
                clipPath: "polygon(4px 0, 100% 0, calc(100% - 4px) 100%, 0 100%)",
              }}>
          Most picked
        </span>
      )}
      <TierSigil offset={t.sigilOffset} />
      <div className="text-center mb-1">
        <div className="chr-eyebrow mb-1.5">{t.name}</div>
        <h3 className={`chr-display-tight text-2xl ${t.badgeClass} mb-2 leading-tight`}>
          {t.headline}
        </h3>
        <p className="text-xs chr-mute font-medium px-2">{t.tagline}</p>
      </div>

      <div className="border-t border-white/5 pt-4 text-center">
        {t.monthly != null ? (
          <>
            <div className="flex items-baseline justify-center gap-1.5">
              <span className="chr-display text-4xl chr-text">${t.monthly}</span>
              <span className="chr-mute text-xs font-semibold">/ MONTH</span>
            </div>
            <p className="text-[10px] chr-mute mt-1 tracking-widest uppercase">
              ${t.annual}/mo annual · 17% off
            </p>
            {t.seatsLabel && (
              <p className="text-[10px] chr-mute mt-1 italic">{t.seatsLabel}</p>
            )}
          </>
        ) : (
          <>
            <div className="chr-display text-4xl chr-text">Custom</div>
            <p className="text-[10px] chr-mute mt-1 tracking-widest uppercase">
              Starting at ${t.startingAt.toLocaleString()}/MO
            </p>
          </>
        )}
      </div>

      <ul className="space-y-1.5 border-t border-white/5 pt-4 flex-1">
        {t.perks.map((p) => (
          <li key={p} className="flex gap-2 text-xs leading-snug font-medium">
            <Check className="w-3.5 h-3.5 mt-0.5 flex-shrink-0 text-blue-300" />
            <span>{p}</span>
          </li>
        ))}
      </ul>

      <div className="chr-card !p-3 text-[11px] flex gap-2"
           style={{
             background: "rgba(95, 168, 214, .05)",
             border: "1px solid rgba(95, 168, 214, .18)",
           }}>
        <Wrench className="w-3 h-3 text-blue-300 mt-0.5 flex-shrink-0" />
        <span className="leading-relaxed font-medium">
          <span className="chr-eyebrow !text-[9px] block mb-0.5">ArcheForge Studios</span>
          {t.studioPerk}
        </span>
      </div>

      <button className={`chr-btn ${t.hot ? "chr-btn-primary" : "chr-btn-ghost"} w-full justify-center`}>
        {t.cta} <ArrowRight className="w-4 h-4" />
      </button>
    </div>
  );
}


function StudioCard({ s }) {
  const Icon = s.icon;
  return (
    <div className={`chr-card ${s.flagship ? "chr-card-steel" : ""}`}>
      <div className="flex items-start justify-between mb-3">
        <div className="w-9 h-9 flex items-center justify-center"
             style={{
               background: "linear-gradient(135deg, rgba(255,255,255,.05), rgba(255,255,255,.01))",
               border: "1px solid rgba(255,255,255,.08)",
               clipPath: "polygon(4px 0, 100% 0, calc(100% - 4px) 100%, 0 100%)",
             }}>
          <Icon className="w-4 h-4 text-blue-300" />
        </div>
        {s.bestseller && (
          <span className="text-[9px] uppercase tracking-widest text-orange-300
                           bg-orange-400/10 px-2 py-1 chr-display-tight"
                style={{
                  clipPath: "polygon(3px 0, 100% 0, calc(100% - 3px) 100%, 0 100%)",
                }}>
            Bestseller
          </span>
        )}
      </div>
      <h4 className="chr-display-tight text-base chr-text mb-2 leading-tight">{s.name}</h4>
      <p className="text-xs chr-mute leading-relaxed mb-3 font-medium">{s.blurb}</p>
      <div className="border-t border-white/5 pt-3 flex items-end justify-between">
        <div className="chr-display text-2xl chr-text-steel">
          ${s.retail.toLocaleString()}
          <span className="text-[10px] chr-mute ml-1 font-semibold tracking-widest uppercase">
            {s.unit}
          </span>
        </div>
        <div className="text-right">
          <div className="chr-eyebrow !text-[9px]">Temper / Refine</div>
          <div className="text-[11px] text-blue-300 font-semibold">−15% / −30%</div>
        </div>
      </div>
    </div>
  );
}


export default function MockupPricingV2Page() {
  return (
    <div className="chr-page">
      <header className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 py-6
                         flex items-center justify-between">
        <Wordmark />
        <nav className="hidden md:flex items-center gap-8 text-sm chr-mute font-medium">
          <Link to="/mockup/home-v2" className="hover:text-white">Home</Link>
          <Link to="/mockup/pricing-v2" className="text-white">Pricing</Link>
          <Link to="/mockup/pricing" className="hover:text-white">v1 mockup</Link>
        </nav>
        <div className="flex items-center gap-2">
          <Link to="/auth" className="chr-btn chr-btn-ghost">Sign in</Link>
          <Link to="/auth?mode=register" className="chr-btn chr-btn-primary">
            Get started <ArrowRight className="w-4 h-4" />
          </Link>
        </div>
      </header>

      <section className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 pt-12 pb-12 text-center">
        <div className="chr-eyebrow inline-flex items-center gap-2 mb-4">
          <Hexagon className="w-3 h-3 text-blue-300" /> Three sigils. One forge.
        </div>
        <h1 className="chr-display text-5xl sm:text-6xl lg:text-7xl leading-[1.1]">
          <span className="chr-text">Start with a </span>
          <span className="chr-text-copper">spark</span>.<br />
          <span className="chr-text">Scale through </span>
          <span className="chr-text-steel">temper</span>.<br />
          <span className="chr-text">Finish in </span>
          <span className="chr-text">refine</span>.
        </h1>
        <p className="chr-mute mt-5 max-w-xl mx-auto leading-relaxed font-medium">
          A working crew of AI agents at every plan.
          When Foundry tells you what to do, <span className="text-white">ArcheForge Studios </span>
          ships it for you.
        </p>
      </section>

      <section className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 pb-24">
        <div className="grid md:grid-cols-3 gap-6">
          {TIERS.map((t) => <TierCard key={t.id} t={t} />)}
        </div>
        <p className="text-[11px] chr-mute text-center mt-6 italic font-medium">
          All plans include Bitcoin-anchored Proof of Integrity, brute-force lockout,
          CSP enforcement, encrypted credential vault.
        </p>
      </section>

      <div className="max-w-7xl mx-auto px-6 md:px-10">
        <div className="chr-rule" />
      </div>

      <section className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 py-24">
        <div className="text-center mb-14">
          <div className="chr-eyebrow inline-flex items-center gap-2 mb-3">
            <Wrench className="w-3 h-3" /> Done for you
          </div>
          <h2 className="chr-display text-4xl sm:text-5xl leading-tight">
            <span className="chr-text">We don't just sell software.</span><br />
            <span className="chr-text">We'll </span>
            <span className="chr-text-copper">ship the work</span>
            <span className="chr-text"> for you.</span>
          </h2>
          <p className="chr-mute mt-5 max-w-xl mx-auto font-medium">
            ArcheForge Studios is our agency arm. Productized, fixed-price
            engagements for the things Foundry recommends but you don't have
            time to execute.
          </p>
        </div>

        <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {STUDIO.map((s) => <StudioCard key={s.name} s={s} />)}
        </div>

        <div className="chr-card chr-card-copper mt-8 p-6 flex flex-col sm:flex-row
                        items-start sm:items-center justify-between gap-4">
          <div>
            <h3 className="chr-display-tight text-xl chr-text">
              Need something not on the menu?
            </h3>
            <p className="text-xs chr-mute mt-1 font-medium">
              Custom builds, fractional CMO/CTO, anything custom — all available.
            </p>
          </div>
          <a href="mailto:hello@foundryagents.ai" className="chr-btn chr-btn-copper">
            Tell us what you need <ArrowRight className="w-4 h-4" />
          </a>
        </div>
      </section>

      <footer className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 py-10
                         border-t border-white/5 flex items-center justify-between
                         text-xs chr-mute font-medium">
        <span className="tracking-widest uppercase">
          © {new Date().getFullYear()} Foundry · by Foundry
        </span>
        <span className="inline-flex items-center gap-2 tracking-widest uppercase">
          <ShieldCheck className="w-3 h-3 text-blue-300" />
          foundryagents.ai
        </span>
      </footer>
    </div>
  );
}
