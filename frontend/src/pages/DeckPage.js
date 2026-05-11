import React, { useEffect, useState, useCallback, useRef } from "react";
import { Link } from "react-router-dom";
import {
  ArrowLeft, ArrowRight, Wand2, Users, Rocket, FileText, Megaphone, Building2, MessageCircle, Maximize2, Minimize2, Sun, Moon, Target, Monitor, ShieldCheck
} from "lucide-react";
import Logo from "../components/Logo";
import { useTheme } from "../context/ThemeContext";

/**
 * DeckPage — public, brand-aligned product showcase deck at `/deck`.
 *
 * Eight slides, full-viewport, keyboard-navigable. Designed to be the
 * single shareable URL we hand to a prospect: "here's the 5-minute tour".
 * No auth required — the deck embeds real screenshots from the preview
 * environment and lets the viewer drive at their own pace.
 *
 * Why HTML over PDF/PPTX:
 *  - Live URL, always up to date with the brand
 *  - Animated reveals add polish that static decks can't
 *  - Mobile-friendly out of the box
 *  - One click → /auth and they're inside the product
 */

// ---- Slide content -------------------------------------------------------
// Kept inside the file (not a JSON config) because each slide has its own
// composition. If we ever ship a v2 with 12+ slides we can extract these
// to /src/data/deck.js — not worth the indirection at 8.

