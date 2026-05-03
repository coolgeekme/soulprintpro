import React, { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";

/**
 * WorkingIndicator — a friendlier "we're on it" affordance for any
 * long-running async task (image gen, video gen, PDF/artifact build,
 * Soul re-synthesis, social-style synth, etc.).
 *
 * Renders a soft glass card with:
 *   • A spinning glyph
 *   • A primary `label` (e.g. "Generating cover image")
 *   • An escalating helper hint sourced from `hints[]` that advances
 *     based on how long the task has been running. This keeps users
 *     calm during the inevitable 20-60s waits that hit AI services and
 *     prevents the "is this thing broken?" moment.
 *   • A live mm:ss timer so users can see motion at all times.
 *
 * Hint advancement schedule (tuned for typical LLM/image-gen jobs):
 *   t < 4s    → hints[0]   (acknowledge)
 *   t < 12s   → hints[1]   (reassure mid-flight)
 *   t < 25s   → hints[2]   (manage expectations)
 *   t ≥ 25s   → hints[hints.length - 1] (persistent "almost there")
 *
 * Usage:
 *   <WorkingIndicator label="Generating PDF"
 *     hints={[
 *       "Drafting the document…",
 *       "Formatting tables and applying brand styling…",
 *       "Almost there — embedding artifacts and checking links.",
 *     ]} />
 */
export default function WorkingIndicator({
  label = "Working on it",
  hints = [
    "Hang tight — this can take a few seconds.",
    "Still going — complex requests sometimes need a minute.",
    "Almost there — final passes can take a little longer.",
  ],
  compact = false,
  testId = "working-indicator",
}) {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  // Pick which hint to show based on elapsed seconds. Always within bounds.
  const idx = tick < 4 ? 0 : tick < 12 ? 1 : tick < 25 ? 2 : (hints.length - 1);
  const hint = hints[Math.min(idx, hints.length - 1)] || "";
  const mm = String(Math.floor(tick / 60)).padStart(1, "0");
  const ss = String(tick % 60).padStart(2, "0");

  if (compact) {
    return (
      <span
        className="inline-flex items-center gap-2 text-xs sp-subtle"
        data-testid={testId}
        role="status"
        aria-live="polite"
      >
        <Loader2 className="w-3.5 h-3.5 animate-spin" />
        <span>{label}</span>
        <span className="opacity-60">· {mm}:{ss}</span>
      </span>
    );
  }

  return (
    <div
      className="sp-glass rounded-xl px-3.5 py-2.5 mt-2 flex items-center gap-3"
      data-testid={testId}
      role="status"
      aria-live="polite"
    >
      <div className="relative shrink-0">
        <Loader2 className="w-4 h-4 animate-spin" style={{ color: "var(--fg)" }} />
        {/* Pulsing aurora behind the spinner — adds visible motion at the
            edge of the user's eye even if they don't focus on it. */}
        <span
          aria-hidden
          className="absolute inset-0 rounded-full animate-ping"
          style={{
            background: "radial-gradient(circle, rgba(246,64,0,0.55) 0%, transparent 70%)",
            animationDuration: "2.4s",
          }}
        />
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium leading-tight" style={{ color: "var(--fg)" }}>
          {label}
          <span className="ml-2 text-xs sp-subtle font-normal tabular-nums">{mm}:{ss}</span>
        </div>
        {hint && (
          <div className="text-xs sp-subtle mt-0.5 leading-snug">{hint}</div>
        )}
      </div>
      <div className="sp-typing shrink-0" aria-hidden>
        <span /><span /><span />
      </div>
    </div>
  );
}
