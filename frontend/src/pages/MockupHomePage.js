import React, { useMemo } from "react";
import { Link } from "react-router-dom";
import {
  ArrowRight, Sparkles, Hammer, Gem, Flame, ShieldCheck,
  Network, Users, Zap, Compass, Wrench, Bot,
} from "lucide-react";
import "../styles/foundry-dark.css";

/**
 * MockupHomePage — pure-mockup route at /mockup/home.
 *
 * Replicates the industrial foundry aesthetic from the brand artwork
 * the user shared: pure-black coal background, molten orange/amber
 * edges, condensed display type, ember particles floating up the page.
 *
 * Intentionally heavier on visual flourish than the production landing
 * page — this is a "what could it look like?" pitch deck the user can
 * compare side-by-side with /  and decide whether to promote.
 */

const TIERS = [
  {
    name: "Spark",
    headline: "Ignite ideas",
    blurb: "Solo founders. Drop a thought, get a working crew of 169 specialists.",
    price: "$79",
    cadence: "/mo",
    icon: Sparkles,
    visual: "spark",
  },
  {
    name: "Temper",
    headline: "Strengthen with purpose",
    blurb: "Growing teams. Forge work into shippable deliverables, daily.",
    price: "$299",
    cadence: "/mo",
    icon: Hammer,
    visual: "anvil",
    hot: true,
  },
  {
    name: "Refine",
    headline: "Precision that leaves a mark",
    blurb: "Agencies + enterprise. White-label, multi-workspace, dedicated CSM.",
    price: "Custom",
    cadence: "",
    icon: Gem,
    visual: "ingot",
  },
];

const BRAND_ART = "https://customer-assets.emergentagent.com/job_emergent-wing/artifacts/lvc9vmgn_CDFB4D18-3EA5-4377-8C72-6F117E60C462%20%281%29.png";


function EmberField({ count = 24 }) {
  // Memoised so embers don't reposition on every render.
  const dots = useMemo(() => {
    return Array.from({ length: count }, (_, i) => ({
      left: `${Math.random() * 100}%`,
      delay: `${Math.random() * 8}s`,
      duration: `${6 + Math.random() * 8}s`,
      scale: 0.6 + Math.random() * 1.2,
      hue: Math.random() > 0.5 ? "var(--fdy-ember)" : "var(--fdy-molten)",
      key: i,
    }));
  }, [count]);
  return (
    <div className="fdy-embers" aria-hidden>
      {dots.map((d) => (
        <span
          key={d.key}
          className="fdy-ember-dot"
          style={{
            left: d.left,
            bottom: "-10px",
            animationDelay: d.delay,
            animationDuration: d.duration,
            transform: `scale(${d.scale})`,
            background: d.hue,
          }}
        />
      ))}
    </div>
  );
}


function Wordmark() {
  return (
    <Link to="/mockup/home" className="block leading-none" data-testid="mockup-wordmark">
      <div className="fdy-wordmark">FOUNDRY</div>
      <div className="fdy-sub-wordmark">
        POWERED BY <span className="accent">SOULPRINT</span> ENGINE
      </div>
    </Link>
  );
}


function TierVisual({ kind }) {
  // Pure CSS visuals so we don't need to ship more art for the mockup.
  if (kind === "spark") {
    return (
      <div className="h-32 flex items-end justify-center relative">
        {/* sparks shooting up */}
        {Array.from({ length: 7 }).map((_, i) => (
          <span
            key={i}
            className="absolute w-px"
            style={{
              left: `${30 + i * 7}%`,
              bottom: 0,
              height: `${40 + Math.random() * 60}px`,
              background: "linear-gradient(180deg, transparent, var(--fdy-ember), var(--fdy-molten))",
              filter: "blur(0.3px)",
              opacity: 0.85,
            }}
          />
        ))}
        <div
          className="absolute bottom-0 left-1/2 -translate-x-1/2 w-6 h-6 rounded-full"
          style={{
            background: "radial-gradient(circle, var(--fdy-ember), var(--fdy-molten) 60%, transparent)",
            filter: "blur(2px)",
          }}
        />
      </div>
    );
  }
  if (kind === "anvil") {
    return (
      <div className="h-32 flex items-end justify-center relative">
        <div className="relative">
          <div
            className="w-24 h-12 mx-auto"
            style={{
              background: "linear-gradient(180deg, #2a2a30, #0d0d10)",
              clipPath: "polygon(0 0, 100% 0, 90% 70%, 100% 100%, 0 100%, 10% 70%)",
              borderTop: "1px solid rgba(255,255,255,.08)",
            }}
          />
          <div
            className="absolute -top-2 left-1/2 -translate-x-1/2 w-16 h-1 rounded-full"
            style={{
              background: "var(--fdy-molten)",
              boxShadow: "0 0 16px var(--fdy-molten), 0 0 32px var(--fdy-ember)",
            }}
          />
        </div>
      </div>
    );
  }
  // ingot
  return (
    <div className="h-32 flex items-center justify-center relative">
      <div
        className="w-24 h-14"
        style={{
          background:
            "linear-gradient(135deg, #4a4a4f 0%, #2a2a30 60%, #1a1a1c 100%)",
          clipPath: "polygon(20% 0, 100% 0, 80% 100%, 0% 100%)",
          borderTop: "1px solid rgba(255,255,255,.18)",
          borderLeft: "1px solid rgba(255,255,255,.08)",
        }}
      />
      <div
        className="absolute bottom-1/3 left-1/2 -translate-x-1/2 w-20 h-px"
        style={{
          background: "var(--fdy-molten)",
          boxShadow: "0 0 12px var(--fdy-molten)",
        }}
      />
    </div>
  );
}


