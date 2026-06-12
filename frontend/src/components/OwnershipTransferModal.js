import React, { useState } from "react";
import { createPortal } from "react-dom";
import { toast } from "sonner";
import { Crown, X, Loader2, AlertTriangle } from "lucide-react";
import api from "../lib/api";

/**
 * OwnershipTransferModal
 * ----------------------
 * Outgoing-owner side of the transfer handshake. Triggered from a
 * member row's "Transfer ownership" action. Sends:
 *   POST /api/orgs/{org_id}/ownership-transfers
 *     { recipient_user_id, outgoing_role_after, also_leave_after }
 *
 * After init, the transfer is pending — the recipient sees the banner
 * on their next /app load. The outgoing owner stays owner until the
 * recipient accepts.
 */
export default function OwnershipTransferModal({ orgId, orgName, member, onClose, onInitiated }) {
  // Sensible defaults: keep "admin" rights so the outgoing owner can
  // still help the team, but pre-tick "also leave" off so it's an
  // explicit choice rather than an accident.
  const [outgoingRoleAfter, setOutgoingRoleAfter] = useState("admin");
  const [alsoLeave, setAlsoLeave] = useState(false);
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e?.preventDefault?.();
    if (busy) return;
    setBusy(true);
    try {
      await api.post(`/orgs/${orgId}/ownership-transfers`, {
        recipient_user_id: member.user_id,
        outgoing_role_after: outgoingRoleAfter,
        also_leave_after: alsoLeave,
      });
      toast.success(`Transfer offer sent to ${member.name || "the recipient"}.`);
      onInitiated?.();
      onClose?.();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not start ownership transfer.");
    } finally {
      setBusy(false);
    }
  };

  return createPortal(
    <div
      className="fixed inset-0 z-[95] flex items-center justify-center p-4"
      style={{ background: "rgba(0,0,0,0.72)" }}
      onClick={onClose}
      data-testid="ownership-transfer-modal"
    >
      <form
        onSubmit={submit}
        onClick={(e) => e.stopPropagation()}
        className="sp-glass-strong rounded-2xl p-5 max-w-lg w-full"
      >
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <Crown className="w-4 h-4 text-amber-300" />
            <h3 className="sp-display text-lg">Transfer ownership</h3>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="sp-btn sp-btn-ghost !p-2"
            data-testid="ownership-transfer-close"
            disabled={busy}
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <p className="text-sm sp-subtle leading-relaxed">
          You're about to offer ownership of <span className="text-white">{orgName || "this org"}</span> to{" "}
          <span className="text-white">{member?.name || member?.email || "this member"}</span>.
          They'll have 7 days to accept. Until they accept, you stay the owner — nothing changes.
        </p>

        <div className="mt-4 rounded-xl border border-amber-400/30 bg-amber-500/10 p-3 text-xs leading-relaxed">
          <div className="flex items-center gap-2 text-amber-200 font-medium">
            <AlertTriangle className="w-3.5 h-3.5" /> What transfers — and what doesn't
          </div>
          <ul className="mt-1 list-disc list-inside space-y-1 sp-subtle">
            <li>Ownership of the org and its teams moves to the recipient.</li>
            <li>The recipient must be on a plan that fits this org's size. Spark covers 1 member, Temper covers up to 5, Refine covers any.</li>
            <li>Add-on seats <span className="text-white">don't</span> transfer — they're billed to your Stripe customer.</li>
          </ul>
        </div>

        <div className="mt-4 space-y-3">
          <label className="block">
            <span className="text-xs sp-subtle uppercase tracking-widest">Your role after transfer</span>
            <select
              value={outgoingRoleAfter}
              onChange={(e) => setOutgoingRoleAfter(e.target.value)}
              disabled={busy || alsoLeave}
              className="mt-1 w-full text-sm rounded-md px-3 py-2 focus:sp-focus"
              style={{
                background: "var(--input-bg)",
                border: "1px solid var(--input-border)",
                color: "var(--fg)",
              }}
              data-testid="transfer-outgoing-role"
            >
              <option value="member">Member — just keep access</option>
              <option value="admin">Admin — manage teams &amp; invites (recommended)</option>
              <option value="owner">Co-owner — full ownership rights</option>
            </select>
          </label>

          <label className="flex items-start gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={alsoLeave}
              onChange={(e) => setAlsoLeave(e.target.checked)}
              disabled={busy}
              className="mt-0.5"
              data-testid="transfer-also-leave"
            />
            <span className="text-sm">
              <span className="text-white">Also leave the org after transfer completes</span>
              <span className="block text-xs sp-subtle">
                You'll be removed from this org and its teams the moment the recipient accepts. Choose this if you're fully handing it over.
              </span>
            </span>
          </label>
        </div>

        <div className="flex items-center justify-end gap-2 mt-5">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="sp-btn sp-btn-ghost text-xs"
            data-testid="transfer-cancel"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={busy}
            className="sp-btn sp-btn-primary text-xs disabled:opacity-60"
            data-testid="transfer-submit"
          >
            {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Crown className="w-3 h-3" />}
            Send transfer offer
          </button>
        </div>
      </form>
    </div>,
    document.body,
  );
}
