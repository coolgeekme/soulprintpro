import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import TopBar from "../components/TopBar";
import api from "../lib/api";
import { useAuth } from "../context/AuthContext";
import { toast } from "sonner";
import { parseServerDate, formatLocalDateTime } from "../lib/datetime";
import {
  Search, Loader2, ArrowRight, Download, ExternalLink, Trophy,
  Clock, AlertTriangle, FileText, Play, BarChart3, RefreshCw, Trash2,
} from "lucide-react";

const STATUS_META = {
  generating: { label: "Generating",  tint: "bg-amber-400/15 text-amber-300",     Icon: Loader2 },
  ready:      { label: "Ready",       tint: "bg-emerald-400/15 text-emerald-300", Icon: Trophy },
  failed:     { label: "Failed",      tint: "bg-red-400/15 text-red-300",         Icon: AlertTriangle },
};

function ScoreBadge({ score }) {
  if (score == null) return null;
  const tone =
    score >= 80 ? "text-emerald-300 bg-emerald-400/15" :
    score >= 60 ? "text-amber-300 bg-amber-400/15" :
                  "text-red-300 bg-red-400/15";
  return (
    <span
      className={`px-2 py-1 rounded-md text-sm font-medium ${tone}`}
      data-testid="audit-score-badge"
    >
      {score}
    </span>
  );
}

function StatusPill({ status }) {
  const meta = STATUS_META[status] || STATUS_META.generating;
  const { Icon, tint, label } = meta;
  return (
    <span className={`inline-flex items-center gap-1 text-[10px] uppercase tracking-widest px-1.5 py-0.5 rounded ${tint}`}>
      <Icon className={`w-3 h-3 ${status === "generating" ? "animate-spin" : ""}`} />
      {label}
    </span>
  );
}