export default function MockupHomePage() {
  return (
    <div className="fdy-page">
      <EmberField />

      {/* Nav */}
      <header className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 py-6 flex items-center justify-between">
        <Wordmark />
        <nav className="hidden md:flex items-center gap-8 text-sm fdy-mute">
          <a href="#how" className="hover:text-white transition-colors">How it works</a>
          <a href="#tiers" className="hover:text-white transition-colors">Pricing</a>
          <a href="#studios" className="hover:text-white transition-colors">ArcheForge Studios</a>
          <Link to="/mockup/pricing" className="hover:text-white transition-colors">Mockup pricing</Link>
        </nav>
        <div className="flex items-center gap-2">
          <Link to="/auth" className="fdy-btn fdy-btn-ghost text-xs"
                data-testid="mockup-nav-signin">Sign in</Link>
          <Link to="/auth?mode=register" className="fdy-btn fdy-btn-primary text-xs"
                data-testid="mockup-nav-cta">
            Get started <ArrowRight className="w-3.5 h-3.5" />
          </Link>
        </div>
      </header>

      {/* Hero */}
      <section className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 pt-12 pb-28
                          grid lg:grid-cols-[1.1fr_1fr] gap-12 items-center">
        <div className="space-y-6">
          <div className="fdy-eyebrow inline-flex items-center gap-2">
            <Flame className="w-3 h-3 text-orange-400" /> The foundry for AI work
          </div>
          <h1 className="fdy-display text-6xl sm:text-7xl lg:text-8xl">
            Forge the<br />
            <span className="fdy-molten-text">work</span> that
            <br />
            <span className="fdy-molten-text">moves you</span>.
          </h1>
          <p className="text-lg max-w-md leading-relaxed text-stone-300">
            169 specialist AI agents. Brand-aware on day one.
            When the work needs hands on the keys,
            <span className="text-white font-semibold"> ArcheForge Studios </span>
            steps in.
          </p>
          <div className="flex flex-wrap gap-3 pt-2">
            <Link to="/get-audit" className="fdy-btn fdy-btn-primary"
                  data-testid="mockup-hero-audit">
              Free audit in 60s <ArrowRight className="w-4 h-4" />
            </Link>
            <Link to="/mockup/pricing" className="fdy-btn fdy-btn-ghost"
                  data-testid="mockup-hero-pricing">
              See the tiers
            </Link>
          </div>
          <div className="flex items-center gap-6 pt-6 text-xs fdy-mute">
            <span className="inline-flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-full"
                    style={{ background: "var(--fdy-molten)" }} />
              Bitcoin-anchored receipts
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-full"
                    style={{ background: "var(--fdy-ember)" }} />
              Invite-only pre-launch
            </span>
          </div>
        </div>

        <div className="relative">
          <div
            className="absolute inset-0 -z-10 rounded-3xl"
            style={{
              background:
                "radial-gradient(circle at 50% 50%, rgba(255,90,24,.25), transparent 70%)",
              filter: "blur(40px)",
            }}
          />
          <img
            src={BRAND_ART}
            alt="Foundry — Spark, Temper, Refine"
            className="w-full rounded-2xl border border-white/5 shadow-2xl"
            style={{ boxShadow: "0 0 60px rgba(255,90,24,.18)" }}
          />
        </div>
      </section>

      <div className="max-w-7xl mx-auto px-6 md:px-10">
        <div className="fdy-rule" />
      </div>

      {/* The three temperatures */}
      <section id="tiers" className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 py-24">
        <div className="text-center mb-14">
          <div className="fdy-eyebrow mb-3">Three temperatures</div>
          <h2 className="fdy-display text-5xl sm:text-6xl">
            One foundry. <span className="fdy-molten-text">Three flames.</span>
          </h2>
          <p className="text-base fdy-mute mt-4 max-w-xl mx-auto">
            From the first idea to the final ingot — each tier is named
            for what we do to your work at that temperature.
          </p>
        </div>

        <div className="grid md:grid-cols-3 gap-6">
          {TIERS.map((t) => {
            const Icon = t.icon;
            return (
              <div
                key={t.name}
                className={`fdy-card ${t.hot ? "fdy-card-hot" : ""}`}
                data-testid={`mockup-tier-${t.name.toLowerCase()}`}
              >
                <TierVisual kind={t.visual} />
                <div className="flex items-center gap-2 mb-2 mt-4">
                  <Icon className="w-4 h-4 text-orange-400" />
                  <span className="fdy-eyebrow">{t.name}</span>
                </div>
                <h3 className="fdy-display text-4xl mb-2 fdy-molten-text">
                  {t.headline}
                </h3>
                <p className="text-sm fdy-mute leading-relaxed mb-5">{t.blurb}</p>
                <div className="flex items-baseline gap-1 mb-4">
                  <span className="fdy-display text-3xl">{t.price}</span>
                  <span className="fdy-mute text-sm">{t.cadence}</span>
                </div>
                <Link to="/mockup/pricing"
                      className={`fdy-btn ${t.hot ? "fdy-btn-primary" : "fdy-btn-ghost"} w-full justify-center text-xs`}>
                  See {t.name} details <ArrowRight className="w-3.5 h-3.5" />
                </Link>
              </div>
            );
          })}
        </div>
      </section>

      {/* How it works strip */}
      <section id="how" className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 py-24">
        <div className="text-center mb-14">
          <div className="fdy-eyebrow mb-3">How it works</div>
          <h2 className="fdy-display text-5xl sm:text-6xl">
            From <span className="fdy-molten-text">spark</span> to{" "}
            <span className="fdy-molten-text">ingot</span> in days, not quarters.
          </h2>
        </div>
        <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {[
            { i: Compass, eyebrow: "1. Audit", title: "Drop a URL.",
              blurb: "60-second AI audit. No card. Receive a real PDF, scored and signed." },
            { i: Network, eyebrow: "2. Soul", title: "Teach Foundry your mission.",
              blurb: "One brief defines your tone, audience, values. Every agent inherits it." },
            { i: Bot, eyebrow: "3. Forge", title: "169 specialists go to work.",
              blurb: "Direct chats, group threads, autonomous missions. All brand-aware." },
            { i: Wrench, eyebrow: "4. Studio", title: "Hand off execution.",
              blurb: "ArcheForge Studios runs the campaigns, builds the site, ships the app." },
          ].map((s) => {
            const Icon = s.i;
            return (
              <div key={s.eyebrow} className="fdy-card">
                <Icon className="w-5 h-5 text-orange-400 mb-3" />
                <div className="fdy-eyebrow mb-1">{s.eyebrow}</div>
                <h4 className="fdy-display-condensed text-2xl mb-2">{s.title}</h4>
                <p className="text-sm fdy-mute leading-relaxed">{s.blurb}</p>
              </div>
            );
          })}
        </div>
      </section>

      {/* Studio promo */}
      <section id="studios" className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 py-24">
        <div className="fdy-card fdy-card-hot relative overflow-hidden p-10 md:p-14">
          <div className="absolute -top-20 -right-20 w-72 h-72 rounded-full"
               style={{ background: "radial-gradient(circle, rgba(255,90,24,.4), transparent 70%)" }} />
          <div className="relative z-10 grid md:grid-cols-[1.4fr_1fr] gap-8 items-center">
            <div>
              <div className="fdy-eyebrow mb-3 inline-flex items-center gap-2">
                <Wrench className="w-3 h-3" /> ArcheForge Studios
              </div>
              <h2 className="fdy-display text-4xl sm:text-5xl mb-4">
                When the work needs <span className="fdy-molten-text">human hands</span>,
                we ship it for you.
              </h2>
              <p className="text-base fdy-mute leading-relaxed max-w-lg mb-6">
                Productized, fixed-price services for the things Foundry
                recommends but you don't have time to execute. Paid ads.
                Marketing sites. App MVPs. Brand identity.
                <span className="text-white"> Temper saves 15%. Refine gets credits.</span>
              </p>
              <Link to="/mockup/pricing" className="fdy-btn fdy-btn-primary"
                    data-testid="mockup-studio-cta">
                See the menu <ArrowRight className="w-4 h-4" />
              </Link>
            </div>
            <div className="grid grid-cols-2 gap-3 text-xs">
              {["Paid Ads", "Marketing Sites", "Web Apps", "Brand", "SEO", "Email Automation"].map((s) => (
                <div key={s} className="fdy-card !p-3 text-center fdy-display-condensed text-sm">
                  {s}
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>

      <footer className="relative z-10 max-w-7xl mx-auto px-6 md:px-10 py-10 border-t border-white/5
                         flex flex-wrap items-center justify-between gap-3 text-xs fdy-mute">
        <span>© {new Date().getFullYear()} Foundry · Powered by SoulPrint Engine</span>
        <span className="inline-flex items-center gap-2">
          <ShieldCheck className="w-3 h-3 text-orange-400" />
          foundryagents.ai
        </span>
      </footer>
    </div>
  );
}
