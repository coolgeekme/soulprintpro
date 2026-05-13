import React from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Users, Network, ShieldCheck, Radio, HelpCircle, Wand2, Sun, Moon } from "lucide-react";
import Logo from "../components/Logo";
import { useTheme } from "../context/ThemeContext";

const Pill = ({ children }) => <span className="sp-chip">{children}</span>;

const FeatureCard = ({ icon: Icon, title, body }) => (
  <div className="sp-glass rounded-2xl p-6 sp-fade-up">
    <div className="w-10 h-10 rounded-xl flex items-center justify-center sp-glass-strong mb-4">
      <Icon className="w-5 h-5 text-white/90" />
    </div>
    <h3 className="text-lg font-medium text-white">{title}</h3>
    <p className="text-sm sp-subtle mt-1 leading-relaxed">{body}</p>
  </div>
);

export default function LandingPage() {
  const { mode, toggle } = useTheme();
  return (
    <div className="relative min-h-screen overflow-x-hidden">
      {/* Aurora halo */}
      <div className="pointer-events-none fixed inset-0 -z-10">
        <div className="sp-aurora" />
      </div>

      {/* Nav */}
      <header
        className="relative z-10 flex items-center justify-between px-6 md:px-10 py-6"
        style={{
          // Push content below the iPhone notch / Dynamic Island when
          // running as a PWA. Resolves to 0 in a regular browser window
          // so desktop layout is unchanged.
          paddingTop: "calc(env(safe-area-inset-top, 0px) + 1.5rem)",
          paddingLeft: "max(1.5rem, env(safe-area-inset-left, 0px))",
          paddingRight: "max(1.5rem, env(safe-area-inset-right, 0px))",
        }}
      >
        <Link to="/" className="flex items-center gap-2" data-testid="brand-logo" aria-label="Foundry home">
          <Logo variant="wordmark" className="h-9 w-auto" />
        </Link>
        <nav className="hidden md:flex items-center gap-8 text-sm sp-subtle">
          <a href="#features" className="hover:text-white transition-colors">How it works</a>
          <a href="#agents" className="hover:text-white transition-colors">Agents</a>
          <a href="#teams" className="hover:text-white transition-colors">Teams</a>
          <Link to="/about" className="hover:text-white transition-colors" data-testid="nav-about">What is Foundry?</Link>
          {/* Pricing link intentionally hidden until pricing is locked in.
              The /pricing route is still live — staff visit directly. */}
          <Link to="/trust" className="hover:text-white transition-colors" data-testid="nav-trust">Trust &amp; Security</Link>
        </nav>
        <div className="flex items-center gap-2">
          <Link
            to="/about"
            className="sp-btn sp-btn-ghost text-sm md:hidden"
            data-testid="nav-about-mobile"
            title="What is Foundry?"
            aria-label="What is Foundry?"
          >
            <HelpCircle className="w-4 h-4" />
          </Link>
          <button
            type="button"
            onClick={toggle}
            className="sp-btn sp-btn-ghost text-sm"
            data-testid="landing-theme-toggle"
            aria-label="Toggle theme"
            title={mode === "dark" ? "Switch to light mode" : "Switch to dark mode"}
          >
            {mode === "dark" ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
          </button>
          <Link to="/auth" className="sp-btn sp-btn-ghost text-sm" data-testid="nav-signin">Sign in</Link>
          <Link to="/auth?mode=register" className="sp-btn sp-btn-primary text-sm" data-testid="nav-getstarted">
            Get started <ArrowRight className="w-4 h-4" />
          </Link>
        </div>
      </header>

      {/* Hero */}
      <section className="relative z-10 max-w-6xl mx-auto px-6 md:px-10 pt-16 pb-28">
        <div className="flex flex-col items-start gap-6">
          <Pill>For ambitious teams</Pill>
          <h1 className="sp-display text-5xl sm:text-6xl lg:text-7xl max-w-4xl">
            <span className="sp-gradient-text">Your company has a soul.</span>
            <br />
            <span className="italic text-white/75">Teach it to your AI.</span>
          </h1>
          <p className="sp-subtle text-base md:text-lg max-w-2xl leading-relaxed">
            Foundry gives your team a working crew of specialised AI agents that know your mission,
            your tone, and your people. Spin up org-wide teams, brief them once, and collaborate in direct
            and group chats that actually feel like yours.
          </p>
          <div className="flex flex-wrap gap-3 mt-2">
            <Link to="/auth?mode=register" className="sp-btn sp-btn-primary" data-testid="hero-cta-primary">
              Start your SoulPrint <ArrowRight className="w-4 h-4" />
            </Link>
            {/* Signal Audit CTA — drives prospects into the
                self-serve audit funnel. Visually distinct from the
                primary "register" CTA so they don't compete: this is
                accent-orange-bordered ghost, the primary stays solid. */}
            <Link
              to="/get-audit"
              className="sp-btn"
              style={{
                background: "rgba(251,146,60,0.10)",
                border: "1px solid rgba(251,146,60,0.40)",
                color: "rgb(251,146,60)",
              }}
              data-testid="hero-cta-audit"
            >
              <Wand2 className="w-4 h-4" /> Try a Signal Audit
            </Link>
            <a href="#agents" className="sp-btn sp-btn-ghost" data-testid="hero-cta-secondary">Meet the agents</a>
          </div>
          <div className="flex items-center gap-6 mt-6 text-xs sp-subtle">
            <span className="inline-flex items-center gap-2"><span className="sp-dot bg-emerald-400/80"/> Private workspaces</span>
            <span className="inline-flex items-center gap-2"><span className="sp-dot bg-sky-400/80"/> Group + direct chats</span>
            <span className="inline-flex items-center gap-2"><span className="sp-dot bg-fuchsia-400/80"/> Tuned to your brand</span>
          </div>
        </div>
      </section>

      <div className="sp-divider max-w-6xl mx-auto" />

      {/* Features */}
      <section id="features" className="relative z-10 max-w-6xl mx-auto px-6 md:px-10 py-24">
        <div className="flex flex-col items-start gap-4 mb-12">
          <Pill>Why SoulPrint</Pill>
          <h2 className="sp-display text-4xl sm:text-5xl max-w-3xl">
            Not another chatbot. A <em className="italic">crew</em> that understands your company.
          </h2>
        </div>
        <div className="grid md:grid-cols-3 gap-4">
          <FeatureCard icon={Network} title="One SoulPrint, every agent"
            body="Define your team's mission, tone, audience, and values once — every agent on the team is briefed automatically." />
          <FeatureCard icon={Users} title="Group + direct conversations"
            body="Work 1:1 with an agent or pull multiple humans and agents into a single thread. @mention to steer, or let the room respond." />
          <FeatureCard icon={Radio} title="150+ specialised agents"
            body="Engineers, strategists, copywriters, researchers, ops, finance, legal, product, XR, game dev — the full agency roster, tuned to your team." />
          <FeatureCard icon={ShieldCheck} title="Org-first architecture"
            body="Invite your company by link, spin up teams per product, brand, or squad. Keep contexts clean and conversations private." />
          <FeatureCard icon={Logo} title="Tuned, not templated"
            body="Your brief is woven into every system prompt, so answers feel like they came from someone who actually works at your company." />
          <FeatureCard icon={ArrowRight} title="Ship in minutes"
            body="No configuration theatre. Create an org, draft your SoulPrint, and you're in a working thread in under three minutes." />
        </div>
      </section>

      <section id="agents" className="relative z-10 max-w-6xl mx-auto px-6 md:px-10 py-24">
        <div className="flex flex-col items-start gap-4 mb-12">
          <Pill>The crew</Pill>
          <h2 className="sp-display text-4xl sm:text-5xl max-w-3xl">
            150+ specialists. 14 divisions. <em className="italic">One voice — yours.</em>
          </h2>
          <p className="sp-subtle max-w-2xl">
            Frontend wizards, brand guardians, deal coaches, privacy architects, narrative designers —
            a full agency of specialists, tuned by your team's SoulPrint and grounded in your
            organisation's own assets.
          </p>
        </div>
        <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-3">
          {[
            { name: "Engineering", count: 29, c: "#22d3ee" },
            { name: "Marketing", count: 30, c: "#f472b6" },
            { name: "Specialized", count: 41, c: "#F64000" },
            { name: "Sales", count: 8, c: "#34d399" },
            { name: "Design", count: 8, c: "#f97066" },
            { name: "Product", count: 5, c: "#38bdf8" },
            { name: "Finance", count: 5, c: "#facc15" },
            { name: "Testing", count: 8, c: "#fb923c" },
            { name: "Paid Media", count: 7, c: "#c084fc" },
            { name: "Project Mgmt", count: 6, c: "#60a5fa" },
            { name: "Support", count: 6, c: "#2dd4bf" },
            { name: "Spatial Computing", count: 6, c: "#e879f9" },
            { name: "Academic", count: 5, c: "#a3e635" },
            { name: "Game Development", count: 5, c: "#fb7185" },
          ].map((d) => (
            <div key={d.name} className="sp-glass rounded-2xl p-5">
              <div className="w-9 h-9 rounded-lg mb-4" style={{ background: `linear-gradient(160deg, ${d.c}, rgba(255,255,255,0.06))` }} />
              <div className="text-white font-medium">{d.name}</div>
              <div className="text-xs sp-subtle mt-1 uppercase tracking-widest">{d.count} agents</div>
            </div>
          ))}
        </div>
      </section>

      {/* Closing CTA */}
      <section id="teams" className="relative z-10 max-w-6xl mx-auto px-6 md:px-10 py-24">
        <div className="sp-glass-strong rounded-3xl p-10 md:p-14 text-center flex flex-col items-center gap-6">
          <Pill>Ready when you are</Pill>
          <h3 className="sp-display text-4xl sm:text-5xl max-w-2xl">
            Give your company an AI crew that feels like it already works there.
          </h3>
          <Link to="/auth?mode=register" className="sp-btn sp-btn-primary" data-testid="footer-cta-primary">
            Create your org <ArrowRight className="w-4 h-4" />
          </Link>
        </div>
      </section>

      <footer className="relative z-10 max-w-6xl mx-auto px-6 md:px-10 py-10 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 text-xs sp-subtle">
        <div>© {new Date().getFullYear()} ArcheForge, LLC · Foundry</div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <Link to="/legal/privacy" className="hover:text-white transition-colors" data-testid="footer-privacy">Privacy</Link>
          <Link to="/legal/terms" className="hover:text-white transition-colors" data-testid="footer-terms">Terms</Link>
          <Link to="/legal/sub-processors" className="hover:text-white transition-colors" data-testid="footer-subprocessors">Sub-processors</Link>
          <Link to="/security" className="hover:text-white transition-colors" data-testid="footer-security">Security</Link>
          <span className="opacity-50">·</span>
          <span>Built for teams</span>
        </div>
      </footer>
    </div>
  );
}
