/**
 * DynamicIntelligencePicker — the prominent chip + dropdown that lets a
 * user configure Dynamic Intelligence on a per-content-type basis.
 *
 * Each of the four content types (chat, image, video, music) has its
 * own routing default: either ``"auto"`` (Dynamic Intelligence picks
 * the best model for that type) or a specific model key (locks that
 * type to one model).
 *
 * Persistence is two-tier: localStorage for instant UI hydration before
 * the network call lands, plus the server-side ``routing_preferences``
 * map on the user doc (PATCH /api/auth/me/preferences) so choices
 * survive logout / device hops. The ``useRoutingMode()`` hook handles
 * the sync transparently for any consumer.
 *
 * Provider names (kie / emergent / etc.) are intentionally NEVER shown
 * to users — they only see human-readable model labels.
 */
import React, { useEffect, useMemo, useRef, useState, useCallback } from "react";
import { Cpu, Image as ImageIcon, Film, Music, MessageCircle, ChevronDown, Check, X } from "lucide-react";
import api from "../lib/api";

// localStorage cache key. The shape is a JSON object:
// { chat: "auto"|model, image: "auto"|model, video: "auto"|model, music: "auto"|model }
const LS_KEY = "sp:routing-preferences";

const TYPE_ICON = { chat: MessageCircle, image: ImageIcon, video: Film, music: Music };
const TYPE_LABEL = { chat: "Chat", image: "Image", video: "Video", music: "Music" };
const TYPE_ORDER = ["chat", "image", "video", "music"];

const DEFAULT_PREFS = { chat: "auto", image: "auto", video: "auto", music: "auto" };


/** Read prefs from localStorage. Always returns a complete object so
 * components can safely destructure even on first run. */
export function readRoutingPrefs() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return { ...DEFAULT_PREFS };
    const parsed = JSON.parse(raw);
    return { ...DEFAULT_PREFS, ...parsed };
  } catch (_) {
    return { ...DEFAULT_PREFS };
  }
}

function writeRoutingPrefsLocal(prefs) {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(prefs));
    window.dispatchEvent(new CustomEvent("sp:routing-prefs-change", { detail: prefs }));
  } catch (_) { /* best-effort */ }
}


/**
 * Reactive hook returning ``[prefs, setPref]``.
 *   - ``prefs`` is the full {chat, image, video, music} object.
 *   - ``setPref(type, value)`` updates a single key, persists locally
 *     immediately, and PATCHes the server so the choice survives
 *     logout. Errors are silent (UI stays responsive).
 *
 * The hook syncs across mounts via the ``sp:routing-prefs-change``
 * window event AND the standard ``storage`` event for cross-tab parity.
 * On first mount it also pulls the canonical state from
 * ``GET /api/auth/me`` so a fresh device picks up server-side prefs.
 */
