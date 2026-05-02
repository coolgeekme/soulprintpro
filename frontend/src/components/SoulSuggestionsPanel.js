import React, { useEffect, useState, useCallback } from "react";
import api from "../lib/api";
import { toast } from "sonner";
import {
  Sparkles,
  Send,
  Check,
  X as XIcon,
  Loader2,
  Trash2,
  ChevronDown,
  ChevronRight,
  ClipboardEdit,
} from "lucide-react";

/**
 * SoulSuggestionsPanel — role-aware Soul change-request UI.
 *
 * Why: Per business policy, only the org **owner** can directly mutate the
 * company Soul. Admins and members route changes through this suggestion
 * pipeline. Owners + admins also get reviewer affordances here so they can
 * approve / reject pending requests without leaving the page.
 *
 * Surfaces three things in one card:
 *   1. The submitter form (always available — even owner/admin can use it
 *      as a notepad to track proposed copy edits before applying them).
 *   2. A pending-review list (visible to all roles; reviewer-only buttons
 *      conditionally rendered for owner/admin).
 *   3. A history strip of recently approved/rejected suggestions so the
 *      owner can audit who suggested what.
 */
const SOUL_FIELDS = [
  ["mission", "Mission", "What you exist to do — one to two sentences."],
  ["audience", "Audience", "Who you serve. Be specific."],
  ["tone", "Tone", "How you sound. Adjectives, not prose."],
  ["values", "Values", "What you stand for, comma-separated."],
  ["north_star", "North star", "The single metric you obsess over."],
  ["summary", "Summary", "30-second elevator pitch — used in agent prompts."],
];

const STATUS_STYLES = {
  pending: { bg: "color-mix(in srgb, var(--primary-bg) 14%, transparent)", border: "color-mix(in srgb, var(--primary-bg) 30%, transparent)", color: "var(--primary-bg)" },
  approved: { bg: "rgba(34,197,94,0.14)", border: "rgba(34,197,94,0.4)", color: "rgb(74,222,128)" },
  rejected: { bg: "rgba(239,68,68,0.14)", border: "rgba(239,68,68,0.4)", color: "rgb(248,113,113)" },
};

function StatusPill({ status }) {
  const s = STATUS_STYLES[status] || STATUS_STYLES.pending;
  return (
    <span
      className="px-2 py-0.5 rounded-full text-[10px] uppercase tracking-wider font-semibold"
      style={{ background: s.bg, border: `1px solid ${s.border}`, color: s.color }}
    >
      {status}
    </span>
  );
}

