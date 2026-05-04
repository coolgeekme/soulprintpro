/**
 * DynamicIntelligencePicker — the prominent chip + dropdown that lets a
 * user either run on "Dynamic Intelligence" (the SDK auto-routes their
 * text to the best image/video/music model) or lock a specific default
 * model so every media request goes there.
 *
 * Persists the choice to localStorage under `sp:routing-mode`. Three
 * legal values:
 *   "auto"          → SDK picks the model per-request (recommended)
 *   "off"           → never auto-route; everything is plain chat
 *   "<model-key>"   → lock to a specific model (e.g. "veo3" or "suno")
 *
 * Exposes a hook (`useRoutingMode`) so ConversationPage and any future
 * surface (settings page, missions composer) can read/write the choice
 * with a single import.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import { Cpu, Sparkles, Image as ImageIcon, Film, Music, ChevronDown, Check, X } from "lucide-react";
import api from "../lib/api";

// localStorage key — one source of truth for the whole app.
const LS_KEY = "sp:routing-mode";

/** Read the persisted mode (defaults to "auto"). */
export function readRoutingMode() {
  try {
    const v = localStorage.getItem(LS_KEY);
    return v || "auto";
  } catch (_) {
    return "auto";
  }
}

/** Write the routing mode + emit a window event so other tabs / mounted
 * pickers stay in sync without a global state lib. */
export function writeRoutingMode(value) {
  try {
    localStorage.setItem(LS_KEY, value);
    window.dispatchEvent(new CustomEvent("sp:routing-mode-change", { detail: value }));
  } catch (_) { /* best-effort */ }
}

/** Reactive hook for components that want to render based on the mode. */
export function useRoutingMode() {
  const [mode, setMode] = useState(() => readRoutingMode());
  useEffect(() => {
    const onChange = (e) => setMode(e.detail || readRoutingMode());
    const onStorage = (e) => { if (e.key === LS_KEY) setMode(e.newValue || "auto"); };
    window.addEventListener("sp:routing-mode-change", onChange);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener("sp:routing-mode-change", onChange);
      window.removeEventListener("storage", onStorage);
    };
  }, []);
  return [mode, writeRoutingMode];
}


// Glyph used in the dropdown for each media type.
const TYPE_ICON = { image: ImageIcon, video: Film, music: Music };
const TYPE_ORDER = ["image", "video", "music"];


