import React, { useEffect, useMemo, useState } from "react";
import { Navigate } from "react-router-dom";
import {
  AreaChart, Area, BarChart, Bar, XAxis, YAxis,
  CartesianGrid, Tooltip, ResponsiveContainer, Legend,
} from "recharts";
import {
  Loader2, Users, Building2, Layers, Rocket, MessageSquare,
  Activity, DollarSign, Search, ShieldCheck, Target, KeyRound,
} from "lucide-react";

import api from "../lib/api";
import { useAuth } from "../context/AuthContext";
import TopBar from "../components/TopBar";
import AdminUsersPanel from "../components/AdminUsersPanel";
import AdminAuditsPanel from "../components/AdminAuditsPanel";
import AdminAccessCodesPanel from "../components/AdminAccessCodesPanel";
import { formatLocalDateTime, formatLocalDate } from "../lib/datetime";

/**
 * AdminDashboard — superadmin-only analytics + tooling surface.
 *
 * Tabs: Overview · API Spend · Orgs · Users · Missions · Audits · Access Codes
 * Hard-gated client-side via `user.is_superadmin`; the backend also
 * 403s any non-superadmin hitting /api/admin/*, so a tampered client
 * still gets nothing.
 */
const TAB_DEFS = [
  { id: "overview", label: "Overview", icon: Activity },
  { id: "spend",    label: "API Spend", icon: DollarSign },
  { id: "orgs",     label: "Orgs", icon: Building2 },
  { id: "users",    label: "Users", icon: Users },
  { id: "missions", label: "Missions", icon: Rocket },
  { id: "audits",   label: "Audits", icon: Target },
  { id: "access",   label: "Access Codes", icon: KeyRound },
];

function StatCard({ icon: Icon, label, value, sub }) {
  return (
    <div className="sp-glass-strong rounded-2xl p-5" data-testid={`admin-stat-${label.toLowerCase().replace(/\s+/g, "-")}`}>
      <div className="flex items-center gap-2 mb-2">
        <div className="w-7 h-7 rounded-lg sp-glass flex items-center justify-center">
          <Icon className="w-3.5 h-3.5" />
        </div>
        <span className="text-[10px] uppercase tracking-widest sp-subtle">{label}</span>
      </div>
      <div className="sp-display text-3xl">{value}</div>
      {sub && <div className="text-xs sp-subtle mt-1">{sub}</div>}
    </div>
  );
}