function SuggestionCard({ s, currentSoul, role, onAction, busyId }) {
  const isReviewer = role === "owner" || role === "admin";
  const [reviewerNote, setReviewerNote] = useState("");
  const [expanded, setExpanded] = useState(s.status === "pending");
  const isBusy = busyId === s.id;

  return (
    <div
      className="sp-glass rounded-xl p-4 space-y-3"
      data-testid={`soul-suggestion-${s.id}`}
    >
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div className="flex flex-col min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <StatusPill status={s.status} />
            <span className="text-sm font-medium truncate">
              {s.submitted_by_name || s.submitted_by_email || "A teammate"}
            </span>
            <span className="text-[11px] sp-subtle">
              {s.created_at ? new Date(s.created_at).toLocaleString() : ""}
            </span>
          </div>
          {s.note && (
            <p className="text-xs sp-subtle italic mt-1 leading-relaxed">"{s.note}"</p>
          )}
        </div>
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="sp-btn sp-btn-ghost text-xs"
          data-testid={`soul-suggestion-toggle-${s.id}`}
        >
          {expanded ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
          {expanded ? "Hide" : "Show"} changes
        </button>
      </div>

      {expanded && (
        <>
          <div className="grid sm:grid-cols-2 gap-2 text-xs">
            {SOUL_FIELDS.map(([key, label]) => {
              const proposed = s.fields?.[key];
              if (!proposed) return null;
              const current = (currentSoul || {})[key] || "";
              return (
                <div key={key} className="rounded-lg p-2 border" style={{ borderColor: "var(--border-soft)" }}>
                  <div className="text-[10px] uppercase tracking-widest sp-subtle mb-1">{label}</div>
                  {current && (
                    <div className="line-through opacity-50 mb-1 leading-snug">{current}</div>
                  )}
                  <div className="leading-snug" style={{ color: "var(--fg)" }}>{proposed}</div>
                </div>
              );
            })}
          </div>

          {s.reviewer_note && (
            <div className="text-xs sp-subtle">
              Reviewer note: <em>"{s.reviewer_note}"</em>
              {s.reviewed_by_name && ` — ${s.reviewed_by_name}`}
            </div>
          )}

          {isReviewer && s.status === "pending" && (
            <div className="flex flex-wrap gap-2 items-center">
              <input
                value={reviewerNote}
                onChange={(e) => setReviewerNote(e.target.value)}
                placeholder="Optional reviewer note"
                className="flex-1 min-w-[180px] rounded-lg px-3 py-1.5 text-xs"
                style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
                data-testid={`soul-reviewer-note-${s.id}`}
              />
              <button
                type="button"
                disabled={isBusy}
                onClick={() => onAction("reject", s, reviewerNote)}
                className="sp-btn sp-btn-ghost text-xs hover:!text-red-300"
                data-testid={`soul-reject-${s.id}`}
              >
                {isBusy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <XIcon className="w-3.5 h-3.5" />}
                Reject
              </button>
              <button
                type="button"
                disabled={isBusy}
                onClick={() => onAction("approve", s, reviewerNote)}
                className="sp-btn sp-btn-primary text-xs"
                data-testid={`soul-approve-${s.id}`}
              >
                {isBusy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />}
                Approve & apply
              </button>
            </div>
          )}

          {!isReviewer && s.status === "pending" && (
            <div className="flex justify-end">
              <button
                type="button"
                disabled={isBusy}
                onClick={() => onAction("cancel", s)}
                className="sp-btn sp-btn-ghost text-xs hover:!text-red-300"
                data-testid={`soul-cancel-${s.id}`}
              >
                <Trash2 className="w-3.5 h-3.5" /> Cancel my suggestion
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default function SoulSuggestionsPanel({ orgId, role, currentSoul, onSoulApplied }) {
  const [list, setList] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState("");
  const [composerOpen, setComposerOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [draft, setDraft] = useState({ mission: "", audience: "", tone: "", values: "", north_star: "", summary: "", note: "" });
  const isReviewer = role === "owner" || role === "admin";

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await api.get(`/orgs/${orgId}/soul-suggestions`);
      setList(data || []);
    } catch (err) {
      // Quietly ignore — endpoint may 403 momentarily during role hydration.
    } finally {
      setLoading(false);
    }
  }, [orgId]);

  useEffect(() => { load(); }, [load]);

  const submit = async (e) => {
    e.preventDefault();
    const payload = { note: draft.note.trim() };
    let any = false;
    SOUL_FIELDS.forEach(([k]) => {
      const v = (draft[k] || "").trim();
      if (v) { payload[k] = v; any = true; }
    });
    if (!any) {
      toast.error("Fill in at least one Soul field before submitting.");
      return;
    }
    setSubmitting(true);
    try {
      await api.post(`/orgs/${orgId}/soul-suggestions`, payload);
      toast.success(isReviewer ? "Suggestion logged — review it below." : "Sent for owner review.");
      setDraft({ mission: "", audience: "", tone: "", values: "", north_star: "", summary: "", note: "" });
      setComposerOpen(false);
      await load();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Submit failed");
    } finally {
      setSubmitting(false);
    }
  };

  const onAction = async (kind, s, reviewerNote = "") => {
    setBusyId(s.id);
    try {
      if (kind === "approve") {
        await api.post(`/orgs/${orgId}/soul-suggestions/${s.id}/approve`, { note: reviewerNote });
        toast.success("Approved — Soul updated.");
        if (onSoulApplied) onSoulApplied();
      } else if (kind === "reject") {
        await api.post(`/orgs/${orgId}/soul-suggestions/${s.id}/reject`, { note: reviewerNote });
        toast.success("Rejected.");
      } else if (kind === "cancel") {
        if (!window.confirm("Cancel this suggestion?")) { setBusyId(""); return; }
        await api.delete(`/orgs/${orgId}/soul-suggestions/${s.id}`);
        toast.success("Cancelled.");
      }
      await load();
    } catch (err) {
      toast.error(err?.response?.data?.detail || `${kind} failed`);
    } finally {
      setBusyId("");
    }
  };

  const pending = list.filter((s) => s.status === "pending");
  const reviewed = list.filter((s) => s.status !== "pending");

  return (
    <div className="sp-glass rounded-2xl p-5 mb-8" data-testid="soul-suggestions-panel">
      <div className="flex items-start justify-between gap-3 mb-4 flex-wrap">
        <div>
          <h3 className="text-sm uppercase tracking-widest sp-subtle flex items-center gap-2">
            <Sparkles className="w-4 h-4 text-orange-400" /> Soul change requests
            {pending.length > 0 && isReviewer && (
              <span
                className="px-2 py-0.5 rounded-full text-[10px] uppercase tracking-wider font-semibold animate-pulse"
                style={{ background: "color-mix(in srgb, var(--primary-bg) 22%, transparent)", color: "var(--primary-bg)" }}
                data-testid="soul-pending-badge"
              >
                {pending.length} to review
              </span>
            )}
          </h3>
          <p className="text-xs sp-subtle mt-1 leading-relaxed max-w-xl">
            {role === "owner"
              ? "Only you can directly change the company Soul. Members and admins can suggest edits — review and approve them here."
              : isReviewer
                ? "Members can suggest Soul edits — review and approve them here. Direct edits remain with the owner."
                : "Suggest changes to the company Soul. The owner will review and apply them."}
          </p>
        </div>
        <button
          type="button"
          onClick={() => setComposerOpen((v) => !v)}
          className="sp-btn sp-btn-primary text-xs"
          data-testid="soul-suggest-toggle"
        >
          <ClipboardEdit className="w-3.5 h-3.5" />
          {composerOpen ? "Close" : "Suggest changes"}
        </button>
      </div>

      {composerOpen && (
        <form onSubmit={submit} className="space-y-3 mb-5" data-testid="soul-suggest-form">
          <div className="grid sm:grid-cols-2 gap-3">
            {SOUL_FIELDS.map(([key, label, hint]) => (
              <label key={key} className="block">
                <span className="text-[10px] uppercase tracking-widest sp-subtle">{label}</span>
                <textarea
                  value={draft[key]}
                  onChange={(e) => setDraft((d) => ({ ...d, [key]: e.target.value }))}
                  placeholder={`${hint} ${currentSoul?.[key] ? `Current: "${currentSoul[key].slice(0, 80)}${currentSoul[key].length > 80 ? "…" : ""}"` : ""}`}
                  rows={2}
                  className="w-full rounded-xl px-3 py-2 text-sm resize-y mt-1"
                  style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
                  data-testid={`soul-suggest-${key}`}
                />
              </label>
            ))}
          </div>
          <textarea
            value={draft.note}
            onChange={(e) => setDraft((d) => ({ ...d, note: e.target.value }))}
            placeholder="Why are you proposing this change? (optional, helps the reviewer)"
            rows={2}
            className="w-full rounded-xl px-3 py-2 text-sm resize-y"
            style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
            data-testid="soul-suggest-note"
          />
          <div className="flex justify-end gap-2">
            <button type="button" onClick={() => setComposerOpen(false)} className="sp-btn sp-btn-ghost text-xs">Cancel</button>
            <button
              type="submit"
              disabled={submitting}
              className="sp-btn sp-btn-primary text-xs disabled:opacity-50"
              data-testid="soul-suggest-submit"
            >
              {submitting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Send className="w-3.5 h-3.5" />}
              {submitting ? "Sending…" : "Submit suggestion"}
            </button>
          </div>
        </form>
      )}

      {loading ? (
        <div className="text-xs sp-subtle italic">Loading suggestions…</div>
      ) : list.length === 0 ? (
        <div className="text-xs sp-subtle italic">
          No suggestions yet. {!composerOpen && "Click \"Suggest changes\" to propose one."}
        </div>
      ) : (
        <div className="space-y-3">
          {pending.length > 0 && (
            <div className="space-y-2">
              <div className="text-[10px] uppercase tracking-widest sp-subtle">Pending review</div>
              {pending.map((s) => (
                <SuggestionCard key={s.id} s={s} currentSoul={currentSoul} role={role} onAction={onAction} busyId={busyId} />
              ))}
            </div>
          )}
          {reviewed.length > 0 && (
            <div className="space-y-2">
              <div className="text-[10px] uppercase tracking-widest sp-subtle pt-2">Recently reviewed</div>
              {reviewed.slice(0, 6).map((s) => (
                <SuggestionCard key={s.id} s={s} currentSoul={currentSoul} role={role} onAction={onAction} busyId={busyId} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