export default function AuditsPage() {
  const { user } = useAuth(); // eslint-disable-line no-unused-vars
  const [orgs, setOrgs] = useState([]);
  const [orgId, setOrgId] = useState("");
  const [audits, setAudits] = useState([]);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [form, setForm] = useState({
    business_name: "",
    website_url: "",
    hint: "",
  });

  // Load user's orgs
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { data } = await api.get("/orgs");
        if (cancelled) return;
        setOrgs(Array.isArray(data) ? data : []);
        if (data?.[0]) setOrgId(data[0].id);
      } catch {
        toast.error("Could not load organizations");
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Load this org's audits
  const refresh = async (currentOrgId) => {
    const id = currentOrgId || orgId;
    if (!id) { setLoading(false); return; }
    setLoading(true);
    try {
      const { data } = await api.get("/audits", { params: { org_id: id, limit: 50 } });
      setAudits(Array.isArray(data) ? data : []);
    } catch {
      toast.error("Could not load audits");
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { refresh(orgId); /* eslint-disable-next-line */ }, [orgId]);

  // Poll while any audits are still generating
  useEffect(() => {
    if (!orgId) return;
    const stillRunning = audits.some(a => a.status === "generating");
    if (!stillRunning) return;
    const t = setInterval(() => refresh(orgId), 5000);
    return () => clearInterval(t);
  /* eslint-disable-next-line */
  }, [audits, orgId]);

  const submit = async (e) => {
    e?.preventDefault?.();
    if (!form.business_name.trim() || !form.website_url.trim()) {
      toast.error("Business name and website are required");
      return;
    }
    if (!orgId) {
      toast.error("Pick an organization first");
      return;
    }
    setRunning(true);
    try {
      const { data } = await api.post("/audits", {
        org_id: orgId,
        business_name: form.business_name.trim(),
        website_url: form.website_url.trim(),
        hint: form.hint.trim(),
      });
      toast.success(`Audit started — generating "${data.business_name}"`);
      setForm({ business_name: "", website_url: "", hint: "" });
      // Optimistically prepend so the user sees movement immediately
      setAudits((prev) => [data, ...prev]);
    } catch (err) {
      const msg = err?.response?.data?.detail || "Could not start audit";
      toast.error(typeof msg === "string" ? msg : "Could not start audit");
    } finally {
      setRunning(false);
    }
  };

  const downloadPdf = async (a) => {
    try {
      const { data } = await api.get(`/audits/${a.id}/pdf`);
      if (data?.url) window.open(data.url, "_blank");
      else toast.error("PDF not ready yet");
    } catch {
      toast.error("Could not fetch PDF");
    }
  };

  const retryAudit = async (a) => {
    try {
      const { data } = await api.post(`/audits/${a.id}/retry`);
      toast.success(`Re-running "${a.business_name}"`);
      setAudits((prev) => prev.map((x) => (x.id === a.id ? data : x)));
    } catch (err) {
      const msg = err?.response?.data?.detail || "Could not retry";
      toast.error(typeof msg === "string" ? msg : "Could not retry");
    }
  };

  const deleteAudit = async (a) => {
    if (!window.confirm(
      `Delete the audit for "${a.business_name}"? This removes the PDF, the public page, and any captured leads. This cannot be undone.`
    )) return;
    try {
      await api.delete(`/audits/${a.id}`);
      toast.success(`Deleted "${a.business_name}"`);
      setAudits((prev) => prev.filter((x) => x.id !== a.id));
    } catch (err) {
      const msg = err?.response?.data?.detail || "Could not delete";
      toast.error(typeof msg === "string" ? msg : "Could not delete");
    }
  };

  // A "generating" audit that hasn't progressed for >2 minutes is
  // almost certainly stuck (the watchdog will eventually flip it to
  // failed at 4 min, but the user shouldn't wait that long without
  // an escape hatch).
  // An audit is considered stuck once it's been "generating" longer
  // than the backend watchdog (6 min) gives it. We pad to 6.5 min so
  // a watchdog-killed audit lands in "failed" before the UI labels it
  // stuck — keeps state consistent.
  const isStuck = (a) => {
    if (a.status !== "generating") return false;
    const t = a.updated_at || a.created_at;
    const d = parseServerDate(t);
    if (!d) return false;
    return (Date.now() - d.getTime()) > 6.5 * 60 * 1000;
  };

  return (
    <>
      <TopBar />
      <main className="max-w-5xl mx-auto px-6 md:px-10 py-10 space-y-10" data-testid="audits-page">
        {/* Header */}
        <header className="flex items-start justify-between flex-wrap gap-4">
          <div>
            <div className="flex items-center gap-2 mb-2">
              <BarChart3 className="w-5 h-5 text-orange-300" />
              <span className="text-[10px] uppercase tracking-widest sp-subtle">
                Signal Audits
              </span>
            </div>
            <h1 className="sp-display text-3xl sm:text-4xl leading-tight">
              Audit any business in <span className="text-orange-300">~90 seconds</span>
            </h1>
            <p className="text-sm sp-subtle max-w-xl mt-2 leading-relaxed">
              Drop a website. We scan the public site, run Core Web Vitals,
              inventory tracking pixels, check email security, pull social
              signals and live market context — then surface gaps,
              opportunities, and a scored executive brief. Every Signal
              Audit is org-scoped and saved here.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-[10px] uppercase tracking-widest sp-subtle">Org</label>
            <select
              value={orgId}
              onChange={(e) => setOrgId(e.target.value)}
              className="sp-input text-sm"
              data-testid="audits-org-picker"
            >
              {orgs.map((o) => (
                <option key={o.id} value={o.id}>{o.name}</option>
              ))}
            </select>
          </div>
        </header>

        {/* New audit form */}
        <section className="sp-glass-strong rounded-2xl p-6" data-testid="audits-form-section">
          <form className="grid sm:grid-cols-[1fr_1.5fr_auto] gap-3" onSubmit={submit}>
            <div>
              <label className="text-[11px] uppercase tracking-widest sp-subtle">Business name</label>
              <input
                type="text"
                value={form.business_name}
                onChange={(e) => setForm({ ...form, business_name: e.target.value })}
                placeholder="Acme Coffee Roasters"
                className="sp-input w-full mt-1"
                maxLength={200}
                data-testid="audit-input-name"
                required
              />
            </div>
            <div>
              <label className="text-[11px] uppercase tracking-widest sp-subtle">Website URL</label>
              <input
                type="url"
                value={form.website_url}
                onChange={(e) => setForm({ ...form, website_url: e.target.value })}
                placeholder="https://acme.com"
                className="sp-input w-full mt-1"
                maxLength={400}
                data-testid="audit-input-url"
                required
              />
            </div>
            <div className="flex items-end">
              <button
                type="submit"
                disabled={running || !orgId}
                className="sp-btn sp-btn-primary text-sm w-full"
                data-testid="audit-submit"
              >
                {running ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
                {running ? "Starting…" : "Run Signal Audit"}
              </button>
            </div>
            <div className="sm:col-span-3">
              <label className="text-[11px] uppercase tracking-widest sp-subtle">
                Hint (optional) — anything specific to focus on
              </label>
              <input
                type="text"
                value={form.hint}
                onChange={(e) => setForm({ ...form, hint: e.target.value })}
                placeholder="e.g. We're a B2B SaaS targeting solo founders. Focus on conversion friction."
                className="sp-input w-full mt-1"
                maxLength={600}
                data-testid="audit-input-hint"
              />
            </div>
          </form>
          <p className="text-[11px] sp-subtle mt-3 leading-relaxed">
            Counts against your monthly audit allowance. Spark: 15 · Temper: 200 · Refine: 2,000.
          </p>
        </section>

        {/* History */}
        <section data-testid="audits-history-section">
          <div className="flex items-center justify-between mb-4">
            <h2 className="sp-display text-xl">
              Recent audits {audits.length > 0 && (
                <span className="sp-subtle text-sm font-normal">({audits.length})</span>
              )}
            </h2>
          </div>

          {loading ? (
            <div className="sp-glass rounded-xl p-8 text-center text-sm sp-subtle">
              <Loader2 className="w-4 h-4 animate-spin inline mr-2" /> Loading…
            </div>
          ) : audits.length === 0 ? (
            <div className="sp-glass rounded-xl p-8 text-center text-sm sp-subtle">
              No audits yet. Run your first one above — typical generation
              takes ~90 seconds.
            </div>
          ) : (
            <div className="grid sm:grid-cols-2 gap-3">
              {audits.map((a) => (
                <article
                  key={a.id}
                  className="sp-glass rounded-2xl p-5 flex flex-col gap-3 border border-white/10 hover:border-white/25 transition-colors"
                  data-testid={`audit-card-${a.id}`}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="font-medium truncate">{a.business_name}</div>
                      <a
                        href={a.website_url}
                        target="_blank"
                        rel="noreferrer noopener"
                        className="text-[11px] sp-subtle truncate flex items-center gap-1 hover:text-white transition-colors"
                      >
                        <ExternalLink className="w-3 h-3 shrink-0" />
                        <span className="truncate">{a.website_url}</span>
                      </a>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <ScoreBadge score={a.score} />
                      <StatusPill status={a.status} />
                    </div>
                  </div>

                  {a.executive_summary && (
                    <p className="text-xs sp-subtle line-clamp-3 leading-relaxed">
                      {a.executive_summary}
                    </p>
                  )}

                  <div className="flex items-center justify-between gap-2 pt-1 border-t border-white/10">
                    <div className="text-[10px] sp-subtle flex items-center gap-1">
                      <Clock className="w-3 h-3" />
                      {a.created_at ? formatLocalDateTime(a.created_at) : ""}
                    </div>
                    <div className="flex items-center gap-1">
                      {a.status === "ready" && (
                        <>
                          <Link
                            to={`/audit/${a.slug}`}
                            className="sp-btn sp-btn-ghost !p-1.5 text-[11px]"
                            data-testid={`audit-view-${a.id}`}
                            title="View full audit"
                            target="_blank"
                          >
                            <ArrowRight className="w-3.5 h-3.5" />
                          </Link>
                          {a.pdf_filename && (
                            <button
                              onClick={() => downloadPdf(a)}
                              className="sp-btn sp-btn-ghost !p-1.5 text-[11px]"
                              data-testid={`audit-pdf-${a.id}`}
                              title="Download PDF"
                            >
                              <Download className="w-3.5 h-3.5" />
                            </button>
                          )}
                        </>
                      )}
                      {(a.status === "failed" || isStuck(a)) && (
                        <button
                          onClick={() => retryAudit(a)}
                          className="sp-btn sp-btn-ghost !p-1.5 text-[11px] text-orange-300"
                          data-testid={`audit-retry-${a.id}`}
                          title={a.status === "failed" ? "Retry this audit" : "Audit appears stuck — retry"}
                        >
                          <RefreshCw className="w-3.5 h-3.5" />
                        </button>
                      )}
                      {a.status === "failed" && a.error && (
                        <span className="text-[10px] text-red-300/80 max-w-[180px] truncate" title={a.error}>
                          {a.error}
                        </span>
                      )}
                      <button
                        onClick={() => deleteAudit(a)}
                        className="sp-btn sp-btn-ghost !p-1.5 text-[11px] hover:text-red-300"
                        data-testid={`audit-delete-${a.id}`}
                        title="Delete this audit"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>
      </main>
    </>
  );
}
