import React from "react";
import { Link } from "react-router-dom";
import Logo from "../components/Logo";
import {
  ArrowLeft, ArrowRight, Mail,
  Calendar, Sparkles, CheckCircle2, AlertTriangle, MessageSquare,
  Plug, Layers, Send,
} from "lucide-react";

/**
 * /docs/social-queue — public, no-auth walkthrough that an org owner
 * can read (or share with their team) to learn the schedule +
 * auto-publish flow without a sales call. Every claim mirrors what
 * actually ships in the SocialPage + QuickSchedulePopover + the
 * APScheduler social_autopublish job.
 *
 * Design intent: scannable. Heavy on numbered steps, light on prose.
 * Cards have one job. Honest limits section so prospects don't
 * onboard with the wrong expectations.
 */

const PREREQS = [
  {
    icon: Plug,
    title: "Connect at least one platform",
    body: (
      <>
        Org page → <b className="text-white/85">Connections</b> tab → click{" "}
        <b className="text-white/85">+ Add</b> on Twitter, LinkedIn, Facebook,
        Reddit, Threads, Bluesky, or Mastodon. Complete OAuth; wait for the
        row to flip green ✓. For Facebook/Instagram, also click{" "}
        <b className="text-white/85">Change target</b> to pin which Page +
        linked IG account to post to.
      </>
    ),
  },
  {
    icon: Sparkles,
    title: "Fill in your Soul + Style Guide",
    body: (
      <>
        Org page → <b className="text-white/85">Brand</b> panel. The agent
        uses these to draft on-brand copy and brand-consistent cover images.
        Without them, drafts will be generic and you'll spend time editing.
      </>
    ),
  },
];

const STEPS = [
  {
    n: 1,
    title: "Open Social Queue",
    body: (
      <>
        From your org page, click the <b className="text-white/85">Social Queue</b>{" "}
        tab. You'll see four sub-tabs: <i>Drafts</i>, <i>Scheduled</i>,{" "}
        <i>Published</i>, <i>Failed</i>.
      </>
    ),
  },
  {
    n: 2,
    title: "Get a draft into the queue",
    body: (
      <>
        Two ways:
        <ul className="mt-2 space-y-1 pl-4 list-disc">
          <li>
            <b className="text-white/85">+ New post</b> button — write text,
            pick platforms, save.
          </li>
          <li>
            <b className="text-white/85">Ask the agent</b> in chat:
            <code className="block mt-1 px-2 py-1.5 sp-glass rounded text-[12px]">
              Draft 5 LinkedIn posts about our spring collection.
            </code>
          </li>
        </ul>
        Drafts arrive in the queue with cover images already generated.
      </>
    ),
  },
  {
    n: 3,
    title: "Click the orange Schedule button",
    body: (
      <>
        In the draft card's action row. (Becomes <i>Reschedule</i> if a
        time was already set.) The Schedule popover opens.
      </>
    ),
  },
  {
    n: 4,
    title: "Pick a time",
    body: (
      <>
        Either:
        <ul className="mt-2 space-y-1 pl-4 list-disc">
          <li>
            <b className="text-white/85">Smart preset</b> — In 1 hour ·
            Tomorrow 9am / 1pm / 6pm · Next Monday 10am.
          </li>
          <li>
            <b className="text-white/85">Custom datetime</b> — picker at the
            bottom of the popover.
          </li>
        </ul>
      </>
    ),
  },
  {
    n: 5,
    title: "Confirm Auto-publish is checked",
    body: (
      <>
        It's <b className="text-white/85">on by default</b>. Leave it on if
        you want Foundry to fire the live publish at the scheduled time.
        Uncheck if you'd rather get a "ready to publish" prompt and click
        Publish yourself.
      </>
    ),
  },
  {
    n: 6,
    title: "Click Schedule",
    body: (
      <>
        Done. The post moves to <b className="text-white/85">Scheduled</b>{" "}
        with a clock-icon pill showing the fire time. Foundry's
        auto-publish job (runs every 60s) takes over from here.
      </>
    ),
  },
  {
    n: 7,
    title: "Verify after publish",
    body: (
      <>
        When the scheduled time passes:
        <ul className="mt-2 space-y-1 pl-4 list-disc">
          <li>The card flips to <b className="text-white/85">Published</b>.</li>
          <li>
            Each platform chip becomes a clickable green link —{" "}
            <code className="px-1 sp-glass rounded text-[11px]">linkedin → View</code>,{" "}
            <code className="px-1 sp-glass rounded text-[11px]">twitter → View</code>.
            Click any chip to open the actual published post on that
            platform.
          </li>
          <li>
            If a chip is amber/red, click for a detailed error +
            suggested next step.
          </li>
        </ul>
      </>
    ),
  },
];

const BULK_FLOW = [
  "Ask CoS: \"Generate 12 LinkedIn drafts about [topic] — 4 product, 4 lifestyle, 4 behind-the-scenes.\"",
  "Agent runs a Mission and drops 12 finished drafts into Social Queue.",
  "Schedule each one with a preset or custom time. (Bulk-multi-select shipping in a later release.)",
];

