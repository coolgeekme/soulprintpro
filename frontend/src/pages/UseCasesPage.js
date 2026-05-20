import React from "react";
import { Link } from "react-router-dom";
import Logo from "../components/Logo";
import {
  ArrowRight, ArrowLeft, Mail,
  Megaphone, FileText, Search, ImageIcon, Smartphone,
  Check, X, Sparkles,
} from "lucide-react";

/**
 * /use-cases — public, no-auth marketing page showcasing the 5 things
 * the platform can ACTUALLY deliver for a small-business buyer.
 *
 * Design intent: prospects landing here from a sales email or Google
 * search need to see WORKED EXAMPLES, not generic feature bullets. Each
 * use case spells out:
 *
 *   - what the user types into the platform (real prompt)
 *   - what the agent ships (concrete deliverable shape)
 *   - what the platform CAN'T do in that workflow (honest limits)
 *
 * Honesty is the wedge — competitor SaaS pages overclaim, we don't.
 * That's also the brand position from /trust.
 */

const USE_CASES = [
  {
    id: "social-autopilot",
    icon: Megaphone,
    accent: "rgba(251,146,60,1)",        // brand orange
    eyebrow: "Use case #1",
    title: "Run social media on autopilot — draft, schedule, auto-publish.",
    body: (
      <>
        The platform reads your company's Soul (mission, audience, tone — synthesized
        from your website) and your Style Guide (synthesized from your own profiles
        plus 2-3 competitors), then drafts platform-specific posts. Schedule any post
        for a future time and check "Auto-post via Mission" — a real Chromium browser
        opens at the scheduled time, fills the composer with your post, submits it,
        and stamps it as published.
      </>
    ),
    prompt: 'Draft 5 LinkedIn posts about our new spring collection, scheduled across the next 14 days at 10am.',
    output: (
      <>
        5 brand-consistent LinkedIn drafts in the Social Queue (each with a Nano Banana
        cover image), already scheduled. At each fire time, the post auto-publishes;
        the status pill flips to <b className="text-emerald-400">PUBLISHED</b> and the
        published URL is stored.
      </>
    ),
    canDo: [
      "Twitter/X, LinkedIn, Facebook, Threads, Bluesky, Mastodon, Reddit (direct autopost)",
      "Instagram, TikTok, YouTube (draft + one-click copy to publish manually)",
      "Inline image editing on cover images — change prompt, regenerate, override",
      "Same draft works across multiple platforms with per-platform tone tweaks",
    ],
    cannotDo: [
      "No OAuth-managed publishing yet — you upload a saved-login cookie per platform (Chrome extension export, takes ~60 seconds per account)",
      "No reply / DM monitoring — outbound publishing only",
    ],
  },
  {
    id: "real-documents",
    icon: FileText,
    accent: "rgba(168,85,247,1)",        // violet
    eyebrow: "Use case #2",
    title: "Real, polished business documents on demand — not chatty replies.",
    body: (
      <>
        Ask any of the 169 specialised agents for a brief, audit, report, proposal,
        SOP, meeting agenda, contract template, or memo. The system renders the
        agent's Markdown body to a real <code className="text-[12px] px-1 sp-glass rounded">.pdf</code>{" "}
        (Helvetica, proper headings, tables, lists, page footer) or{" "}
        <code className="text-[12px] px-1 sp-glass rounded">.docx</code>{" "}
        (native Heading + Bullet styles). Every artifact is SHA-256-anchored on
        Bitcoin via OpenTimestamps — tamper-evident the moment it lands in your
        library.
      </>
    ),
    prompt: 'Write me a 1-page strategy brief on the future of AI agents in B2B SaaS. Reference real sources.',
    output: (
      <>
        A valid <code className="text-[12px] px-1 sp-glass rounded">.pdf</code> file
        in your Documents library (e.g.{" "}
        <code className="text-[12px] px-1 sp-glass rounded">Future_of_AI_Agents_in_B2B_SaaS_Brief.pdf</code>),
        ~5-15KB, with at least 5 cited web sources, downloadable in one click. Bitcoin
        anchor proof becomes available within ~6 hours.
      </>
    ),
    canDo: [
      "PDF, DOCX, CSV, MD, ICS, VCF, EML, HTML, JSON, YAML, SQL, code files",
      "Auto-fetches URLs you mention in the prompt so claims are grounded in real content",
      "Forced-delivery retry: if an agent says 'I'll prepare this', the system retries to attach the file in the same turn",
      "Independently verifiable — third parties run `ots verify` on the proof, no cooperation needed",
    ],
    cannotDo: [
      "No human-quality copy QA loop — agency-grade polish still benefits from a human editor",
      "Long-form (50+ page) reports work but quality degrades vs a 1-5 page brief",
    ],
  },
  {
    id: "research-missions",
    icon: Search,
    accent: "rgba(74,222,128,1)",        // green
    eyebrow: "Use case #3",
    title: "Autonomous research missions — work that runs while you sleep.",
    body: (
      <>
        Type <code className="text-[12px] px-1 sp-glass rounded">/mission</code> or
        click the rocket icon and describe the goal. The agent runs a real loop:
        web search (Brave / Perplexity) → opens promising pages in a Playwright
        Chromium session → clicks, fills forms, takes screenshots → harvests
        structured data → ships finished artifacts. For lead lists, it queries free
        public APIs (SEC EDGAR, USAspending.gov, Wikipedia, OpenCorporates) directly.
      </>
    ),
    prompt: 'Build me a CSV of the top 50 manufacturing companies in Ohio with revenue, website, and a contact phone if public. Cite sources.',
    output: (
      <>
        A growing <code className="text-[12px] px-1 sp-glass rounded">.csv</code>{" "}
        with columns <i>Business Name, City, State, Revenue (USD), Website,
        Contact Phone, Source URL</i> — 25-50 verified rows, each row citing where
        it was sourced. You watch the mission card in chat as it harvests batches
        of rows from each source.
      </>
    ),
    canDo: [
      "Multi-step browsing: navigate, click, fill forms, submit — driven by an LLM that reads the page structure each step",
      "Free public-data scrapers wired in: SEC EDGAR, USAspending, Wikipedia, OpenCorporates",
      "Stalled-mission recovery: any tool exceeding 60s is aborted so the loop can pivot",
      "Cancel and delete missions at any time — full cleanup including artifacts",
    ],
    cannotDo: [
      "Login-walled SaaS (Salesforce, HubSpot Lists) — only if you upload a saved-login cookie for that domain",
      "CAPTCHAs aren't auto-solved (manual 'take browser control' handoff exists in Crucible)",
    ],
  },
  {
    id: "branded-images",
    icon: ImageIcon,
    accent: "rgba(244,114,182,1)",       // pink
    eyebrow: "Use case #4",
    title: "Produce + iterate on branded images — without a designer.",
    body: (
      <>
        Ask in plain English. Nano Banana renders text-to-image using your brand colors
        and the Style Guide's <code className="text-[12px] px-1 sp-glass rounded">image_video_style</code>{" "}
        field. Upload any photo and the same engine flips into image-to-image edit
        mode — remove backgrounds, add a logo, change palettes, restyle, recompose.
        Click <i>Edit prompt</i> on any image to regenerate with a refined direction.
      </>
    ),
    prompt: 'Make me 4 different Instagram-square hero images for our new pistachio gift box. On-brand, warm lighting.',
    output: (
      <>
        4 PNG variants posted as 4 separate agent messages, each with a download
        button, A/B-comparable. Each persists its prompt so you can iterate with
        "darker palette" or "swap to a marble counter background" without re-typing.
      </>
    ),
    canDo: [
      "Text-to-image AND image-to-image (upload a photo, ask for edits)",
      "Bulk count: 'make me 5 variations' renders 5 distinct images (capped at 8)",
      "Aspect-ratio aware: 'Instagram post' → 1:1, 'TikTok' → 9:16, 'YouTube' → 16:9",
      "Model picker: Nano Banana (default, fast) or GPT Image 1 (crisper text)",
    ],
    cannotDo: [
      "Precise long brand-name typography (long words sometimes garble — pick a short variant or use GPT Image 1)",
      "No vector / SVG output — raster only",
      "No multi-scene video composition (single-scene Veo 3.1 clips work, multi-cut sequencing doesn't)",
    ],
  },
  {
    id: "telegram-cos",
    icon: Smartphone,
    accent: "rgba(56,189,248,1)",        // sky
    eyebrow: "Use case #5",
    title: "Field tasks from your phone — voice or text via Telegram.",
    body: (
      <>
        Connect once. Then send your Chief-of-Staff bot a voice note or text from
        anywhere. Whisper transcribes voice; the message is routed by a 4-layer
        cascade (slash command → inline org-name fuzzy match → 30-min sticky context
        → numbered disambiguation) into the right org's CoS thread. The same
        conversation is visible on web — pick it up at your desk later.
      </>
    ),
    prompt: '🎙️ "Reggie Lab — schedule a check-in with Sarah next Tuesday and draft a 3-bullet status email to the team."',
    output: (
      <>
        Whisper transcribes the voice note (you see <i>"🎙️ Heard: …"</i>), the CoS
        routes it to your Reggie Lab org, schedules the calendar invite via Composio
        Google Calendar, drafts the email as a queued artifact, and pings you with a
        confirmation — all from your phone.
      </>
    ),
    canDo: [
      "Voice (OGG/Opus via Whisper) and text — same routing",
      "4-layer org routing so a single Telegram chat serves all your orgs without confusion",
      "Same Composio integrations available on web (Gmail, Slack, HubSpot, Calendar, etc.) flow through automatically",
      "Conversations sync back to web — start on phone, finish on laptop",
    ],
    cannotDo: [
      "Phase 4 (smart proactive nudges based on calendar / schedule events) is only partially live — org-level reminders work, per-user smart triggers don't",
      "No phone calls — text + voice notes only",
    ],
  },
];