function OverviewPanel() {
  const [data, setData] = useState(null);
  const [excludeTest, setExcludeTest] = useState(true);
  useEffect(() => {
    setData(null);
    api.get(`/admin/overview?exclude_test=${excludeTest}`).then((r) => setData(r.data)).catch(() => setData({}));
  }, [excludeTest]);
  if (!data) return <div className="sp-subtle text-sm flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" /> Loading…</div>;
  const t = data.totals || {};
  const d = data.deltas || {};
  const a = data.active || {};
  const ms = data.mission_status || {};
  const fmt = (n) => Number(n || 0).toLocaleString();
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3 flex-wrap">
        <label className="flex items-center gap-2 text-xs cursor-pointer sp-subtle">
          <input
            type="checkbox"
            checked={excludeTest}
            onChange={(e) => setExcludeTest(e.target.checked)}
            className="accent-orange-500"
            data-testid="admin-exclude-test"
          />
          Exclude obvious test accounts
        </label>
        <span className="text-[11px] sp-subtle italic">
          {excludeTest
            ? "Filter on: hides @test.com / @example.com / synthetic emails. Reflects real adoption."
            : "Filter off: counts include all automated test accounts ever registered."}
        </span>
      </div>
      <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-3">
        <StatCard icon={Users} label="Users" value={fmt(t.users)} sub={`+${d.new_users_7d || 0} last 7d · +${d.new_users_30d || 0} last 30d`} />
        <StatCard icon={Building2} label="Organizations" value={fmt(t.orgs)} sub={`+${d.new_orgs_7d || 0} last 7d · +${d.new_orgs_30d || 0} last 30d`} />
        <StatCard icon={Layers} label="Teams" value={fmt(t.teams)} />
        <StatCard icon={Rocket} label="Missions" value={fmt(t.missions)} sub={`+${d.new_missions_7d || 0} last 7d`} />
        <StatCard icon={MessageSquare} label="Conversations" value={fmt(t.conversations)} />
        <StatCard icon={MessageSquare} label="Messages" value={fmt(t.messages)} />
        <StatCard icon={Activity} label="DAU" value={fmt(a.dau)} sub={`WAU ${fmt(a.wau)} · MAU ${fmt(a.mau)}`} />
        <StatCard icon={Activity} label="Social posts" value={fmt(t.social_posts)} />
      </div>

      <div className="sp-glass-strong rounded-2xl p-5">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm uppercase tracking-widest sp-subtle">New signups · 30 days</h3>
        </div>
        <div className="h-64 -ml-2">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data.signup_series || []}>
              <defs>
                <linearGradient id="signupG" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="var(--primary-bg)" stopOpacity={0.6} />
                  <stop offset="100%" stopColor="var(--primary-bg)" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border-soft)" />
              <XAxis dataKey="date" tick={{ fontSize: 11, fill: "var(--fg-soft)" }} />
              <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: "var(--fg-soft)" }} />
              <Tooltip contentStyle={{ background: "var(--bg)", border: "1px solid var(--border-soft)", borderRadius: 8 }} />
              <Area type="monotone" dataKey="count" stroke="var(--primary-bg)" fill="url(#signupG)" />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="sp-glass-strong rounded-2xl p-5">
        <h3 className="text-sm uppercase tracking-widest sp-subtle mb-3">Mission status (lifetime)</h3>
        <div className="flex flex-wrap gap-2">
          {Object.entries(ms).map(([k, v]) => (
            <span key={k} className="sp-chip text-xs">
              {k} · <strong>{fmt(v)}</strong>
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

function ApiSpendPanel() {
  const [data, setData] = useState(null);
  const [days, setDays] = useState(30);
  useEffect(() => {
    setData(null);
    api.get(`/admin/llm-usage?days=${days}`).then((r) => setData(r.data)).catch(() => setData({}));
  }, [days]);
  const fmt = (n) => Number(n || 0).toLocaleString();
  const cur = (n) => `$${Number(n || 0).toFixed(2)}`;
  const cur4 = (n) => `$${Number(n || 0).toFixed(4)}`;
  const llm = data?.totals || {};
  const ext = data?.external?.totals || {};
  const grand = Number(data?.grand_total_cost || 0);
  const grandCalls = Number(llm.calls || 0) + Number(ext.calls || 0);

  // Merge LLM daily + external daily into a single stacked dataset
  // keyed by date so the chart reads as one continuous spend story
  // rather than two parallel ones the eye has to add up.
  const mergedDaily = useMemo(() => {
    if (!data) return [];
    const byDate = {};
    (data.daily_series || []).forEach((d) => {
      byDate[d.date] = { date: d.date, llm_cost: d.cost, ext_cost: 0 };
    });
    (data.external?.daily_series || []).forEach((d) => {
      if (!byDate[d.date]) byDate[d.date] = { date: d.date, llm_cost: 0 };
      byDate[d.date].ext_cost = d.cost;
    });
    return Object.values(byDate).sort((a, b) => a.date.localeCompare(b.date));
  }, [data]);

  // Merge models + external providers into a single ranked table —
  // operator's "where did the money actually go" view.
  const mergedProviders = useMemo(() => {
    if (!data) return [];
    const rows = [];
    (data.by_model || []).forEach((m) =>
      rows.push({
        kind: "Language model",
        name: m.model || "unknown",
        calls: m.calls,
        cost: m.cost,
      }),
    );
    (data.external?.by_provider || []).forEach((p) =>
      rows.push({
        kind: "Service",
        name: p.provider || "unknown",
        calls: p.calls,
        cost: p.cost,
      }),
    );
    return rows.sort((a, b) => (b.cost || 0) - (a.cost || 0)).slice(0, 15);
  }, [data]);

  if (!data) return <div className="sp-subtle text-sm flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" /> Loading…</div>;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap gap-2 items-center">
        {[7, 30, 90].map((n) => (
          <button
            key={n}
            type="button"
            onClick={() => setDays(n)}
            className={`sp-btn text-xs ${days === n ? "sp-btn-primary" : "sp-btn-ghost"}`}
            data-testid={`admin-llm-days-${n}`}
          >
            {n}d
          </button>
        ))}
        <span className="text-xs sp-subtle ml-auto italic">
          Cost figures are estimates from public list pricing. Exact billing happens upstream via the universal key + provider invoices.
        </span>
      </div>

      {/* Grand total — leads the page so "what is this costing me" is the first answer. */}
      <div className="grid sm:grid-cols-4 gap-3">
        <StatCard icon={DollarSign} label="Total API spend (est.)" value={cur(grand)} sub={`${fmt(grandCalls)} calls total`} />
        <StatCard icon={DollarSign} label="Language models" value={cur(llm.cost)} sub={`${fmt(llm.calls)} calls · ${fmt(llm.tokens)} tokens`} />
        <StatCard icon={DollarSign} label="Other services" value={cur(ext.cost)} sub={`${fmt(ext.calls)} calls · Brave/Perplexity/Resend/kie.ai`} />
        <StatCard icon={Activity} label="$ / call (blended)" value={cur4(grandCalls ? grand / grandCalls : 0)} sub="across both buckets" />
      </div>

      {/* Unified daily spend chart — stacked so the breakdown is visible
          per-day without the eye having to compare two adjacent charts. */}
      <div className="sp-glass-strong rounded-2xl p-5">
        <h3 className="text-sm uppercase tracking-widest sp-subtle mb-3">Daily spend (stacked)</h3>
        <div className="h-56 -ml-2">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={mergedDaily}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border-soft)" />
              <XAxis dataKey="date" tick={{ fontSize: 11, fill: "var(--fg-soft)" }} />
              <YAxis tick={{ fontSize: 11, fill: "var(--fg-soft)" }} />
              <Tooltip
                contentStyle={{ background: "var(--bg)", border: "1px solid var(--border-soft)", borderRadius: 8 }}
                formatter={(v, n) => [`$${Number(v).toFixed(4)}`, n === "llm_cost" ? "Language models" : "Other services"]}
              />
              <Legend wrapperStyle={{ fontSize: 11 }}
                      formatter={(v) => v === "llm_cost" ? "Language models" : "Other services"} />
              <Bar dataKey="llm_cost" stackId="cost" fill="var(--primary-bg)" />
              <Bar dataKey="ext_cost" stackId="cost" fill="#4ade80" />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Top 15 providers ranked across BOTH buckets. The `kind` chip
          tells operators at a glance whether they're looking at a
          token-priced LLM or a per-call utility API. */}
      <div className="grid lg:grid-cols-2 gap-4">
        <div className="sp-glass-strong rounded-2xl p-5">
          <h3 className="text-sm uppercase tracking-widest sp-subtle mb-3">Top providers by spend</h3>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[10px] uppercase tracking-widest sp-subtle">
                <th className="text-left py-1.5">Provider</th>
                <th className="text-left">Kind</th>
                <th className="text-right">Calls</th>
                <th className="text-right">Cost</th>
              </tr>
            </thead>
            <tbody>
              {mergedProviders.map((p, i) => (
                <tr key={`${p.kind}-${p.name}-${i}`} className="border-t border-[var(--border-soft)]"
                    data-testid={`admin-spend-row-${p.name}`}>
                  <td className="py-2 font-mono text-xs truncate max-w-[200px]">{p.name}</td>
                  <td className="text-xs sp-subtle">{p.kind}</td>
                  <td className="text-right">{fmt(p.calls)}</td>
                  <td className="text-right font-medium">{cur(p.cost)}</td>
                </tr>
              ))}
              {!mergedProviders.length && (
                <tr><td colSpan="4" className="text-center py-4 sp-subtle text-xs italic">No spend recorded yet in this window.</td></tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="sp-glass-strong rounded-2xl p-5">
          <h3 className="text-sm uppercase tracking-widest sp-subtle mb-3">Top orgs by spend</h3>
          <p className="text-[11px] sp-subtle mb-2 italic">
            Org-attributed spend currently covers language-model calls only.
          </p>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[10px] uppercase tracking-widest sp-subtle">
                <th className="text-left py-1.5">Org</th>
                <th className="text-right">Calls</th>
                <th className="text-right">Cost</th>
              </tr>
            </thead>
            <tbody>
              {(data.top_orgs || []).map((o) => (
                <tr key={o.org_id} className="border-t border-[var(--border-soft)]">
                  <td className="py-2 truncate max-w-[180px]">{o.org_name}</td>
                  <td className="text-right">{fmt(o.calls)}</td>
                  <td className="text-right font-medium">{cur(o.cost)}</td>
                </tr>
              ))}
              {!(data.top_orgs || []).length && (
                <tr><td colSpan="3" className="text-center py-4 sp-subtle text-xs italic">No org-attributed spend yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Per-user spend leaderboard — merges LLM + external API rows
          server-side so admins can answer "who is burning the budget?"
          at a glance. Anonymous calls (background jobs, public audits)
          are omitted from this view. */}
      <div className="sp-glass-strong rounded-2xl p-5">
        <h3 className="text-sm uppercase tracking-widest sp-subtle mb-3">Top users by spend</h3>
        <p className="text-[11px] sp-subtle mb-2 italic">
          Combined LLM + utility-API spend per user. Background-job
          attribution rolls up to org, not user, so a quiet leaderboard
          doesn't necessarily mean a quiet platform.
        </p>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[10px] uppercase tracking-widest sp-subtle">
                <th className="text-left py-1.5">User</th>
                <th className="text-right">LLM calls</th>
                <th className="text-right">Tokens</th>
                <th className="text-right">LLM $</th>
                <th className="text-right">Other calls</th>
                <th className="text-right">Other $</th>
                <th className="text-right font-semibold">Total $</th>
              </tr>
            </thead>
            <tbody>
              {(data.top_users || []).map((u) => (
                <tr key={u.user_id} className="border-t border-[var(--border-soft)]"
                    data-testid={`admin-user-spend-${u.user_id}`}>
                  <td className="py-2 max-w-[260px]">
                    <div className="truncate font-medium">{u.user_email}</div>
                    {u.user_name && (
                      <div className="text-[11px] sp-subtle truncate">{u.user_name}</div>
                    )}
                  </td>
                  <td className="text-right">{fmt(u.llm_calls)}</td>
                  <td className="text-right">{fmt(u.tokens)}</td>
                  <td className="text-right">{cur4(u.llm_cost)}</td>
                  <td className="text-right">{fmt(u.ext_calls)}</td>
                  <td className="text-right">{cur4(u.ext_cost)}</td>
                  <td className="text-right font-semibold">{cur(u.total_cost)}</td>
                </tr>
              ))}
              {!(data.top_users || []).length && (
                <tr><td colSpan="7" className="text-center py-4 sp-subtle text-xs italic">
                  No user-attributed spend yet in this window.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="sp-glass-strong rounded-2xl p-5">
        <h3 className="text-sm uppercase tracking-widest sp-subtle mb-3">Spend by call kind</h3>
        <div className="flex flex-wrap gap-2">
          {(data.by_kind || []).map((k) => (
            <span key={k.kind} className="sp-chip text-xs">
              {k.kind} · {fmt(k.calls)} calls · <strong>{cur(k.cost)}</strong>
            </span>
          ))}
          {!(data.by_kind || []).length && (
            <span className="sp-subtle text-xs italic">No call-kind breakdown yet.</span>
          )}
        </div>
      </div>

      {/* Pricing reference for the non-LLM utilities — token pricing for
          LLMs varies per-model and is documented upstream, so we don't
          duplicate it here. */}
      <div className="sp-glass-strong rounded-2xl p-5">
        <h3 className="text-sm uppercase tracking-widest sp-subtle mb-3">Pricing reference — utility APIs</h3>
        <p className="text-xs sp-subtle mb-3 leading-relaxed">
          Public list rates we apply when computing estimated cost for non-LLM services.
          Update <code>EXTERNAL_API_RATES</code> when providers change pricing.
        </p>
        <ul className="space-y-1 text-xs">
          {((data.external?.rates) || []).map((r) => (
            <li key={r.provider} className="flex justify-between gap-3 border-t border-white/5 py-1">
              <span className="font-mono">{r.provider}</span>
              <span className="sp-subtle">{r.rate_summary}</span>
            </li>
          ))}
          {!((data.external?.rates) || []).length && (
            <li className="text-center py-3 sp-subtle italic">
              Pricing table unavailable.
            </li>
          )}
        </ul>
      </div>
    </div>
  );
}

function PaginatedTable({ endpoint, columns, testidPrefix, emptyMsg }) {
  const [data, setData] = useState({ rows: [], total: 0, page: 1, page_size: 25 });
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search), 350);
    return () => clearTimeout(t);
  }, [search]);
  useEffect(() => {
    setLoading(true);
    api.get(`${endpoint}?page=${page}&page_size=25&search=${encodeURIComponent(debouncedSearch)}`)
      .then((r) => setData(r.data))
      .finally(() => setLoading(false));
  }, [endpoint, page, debouncedSearch]);
  const pages = Math.max(1, Math.ceil((data.total || 0) / 25));
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <div className="relative flex-1 max-w-sm">
          <Search className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 sp-subtle" />
          <input
            value={search}
            onChange={(e) => { setSearch(e.target.value); setPage(1); }}
            placeholder="Search by name or email…"
            className="w-full rounded-xl pl-9 pr-3 py-2 text-sm"
            style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
            data-testid={`${testidPrefix}-search`}
          />
        </div>
        <span className="text-xs sp-subtle ml-auto">{data.total} total</span>
      </div>
      <div className="sp-glass-strong rounded-2xl overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-[10px] uppercase tracking-widest sp-subtle border-b border-[var(--border-soft)]">
              {columns.map((c) => <th key={c.key} className={`py-2 px-3 ${c.align === "right" ? "text-right" : "text-left"}`}>{c.label}</th>)}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={columns.length} className="text-center py-6 sp-subtle"><Loader2 className="w-4 h-4 animate-spin inline" /></td></tr>
            ) : (data.rows || []).length === 0 ? (
              <tr><td colSpan={columns.length} className="text-center py-6 sp-subtle text-xs italic">{emptyMsg || "No data"}</td></tr>
            ) : (
              (data.rows || []).map((row) => (
                <tr key={row.id} className="border-t border-[var(--border-soft)] hover:bg-white/[0.02]" data-testid={`${testidPrefix}-row-${row.id}`}>
                  {columns.map((c) => (
                    <td key={c.key} className={`py-2 px-3 ${c.align === "right" ? "text-right" : ""}`}>
                      {c.render ? c.render(row) : (row[c.key] ?? "")}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      <div className="flex items-center justify-between text-xs sp-subtle">
        <span>Page {page} of {pages}</span>
        <div className="flex gap-2">
          <button disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))} className="sp-btn sp-btn-ghost text-xs disabled:opacity-40">Prev</button>
          <button disabled={page >= pages} onClick={() => setPage((p) => Math.min(pages, p + 1))} className="sp-btn sp-btn-ghost text-xs disabled:opacity-40">Next</button>
        </div>
      </div>
    </div>
  );
}

function MissionsPanel() {
  const [rows, setRows] = useState([]);
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    setLoading(true);
    api.get(`/admin/missions?limit=50${status ? `&status=${status}` : ""}`)
      .then((r) => setRows(r.data.rows || []))
      .finally(() => setLoading(false));
  }, [status]);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        {["", "running", "finished", "failed", "cancelled", "awaiting_user"].map((s) => (
          <button key={s} type="button" onClick={() => setStatus(s)}
                  className={`sp-btn text-xs ${status === s ? "sp-btn-primary" : "sp-btn-ghost"}`}
                  data-testid={`admin-mission-filter-${s || "all"}`}>
            {s || "All"}
          </button>
        ))}
      </div>
      <div className="sp-glass-strong rounded-2xl overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-[10px] uppercase tracking-widest sp-subtle border-b border-[var(--border-soft)]">
              <th className="text-left py-2 px-3">Goal</th>
              <th className="text-left py-2 px-3">Org</th>
              <th className="text-left py-2 px-3">Agent</th>
              <th className="text-left py-2 px-3">Status</th>
              <th className="text-right py-2 px-3">Steps</th>
              <th className="text-left py-2 px-3">Created</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan="6" className="text-center py-6"><Loader2 className="w-4 h-4 animate-spin inline" /></td></tr>
            ) : rows.length === 0 ? (
              <tr><td colSpan="6" className="text-center py-6 sp-subtle italic">No missions match.</td></tr>
            ) : rows.map((m) => (
              <tr key={m.id} className="border-t border-[var(--border-soft)] hover:bg-white/[0.02]" data-testid={`admin-mission-${m.id}`}>
                <td className="py-2 px-3 max-w-md truncate" title={m.goal}>{m.goal}</td>
                <td className="py-2 px-3 truncate max-w-[140px]">{m.org_name}</td>
                <td className="py-2 px-3">{m.lead_agent_name}</td>
                <td className="py-2 px-3"><span className="sp-chip text-[10px]">{m.status}</span></td>
                <td className="py-2 px-3 text-right">{m.iteration_count}</td>
                <td className="py-2 px-3 text-xs sp-subtle">{m.created_at ? formatLocalDateTime(m.created_at) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function AdminDashboard() {
  const { user, loading } = useAuth();
  const [tab, setTab] = useState("overview");

  const orgCols = useMemo(() => [
    { key: "name", label: "Org" },
    { key: "owner_email", label: "Owner" },
    { key: "members", label: "Members", align: "right" },
    { key: "missions", label: "Missions", align: "right" },
    { key: "social_posts", label: "Posts", align: "right" },
    { key: "llm_calls", label: "LLM calls", align: "right" },
    { key: "llm_cost", label: "LLM cost", align: "right",
      render: (r) => `$${Number(r.llm_cost || 0).toFixed(2)}` },
    { key: "created_at", label: "Created",
      render: (r) => r.created_at ? formatLocalDate(r.created_at) : "—" },
  ], []);

  if (loading) return null;
  if (!user?.is_superadmin) {
    return <Navigate to="/app" replace />;
  }

  return (
    <div className="min-h-screen" style={{ background: "var(--bg)", color: "var(--fg)" }}>
      <TopBar />
      <main className="max-w-7xl mx-auto px-4 sm:px-6 py-6 sm:py-8" data-testid="admin-dashboard">
        <div className="mb-6">
          <div className="flex items-center gap-2 mb-1">
            <ShieldCheck className="w-4 h-4 text-emerald-400" />
            <span className="text-xs uppercase tracking-widest sp-subtle">Superadmin</span>
          </div>
          <h1 className="sp-display text-3xl">Admin dashboard</h1>
          <p className="text-sm sp-subtle mt-1">Activity, API spend, and platform health.</p>
        </div>

        <div className="flex flex-wrap gap-2 mb-6">
          {TAB_DEFS.map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => setTab(t.id)}
              className={`sp-btn text-xs ${tab === t.id ? "sp-btn-primary" : "sp-btn-ghost"}`}
              data-testid={`admin-tab-${t.id}`}
            >
              <t.icon className="w-3.5 h-3.5" /> {t.label}
            </button>
          ))}
        </div>

        {tab === "overview" && <OverviewPanel />}
        {tab === "spend" && <ApiSpendPanel />}
        {tab === "orgs" && <PaginatedTable endpoint="/admin/orgs" columns={orgCols} testidPrefix="admin-org" emptyMsg="No organizations yet." />}
        {tab === "users" && <AdminUsersPanel currentUserId={user?.id} />}
        {tab === "missions" && <MissionsPanel />}
        {tab === "audits" && <AdminAuditsPanel />}
        {tab === "access" && <AdminAccessCodesPanel />}
      </main>
    </div>
  );
}
