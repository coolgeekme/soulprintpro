import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams, Link } from "react-router-dom";
import { toast } from "sonner";
import { createPortal } from "react-dom";
import {
  Loader2, Check, AlertTriangle, CreditCard, TrendingUp,
  Receipt, Sparkles, ArrowUpRight, ExternalLink, Mail,
  Shield, X, Settings, Layers,
} from "lucide-react";
import api from "../lib/api";
import TopBar from "../components/TopBar";

/**
 * BillingPage — single user-level dashboard for subscription + usage.
 *
 * Subscriptions live on the USER, not the org. One subscription
 * unlocks N workspaces (Spark=1, Temper=3, Refine=unlimited). The
 * card shows: current plan, workspaces used vs limit, this-month
 * pooled spend across all workspaces, outstanding invoice, plan
 * picker, and the Refine "contact sales" form.
 *
 * Three flows live here:
 *   1. **Subscribe** (Spark + Temper) — opens Stripe Checkout in
 *      subscription mode after the user explicitly agrees to a
 *      verbatim disclosure block (FTC ROSCA-style affirmative
 *      consent). Disclosure text is persisted on payment_transactions.
 *   2. **Contact sales** (Refine) — small modal form that POSTs to
 *      `/api/billing/contact-sales`; backend stores the lead and
 *      emails team@archeforge.com via Resend.
 *   3. **Manage subscription** — opens Stripe Customer Portal so the
 *      user can cancel / update card / view invoices.
 */
