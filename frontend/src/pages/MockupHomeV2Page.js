import React from "react";
import { Link } from "react-router-dom";
import {
  ArrowRight, ShieldCheck, Network, Bot, Compass, Wrench, Cpu, Hexagon,
  Users, Radio, Sparkles, Zap,
} from "lucide-react";
import "../styles/foundry-chrome.css";

/**
 * MockupHomeV2Page — `/mockup/home-v2`.
 *
 * Cooler counterpart to MockupHomePage. Same "Foundry" brand, same
 * three-tier story (Spark / Temper / Refine), but rendered in the
 * crystalline steel-blue + copper sigil aesthetic of the v2 brand art.
 *
 * The v2 image itself is the hero — we crop it into three tier-emblem
 * panels via background-position so we don't need to ship three
 * separate sprites.
 */

const SIGIL_IMG = "/brand/foundry_agents.png";
const HERO_LOGO = "/brand/foundry_agents.png";
// Background-cleaned swirl mark used in the navbar. Sourced as
// transparent PNG so it sits cleanly on the dark page surface
// without the original artwork's navy bezel.
const NAV_LOGO = "/brand/foundry_swirl.png";

// Individual tier sigils — alpha-stripped from the v2 brand pack so
// each tier card gets its OWN crisp emblem instead of a slice of a
// 3-up sheet. Way cleaner than the old `background-position` crop
// approach (which softened the edges and clipped the outer fins).
const TIER_SIGILS = {
  spark:  "/brand/spark_sigil.png",
  temper: "/brand/temper_sigil.png",
  refine: "/brand/refine_sigil.png",
};

const TIERS = [
  {
    id: "spark",
    name: "Spark",
    headline: "Ignite ideas",
    blurb: "Solo founders. One thought becomes a working crew of 169 specialists.",
    price: "$80",
    cadence: "/mo",
    glowClass: "chr-tier-glow-spark",
    badgeClass: "chr-text-copper",
    sigilOffset: "0%",
  },
  {
    id: "temper",
    name: "Temper",
    headline: "Strengthen with purpose",
    blurb: "Growing teams. Forge work into shippable deliverables, daily.",
    price: "$300",
    cadence: "/mo",
    glowClass: "chr-tier-glow-temper",
    badgeClass: "chr-text-steel",
    hot: true,
    sigilOffset: "50%",
  },
  {
    id: "refine",
    name: "Refine",
    headline: "Precision that leaves a mark",
    blurb: "Agencies + enterprise. White-label, multi-workspace, dedicated CSM.",
    price: "Custom",
    cadence: "",
    glowClass: "chr-tier-glow-refine",
    badgeClass: "chr-text",
    sigilOffset: "100%",
  },
];


function Wordmark() {
  return (
    <Link to="/mockup/home-v2"
          className="flex items-center gap-3 leading-none"
          data-testid="mockup-v2-wordmark">
      <img
        src={NAV_LOGO}
        alt="Foundry"
        className="h-12 w-12 object-contain shrink-0"
        style={{ filter: "drop-shadow(0 0 10px rgba(216,118,49,.32))" }}
      />
      <div className="leading-tight">
        <div className="chr-display text-lg tracking-[0.18em] text-white/90">
          FOUNDRY
        </div>
        <div className="chr-sub-wordmark mt-0.5">
          POWERED BY <span className="accent">SOULPRINT</span> ENGINE
        </div>
      </div>
    </Link>
  );
}


/**
 * Tier sigil — renders one of the alpha-stripped tier emblems
 * (spark / temper / refine) at a consistent visual weight. Each
 * sigil is its own PNG with transparent background so the page
 * vignette + tier glow show through behind the metalwork.
 */
function TierSigil({ tier = "spark" }) {
  return (
    <div className="chr-sigil-mount">
      <img
        src={TIER_SIGILS[tier] || TIER_SIGILS.spark}
        alt={`Foundry ${tier} sigil`}
        className="w-40 h-40 object-contain mx-auto"
        loading="lazy"
        style={{
          filter: "drop-shadow(0 0 18px rgba(95,168,214,.25))",
        }}
      />
    </div>
  );
}


