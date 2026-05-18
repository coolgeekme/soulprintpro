import { useEffect, useState } from "react";
import { Download, Trash2, Loader2, ShieldCheck, AlertTriangle, X, CheckCircle2 } from "lucide-react";
import { toast } from "sonner";
import api from "../lib/api";

/**
 * Privacy panel — surfaces GDPR data-export + right-to-erasure flows.
 *
 * Three states the user can be in:
 *   1. Idle — no pending export, no pending deletion. Show both buttons.
 *   2. Export building / ready — show progress + download link.
 *   3. Deletion pending — show grace-period banner + cancel button.
 *
 * The component is intentionally self-contained: it owns its export job
 * polling, its deletion-status state, and its modal flows. The parent
 * AccountPage just drops it in among the other panels.
 */
export default function PrivacyPanel() {
  const [exportJob, setExportJob] = useState(null);
  const [exportLoading, setExportLoading] = useState(false);
  const [delStatus, setDelStatus] = useState(null);
  const [delLoading, setDelLoading] = useState(false);
  const [showDeleteWizard, setShowDeleteWizard] = useState(false);

  // Load both export status + deletion status on mount. Both are
  // cheap GETs; we hit them in parallel so the panel renders fast.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [exp, del] = await Promise.all([
          api.get("/account/export").catch(() => ({ data: { status: "none" } })),
          api.get("/account/delete/status").catch(() => ({ data: {} })),
        ]);
        if (cancelled) return;
        setExportJob(exp.data);
        setDelStatus(del.data);
      } catch (e) {
        console.warn("Privacy panel load failed:", e?.message);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Poll the export job every 2s while building. Stop when status
  // transitions to ready / failed / consumed / expired.
  useEffect(() => {
    if (exportJob?.status !== "building") return;
    let cancelled = false;
    const tick = setInterval(async () => {
      try {
        const { data } = await api.get("/account/export");
        if (cancelled) return;
        setExportJob(data);
        if (data.status !== "building") clearInterval(tick);
      } catch (e) {
        clearInterval(tick);
      }
    }, 2000);
    return () => { cancelled = true; clearInterval(tick); };
  }, [exportJob?.status]);

  const requestExport = async () => {
    setExportLoading(true);
    try {
      const { data } = await api.post("/account/export");
      setExportJob(data);
      toast.success("Export started — we'll let you know when it's ready.");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not start export");
    } finally {
      setExportLoading(false);
    }
  };

  const refreshDeletionStatus = async () => {
    try {
      const { data } = await api.get("/account/delete/status");
      setDelStatus(data);
    } catch (e) { /* non-fatal */ }
  };

  const cancelDeletion = async () => {
    setDelLoading(true);
    try {
      await api.post("/account/delete/cancel");
      toast.success("Deletion cancelled. Your account is staying.");
      await refreshDeletionStatus();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not cancel deletion");
    } finally {
      setDelLoading(false);
    }
  };

  return (
    <div className="sp-glass-strong rounded-2xl p-5 sm:p-6 space-y-5" data-testid="privacy-card">
      <h2 className="text-sm uppercase tracking-widest sp-subtle flex items-center gap-2">
        <ShieldCheck className="w-3.5 h-3.5" /> Privacy &amp; data rights
      </h2>

      {/* Pending-deletion banner — only shown when a request is in-flight */}
      {delStatus?.pending && (
        <div className="rounded-lg border border-amber-400/40 bg-amber-50/80 dark:bg-amber-500/10 text-amber-900 dark:text-amber-200 px-4 py-3 text-sm" data-testid="privacy-deletion-banner">
          <div className="flex items-start gap-2">
            <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />
            <div className="flex-1 min-w-0">
              <div className="font-medium">
                Your account is scheduled for deletion on{" "}
                {delStatus.scheduled_for ? new Date(delStatus.scheduled_for).toLocaleDateString() : "(soon)"}
              </div>
              <div className="opacity-80 text-[12px] mt-1">
                We'll hard-delete your data {delStatus.grace_days} days from your request.
                You can change your mind any time before then.
              </div>
              <button
                onClick={cancelDeletion}
                disabled={delLoading}
                className="mt-3 inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md bg-white/80 dark:bg-white/10 hover:bg-white dark:hover:bg-white/15 border border-amber-500/30"
                data-testid="privacy-cancel-deletion"
              >
                {delLoading ? <Loader2 className="w-3 h-3 animate-spin" /> : <CheckCircle2 className="w-3 h-3" />}
                Cancel deletion
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Export section */}
      <div className="space-y-3" data-testid="privacy-export-section">
        <div>
          <div className="text-sm font-medium">Export my data</div>
          <p className="text-[12px] sp-subtle leading-relaxed">
            Download a ZIP of every piece of personal data we hold on you — your account,
            conversations you own, documents you uploaded, your role in each org, and
            cross-org memory. Other members' messages and org data you don't own are
            excluded. Required by GDPR Article 15 (access) and Article 20 (portability).
          </p>
        </div>

        {!exportJob || exportJob.status === "none" || exportJob.status === "consumed" || exportJob.status === "expired" || exportJob.status === "failed" ? (
          <button
            onClick={requestExport}
            disabled={exportLoading}
            className="sp-btn sp-btn-secondary inline-flex items-center gap-2 text-sm"
            data-testid="privacy-export-start"
          >
            {exportLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
            {exportJob?.status === "failed" ? "Retry export" : "Start export"}
          </button>
        ) : exportJob.status === "building" ? (
          <div className="text-xs sp-subtle flex items-center gap-2" data-testid="privacy-export-building">
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
            Building your ZIP — this usually takes a few seconds.
          </div>
        ) : exportJob.status === "ready" && exportJob.download_url ? (
          <div className="flex items-center gap-3 flex-wrap" data-testid="privacy-export-ready">
            <a
              href={exportJob.download_url}
              className="sp-btn sp-btn-primary inline-flex items-center gap-2 text-sm"
              data-testid="privacy-export-download"
            >
              <Download className="w-4 h-4" />
              Download my data
            </a>
            <div className="text-[11px] sp-subtle">
              {exportJob.size_bytes ? `${(exportJob.size_bytes / 1024).toFixed(1)} KB` : ""}
              {exportJob.expires_at && ` · expires ${new Date(exportJob.expires_at).toLocaleString()}`}
              {" · single-use link"}
            </div>
          </div>
        ) : null}

        {exportJob?.status === "failed" && exportJob?.error && (
          <div className="text-[11px] text-red-400">
            Last attempt failed: {exportJob.error}
          </div>
        )}
      </div>

      {/* Delete section — hidden when already pending */}
      {!delStatus?.pending && (
        <div className="space-y-3 pt-4 border-t border-white/5" data-testid="privacy-delete-section">
          <div>
            <div className="text-sm font-medium">Delete my account</div>
            <p className="text-[12px] sp-subtle leading-relaxed">
              Permanently remove your account and personal data after a {delStatus?.grace_days || 30}-day grace
              period. Your messages in team threads will be anonymised rather than removed (to preserve
              other members' context). Stripe billing records, auth-event logs (12 mo retention),
              and aggregated analytics are kept under legal &amp; legitimate-interest bases.
            </p>
          </div>
          <button
            onClick={() => setShowDeleteWizard(true)}
            className="inline-flex items-center gap-2 text-sm px-4 py-2 rounded-md border border-red-500/40 text-red-400 hover:bg-red-500/10"
            data-testid="privacy-delete-open"
          >
            <Trash2 className="w-4 h-4" />
            Delete my account…
          </button>
        </div>
      )}

      {showDeleteWizard && (
        <DeleteWizard
          delStatus={delStatus}
          onClose={() => setShowDeleteWizard(false)}
          onCompleted={async () => {
            setShowDeleteWizard(false);
            await refreshDeletionStatus();
          }}
        />
      )}
    </div>
  );
}


function DeleteWizard({ delStatus, onClose, onCompleted }) {
  const [blockers, setBlockers] = useState(delStatus?.blockers || []);
  const [transferTarget, setTransferTarget] = useState({});  // org_id -> user_id
  const [working, setWorking] = useState(false);
  const [confirmText, setConfirmText] = useState("");

  // Refresh blockers whenever the wizard mounts — the user may have
  // resolved them in another tab.
  useEffect(() => {
    (async () => {
      try {
        const { data } = await api.get("/account/delete/status");
        setBlockers(data.blockers || []);
      } catch (e) { /* non-fatal */ }
    })();
  }, []);

  const transfer = async (orgId) => {
    const newOwner = transferTarget[orgId];
    if (!newOwner) {
      toast.error("Pick a new owner first.");
      return;
    }
    setWorking(true);
    try {
      await api.post("/account/delete/transfer-ownership", {
        org_id: orgId, new_owner_id: newOwner,
      });
      toast.success("Ownership transferred.");
      // Re-check blockers — that org should fall off the list.
      const { data } = await api.get("/account/delete/status");
      setBlockers(data.blockers || []);
    } catch (err) {
      toast.error(err?.response?.data?.detail?.message
        || err?.response?.data?.detail
        || "Could not transfer ownership");
    } finally {
      setWorking(false);
    }
  };

  const confirmDeletion = async () => {
    if (confirmText.trim().toUpperCase() !== "DELETE") {
      toast.error("Please type DELETE to confirm.");
      return;
    }
    setWorking(true);
    try {
      await api.post("/account/delete");
      toast.success("Deletion scheduled.");
      onCompleted?.();
    } catch (err) {
      const detail = err?.response?.data?.detail;
      const msg = (detail && typeof detail === "object" ? detail.message : detail)
        || "Could not schedule deletion";
      toast.error(msg);
    } finally {
      setWorking(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" data-testid="privacy-delete-wizard">
      <div className="w-full max-w-xl max-h-[85vh] overflow-hidden flex flex-col rounded-xl shadow-2xl border border-black/10 dark:border-white/10 bg-white text-zinc-900 dark:bg-zinc-900 dark:text-white">
        <div className="px-5 py-4 border-b border-black/10 dark:border-white/10 flex items-center justify-between">
          <div>
            <div className="text-base font-semibold flex items-center gap-2">
              <Trash2 className="w-4 h-4 text-red-500" /> Delete my account
            </div>
            <div className="text-[12px] opacity-70 mt-0.5">
              Final step. 30-day grace period applies; you can cancel any time before that.
            </div>
          </div>
          <button onClick={onClose} className="opacity-60 hover:opacity-100" data-testid="privacy-wizard-close">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="overflow-auto px-5 py-4 flex-1 space-y-4">
          {blockers.length > 0 ? (
            <>
              <div className="rounded-lg border border-amber-400/40 bg-amber-50/80 dark:bg-amber-500/10 text-amber-900 dark:text-amber-200 px-3 py-3 text-sm flex items-start gap-2">
                <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />
                <div>
                  You're the sole Owner of {blockers.length} org{blockers.length === 1 ? "" : "s"} with other
                  members. Transfer ownership before deleting your account, or these orgs (and everyone's
                  data inside them) would be left without a billing entity.
                </div>
              </div>
              <ul className="space-y-3">
                {blockers.map((b) => (
                  <li key={b.org_id} className="rounded-lg border border-black/10 dark:border-white/10 px-3 py-3" data-testid={`privacy-blocker-${b.org_id}`}>
                    <div className="text-sm font-medium">{b.org_name}</div>
                    <div className="text-[11px] opacity-70 mb-2">{b.member_count} member{b.member_count === 1 ? "" : "s"}</div>
                    {b.transfer_targets?.length === 0 ? (
                      <div className="text-[12px] text-amber-700 dark:text-amber-300">
                        No other members eligible to receive ownership. Remove the other members from
                        the org first, or contact support.
                      </div>
                    ) : (
                      <div className="flex items-center gap-2 flex-wrap">
                        <select
                          value={transferTarget[b.org_id] || ""}
                          onChange={(e) => setTransferTarget((m) => ({ ...m, [b.org_id]: e.target.value }))}
                          className="text-xs px-2 py-1.5 rounded-md bg-white dark:bg-zinc-800 border border-black/10 dark:border-white/10"
                          data-testid={`privacy-transfer-select-${b.org_id}`}
                        >
                          <option value="">— Choose new owner —</option>
                          {b.transfer_targets.map((t) => (
                            <option key={t.user_id} value={t.user_id}>
                              {t.name || t.email} ({t.role})
                            </option>
                          ))}
                        </select>
                        <button
                          onClick={() => transfer(b.org_id)}
                          disabled={working || !transferTarget[b.org_id]}
                          className="text-xs px-3 py-1.5 rounded-md bg-orange-500 text-white hover:bg-orange-600 disabled:opacity-50"
                          data-testid={`privacy-transfer-go-${b.org_id}`}
                        >
                          {working ? <Loader2 className="w-3 h-3 animate-spin" /> : "Transfer"}
                        </button>
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <>
              <div className="rounded-lg border border-red-400/40 bg-red-50/80 dark:bg-red-500/10 text-red-900 dark:text-red-200 px-3 py-3 text-sm space-y-2">
                <div className="font-medium flex items-center gap-2">
                  <AlertTriangle className="w-4 h-4 flex-shrink-0" />
                  This action is reversible for 30 days, then permanent.
                </div>
                <div className="text-[12px] opacity-90 leading-relaxed">
                  Within 30 days we'll hard-delete your account, your direct conversations, your
                  cross-org memory, and revoke every third-party integration you own. Messages
                  you wrote in team-visible threads will be anonymised (sender becomes
                  "[deleted user]") to preserve context for other members.
                </div>
              </div>
              <div>
                <div className="text-sm mb-2">Type <code className="font-mono px-1 py-0.5 bg-black/5 dark:bg-white/10 rounded">DELETE</code> to confirm:</div>
                <input
                  type="text"
                  value={confirmText}
                  onChange={(e) => setConfirmText(e.target.value)}
                  className="w-full px-3 py-2 rounded-md bg-white dark:bg-zinc-800 border border-black/10 dark:border-white/10 text-sm"
                  data-testid="privacy-delete-confirm-input"
                  autoFocus
                />
              </div>
            </>
          )}
        </div>

        <div className="px-5 py-3 border-t border-black/10 dark:border-white/10 flex items-center justify-end gap-2">
          <button onClick={onClose} className="text-xs px-3 py-1.5 rounded-md hover:bg-black/5 dark:hover:bg-white/5" data-testid="privacy-wizard-cancel">
            Cancel
          </button>
          {blockers.length === 0 && (
            <button
              onClick={confirmDeletion}
              disabled={working || confirmText.trim().toUpperCase() !== "DELETE"}
              className="text-xs px-3 py-1.5 rounded-md bg-red-600 hover:bg-red-700 text-white inline-flex items-center gap-1 disabled:opacity-40 disabled:cursor-not-allowed"
              data-testid="privacy-delete-confirm"
            >
              {working ? <Loader2 className="w-3 h-3 animate-spin" /> : <Trash2 className="w-3 h-3" />}
              Schedule deletion in 30 days
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