const NOT_TODAY = [
  "Take a phone call (no inbound/outbound voice infrastructure)",
  "Mass email blasts to a customer list (agents can draft, you click send)",
  "Accept payments or send invoices end-to-end (no Stripe billing for end-customer flows yet)",
  "Deploy a marketing website (agents can draft copy + wireframes, but no hosting integration)",
  "Live customer chat widget on your website (not yet shippable as an embed)",
  "Read your QuickBooks / Xero accounting data (no Composio toolkit wired for these yet)",
  "Multi-scene video production (single-scene clips work; cuts + transitions don't)",
];


function Limit({ icon: Icon, text, tone }) {
  const isCan = tone === "can";
  return (
    <li className="flex items-start gap-2 text-sm leading-relaxed">
      <Icon className={`w-4 h-4 mt-0.5 shrink-0 ${isCan ? "text-emerald-400" : "text-rose-400"}`} />
      <span className="sp-subtle">{text}</span>
    </li>
  );
}


function UseCaseCard({ uc, idx }) {
  const Icon = uc.icon;
  return (
    <section
      id={uc.id}
      data-testid={`use-case-${uc.id}`}
      className="sp-glass-strong rounded-2xl p-6 sm:p-8 mb-5"
      style={{
        borderLeft: `3px solid ${uc.accent}`,
      }}
    >
      <div className="flex items-start gap-3 mb-4">
        <div
          className="w-10 h-10 rounded-xl flex items-center justify-center shrink-0"
          style={{ background: `${uc.accent}1f` }}
        >
          <Icon className="w-5 h-5" style={{ color: uc.accent }} />
        </div>
        <div className="min-w-0">
          <div className="text-[10px] uppercase tracking-widest sp-subtle mb-0.5">
            {uc.eyebrow}
          </div>
          <h2 className="text-xl sm:text-2xl font-medium tracking-tight leading-snug">
            {uc.title}
          </h2>
        </div>
      </div>
      <p className="text-sm sm:text-base sp-subtle leading-relaxed mb-5">
        {uc.body}
      </p>

      {/* Prompt → output */}
      <div className="grid sm:grid-cols-2 gap-3 mb-5">
        <div className="sp-glass rounded-xl p-4">
          <div className="text-[10px] uppercase tracking-widest sp-subtle mb-1.5">
            You type
          </div>
          <code className="block text-[13px] leading-relaxed">
            {uc.prompt}
          </code>
        </div>
        <div className="sp-glass rounded-xl p-4">
          <div className="text-[10px] uppercase tracking-widest sp-subtle mb-1.5">
            You get
          </div>
          <div className="text-[13px] leading-relaxed sp-subtle">
            {uc.output}
          </div>
        </div>
      </div>

      {/* Can do / can't do */}
      <div className="grid sm:grid-cols-2 gap-4">
        <div>
          <div className="text-[10px] uppercase tracking-widest text-emerald-400/90 mb-2">
            Works today
          </div>
          <ul className="space-y-1.5">
            {uc.canDo.map((t, i) => <Limit key={i} icon={Check} text={t} tone="can" />)}
          </ul>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-widest text-rose-400/90 mb-2">
            Honest limits
          </div>
          <ul className="space-y-1.5">
            {uc.cannotDo.map((t, i) => <Limit key={i} icon={X} text={t} tone="cannot" />)}
          </ul>
        </div>
      </div>
    </section>
  );
}


