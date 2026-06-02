import React, { useCallback, useEffect, useState } from "react";
import { KeyRound, Loader2, Clock, AlertTriangle } from "lucide-react";
import { toast } from "sonner";
import api from "../lib/api";
import { parseServerDate, formatLocalDate } from "../lib/datetime";

/**
 * Access-state helpers shared between the TopBar pill, the Billing
 * page section, and the read-only banner. Single source of truth for
 * how we render the user's `access_expires_at` window.
 *
 * Backend contract: `GET /api/access-codes/me` returns:
 *   {
 *     mode: "full" | "readonly" | "blocked",
 *     reason: string,
 *     expires_at: ISO | null,
 *     days_remaining: int | null,
 *     in_grace: bool,
 *     grace_days_remaining: int | null,
 *     is_perpetual: bool,
 *   }
 */
export function useAccessState() {
  const [state, setState] = useState(null);
  const [usage, setUsage] = useState(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const [s, u] = await Promise.all([
        api.get("/access-codes/me").then((r) => r.data).catch(() => null),
        // Trial usage is fetched in parallel — non-trial users get
        // an empty payload back so this is cheap.
        api.get("/trial/usage").then((r) => r.data).catch(() => null),
      ]);
      setState(s);
      setUsage(u);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return { state, usage, loading, refresh };
}

/**
 * Small color-coded pill summarising the user's access window. Renders
 * NOTHING for perpetual / >14-day-remaining users so it doesn't add
 * visual noise for the happy path.
 *
 * Tier thresholds:
 *   • > 14 days  → hide entirely
 *   • 4–14 days  → slate "12 days left"
 *   • 1–3 days   → amber "2 days left — extend?"
 *   • 0, in grace→ red "Read-only — extend access"
 *   • blocked    → red "Access expired"
 */
export function AccessStatusPill({ state, usage, onOpen }) {
  if (!state) return null;
  if (state.is_perpetual) return null;
  // Detect "running low on a cap" — surface the most-burned resource
  // as a secondary signal even when the days-remaining count is fine.
  // E.g. a user with 20 days left but 18/20 images used should still
  // see something amber so they're nudged to subscribe BEFORE they
  // hit the wall mid-draft.
  let hottestCap = null;
  if (usage && usage.tier === "trial" && usage.caps) {
    Object.entries(usage.caps).forEach(([key, c]) => {
      const pct = c.percent || 0;
      if (pct >= 75 && (!hottestCap || pct > hottestCap.percent)) {
        hottestCap = { key, ...c };
      }
    });
  }
  if (
    state.mode === "full"
    && (state.days_remaining ?? 0) > 14
    && !hottestCap
  ) {
    // Happy path — > 14 days left and no cap stressed: stay quiet.
    return null;
  }

  let color = "text-slate-300 border-white/15";
  let label = `${state.days_remaining ?? 0} days left`;
  let Icon = Clock;

  if (state.mode === "blocked") {
    color = "text-red-300 border-red-500/40 bg-red-500/10";
    label = "Access expired";
    Icon = AlertTriangle;
  } else if (state.mode === "readonly") {
    color = "text-red-300 border-red-500/40 bg-red-500/10";
    label = `Read-only · ${state.grace_days_remaining ?? 0}d grace`;
    Icon = AlertTriangle;
  } else if ((state.days_remaining ?? 0) <= 3) {
    color = "text-amber-300 border-amber-500/40 bg-amber-500/10";
    label = `${state.days_remaining} day${state.days_remaining === 1 ? "" : "s"} left`;
  } else if (hottestCap) {
    // Cap-driven pill — primary text shows the burned resource.
    color = "text-amber-300 border-amber-500/40 bg-amber-500/10";
    const left = (hottestCap.limit ?? 0) - (hottestCap.used ?? 0);
    label = `${left} ${hottestCap.label.toLowerCase()} left`;
    Icon = AlertTriangle;
  }

  return (
    <button
      type="button"
      onClick={onOpen}
      className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs border ${color} hover:brightness-110 transition`}
      data-testid="access-status-pill"
      title="Click to extend your access window with a code"
    >
      <Icon className="w-3 h-3" />
      <span>{label}</span>
    </button>
  );
}

/**
 * Modal-style redeem prompt. Used by the TopBar pill and the
 * read-only banner — keeps the user-facing copy in one place so we
 * don't have to keep three slightly-different forms in sync.
 *
 * Posts to `POST /api/access-codes/redeem` and surfaces the
 * `extended_days` / `new_expires_at` from the response in the
 * success toast — so the user immediately knows "yes, you have N
 * more days now" instead of having to refresh and squint at the pill.
 */
export function RedeemCodeModal({ open, onClose, onRedeemed, currentState }) {
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) setCode("");
  }, [open]);

  if (!open) return null;

  const submit = async (e) => {
    e?.preventDefault();
    if (!code.trim() || busy) return;
    setBusy(true);
    try {
      const { data } = await api.post("/access-codes/redeem", {
        code: code.trim(),
      });
      if (data?.kept_perpetual) {
        toast.success("Code accepted — you already have unlimited access, so the window stays open.");
      } else if (data?.extended_days) {
        const expiry = data.new_expires_at
          ? formatLocalDate(data.new_expires_at)
          : null;
        toast.success(
          expiry
            ? `+${data.extended_days} days added — access now runs until ${expiry}.`
            : `+${data.extended_days} days of access added.`,
        );
      } else if (data?.already_granted) {
        toast.success("You already have active access — no change needed.");
      } else {
        toast.success("Code redeemed. Welcome back.");
      }
      onRedeemed?.(data);
      onClose();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't redeem that code.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
         data-testid="redeem-code-modal"
         onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <form
        onSubmit={submit}
        className="w-full max-w-md sp-glass-strong rounded-2xl p-6 shadow-2xl"
      >
        <div className="flex items-center gap-3 mb-4">
          <div className="w-9 h-9 rounded-lg bg-orange-500/20 border border-orange-400/40 flex items-center justify-center">
            <KeyRound className="w-4 h-4 text-orange-300" />
          </div>
          <div>
            <h3 className="text-lg font-semibold leading-tight">Extend access</h3>
            <p className="text-xs sp-subtle leading-tight">
              {currentState?.is_perpetual
                ? "You already have unlimited access — this is FYI only."
                : currentState?.mode === "blocked"
                  ? "Your access has expired. Redeem a fresh code to continue."
                  : currentState?.mode === "readonly"
                    ? "You're in a 7-day read-only window. Redeem a code to restore writes."
                    : "Stack additional days onto your current access window."}
            </p>
          </div>
        </div>

        {currentState && !currentState.is_perpetual && currentState.expires_at && (
          <div className="text-xs sp-subtle mb-3 flex items-center gap-2">
            <Clock className="w-3 h-3" />
            <span>
              Current expiry: <span className="text-white">{formatLocalDate(currentState.expires_at)}</span>
              {currentState.days_remaining != null && currentState.mode === "full" && (
                <> ({currentState.days_remaining} days remaining)</>
              )}
            </span>
          </div>
        )}

        <label className="text-xs sp-subtle uppercase tracking-widest">Access code</label>
        <input
          autoFocus
          value={code}
          onChange={(e) => setCode(e.target.value.toUpperCase())}
          placeholder="e.g. 7K3D-92F8-MX4P"
          className="w-full mt-1 bg-white/[0.03] border border-white/10 rounded-xl px-3 py-2.5 text-sm text-white font-mono tracking-widest"
          maxLength={40}
          data-testid="redeem-code-input"
        />

        <div className="flex items-center justify-end gap-2 mt-5">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="sp-btn sp-btn-ghost text-xs disabled:opacity-60"
            data-testid="redeem-code-cancel"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={busy || !code.trim()}
            className="sp-btn sp-btn-primary text-xs disabled:opacity-60"
            data-testid="redeem-code-submit"
          >
            {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <KeyRound className="w-3.5 h-3.5" />}
            {busy ? "Redeeming…" : "Redeem code"}
          </button>
        </div>
      </form>
    </div>
  );
}