export default function BillingPage() {
  const [params, setParams] = useSearchParams();
  const sessionId = params.get("session_id");
  const cancelled = params.get("cancelled");
  const subscribeIntent = params.get("subscribe"); // 'spark' | 'temper' from /auth?plan=…
  const contactIntent = params.get("contact"); // '1' from /pricing → Refine "Talk to us"

  const [loading, setLoading] = useState(true);
  const [summary, setSummary] = useState(null);
  const [plans, setPlans] = useState([]);
  const [busy, setBusy] = useState("");
  const [polling, setPolling] = useState(false);

  const [pendingPlan, setPendingPlan] = useState(null);
  const [showSales, setShowSales] = useState(false);
  // Monthly/annual toggle — affects both the price label and the
  // cadence we pass to /api/billing/subscribe.
  const [cadence, setCadence] = useState("monthly");

  const refresh = useCallback(async () => {
    try {
      const [s, p] = await Promise.all([
        api.get(`/billing/summary`),
        api.get(`/billing/plans`),
      ]);
      setSummary(s.data);
      setPlans(p.data?.plans || []);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not load billing.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // Open the consent modal automatically when the user arrived from
  // /pricing → /auth → here with a `?subscribe=spark|temper` hint.
  useEffect(() => {
    if (!subscribeIntent || !plans.length) return;
    const target = plans.find(p => p.id === subscribeIntent && p.recurring);
    if (!target) return;
    setPendingPlan(target);
    const next = new URLSearchParams(params);
    next.delete("subscribe");
    setParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subscribeIntent, plans.length]);

  // Open the Refine "contact sales" modal when the user arrived from
  // /pricing → /auth → here with a `?contact=1` hint.
  useEffect(() => {
    if (!contactIntent) return;
    setShowSales(true);
    const next = new URLSearchParams(params);
    next.delete("contact");
    setParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contactIntent]);

  // After Stripe redirects back, poll status until paid.
  useEffect(() => {
    if (!sessionId) {
      if (cancelled) {
        toast.info("Checkout cancelled — no charge was made.");
        setParams({}, { replace: true });
      }
      return;
    }
    let alive = true;
    setPolling(true);
    const waits = [1500, 1500, 2000, 2500, 3000, 4000];

    (async () => {
      for (let i = 0; i < waits.length; i++) {
        if (!alive) return;
        try {
          const { data } = await api.get(`/billing/status/${sessionId}`);
          if (data?.payment_status === "paid") {
            toast.success("Subscription active. Welcome aboard.");
            await refresh();
            setPolling(false);
            setParams({}, { replace: true });
            return;
          }
          if (data?.status === "expired") {
            toast.error("Checkout expired — please try again.");
            setPolling(false);
            setParams({}, { replace: true });
            return;
          }
        } catch {/* keep polling */}
        await new Promise(r => setTimeout(r, waits[i]));
      }
      toast.info("Payment still processing — we'll update when it clears.");
      setPolling(false);
      setParams({}, { replace: true });
    })();

    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

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

  const confirmSubscribe = useCallback(async () => {
    if (!pendingPlan) return;
    setBusy(`plan:${pendingPlan.id}`);
    try {
      const origin = window.location.origin;
      const consentText = buildConsentText(pendingPlan, cadence);
      const { data } = await api.post(`/billing/subscribe`, {
        plan_id: pendingPlan.id,
        origin_url: origin,
        consent_text: consentText,
        cadence,
      });
      if (data?.url) {
        window.location.href = data.url;
      } else {
        toast.error("No checkout URL returned.");
        setBusy("");
      }
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not start checkout.");
      setBusy("");
      setPendingPlan(null);
    }
  }, [pendingPlan, cadence]);

  const openManagePortal = useCallback(async () => {
    setBusy("portal");
    try {
      const { data } = await api.post(`/billing/portal`, {
        origin_url: window.location.origin,
      });
      if (data?.url) {
        window.location.href = data.url;
      } else {
        toast.error("Could not open the billing portal.");
        setBusy("");
      }
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not open the billing portal.");
      setBusy("");
    }
  }, []);

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
  const isStaff = !!summary?.is_staff;
  const isSubscribed = summary?.subscription_status === "active"
    || summary?.subscription_status === "trialing";
  const cancelling = !!summary?.cancel_at_period_end;
  const orgsUsed = summary?.orgs_used ?? 0;
  const orgsLimit = summary?.orgs_limit ?? 1;
  const atOrgLimit = !isStaff && orgsUsed >= orgsLimit;

  return (
    <div className="min-h-screen">
      <TopBar>Billing</TopBar>
      <main className="max-w-5xl mx-auto px-4 sm:px-6 py-6 sm:py-10" data-testid="billing-page">
        <div className="mb-8">
          <div className="text-[10px] uppercase tracking-widest sp-subtle">Billing</div>
          <h1 className="text-3xl sm:text-4xl font-medium tracking-tight">Plan &amp; usage</h1>
          <p className="text-sm sp-subtle mt-1">
            Subscribe monthly. Cancel anytime — no annual lock-in, no hidden markup.
            One subscription unlocks every organization you create.
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

        {/* Current plan + manage button */}
        <section className="sp-glass-strong rounded-2xl p-5 sm:p-6 mb-6" data-testid="current-plan-card">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <div className="text-xs uppercase tracking-widest sp-subtle flex items-center gap-2">
                <Sparkles className="w-3.5 h-3.5" /> Current plan
              </div>
              <h2 className="sp-display text-2xl mt-1">{currentPlan?.name || "Spark"}</h2>
              <div className="text-sm sp-subtle mt-1">{currentPlan?.tagline}</div>
            </div>
            <div className="flex flex-col items-end gap-2">
              <PlanStatusPill
                status={summary?.plan_status}
                cancelling={cancelling}
                paidUntil={paidUntil}
                isStaff={isStaff}
              />
              {(isSubscribed || summary?.has_stripe_customer) && (
                <button
                  type="button"
                  onClick={openManagePortal}
                  disabled={busy === "portal"}
                  className="sp-btn sp-btn-ghost text-xs"
                  data-testid="manage-subscription-btn"
                >
                  {busy === "portal" ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Settings className="w-3.5 h-3.5" />}
                  Manage subscription
                </button>
              )}
            </div>
          </div>

          {/* Organizations used vs limit */}
          <div className="mt-5 pt-4 border-t border-white/5">
            <div className="flex items-center justify-between gap-3 text-sm">
              <div className="flex items-center gap-2">
                <Layers className="w-3.5 h-3.5 sp-subtle" />
                Organizations
              </div>
              <div className={`text-sm font-medium ${atOrgLimit ? "text-orange-300" : ""}`}>
                {orgsUsed}
                {" "}/{" "}
                {orgsLimit >= 1000 ? "Unlimited" : orgsLimit}
              </div>
            </div>
            {atOrgLimit && (
              <p className="text-[11px] text-orange-300 mt-2 leading-relaxed" data-testid="org-limit-warning">
                You've used every organization your {currentPlan?.name} plan includes.
                {" "}Upgrade below to create another.
              </p>
            )}
          </div>
        </section>

        {/* Usage meter */}
        {meter && (
          <section className="sp-glass-strong rounded-2xl p-5 sm:p-6 mb-6" data-testid="usage-meter-card">
            <div className="text-xs uppercase tracking-widest sp-subtle flex items-center gap-2 mb-3">
              <TrendingUp className="w-3.5 h-3.5" /> This month's agent compute
              <span className="text-[10px] sp-subtle font-normal normal-case tracking-normal">
                (across all your organizations)
              </span>
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
            <div>
              <div className="text-xs uppercase tracking-widest text-orange-300 flex items-center gap-2">
                <Receipt className="w-3.5 h-3.5" /> Outstanding invoice
              </div>
              <div className="text-2xl font-medium mt-1">
                ${outstanding.amount_due_usd?.toFixed(2)}
              </div>
              <div className="text-xs sp-subtle mt-1">
                For {outstanding.period_key} · raw cost ${outstanding.raw_cost_usd?.toFixed(2)}
                {" "}vs included ${outstanding.included_usd?.toFixed(2)}. We'll add this to your next invoice.
              </div>
            </div>
          </section>
        )}

        {/* Plan cards — hidden for staff */}
        {!isStaff && (
        <section className="mb-6" data-testid="plan-cards">
          <div className="flex items-center justify-between mb-3 flex-wrap gap-3">
            <h3 className="text-sm uppercase tracking-widest sp-subtle">
              {isSubscribed ? "Change plan" : "Choose a plan"}
            </h3>
            <CadenceToggle cadence={cadence} onChange={setCadence} />
          </div>
          <div className="grid md:grid-cols-3 gap-4">
            {plans.map(p => (
              <PlanCard
                key={p.id}
                plan={p}
                cadence={cadence}
                current={currentPlan?.id === p.id && (isSubscribed || isStaff)}
                busy={busy === `plan:${p.id}`}
                onSelect={() => {
                  if (p.recurring) setPendingPlan(p);
                  else setShowSales(true);
                }}
              />
            ))}
          </div>
        </section>
        )}

        {isStaff && (
          <section className="sp-glass-strong rounded-2xl p-5 sm:p-6 mb-6" data-testid="staff-banner">
            <div className="flex items-start gap-3">
              <Sparkles className="w-4 h-4 text-orange-300 mt-0.5 flex-shrink-0" />
              <div className="text-sm leading-relaxed">
                <strong>You have staff access.</strong> Every feature is unlocked
                with no monthly charge or usage cap. This applies to anyone at
                @archeforge.com and the founding team. Organizations you create
                inherit the same access automatically.
              </div>
            </div>
          </section>
        )}

        {/* Recent transactions */}
        <section className="sp-glass rounded-2xl p-5 sm:p-6" data-testid="transactions-card">
          <h3 className="text-xs uppercase tracking-widest sp-subtle mb-3 flex items-center gap-2">
            <Receipt className="w-3.5 h-3.5" /> Recent charges
          </h3>
          {(summary?.recent_transactions || []).length === 0 ? (
            <div className="text-sm sp-subtle">No charges yet.</div>
          ) : (
            <ul className="divide-y divide-white/5">
              {summary.recent_transactions.map((t, idx) => (
                <li key={t.session_id || t.invoice_id || idx}
                    className="py-2 flex items-center justify-between text-sm">
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

        <FtcDisclosure />

        <div className="mt-6 text-xs sp-subtle">
          <Link to="/app/account" className="hover:underline" data-testid="back-to-account">← Back to account settings</Link>
        </div>
      </main>

      {pendingPlan && (
        <ConsentModal
          plan={pendingPlan}
          cadence={cadence}
          busy={busy === `plan:${pendingPlan.id}`}
          onCancel={() => setPendingPlan(null)}
          onConfirm={confirmSubscribe}
        />
      )}
      {showSales && (
        <SalesContactModal
          onClose={() => setShowSales(false)}
          onSubmitted={() => {
            setShowSales(false);
            toast.success("Thanks — our team will reach out shortly.");
          }}
        />
      )}
    </div>
  );
}

// ─── Sub-components ─────────────────────────────────────────────────────

function buildConsentText(plan, cadence = "monthly") {
  const orgs = plan.orgs_included >= 1000 ? "unlimited" : plan.orgs_included;
  if (cadence === "annual") {
    const monthly = plan.annual_usd.toFixed(2);
    const total = (plan.annual_usd * 12).toFixed(2);
    return (
      `I authorize Foundry to charge my payment method $${total} once per year ` +
      `(equivalent to $${monthly}/mo) for the ${plan.name} plan on a recurring ` +
      `annual basis. This subscription unlocks ${orgs} organization${plan.orgs_included === 1 ? "" : "s"}. ` +
      `My subscription will automatically renew each year until I cancel it ` +
      `via the Manage subscription button on this page. Cancellations take ` +
      `effect at the end of the current billing period.`
    );
  }
  return (
    `I authorize Foundry to charge my payment method $${plan.monthly_usd.toFixed(2)}/mo ` +
    `for the ${plan.name} plan on a recurring monthly basis. This subscription ` +
    `unlocks ${orgs} organization${plan.orgs_included === 1 ? "" : "s"}. ` +
    `My subscription will automatically renew each month until I cancel it ` +
    `via the Manage subscription button on this page. Cancellations take effect ` +
    `at the end of the current billing period.`
  );
}

function CadenceToggle({ cadence, onChange }) {
  return (
    <div
      className="inline-flex items-center rounded-full p-1 bg-white/5 text-xs"
      data-testid="cadence-toggle"
    >
      <button
        onClick={() => onChange("monthly")}
        className={`px-3 py-1 rounded-full transition-colors ${cadence === "monthly" ? "bg-white/15 text-white" : "sp-subtle"}`}
        data-testid="cadence-monthly"
      >
        Monthly
      </button>
      <button
        onClick={() => onChange("annual")}
        className={`px-3 py-1 rounded-full transition-colors flex items-center gap-1 ${cadence === "annual" ? "bg-white/15 text-white" : "sp-subtle"}`}
        data-testid="cadence-annual"
      >
        Annual
        <span className="text-[9px] uppercase tracking-widest text-orange-300 font-medium">
          Save ~17%
        </span>
      </button>
    </div>
  );
}

function PlanStatusPill({ status, cancelling, paidUntil, isStaff }) {
  if (isStaff || status === "staff") {
    return (
      <div className="inline-flex items-center gap-1.5 text-xs px-2 py-1 rounded-full bg-orange-500/15 text-orange-300"
           data-testid="plan-status-staff">
        <Sparkles className="w-3 h-3" /> Staff · unlimited access
      </div>
    );
  }
  if (cancelling) {
    return (
      <div className="inline-flex items-center gap-1.5 text-xs px-2 py-1 rounded-full bg-amber-500/15 text-amber-300"
           data-testid="plan-status-cancelling">
        <AlertTriangle className="w-3 h-3" />
        Cancels on {paidUntil || "period end"}
      </div>
    );
  }
  if (status === "active") {
    return (
      <div className="inline-flex items-center gap-1.5 text-xs px-2 py-1 rounded-full bg-emerald-500/15 text-emerald-300"
           data-testid="plan-status-active">
        <Check className="w-3 h-3" /> Active{paidUntil ? ` · renews ${paidUntil}` : ""}
      </div>
    );
  }
  return (
    <div className="inline-flex items-center gap-1.5 text-xs px-2 py-1 rounded-full bg-white/10 sp-subtle"
         data-testid="plan-status-none">
      Not subscribed
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

function PlanCard({ plan, cadence = "monthly", current, busy, onSelect }) {
  const isRefine = !plan.recurring;
  const orgsLabel = plan.orgs_included >= 1000
    ? "Unlimited organizations"
    : `${plan.orgs_included} organization${plan.orgs_included === 1 ? "" : "s"}`;
  // Effective monthly price for the chosen cadence.
  const monthlyEffective = cadence === "annual" ? plan.annual_usd : plan.monthly_usd;
  const cta = current
    ? "Current plan"
    : isRefine
      ? "Talk to us"
      : cadence === "annual"
        ? `Subscribe — $${(plan.annual_usd * 12).toFixed(0)}/yr`
        : `Subscribe — $${plan.monthly_usd.toFixed(0)}/mo`;
  const CtaIcon = current ? Check : isRefine ? Mail : ArrowUpRight;
  return (
    <div
      className={`sp-glass-strong rounded-2xl p-5 flex flex-col ${current ? "ring-1 ring-orange-300/40" : ""} ${plan.highlight ? "ring-1 ring-orange-300/60 relative" : ""}`}
      data-testid={`plan-card-${plan.id}`}
    >
      {plan.highlight && !current && (
        <div className="absolute -top-2 right-4 px-2 py-0.5 text-[10px] uppercase tracking-widest rounded-full bg-orange-400 text-black font-medium">
          Most popular
        </div>
      )}
      <div className="text-xs uppercase tracking-widest sp-subtle">{plan.name}</div>
      <div className="text-3xl font-medium mt-1">
        {plan.monthly_usd == null
          ? <span className="text-2xl">Custom</span>
          : <>${monthlyEffective.toFixed(0)}<span className="text-sm sp-subtle">/mo</span></>}
      </div>
      {plan.annual_usd != null && cadence === "annual" && (
        <div className="text-[11px] sp-subtle mt-0.5">
          ${(plan.annual_usd * 12).toFixed(0)} billed yearly
        </div>
      )}
      <div className="text-xs sp-subtle mt-1">{plan.tagline}</div>
      <div className="text-[11px] mt-2 inline-flex items-center gap-1 text-orange-300">
        <Layers className="w-3 h-3" /> {orgsLabel}
      </div>
      <ul className="text-sm space-y-1.5 mt-4 flex-1">
        {plan.features.map((f, i) => (
          <li key={i} className="flex items-start gap-2">
            <Check className="w-3.5 h-3.5 mt-1 text-emerald-300 flex-shrink-0" />
            <span>{f}</span>
          </li>
        ))}
      </ul>
      {plan.studio_perk && (
        <div className="text-[11px] sp-subtle italic mt-3 pt-3 border-t border-white/5 leading-relaxed">
          {plan.studio_perk}
        </div>
      )}
      <button
        onClick={onSelect}
        disabled={current || busy}
        className={`mt-5 sp-btn ${current ? "sp-btn-ghost" : "sp-btn-primary"} text-sm disabled:opacity-60 disabled:cursor-not-allowed`}
        data-testid={`plan-select-${plan.id}`}
      >
        {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <CtaIcon className="w-4 h-4" />}
        {cta}
      </button>
      {!current && plan.recurring && (
        <p className="text-[10px] sp-subtle mt-2 leading-relaxed">
          Billed {cadence}. Cancel anytime.
        </p>
      )}
    </div>
  );
}

function FtcDisclosure() {
  return (
    <section
      className="sp-glass rounded-2xl p-4 mt-6 text-[11px] sp-subtle leading-relaxed"
      data-testid="ftc-disclosure"
    >
      <div className="flex items-start gap-2">
        <Shield className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" />
        <div>
          <strong className="text-white/80">How billing works:</strong>{" "}
          Spark and Temper are recurring monthly subscriptions. Your card is
          charged once on the day you subscribe and on the same date each month
          thereafter, until you cancel via <em>Manage subscription</em>.
          Cancellations take effect at the end of your current billing period —
          you keep access until then, and you won't be charged again.
          Refine is custom-priced — reach out for a quote.
        </div>
      </div>
    </section>
  );
}

function ConsentModal({ plan, busy, onCancel, onConfirm }) {
  const [agreed, setAgreed] = useState(false);
  const consentText = buildConsentText(plan);
  const node = (
    <div
      className="fixed inset-0 z-[1000] flex items-center justify-center p-4"
      style={{ background: "rgba(0,0,0,0.72)" }}
      onClick={onCancel}
      data-testid="consent-modal"
    >
      <div
        className="sp-glass-strong rounded-2xl p-6 w-full max-w-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between mb-3">
          <div>
            <div className="text-xs uppercase tracking-widest sp-subtle">Confirm subscription</div>
            <h3 className="sp-display text-xl mt-1">{plan.name} — ${plan.monthly_usd.toFixed(0)}/month</h3>
          </div>
          <button onClick={onCancel} className="sp-btn sp-btn-ghost !p-2" data-testid="consent-close">
            <X className="w-4 h-4" />
          </button>
        </div>

        <p className="text-sm sp-subtle leading-relaxed mb-4">
          You're about to start a recurring monthly subscription. Before we hand
          you to Stripe to enter your card, please confirm you understand the
          terms below.
        </p>

        <div className="sp-glass rounded-xl p-3 text-sm leading-relaxed mb-4"
             data-testid="consent-text-block">
          {consentText}
        </div>

        <label className="flex items-start gap-2 cursor-pointer text-sm">
          <input
            type="checkbox"
            checked={agreed}
            onChange={(e) => setAgreed(e.target.checked)}
            className="mt-1 accent-orange-400"
            data-testid="consent-checkbox"
          />
          <span>
            I authorize this recurring charge and understand my subscription
            auto-renews monthly until I cancel it from the Manage subscription
            page.
          </span>
        </label>

        <div className="flex items-center justify-end gap-2 mt-5">
          <button
            onClick={onCancel}
            disabled={busy}
            className="sp-btn sp-btn-ghost text-sm"
            data-testid="consent-cancel"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={!agreed || busy}
            className="sp-btn sp-btn-primary text-sm disabled:opacity-60 disabled:cursor-not-allowed"
            data-testid="consent-confirm"
          >
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <ExternalLink className="w-4 h-4" />}
            Continue to Stripe
          </button>
        </div>
      </div>
    </div>
  );
  return createPortal(node, document.body);
}

function SalesContactModal({ onClose, onSubmitted }) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [company, setCompany] = useState("");
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (!name.trim() || !email.trim()) {
      toast.error("Name and email are required.");
      return;
    }
    setSubmitting(true);
    try {
      await api.post(`/billing/contact-sales`, {
        name: name.trim(), email: email.trim(),
        company: company.trim(), message: message.trim(),
      });
      onSubmitted?.();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not submit your inquiry.");
    } finally {
      setSubmitting(false);
    }
  };

  const node = (
    <div
      className="fixed inset-0 z-[1000] flex items-center justify-center p-4"
      style={{ background: "rgba(0,0,0,0.72)" }}
      onClick={onClose}
      data-testid="sales-modal"
    >
      <form
        onSubmit={submit}
        onClick={(e) => e.stopPropagation()}
        className="sp-glass-strong rounded-2xl p-6 w-full max-w-lg"
      >
        <div className="flex items-start justify-between mb-3">
          <div>
            <div className="text-xs uppercase tracking-widest sp-subtle">Refine</div>
            <h3 className="sp-display text-xl mt-1">Talk to our team</h3>
          </div>
          <button type="button" onClick={onClose} className="sp-btn sp-btn-ghost !p-2"
                  data-testid="sales-close">
            <X className="w-4 h-4" />
          </button>
        </div>
        <p className="text-sm sp-subtle leading-relaxed mb-4">
          Refine is custom-priced based on the size of your team and how you'll
          use the platform. Tell us a bit about you and we'll get back within
          one business day.
        </p>
        <div className="space-y-3">
          <input value={name} onChange={(e) => setName(e.target.value)}
            placeholder="Your name" className="sp-input w-full text-sm"
            data-testid="sales-name" required />
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)}
            placeholder="Work email" className="sp-input w-full text-sm"
            data-testid="sales-email" required />
          <input value={company} onChange={(e) => setCompany(e.target.value)}
            placeholder="Company (optional)" className="sp-input w-full text-sm"
            data-testid="sales-company" />
          <textarea value={message} onChange={(e) => setMessage(e.target.value)}
            placeholder="What are you trying to do with Foundry? (optional)"
            className="sp-input w-full text-sm min-h-[100px]"
            data-testid="sales-message" />
        </div>
        <div className="flex items-center justify-end gap-2 mt-5">
          <button type="button" onClick={onClose} disabled={submitting}
                  className="sp-btn sp-btn-ghost text-sm"
                  data-testid="sales-cancel-btn">
            Cancel
          </button>
          <button type="submit" disabled={submitting}
                  className="sp-btn sp-btn-primary text-sm"
                  data-testid="sales-submit-btn">
            {submitting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Mail className="w-4 h-4" />}
            Send inquiry
          </button>
        </div>
      </form>
    </div>
  );
  return createPortal(node, document.body);
}
