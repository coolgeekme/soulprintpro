import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  Loader2, Plus, Trash2, RefreshCw, ExternalLink, Mail, Phone,
  AlertCircle, CheckCircle2, Clock, Building, FileText, Users as UsersIcon,
  Eye, X, Download, Sparkles, TrendingUp, AlertTriangle,
} from "lucide-react";
import { toast } from "sonner";
import api from "../lib/api";
import AuditFullReport from "./AuditFullReport";

/**
 * AdminAuditsPanel — superadmin-only prospecting tool.
 *
 * UX flow:
 *   1. Admin enters business name + website + (optional) social URLs.
 *   2. Backend kicks off a 3-10 minute audit pipeline (web fetch + Brave
 *      + LLM synthesis + PDF render). Row appears in the table with
 *      status=generating; the page polls every 5s until status=ready.
 *   3. Admin can view the public-facing audit URL, copy it for outreach,
 *      see the captured leads, and re-trigger generation if they want
 *      a fresher pass.
 *
 * Auth: this panel is rendered only inside AdminDashboard which is itself
 * gated by `user.is_superadmin` — but the backend ALSO 403s any non-super
 * hitting /api/admin/audits/*, so a tampered client gets nothing.
 */
const STATUS_BADGE = {
  generating: { label: "Generating…", icon: Clock,        cls: "bg-orange-500/15 text-orange-200 border-orange-500/30" },
  ready:      { label: "Ready",       icon: CheckCircle2, cls: "bg-emerald-500/15 text-emerald-200 border-emerald-500/30" },
  failed:     { label: "Failed",      icon: AlertCircle,  cls: "bg-red-500/15 text-red-200 border-red-500/30" },
};