const SLIDES = [
  // 1. Hero
  {
    id: "hero",
    kind: "hero",
    eyebrow: "A guided tour",
    title: "Your AI agency,\nbrand-aware on day one.",
    subtitle:
      "Foundry turns one prompt into a polished deliverable — \n drafted, designed, and shipped by 169+ specialist agents that already \n know your brand voice.",
    cta: "Use ← / → to explore",
  },

  // 2. The Soul (brand alignment)
  {
    id: "soul",
    kind: "feature",
    eyebrow: "The SoulPrint",
    icon: Wand2,
    title: "Your brand, distilled\ninto every reply.",
    body: [
      "Drop in your website, a description, or both.",
      "We synthesise the mission, audience, tone of voice, vocabulary, and visual style — and inject it into every agent prompt.",
      "Re-synthesise any time the brand evolves; everyone realigns instantly.",
    ],
    bullets: [
      "Mission & north-star captured automatically",
      "Tone, vocabulary, and 'do-not-use' phrases enforced",
      "Image style guide flows into generated visuals",
    ],
    image: "/deck/02-org.jpg",
    imageCaption: "An organization page — chat, queue, missions, mind map, all anchored on the same Soul.",
  },

  // 3. 169 specialist agents
  {
    id: "agents",
    kind: "feature",
    icon: Users,
    eyebrow: "The Crew",
    title: "169 specialists.\nOne front door.",
    body: [
      "Just type. The Chief of Staff smart-routes you to the right specialist — Strategist, Researcher, Copywriter, Engineer, Designer, you name it.",
      "Need a second opinion? Add agents to the room. They respond on @mention or auto-respond as the topic shifts.",
    ],
    bullets: [
      "14 divisions covering strategy, content, research, ops, design, dev",
      "Group chat with @mentions, smart expand, fork-to-private",
      "Each specialist writes inside your brand voice automatically",
    ],
    image: "/deck/06-chat.jpg",
    imageCaption: "Group chat — Reality Checker, Game Audio Engineer, Chief of Staff, all aligned on your Soul.",
  },

  // 4. Mission Mode
  {
    id: "missions",
    kind: "feature",
    icon: Rocket,
    eyebrow: "Mission Mode",
    title: "Set a goal.\nWalk away.",
    body: [
      "Mission Mode hands the wheel to a lead agent. It searches the live web, browses real pages, fills forms, drafts copy, designs covers, and ships every artifact to your library — autonomously.",
      "You can pause, answer a question, take control of the browser, or cancel — anytime.",
    ],
    bullets: [
      "Real Chromium browser — clicks, fills, submits",
      "Perplexity + Brave for fresh, cited research",
      "Heartbeat watchdog auto-recovers stalled runs",
    ],
    image: "/deck/04-missions.jpg",
    imageCaption: "Missions list — every run timestamped, every artifact saved.",
  },

  // 5. Social Queue
  {
    id: "social",
    kind: "feature",
    icon: Megaphone,
    eyebrow: "Social Queue",
    title: "Draft, schedule,\nautopost.",
    body: [
      "Generate platform-perfect drafts across 11 networks. Edit, attach a generated cover (Nano Banana), schedule, and queue.",
      "For X, LinkedIn, Facebook, Threads, Bluesky, Mastodon, and Reddit — let an autonomous Mission open the composer and publish on time.",
    ],
    bullets: [
      "11 platforms — text, hashtags, optional cover image",
      "Brand voice + visual style guide baked into every draft",
      "Auto-publish via Mission (no API keys to wrangle)",
    ],
    image: "/deck/03-social.jpg",
    imageCaption: "Social Queue — drafts on the left, schedule on the right, one click to publish.",
  },

  // 6. Documents / Artifacts
  {
    id: "docs",
    kind: "feature",
    icon: FileText,
    eyebrow: "Real Deliverables",
    title: "Polished PDFs,\nnot copy-paste.",
    body: [
      "Every brief, audit, plan, or report comes back as a real .pdf or .docx — typeset, branded, downloadable in one click.",
      "Lists land as .csv, calendar invites as .ics, code as .sh / .json. Agents pick the format that fits.",
    ],
    bullets: [
      "PDF + DOCX synthesis built in (reportlab + python-docx)",
      "Files mirrored to durable storage — no broken links",
      "Searchable, kind-filtered Documents library per org",
    ],
    image: "/deck/02-org.jpg",
    imageCaption: "Org page — Documents library lives one click below the chat.",
  },

  // 7. Workspaces & teams
  {
    id: "workspaces",
    kind: "feature",
    icon: Building2,
    eyebrow: "Built for B2B",
    title: "Workspaces,\norganizations,\nteams.",
    body: [
      "A Workspace is your agency umbrella. Inside it, Organizations represent each client, brand, or product. Teams scope the work — each with its own private SoulPrint.",
      "One invite link grants role-based access across multiple orgs. Open or closed teams keep sensitive work private.",
    ],
    bullets: [
      "Three-tier hierarchy: Workspace → Org → Team",
      "Role-based invites with multi-org grants",
      "Open/Closed teams + private threads + per-org Documents",
    ],
    image: "/deck/05-workspace.jpg",
    imageCaption: "Workspace settings — orgs in this workspace, invites, members, all in one place.",
  },

  // 8. Public Business Audits — homepage lead funnel
  {
    id: "audits",
    kind: "feature",
    icon: Target,
    eyebrow: "Top-of-funnel",
    title: "Drop a URL.\nShip a real audit\nin 60 seconds.",
    body: [
      "Anyone with a website link can self-serve a personalised audit from the homepage — no signup, no credit card.",
      "Our agents scrape the site, fetch reviews and competitor signal, score the brand, and write back a prioritised fix list mapped to features they can act on inside Foundry.",
    ],
    bullets: [
      "Public report, branded teaser → lead-gated full PDF",
      "Auto-emailed to the visitor via Resend on completion",
      "Bitcoin-anchored timestamp on every PDF for tamper-proof citation",
    ],
    image: "/deck/08-audit-report.jpg",
    imageCaption: "Public audit report — score, strengths, critical issues, with a lead-gated full PDF unlock.",
  },

  // 9. Agent Mode — ChatGPT-style live browser
  {
    id: "agent-mode",
    kind: "feature",
    icon: Monitor,
    eyebrow: "Agent Mode",
    title: "Watch your agent\ndrive a real browser.",
    body: [
      "When an agent needs to log into a site, fill a form, or click something a scraper can't, Agent Mode opens a live remote Chromium and streams the screen back to you in real time.",
      "Take over with a click. Save the session as a named login. Next time, the agent reuses it — no re-auth, no leaked credentials.",
    ],
    bullets: [
      "Live CDP stream — see every click, type, scroll",
      "One-tap human takeover for CAPTCHAs and 2FA",
      "Encrypted credential vault (Fernet) per organization",
    ],
    image: "/deck/04-missions.jpg",
    imageCaption: "Mission Mode + Agent Mode — autonomous browser work with a human escape hatch.",
  },

  // 10. Proof of Integrity — Bitcoin-anchored trust
  {
    id: "proof",
    kind: "feature",
    icon: ShieldCheck,
    eyebrow: "Proof of Integrity",
    title: "Every deliverable,\nanchored to Bitcoin.",
    body: [
      "Each document, audit, image, and report gets a SHA-256 fingerprint stamped onto the Bitcoin blockchain via OpenTimestamps — for free, forever, and verifiable by anyone with a copy of the file.",
      "Pair that with brute-force lockout, CSP enforcement, encrypted credential storage, and never-sold-to-anyone data — and your enterprise security review is no longer the bottleneck.",
    ],
    bullets: [
      "OpenTimestamps + Merkle batching (no per-file fee)",
      "Public verify page — prospects can audit YOU, not just trust you",
      "SOC-grade hygiene: CSP enforce, rate limits, encrypted vault",
    ],
    image: "/deck/09-trust.jpg",
    imageCaption: "Trust page — public verifier, security posture, what we promise we never do.",
  },

  // 8. CTA
  {
    id: "cta",
    kind: "cta",
    eyebrow: "Ready to try it?",
    title: "Spin up your first\norganization in 60 seconds.",
    subtitle:
      "Paste a website. We'll read it, synthesise your Soul, and stand up a Chief of Staff chat ready to ship.",
    primaryHref: "/auth?mode=register",
    primaryLabel: "Create your free org",
    secondaryHref: "/about",
    secondaryLabel: "What is Foundry?",
  },
];

