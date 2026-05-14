import React, { useEffect, useState } from "react";
import {
  DollarSign, Users, FileBarChart, Rocket, Loader2, ChevronDown, ChevronUp,
} from "lucide-react";
import { toast } from "sonner";
import api from "../lib/api";

/**
 * SpendDashboard — Phase A billing visibility for org owners/admins.
 *
 * Surfaces what each org is actually spending on LLM calls (the cost
 * we'd be invoicing once Stripe is wired up in Phase B), broken down
 * per-member so an owner can see who's driving spend inside their
 * workspace. Read-only for now — Phase B (June 1 target) layers
 * Stripe subscriptions + metered seat billing on top of this data.
 *
 * Endpoint: GET /api/orgs/{org_id}/spend?period={current_month|previous_month}
 * Gated: 403 for non-admin/owner members.
 */
export default function SpendDashboard({ orgId }) {
  const [period, setPeriod] = useState("current_month");
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    (async () => {
      try {
        const { data: payload } = await api.get(
          `/orgs/${orgId}/spend`, { params: { period } }
        );
        if (!cancelled) setData(payload);
      } catch (err) {
        if (cancelled) return;
        if (err?.response?.status === 403) {
          // Members can't see whole-org spend — quietly hide the dashboard.
          setData(null);
        } else {
          toast.error("Couldn't load spend data.");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [orgId, period]);

  if (loading) {
    return (
      <div className="sp-glass rounded-2xl p-5 flex items-center gap-2 text-sm sp-subtle"
           data-testid="spend-dashboard-loading">
        <Loader2 className="w-4 h-4 animate-spin" /> Loading spend…
      </div>
    );
  }
  if (!data) return null;  // 403 — member-tier view, hide entirely.

  const { totals, by_user, by_kind, seat_status } = data;
  const cost = totals.cost_usd || 0;
  const isCurrent = period === "current_month";

  return (
    <section
      className="sp-glass-strong rounded-2xl p-5 space-y-4"
      data-testid="spend-dashboard"
    >
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h3 className="text-base font-medium flex items-center gap-2">
            <DollarSign className="w-4 h-4 text-orange-300" />
            Workspace spend
          </h3>
          <p className="text-[11px] sp-subtle mt-0.5">
            {isCurrent ? "This month, so far." : "Previous calendar month."}{" "}
            Phase B (Stripe billing) targets June 1 — until then, this is for visibility only.
          </p>
        </div>
        <div className="flex gap-1 text-[11px]" role="tablist">
          <button
            onClick={() => setPeriod("current_month")}
            className={`px-2.5 py-1 rounded-full transition ${
              isCurrent
                ? "bg-orange-500/20 text-orange-200 border border-orange-500/40"
                : "sp-subtle border border-white/10 hover:bg-white/10"
            }`}
            data-testid="spend-period-current"
          >
            This month
          </button>
          <button
            onClick={() => setPeriod("previous_month")}
            className={`px-2.5 py-1 rounded-full transition ${
              !isCurrent
                ? "bg-orange-500/20 text-orange-200 border border-orange-500/40"
                : "sp-subtle border border-white/10 hover:bg-white/10"
            }`}
            data-testid="spend-period-prev"
          >
            Last month
          </button>
        </div>
      </div>

      {/* KPI strip */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
        <Kpi
          label="LLM spend"
          value={`$${cost.toFixed(2)}`}
          hint={`${(totals.llm_calls || 0).toLocaleString()} calls`}
          accent="text-orange-300"
          testId="spend-kpi-cost"
        />
        <Kpi
          label="Members"
          value={(totals.members || 0).toString()}
          hint={
            seat_status
              ? `${seat_status.seats_used} of ${seat_status.seats_included} seats`
              : ""
          }
          icon={<Users className="w-3.5 h-3.5" />}
          testId="spend-kpi-members"
        />
        <Kpi
          label="Missions"
          value={(totals.missions_started || 0).toString()}
          icon={<Rocket className="w-3.5 h-3.5" />}
          testId="spend-kpi-missions"
        />
        <Kpi
          label="Signal audits"
          value={(totals.audits_started || 0).toString()}
          icon={<FileBarChart className="w-3.5 h-3.5" />}
          testId="spend-kpi-audits"
        />
      </div>

      {/* By-user breakdown — the punchline for owners */}
      {by_user.length > 0 && (
        <div data-testid="spend-by-user">
          <button
            onClick={() => setExpanded((v) => !v)}
            className="w-full flex items-center justify-between text-[11px] uppercase tracking-widest sp-subtle hover:text-white transition"
          >
            <span>Spend by member ({by_user.length})</span>
            {expanded ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
          </button>
          {expanded && (
            <div className="mt-2 rounded-xl overflow-hidden border border-white/5">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-white/5 sp-subtle">
                    <th className="text-left px-3 py-2 font-normal">Member</th>
                    <th className="text-right px-3 py-2 font-normal">Spend</th>
                    <th className="text-right px-3 py-2 font-normal">Calls</th>
                    <th className="text-right px-3 py-2 font-normal hidden sm:table-cell">% of total</th>
                  </tr>
                </thead>
                <tbody>
                  {by_user.map((row) => {
                    const pct = cost > 0 ? (row.cost_usd / cost) * 100 : 0;
                    return (
                      <tr key={row.user_id} className="border-b border-white/5 last:border-0">
                        <td className="px-3 py-2">
                          <div className="font-medium truncate">{row.name}</div>
                          <div className="text-[10px] sp-subtle truncate">{row.email}</div>
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums">
                          ${row.cost_usd.toFixed(2)}
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums sp-subtle">
                          {row.calls}
                        </td>
                        <td className="px-3 py-2 text-right hidden sm:table-cell">
                          <div className="inline-flex items-center gap-2">
                            <div className="w-16 h-1.5 rounded-full bg-white/10 overflow-hidden">
                              <div
                                className="h-full bg-orange-400/60"
                                style={{ width: `${Math.min(100, pct).toFixed(1)}%` }}
                              />
                            </div>
                            <span className="tabular-nums sp-subtle text-[10px] w-8 text-right">
                              {pct.toFixed(0)}%
                            </span>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* By-kind breakdown — what's actually driving the spend */}
      {by_kind.length > 0 && (
        <div className="flex flex-wrap gap-1.5 pt-1" data-testid="spend-by-kind">
          {by_kind.map((k) => (
            <span
              key={k.label}
              className="text-[10px] px-2 py-1 rounded-full bg-white/5 border border-white/10 sp-subtle"
            >
              <span className="text-orange-300 font-medium">${k.cost_usd.toFixed(2)}</span>{" "}
              {k.label.replace(/_/g, " ")} · {k.calls}
            </span>
          ))}
        </div>
      )}

      {/* Soft callout — what this becomes after Phase B */}
      {cost > 0 && (
        <p className="text-[10px] sp-subtle italic border-t border-white/5 pt-2">
          Once Stripe billing ships (target: June 1, 2026), this total becomes
          your invoice. Members above your plan's seat allowance will appear as
          separate per-seat line items.
        </p>
      )}
    </section>
  );
}

function Kpi({ label, value, hint, icon, accent, testId }) {
  return (
    <div className="sp-glass rounded-xl p-3" data-testid={testId}>
      <div className="text-[10px] uppercase tracking-widest sp-subtle flex items-center gap-1.5 mb-1">
        {icon}
        {label}
      </div>
      <div className={`text-xl font-medium tabular-nums ${accent || ""}`}>
        {value}
      </div>
      {hint && (
        <div className="text-[10px] sp-subtle mt-0.5">{hint}</div>
      )}
    </div>
  );
}