export default function MockupHomeV2Page() {
  return (
    <div className="chr-page">
      {/* Nav */}
      <header className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 py-6
                         flex items-center justify-between">
        <Wordmark />
        <nav className="hidden md:flex items-center gap-8 text-sm chr-mute font-medium">
          <a href="#tiers" className="hover:text-white transition-colors">Tiers</a>
          <a href="#why"   className="hover:text-white transition-colors">Why</a>
          <a href="#crew"  className="hover:text-white transition-colors">Crew</a>
          <a href="#how"   className="hover:text-white transition-colors">How it works</a>
          <Link to="/mockup/pricing-v2" className="hover:text-white">Pricing</Link>
          <Link to="/mockup/home" className="hover:text-white">v1 mockup</Link>
        </nav>
        <div className="flex items-center gap-2">
          <Link to="/auth" className="chr-btn chr-btn-ghost"
                data-testid="mockup-v2-nav-signin">Sign in</Link>
          <Link to="/auth?mode=register" className="chr-btn chr-btn-primary"
                data-testid="mockup-v2-nav-cta">
            Get started <ArrowRight className="w-4 h-4" />
          </Link>
        </div>
      </header>

      {/* Hero */}
      <section className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 pt-16 pb-20
                          grid lg:grid-cols-[1.05fr_1fr] gap-14 items-center">
        <div className="space-y-7">
          <div className="chr-eyebrow inline-flex items-center gap-2">
            <Hexagon className="w-3 h-3 text-blue-300" /> Forged for serious teams
          </div>
          <h1 className="chr-display text-5xl sm:text-6xl lg:text-7xl leading-[1.05]">
            <span className="chr-text">Built to</span>{" "}
            <span className="chr-text-steel">cut.</span><br />
            <span className="chr-text">Tempered to</span>{" "}
            <span className="chr-text-copper">last.</span><br />
            <span className="chr-text">Refined to</span>{" "}
            <span className="chr-text-steel">scale.</span>
          </h1>
          <p className="text-base max-w-md leading-relaxed text-stone-300 font-medium">
            169 specialist AI agents. Each one a precision instrument.
            When the work needs hands on the keys,
            <span className="text-white"> ArcheForge Studios </span>
            ships it for you.
          </p>
          <div className="flex flex-wrap gap-3 pt-2">
            <Link to="/get-audit" className="chr-btn chr-btn-primary"
                  data-testid="mockup-v2-hero-audit">
              Free audit in 60s <ArrowRight className="w-4 h-4" />
            </Link>
            <Link to="/mockup/pricing-v2" className="chr-btn chr-btn-ghost">
              See the tiers
            </Link>
          </div>
          <div className="flex items-center gap-6 pt-4 text-xs chr-mute font-medium tracking-widest uppercase">
            <span className="inline-flex items-center gap-1.5">
              <span className="w-1.5 h-1.5"
                    style={{ background: "var(--chr-steel)",
                             clipPath: "polygon(50% 0,100% 50%,50% 100%,0 50%)" }} />
              Bitcoin-anchored
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span className="w-1.5 h-1.5"
                    style={{ background: "var(--chr-copper)",
                             clipPath: "polygon(50% 0,100% 50%,50% 100%,0 50%)" }} />
              Invite-only
            </span>
          </div>
        </div>

        <div className="relative">
          <div className="absolute inset-0 -z-10"
               style={{
                 background:
                   "radial-gradient(circle at 50% 50%, rgba(95,168,214,.18), transparent 70%)",
                 filter: "blur(40px)",
               }} />
          {/* Hero artwork — the alpha-stripped Foundry marketing
              composition (foundryagents.ai). Centered on the dark
              vignette so the copper sigil + workshop frames glow
              through without the source PNG's hard black border. */}
          <div
            role="img"
            aria-label="Foundry — Forge your edge"
            className="w-full max-w-[560px] mx-auto aspect-square"
            style={{
              backgroundImage: `url('${HERO_LOGO}')`,
              backgroundRepeat: "no-repeat",
              backgroundPosition: "center center",
              backgroundSize: "contain",
              filter: "drop-shadow(0 30px 80px rgba(229,123,58,.18))",
            }}
          />
        </div>
      </section>

      <div className="max-w-7xl mx-auto px-6 md:px-10">
        <div className="chr-rule" />
      </div>

      {/* Tiers */}
      <section id="tiers" className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 py-24">
        <div className="text-center mb-16">
          <div className="chr-eyebrow mb-4">Three sigils. One forge.</div>
          <h2 className="chr-display text-5xl sm:text-6xl">
            <span className="chr-text-copper">Spark.</span>{" "}
            <span className="chr-text-steel">Temper.</span>{" "}
            <span className="chr-text">Refine.</span>
          </h2>
          <p className="chr-mute mt-4 max-w-xl mx-auto font-medium">
            Three forged states for three kinds of work. Each is a sigil —
            wear the one that fits.
          </p>
        </div>

        <div className="grid md:grid-cols-3 gap-6">
          {TIERS.map((t) => (
            <div
              key={t.id}
              className={`chr-card ${t.hot ? "chr-card-steel" : ""} ${t.glowClass}
                          flex flex-col gap-4 transition-transform hover:-translate-y-1`}
              data-testid={`mockup-v2-tier-${t.id}`}
            >
              <TierSigil tier={t.id} />
              <div className="text-center">
                <div className="chr-eyebrow mb-1.5">{t.name}</div>
                <h3 className={`chr-display-tight text-2xl ${t.badgeClass} mb-3`}>
                  {t.headline}
                </h3>
                <p className="text-sm chr-mute leading-relaxed font-medium px-2">
                  {t.blurb}
                </p>
              </div>
              <div className="border-t border-white/5 pt-4 flex items-baseline justify-center gap-1">
                <span className="chr-display text-3xl chr-text">{t.price}</span>
                <span className="chr-mute text-sm font-semibold">{t.cadence}</span>
              </div>
              <Link to="/mockup/pricing-v2"
                    className={`chr-btn ${t.hot ? "chr-btn-primary" : "chr-btn-ghost"}
                                w-full justify-center`}>
                {t.name} details <ArrowRight className="w-3.5 h-3.5" />
              </Link>
            </div>
          ))}
        </div>
      </section>

      {/* Spark showcase section was removed (Jun 2026) — the big banner
          was too dominant against the tighter narrative above (Hero →
          Tiers → How → Studio → Trust). The per-tier sigil cards directly
          above already do the "here's what a Spark workspace looks like"
          job at a fraction of the visual cost. If we ever want
          day-one-output proof back, prefer a small inline carousel of
          3-4 thumbnails instead of one full-bleed image. */}

      {/* Why Foundry — 6 product-explainer cards. Ported from the v1
          LandingPage and re-tinted to the chrome aesthetic. Sits between
          Tiers (the offer) and How-it-works (the process) so the visitor
          understands the *product* before the *steps*. */}
      <section id="why" className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 py-24">
        <div className="text-center mb-12">
          <div className="chr-eyebrow mb-3">Why Foundry</div>
          <h2 className="chr-display text-4xl sm:text-5xl max-w-3xl mx-auto leading-tight">
            <span className="chr-text">Not another chatbot. </span>
            <span className="chr-text-copper">A crew </span>
            <span className="chr-text">that understands your company.</span>
          </h2>
        </div>
        <div className="grid md:grid-cols-3 gap-4">
          {[
            { i: Network, title: "One Soul, every agent",
              body: "Define your mission, tone, audience, and values once — every agent on the team is briefed automatically before they say a word." },
            { i: Users, title: "Group + direct conversations",
              body: "Work 1:1 with an agent or pull multiple humans + agents into one thread. @mention to steer, or let the room respond." },
            { i: Radio, title: "169 specialists. 14 divisions.",
              body: "Engineers, strategists, copywriters, researchers, ops, finance, legal, product, XR, game dev — the full agency, tuned to you." },
            { i: ShieldCheck, title: "Org-first architecture",
              body: "Invite your company by link. Spin up teams per product, brand, or squad. Keep contexts clean and conversations private." },
            { i: Sparkles, title: "Tuned, not templated",
              body: "Your brief is woven into every system prompt, so output feels like it came from someone who actually works at your company." },
            { i: Zap, title: "Ship in minutes",
              body: "No configuration theatre. Create an org, draft your Soul, and you're in a working thread in under three minutes." },
          ].map((f) => {
            const Icon = f.i;
            return (
              <div key={f.title} className="chr-card">
                <Icon className="w-5 h-5 text-blue-300 mb-3" />
                <h4 className="chr-display-tight text-xl chr-text mb-2 leading-tight">
                  {f.title}
                </h4>
                <p className="text-sm chr-mute leading-relaxed font-medium">{f.body}</p>
              </div>
            );
          })}
        </div>
      </section>

      <div className="max-w-7xl mx-auto px-6 md:px-10">
        <div className="chr-rule" />
      </div>

      {/* The Crew — 14-division agent catalogue. Concrete proof of
          depth that closes deals: "169 agents across 14 divisions"
          lands harder than just "150+ specialists". The colored accent
          bar on each card uses a desaturated chrome-friendly palette
          (not the v1 LandingPage's bright neons) so the grid stays
          on-brand with the rest of the chrome aesthetic. */}
      <section id="crew" className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 py-24">
        <div className="text-center mb-12">
          <div className="chr-eyebrow mb-3">The crew</div>
          <h2 className="chr-display text-4xl sm:text-5xl max-w-3xl mx-auto leading-tight">
            <span className="chr-text-steel">169 specialists.</span>{" "}
            <span className="chr-text">14 divisions.</span><br />
            <span className="chr-text-copper italic">One voice — yours.</span>
          </h2>
          <p className="chr-mute mt-4 max-w-2xl mx-auto font-medium">
            Frontend wizards, brand guardians, deal coaches, privacy architects,
            narrative designers — a full agency, tuned by your Soul and
            grounded in your org's own assets.
          </p>
        </div>
        <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-3">
          {[
            { name: "Engineering",        count: 29, c: "#5fa8d6" },
            { name: "Marketing",          count: 30, c: "#e57b3a" },
            { name: "Specialized",        count: 41, c: "#cc6633" },
            { name: "Sales",              count: 8,  c: "#7fc2a3" },
            { name: "Design",             count: 8,  c: "#c98069" },
            { name: "Product",            count: 5,  c: "#6fa3c4" },
            { name: "Finance",            count: 5,  c: "#c4a86b" },
            { name: "Testing",            count: 8,  c: "#d68a5a" },
            { name: "Paid Media",         count: 7,  c: "#9b8fc4" },
            { name: "Project Mgmt",       count: 6,  c: "#7a9cc4" },
            { name: "Support",            count: 6,  c: "#7fbab0" },
            { name: "Spatial Computing",  count: 6,  c: "#b88fc4" },
            { name: "Academic",           count: 5,  c: "#9bb87f" },
            { name: "Game Development",   count: 5,  c: "#c47f99" },
          ].map((d) => (
            <div key={d.name} className="chr-card"
                 data-testid={`mockup-v2-division-${d.name.toLowerCase().replace(/\s+/g, "-")}`}>
              <div className="w-10 h-1 rounded-full mb-4"
                   style={{
                     background: `linear-gradient(90deg, ${d.c}, ${d.c}22)`,
                     boxShadow: `0 0 12px ${d.c}66`,
                   }} />
              <div className="chr-display-tight text-base chr-text mb-1">{d.name}</div>
              <div className="text-[10px] chr-mute uppercase tracking-widest font-semibold">
                {d.count} agents
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* How it works */}
      <section id="how" className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 py-20">
        <div className="text-center mb-12">
          <div className="chr-eyebrow mb-3">Four moves</div>
          <h2 className="chr-display text-4xl sm:text-5xl">
            <span className="chr-text-steel">Audit.</span>{" "}
            <span className="chr-text-steel">Brief.</span>{" "}
            <span className="chr-text-steel">Forge.</span>{" "}
            <span className="chr-text-copper">Ship.</span>
          </h2>
        </div>
        <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {[
            { i: Compass, eyebrow: "1. Audit",
              title: "Drop a URL.",
              blurb: "60-second AI audit. No card. Real PDF, scored and signed." },
            { i: Network, eyebrow: "2. Brief",
              title: "Teach Foundry your mission.",
              blurb: "One soul-print. Every agent inherits it from minute one." },
            { i: Bot, eyebrow: "3. Forge",
              title: "169 specialists go to work.",
              blurb: "Direct chats, group threads, autonomous missions. Brand-aware." },
            { i: Wrench, eyebrow: "4. Ship",
              title: "Hand off execution.",
              blurb: "ArcheForge Studios runs the campaigns, builds the site, ships the app." },
          ].map((s) => {
            const Icon = s.i;
            return (
              <div key={s.eyebrow} className="chr-card">
                <Icon className="w-5 h-5 text-blue-300 mb-3" />
                <div className="chr-eyebrow mb-1">{s.eyebrow}</div>
                <h4 className="chr-display-tight text-xl chr-text mb-2 leading-tight">
                  {s.title}
                </h4>
                <p className="text-sm chr-mute leading-relaxed font-medium">{s.blurb}</p>
              </div>
            );
          })}
        </div>
      </section>

      {/* Studio promo */}
      <section className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 py-20">
        <div className="chr-card chr-card-copper relative overflow-hidden p-10 md:p-14">
          <div className="absolute -top-20 -right-20 w-80 h-80 rounded-full opacity-50"
               style={{ background: "radial-gradient(circle, rgba(229,123,58,.4), transparent 70%)" }} />
          <div className="relative z-10 grid md:grid-cols-[1.3fr_1fr] gap-10 items-center">
            <div>
              <div className="chr-eyebrow mb-3 inline-flex items-center gap-2">
                <Wrench className="w-3 h-3" /> ArcheForge Studios
              </div>
              <h2 className="chr-display text-4xl sm:text-5xl mb-4 leading-tight">
                <span className="chr-text">When the work needs</span><br />
                <span className="chr-text-copper">human hands</span>,<br />
                <span className="chr-text">we ship it for you.</span>
              </h2>
              <p className="chr-mute leading-relaxed max-w-lg mb-6 font-medium">
                Productized, fixed-price services for the things Foundry
                recommends but you don't have time to execute.
                <span className="text-white"> Temper saves 15%. Refine gets credits.</span>
              </p>
              <Link to="/mockup/pricing-v2" className="chr-btn chr-btn-copper"
                    data-testid="mockup-v2-studio-cta">
                See the menu <ArrowRight className="w-4 h-4" />
              </Link>
            </div>
            <div className="grid grid-cols-2 gap-2 text-xs">
              {["Paid Ads", "Marketing Sites", "Web Apps", "Brand", "SEO", "Email"].map((s) => (
                <div key={s}
                     className="chr-card !p-3 text-center chr-display-tight text-sm chr-text">
                  {s}
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>

      {/* Trust strip */}
      <section className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 py-12">
        <div className="grid sm:grid-cols-3 gap-4 text-center">
          {[
            { i: ShieldCheck, k: "Bitcoin-anchored", v: "Every deliverable, every PDF, every audit." },
            { i: Cpu, k: "Encrypted vault", v: "Fernet-encrypted credential store. Per-org keys." },
            { i: Network, k: "Never sells data", v: "We don't train on you. We don't sell you. Ever." },
          ].map((t) => {
            const Icon = t.i;
            return (
              <div key={t.k} className="chr-card flex flex-col items-center gap-2">
                <Icon className="w-5 h-5 text-blue-300" />
                <div className="chr-display-tight text-lg chr-text-steel">{t.k}</div>
                <div className="text-xs chr-mute font-medium">{t.v}</div>
              </div>
            );
          })}
        </div>
      </section>

      <footer className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 py-10
                         border-t border-white/5
                         flex flex-col sm:flex-row items-start sm:items-center
                         justify-between gap-4 text-xs chr-mute font-medium">
        <div className="tracking-widest uppercase">
          © {new Date().getFullYear()} ArcheForge, LLC · Foundry
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <Link to="/legal/privacy" className="hover:text-white transition-colors"
                data-testid="mockup-v2-footer-privacy">Privacy</Link>
          <Link to="/legal/terms" className="hover:text-white transition-colors"
                data-testid="mockup-v2-footer-terms">Terms</Link>
          <Link to="/legal/sub-processors" className="hover:text-white transition-colors"
                data-testid="mockup-v2-footer-subprocessors">Sub-processors</Link>
          <Link to="/security" className="hover:text-white transition-colors"
                data-testid="mockup-v2-footer-security">Security</Link>
          <span className="opacity-50">·</span>
          <span className="inline-flex items-center gap-1.5 tracking-widest uppercase">
            <ShieldCheck className="w-3 h-3 text-blue-300" />
            foundryagents.ai
          </span>
        </div>
      </footer>
    </div>
  );
}