// ---- Page ---------------------------------------------------------------

export default function DeckPage() {
  const { mode, toggle } = useTheme();
  const [idx, setIdx] = useState(0);
  const [direction, setDirection] = useState(1); // 1=fwd, -1=back — drives slide-in animation
  const wrapRef = useRef(null);
  const total = SLIDES.length;

  const go = useCallback((next) => {
    setIdx((cur) => {
      const clamped = Math.max(0, Math.min(total - 1, next));
      setDirection(clamped > cur ? 1 : -1);
      return clamped;
    });
  }, [total]);

  // Keyboard nav. Capture-phase listener so it works even when no
  // element has focus (the typical "first load" state).
  useEffect(() => {
    const onKey = (e) => {
      if (e.target?.tagName === "INPUT" || e.target?.tagName === "TEXTAREA") return;
      if (e.key === "ArrowRight" || e.key === " " || e.key === "PageDown") {
        e.preventDefault();
        go(idx + 1);
      } else if (e.key === "ArrowLeft" || e.key === "PageUp") {
        e.preventDefault();
        go(idx - 1);
      } else if (e.key === "Home") { go(0); }
      else if (e.key === "End")   { go(total - 1); }
      else if (e.key === "f" || e.key === "F") { toggleFullscreen(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [idx, go, total]);

  const [fs, setFs] = useState(false);
  const toggleFullscreen = useCallback(() => {
    const el = wrapRef.current || document.documentElement;
    if (!document.fullscreenElement) {
      el.requestFullscreen?.().then(() => setFs(true)).catch(() => {});
    } else {
      document.exitFullscreen?.().then(() => setFs(false)).catch(() => {});
    }
  }, []);

  useEffect(() => {
    const onFsChange = () => setFs(Boolean(document.fullscreenElement));
    document.addEventListener("fullscreenchange", onFsChange);
    return () => document.removeEventListener("fullscreenchange", onFsChange);
  }, []);

  const slide = SLIDES[idx];

  return (
    <div
      ref={wrapRef}
      className="min-h-screen w-full relative overflow-hidden sp-grain"
      data-testid="deck-page"
      style={{
        // Aurora wash — same recipe as the landing page, slightly amped
        // so each slide feels its own without losing brand continuity.
        backgroundImage: `
          radial-gradient(1100px 700px at 12% -10%, var(--bg-aurora-1), transparent 60%),
          radial-gradient(900px 500px at 92% 110%, var(--bg-aurora-3), transparent 55%),
          radial-gradient(800px 400px at 50% 50%, var(--bg-aurora-2), transparent 70%)
        `,
        backgroundColor: "var(--bg)",
      }}
    >
      {/* Top chrome — logo + theme + fullscreen toggle. Stays out of the
          slide content so the canvas reads cleanly. */}
      <header
        className="absolute top-0 left-0 right-0 z-30 flex items-center justify-between px-5 sm:px-10 py-4"
        style={{ paddingTop: "calc(env(safe-area-inset-top, 0px) + 12px)" }}
      >
        <Link to="/" className="flex items-center gap-2 shrink-0" data-testid="deck-brand">
          <Logo variant="mark" className="w-8 h-8 sm:hidden" />
          <Logo variant="wordmark" className="hidden sm:block h-7 w-auto" />
        </Link>
        <div className="flex items-center gap-2">
          <span className="hidden sm:block text-[11px] tracking-widest uppercase sp-subtle pr-3">
            Slide {idx + 1} of {total}
          </span>
          <button
            onClick={toggle}
            className="sp-btn sp-btn-ghost"
            title={mode === "dark" ? "Light mode" : "Dark mode"}
            data-testid="deck-theme-toggle"
            aria-label="Toggle theme"
          >
            {mode === "dark" ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
          </button>
          <button
            onClick={toggleFullscreen}
            className="sp-btn sp-btn-ghost hidden sm:inline-flex"
            title="Fullscreen (F)"
            data-testid="deck-fs-toggle"
            aria-label="Toggle fullscreen"
          >
            {fs ? <Minimize2 className="w-4 h-4" /> : <Maximize2 className="w-4 h-4" />}
          </button>
        </div>
      </header>

      {/* Slide canvas — single visible slide, animated in from the side
          based on direction. We don't render off-screen siblings to keep
          the DOM cheap; the only "trick" is keying the slide on idx so
          React unmounts/remounts it and the entrance animation re-fires. */}
      <main className="min-h-screen flex items-center justify-center px-5 sm:px-10 py-24">
        <div
          key={`slide-${idx}`}
          className="w-full max-w-6xl mx-auto"
          style={{
            animation: `${direction > 0 ? "deckSlideInRight" : "deckSlideInLeft"} 480ms cubic-bezier(0.22, 1, 0.36, 1) both`,
          }}
        >
          {slide.kind === "hero"    && <HeroSlide slide={slide} />}
          {slide.kind === "feature" && <FeatureSlide slide={slide} />}
          {slide.kind === "cta"     && <CtaSlide slide={slide} />}
        </div>
      </main>

      {/* Bottom nav — chunky prev/next + clickable dot index */}
      <footer
        className="absolute bottom-0 left-0 right-0 z-30 px-5 sm:px-10 pb-6"
        style={{ paddingBottom: "calc(env(safe-area-inset-bottom, 0px) + 16px)" }}
      >
        <div className="max-w-6xl mx-auto flex items-center justify-between gap-3">
          <button
            onClick={() => go(idx - 1)}
            disabled={idx === 0}
            className="sp-btn sp-btn-ghost"
            data-testid="deck-prev"
            aria-label="Previous slide"
          >
            <ArrowLeft className="w-4 h-4" /> <span className="hidden sm:inline">Back</span>
          </button>
          <div className="flex items-center gap-2" data-testid="deck-dots">
            {SLIDES.map((s, i) => (
              <button
                key={s.id}
                onClick={() => go(i)}
                aria-label={`Go to slide ${i + 1}`}
                data-testid={`deck-dot-${i}`}
                className="transition-all"
                style={{
                  width: i === idx ? 32 : 8,
                  height: 8,
                  borderRadius: 999,
                  background: i === idx
                    ? "linear-gradient(90deg, #f64000, #ff8e3a)"
                    : "color-mix(in srgb, var(--fg) 22%, transparent)",
                }}
              />
            ))}
          </div>
          <button
            onClick={() => go(idx + 1)}
            disabled={idx === total - 1}
            className="sp-btn sp-btn-primary"
            data-testid="deck-next"
            aria-label="Next slide"
          >
            <span className="hidden sm:inline">Next</span> <ArrowRight className="w-4 h-4" />
          </button>
        </div>
      </footer>

      {/* Slide-in keyframes (scoped via <style> so we don't pollute index.css) */}
      <style>{`
        @keyframes deckSlideInRight {
          from { opacity: 0; transform: translateX(40px) scale(0.985); }
          to   { opacity: 1; transform: translateX(0)    scale(1); }
        }
        @keyframes deckSlideInLeft {
          from { opacity: 0; transform: translateX(-40px) scale(0.985); }
          to   { opacity: 1; transform: translateX(0)     scale(1); }
        }
        @keyframes deckRiseIn {
          from { opacity: 0; transform: translateY(14px); }
          to   { opacity: 1; transform: translateY(0); }
        }
        .deck-rise > * { animation: deckRiseIn 600ms cubic-bezier(0.22, 1, 0.36, 1) both; }
        .deck-rise > *:nth-child(1) { animation-delay: 80ms; }
        .deck-rise > *:nth-child(2) { animation-delay: 180ms; }
        .deck-rise > *:nth-child(3) { animation-delay: 280ms; }
        .deck-rise > *:nth-child(4) { animation-delay: 380ms; }
        .deck-rise > *:nth-child(5) { animation-delay: 480ms; }
      `}</style>
    </div>
  );
}

// ---- Slide types --------------------------------------------------------

function HeroSlide({ slide }) {
  return (
    <div className="text-center deck-rise" data-testid={`slide-${slide.id}`}>
      <div className="text-[11px] tracking-[0.2em] uppercase sp-subtle">{slide.eyebrow}</div>
      <h1 className="font-instrument-serif text-5xl sm:text-6xl lg:text-7xl leading-[1.05] tracking-tight mt-4 whitespace-pre-line"
          style={{ color: "var(--fg)" }}>
        {slide.title.split("\n").map((line, i) => (
          <div key={i}>
            {i === 0
              ? line
              : <span style={{ background: "linear-gradient(90deg, #f64000, #ff8e3a)", WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent" }}>{line}</span>}
          </div>
        ))}
      </h1>
      <p className="mt-6 text-base sm:text-lg max-w-2xl mx-auto whitespace-pre-line"
         style={{ color: "var(--fg-soft)" }}>
        {slide.subtitle}
      </p>
      <div className="mt-10 inline-flex items-center gap-3 px-4 py-2 rounded-full text-xs sp-subtle sp-glass">
        <span className="w-1.5 h-1.5 rounded-full bg-orange-500" />
        {slide.cta}
      </div>
    </div>
  );
}

function FeatureSlide({ slide }) {
  const Icon = slide.icon || Wand2;
  return (
    <div className="grid lg:grid-cols-12 gap-8 lg:gap-12 items-center" data-testid={`slide-${slide.id}`}>
      <div className="lg:col-span-5 deck-rise">
        <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full text-[10px] uppercase tracking-widest sp-glass"
             style={{ color: "var(--fg-soft)" }}>
          <Icon className="w-3.5 h-3.5 text-orange-500" />
          {slide.eyebrow}
        </div>
        <h2 className="font-instrument-serif text-4xl sm:text-5xl leading-[1.05] tracking-tight mt-5 whitespace-pre-line"
            style={{ color: "var(--fg)" }}>
          {slide.title}
        </h2>
        <div className="mt-5 space-y-3">
          {slide.body?.map((p, i) => (
            <p key={i} className="text-sm sm:text-base leading-relaxed" style={{ color: "var(--fg-soft)" }}>
              {p}
            </p>
          ))}
        </div>
        {slide.bullets?.length > 0 && (
          <ul className="mt-6 space-y-2.5">
            {slide.bullets.map((b, i) => (
              <li key={i} className="flex items-start gap-3 text-sm" style={{ color: "var(--fg)" }}>
                <span className="mt-1.5 inline-block w-1.5 h-1.5 rounded-full shrink-0"
                      style={{ background: "linear-gradient(135deg, #f64000, #ff8e3a)" }} />
                <span>{b}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="lg:col-span-7 deck-rise">
        <FrameImage src={slide.image} caption={slide.imageCaption} />
      </div>
    </div>
  );
}

function CtaSlide({ slide }) {
  return (
    <div className="text-center deck-rise" data-testid={`slide-${slide.id}`}>
      <div className="text-[11px] tracking-[0.2em] uppercase sp-subtle">{slide.eyebrow}</div>
      <h2 className="font-instrument-serif text-5xl sm:text-6xl lg:text-7xl leading-[1.05] tracking-tight mt-4 whitespace-pre-line"
          style={{ color: "var(--fg)" }}>
        {slide.title}
      </h2>
      <p className="mt-6 text-base sm:text-lg max-w-2xl mx-auto"
         style={{ color: "var(--fg-soft)" }}>
        {slide.subtitle}
      </p>
      <div className="mt-10 flex flex-wrap items-center justify-center gap-3">
        <Link
          to={slide.primaryHref}
          className="sp-btn sp-btn-primary px-6 py-3 text-sm"
          data-testid="deck-cta-primary"
        >
          <Rocket className="w-4 h-4" />
          {slide.primaryLabel}
        </Link>
        <Link
          to={slide.secondaryHref}
          className="sp-btn sp-btn-ghost px-5 py-3 text-sm"
          data-testid="deck-cta-secondary"
        >
          <MessageCircle className="w-4 h-4" />
          {slide.secondaryLabel}
        </Link>
      </div>
      <div className="mt-12 inline-flex items-center gap-3 px-4 py-2 rounded-full text-xs sp-subtle">
        <span>Press</span>
        <kbd className="px-1.5 py-0.5 rounded text-[10px]"
             style={{ background: "color-mix(in srgb, var(--fg) 10%, transparent)" }}>F</kbd>
        <span>for fullscreen · </span>
        <kbd className="px-1.5 py-0.5 rounded text-[10px]"
             style={{ background: "color-mix(in srgb, var(--fg) 10%, transparent)" }}>← →</kbd>
        <span>to navigate</span>
      </div>
    </div>
  );
}

function FrameImage({ src, caption }) {
  // Glassy "browser frame" effect — matte border, subtle shadow stack,
  // and a 3-dot title bar so the screenshot reads as "real product"
  // even at a glance.
  return (
    <figure className="relative">
      <div
        className="rounded-2xl overflow-hidden"
        style={{
          background: "color-mix(in srgb, var(--fg) 6%, transparent)",
          border: "1px solid color-mix(in srgb, var(--fg) 14%, transparent)",
          boxShadow:
            "0 1px 0 0 color-mix(in srgb, var(--fg) 8%, transparent) inset, " +
            "0 30px 60px -20px rgba(0,0,0,0.45), " +
            "0 12px 30px -10px rgba(246,64,0,0.25)",
        }}
      >
        <div className="flex items-center gap-1.5 px-3 py-2"
             style={{ background: "color-mix(in srgb, var(--fg) 8%, transparent)",
                      borderBottom: "1px solid color-mix(in srgb, var(--fg) 10%, transparent)" }}>
          <span className="w-2.5 h-2.5 rounded-full" style={{ background: "#ff5f57" }} />
          <span className="w-2.5 h-2.5 rounded-full" style={{ background: "#febc2e" }} />
          <span className="w-2.5 h-2.5 rounded-full" style={{ background: "#28c840" }} />
        </div>
        <img
          src={src}
          alt={caption || "Product screenshot"}
          className="block w-full h-auto"
          loading="eager"
          decoding="async"
          data-testid="deck-screenshot"
        />
      </div>
      {caption && (
        <figcaption className="text-xs sp-subtle mt-3 text-center">
          {caption}
        </figcaption>
      )}
    </figure>
  );
}
