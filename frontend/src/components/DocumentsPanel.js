import React, { useEffect, useMemo, useState } from "react";
import api, { BACKEND_URL, downloadFile } from "../lib/api";
import { toast } from "sonner";
import {
  FileText, FileCode2, FileSpreadsheet, Calendar, Mail, User as UserIcon,
  Download, Trash2, Search, ScrollText, Wand2, X as XIcon,
} from "lucide-react";

/**
 * DocumentsPanel — Org-wide library of every artifact agents have generated.
 *
 * Populated automatically: when an agent emits a `[[ARTIFACT ...]]` block in
 * a reply, the backend persists the file + a `documents` row, and this
 * panel surfaces it. Search by filename / title / content / agent. Click to
 * preview text-like docs inline; download or delete with the row actions.
 */
const KIND_ICON = {
  calendar: Calendar,
  contact: UserIcon,
  email: Mail,
  data: FileSpreadsheet,
  text: FileText,
  html: FileCode2,
  code: FileCode2,
  file: FileText,
};

const KIND_FILTERS = [
  { key: "", label: "All" },
  { key: "text", label: "Briefs" },
  { key: "data", label: "Data" },
  { key: "calendar", label: "Calendar" },
  { key: "email", label: "Email" },
  { key: "code", label: "Scripts" },
  { key: "html", label: "Web" },
  { key: "contact", label: "Contacts" },
];

const niceSize = (n) => {
  if (!n && n !== 0) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
};

