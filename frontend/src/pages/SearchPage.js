import React, { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Search, ArrowLeft, ExternalLink, FileText, File as FileIcon, Image as ImageIcon, FileBox, Link as LinkIcon, AlertCircle, Loader2 } from "lucide-react";
import api from "../lib/api";
import TopBar from "../components/TopBar";
import { toast } from "sonner";

/**
 * Org-wide unified search.
 *
 * Hits `GET /api/orgs/{id}/search?q=…&sources=local,googledrive,…` which
 * fans out in parallel:
 *   • local       — Asset Library (uploaded files + external links)
 *   • googledrive — live via Composio (if user has an ACTIVE connection)
 *   • dropbox     — live via Composio
 *   • notion      — live via Composio
 *   • googledocs  — live via Composio
 *   • onedrive    — live via Composio
 *
 * Each source returns ``{source, ok, results, error?}`` so we can render
 * partial results when one provider is degraded (e.g. token expired).
 */

const KIND_ICONS = {
  image: ImageIcon, pdf: FileText, doc: FileText, text: FileText,
  audio: FileBox, video: FileBox, file: FileIcon, link: LinkIcon,
};

const SOURCE_LABELS = {
  local: "This org's library",
  googledrive: "Google Drive",
  dropbox: "Dropbox",
  notion: "Notion",
  googledocs: "Google Docs",
  onedrive: "OneDrive",
};

// Sources the user can toggle. Local is implicit + always on.
const EXTERNAL_SOURCES = ["googledrive", "dropbox", "notion", "googledocs", "onedrive"];

export default function SearchPage() {
  const { orgId } = useParams();
  const [q, setQ] = useState("");
  const [enabledExt, setEnabledExt] = useState(() => new Set(EXTERNAL_SOURCES));
  const [results, setResults] = useState(null);  // null=idle, [] groups list
  const [busy, setBusy] = useState(false);
  const inputRef = useRef(null);

  useEffect(() => { inputRef.current?.focus(); }, []);

  const sources = useMemo(() => {
    const list = ["local", ...Array.from(enabledExt)];
    return list.join(",");
  }, [enabledExt]);

  // Debounced search — fire ~300ms after typing settles. External
  // providers are slow enough that we don't want to bombard them on
  // every keystroke.
  useEffect(() => {
    const text = q.trim();
    if (!text) { setResults(null); return; }
    const id = setTimeout(async () => {
      setBusy(true);
      try {
        const { data } = await api.get(`/orgs/${orgId}/search`, {
          params: { q: text, sources, limit: 12 },
        });
        setResults(data.groups || []);
      } catch (err) {
        toast.error(err?.response?.data?.detail || "Search failed");
        setResults([]);
      } finally { setBusy(false); }
    }, 300);
    return () => clearTimeout(id);
  }, [q, sources, orgId]);

  const toggleSource = (s) => setEnabledExt((prev) => {
    const next = new Set(prev);
    if (next.has(s)) next.delete(s); else next.add(s);
    return next;
  });

  const totalHits = (results || []).reduce((n, g) => n + (g.ok ? g.results.length : 0), 0);

  return (
    <div className="min-h-screen" style={{ background: "var(--bg)", color: "var(--fg)" }}>
      <TopBar />
      <div className="max-w-4xl mx-auto px-4 sm:px-6 py-8">
        <Link
          to={`/app/org/${orgId}`}
          className="text-xs sp-subtle inline-flex items-center gap-1 hover:text-white mb-4"
          data-testid="search-back-to-org"
        >
          <ArrowLeft className="w-3 h-3" /> Back to org
        </Link>

        <div className="flex items-center gap-3 mb-6">
          <div className="w-11 h-11 rounded-xl flex items-center justify-center"
               style={{ background: "linear-gradient(160deg, rgba(168,85,247,0.45), rgba(168,85,247,0.08))" }}>
            <Search className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-2xl font-semibold leading-tight">Search everywhere</h1>
            <p className="text-xs sp-subtle">Live search across this org&apos;s Asset Library + your connected cloud storage. We never copy your files.</p>
          </div>
        </div>

        <div className="sp-glass-strong rounded-2xl p-5 mb-6">
          <div className="flex items-center gap-2">
            <Search className="w-4 h-4 sp-subtle" />
            <input
              ref={inputRef}
              type="search"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="What are you looking for? (e.g. Q3 plan, brand kit, last week's deck)"
              className="flex-1 rounded-lg px-3 py-2.5 text-sm bg-transparent border-0 focus:outline-none focus:ring-0"
              style={{ color: "var(--fg)" }}
              data-testid="unified-search-input"
            />
            {busy && <Loader2 className="w-4 h-4 animate-spin sp-subtle" />}
          </div>
          {/* Source toggles — local is implicit. Each external chip
              flips a provider on/off; the URL params update next tick. */}
          <div className="flex flex-wrap items-center gap-2 mt-3 text-xs sp-subtle">
            <span>Search in:</span>
            <span className="sp-chip !bg-emerald-400/15 !border-emerald-400/40 !text-white" data-testid="src-chip-local">
              {SOURCE_LABELS.local}
            </span>
            {EXTERNAL_SOURCES.map((s) => {
              const on = enabledExt.has(s);
              return (
                <button
                  key={s}
                  type="button"
                  onClick={() => toggleSource(s)}
                  className={`sp-chip ${on ? "!bg-emerald-400/15 !border-emerald-400/40 !text-white" : "!bg-white/5"} hover:!bg-white/15 hover:text-white`}
                  data-testid={`src-chip-${s}`}
                  title={on ? "Click to exclude this source" : "Click to include this source"}
                >
                  {SOURCE_LABELS[s]} {on ? "ON" : "OFF"}
                </button>
              );
            })}
          </div>
        </div>

        {/* Results */}
        {results === null && (
          <div className="text-center text-sm sp-subtle py-12" data-testid="unified-search-empty">
            Start typing to search.
          </div>
        )}

        {results !== null && totalHits === 0 && !busy && (
          <div className="text-center text-sm sp-subtle py-12" data-testid="unified-search-no-results">
            No matches for &ldquo;{q.trim()}&rdquo;.
            <div className="text-xs mt-1">Try broader terms, or connect more sources above.</div>
          </div>
        )}

        {(results || []).map((g) => (
          <SourceGroup key={g.source} group={g} orgId={orgId} />
        ))}
      </div>
    </div>
  );
}


