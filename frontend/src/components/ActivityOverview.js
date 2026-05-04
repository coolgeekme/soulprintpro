import React, { useEffect, useState } from "react";
import api from "../lib/api";
import { Link } from "react-router-dom";
import {
  Activity, MessageCircle, Megaphone, Calendar, FileText, Image as ImageIcon,
  CheckCircle2, Clock, Edit3, Send,
} from "lucide-react";

// Maps the backend feed `kind` to icon, accent color, and a friendly label.
const KIND_META = {
  "social.draft":          { icon: Edit3,        label: "Draft",       color: "#F64000" },
  "social.scheduled":      { icon: Calendar,     label: "Scheduled",   color: "#F59E0B" },
  "social.published":      { icon: CheckCircle2, label: "Published",   color: "#10B981" },
  "schedule.created":      { icon: Clock,        label: "New schedule",color: "#0EA5E9" },
  "schedule.fired":        { icon: Send,         label: "Fired",       color: "#10B981" },
  "conversation.created":  { icon: MessageCircle, label: "Conversation",color: "#A855F7" },
  "message.agent_reply":   { icon: MessageCircle, label: "Agent reply", color: "#A855F7" },
  "asset.uploaded":        { icon: FileText,     label: "Asset",       color: "#94A3B8" },
};

const fmtRelative = (iso) => {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  const now = Date.now();
  const s = Math.floor((now - then) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 7) return `${Math.floor(s / 86400)}d ago`;
  try { return new Date(iso).toLocaleDateString(); } catch { return ""; }
};

export default function ActivityOverview({ orgId }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const { data } = await api.get(`/orgs/${orgId}/activity?limit=30`);
        if (!cancelled) setData(data);
      } catch {
        // Non-fatal — just hide the panel.
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    load();
    // Light refresh every 30s so KPIs stay current without a full page reload.
    const t = setInterval(load, 30000);
    return () => { cancelled = true; clearInterval(t); };
  }, [orgId]);

  if (loading || !data) return null;
  const k = data.kpis || {};

  const tiles = [
    { label: "Social drafts",  value: k.social_drafts,    sub: `${k.social_queued || 0} scheduled`,  testid: "kpi-social-drafts" },
    { label: "Published 7d",   value: k.social_published_7d || 0, sub: `${k.social_published || 0} all time`, testid: "kpi-social-published" },
    { label: "Active schedules", value: k.active_schedules, sub: `${k.total_schedules || 0} total`,  testid: "kpi-schedules" },
    { label: "Active chats 7d",  value: k.active_conversations_7d || 0, sub: `${k.total_conversations || 0} total`, testid: "kpi-conversations" },
    { label: "Teams",          value: k.total_teams,      sub: "in this org",                       testid: "kpi-teams" },
    { label: "Assets",         value: k.total_assets,     sub: "in library",                        testid: "kpi-assets" },
  ];

  return (
    <section className="sp-glass-strong rounded-2xl p-5 sm:p-6 mb-6" data-testid="activity-overview">
      <div className="flex items-center gap-2 mb-4">
        <Activity className="w-4 h-4 text-orange-400" />
        <h3 className="text-sm uppercase tracking-widest sp-subtle">Overview</h3>
        <span className="text-[11px] sp-subtle ml-2">Live activity from across the org</span>
      </div>

      {/* KPI tiles */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2 mb-5" data-testid="kpi-grid">
        {tiles.map((t) => (
          <div key={t.label} className="sp-glass rounded-xl px-4 py-3" data-testid={t.testid}>
            <div className="text-2xl font-medium leading-none" style={{ color: "var(--fg)" }}>
              {t.value ?? 0}
            </div>
            <div className="text-[11px] sp-subtle mt-1.5">{t.label}</div>
            <div className="text-[11px] sp-subtle opacity-70 mt-0.5">{t.sub}</div>
          </div>
        ))}
      </div>

      {/* Activity feed */}
      <div className="text-xs uppercase tracking-widest sp-subtle mb-2 flex items-center gap-2">
        Recent activity
        <span className="opacity-60">{data.feed.length}</span>
      </div>
      {data.feed.length === 0 ? (
        <div className="sp-glass rounded-xl p-6 text-center sp-subtle text-sm">Nothing happening yet — try sending a message in Chief of Staff or drafting a social post.</div>
      ) : (
        <ul className="space-y-1.5" data-testid="activity-feed">
          {data.feed.slice(0, 20).map((e, idx) => {
            const meta = KIND_META[e.kind] || { icon: Activity, label: e.kind, color: "#94A3B8" };
            const Icon = meta.icon;
            const Inner = (
              <div className="flex items-start gap-3 sp-glass rounded-xl px-4 py-2.5 hover:border-white/20 transition group">
                <div className="w-7 h-7 rounded-lg shrink-0 flex items-center justify-center"
                     style={{ background: `${meta.color}26`, color: meta.color }}>
                  <Icon className="w-3.5 h-3.5" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-baseline gap-2 flex-wrap">
                    <span className="text-[11px] uppercase tracking-widest" style={{ color: meta.color }}>
                      {meta.label}
                    </span>
                    {e.agent_name && <span className="text-[11px] sp-subtle">· {e.agent_name}</span>}
                    {e.subtitle && <span className="text-[11px] sp-subtle truncate max-w-[200px]">· {e.subtitle}</span>}
                    <span className="text-[11px] sp-subtle ml-auto">{fmtRelative(e.timestamp)}</span>
                  </div>
                  <div className="text-sm leading-snug truncate" style={{ color: "var(--fg)" }}>
                    {e.title || "(untitled)"}
                  </div>
                </div>
                {e.image_url && <img src={`${e.image_url.startsWith("http") ? "" : ""}${e.image_url}`} alt="" className="w-10 h-10 rounded-md object-cover" />}
              </div>
            );
            return (
              <li key={idx} data-testid={`activity-${e.kind}-${idx}`}>
                {e.deeplink ? <Link to={e.deeplink}>{Inner}</Link> : Inner}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