export function useRoutingPrefs() {
  const [prefs, setPrefsState] = useState(() => readRoutingPrefs());

  // Hydrate from server on mount — corrects any drift between
  // localStorage and the user's saved server-side prefs.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { data } = await api.get("/auth/me");
        if (cancelled) return;
        if (data?.routing_preferences) {
          const merged = { ...DEFAULT_PREFS, ...data.routing_preferences };
          setPrefsState(merged);
          writeRoutingPrefsLocal(merged);
        }
      } catch (_) { /* offline / unauthenticated — keep cache */ }
    })();
    return () => { cancelled = true; };
  }, []);

  // Local + cross-tab sync
  useEffect(() => {
    const onChange = (e) => setPrefsState(e.detail || readRoutingPrefs());
    const onStorage = (e) => { if (e.key === LS_KEY) setPrefsState(readRoutingPrefs()); };
    window.addEventListener("sp:routing-prefs-change", onChange);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener("sp:routing-prefs-change", onChange);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  const setPref = useCallback((type, value) => {
    setPrefsState((prev) => {
      const next = { ...prev, [type]: value };
      writeRoutingPrefsLocal(next);
      // Fire-and-forget server PATCH — survives logout
      api.patch("/auth/me/preferences", { [type]: value }).catch(() => {});
      return next;
    });
  }, []);

  return [prefs, setPref];
}

/** Back-compat for the original single-mode hook. Returns ``"auto"`` if
 * either chat OR image OR video is set to a specific model — i.e. used
 * by older callers that just want a yes/no on Dynamic Intelligence. */
export function useRoutingMode() {
  const [prefs] = useRoutingPrefs();
  // Composite mode for legacy consumers — "auto" if every type is auto.
  const allAuto = TYPE_ORDER.every((t) => (prefs[t] || "auto") === "auto");
  return [allAuto ? "auto" : "mixed", () => {}];
}


/**
 * Compact summary of a prefs object — used by both the picker chip and
 * the global TopBar indicator.  Returns
 *   { allAuto, anyLocked, lockedTypes: ["video","music"] }
 */
export function summarisePrefs(prefs) {
  const lockedTypes = TYPE_ORDER.filter((t) => (prefs[t] || "auto") !== "auto");
  return {
    allAuto: lockedTypes.length === 0,
    anyLocked: lockedTypes.length > 0,
    lockedTypes,
  };
}


export default function DynamicIntelligencePicker({ compact = false }) {
  const [prefs, setPref] = useRoutingPrefs();
  const [open, setOpen] = useState(false);
  const [models, setModels] = useState([]);
  const [loading, setLoading] = useState(false);
  const wrapperRef = useRef(null);

  // Lazy-load model registry the first time the dropdown opens.
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

  // Group models by type
  const grouped = useMemo(() => {
    const out = { chat: [], image: [], video: [], music: [] };
    for (const m of models) {
      if (out[m.type]) out[m.type].push(m);
    }
    return out;
  }, [models]);

  const summary = summarisePrefs(prefs);

  // Trigger label — "Dynamic Intelligence" if all auto, else the count of locked types.
  const triggerTitle = summary.allAuto
    ? "Dynamic Intelligence"
    : `Custom defaults (${summary.lockedTypes.length}/4)`;
  const triggerSub = summary.allAuto
    ? "Auto-picks per content type"
    : summary.lockedTypes.map((t) => TYPE_LABEL[t]).join(" · ");

  return (
    <div className="relative inline-flex" ref={wrapperRef} data-testid="dynamic-intelligence-picker">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={`inline-flex items-center gap-2 ${compact ? "px-2 py-1" : "px-3 py-1.5"} rounded-full sp-glass hover:bg-white/5 transition whitespace-nowrap`}
        title="Dynamic Intelligence — choose how each content type gets routed"
        data-testid="dynamic-intelligence-trigger"
        style={{
          border: summary.allAuto
            ? "1px solid rgba(246,64,0,0.45)"
            : "1px solid rgba(160,120,255,0.45)",
          boxShadow: summary.allAuto
            ? "0 0 12px rgba(246,64,0,0.18)"
            : "0 0 12px rgba(160,120,255,0.18)",
        }}
      >
        <span
          className="w-2 h-2 rounded-full shrink-0"
          style={{
            background: summary.allAuto ? "#F64000" : "#A078FF",
            boxShadow: summary.allAuto
              ? "0 0 6px rgba(246,64,0,0.7)"
              : "0 0 6px rgba(160,120,255,0.7)",
          }}
        />
        <Cpu className={`${compact ? "w-3 h-3" : "w-3.5 h-3.5"}`} style={{ color: "var(--fg)" }} />
        {!compact && (
          <div className="flex flex-col items-start leading-tight">
            <span className="text-[11px] font-medium" style={{ color: "var(--fg)" }}>
              {triggerTitle}
            </span>
            <span className="text-[9px] sp-subtle truncate max-w-[160px]">{triggerSub}</span>
          </div>
        )}
        {compact && (
          <span className="text-[10px] font-medium" style={{ color: "var(--fg)" }}>
            {summary.allAuto ? "Dynamic Intelligence" : "Custom"}
          </span>
        )}
        <ChevronDown className={`w-3 h-3 sp-subtle transition ${open ? "rotate-180" : ""}`} />
      </button>

      {open && (
        <div
          className="absolute right-0 top-full mt-2 w-[320px] max-h-[520px] overflow-y-auto sp-glass-strong rounded-xl shadow-2xl z-50 p-2"
          data-testid="dynamic-intelligence-dropdown"
        >
          <div className="px-2 py-2 mb-1">
            <div className="text-[12px] font-medium" style={{ color: "var(--fg)" }}>
              Dynamic Intelligence
            </div>
            <div className="text-[10px] sp-subtle leading-snug mt-0.5">
              For each content type, pick "Dynamic Intelligence" to let
              the system auto-choose the best model — or lock a specific
              default. Your choice is saved across devices.
            </div>
          </div>

          {loading && (
            <div className="px-3 py-3 text-[11px] sp-subtle">Loading models…</div>
          )}

          {!loading && TYPE_ORDER.map((type) => {
            const items = grouped[type] || [];
            const Icon = TYPE_ICON[type];
            const current = prefs[type] || "auto";
            return (
              <div key={type} className="mt-2 pb-2 border-b border-white/10 last:border-b-0">
                <div className="px-2 py-1 flex items-center gap-1.5">
                  <Icon className="w-3.5 h-3.5" style={{ color: "var(--fg)" }} />
                  <span className="text-[10px] uppercase tracking-widest" style={{ color: "var(--fg)" }}>
                    {TYPE_LABEL[type]}
                  </span>
                </div>
                {/* Auto option */}
                <button
                  type="button"
                  onClick={() => setPref(type, "auto")}
                  className="w-full text-left px-3 py-1.5 rounded-md flex items-center gap-2 hover:bg-white/5 transition"
                  data-testid={`routing-${type}-auto`}
                  style={current === "auto" ? {
                    background: "rgba(246,64,0,0.10)",
                    border: "1px solid rgba(246,64,0,0.30)",
                  } : { border: "1px solid transparent" }}
                >
                  <Cpu className="w-3 h-3" style={{ color: "#F64000" }} />
                  <span className="text-[11px] flex-1" style={{ color: "var(--fg)" }}>
                    Dynamic Intelligence
                  </span>
                  {current === "auto" && <Check className="w-3 h-3 text-orange-400 shrink-0" />}
                </button>
                {/* Specific models for this type */}
                {items.map((m) => (
                  <button
                    key={m.key}
                    type="button"
                    onClick={() => setPref(type, m.key)}
                    className="w-full text-left px-3 py-1.5 rounded-md flex items-center gap-2 hover:bg-white/5 transition"
                    data-testid={`routing-${type}-${m.key}`}
                    style={current === m.key ? {
                      background: "rgba(160,120,255,0.10)",
                      border: "1px solid rgba(160,120,255,0.30)",
                    } : { border: "1px solid transparent" }}
                  >
                    <span className="w-3 h-3 shrink-0" />
                    <span className="text-[11px] flex-1 truncate" style={{ color: "var(--fg)" }}>
                      {m.label}
                    </span>
                    {current === m.key && <Check className="w-3 h-3 text-violet-400 shrink-0" />}
                  </button>
                ))}
                {items.length === 0 && (
                  <div className="px-3 py-1.5 text-[10px] sp-subtle italic">
                    No models registered yet for {TYPE_LABEL[type].toLowerCase()}.
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