function SourceGroup({ group, orgId }) {
  const label = SOURCE_LABELS[group.source] || group.source;
  if (!group.ok) {
    // Degraded source — render a soft warning so users know WHY a
    // provider returned nothing (token expired, not connected, etc).
    return (
      <div className="sp-glass rounded-xl p-4 mb-4" data-testid={`group-${group.source}`}>
        <div className="flex items-center gap-2 mb-1 text-sm font-medium">
          <AlertCircle className="w-4 h-4 text-amber-400" /> {label}
        </div>
        <div className="text-xs sp-subtle">
          {group.error === "not_connected" ? (
            <>You haven&apos;t connected {label} yet. <Link to={`/app/org/${orgId}/connections`} className="underline">Connect →</Link></>
          ) : (
            <>Couldn&apos;t reach {label}: {group.error || "unknown error"}.</>
          )}
        </div>
      </div>
    );
  }
  if (!group.results.length) return null;
  return (
    <div className="mb-6" data-testid={`group-${group.source}`}>
      <div className="text-xs uppercase tracking-widest sp-subtle mb-2">
        {label} <span className="opacity-60">· {group.results.length} {group.results.length === 1 ? "match" : "matches"}</span>
      </div>
      <div className="grid gap-2">
        {group.results.map((r, i) => (
          <ResultRow key={`${group.source}-${i}`} row={r} source={group.source} />
        ))}
      </div>
    </div>
  );
}


function ResultRow({ row, source }) {
  const isLocal = source === "local";
  const Icon = isLocal ? (KIND_ICONS[row.kind] || FileIcon) : LinkIcon;
  // Local hits store the canonical url on `.url` (file route or external
  // link). External hits use {title, url, snippet}.
  const title = isLocal ? (row.original_name || "Untitled") : (row.title || "Untitled");
  const href = isLocal ? row.url : row.url;
  const snippet = row.snippet || row.description || "";

  const RowInner = (
    <div className="sp-glass rounded-xl p-3 flex items-start gap-3 hover:border-white/25 transition" data-testid={`result-${source}-${title.slice(0, 24)}`}>
      <div className="w-9 h-9 rounded-lg sp-glass-strong flex items-center justify-center shrink-0">
        <Icon className="w-4 h-4" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium truncate" title={title}>{title}</div>
        {snippet && (
          <div className="text-xs sp-subtle line-clamp-2 mt-0.5">{snippet}</div>
        )}
        {isLocal && row.kind === "link" && row.link_provider && (
          <div className="text-[10px] sp-subtle mt-1">{row.link_provider}</div>
        )}
      </div>
      <ExternalLink className="w-3.5 h-3.5 sp-subtle shrink-0 mt-1" />
    </div>
  );

  if (!href) return RowInner;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className="block">
      {RowInner}
    </a>
  );
}