export default function UseCasesPage() {
  return (
    <div className="min-h-screen" data-testid="use-cases-page">
      {/* Top bar */}
      <header
        className="border-b border-white/5 sticky top-0 z-30 backdrop-blur-md"
        style={{
          background: "color-mix(in srgb, var(--bg) 75%, transparent)",
          paddingTop: "calc(env(safe-area-inset-top, 0px))",
        }}
      >
        <div className="max-w-4xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between">
          <Link to="/" className="inline-flex items-center gap-2" data-testid="usecases-brand">
            <Logo variant="wordmark" className="h-6 w-auto" />
          </Link>
          <nav className="flex items-center gap-1">
            <Link to="/pricing" className="sp-btn sp-btn-ghost text-xs">Pricing</Link>
            <Link to="/trust" className="sp-btn sp-btn-ghost text-xs">Trust</Link>
            <Link to="/auth" className="sp-btn sp-btn-primary text-xs" data-testid="usecases-cta-auth">
              Get started <ArrowRight className="w-3 h-3" />
            </Link>
          </nav>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-4 sm:px-6 py-10 sm:py-14">
        {/* Hero */}
        <div className="mb-10">
          <div className="inline-flex items-center gap-1.5 sp-glass rounded-full px-3 py-1 text-[10px] uppercase tracking-widest sp-subtle mb-4">
            <Sparkles className="w-3 h-3" /> What it actually does
          </div>
          <h1 className="text-3xl sm:text-5xl font-medium tracking-tight leading-[1.05] mb-4">
            5 things AI agents will do for your business — today.
          </h1>
          <p className="text-base sm:text-lg sp-subtle leading-relaxed max-w-2xl">
            Other AI platforms show you a hero video and a logo wall. We'll show
            you exactly what to type, exactly what you get back, and exactly
            what we <i>can't</i> do yet. Every claim below is traced to live
            code shipping right now.
          </p>
        </div>

        {/* Quick jump nav */}
        <div className="flex flex-wrap gap-2 mb-8" data-testid="usecases-jumpnav">
          {USE_CASES.map((uc, i) => (
            <a
              key={uc.id}
              href={`#${uc.id}`}
              className="sp-glass rounded-full px-3 py-1 text-[11px] hover:bg-white/5 transition"
              style={{ borderLeft: `2px solid ${uc.accent}` }}
              data-testid={`usecases-jump-${uc.id}`}
            >
              {i + 1}. {uc.title.split(" — ")[0]}
            </a>
          ))}
        </div>

        {/* Use case cards */}
        {USE_CASES.map((uc, i) => (
          <UseCaseCard key={uc.id} uc={uc} idx={i} />
        ))}

        {/* What we don't do — honesty section */}
        <section
          className="rounded-2xl p-6 sm:p-8 mt-8 border"
          style={{
            background: "linear-gradient(135deg, rgba(244,63,94,0.08), rgba(244,63,94,0.02))",
            borderColor: "rgba(244,63,94,0.20)",
          }}
          data-testid="usecases-not-today"
        >
          <div className="text-[10px] uppercase tracking-widest text-rose-400/90 mb-2">
            Honest about what we DON'T do yet
          </div>
          <h2 className="text-xl sm:text-2xl font-medium tracking-tight mb-3">
            If a SaaS vendor told you they did all this stuff, they're lying. We won't.
          </h2>
          <p className="text-sm sm:text-base sp-subtle leading-relaxed mb-4">
            These are real asks we get from SMB owners — and the honest answer is
            "not yet." If you need any of these as a deal-breaker, it's better
            you know now than discover it 3 weeks into onboarding.
          </p>
          <ul className="space-y-1.5">
            {NOT_TODAY.map((t, i) => <Limit key={i} icon={X} text={t} tone="cannot" />)}
          </ul>
        </section>

        {/* CTA */}
        <div className="mt-10 pt-6 border-t border-white/5 grid sm:grid-cols-2 gap-3">
          <Link
            to="/auth?mode=register"
            className="sp-btn sp-btn-primary text-sm justify-center sm:justify-start"
            data-testid="usecases-cta-register"
          >
            Try it on your business — free <ArrowRight className="w-4 h-4" />
          </Link>
          <a
            href="mailto:team@archeforge.com?subject=Use-case%20fit%20question"
            className="sp-btn sp-btn-ghost text-sm justify-center sm:justify-start"
            data-testid="usecases-cta-contact"
          >
            <Mail className="w-4 h-4" /> Ask if your workflow fits
          </a>
        </div>

        {/* Footer */}
        <div className="mt-8 text-xs sp-subtle">
          <Link to="/" className="hover:text-white/85 inline-flex items-center gap-1">
            <ArrowLeft className="w-3 h-3" /> Back home
          </Link>
        </div>
      </main>
    </div>
  );
}