export default function DynamicIntelligencePicker() {
  const [mode, setMode] = useRoutingMode();
  const [open, setOpen] = useState(false);
  const [models, setModels] = useState([]);
  const [loading, setLoading] = useState(false);
  const wrapperRef = useRef(null);

  // Lazy-load the model registry the first time the dropdown opens —
  // saves a request on every conversation load.
  useEffect(() => {
    if (!open || models.length > 0) return;
    let cancelled = false;
    setLoading(true);
    (async () => {
      try {
        const { data } = await api.get("/media/models");
        if (!cancelled) setModels(data?.models || []);
      } catch (_) {
        if (!cancelled) setModels([]);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [open, models.length]);

  // Click-outside to dismiss
  useEffect(() => {
    if (!open) return;
    const onDoc = (e) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  // Group models by type for the dropdown — image first, then video, then music.
  const grouped = useMemo(() => {
    const out = { image: [], video: [], music: [] };
    for (const m of models) {
      if (out[m.type]) out[m.type].push(m);
    }
    return out;
  }, [models]);

  // Resolve label + sub-label for the trigger chip
  const trigger = useMemo(() => {
    if (mode === "auto") {
      return {
        title: "Dynamic Intelligence",
        sub: "Auto-picks the best model",
        icon: Cpu,
        active: true,
      };
    }
    if (mode === "off") {
      return {
        title: "Off",
        sub: "Plain chat only",
        icon: X,
        active: false,
      };
    }
    const m = models.find((x) => x.key === mode);
    return {
      title: m?.label || mode,
      sub: m ? `Default ${m.type} model` : "Locked model",
      icon: TYPE_ICON[m?.type] || Sparkles,
      active: true,
    };
  }, [mode, models]);

  const TIcon = trigger.icon;
  const pick = (val) => { setMode(val); setOpen(false); };

  return (
    <div className="relative inline-flex" ref={wrapperRef} data-testid="dynamic-intelligence-picker">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full sp-glass hover:bg-white/5 transition group"
        title="Dynamic Intelligence — choose how messages should be routed"
        data-testid="dynamic-intelligence-trigger"
        style={{
          border: trigger.active
            ? "1px solid rgba(246,64,0,0.45)"
            : "1px solid rgba(120,120,140,0.25)",
          boxShadow: trigger.active
            ? "0 0 12px rgba(246,64,0,0.18)"
            : "none",
        }}
      >
        <span
          className="w-2 h-2 rounded-full shrink-0"
          style={{
            background: trigger.active ? "#F64000" : "rgba(180,180,200,0.4)",
            boxShadow: trigger.active ? "0 0 6px rgba(246,64,0,0.7)" : "none",
          }}
        />
        <TIcon className="w-3.5 h-3.5" style={{ color: "var(--fg)" }} />
        <div className="flex flex-col items-start leading-tight">
          <span className="text-[11px] font-medium" style={{ color: "var(--fg)" }}>
            {trigger.title}
          </span>
          <span className="text-[9px] sp-subtle">{trigger.sub}</span>
        </div>
        <ChevronDown className={`w-3 h-3 sp-subtle transition ${open ? "rotate-180" : ""}`} />
      </button>

      {open && (
        <div
          className="absolute right-0 top-full mt-2 w-[300px] max-h-[420px] overflow-y-auto sp-glass-strong rounded-xl shadow-2xl z-50 p-2"
          data-testid="dynamic-intelligence-dropdown"
        >
          {/* The hero option — Dynamic Intelligence (auto) */}
          <button
            type="button"
            onClick={() => pick("auto")}
            className="w-full text-left px-3 py-2.5 rounded-lg flex items-start gap-3 hover:bg-white/5 transition"
            data-testid="routing-mode-auto"
            style={{
              background: mode === "auto" ? "rgba(246,64,0,0.10)" : "transparent",
              border: mode === "auto"
                ? "1px solid rgba(246,64,0,0.35)"
                : "1px solid transparent",
            }}
          >
            <div
              className="w-8 h-8 rounded-lg flex items-center justify-center shrink-0"
              style={{ background: "linear-gradient(160deg, #F64000, #FF7A36)" }}
            >
              <Cpu className="w-4 h-4 text-white" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-1.5">
                <span className="text-[12px] font-medium" style={{ color: "var(--fg)" }}>
                  Dynamic Intelligence
                </span>
                <span className="sp-chip text-[8px]" style={{
                  borderColor: "rgba(246,64,0,0.4)", color: "var(--fg)",
                }}>RECOMMENDED</span>
              </div>
              <div className="text-[10px] sp-subtle leading-snug mt-0.5">
                Detects what you want (image, video, or music) and picks the best model automatically.
              </div>
            </div>
            {mode === "auto" && <Check className="w-3.5 h-3.5 text-orange-400 shrink-0 mt-1.5" />}
          </button>

          {/* Per-type model groups */}
          {!loading && TYPE_ORDER.map((type) => {
            const items = grouped[type] || [];
            if (items.length === 0) return null;
            const Icon = TYPE_ICON[type];
            return (
              <div key={type} className="mt-2">
                <div className="px-2 py-1 flex items-center gap-1.5">
                  <Icon className="w-3 h-3 sp-subtle" />
                  <span className="text-[9px] uppercase tracking-widest sp-subtle">
                    {type === "image" ? "Default image model"
                      : type === "video" ? "Default video model"
                      : "Default music model"}
                  </span>
                </div>
                {items.map((m) => (
                  <button
                    key={m.key}
                    type="button"
                    onClick={() => pick(m.key)}
                    className="w-full text-left px-3 py-1.5 rounded-md flex items-center gap-2 hover:bg-white/5 transition"
                    data-testid={`routing-mode-${m.key}`}
                    style={mode === m.key ? {
                      background: "rgba(246,64,0,0.10)",
                    } : {}}
                  >
                    <span className="text-[11px] flex-1 truncate" style={{ color: "var(--fg)" }}>
                      {m.label}
                    </span>
                    <span className="text-[9px] sp-subtle uppercase">{m.provider}</span>
                    {mode === m.key && <Check className="w-3 h-3 text-orange-400 shrink-0" />}
                  </button>
                ))}
              </div>
            );
          })}
          {loading && (
            <div className="px-3 py-2 text-[11px] sp-subtle">Loading models…</div>
          )}

          {/* Off — pure chat */}
          <div className="mt-2 pt-2 border-t border-white/10">
            <button
              type="button"
              onClick={() => pick("off")}
              className="w-full text-left px-3 py-2 rounded-md flex items-center gap-2 hover:bg-white/5 transition"
              data-testid="routing-mode-off"
              style={mode === "off" ? { background: "rgba(120,120,140,0.18)" } : {}}
            >
              <X className="w-3 h-3 sp-subtle" />
              <div className="flex-1 min-w-0">
                <div className="text-[11px]" style={{ color: "var(--fg)" }}>Off — pure chat</div>
                <div className="text-[9px] sp-subtle">Never auto-generate media. Use the upload button or /mission instead.</div>
              </div>
              {mode === "off" && <Check className="w-3 h-3 sp-subtle shrink-0" />}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
