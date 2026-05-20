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
 * Axis 1: Platform subscription (Spark / Temper / Refine) — clean SaaS.
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
    monthly: 80,
    annual: 66,
    cta: "Start with Spark",
    href: "/auth?mode=register",
    perks: [
      "1 user · 1 organization · 3 teams",
      "Full 150+ agent catalogue",
      "750 agent messages / month",
      "5 autonomous mission runs / month",
      "15 Signal Audits / month",
      "60 browser automation min / mo (Crucible + Missions)",
      "10 GB documents · 250 files",
      "Community email support",
      "+ Add-ons: extra orgs ($20/mo) · seats ($30/mo) · usage packs",
    ],
    studioPerk: "ArcheForge Studios services available at retail",
    accent: "from-amber-400/30 to-orange-400/10",
  },
  {
    id: "tempered",
    name: "Temper",
    tagline: "For teams scaling without scaling headcount",
    icon: Flame,
    monthly: 300,
    annual: 250,
    cta: "Choose Temper",
    href: "/auth?mode=register",
    highlight: true,
    seatsLabel: "5 seats included · $40/seat after (max 20)",
    perks: [
      "5 seats · 3 orgs · unlimited teams",
      "Custom agent creation",
      "5,000 messages / month (pooled)",
      "75 mission runs / month",
      "200 Signal Audits / month",
      "600 browser automation min / mo + 3 saved logins",
      "25 GB documents · unlimited files",
      "Proof of Integrity (Bitcoin-anchored)",
      "White-label Signal Audit PDFs",
      "Priority support",
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
    href: "mailto:hello@foundryagents.ai?subject=Refine%20tier%20enquiry",
    perks: [
      "Unlimited seats · unlimited orgs · multi-workspace",
      "Custom agent personalities co-designed with our team",
      "Fair-use everything (100k msgs · 1k missions · 2k audits / mo)",
      "Unlimited browser automation min + unlimited saved logins",
      "Bring-your-own LLM key option",
      "White-label everything: domain, logo, client portal",
      "SSO/SAML · SCIM · DPA · custom data retention (scoped into your build)",
      "Dedicated infra option — your own cluster (scoped into your build)",
      "SOC 2 Type II attestation on your dedicated build · HIPAA-ready BAA on request",
      "Dedicated CSM + QBR",
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
    note: "Management fee only — ad spend billed by Google/Meta directly to your account.",
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
    note: "Management fee only — ad spend billed by Google directly to your account.",
  },
  {
    id: "ads-meta",
    name: "Paid Ads — Meta only",
    blurb: "Facebook + Instagram. Creative refresh every 14 days.",
    scope: "Setup + 30 days managed",
    retail: 2500, unit: "/mo",
    icon: Megaphone,
    note: "Management fee only — ad spend billed by Meta directly to your account.",
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

const COMPARE_MATRIX = [
  {
    group: "Plan basics",
    rows: [
      { label: "Monthly price",         spark: "$80",            tempered: "$300",          refine: "Custom (from $1,500)" },
      { label: "Annual price (~17% off)", spark: "$66/mo",       tempered: "$250/mo",       refine: "Custom" },
      { label: "Seats included",        spark: "1",              tempered: "5 (then $40/seat, max 20)", refine: "Unlimited" },
      { label: "Organizations",         spark: "1",              tempered: "3",             refine: "Unlimited · multi-workspace" },
      { label: "Teams per org",         spark: "3",              tempered: "Unlimited",     refine: "Unlimited" },
    ],
  },
  {
    group: "Agents & intelligence",
    rows: [
      { label: "Full 150+ agent catalogue",       spark: true, tempered: true, refine: true },
      { label: "Smart auto-routing",              spark: true, tempered: true, refine: true },
      { label: "Dynamic model intelligence",      spark: true, tempered: true, refine: true },
      { label: "Real-time web search + citations", spark: true, tempered: true, refine: true },
      { label: "Custom agent creation",           spark: false, tempered: true, refine: "Co-designed with our team" },
      { label: "Bring-your-own LLM key",          spark: false, tempered: false, refine: true },
    ],
  },
  {
    group: "Monthly usage",
    rows: [
      { label: "Agent messages",          spark: "750",        tempered: "5,000 (pooled)",      refine: "100k fair-use" },
      { label: "Autonomous mission runs", spark: "5",          tempered: "75",                  refine: "1,000 fair-use" },
      { label: "Signal Audits",            spark: "15",         tempered: "200",                 refine: "2,000 fair-use" },
      { label: "Browser automation min (Crucible + Missions)", spark: "60 min",  tempered: "600 min", refine: "Unlimited" },
      { label: "Saved logins (cookies)",  spark: "0",          tempered: "3",                   refine: "Unlimited" },
    ],
  },
  {
    group: "Storage",
    rows: [
      { label: "Document storage",   spark: "10 GB", tempered: "25 GB",       refine: "Unlimited" },
      { label: "Files",              spark: "250",   tempered: "Unlimited",   refine: "Unlimited" },
    ],
  },
  {
    group: "Trust, brand & enterprise",
    rows: [
      { label: "Proof of Integrity (Bitcoin-anchored)", spark: false, tempered: true, refine: true },
      { label: "White-label Signal Audit PDFs",        spark: false, tempered: true, refine: true },
      { label: "White-label domain / logo / portal",    spark: false, tempered: false, refine: true },
      { label: "SSO / SAML · SCIM",                     spark: false, tempered: false, refine: "custom" },
      { label: "DPA + custom data retention",           spark: false, tempered: false, refine: "custom" },
      { label: "Dedicated infra option",                spark: false, tempered: false, refine: "custom" },
      { label: "SOC 2 Type II attestation",             spark: false, tempered: false, refine: "Included on dedicated build" },
      { label: "HIPAA-ready (BAA on request)",          spark: false, tempered: false, refine: "Included on dedicated build" },
      { label: "Customer-managed encryption keys (BYOK)", spark: false, tempered: false, refine: "custom" },
      { label: "Audit log export (SIEM-ready)",          spark: false, tempered: false, refine: true },
    ],
  },
  {
    group: "Support",
    rows: [
      { label: "Dedicated CSM",      spark: false,       tempered: false,             refine: true },
      { label: "Quarterly business review", spark: false, tempered: false,            refine: true },
    ],
  },
  {
    group: "ArcheForge Studios",
    rows: [
      { label: "Pricing on managed services", spark: "Retail",
        tempered: "15% off · priority queue", refine: "$2k/mo credits · 30% off after" },
    ],
  },
];

const ADD_ONS = [
  {
    id: "org",
    name: "Extra organization",
    blurb: "Spin up another org with its own teams, agents, and brand soul. Bills monthly per extra org.",
    price: 20, unit: "/org / mo",
    cadence: "recurring",
    icon: Compass,
  },
  {
    id: "seat",
    name: "Extra seat (Spark)",
    blurb: "Invite another teammate to your Spark org. Each seat counts toward the same usage pool.",
    price: 30, unit: "/seat / mo",
    cadence: "recurring",
    icon: Bot,
    note: "Temper already includes 5 seats — upgrade once you reach 4+.",
  },
  {
    id: "messages",
    name: "Message pack",
    blurb: "Top up 500 extra agent messages whenever you spike. No expiry until consumed.",
    price: 15, unit: "/ 500 msgs",
    cadence: "one-time",
    icon: Zap,
  },
  {
    id: "browser",
    name: "Browser automation pack",
    blurb: "Add 200 extra minutes for Crucible + Mission browser work. Carry over until used.",
    price: 20, unit: "/ 200 min",
    cadence: "one-time",
    icon: Search,
  },
  {
    id: "audits",
    name: "Signal Audit pack",
    blurb: "10 extra Signal Audits for client prospecting. Reuses your saved templates.",
    price: 10, unit: "/ 10 audits",
    cadence: "one-time",
    icon: PenTool,
  },
];

function AddOnCard({ a }) {
  const Icon = a.icon;
  const isRecurring = a.cadence === "recurring";
  return (
    <div
      className="sp-glass rounded-2xl p-5 flex flex-col gap-3 border border-white/10 hover:border-white/25 transition-colors"
      data-testid={`addon-card-${a.id}`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="w-9 h-9 rounded-xl sp-glass-strong flex items-center justify-center shrink-0">
          <Icon className="w-4 h-4" />
        </div>
        <span
          className={`text-[9px] uppercase tracking-widest px-2 py-1 rounded-full ${
            isRecurring
              ? "bg-orange-400/15 text-orange-300"
              : "bg-amber-300/15 text-amber-300"
          }`}
        >
          {isRecurring ? "Recurring" : "One-time"}
        </span>
      </div>
      <h4 className="sp-display text-base leading-snug">{a.name}</h4>
      <p className="text-xs sp-subtle leading-relaxed flex-1">{a.blurb}</p>
      {a.note && (
        <p className="text-[10px] italic text-amber-300/80 leading-snug -mt-1">
          {a.note}
        </p>
      )}
      <div className="border-t border-white/10 pt-3 flex items-baseline gap-1">
        <span className="text-2xl font-medium tracking-tight">${a.price}</span>
        <span className="text-xs sp-subtle">{a.unit}</span>
      </div>
    </div>
  );
}

function AddOnsSection() {
  return (
    <section
      className="relative z-10 max-w-6xl mx-auto px-6 md:px-10 py-20 border-t border-white/10"
      data-testid="pricing-addons-section"
      id="add-ons"
    >
      <div className="text-center mb-10">
        <span className="inline-flex items-center gap-2 text-[10px] uppercase tracking-widest sp-glass rounded-full px-3 py-1 mb-4">
          <Zap className="w-3 h-3" /> Add as you grow
        </span>
        <h2 className="sp-display text-3xl sm:text-4xl lg:text-5xl max-w-3xl mx-auto leading-tight">
          Need more headroom? <span className="text-orange-300">Add it on.</span>
        </h2>
        <p className="text-base sp-subtle mt-4 max-w-2xl mx-auto leading-relaxed">
          Built for small agencies managing multiple clients on Spark. Stack extra
          orgs and seats month-to-month, top up usage packs when a project spikes.
          No upgrade required.
        </p>
      </div>

      <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {ADD_ONS.map((a) => <AddOnCard key={a.id} a={a} />)}
      </div>

      <p className="text-[11px] sp-subtle text-center mt-8 max-w-2xl mx-auto italic">
        Recurring add-ons bill on your monthly Spark cycle. One-time packs are
        consumed against your current usage pool until used up — no expiry.
        Cancel any recurring add-on anytime.
      </p>
    </section>
  );
}

const FAQ = [
  {
    q: "Do the paid-ads packages include ad spend?",
    a: "No — the monthly fees ($2,500 Google · $2,500 Meta · $3,750 combined) are management fees only. Ad spend is billed by Google or Meta directly to your own ad account, which we set up alongside (or use the one you already have). This is the industry-standard model and keeps you in full control of your spend, payment method, and billing history. We'll recommend a starting daily/monthly budget based on your goals before launch, but you decide the number and can change it any time.",
  },
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
    q: "Can I add organizations or seats to Spark without upgrading?",
    a: "Yes. Spark add-ons are designed for small agencies on Spark managing multiple clients: extra organization ($20/mo each), extra seat ($30/mo each). You can also top up one-time usage packs — 500 messages ($15), 200 browser automation minutes ($20), or 10 Signal Audits ($10) — when a project spikes. Add-ons stack so a Spark + 2 orgs + 1 seat is still cheaper than jumping to Temper if you don't need the rest of Temper's features.",
  },
  {
    q: "Is there a free trial?",
    a: "Yes — every public Signal Audit you generate at /get-audit includes a 24-hour access code to the full platform. No card required.",
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
      {s.note && (
        <p className="text-[10px] italic text-amber-300/80 leading-snug -mt-1">
          {s.note}
        </p>
      )}
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
            Temper / Refine
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

function CellValue({ value }) {
  if (value === true) {
    return (
      <span className="inline-flex items-center justify-center w-6 h-6 rounded-full bg-emerald-500/15 text-emerald-300">
        <Check className="w-3.5 h-3.5" strokeWidth={3} />
      </span>
    );
  }
  if (value === false) {
    return <span className="text-white/25 text-sm select-none">—</span>;
  }
  // Special-cased pill — used by Enterprise security / data items that
  // are delivered as part of the custom-build engagement rather than
  // shipped as off-the-shelf SaaS features. Rendering them as "Custom
  // build" instead of a ✓ keeps the matrix honest while still showing
  // the line items prospects expect to see when evaluating Enterprise.
  if (value === "custom") {
    return (
      <span
        className="inline-flex items-center gap-1 text-[10px] uppercase tracking-widest px-2 py-0.5 rounded-full border bg-violet-500/15 text-violet-200 border-violet-500/30"
        title="Delivered as part of your Refine custom-build engagement"
      >
        Custom build
      </span>
    );
  }
  return <span className="text-sm">{value}</span>;
}

function ComparisonMatrix() {
  return (
    <section
      className="relative z-10 max-w-6xl mx-auto px-6 md:px-10 py-20 border-t border-white/10"
      data-testid="pricing-matrix-section"
      id="compare"
    >
      <div className="text-center mb-10">
        <span className="inline-flex items-center gap-2 text-[10px] uppercase tracking-widest sp-glass rounded-full px-3 py-1 mb-4">
          <Zap className="w-3 h-3" /> Compare every detail
        </span>
        <h2 className="sp-display text-3xl sm:text-4xl lg:text-5xl max-w-3xl mx-auto leading-tight">
          The same crew. <span className="text-orange-300">Different gear.</span>
        </h2>
        <p className="text-base sp-subtle mt-4 max-w-2xl mx-auto leading-relaxed">
          Every plan unlocks all 150+ agents. What changes is how much they
          can run, who else is on the team, and how customized the engine gets.
        </p>
      </div>

      <div className="sp-glass-strong rounded-2xl overflow-hidden border border-white/10">
        <div className="overflow-x-auto">
          <table className="w-full text-sm" data-testid="pricing-matrix-table">
            <thead>
              <tr className="bg-white/[0.04] border-b border-white/10">
                <th className="text-left px-5 py-4 font-medium text-xs uppercase tracking-widest sp-subtle min-w-[220px]">
                  Feature
                </th>
                <th className="text-left px-5 py-4 min-w-[160px]">
                  <div className="flex items-center gap-2">
                    <Sparkles className="w-4 h-4 text-amber-300" />
                    <span className="sp-display text-lg">Spark</span>
                  </div>
                  <div className="text-[11px] sp-subtle font-normal mt-0.5">$80/mo</div>
                </th>
                <th className="text-left px-5 py-4 min-w-[180px] bg-orange-500/[0.06]">
                  <div className="flex items-center gap-2">
                    <Flame className="w-4 h-4 text-orange-300" />
                    <span className="sp-display text-lg">Temper</span>
                    <span className="text-[9px] uppercase tracking-widest bg-orange-400/15 text-orange-300 px-1.5 py-0.5 rounded">
                      Most picked
                    </span>
                  </div>
                  <div className="text-[11px] sp-subtle font-normal mt-0.5">$300/mo</div>
                </th>
                <th className="text-left px-5 py-4 min-w-[200px]">
                  <div className="flex items-center gap-2">
                    <Gem className="w-4 h-4 text-violet-300" />
                    <span className="sp-display text-lg">Refine</span>
                  </div>
                  <div className="text-[11px] sp-subtle font-normal mt-0.5">From $1,500/mo</div>
                </th>
              </tr>
            </thead>
            <tbody>
              {COMPARE_MATRIX.map((section) => (
                <React.Fragment key={section.group}>
                  <tr className="bg-white/[0.02]">
                    <td
                      colSpan={4}
                      className="px-5 py-2.5 text-[10px] uppercase tracking-widest sp-subtle font-medium border-t border-white/5"
                    >
                      {section.group}
                    </td>
                  </tr>
                  {section.rows.map((row) => (
                    <tr
                      key={row.label}
                      className="border-t border-white/5 hover:bg-white/[0.02] transition-colors"
                      data-testid={`matrix-row-${row.label.replace(/\W+/g, "-").toLowerCase()}`}
                    >
                      <td className="px-5 py-3 sp-subtle">{row.label}</td>
                      <td className="px-5 py-3"><CellValue value={row.spark} /></td>
                      <td className="px-5 py-3 bg-orange-500/[0.04]"><CellValue value={row.tempered} /></td>
                      <td className="px-5 py-3"><CellValue value={row.refine} /></td>
                    </tr>
                  ))}
                </React.Fragment>
              ))}
            </tbody>
          </table>
        </div>
        <p
          className="mt-4 text-xs sp-subtle leading-relaxed"
          data-testid="matrix-custom-build-note"
        >
          <span className="inline-block w-2 h-2 rounded-full bg-violet-400/70 mr-2 align-middle" />
          Items marked <span className="text-violet-200">Custom build</span> —
          SSO/SAML, SCIM, DPA + custom data retention, and dedicated
          infrastructure — are scoped, built, and delivered as part of
          your Refine custom engagement rather than shipped as
          off-the-shelf toggles. Timeline and exact scope are set in
          your kickoff.
        </p>
      </div>

      <div className="flex flex-wrap justify-center gap-3 mt-8">
        <Link
          to="/auth?mode=register"
          className="sp-btn sp-btn-primary text-sm"
          data-testid="matrix-cta-spark"
        >
          Start with Spark <ArrowRight className="w-4 h-4" />
        </Link>
        <a
          href="mailto:hello@foundryagents.ai?subject=Refine%20tier%20enquiry"
          className="sp-btn sp-btn-ghost text-sm"
          data-testid="matrix-cta-refine"
        >
          Talk to us about Refine
        </a>
      </div>
    </section>
  );
}

const LADDER = [
  { step: 1, icon: Search, name: "Signal Audit",
    blurb: "Drop a URL on the homepage. Get a real audit in 60 seconds — Core Web Vitals, tracking inventory, email security, and prioritised fixes.",
    cta: "Try a Signal Audit", href: "/get-audit" },
  { step: 2, icon: Sparkles, name: "Spark",
    blurb: "$80/mo. Foundry tells you what to do — you ship it yourself.",
    cta: null },
  { step: 3, icon: Flame, name: "Temper",
    blurb: "$300/mo. Your team is using it. Some things they hand to ArcheForge Studios.",
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
        <div className="text-center mt-3">
          <a
            href="#compare"
            className="text-xs sp-subtle hover:text-white transition-colors underline underline-offset-2"
            data-testid="pricing-jump-to-matrix"
          >
            Compare every feature side-by-side ↓
          </a>
        </div>
      </section>

      {/* Comparison matrix */}
      <ComparisonMatrix />

      {/* Add-ons */}
      <AddOnsSection />

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
            Monthly billing, cancel anytime. Annual saves ~17%. Refine ships
            with a dedicated CSM from day one.
          </p>
          <div className="flex gap-2">
            <Link to="/get-audit" className="sp-btn sp-btn-ghost"
                  data-testid="pricing-final-audit">
              Try a Signal Audit
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