const GOTCHAS = [
  {
    when: "Schedule button missing on a card",
    why: "Post is already Published or Publishing.",
    fix: "Click Move back to drafts first.",
  },
  {
    when: "Post stays in Scheduled forever",
    why: "Auto-publish ticker not running on this deployment.",
    fix: "Contact your admin — the social_autopublish APScheduler job needs to be live.",
  },
  {
    when: "LinkedIn says success but you can't find the post",
    why: "Connected to a different LinkedIn identity than you expected.",
    fix: "Click the green linkedin → View chip to see which account it went to. If wrong account, disconnect → revoke at linkedin.com/psettings/permissions → reconnect with the right identity.",
  },
  {
    when: "Facebook / Instagram won't finish connecting",
    why: "Composio's managed Meta app scope approval.",
    fix: "Revoke Composio in Meta Business Settings → Admin → Tools → Wipe Meta connections → re-authorize → reconnect from a clean state.",
  },
  {
    when: "Instagram or TikTok stays in draft after a publish",
    why: "Those platforms don't expose a stable publishing API — Foundry only drafts them.",
    fix: "Use Copy text + Download media on the card, then paste into the IG/TikTok mobile app.",
  },
];

export default function DocsSocialQueuePage() {
  return (
    <div className="min-h-screen" data-testid="docs-social-queue-page">
      {/* Top bar */}
      <header
        className="border-b border-white/5 sticky top-0 z-30 backdrop-blur-md"
        style={{
          background: "color-mix(in srgb, var(--bg) 75%, transparent)",
          paddingTop: "calc(env(safe-area-inset-top, 0px))",
        }}
      >
        <div className="max-w-3xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between">
          <Link to="/" className="inline-flex items-center gap-2">
            <Logo variant="wordmark" className="h-6 w-auto" />
          </Link>
          <nav className="flex items-center gap-1">
            <Link to="/use-cases" className="sp-btn sp-btn-ghost text-xs">Use cases</Link>
            <Link to="/pricing" className="sp-btn sp-btn-ghost text-xs">Pricing</Link>
            <Link to="/auth" className="sp-btn sp-btn-primary text-xs">
              Open app <ArrowRight className="w-3 h-3" />
            </Link>
          </nav>
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-4 sm:px-6 py-10 sm:py-14">
        {/* Hero */}
        <div className="mb-8">
          <div className="inline-flex items-center gap-1.5 sp-glass rounded-full px-3 py-1 text-[10px] uppercase tracking-widest sp-subtle mb-4">
            <Calendar className="w-3 h-3" /> Docs · Social Queue
          </div>
          <h1 className="text-3xl sm:text-5xl font-medium tracking-tight leading-[1.05] mb-4">
            Schedule and auto-post social media.
          </h1>
          <p className="text-base sm:text-lg sp-subtle leading-relaxed">
            A 2-click flow to schedule a post. A 5-minute flow to schedule a
            month of content. Foundry publishes at the scheduled time —
            verified on each platform with a clickable View link.
          </p>
        </div>

        {/* Prerequisites */}
        <section className="mb-8">
          <h2 className="text-base font-medium tracking-tight mb-3 text-white/85">
            Before you start
          </h2>
          <div className="grid sm:grid-cols-2 gap-3">
            {PREREQS.map((p, i) => {
              const Icon = p.icon;
              return (
                <div key={i} className="sp-glass-strong rounded-2xl p-4"
                     data-testid={`docs-prereq-${i}`}>
                  <div className="flex items-center gap-2 mb-2">
                    <Icon className="w-4 h-4 text-orange-300" />
                    <div className="text-sm font-medium">{p.title}</div>
                  </div>
                  <div className="text-xs sp-subtle leading-relaxed">{p.body}</div>
                </div>
              );
            })}
          </div>
        </section>

        {/* Main 7-step flow */}
        <section className="mb-10">
          <h2 className="text-base font-medium tracking-tight mb-3 text-white/85 flex items-center gap-2">
            <Send className="w-4 h-4 text-orange-300" />
            The 7 steps
          </h2>
          <ol className="space-y-2">
            {STEPS.map((s) => (
              <li key={s.n}
                  className="sp-glass-strong rounded-2xl p-4 sm:p-5 flex gap-3 sm:gap-4"
                  data-testid={`docs-step-${s.n}`}>
                <div className="flex-shrink-0 w-7 h-7 rounded-full bg-orange-500/15 border border-orange-500/30 flex items-center justify-center text-orange-300 text-xs font-medium">
                  {s.n}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium mb-1.5">{s.title}</div>
                  <div className="text-xs sm:text-sm sp-subtle leading-relaxed">{s.body}</div>
                </div>
              </li>
            ))}
          </ol>
        </section>

        {/* Bulk flow */}
        <section className="mb-10">
          <div
            className="rounded-2xl p-5 sm:p-6 border"
            style={{
              background: "linear-gradient(135deg, rgba(168,85,247,0.10), rgba(168,85,247,0.02))",
              borderColor: "rgba(168,85,247,0.25)",
            }}
            data-testid="docs-bulk-flow"
          >
            <div className="flex items-center gap-2 mb-2">
              <Layers className="w-4 h-4 text-violet-300" />
              <h2 className="text-base font-medium">Power move: a month of content in 5 minutes</h2>
            </div>
            <ol className="space-y-1.5 mt-3">
              {BULK_FLOW.map((line, i) => (
                <li key={i} className="text-sm sp-subtle flex gap-2 leading-relaxed">
                  <span className="text-violet-300 font-medium tabular-nums">{i + 1}.</span>
                  <span>{line}</span>
                </li>
              ))}
            </ol>
          </div>
        </section>

        {/* From chat / Telegram */}
        <section className="mb-10">
          <div className="sp-glass-strong rounded-2xl p-5">
            <div className="flex items-center gap-2 mb-2">
              <MessageSquare className="w-4 h-4 text-orange-300" />
              <h2 className="text-base font-medium">Or just ask in chat / Telegram</h2>
            </div>
            <p className="text-xs sm:text-sm sp-subtle leading-relaxed mb-3">
              Once Telegram is connected (Account → Connect Telegram), you can
              schedule from your phone:
            </p>
            <code className="block px-3 py-2 sp-glass rounded text-[12px] leading-relaxed">
              "Schedule a LinkedIn post about our spring drop for tomorrow at 9am. Auto-publish it."
            </code>
            <p className="text-[11px] sp-subtle leading-relaxed mt-3">
              The Chief of Staff agent drafts the post, schedules it, enables
              auto-publish, and confirms — all in one turn.
            </p>
          </div>
        </section>

        {/* What "verify" looks like — success indicators */}
        <section className="mb-10">
          <h2 className="text-base font-medium tracking-tight mb-3 text-white/85 flex items-center gap-2">
            <CheckCircle2 className="w-4 h-4 text-emerald-400" />
            How to know it worked
          </h2>
          <ul className="space-y-2 text-sm sp-subtle leading-relaxed">
            <li className="flex gap-2">
              <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400 mt-0.5 flex-shrink-0" />
              Card moves from <b className="text-white/85 mx-1">Scheduled</b> to <b className="text-white/85 mx-1">Published</b>.
            </li>
            <li className="flex gap-2">
              <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400 mt-0.5 flex-shrink-0" />
              Each platform chip becomes a clickable green "→ View" link.
            </li>
            <li className="flex gap-2">
              <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400 mt-0.5 flex-shrink-0" />
              Click any chip → opens the actual published post on that platform, in the account you connected.
            </li>
            <li className="flex gap-2">
              <AlertTriangle className="w-3.5 h-3.5 text-amber-400 mt-0.5 flex-shrink-0" />
              Amber chip = "succeeded but no URN" warning — Composio reported success but couldn't return a post URN. Click for raw response.
            </li>
            <li className="flex gap-2">
              <AlertTriangle className="w-3.5 h-3.5 text-rose-400 mt-0.5 flex-shrink-0" />
              Red chip = failure. Click for the error + which Composio tool was called.
            </li>
          </ul>
        </section>

        {/* Common gotchas */}
        <section className="mb-10">
          <h2 className="text-base font-medium tracking-tight mb-3 text-white/85 flex items-center gap-2">
            <AlertTriangle className="w-4 h-4 text-amber-400" />
            Common gotchas
          </h2>
          <div className="rounded-2xl overflow-hidden border border-white/5">
            <table className="w-full text-xs sm:text-sm">
              <thead>
                <tr className="bg-white/5 sp-subtle">
                  <th className="text-left px-3 py-2 font-normal">Symptom</th>
                  <th className="text-left px-3 py-2 font-normal hidden sm:table-cell">Cause</th>
                  <th className="text-left px-3 py-2 font-normal">Fix</th>
                </tr>
              </thead>
              <tbody>
                {GOTCHAS.map((g, i) => (
                  <tr key={i} className="border-t border-white/5 align-top">
                    <td className="px-3 py-2 text-white/85">{g.when}</td>
                    <td className="px-3 py-2 sp-subtle hidden sm:table-cell">{g.why}</td>
                    <td className="px-3 py-2 sp-subtle">
                      <span className="sm:hidden text-[10px] uppercase tracking-widest opacity-60">Cause:</span>{" "}
                      <span className="sm:hidden block mb-1">{g.why}</span>
                      <span className="sm:hidden text-[10px] uppercase tracking-widest opacity-60">Fix:</span>{" "}
                      {g.fix}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        {/* CTA */}
        <div className="grid sm:grid-cols-2 gap-3 mt-10">
          <Link to="/auth?mode=login" className="sp-btn sp-btn-primary text-sm justify-center sm:justify-start"
                data-testid="docs-cta-login">
            Open Social Queue <ArrowRight className="w-4 h-4" />
          </Link>
          <a href="mailto:team@archeforge.com?subject=Social%20Queue%20question"
             className="sp-btn sp-btn-ghost text-sm justify-center sm:justify-start"
             data-testid="docs-cta-contact">
            <Mail className="w-4 h-4" /> Ask a question
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
