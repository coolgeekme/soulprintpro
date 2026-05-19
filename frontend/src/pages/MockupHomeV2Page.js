import React from "react";
import { Link } from "react-router-dom";
import {
  ArrowRight, ShieldCheck, Network, Bot, Compass, Wrench, Cpu, Hexagon,
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

const SIGIL_IMG = "https://customer-assets.emergentagent.com/job_emergent-wing/artifacts/sner1z09_Foundry%20intro%20%281%29.png";
const HERO_LOGO = "https://customer-assets.emergentagent.com/job_emergent-wing/artifacts/sner1z09_Foundry%20intro%20%281%29.png";
// Side-by-side marketing banner showcasing what a Spark-tier Foundry
// workspace produces — slotted into the "See what Foundry forges"
// section below the tier grid.
const SPARK_SHOWCASE_IMG = "https://customer-assets.emergentagent.com/job_emergent-wing/artifacts/c12bbtiy_26b7e30cac5e4b58b89f271b067737b3.png";
// Compact stacked-mark used in the navbar (emblem + FOUNDRY + tagline).
// Different file from HERO_LOGO so we can tune size/crop independently
// when the hero artwork evolves.
const NAV_LOGO = "https://customer-assets.emergentagent.com/job_emergent-wing/artifacts/zntnh88h_Foundry%20logo.png";

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
        style={{ filter: "drop-shadow(0 0 6px rgba(216,118,49,.22))" }}
      />
      <div className="chr-sub-wordmark leading-tight">
        POWERED BY <span className="accent">SOULPRINT</span> ENGINE
      </div>
    </Link>
  );
}


/**
 * Tier sigil — pulls one third of the v2 sigil sheet using
 * background-position. Keeps the three emblems "fused" to the same
 * source artwork without exporting three files.
 *
 * The "Foundry intro" composition stacks the FOUNDRY mark on top and
 * three tier cards along the bottom half. We crop the bottom band
 * (vertical ~65%) and slide horizontally to pick each tier panel.
 */
function TierSigil({ offset = "50%" }) {
  return (
    <div className="chr-sigil-mount">
      <div
        className="w-40 h-40"
        role="img"
        aria-label="Foundry tier sigil"
        style={{
          backgroundImage: `url('${SIGIL_IMG}')`,
          backgroundRepeat: "no-repeat",
          // Crop the lower band where the three tier emblems live.
          backgroundPosition: `${offset} 62%`,
          backgroundSize: "300% auto",
          filter: "drop-shadow(0 0 12px rgba(95,168,214,.25))",
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
          {/* Crop the TOP half of the Foundry-intro image — that's
              where the master diamond + FOUNDRY wordmark live. The
              bottom half (three tier cards) is repurposed below in
              the tiers section via background-position. */}
          <div
            role="img"
            aria-label="Foundry — Forge your edge"
            className="w-full max-w-[560px] mx-auto aspect-square"
            style={{
              backgroundImage: `url('${HERO_LOGO}')`,
              backgroundRepeat: "no-repeat",
              backgroundPosition: "center top",
              backgroundSize: "100% auto",
              filter: "drop-shadow(0 30px 80px rgba(95,168,214,.18))",
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
              <TierSigil offset={t.sigilOffset} />
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

      {/* Spark showcase — side-by-side marketing assets straight out
          of a Spark-tier workspace. Shows prospects exactly what the
          platform produces on Day 1 before they commit to a plan. */}
      <section className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 py-20">
        <div className="text-center mb-10">
          <div className="chr-eyebrow mb-3">Day-one output</div>
          <h2 className="chr-display text-4xl sm:text-5xl">
            <span className="chr-text">What a </span>
            <span className="chr-text-copper">Spark</span>
            <span className="chr-text"> workspace produces.</span>
          </h2>
          <p className="chr-mute mt-4 max-w-xl mx-auto font-medium">
            Brand-aware marketing assets, generated by your specialists
            in minutes — not weeks. Below: a real pair pulled from a
            Spark-tier launch.
          </p>
        </div>
        <div className="chr-card relative overflow-hidden p-2 md:p-3"
             data-testid="mockup-v2-spark-showcase">
          <img
            src={SPARK_SHOWCASE_IMG}
            alt="Two Spark-tier marketing banners: Power Your Small Business with Spark, and Small Business? Meet Big Innovation"
            className="w-full h-auto rounded-lg block"
            loading="lazy"
            style={{
              filter: "drop-shadow(0 20px 60px rgba(95,168,214,.12))",
            }}
          />
        </div>
        <div className="flex justify-center mt-6">
          <Link to="/mockup/pricing-v2" className="chr-btn chr-btn-ghost">
            See what Temper + Refine unlock <ArrowRight className="w-4 h-4" />
          </Link>
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
                         border-t border-white/5 flex flex-wrap items-center
                         justify-between gap-3 text-xs chr-mute font-medium">
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