const niceDate = (iso) => {
  try {
    return new Date(iso).toLocaleString([], {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  } catch { return ""; }
};

// Initial number of documents to show before the user expands the list. Org
// libraries can balloon past 100+ artifacts very quickly once agents start
// generating briefs / CSVs / calendar invites; rendering them all by default
// makes the org page feel cluttered.
const DOCS_INITIAL_LIMIT = 8;

export default function DocumentsPanel({ orgId }) {
  const [docs, setDocs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [kind, setKind] = useState("");
  const [preview, setPreview] = useState(null); // {doc, content}
  const [previewBusy, setPreviewBusy] = useState(false);
  const [expanded, setExpanded] = useState(false);

  // Collapse back to the truncated view whenever the result set changes
  // (search query / filter), so a new search starts compact again.
  useEffect(() => { setExpanded(false); }, [q, kind]);

  const load = async () => {
    setLoading(true);
    try {
      const params = {};
      if (q.trim()) params.q = q.trim();
      if (kind) params.kind = kind;
      const { data } = await api.get(`/orgs/${orgId}/documents`, { params });
      setDocs(data || []);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed to load documents");
    } finally { setLoading(false); }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [orgId, kind]);
  // Debounced search — re-query 250ms after the user stops typing
  useEffect(() => {
    const t = setTimeout(() => load(), 250);
    return () => clearTimeout(t);
    // eslint-disable-next-line
  }, [q]);

  const removeDoc = async (id) => {
    if (!window.confirm("Delete this document? It can't be recovered.")) return;
    try {
      await api.delete(`/orgs/${orgId}/documents/${id}`);
      setDocs((prev) => prev.filter((d) => d.id !== id));
      if (preview?.doc?.id === id) setPreview(null);
      toast.success("Deleted");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Delete failed");
    }
  };

  const openPreview = async (doc) => {
    setPreview({ doc, content: doc.preview || "" });
    // For text-like docs, fetch the full body so the preview isn't truncated
    if (["text", "data", "html", "code", "calendar", "email", "contact"].includes(doc.kind)) {
      setPreviewBusy(true);
      try {
        const url = doc.url.startsWith("http") ? doc.url : `${BACKEND_URL}${doc.url}`;
        const r = await fetch(url);
        if (r.ok) {
          const txt = await r.text();
          setPreview({ doc, content: txt });
        }
      } catch {/* keep the preview snippet */}
      finally { setPreviewBusy(false); }
    }
  };

  const counts = useMemo(() => {
    const c = {};
    docs.forEach((d) => { c[d.kind || "file"] = (c[d.kind || "file"] || 0) + 1; });
    return c;
  }, [docs]);

  return (
    <section id="documents" className="sp-glass-strong rounded-2xl p-6 mb-8" data-testid="documents-panel">
      <div className="flex items-start justify-between gap-4 mb-4 flex-wrap">
        <div className="min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <ScrollText className="w-4 h-4 text-white/80" />
            <h3 className="text-sm uppercase tracking-widest sp-subtle">Documents</h3>
            <span className="sp-chip text-[10px]" data-testid="documents-count">
              {docs.length}
            </span>
          </div>
          <p className="text-xs sp-subtle max-w-2xl">
            Anything your agents generate — calendar invites, briefs, CSVs, scripts, emails — lands here automatically. Search, download, or reuse.
          </p>
        </div>
        <div className="flex items-center gap-2 sp-glass rounded-xl px-3 py-2 w-full sm:w-auto">
          <Search className="w-3.5 h-3.5 text-white/60 shrink-0" />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search by name, agent, or content…"
            className="flex-1 bg-transparent text-sm text-white placeholder:text-white/30 focus:outline-none w-full sm:w-64"
            data-testid="documents-search"
          />
        </div>
      </div>

      <div className="flex flex-wrap gap-1.5 mb-4">
        {KIND_FILTERS.map((f) => (
          <button
            key={f.key || "all"}
            onClick={() => setKind(f.key)}
            className={`sp-chip ${kind === f.key ? "!bg-white/15 !text-white" : ""}`}
            data-testid={`documents-filter-${f.key || "all"}`}
          >
            {f.label}
            {/* UX audit #9: every filter chip — including "All" — shows
                a count so it never appears blank. The All count is the
                total of every kind. */}
            {f.key
              ? (counts[f.key] ? <span className="opacity-60 ml-1">· {counts[f.key]}</span> : null)
              : <span className="opacity-60 ml-1">· {Object.values(counts).reduce((a, b) => a + b, 0)}</span>}
          </button>
        ))}
      </div>

      {loading && docs.length === 0 ? (
        <div className="sp-subtle text-sm py-6 text-center">Loading documents…</div>
      ) : docs.length === 0 ? (
        <div className="sp-glass rounded-xl p-6 text-center">
          <Wand2 className="w-5 h-5 mx-auto mb-2 text-white/60" />
          <div className="text-sm">Nothing here yet.</div>
          <div className="text-xs sp-subtle mt-1 max-w-md mx-auto">
            Ask any agent to schedule a meeting, draft an email, or build a checklist.
            They'll generate a real, downloadable file and drop it here.
          </div>
        </div>
      ) : (
        <>
        <ul className="grid sm:grid-cols-2 gap-2" data-testid="documents-list">
          {(expanded ? docs : docs.slice(0, DOCS_INITIAL_LIMIT)).map((d) => {
            const Icon = KIND_ICON[d.kind] || FileText;
            const url = d.url?.startsWith("http") ? d.url : `${BACKEND_URL}${d.url}`;
            return (
              <li
                key={d.id}
                className="sp-glass rounded-xl p-3 flex items-start gap-3 hover:border-white/25 transition group"
                data-testid={`document-${d.id}`}
              >
                <button
                  type="button"
                  onClick={() => openPreview(d)}
                  className="w-9 h-9 rounded-lg flex items-center justify-center sp-glass-strong shrink-0 hover:scale-105 transition"
                  title="Preview"
                  data-testid={`document-preview-${d.id}`}
                >
                  <Icon className="w-4 h-4" />
                </button>
                <button
                  type="button"
                  onClick={() => openPreview(d)}
                  className="min-w-0 flex-1 text-left"
                >
                  <div className="text-sm font-medium truncate" title={d.original_name}>
                    {d.title || d.original_name}
                  </div>
                  <div className="text-[11px] sp-subtle truncate">
                    {(d.ext || "FILE").toUpperCase()} · {niceSize(d.size)}
                    {d.agent_name ? ` · ${d.agent_name}` : ""}
                    {d.created_at ? ` · ${niceDate(d.created_at)}` : ""}
                  </div>
                </button>
                <div className="flex items-center gap-1 shrink-0 opacity-60 group-hover:opacity-100 transition">
                  <button
                    type="button"
                    onClick={async (ev) => {
                      ev.stopPropagation();
                      try { await downloadFile(d.url || url, d.original_name || "document"); }
                      catch (err) { toast.error(err?.message || "Download failed"); }
                    }}
                    className="p-1.5 rounded hover:bg-white/10"
                    data-testid={`document-download-${d.id}`}
                    title="Download"
                  >
                    <Download className="w-3.5 h-3.5" />
                  </button>
                  <button
                    onClick={() => removeDoc(d.id)}
                    className="p-1.5 rounded hover:bg-white/10 hover:text-red-300"
                    data-testid={`document-delete-${d.id}`}
                    title="Delete"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
        {/* Show more / Show less — only renders when there's something
            hidden. Keeps a quiet org page even after agents stack up
            dozens of generated artifacts. */}
        {docs.length > DOCS_INITIAL_LIMIT && (
          <div className="flex justify-center mt-3">
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="sp-btn sp-btn-ghost text-xs"
              data-testid="documents-toggle-more"
            >
              {expanded
                ? `Show fewer (collapse to ${DOCS_INITIAL_LIMIT})`
                : `Show all ${docs.length} (${docs.length - DOCS_INITIAL_LIMIT} more)`}
            </button>
          </div>
        )}
        </>
      )}

      {/* Inline preview drawer — text-like content rendered as <pre> */}
      {preview && (
        <div
          className="fixed inset-0 z-40 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm"
          onClick={() => setPreview(null)}
          data-testid="document-preview-modal"
        >
          <div
            className="sp-glass-strong rounded-2xl w-full max-w-3xl max-h-[85vh] flex flex-col overflow-hidden"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between gap-3 p-4 border-b border-white/10">
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium truncate">{preview.doc.title || preview.doc.original_name}</div>
                <div className="text-[11px] sp-subtle truncate">
                  {(preview.doc.ext || "FILE").toUpperCase()} · {niceSize(preview.doc.size)}
                  {preview.doc.agent_name ? ` · ${preview.doc.agent_name}` : ""}
                </div>
              </div>
              <button
                type="button"
                onClick={async () => {
                  try { await downloadFile(preview.doc.url, preview.doc.original_name || "document"); }
                  catch (err) { toast.error(err?.message || "Download failed"); }
                }}
                className="sp-btn sp-btn-ghost text-xs"
                data-testid="document-modal-download"
              >
                <Download className="w-3.5 h-3.5" /> Download
              </button>
              <button
                onClick={() => setPreview(null)}
                className="text-white/60 hover:text-white p-1"
                data-testid="document-modal-close"
              >
                <XIcon className="w-4 h-4" />
              </button>
            </div>
            <div className="flex-1 overflow-auto p-4">
              {previewBusy ? (
                <div className="sp-subtle text-sm py-6 text-center">Loading…</div>
              ) : (
                <pre className="text-[12.5px] leading-relaxed whitespace-pre-wrap break-words font-mono opacity-90">
                  {preview.content || "(no preview available)"}
                </pre>
              )}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
