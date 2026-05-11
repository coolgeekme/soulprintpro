import React, { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { ShieldCheck, Loader2, Anchor, Copy, Check, Download, RefreshCw, ExternalLink, AlertCircle } from "lucide-react";
import Logo from "../components/Logo";
import { BACKEND_URL } from "../lib/api";

/**
 * Public verify page — `/v/<gridfs_id>`.
 *
 * Anyone (prospect, auditor, journalist, RFP reviewer) can hit this
 * URL with a file_id from a SoulPrint share-link and confirm the file
 * is Bitcoin-anchored via OpenTimestamps. No login required.
 *
 * What we show:
 *   * status (anchored / pending)
 *   * SHA-256 digest the file was stamped with
 *   * which OTS calendars accepted it
 *   * stamped-at and (if anchored) anchored-at timestamps
 *   * "Download .ots" — the standard proof file for independent
 *     verification with `ots verify`
 *   * step-by-step CLI instructions to verify against a copy of the
 *     original file
 *
 * What we DON'T show:
 *   * the filename or any other label (leak protection — names like
 *     "Acme_Q3_audit.pdf" would expose business context)
 *   * the original file bytes (we never proxied them to the public
 *     endpoint)
 */
export default function VerifyPublicPage() {
  const { fileId } = useParams();
  const [proof, setProof] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [copied, setCopied] = useState(false);

  const fetchProof = async () => {
    try {
      const r = await fetch(`${BACKEND_URL}/api/public/v/${fileId}`);
      if (!r.ok) {
        if (r.status === 404) setError("No proof exists for this file id.");
        else setError("Could not load the integrity proof. Please try again.");
        return;
      }
      const data = await r.json();
      setProof(data);
      setError("");
    } catch (e) {
      setError("Network error — could not reach the verifier.");
    }
  };

  useEffect(() => {
    if (!fileId) return;
    let alive = true;
    (async () => {
      await fetchProof();
      if (alive) setLoading(false);
    })();
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fileId]);

  const onRefresh = async () => {
    setRefreshing(true);
    try {
      const r = await fetch(`${BACKEND_URL}/api/public/v/${fileId}/upgrade`, { method: "POST" });
      if (r.ok) {
        const data = await r.json();
        setProof(data);
      }
    } catch (_) { /* best-effort */ }
    finally { setRefreshing(false); }
  };

  const onCopyDigest = () => {
    if (!proof?.digest_hex) return;
    navigator.clipboard.writeText(proof.digest_hex);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className="min-h-screen" data-testid="verify-public-page">
      {/* Public top bar — minimal, no auth-aware nav. */}
      <header className="border-b border-white/5">
        <div className="max-w-3xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between">
          <Link to="/" className="inline-flex items-center gap-2">
            <Logo variant="wordmark" className="h-6 w-auto" />
          </Link>
          <Link to="/security" className="sp-btn sp-btn-ghost text-xs"
                data-testid="verify-security-link">
            About our security
          </Link>
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-4 sm:px-6 py-10 sm:py-14">
        {/* Hero */}
        <div className="mb-6">
          <div className="inline-flex items-center gap-1.5 sp-glass rounded-full px-3 py-1 text-[10px] uppercase tracking-widest sp-subtle mb-3">
            <Anchor className="w-3 h-3" /> Proof of Integrity · OpenTimestamps · Bitcoin
          </div>
          <h1 className="text-2xl sm:text-4xl font-medium tracking-tight leading-[1.1]">
            Verify a SoulPrint artifact.
          </h1>
          <p className="text-sm sm:text-base sp-subtle leading-relaxed mt-2 max-w-2xl">
            Whoever shared this link claims the file&rsquo;s contents have been
            cryptographically anchored on Bitcoin. Below is the proof — and
            instructions to confirm it yourself, without trusting us or them.
          </p>
        </div>

        {loading && (
          <div className="flex items-center gap-2 sp-subtle text-sm">
            <Loader2 className="w-4 h-4 animate-spin" /> Loading proof…
          </div>
        )}

        {error && !loading && (
          <div className="sp-glass rounded-2xl p-5 flex items-start gap-3"
               data-testid="verify-error">
            <AlertCircle className="w-5 h-5 text-red-400 shrink-0 mt-0.5" />
            <div>
              <div className="text-sm font-medium">Proof not found</div>
              <div className="text-xs sp-subtle mt-1 leading-relaxed">{error}</div>
              <div className="text-xs sp-subtle mt-2">
                If you expected a proof here, double-check the file id in the URL.
                Older artifacts (created before the Proof of Integrity feature
                shipped) may not have a stamp.
              </div>
            </div>
          </div>
        )}

        {proof && (
          <>
            {/* Status card */}
            <StatusCard proof={proof} onRefresh={onRefresh} refreshing={refreshing} />

            {/* Details */}
            <section className="sp-glass rounded-2xl p-5 sm:p-6 mt-3">
              <div className="text-[10px] uppercase tracking-widest sp-subtle mb-3">Proof details</div>
              <DetailRow label="SHA-256 digest" mono>
                <div className="flex items-center gap-2 min-w-0">
                  <code className="break-all text-[11px] flex-1 min-w-0">{proof.digest_hex}</code>
                  <button
                    type="button"
                    onClick={onCopyDigest}
                    className="sp-btn sp-btn-ghost !p-1.5 shrink-0"
                    title="Copy digest"
                    data-testid="verify-copy-digest"
                  >
                    {copied ? <Check className="w-3 h-3 text-green-400" /> : <Copy className="w-3 h-3" />}
                  </button>
                </div>
              </DetailRow>
              <DetailRow label="Stamped">{fmtDate(proof.created_at)}</DetailRow>
              {proof.anchored_at && (
                <DetailRow label="Anchored">{fmtDate(proof.anchored_at)}</DetailRow>
              )}
              <DetailRow label="Calendars">
                <div className="flex flex-col gap-1">
                  {(proof.calendars || []).map((c) => (
                    <a key={c}
                       href={c} target="_blank" rel="noreferrer"
                       className="text-[11px] hover:text-white inline-flex items-center gap-1">
                      {c} <ExternalLink className="w-2.5 h-2.5 opacity-60" />
                    </a>
                  ))}
                </div>
              </DetailRow>
              <DetailRow label=".ots proof">
                <a href={`${BACKEND_URL}/api/public/v/${fileId}/proof.ots`}
                   className="sp-btn sp-btn-ghost text-[11px]"
                   data-testid="verify-download-ots">
                  <Download className="w-3 h-3" /> Download {proof.proof_size_bytes || ""} bytes
                </a>
              </DetailRow>
            </section>

            {/* Verify yourself */}
            <section className="sp-glass-strong rounded-2xl p-5 sm:p-6 mt-3" data-testid="verify-instructions">
              <div className="text-[10px] uppercase tracking-widest sp-subtle mb-2">
                Verify it yourself
              </div>
              <p className="text-sm sp-subtle leading-relaxed mb-3">
                You don&rsquo;t have to trust SoulPrint to confirm this proof is real.
                Anyone with the original file and the standard OpenTimestamps
                CLI can verify in three commands:
              </p>
              <ol className="space-y-2 text-sm">
                <Step n={1} title="Install the OpenTimestamps CLI">
                  <code className="block sp-glass rounded-lg p-2 text-[11px] mt-1 overflow-x-auto">
                    pip install opentimestamps-client
                  </code>
                </Step>
                <Step n={2} title="Download the proof">
                  <code className="block sp-glass rounded-lg p-2 text-[11px] mt-1 overflow-x-auto">
                    curl -O {BACKEND_URL}/api/public/v/{fileId}/proof.ots
                  </code>
                </Step>
                <Step n={3} title="Verify against your copy of the file">
                  <code className="block sp-glass rounded-lg p-2 text-[11px] mt-1 overflow-x-auto">
                    ots verify {fileId}.ots --file your_copy_of_the_original_file
                  </code>
                  <div className="text-[11px] sp-subtle mt-2 leading-relaxed">
                    The CLI will tell you which Bitcoin block height the file
                    is anchored in (or report &ldquo;pending&rdquo; if the proof
                    hasn&rsquo;t made it on-chain yet — usually &lt; 6 hours after
                    stamping).
                  </div>
                </Step>
              </ol>
            </section>

            {/* What this proves */}
            <section className="rounded-2xl p-5 sm:p-6 mt-3 border"
                     style={{
                       background: "linear-gradient(135deg, rgba(74,222,128,0.08), rgba(74,222,128,0.02))",
                       borderColor: "rgba(74,222,128,0.18)",
                     }}>
              <div className="flex items-start gap-3">
                <ShieldCheck className="w-5 h-5 text-green-300 shrink-0 mt-0.5" />
                <div className="text-sm sp-subtle leading-relaxed">
                  <span className="text-white/85 font-medium">What this proves:</span>{" "}
                  the exact byte-content of the file existed at the stamped
                  timestamp above. If you change a single byte and re-hash,
                  the digest won&rsquo;t match — the proof is broken. SoulPrint
                  cannot retroactively alter or backdate this proof; the
                  Bitcoin blockchain is the source of truth.
                </div>
              </div>
            </section>

            {/* Footer CTA */}
            <div className="mt-8 pt-6 border-t border-white/5 flex items-center justify-between flex-wrap gap-3">
              <div className="text-xs sp-subtle">
                Want this for your team&rsquo;s documents?
              </div>
              <Link to="/about" className="sp-btn sp-btn-primary text-xs"
                    data-testid="verify-cta-learn-more">
                Learn about Foundry →
              </Link>
            </div>
          </>
        )}
      </main>
    </div>
  );
}


// ── Sub-components ─────────────────────────────────────────────────────────


function StatusCard({ proof, onRefresh, refreshing }) {
  const anchored = proof.status === "anchored";
  return (
    <section className="rounded-2xl p-5 sm:p-6 border"
             style={{
               background: anchored
                 ? "linear-gradient(135deg, rgba(74,222,128,0.10), rgba(74,222,128,0.02))"
                 : "linear-gradient(135deg, rgba(251,146,60,0.10), rgba(251,146,60,0.02))",
               borderColor: anchored
                 ? "rgba(74,222,128,0.25)"
                 : "rgba(251,146,60,0.25)",
             }}
             data-testid={`verify-status-${proof.status}`}>
      <div className="flex items-start gap-3">
        <div className="w-10 h-10 rounded-xl flex items-center justify-center shrink-0"
             style={{ background: anchored ? "rgba(74,222,128,0.15)" : "rgba(251,146,60,0.15)" }}>
          {anchored ? <ShieldCheck className="w-5 h-5 text-green-300" />
                    : <Loader2 className="w-5 h-5 text-orange-300 animate-spin-slow" />}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h2 className="text-lg sm:text-xl font-medium tracking-tight">
              {anchored ? "Bitcoin-anchored" : "Anchoring in progress"}
            </h2>
            <span className="text-[10px] uppercase tracking-widest sp-subtle">
              {proof.status}
            </span>
          </div>
          <p className="text-xs sm:text-sm sp-subtle leading-relaxed mt-1">
            {anchored
              ? "This file's SHA-256 digest is permanently anchored in the Bitcoin blockchain via OpenTimestamps. The proof is tamper-evident and verifiable by anyone with the original file."
              : "This file's SHA-256 digest has been submitted to the OpenTimestamps calendar network and is queued for the next Bitcoin block. Confirmation typically takes 1-6 hours after stamping. The proof IS already valid — it'll just gain a stronger Bitcoin attestation once the next block lands."}
          </p>
        </div>
        {!anchored && (
          <button
            type="button"
            onClick={onRefresh}
            disabled={refreshing}
            className="sp-btn sp-btn-ghost text-[11px] shrink-0"
            data-testid="verify-refresh-btn"
            title="Poll the calendars to check if the next Bitcoin block is in"
          >
            {refreshing
              ? <Loader2 className="w-3 h-3 animate-spin" />
              : <RefreshCw className="w-3 h-3" />}
            Refresh
          </button>
        )}
      </div>
    </section>
  );
}


function DetailRow({ label, children, mono }) {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-[140px_1fr] gap-2 sm:gap-4 py-2 border-t border-white/5 first:border-t-0">
      <div className="text-[10px] uppercase tracking-widest sp-subtle">{label}</div>
      <div className={`min-w-0 ${mono ? "font-mono" : "text-sm"}`}>{children}</div>
    </div>
  );
}


function Step({ n, title, children }) {
  return (
    <li className="flex gap-3">
      <div className="w-6 h-6 rounded-full sp-glass-strong flex items-center justify-center text-[11px] shrink-0">
        {n}
      </div>
      <div className="flex-1 min-w-0">
        <div className="font-medium">{title}</div>
        {children}
      </div>
    </li>
  );
}


function fmtDate(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch (_) {
    return iso;
  }
}
