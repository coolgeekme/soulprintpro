import React, { useEffect, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import api from "../lib/api";
import { MessageCircle, ArrowRight, Clock, Lock, Users, Trash2, Loader2 } from "lucide-react";
import { toast } from "sonner";

const relTime = (iso) => {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60) return "just now";
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    if (diff < 86400 * 7) return `${Math.floor(diff / 86400)}d ago`;
    return d.toLocaleDateString();
  } catch { return ""; }
};

/**
 * RecentConversations — thin "pick up where you left off" strip on the Org
 * page. Shows the 5 most-recently-active conversations the caller is a
 * member of, with a one-line preview of the last reply and a one-click
 * jump to the chat.
 *
 * Each card with `can_delete=true` (creator-of-thread OR org owner; never
 * the always-on Chief of Staff thread) gets a hover trash button.
 */
export default function RecentConversations({ orgId }) {
  const [items, setItems] = useState(null);
  const [deletingId, setDeletingId] = useState(null);

  const load = useCallback(async () => {
    try {
      const { data } = await api.get(`/orgs/${orgId}/recent-conversations`, {
        params: { limit: 5 },
      });
      setItems(data || []);
    } catch {
      setItems([]);
    }
  }, [orgId]);

  useEffect(() => { load(); }, [load]);

  const onDelete = async (e, c) => {
    e.preventDefault();
    e.stopPropagation();
    if (!c?.can_delete) return;
    if (!window.confirm(`Delete "${c.title}"? This permanently removes the conversation and all its messages.`)) return;
    setDeletingId(c.id);
    try {
      await api.delete(`/conversations/${c.id}`);
      // Optimistic remove + refresh
      setItems((prev) => (prev || []).filter((x) => x.id !== c.id));
      toast.success("Conversation deleted");
      // Pull fresh list so the strip refills if there were more behind it
      load();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not delete this conversation");
    } finally {
      setDeletingId(null);
    }
  };

  if (items === null) return null;    // loading — silent
  if (items.length === 0) return null; // nothing to resume — keep page clean

  return (
    <section className="mb-8" data-testid="recent-conversations">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <Clock className="w-3.5 h-3.5 sp-subtle" />
          <h3 className="text-[11px] uppercase tracking-widest sp-subtle">
            Pick up where you left off
          </h3>
        </div>
      </div>
      <ul className="grid md:grid-cols-2 lg:grid-cols-3 gap-2">
        {items.map((c) => (
          <li key={c.id} className="relative group/conv">
            <Link
              to={c.deeplink}
              className="sp-glass rounded-xl p-3 flex items-start gap-2.5 hover:border-white/25 transition group block"
              data-testid={`recent-conv-${c.id}`}
            >
              <div className="w-8 h-8 rounded-lg sp-glass-strong flex items-center justify-center shrink-0">
                {c.is_direct ? (
                  <MessageCircle className="w-3.5 h-3.5" />
                ) : c.is_general ? (
                  <Users className="w-3.5 h-3.5" />
                ) : (
                  <Users className="w-3.5 h-3.5" />
                )}
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 mb-0.5">
                  <span className="text-sm font-medium truncate">{c.title}</span>
                  {c.is_direct && (
                    <span
                      className="inline-flex items-center gap-0.5 text-[9px] uppercase tracking-widest sp-subtle px-1 py-0.5 rounded shrink-0"
                      style={{ background: "rgba(255,255,255,0.06)", border: "1px solid rgba(255,255,255,0.1)" }}
                      title="Direct chat — only you and the agent can see this thread"
                    >
                      <Lock className="w-2.5 h-2.5 opacity-70" />
                      Private
                    </span>
                  )}
                </div>
                <div className="text-[11px] sp-subtle truncate">
                  {c.agent_name ? `${c.agent_name} · ` : ""}
                  {c.preview || (c.team_name || "Conversation")}
                </div>
                <div className="text-[10px] sp-subtle mt-0.5">{relTime(c.last_message_at)}</div>
              </div>
              <ArrowRight className="w-3.5 h-3.5 text-white/40 group-hover:text-white transition shrink-0 self-center" />
            </Link>
            {c.can_delete && (
              <button
                type="button"
                onClick={(e) => onDelete(e, c)}
                disabled={deletingId === c.id}
                className="absolute top-1.5 right-1.5 rounded-md p-1.5 opacity-0 group-hover/conv:opacity-100 focus:opacity-100 transition hover:bg-red-500/15 disabled:opacity-60"
                style={{ color: "rgb(239 68 68)" }}
                title="Delete conversation"
                aria-label={`Delete conversation ${c.title}`}
                data-testid={`recent-conv-delete-${c.id}`}
              >
                {deletingId === c.id
                  ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  : <Trash2 className="w-3.5 h-3.5" />}
              </button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
