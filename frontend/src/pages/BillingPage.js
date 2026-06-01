import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import {
  Loader2, Check, AlertTriangle, CreditCard, TrendingUp,
  Receipt, UserPlus, Sparkles, ArrowUpRight,
} from "lucide-react";
import api from "../lib/api";
import TopBar from "../components/TopBar";

/**
 * BillingPage — single dashboard for everything money in an org:
 *   • Current plan + paid-until banner
 *   • This-month LLM spend vs included budget (with a visual bar)
 *   • Projected overage with markup math made explicit
 *   • Outstanding overage invoice (settle in one click via Stripe)
 *   • Plan cards for upgrades (Spark / Temper / Refine)
 *   • Extra-seat purchase
 *   • Recent Stripe transactions (audit log)
 *
 * The page also handles the Stripe redirect by polling
 * /api/billing/status/:session_id with backoff until paid|expired —
 * the same component that initiates checkout reconciles its result.
 */
export default function BillingPage() {
  const { orgId } = useParams();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const sessionId = params.get("session_id");
  const cancelled = params.get("cancelled");

  const [loading, setLoading] = useState(true);
  const [summary, setSummary] = useState(null);
  const [plans, setPlans] = useState([]);
  const [busy, setBusy] = useState(""); // which CTA is mid-flight
  const [polling, setPolling] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [s, p] = await Promise.all([
        api.get(`/orgs/${orgId}/billing/summary`),
        api.get(`/billing/plans`),
      ]);
      setSummary(s.data);
      setPlans(p.data?.plans || []);
    } catch (err) {
      const code = err?.response?.status;
      if (code === 403) {
        toast.error("Only owners/admins can view billing.");
        navigate(`/app/org/${orgId}`);
      } else {
        toast.error(err?.response?.data?.detail || "Could not load billing.");
      }
    } finally {
      setLoading(false);
    }
  }, [orgId, navigate]);

  useEffect(() => { refresh(); }, [refresh]);

  // Stripe redirect — poll status with backoff until terminal.
  useEffect(() => {
    if (!sessionId) {
      if (cancelled) {
        toast.info("Checkout cancelled.");
        setParams({}, { replace: true });
      }
      return;
    }
    let cancelledLocal = false;
    setPolling(true);
    const attempts = [1500, 1500, 2000, 2500, 3000, 4000]; // ~14.5s

    (async () => {
      for (let i = 0; i < attempts.length; i++) {
        if (cancelledLocal) return;
        try {
          const { data } = await api.get(`/billing/status/${sessionId}`);
          if (data?.payment_status === "paid") {
            toast.success(
              data.kind === "plan" ? "Plan upgraded. Welcome aboard." :
              data.kind === "seats" ? "Extra seats added to your plan." :
              "Overage invoice settled. Thanks!"
            );
            await refresh();
            setPolling(false);
            setParams({}, { replace: true });
            return;
          }
          if (data?.status === "expired") {
            toast.error("Checkout expired. Please try again.");
            setPolling(false);
            setParams({}, { replace: true });
            return;
          }
        } catch (err) {
          // Status endpoint can 404 briefly for very-new sessions on
          // the webhook path — keep polling.
        }
        await new Promise(r => setTimeout(r, attempts[i]));
      }
      // Timed out — Stripe should fire the webhook eventually.
      toast.info("Payment is still processing. We'll update once it clears.");
      setPolling(false);
      setParams({}, { replace: true });
    })();

    return () => { cancelledLocal = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  // Memoize derived spend/overage math so the bar stays stable across renders.
  const meter = useMemo(() => {
    if (!summary) return null;
    const spent = summary.this_month_spend_usd || 0;
    const included = summary.included_llm_budget_usd || 0;
    const pct = included > 0 ? Math.min(100, (spent / included) * 100) : 0;
    return {
      spent: spent.toFixed(2),
      included: included.toFixed(2),
      pct: pct.toFixed(0),
      over: spent > included,
      overageRaw: (summary.projected_overage_raw_usd || 0).toFixed(2),
      overageBill: (summary.projected_overage_billable_usd || 0).toFixed(2),
      markup: summary.overage_markup || 2,
    };
  }, [summary]);

  const startCheckout = useCallback(async (route, body = {}, label = "") => {
    setBusy(label);
    try {
      const origin = window.location.origin;
      const { data } = await api.post(route, { ...body, origin_url: origin });
      if (data?.url) {
        window.location.href = data.url;
      } else {
        toast.error("No checkout URL returned.");
        setBusy("");
      }
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not start checkout.");
      setBusy("");
    }
  }, []);

  const upgradeTo = (planId) => startCheckout(
    `/orgs/${orgId}/billing/checkout/plan`,
    { plan_id: planId },
    `plan:${planId}`,
  );
  const buySeats = (quantity) => startCheckout(
    `/orgs/${orgId}/billing/checkout/seats`,
    { quantity },
    `seats:${quantity}`,
  );
  const settleOverage = () => startCheckout(
    `/orgs/${orgId}/billing/checkout/overage`, {}, "overage",
  );

  if (loading) {
    return (
      <div className="min-h-screen">
        <TopBar>Billing</TopBar>
        <div className="flex items-center justify-center py-32" data-testid="billing-loading">
          <Loader2 className="w-6 h-6 animate-spin sp-subtle" />
        </div>
      </div>
    );
  }

  const currentPlan = summary?.plan;
  const outstanding = summary?.outstanding_overage;
  const paidUntil = summary?.plan_paid_until
    ? new Date(summary.plan_paid_until).toLocaleDateString()
    : null;

  return (
    <div className="min-h-screen">
      <TopBar>Billing</TopBar>
      <main className="max-w-5xl mx-auto px-4 sm:px-6 py-6 sm:py-10" data-testid="billing-page">
        <div className="mb-8">
          <div className="text-[10px] uppercase tracking-widest sp-subtle">Billing</div>
          <h1 className="text-3xl sm:text-4xl font-medium tracking-tight">Plan &amp; usage</h1>
          <p className="text-sm sp-subtle mt-1">
            Pay only for what your agents actually use. No annual lock-in, no hidden markup.
          </p>
        </div>

        {polling && (
          <div
            className="sp-glass rounded-2xl p-4 mb-6 flex items-center gap-3 text-sm"
            data-testid="billing-polling"
          >
            <Loader2 className="w-4 h-4 animate-spin" />
            Confirming your payment with Stripe…
          </div>
        )}

        {/* Current plan card */}
        <section className="sp-glass-strong rounded-2xl p-5 sm:p-6 mb-6" data-testid="current-plan-card">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <div className="text-xs uppercase tracking-widest sp-subtle flex items-center gap-2">
                <Sparkles className="w-3.5 h-3.5" /> Current plan
              </div>
              <h2 className="sp-display text-2xl mt-1">{currentPlan?.name || "Spark"}</h2>
              <div className="text-sm sp-subtle mt-1">{currentPlan?.tagline}</div>
              <div className="text-xs sp-subtle mt-2">
                ${currentPlan?.monthly_usd?.toFixed(0)}/mo · {
                  currentPlan?.seats_included >= 1000
                    ? "Unlimited seats"
                    : `${currentPlan?.seats_included} seat${currentPlan?.seats_included === 1 ? "" : "s"} included`
                }
                {(summary?.extra_seats_quantity || 0) > 0 && (
                  <> · <span className="text-orange-300">+{summary.extra_seats_quantity} extra</span></>
                )}
              </div>
            </div>
            <div className="text-right">
              <PlanStatusPill status={summary?.plan_status} paidUntil={paidUntil} />
            </div>
          </div>
        </section>

        {/* Usage meter */}
        {meter && (
          <section className="sp-glass-strong rounded-2xl p-5 sm:p-6 mb-6" data-testid="usage-meter-card">
            <div className="text-xs uppercase tracking-widest sp-subtle flex items-center gap-2 mb-3">
              <TrendingUp className="w-3.5 h-3.5" /> This month's agent compute
            </div>
            <div className="flex items-baseline justify-between mb-2">
              <div className="text-2xl font-medium">${meter.spent}</div>
              <div className="text-xs sp-subtle">of ${meter.included} included</div>
            </div>
            <div className="h-2 rounded-full overflow-hidden bg-white/5 mb-2">
              <div
                className={`h-full transition-[width] ${meter.over ? "bg-orange-400" : "bg-emerald-400"}`}
                style={{ width: `${meter.pct}%` }}
                data-testid="usage-meter-bar"
              />
            </div>
            {meter.over ? (
              <div className="mt-3 text-sm">
                <div className="flex items-center gap-2 text-orange-300">
                  <AlertTriangle className="w-3.5 h-3.5" />
                  Projected overage this month
                </div>
                <div className="sp-subtle text-xs mt-1 leading-relaxed">
                  ${meter.overageRaw} raw cost × {meter.markup}× markup =
                  {" "}<span className="text-white">${meter.overageBill}</span> billable on the 1st.
                </div>
              </div>
            ) : (
              <div className="mt-3 text-xs sp-subtle">
                You're inside your included budget. Overage only kicks in past ${meter.included}.
              </div>
            )}
          </section>
        )}

        {/* Outstanding overage */}
        {outstanding && (
          <section
            className="sp-glass-strong rounded-2xl p-5 sm:p-6 mb-6 border border-orange-400/30"
            data-testid="outstanding-overage-card"
          >
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <div className="text-xs uppercase tracking-widest text-orange-300 flex items-center gap-2">
                  <Receipt className="w-3.5 h-3.5" /> Outstanding invoice
                </div>
                <div className="text-2xl font-medium mt-1">
                  ${outstanding.amount_due_usd?.toFixed(2)}
                </div>
                <div className="text-xs sp-subtle mt-1">
                  For {outstanding.period_key} · raw cost ${outstanding.raw_cost_usd?.toFixed(2)}
                  {" "}vs included ${outstanding.included_usd?.toFixed(2)}.
                </div>
              </div>
              <button
                onClick={settleOverage}
                disabled={busy === "overage"}
                className="sp-btn sp-btn-primary text-sm"
                data-testid="settle-overage-btn"
              >
                {busy === "overage" ? <Loader2 className="w-4 h-4 animate-spin" /> : <CreditCard className="w-4 h-4" />}
                Pay invoice
              </button>
            </div>
          </section>
        )}

        {/* Plan cards */}
        <section className="mb-6" data-testid="plan-cards">
          <h3 className="text-sm uppercase tracking-widest sp-subtle mb-3">Change plan</h3>
          <div className="grid md:grid-cols-3 gap-4">
            {plans.map(p => (
              <PlanCard
                key={p.id}
                plan={p}
                current={currentPlan?.id === p.id}
                busy={busy === `plan:${p.id}`}
                onSelect={() => upgradeTo(p.id)}
              />
            ))}
          </div>
        </section>

        {/* Extra seats */}
        {currentPlan && currentPlan.extra_seat_usd > 0 && (
          <section className="sp-glass-strong rounded-2xl p-5 sm:p-6 mb-6" data-testid="seats-card">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <div className="text-xs uppercase tracking-widest sp-subtle flex items-center gap-2">
                  <UserPlus className="w-3.5 h-3.5" /> Extra seats
                </div>
                <div className="text-sm mt-2">
                  Add seats à-la-carte on top of your {currentPlan.name} plan —
                  {" "}<span className="text-orange-300">${currentPlan.extra_seat_usd}/seat/mo</span>.
                </div>
                <div className="text-xs sp-subtle mt-1">
                  Already added: {summary.extra_seats_quantity || 0} extra seat(s).
                </div>
              </div>
              <div className="flex items-center gap-2">
                {[1, 3, 5].map(q => (
                  <button
                    key={q}
                    onClick={() => buySeats(q)}
                    disabled={busy === `seats:${q}`}
                    className="sp-btn sp-btn-ghost text-sm"
                    data-testid={`buy-seats-${q}`}
                  >
                    {busy === `seats:${q}`
                      ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                      : <>+{q} seat{q > 1 ? "s" : ""}</>}
                  </button>
                ))}
              </div>
            </div>
          </section>
        )}

        {/* Recent transactions */}
        <section className="sp-glass rounded-2xl p-5 sm:p-6" data-testid="transactions-card">
          <h3 className="text-xs uppercase tracking-widest sp-subtle mb-3 flex items-center gap-2">
            <Receipt className="w-3.5 h-3.5" /> Recent transactions
          </h3>
          {(summary?.recent_transactions || []).length === 0 ? (
            <div className="text-sm sp-subtle">No charges yet.</div>
          ) : (
            <ul className="divide-y divide-white/5">
              {summary.recent_transactions.map((t, idx) => (
                <li key={t.session_id || idx} className="py-2 flex items-center justify-between text-sm">
                  <div>
                    <div className="capitalize">{t.kind}{t.plan_id ? ` · ${t.plan_id}` : ""}</div>
                    <div className="text-[11px] sp-subtle">
                      {t.created_at ? new Date(t.created_at).toLocaleString() : ""}
                    </div>
                  </div>
                  <div className="flex items-center gap-3">
                    <div>${t.amount_usd?.toFixed(2)}</div>
                    <TxnPill status={t.payment_status} />
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </main>
    </div>
  );
}

// ─── Sub-components ─────────────────────────────────────────────────────

function PlanStatusPill({ status, paidUntil }) {
  if (status === "paid") {
    return (
      <div className="inline-flex items-center gap-1.5 text-xs px-2 py-1 rounded-full bg-emerald-500/15 text-emerald-300"
           data-testid="plan-status-paid">
        <Check className="w-3 h-3" /> Active{paidUntil ? ` · renews ${paidUntil}` : ""}
      </div>
    );
  }
  return (
    <div className="inline-flex items-center gap-1.5 text-xs px-2 py-1 rounded-full bg-orange-500/15 text-orange-300"
         data-testid="plan-status-unpaid">
      <AlertTriangle className="w-3 h-3" /> Unpaid
    </div>
  );
}

function TxnPill({ status }) {
  const styles =
    status === "paid" ? "bg-emerald-500/15 text-emerald-300"
    : status === "initiated" || status === "pending" ? "bg-white/10 sp-subtle"
    : "bg-rose-500/15 text-rose-300";
  return (
    <span className={`text-[10px] uppercase tracking-widest px-2 py-0.5 rounded-full ${styles}`}>
      {status || "unknown"}
    </span>
  );
}

function PlanCard({ plan, current, busy, onSelect }) {
  return (
    <div
      className={`sp-glass-strong rounded-2xl p-5 flex flex-col ${current ? "ring-1 ring-orange-300/40" : ""}`}
      data-testid={`plan-card-${plan.id}`}
    >
      <div className="text-xs uppercase tracking-widest sp-subtle">{plan.name}</div>
      <div className="text-3xl font-medium mt-1">
        ${plan.monthly_usd.toFixed(0)}
        <span className="text-sm sp-subtle">/mo</span>
      </div>
      <div className="text-xs sp-subtle mt-1">{plan.tagline}</div>
      <ul className="text-sm space-y-1.5 mt-4 flex-1">
        {plan.features.map((f, i) => (
          <li key={i} className="flex items-start gap-2">
            <Check className="w-3.5 h-3.5 mt-1 text-emerald-300 flex-shrink-0" />
            <span>{f}</span>
          </li>
        ))}
      </ul>
      <button
        onClick={onSelect}
        disabled={current || busy}
        className={`mt-5 sp-btn ${current ? "sp-btn-ghost" : "sp-btn-primary"} text-sm disabled:opacity-60 disabled:cursor-not-allowed`}
        data-testid={`plan-select-${plan.id}`}
      >
        {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : current ? <Check className="w-4 h-4" /> : <ArrowUpRight className="w-4 h-4" />}
        {current ? "Current plan" : `Choose ${plan.name}`}
      </button>
    </div>
  );
}
