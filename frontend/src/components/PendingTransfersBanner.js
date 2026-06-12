import React, { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { Crown, ArrowUpRight, Loader2, X, ShieldCheck } from "lucide-react";
import api from "../lib/api";

/**
 * PendingTransfersBanner
 * ----------------------
 * Mounted in the /app shell (AppHome + OrgPage). Polls
 * `/api/me/pending-ownership-transfers` for ownership transfers
 * targeting the current user and shows an offer card per transfer.
 *
 *   • Accept → POST /accept. On 402, surface inline upgrade CTA.
 *   • Decline → DELETE same endpoint (recipient path).
 *
 * Hides itself silently when no offers exist so the rest of the
 * page UI is untouched for the common case.
 */
export default function PendingTransfersBanner({ onAccepted }) {
  const [offers, setOffers] = useState([]);
  const [loading, setLoading] = useState(true);
  // Map<transfer_id, { code, required_tier, current_tier, message }> so
  // each card remembers its 402 state independently across re-renders.
  const [upgradeNeeded, setUpgradeNeeded] = useState({});
  const [busyId, setBusyId] = useState("");
  const nav = useNavigate();

  const fetchOffers = useCallback(async () => {
    try {
      const { data } = await api.get("/me/pending-ownership-transfers");
      setOffers(Array.isArray(data) ? data : []);
    } catch (_err) {
      // Silent — the banner just stays empty on transient errors.
      setOffers([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchOffers();
    // Light poll every 60s so newly-issued offers appear without a
    // hard refresh, but we don't hammer the API.
    const t = setInterval(fetchOffers, 60_000);
    return () => clearInterval(t);
  }, [fetchOffers]);

  const accept = async (t) => {
    if (busyId) return;
    setBusyId(t.id);
    try {
      await api.post(`/orgs/${t.org_id}/ownership-transfers/${t.id}/accept`);
      toast.success(`You're now the owner of ${t.org_name || "the organization"}.`);
      setOffers((prev) => prev.filter((x) => x.id !== t.id));
      setUpgradeNeeded((prev) => {
        const next = { ...prev };
        delete next[t.id];
        return next;
      });
      onAccepted?.(t);
    } catch (err) {
      const status = err?.response?.status;
      const detail = err?.response?.data?.detail;
      if (status === 402 && detail && typeof detail === "object" && detail.code === "plan_upgrade_required") {
        setUpgradeNeeded((prev) => ({ ...prev, [t.id]: detail }));
        toast.error(detail.message || "Upgrade required before you can accept ownership.");
      } else if (status === 410) {
        toast.error("This transfer has expired. Ask the original owner to issue a new one.");
        setOffers((prev) => prev.filter((x) => x.id !== t.id));
      } else {
        toast.error(
          (typeof detail === "string" && detail)
          || "Could not accept ownership transfer.",
        );
      }
    } finally {
      setBusyId("");
    }
  };

  const decline = async (t) => {
    if (busyId) return;
    const ok = window.confirm(
      `Decline ownership of ${t.org_name || "this organization"}? `
      + `${t.initiated_by_name || "The current owner"} will be notified.`,
    );
    if (!ok) return;
    setBusyId(t.id);
    try {
      await api.delete(`/orgs/${t.org_id}/ownership-transfers/${t.id}`);
      toast.success("Transfer declined.");
      setOffers((prev) => prev.filter((x) => x.id !== t.id));
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not decline transfer.");
    } finally {
      setBusyId("");
    }
  };

  const goUpgrade = (tier) => {
    // Route to billing with subscribe intent. The user can re-accept
    // from the banner after they complete checkout; the offer is
    // still pending until they explicitly accept or it expires.
    nav(`/app/billing?subscribe=${encodeURIComponent(tier)}`);
  };

  if (loading || offers.length === 0) return null;

  return (
    <div className="space-y-3 mb-5" data-testid="pending-transfers-banner">
      {offers.map((t) => {
        const upgrade = upgradeNeeded[t.id];
        const isBusy = busyId === t.id;
        const expires = t.expires_at ? new Date(t.expires_at) : null;
        const daysLeft = expires
          ? Math.max(0, Math.ceil((expires.getTime() - Date.now()) / 86_400_000))
          : null;
        return (
          <div
            key={t.id}
            className="sp-glass-strong rounded-2xl p-4 sm:p-5 border border-amber-400/40 bg-amber-500/10"
            data-testid={`transfer-offer-${t.id}`}
          >
            <div className="flex items-start gap-3 flex-wrap">
              <div className="w-9 h-9 rounded-lg flex items-center justify-center shrink-0 bg-amber-500/20 border border-amber-400/40">
                <Crown className="w-4 h-4 text-amber-200" />
              </div>
              <div className="min-w-0 flex-1">
                <div className="text-sm sm:text-base text-white font-medium">
                  Ownership of <span className="underline decoration-dotted">{t.org_name || "an organization"}</span> is being transferred to you
                </div>
                <div className="text-xs sp-subtle mt-0.5">
                  {t.initiated_by_name ? `From ${t.initiated_by_name}. ` : ""}
                  Plan required to keep the org running: <span className="text-white">{(t.required_plan_tier || "spark").toString().toUpperCase()}</span>
                  {typeof t.member_count_at_init === "number"
                    ? <> · {t.member_count_at_init} member{t.member_count_at_init === 1 ? "" : "s"}</>
                    : null}
                  {daysLeft !== null && (
                    <> · expires in {daysLeft === 0 ? "less than a day" : `${daysLeft} day${daysLeft === 1 ? "" : "s"}`}</>
                  )}
                </div>
                {t.also_leave_after && (
                  <div className="text-[11px] mt-1 text-amber-200/90">
                    {t.initiated_by_name || "The current owner"} will leave the org once you accept.
                  </div>
                )}
                {upgrade && (
                  <div
                    className="mt-3 rounded-xl p-3 border border-amber-400/50 bg-amber-500/15 text-xs leading-relaxed"
                    data-testid={`transfer-upgrade-${t.id}`}
                  >
                    <div className="flex items-center gap-2 text-amber-100 font-medium">
                      <ShieldCheck className="w-3.5 h-3.5" /> Upgrade required to accept
                    </div>
                    <div className="sp-subtle mt-1">
                      {upgrade.message || `This org needs at least the ${upgrade.required_tier?.toUpperCase()} plan to keep all members. You're on ${upgrade.current_tier?.toUpperCase()}.`}
                    </div>
                    <div className="flex flex-wrap gap-2 mt-2">
                      <button
                        type="button"
                        className="sp-btn sp-btn-primary text-xs"
                        onClick={() => goUpgrade(upgrade.required_tier)}
                        data-testid={`transfer-upgrade-cta-${t.id}`}
                      >
                        Upgrade to {(upgrade.required_tier || "").toUpperCase()} <ArrowUpRight className="w-3 h-3" />
                      </button>
                      <button
                        type="button"
                        className="sp-btn sp-btn-ghost text-xs"
                        onClick={() => accept(t)}
                        disabled={isBusy}
                        data-testid={`transfer-retry-accept-${t.id}`}
                      >
                        {isBusy ? <Loader2 className="w-3 h-3 animate-spin" /> : null} I've upgraded — try again
                      </button>
                    </div>
                  </div>
                )}
              </div>
              <div className="flex items-center gap-2 shrink-0">
                {!upgrade && (
                  <button
                    type="button"
                    onClick={() => accept(t)}
                    disabled={isBusy}
                    className="sp-btn sp-btn-primary text-xs disabled:opacity-60"
                    data-testid={`transfer-accept-${t.id}`}
                  >
                    {isBusy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Crown className="w-3 h-3" />}
                    Accept ownership
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => decline(t)}
                  disabled={isBusy}
                  className="sp-btn sp-btn-ghost text-xs disabled:opacity-60"
                  data-testid={`transfer-decline-${t.id}`}
                  title="Decline this transfer"
                >
                  <X className="w-3 h-3" /> Decline
                </button>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
