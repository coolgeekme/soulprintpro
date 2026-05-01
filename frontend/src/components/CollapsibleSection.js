import React, { useEffect, useState } from "react";
import { Settings, ChevronDown, ChevronUp } from "lucide-react";

/**
 * CollapsibleSection — renders children with a click-to-expand header. State
 * is persisted in localStorage under `sp-collapse:{storageKey}` so the
 * user's choice survives reloads.
 *
 * Props:
 *   - title: section label
 *   - subtitle: optional one-liner
 *   - storageKey: unique key for persistence (often includes orgId)
 *   - defaultOpen: fallback when no stored preference exists
 *   - badge: optional right-aligned chip (e.g. count of unconfigured items)
 */
export default function CollapsibleSection({
  title, subtitle, storageKey, defaultOpen = false, badge, children,
  testId = "collapsible-section",
}) {
  const fullKey = `sp-collapse:${storageKey || title}`;
  const [open, setOpen] = useState(() => {
    try {
      const v = window.localStorage.getItem(fullKey);
      if (v === "1") return true;
      if (v === "0") return false;
    } catch { /* ignore */ }
    return defaultOpen;
  });

  useEffect(() => {
    try { window.localStorage.setItem(fullKey, open ? "1" : "0"); }
    catch { /* ignore */ }
  }, [open, fullKey]);

  return (
    <section className="mb-8" data-testid={testId}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full sp-glass rounded-2xl px-5 py-4 flex items-center gap-3 hover:border-white/25 transition"
        aria-expanded={open}
        data-testid={`${testId}-toggle`}
      >
        <div className="w-9 h-9 rounded-xl sp-glass-strong flex items-center justify-center shrink-0">
          <Settings className="w-4 h-4" />
        </div>
        <div className="min-w-0 flex-1 text-left">
          <div className="text-white font-medium">{title}</div>
          {subtitle && <div className="text-xs sp-subtle truncate">{subtitle}</div>}
        </div>
        {badge && (
          <span className="sp-chip text-[10px]" data-testid={`${testId}-badge`}>
            {badge}
          </span>
        )}
        {open
          ? <ChevronUp className="w-4 h-4 text-white/60 shrink-0" />
          : <ChevronDown className="w-4 h-4 text-white/60 shrink-0" />}
      </button>
      {open && (
        <div className="pt-4" data-testid={`${testId}-content`}>
          {children}
        </div>
      )}
    </section>
  );
}
