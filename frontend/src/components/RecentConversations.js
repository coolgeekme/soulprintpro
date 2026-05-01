import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import api from "../lib/api";
import { MessageCircle, ArrowRight, Clock, Lock, Users } from "lucide-react";

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
 */
export default function RecentConversations({ orgId }) {
  const [items, setItems] = useState(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { data } = await api.get(`/orgs/${orgId}/recent-conversations`, {
          params: { limit: 5 },
        });
        if (!cancelled) setItems(data || []);
      } catch {
        if (!cancelled) setItems([]);
      }
    })();
    return () => { cancelled = true; };
  }, [orgId]);

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
          <li key={c.id}>
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
                  {c.is_direct && <Lock className="w-2.5 h-2.5 opacity-60 shrink-0" />}
                </div>
                <div className="text-[11px] sp-subtle truncate">
                  {c.agent_name ? `${c.agent_name} · ` : ""}
                  {c.preview || (c.team_name || "Conversation")}
                </div>
                <div className="text-[10px] sp-subtle mt-0.5">{relTime(c.last_message_at)}</div>
              </div>
              <ArrowRight className="w-3.5 h-3.5 text-white/40 group-hover:text-white transition shrink-0 self-center" />
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}
