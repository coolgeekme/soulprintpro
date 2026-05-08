import React, { useEffect, useState } from "react";
import api from "../lib/api";
import { ShieldCheck, Loader2, ExternalLink, Copy, Check, Link as LinkIcon } from "lucide-react";

/**
 * Tamper-evident integrity badge for any agent-generated artifact.
 *
 * Renders as a small inline pill — green "Verified ✓" once anchored,
 * orange "Pending anchor…" while the OpenTimestamps calendars batch
 * the digest into the next Bitcoin block. Clicking opens a popover
 * with the full proof details + a button to download the standard
 * `.ots` file so the customer can verify independently.
 *
 * Props:
 *   gridfsId — the GridFS file_id of the artifact. Required.
 *   compact  — when true, renders a tiny shield icon only (used in
 *              tight UI like document grids). Default false.
 */
export default function IntegrityBadge({ gridfsId, compact = false }) {
  const [proof, setProof] = useState(null);
  const [loading, setLoading] = useState(true);
  const [missing, setMissing] = useState(false);
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [linkCopied, setLinkCopied] = useState(false);

  useEffect(() => {
    if (!gridfsId) return;
    let alive = true;
    (async () => {
      try {
        const { data } = await api.get(`/files/${gridfsId}/proof`);
        if (alive) setProof(data);
      } catch (err) {
        if (alive && err?.response?.status === 404) setMissing(true);
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, [gridfsId]);

  if (loading) {
    return (
      <span className="inline-flex items-center gap-1 text-[10px] sp-subtle">
        <Loader2 className="w-3 h-3 animate-spin" />
        {!compact && "checking proof"}
      </span>
    );
  }

  if (missing || !proof) {
    return null;  // No proof exists — render nothing rather than confuse.
  }

  const anchored = proof.status === "anchored";
  const color = anchored ? "rgba(74, 222, 128, 1)" : "rgba(251, 146, 60, 1)";
  const bg = anchored ? "rgba(74, 222, 128, 0.12)" : "rgba(251, 146, 60, 0.12)";
  const label = anchored ? "Verified" : "Anchoring…";

  return (
    <div className="relative inline-block" data-testid={`integrity-badge-${gridfsId}`}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium transition hover:brightness-125"
        style={{ background: bg, color, border: `1px solid ${bg.replace("0.12", "0.3")}` }}
        title="Cryptographic proof of integrity (OpenTimestamps / Bitcoin)"
      >
        <ShieldCheck className="w-3 h-3" />
        {!compact && label}
      </button>
      {open && (
        <ProofPopover proof={proof} gridfsId={gridfsId} onClose={() => setOpen(false)}
                     onUpgrade={async () => {
                       try {
                         const { data } = await api.post(`/files/${gridfsId}/proof/upgrade`);
                         setProof(data);
                       } catch (_) { /* best-effort */ }
                     }}
                     copied={copied}
                     onCopyDigest={() => {
                       navigator.clipboard.writeText(proof.digest_hex);
                       setCopied(true);
                       setTimeout(() => setCopied(false), 1500);
                     }}
                     linkCopied={linkCopied}
                     onCopyLink={() => {
                       // Build the public, share-safe verify URL. Anyone
                       // (no login) can paste this into their browser
                       // and confirm the Bitcoin anchor — perfect for
                       // RFP responses, compliance docs, or sales.
                       const url = `${window.location.origin}/v/${gridfsId}`;
                       navigator.clipboard.writeText(url);
                       setLinkCopied(true);
                       setTimeout(() => setLinkCopied(false), 1800);
                     }}
        />
      )}
    </div>
  );
}


function ProofPopover({ proof, gridfsId, onClose, onUpgrade, copied, onCopyDigest, linkCopied, onCopyLink }) {
  const anchored = proof.status === "anchored";
  return (
    <>
      {/* Click-outside catcher — full-viewport invisible div absorbs
          stray clicks so the popover dismisses cleanly. */}
      <div className="fixed inset-0 z-40" onClick={onClose} />
      <div className="absolute z-50 right-0 mt-1 w-80 sp-glass-strong rounded-xl p-4 shadow-2xl"
           data-testid={`integrity-popover-${gridfsId}`}>
        <div className="flex items-center gap-2 mb-2">
          <ShieldCheck className="w-4 h-4 text-orange-300" />
          <div className="text-sm font-medium">Proof of Integrity</div>
        </div>
        <div className="text-xs sp-subtle leading-relaxed mb-3">
          {anchored
            ? "This file's SHA-256 hash is anchored on Bitcoin via OpenTimestamps. Tamper-evident — any byte change breaks the proof."
            : "This file's SHA-256 hash has been submitted to the OpenTimestamps calendar network and is pending the next Bitcoin block (~1-6 hours). Verifiable now via the calendars."}
        </div>

        <div className="space-y-2 text-[11px]">
          <Row label="Status" value={anchored ? "Bitcoin-anchored" : "Pending anchor"} mono={false}/>
          <Row label="SHA-256" value={proof.digest_hex} mono action={
            <button onClick={onCopyDigest} className="ml-1 hover:text-white" title="Copy digest">
              {copied ? <Check className="w-3 h-3 text-green-400" /> : <Copy className="w-3 h-3" />}
            </button>
          } />
          <Row label="Stamped" value={new Date(proof.created_at).toLocaleString()} mono={false}/>
          {anchored && proof.anchored_at && (
            <Row label="Anchored" value={new Date(proof.anchored_at).toLocaleString()} mono={false}/>
          )}
          <Row label="Calendars" value={`${(proof.calendars || []).length} of 3`} mono={false}/>
        </div>

        <div className="flex items-center gap-2 mt-3 pt-3 border-t border-white/10">
          <button
            type="button"
            onClick={onCopyLink}
            className="sp-btn sp-btn-primary text-[11px] flex-1"
            title="Copy a public, share-safe verify URL — anyone can confirm this proof without a SoulPrint account"
            data-testid={`copy-verify-link-${gridfsId}`}
          >
            {linkCopied ? <Check className="w-3 h-3" /> : <LinkIcon className="w-3 h-3" />}
            {linkCopied ? "Copied!" : "Copy verify link"}
          </button>
          <a
            href={`/api/integrity-proofs/${proof.id}/proof.ots`}
            className="sp-btn sp-btn-ghost text-[11px]"
            data-testid={`download-ots-${gridfsId}`}
            title="Download the standard .ots proof file"
          >
            <ExternalLink className="w-3 h-3" /> .ots
          </a>
          {!anchored && (
            <button
              type="button"
              onClick={onUpgrade}
              className="sp-btn sp-btn-ghost text-[11px]"
              title="Poll the calendars to check if the next Bitcoin block has been mined"
              data-testid={`upgrade-proof-${gridfsId}`}
            >
              Refresh
            </button>
          )}
        </div>
        <div className="text-[10px] sp-subtle mt-2 leading-relaxed">
          Verify independently with: <code className="px-1 sp-glass rounded">ots verify file.ots --file your_file</code>
        </div>
      </div>
    </>
  );
}


function Row({ label, value, mono, action }) {
  return (
    <div className="flex items-start gap-2">
      <div className="sp-subtle min-w-[70px]">{label}</div>
      <div className={`flex-1 break-all ${mono ? "font-mono text-[10px]" : ""}`}>
        {value}
        {action}
      </div>
    </div>
  );
}