export default function AdminAuditsPanel() {
  const [audits, setAudits] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [selected, setSelected] = useState(null); // an audit object or null
  const [leads, setLeads] = useState([]);
  const [leadsLoading, setLeadsLoading] = useState(false);
  // Admin "View report" modal — renders the same shared AuditFullReport
  // the prospect would see post-form, but with no gate. Distinct from
  // the leads modal so admins can quickly toggle between "what does the
  // prospect see" and "who's been on the page".
  const [viewing, setViewing] = useState(null);
  // Two-click delete — when a user clicks Trash, we set this to the
  // audit id and the button switches to a red "Confirm?" state for 4s.
  // A second click within that window actually deletes. Cleaner than
  // window.confirm() which gets blocked in some browsers, and can't be
  // mis-clicked away accidentally. Auto-resets via setTimeout.
  const [confirmDeleteId, setConfirmDeleteId] = useState(null);

  const reload = useCallback(async () => {
    try {
      const { data } = await api.get("/admin/audits");
      setAudits(data || []);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed to load audits");
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { reload(); }, [reload]);

  // Poll while ANY audit is still generating. Stops automatically when
  // every row has settled to ready/failed. Cheap (small list) — runs every
  // 5s rather than 4s to keep load on the LLM provider sane.
  useEffect(() => {
    const hasPending = audits.some((a) => a.status === "generating");
    if (!hasPending) return undefined;
    const id = setInterval(reload, 5000);
    return () => clearInterval(id);
  }, [audits, reload]);

  const onDelete = async (id) => {
    if (!window.confirm("Delete this audit and all its captured leads? This cannot be undone.")) return;
    try {
      await api.delete(`/admin/audits/${id}`);
      toast.success("Audit deleted");
      reload();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Delete failed");
    }
  };

  // Direct PDF download from the row — opens the persisted PDF in a new
  // tab so the admin can save / print / email it without first opening
  // the in-app full-report modal.
  const [pdfBusy, setPdfBusy] = useState(null); // audit id currently downloading
  const downloadAuditPdf = async (audit) => {
    setPdfBusy(audit.id);
    try {
      const { data } = await api.get(`/admin/audits/${audit.id}/pdf`);
      if (!data?.url) throw new Error("no-url");
      // Force a download by clicking a synthetic anchor — works around
      // browsers that ignore window.open on slower links.
      const a = document.createElement("a");
      a.href = data.url;
      a.download = data.filename || `audit-${audit.slug}.pdf`;
      a.target = "_blank";
      a.rel = "noopener";
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch (err) {
      toast.error(
        err?.response?.data?.detail
        || "PDF not available — try regenerating the audit",
      );
    } finally {
      setPdfBusy(null);
    }
  };

  const onRegenerate = async (id) => {
    try {
      await api.post(`/admin/audits/${id}/regenerate`);
      toast.success("Re-running audit — this takes a few minutes");
      reload();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Regenerate failed");
    }
  };

  const openLeads = async (audit) => {
    setSelected(audit);
    setLeadsLoading(true);
    setLeads([]);
    try {
      const { data } = await api.get(`/admin/audits/${audit.id}/leads`);
      setLeads(data || []);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed to load leads");
    } finally { setLeadsLoading(false); }
  };

  const copyPublicUrl = (slug) => {
    const url = `${window.location.origin}/audit/${slug}`;
    navigator.clipboard.writeText(url).then(
      () => toast.success("Public link copied — share it with the prospect"),
      () => toast.error("Couldn't copy — select & copy from the URL pill instead"),
    );
  };

  if (loading) {
    return (
      <div className="flex justify-center py-12">
        <Loader2 className="w-6 h-6 animate-spin opacity-60" />
      </div>
    );
  }

  return (
    <div className="space-y-4" data-testid="admin-audits-panel">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div>
          <div className="text-[10px] uppercase tracking-widest sp-subtle">Prospecting tool</div>
          <h2 className="text-2xl font-medium tracking-tight">Business Audits</h2>
          <p className="text-xs sp-subtle mt-1 max-w-2xl leading-relaxed">
            Generate a full audit of any business — website, social, reviews, brand voice, competitors —
            then share a gated public link that converts cold prospects into qualified leads. Leads land
            in your inbox + this panel.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setShowCreate(true)}
          className="sp-btn sp-btn-primary text-xs"
          data-testid="audits-new-btn"
        >
          <Plus className="w-3.5 h-3.5" /> New audit
        </button>
      </div>

      {audits.length === 0 ? (
        <div className="sp-glass rounded-2xl p-10 text-center">
          <Building className="w-8 h-8 mx-auto opacity-40 mb-3" />
          <div className="text-sm sp-subtle">No audits yet. Click <b>New audit</b> to scan your first prospect.</div>
        </div>
      ) : (
        <div className="sp-glass rounded-2xl overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-widest sp-subtle">
                <th className="px-4 py-3 font-medium">Business</th>
                <th className="px-4 py-3 font-medium">Score</th>
                <th className="px-4 py-3 font-medium">Status</th>
                <th className="px-4 py-3 font-medium">Leads</th>
                <th className="px-4 py-3 font-medium">Views</th>
                <th className="px-4 py-3 font-medium">Public link</th>
                <th className="px-4 py-3 font-medium" />
              </tr>
            </thead>
            <tbody>
              {audits.map((a) => {
                const badge = STATUS_BADGE[a.status] || STATUS_BADGE.generating;
                const BadgeIcon = badge.icon;
                return (
                  <tr key={a.id} className="border-t border-white/5" data-testid={`audit-row-${a.id}`}>
                    <td className="px-4 py-3">
                      <div className="font-medium">{a.business_name}</div>
                      <a href={a.website_url} target="_blank" rel="noopener noreferrer"
                         className="text-[11px] sp-subtle hover:text-orange-300 inline-flex items-center gap-1">
                        {a.website_url} <ExternalLink className="w-3 h-3" />
                      </a>
                    </td>
                    <td className="px-4 py-3">
                      {typeof a.score === "number" ? (
                        <div className="text-2xl font-medium tabular-nums">{a.score}</div>
                      ) : (
                        <span className="sp-subtle text-xs">—</span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] border ${badge.cls}`}>
                        <BadgeIcon className={`w-3 h-3 ${a.status === "generating" ? "animate-spin" : ""}`} />
                        {badge.label}
                      </span>
                      {a.status === "failed" && a.error && (
                        <div className="text-[10px] text-red-300/80 mt-1 max-w-xs truncate" title={a.error}>
                          {a.error}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <button
                        type="button"
                        onClick={() => openLeads(a)}
                        className="text-orange-300 hover:underline text-xs inline-flex items-center gap-1"
                        data-testid={`audit-leads-${a.id}`}
                      >
                        <UsersIcon className="w-3 h-3" /> {a.lead_count || 0}
                      </button>
                    </td>
                    <td className="px-4 py-3 sp-subtle text-xs tabular-nums">{a.view_count || 0}</td>
                    <td className="px-4 py-3">
                      {a.status === "ready" ? (
                        <button
                          type="button"
                          onClick={() => copyPublicUrl(a.slug)}
                          className="text-xs sp-btn sp-btn-ghost"
                          data-testid={`audit-copy-${a.id}`}
                          title={`/audit/${a.slug}`}
                        >
                          Copy link
                        </button>
                      ) : (
                        <span className="sp-subtle text-xs">—</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className="inline-flex gap-1">
                        {a.status === "ready" && (
                          <>
                            {/* Admin-only "View report" — opens an in-app
                                modal with the full audit body. No lead
                                form, no gating — admins already have
                                this row in their dashboard. */}
                            <button
                              type="button" onClick={() => setViewing(a)}
                              className="sp-btn sp-btn-ghost text-xs"
                              data-testid={`audit-view-${a.id}`}
                              title="View the full audit report inline (no lead form)"
                            >
                              <FileText className="w-3 h-3" />
                            </button>
                            {/* Same audit, but rendered through the
                                public page so the admin can see what
                                the prospect sees (teaser + form). */}
                            <a
                              href={`/audit/${a.slug}`}
                              target="_blank" rel="noopener noreferrer"
                              className="sp-btn sp-btn-ghost text-xs"
                              data-testid={`audit-preview-${a.id}`}
                              title="Open the public-facing prospect page in a new tab"
                            >
                              <Eye className="w-3 h-3" />
                            </a>
                            {a.pdf_filename && (
                              <button
                                type="button"
                                onClick={() => downloadAuditPdf(a)}
                                disabled={pdfBusy === a.id}
                                className="sp-btn sp-btn-ghost text-xs"
                                data-testid={`audit-pdf-${a.id}`}
                                title="Download the audit as a PDF"
                              >
                                {pdfBusy === a.id
                                  ? <Loader2 className="w-3 h-3 animate-spin" />
                                  : <Download className="w-3 h-3" />}
                              </button>
                            )}
                          </>
                        )}
                        <button type="button" onClick={() => onRegenerate(a.id)}
                                className="sp-btn sp-btn-ghost text-xs"
                                data-testid={`audit-regen-${a.id}`}
                                title="Re-run the audit pipeline">
                          <RefreshCw className="w-3 h-3" />
                        </button>
                        <button type="button" onClick={() => onDelete(a.id)}
                                className="sp-btn sp-btn-ghost text-xs text-red-300 hover:text-red-200"
                                data-testid={`audit-delete-${a.id}`}
                                title="Delete this audit + all its leads">
                          <Trash2 className="w-3 h-3" />
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {showCreate && (
        <CreateAuditModal
          onClose={() => setShowCreate(false)}
          onCreated={(a) => {
            setShowCreate(false);
            setAudits((prev) => [a, ...prev]);
            toast.success(`Audit started for ${a.business_name} — takes 3-10 minutes`);
          }}
        />
      )}

      {selected && (
        <LeadsModal
          audit={selected}
          leads={leads}
          loading={leadsLoading}
          onClose={() => { setSelected(null); setLeads([]); }}
        />
      )}

      {viewing && (
        <ViewAuditModal
          audit={viewing}
          onClose={() => setViewing(null)}
        />
      )}
    </div>
  );
}


function CreateAuditModal({ onClose, onCreated }) {
  const [businessName, setBusinessName] = useState("");
  const [websiteUrl, setWebsiteUrl] = useState("");
  const [linkedin, setLinkedin] = useState("");
  const [twitter, setTwitter] = useState("");
  const [instagram, setInstagram] = useState("");
  const [hint, setHint] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (!businessName.trim() || !websiteUrl.trim()) return;
    setSubmitting(true);
    try {
      const socials = {};
      if (linkedin.trim()) socials.linkedin = linkedin.trim();
      if (twitter.trim()) socials.twitter = twitter.trim();
      if (instagram.trim()) socials.instagram = instagram.trim();
      const { data } = await api.post("/admin/audits", {
        business_name: businessName.trim(),
        website_url: websiteUrl.trim(),
        socials: Object.keys(socials).length ? socials : undefined,
        hint: hint.trim() || undefined,
      });
      onCreated(data);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not start audit");
    } finally { setSubmitting(false); }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={onClose}>
      <form
        onClick={(e) => e.stopPropagation()}
        onSubmit={submit}
        className="sp-glass-strong rounded-2xl p-6 max-w-lg w-full mx-4 space-y-4"
        data-testid="audit-create-modal"
      >
        <div className="flex items-center justify-between">
          <div>
            <div className="text-[10px] uppercase tracking-widest sp-subtle">Prospect</div>
            <h3 className="text-xl font-medium">New business audit</h3>
          </div>
          <button type="button" onClick={onClose} className="sp-btn sp-btn-ghost text-xs">
            <X className="w-3.5 h-3.5" />
          </button>
        </div>

        <label className="block">
          <div className="text-[11px] uppercase tracking-widest sp-subtle mb-1">Business name</div>
          <input
            type="text" required value={businessName} onChange={(e) => setBusinessName(e.target.value)}
            placeholder="e.g. Acme Widgets"
            className="w-full rounded-lg bg-black/30 border border-white/10 px-3 py-2 text-sm focus:outline-none focus:border-orange-500/50"
            data-testid="audit-create-name"
          />
        </label>

        <label className="block">
          <div className="text-[11px] uppercase tracking-widest sp-subtle mb-1">Website URL</div>
          <input
            type="text" required value={websiteUrl} onChange={(e) => setWebsiteUrl(e.target.value)}
            placeholder="acmewidgets.com"
            className="w-full rounded-lg bg-black/30 border border-white/10 px-3 py-2 text-sm focus:outline-none focus:border-orange-500/50"
            data-testid="audit-create-url"
          />
        </label>

        <details className="text-sm">
          <summary className="cursor-pointer text-xs sp-subtle hover:text-white">Optional: known social profiles</summary>
          <div className="mt-2 space-y-2">
            <input type="text" placeholder="LinkedIn URL" value={linkedin} onChange={(e) => setLinkedin(e.target.value)}
                   className="w-full rounded-lg bg-black/30 border border-white/10 px-3 py-2 text-xs"/>
            <input type="text" placeholder="Twitter / X URL" value={twitter} onChange={(e) => setTwitter(e.target.value)}
                   className="w-full rounded-lg bg-black/30 border border-white/10 px-3 py-2 text-xs"/>
            <input type="text" placeholder="Instagram URL" value={instagram} onChange={(e) => setInstagram(e.target.value)}
                   className="w-full rounded-lg bg-black/30 border border-white/10 px-3 py-2 text-xs"/>
          </div>
        </details>

        <label className="block">
          <div className="text-[11px] uppercase tracking-widest sp-subtle mb-1">Context for the auditor (optional)</div>
          <textarea
            value={hint} onChange={(e) => setHint(e.target.value)}
            placeholder="e.g. SaaS HR tool targeting mid-market HR teams. They reached out about lead-gen support."
            rows={3}
            className="w-full rounded-lg bg-black/30 border border-white/10 px-3 py-2 text-xs resize-y focus:outline-none focus:border-orange-500/50"
            data-testid="audit-create-hint"
          />
        </label>

        <div className="flex justify-end gap-2 pt-2">
          <button type="button" onClick={onClose}
                  className="sp-btn sp-btn-ghost text-xs" disabled={submitting}>Cancel</button>
          <button type="submit"
                  className="sp-btn sp-btn-primary text-xs disabled:opacity-50"
                  disabled={submitting || !businessName.trim() || !websiteUrl.trim()}
                  data-testid="audit-create-submit">
            {submitting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />}
            {submitting ? "Starting…" : "Start audit"}
          </button>
        </div>
      </form>
    </div>
  );
}


function LeadsModal({ audit, leads, loading, onClose }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={onClose}>
      <div
        onClick={(e) => e.stopPropagation()}
        className="sp-glass-strong rounded-2xl p-6 max-w-2xl w-full mx-4 max-h-[80vh] overflow-auto"
        data-testid="audit-leads-modal"
      >
        <div className="flex items-center justify-between mb-3">
          <div>
            <div className="text-[10px] uppercase tracking-widest sp-subtle">Captured leads</div>
            <h3 className="text-xl font-medium">{audit.business_name}</h3>
            <a href={`/audit/${audit.slug}`} target="_blank" rel="noopener noreferrer"
               className="text-[11px] sp-subtle hover:text-orange-300 inline-flex items-center gap-1">
              /audit/{audit.slug} <ExternalLink className="w-3 h-3" />
            </a>
          </div>
          <button type="button" onClick={onClose} className="sp-btn sp-btn-ghost text-xs">
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
        {loading ? (
          <div className="py-8 text-center"><Loader2 className="w-5 h-5 animate-spin opacity-60 mx-auto" /></div>
        ) : leads.length === 0 ? (
          <div className="py-8 text-center sp-subtle text-sm">
            No leads captured yet. Share <code className="bg-black/30 px-1.5 py-0.5 rounded">/audit/{audit.slug}</code> with prospects.
          </div>
        ) : (
          <ul className="space-y-2">
            {leads.map((l) => (
              <li key={l.id} className="sp-glass rounded-xl p-3" data-testid={`lead-row-${l.id}`}>
                <div className="flex items-baseline justify-between gap-3">
                  <div className="font-medium">{l.name} <span className="sp-subtle text-xs">— {l.role} @ {l.company}</span></div>
                  <div className="text-[10px] sp-subtle tabular-nums shrink-0">{new Date(l.submitted_at).toLocaleString()}</div>
                </div>
                <div className="flex flex-wrap gap-3 text-xs sp-subtle mt-1">
                  <a href={`mailto:${l.email}`} className="inline-flex items-center gap-1 hover:text-orange-300">
                    <Mail className="w-3 h-3" /> {l.email}
                  </a>
                  <a href={`tel:${l.phone}`} className="inline-flex items-center gap-1 hover:text-orange-300">
                    <Phone className="w-3 h-3" /> {l.phone}
                  </a>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}


/**
 * ViewAuditModal — admin-only "show me the full audit without a lead
 * form" modal. Renders the same `AuditFullReport` body the prospect
 * gets after submitting the lead form, plus the executive summary,
 * score header, strengths + issues, and a one-click PDF download
 * button that hits the admin-only `/admin/audits/{id}/pdf` endpoint.
 *
 * The list-row payload from `/admin/audits` already contains every
 * field we need (the admin serializer returns the full document) so
 * we don't re-fetch.
 */
function ViewAuditModal({ audit, onClose }) {
  const [downloading, setDownloading] = useState(false);

  const downloadPdf = async () => {
    setDownloading(true);
    try {
      const { data } = await api.get(`/admin/audits/${audit.id}/pdf`);
      const a = document.createElement("a");
      a.href = data.url;
      a.download = data.filename || `audit-${audit.slug}.pdf`;
      a.target = "_blank";
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "PDF not available — try regenerating the audit");
    } finally { setDownloading(false); }
  };

  // Score color band (matches the public ScoreRing color logic so the
  // same audit reads consistently across surfaces).
  const score = Number(audit.score) || 0;
  const scoreColor = score >= 80 ? "text-emerald-300"
                    : score >= 60 ? "text-amber-300"
                    : "text-red-300";

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/70 backdrop-blur-sm overflow-y-auto py-8 px-4"
      onClick={onClose}
      data-testid="audit-view-modal"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="sp-glass-strong rounded-2xl p-6 sm:p-8 max-w-4xl w-full my-auto"
      >
        {/* Header: business name + score + actions */}
        <div className="flex items-start justify-between gap-4 mb-6 flex-wrap">
          <div className="min-w-0">
            <div className="text-[10px] uppercase tracking-widest sp-subtle">Full audit · admin view</div>
            <h2 className="text-2xl sm:text-3xl font-medium tracking-tight">{audit.business_name}</h2>
            {audit.website_url && (
              <a href={audit.website_url} target="_blank" rel="noopener noreferrer"
                 className="text-xs sp-subtle hover:text-orange-300 inline-flex items-center gap-1 mt-1">
                {audit.website_url} <ExternalLink className="w-3 h-3" />
              </a>
            )}
          </div>
          <div className="flex items-center gap-3 shrink-0">
            <div className="text-right">
              <div className={`text-4xl font-medium tabular-nums ${scoreColor}`}>{score}</div>
              <div className="text-[10px] uppercase tracking-widest sp-subtle">Health score</div>
            </div>
            <div className="flex flex-col gap-1.5">
              {audit.pdf_filename && (
                <button
                  type="button" onClick={downloadPdf} disabled={downloading}
                  className="sp-btn sp-btn-primary text-xs disabled:opacity-50"
                  data-testid="audit-view-pdf-download"
                >
                  {downloading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Download className="w-3.5 h-3.5" />}
                  PDF
                </button>
              )}
              <button type="button" onClick={onClose}
                      className="sp-btn sp-btn-ghost text-xs"
                      data-testid="audit-view-close">
                <X className="w-3.5 h-3.5" /> Close
              </button>
            </div>
          </div>
        </div>

        {/* Executive summary + score rationale */}
        {(audit.executive_summary || audit.score_rationale) && (
          <div className="sp-glass rounded-xl p-4 mb-6 space-y-2">
            {audit.executive_summary && (
              <div className="text-sm leading-relaxed italic">{audit.executive_summary}</div>
            )}
            {audit.score_rationale && (
              <div className="text-xs sp-subtle leading-relaxed">
                <b>Why this score:</b> {audit.score_rationale}
              </div>
            )}
          </div>
        )}

        {/* Strengths + Issues — admin sees these inline (the public
            page renders them above the lead form, so we toggle on the
            shared component flag). */}
        <div className="grid sm:grid-cols-2 gap-3 mb-6">
          <FullStrengthsIssuesCard
            icon={TrendingUp}
            title="Top strengths"
            items={audit.strengths || []}
            accent="emerald"
          />
          <FullStrengthsIssuesCard
            icon={AlertTriangle}
            title="Critical issues"
            items={audit.issues || []}
            accent="orange"
          />
        </div>

        {/* The shared full-report body (recommendations + competitors +
            brand voice gap + 4 section paragraphs). */}
        <AuditFullReport audit={audit} />

        {/* Public-link footer — quick way to copy the prospect URL
            without leaving the modal. */}
        <div className="mt-8 pt-4 border-t border-white/5 flex items-center justify-between gap-3 flex-wrap">
          <div className="text-[11px] sp-subtle">
            Public link:{" "}
            <code className="bg-black/30 px-1.5 py-0.5 rounded ml-1">/audit/{audit.slug}</code>
          </div>
          <a href={`/audit/${audit.slug}`} target="_blank" rel="noopener noreferrer"
             className="sp-btn sp-btn-ghost text-xs"
             data-testid="audit-view-open-public">
            <Eye className="w-3 h-3" /> Open prospect page
          </a>
        </div>
      </div>
    </div>
  );
}


/**
 * Strengths/Issues card used inside the admin View modal. Same look as
 * the public-page TeaserCard but renders the full detail copy (no blur)
 * since admins don't need the gate.
 */
function FullStrengthsIssuesCard({ icon: Icon, title, items, accent }) {
  const border = accent === "emerald" ? "border-emerald-500/30" : "border-orange-500/30";
  return (
    <div className={`sp-glass rounded-xl p-4 border ${border}`}>
      <div className="flex items-center gap-2 mb-3">
        <Icon className="w-4 h-4 opacity-80" />
        <div className="text-[11px] uppercase tracking-widest sp-subtle">{title}</div>
      </div>
      {items.length === 0 ? (
        <div className="text-xs sp-subtle">—</div>
      ) : (
        <ul className="space-y-3">
          {items.map((it, i) => (
            <li key={i}>
              <div className="font-medium text-sm">{it.headline}</div>
              {it.detail && (
                <div className="text-xs sp-subtle mt-1 leading-relaxed">{it.detail}</div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

