import React, { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import api from "../lib/api";
import {
  Search, X, Loader2, Sparkles, MessageSquare, Rocket, FileText,
  Megaphone, Bot, Hash, ArrowRight,
} from "lucide-react";

const KIND_META = {
  agent:         { icon: Bot,           label: "Agent",      tint: "text-orange-300 bg-orange-400/10" },
  conversation:  { icon: MessageSquare, label: "Chat",       tint: "text-sky-300 bg-sky-400/10" },
  message:       { icon: Hash,          label: "Message",    tint: "text-violet-300 bg-violet-400/10" },
  mission:       { icon: Rocket,        label: "Mission",    tint: "text-amber-300 bg-amber-400/10" },
  document:      { icon: FileText,      label: "Document",   tint: "text-emerald-300 bg-emerald-400/10" },
  social_post:   { icon: Megaphone,     label: "Social",     tint: "text-pink-300 bg-pink-400/10" },
};

function HitRow({ hit, active, onClick }) {
  const meta = KIND_META[hit.kind] || { icon: Hash, label: hit.kind, tint: "" };
  const Icon = meta.icon;
  return (
    <button
      type="button"
      onClick={onClick}
      className={`w-full text-left px-4 py-3 rounded-lg flex items-start gap-3 transition-colors ${
        active ? "bg-white/[0.08] border border-white/15" : "border border-transparent hover:bg-white/[0.04]"
      }`}
      data-testid={`spotlight-hit-${hit.kind}-${hit.id || hit.title}`}
    >
      <div className={`w-7 h-7 rounded flex items-center justify-center shrink-0 ${meta.tint}`}>
        <Icon className="w-3.5 h-3.5" />
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 text-xs">
          <span className="text-[10px] uppercase tracking-widest sp-subtle">{meta.label}</span>
          {hit.status && (
            <span className="text-[10px] sp-subtle">· {hit.status}</span>
          )}
        </div>
        <div className="text-sm font-medium truncate mt-0.5">{hit.title}</div>
        {hit.excerpt && (
          <div className="text-xs sp-subtle line-clamp-2 mt-0.5">{hit.excerpt}</div>
        )}
      </div>
      <ArrowRight className="w-3.5 h-3.5 sp-subtle mt-1 shrink-0" />
    </button>
  );
}

export default function SpotlightSearch({ open, onClose, orgId }) {
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState(null);
  const [active, setActive] = useState(0);
  const [aiLoading, setAiLoading] = useState(false);
  const [aiAnswer, setAiAnswer] = useState(null);
  const inputRef = useRef(null);
  const navigate = useNavigate();

  const top = useMemo(() => data?.top || [], [data]);

  // Focus + reset when opened
  useEffect(() => {
    if (open) {
      setQ("");
      setData(null);
      setActive(0);
      setAiAnswer(null);
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [open]);

  // Debounced fetch
  useEffect(() => {
    if (!open || !orgId || !q.trim()) {
      setData(null);
      return;
    }
    const t = setTimeout(async () => {
      setLoading(true);
      try {
        const { data } = await api.get("/search", {
          params: { org_id: orgId, q, limit: 8 },
        });
        setData(data);
        setActive(0);
      } catch (err) {
        setData({ top: [], groups: {}, counts: {} });
      } finally {
        setLoading(false);
      }
    }, 220);
    return () => clearTimeout(t);
  }, [q, orgId, open]);

  const runAi = async () => {
    if (!data || !data.top?.length) return;
    setAiLoading(true);
    setAiAnswer(null);
    try {
      const { data: ans } = await api.post("/search/synthesize", {
        org_id: orgId, q, top: data.top,
      });
      setAiAnswer(ans);
    } catch (err) {
      setAiAnswer({ answer: "Synthesis failed. Try again or browse the results below.", citations: [] });
    } finally {
      setAiLoading(false);
    }
  };

  const openHit = (h) => {
    if (h?.url) {
      onClose();
      if (h.url.startsWith("/api/")) {
        // Document file URL — open in new tab
        window.open(h.url, "_blank");
      } else {
        navigate(h.url);
      }
    } else if (h?.kind === "agent") {
      onClose();
      // No deep link — surface a toast hint
      // (toast import would bloat; skip for MVP)
    }
  };

  // Keyboard navigation
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => {
      if (e.key === "Escape") { onClose(); }
      else if (e.key === "ArrowDown") {
        e.preventDefault();
        setActive((v) => Math.min(top.length - 1, v + 1));
      }
      else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActive((v) => Math.max(0, v - 1));
      }
      else if (e.key === "Enter" && top[active]) {
        e.preventDefault();
        openHit(top[active]);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  /* eslint-disable-next-line */
  }, [open, top, active]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[100] flex items-start justify-center pt-[8vh] px-4 bg-black/70 backdrop-blur-sm"
      onClick={onClose}
      data-testid="spotlight-search-overlay"
    >
      <div
        className="sp-glass-strong rounded-2xl w-full max-w-2xl overflow-hidden border border-white/15 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 px-4 py-3 border-b border-white/10">
          <Search className="w-4 h-4 sp-subtle" />
          <input
            ref={inputRef}
            type="text"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search chats, missions, docs, social posts, agents…"
            className="flex-1 bg-transparent outline-none text-sm placeholder:sp-subtle"
            data-testid="spotlight-input"
          />
          {loading && <Loader2 className="w-4 h-4 animate-spin sp-subtle" />}
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-white/10"
            aria-label="Close"
            data-testid="spotlight-close"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="max-h-[65vh] overflow-y-auto p-3 space-y-1" data-testid="spotlight-results">
          {!q.trim() && (
            <div className="px-4 py-6 text-center text-sm sp-subtle">
              Type to search across this org. Use{" "}
              <kbd className="sp-chip text-[10px] !py-0.5">↑↓</kbd> to navigate,{" "}
              <kbd className="sp-chip text-[10px] !py-0.5">⏎</kbd> to open,{" "}
              <kbd className="sp-chip text-[10px] !py-0.5">Esc</kbd> to close.
            </div>
          )}

          {q.trim() && !loading && top.length === 0 && (
            <div className="px-4 py-6 text-center text-sm sp-subtle">
              No matches in this org. Try a different phrase, or ask a
              specialist agent directly.
            </div>
          )}

          {top.length > 0 && (
            <>
              <div className="flex items-center justify-between px-2 py-1">
                <div className="text-[10px] uppercase tracking-widest sp-subtle">
                  {top.length} result{top.length === 1 ? "" : "s"}
                </div>
                <button
                  type="button"
                  onClick={runAi}
                  disabled={aiLoading || aiAnswer}
                  className="sp-btn sp-btn-primary text-[11px] py-1 px-2.5 flex items-center gap-1"
                  data-testid="spotlight-ask-foundry"
                >
                  {aiLoading
                    ? <Loader2 className="w-3 h-3 animate-spin" />
                    : <Sparkles className="w-3 h-3" />}
                  Ask Foundry
                </button>
              </div>

              {aiAnswer && (
                <div
                  className="sp-glass rounded-lg p-3 mx-1 mb-2 border border-orange-400/25"
                  data-testid="spotlight-ai-answer"
                >
                  <div className="text-[10px] uppercase tracking-widest sp-subtle mb-1.5 flex items-center gap-1">
                    <Sparkles className="w-3 h-3 text-orange-300" /> Synthesised answer
                  </div>
                  <div className="text-sm leading-relaxed whitespace-pre-wrap">
                    {aiAnswer.answer}
                  </div>
                  {aiAnswer.citations?.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1">
                      {aiAnswer.citations.map((c) => (
                        <span key={c.n} className="text-[10px] sp-chip !py-0.5">
                          [{c.n}] {c.title?.slice(0, 30)}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {top.map((hit, i) => (
                <HitRow
                  key={`${hit.kind}-${hit.id || hit.title}-${i}`}
                  hit={hit}
                  active={i === active}
                  onClick={() => openHit(hit)}
                />
              ))}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
