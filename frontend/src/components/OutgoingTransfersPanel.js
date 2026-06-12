import React, { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { Crown, X, Loader2, Clock } from "lucide-react";
import api from "../lib/api";

/**
 * OutgoingTransfersPanel
 * ----------------------
 * Renders pending ownership-transfer offers initiated by anyone in the
 * org (owners + admins see all; regular members see only ones aimed at
 * them — but those are already in PendingTransfersBanner so this panel
 * is mainly useful for owners/admins). Lets the initiator cancel
 * the offer.
 *
 * Mounted in OrgPage members panel.
 */
export default function OutgoingTransfersPanel({ orgId, meId, refreshTrigger }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState("");

  const fetchItems = useCallback(async () => {
    try {
      const { data } = await api.get(`/orgs/${orgId}/ownership-transfers`);
      setItems(Array.isArray(data) ? data : []);
    } catch (_err) {
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [orgId]);

  useEffect(() => { fetchItems(); }, [fetchItems, refreshTrigger]);

  const cancel = async (t) => {
    if (busyId) return;
    const ok = window.confirm(
      `Cancel the ownership transfer offered to ${t.recipient_name || "this member"}?`,
    );
    if (!ok) return;
    setBusyId(t.id);
    try {
      await api.delete(`/orgs/${orgId}/ownership-transfers/${t.id}`);
      toast.success("Transfer cancelled.");
      setItems((prev) => prev.filter((x) => x.id !== t.id));
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not cancel transfer.");
    } finally {
      setBusyId("");
    }
  };

  if (loading || items.length === 0) return null;

  return (
    <div className="mt-4 sp-glass rounded-2xl p-4" data-testid="outgoing-transfers-panel">
      <div className="flex items-center gap-2 mb-3">
        <Crown className="w-3.5 h-3.5 text-amber-300" />
        <h4 className="text-xs uppercase tracking-widest sp-subtle">
          Pending ownership transfers ({items.length})
        </h4>
      </div>
      <div className="flex flex-col gap-2">
        {items.map((t) => {
          const expires = t.expires_at ? new Date(t.expires_at) : null;
          const daysLeft = expires
            ? Math.max(0, Math.ceil((expires.getTime() - Date.now()) / 86_400_000))
            : null;
          const canCancel = String(t.initiated_by) === String(meId);
          const isBusy = busyId === t.id;
          return (
            <div
              key={t.id}
              className="flex items-center justify-between gap-3 p-3 rounded-xl sp-glass"
              data-testid={`outgoing-transfer-row-${t.id}`}
            >
              <div className="min-w-0 flex-1">
                <div className="text-sm text-white truncate">
                  → {t.recipient_name || "Unknown recipient"}
                </div>
                <div className="text-[11px] sp-subtle">
                  Required plan: {(t.required_plan_tier || "spark").toUpperCase()} ·{" "}
                  Outgoing role: {t.outgoing_role_after}
                  {t.also_leave_after ? " · will leave" : ""}
                  {daysLeft !== null && (
                    <> · <Clock className="inline w-2.5 h-2.5" /> {daysLeft === 0 ? "<1 day" : `${daysLeft}d`} left</>
                  )}
                </div>
              </div>
              {canCancel && (
                <button
                  type="button"
                  onClick={() => cancel(t)}
                  disabled={isBusy}
                  className="sp-btn sp-btn-ghost text-xs"
                  data-testid={`outgoing-transfer-cancel-${t.id}`}
                >
                  {isBusy ? <Loader2 className="w-3 h-3 animate-spin" /> : <X className="w-3 h-3" />} Cancel
                </button>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
