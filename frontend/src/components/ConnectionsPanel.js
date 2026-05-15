import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import api from "../lib/api";
import { toast } from "sonner";
import { Check, Loader2, Trash2, ExternalLink, RefreshCw, Plus, Search, X, Pencil } from "lucide-react";

/**
 * ConnectionsPanel — Phase 3
 * --------------------------
 * Lets a workspace member connect their own SaaS accounts (Gmail, Slack,
 * etc.) through Composio's managed OAuth. New since Phase 2:
 *   • Multi-account per toolkit (Work Gmail + Personal Gmail).
 *   • Nickname per account, set at Connect time + editable later.
 *   • "Browse all" modal pulls the full ~250-toolkit Composio catalog.
 */
const TOOLKIT_LOGO_BG = {
  gmail: "from-red-500/20 to-orange-500/20",
  googlecalendar: "from-blue-500/20 to-sky-500/20",
  slack: "from-purple-500/20 to-fuchsia-500/20",
  notion: "from-zinc-300/20 to-zinc-500/20",
  hubspot: "from-orange-500/20 to-amber-500/20",
  linear: "from-violet-500/20 to-indigo-500/20",
  jira: "from-blue-600/20 to-indigo-500/20",
  asana: "from-pink-500/20 to-rose-500/20",
  googledrive: "from-yellow-500/20 to-emerald-500/20",
  dropbox: "from-sky-500/20 to-blue-500/20",
  discord: "from-indigo-500/20 to-violet-500/20",
  salesforce: "from-sky-500/20 to-blue-600/20",
  mailchimp: "from-amber-500/20 to-yellow-400/20",
  stripe: "from-violet-500/20 to-purple-500/20",
  calendly: "from-blue-500/20 to-cyan-500/20",
  zoom: "from-blue-500/20 to-sky-500/20",
  twitter: "from-zinc-200/20 to-zinc-400/20",
  linkedin: "from-blue-700/20 to-sky-500/20",
  airtable: "from-amber-500/20 to-rose-500/20",
  gitlab: "from-orange-600/20 to-red-500/20",
};

const initialFor = (slug) => (slug || "?").charAt(0).toUpperCase();

export default function ConnectionsPanel({ orgId }) {
  const [toolkits, setToolkits] = useState([]);
  const [connections, setConnections] = useState([]);
  const [available, setAvailable] = useState(true);
  const [loading, setLoading] = useState(true);
  const [busySlug, setBusySlug] = useState(null);
  const [nicknameDraft, setNicknameDraft] = useState({ slug: null, value: "" });
  const [renameDraft, setRenameDraft] = useState({ id: null, value: "" });
  const [browseOpen, setBrowseOpen] = useState(false);
  const location = useLocation();
  const nav = useNavigate();

  const load = useCallback(async () => {
    try {
      const { data } = await api.get(`/orgs/${orgId}/composio/toolkits`);
      setToolkits(data.toolkits || []);
      setConnections(data.connections || []);
      setAvailable(Boolean(data.available));
    } catch (err) {
      toast.error("Couldn't load integrations");
    } finally {
      setLoading(false);
    }
  }, [orgId]);

  useEffect(() => { load(); }, [load]);

  // Detect return-from-OAuth and poll for ACTIVE status. See Phase 1 doc.
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    if (params.get("composio_callback") !== "1") return undefined;
    let cancelled = false;
    let attempts = 0;
    const poll = async () => {
      attempts += 1;
      try {
        const { data } = await api.get(`/orgs/${orgId}/composio/toolkits`);
        if (cancelled) return;
        setToolkits(data.toolkits || []);
        setConnections(data.connections || []);
        const stillPending = (data.connections || []).some(
          (c) => c.status === "INITIATED",
        );
        for (const c of data.connections || []) {
          if (c.status === "INITIATED") {
            try {
              await api.post(`/orgs/${orgId}/composio/connections/${c.connection_id}/refresh`);
            } catch (_) {/* keep polling */}
          }
        }
        if (!stillPending) {
          toast.success("Account connected.");
          cancelled = true;
          nav(`/app/org/${orgId}#connections`, { replace: true });
          load();
          return;
        }
      } catch (_) {/* keep polling */}
      if (attempts >= 10) {
        cancelled = true;
        nav(`/app/org/${orgId}#connections`, { replace: true });
      } else {
        setTimeout(poll, 3000);
      }
    };
    setTimeout(poll, 1500);
    return () => { cancelled = true; };
  }, [location.search, orgId, load, nav]);

  // Group connections by toolkit so the UI renders one card per toolkit
  // with N accounts listed underneath.
  const connectionsByToolkit = useMemo(() => {
    const m = {};
    for (const c of connections) {
      if (!m[c.toolkit]) m[c.toolkit] = [];
      m[c.toolkit].push(c);
    }
    // Sort each toolkit's connections: ACTIVE first, then by initiated_at desc.
    for (const slug of Object.keys(m)) {
      m[slug].sort((a, b) => {
        const aa = a.status === "ACTIVE" ? 0 : 1;
        const bb = b.status === "ACTIVE" ? 0 : 1;
        if (aa !== bb) return aa - bb;
        return (b.initiated_at || "").localeCompare(a.initiated_at || "");
      });
    }
    return m;
  }, [connections]);

  // Compose the union of featured toolkits + any non-featured toolkits
  // the user has already connected (so they still see them on the page).
  const visibleToolkits = useMemo(() => {
    const featuredSlugs = new Set(toolkits.map((t) => t.slug));
    const extras = [];
    for (const c of connections) {
      if (!featuredSlugs.has(c.toolkit)) {
        extras.push({
          slug: c.toolkit,
          name: c.toolkit_name || c.toolkit,
          description: c.toolkit_description || "",
        });
        featuredSlugs.add(c.toolkit);
      }
    }
    return [...toolkits, ...extras];
  }, [toolkits, connections]);

  const startConnect = (slug) => {
    setNicknameDraft({ slug, value: "" });
  };

  const handleConnectConfirm = async () => {
    const slug = nicknameDraft.slug;
    if (!slug) return;
    setBusySlug(slug);
    try {
      const { data } = await api.post(`/orgs/${orgId}/composio/connect`, {
        toolkit: slug,
        nickname: nicknameDraft.value.trim() || null,
      });
      setNicknameDraft({ slug: null, value: "" });
      window.open(data.redirect_url, "_blank", "noopener,noreferrer");
      toast.message("Finish authorisation in the new tab — we'll auto-detect when done.");
      load();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't start authorisation");
    } finally {
      setBusySlug(null);
    }
  };

  const handleDisconnect = async (connectionId) => {
    if (!window.confirm("Disconnect this account? Agents will lose access immediately.")) return;
    try {
      await api.delete(`/orgs/${orgId}/composio/connections/${connectionId}`);
      toast.success("Disconnected.");
      load();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't disconnect");
    }
  };

  const handleRefresh = async (connectionId) => {
    try {
      await api.post(`/orgs/${orgId}/composio/connections/${connectionId}/refresh`);
      load();
    } catch (err) {
      toast.error("Refresh failed");
    }
  };

  const handleSaveRename = async () => {
    if (!renameDraft.id) return;
    try {
      await api.put(`/orgs/${orgId}/composio/connections/${renameDraft.id}/nickname`, {
        nickname: renameDraft.value.trim(),
      });
      setRenameDraft({ id: null, value: "" });
      load();
    } catch (err) {
      toast.error("Rename failed");
    }
  };

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm opacity-70" data-testid="connections-loading">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading integrations…
      </div>
    );
  }

  if (!available) {
    return (
      <div className="rounded-xl border border-amber-500/40 bg-amber-500/5 p-4 text-sm" data-testid="connections-unavailable">
        Composio integration isn't configured on this deployment yet. Ask your
        admin to set <code className="px-1 rounded bg-black/20">COMPOSIO_API_KEY</code> in the backend.
      </div>
    );
  }

  return (
    <div className="space-y-4" data-testid="connections-panel">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <p className="text-sm opacity-70 max-w-xl">
          Connect your own SaaS accounts so agents can read & write on your behalf.
          Each connection is scoped to <span className="opacity-100 font-medium">your user</span>, not shared.
          Connect <span className="opacity-100">multiple accounts per tool</span> with nicknames like "Work" / "Personal".
        </p>
        <div className="flex gap-2">
          <button
            onClick={() => setBrowseOpen(true)}
            className="text-xs px-3 py-1.5 rounded-md border border-white/15 hover:border-white/30 hover:bg-white/5 inline-flex items-center gap-1"
            data-testid="browse-all-btn"
          >
            <Search className="h-3 w-3" /> Browse 250+ tools
          </button>
          <button
            onClick={load}
            className="text-xs opacity-60 hover:opacity-100 inline-flex items-center gap-1"
            data-testid="connections-refresh-btn"
          >
            <RefreshCw className="h-3 w-3" /> Refresh
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {visibleToolkits.map((t) => {
          const accounts = connectionsByToolkit[t.slug] || [];
          const bg = TOOLKIT_LOGO_BG[t.slug] || "from-zinc-500/20 to-zinc-700/20";
          const hasActive = accounts.some((c) => c.status === "ACTIVE");
          return (
            <div
              key={t.slug}
              className="rounded-xl border border-white/10 bg-black/20 p-4 hover:border-white/20 transition"
              data-testid={`connection-card-${t.slug}`}
            >
              <div className="flex items-center gap-3 mb-2">
                <div className={`h-10 w-10 rounded-lg bg-gradient-to-br ${bg} flex items-center justify-center text-base font-semibold tracking-tight border border-white/10`}>
                  {initialFor(t.slug)}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="font-medium text-sm">{t.name}</div>
                  <p className="text-[11px] opacity-60 truncate">{t.description}</p>
                </div>
                <button
                  onClick={() => startConnect(t.slug)}
                  disabled={busySlug === t.slug}
                  className="text-xs px-2.5 py-1.5 rounded-md bg-orange-500/90 hover:bg-orange-500 text-white inline-flex items-center gap-1 disabled:opacity-50"
                  data-testid={`connect-${t.slug}-btn`}
                  title={accounts.length > 0 ? "Add another account" : "Connect"}
                >
                  {busySlug === t.slug ? <Loader2 className="h-3 w-3 animate-spin" /> : <Plus className="h-3 w-3" />}
                  {accounts.length > 0 ? "Add" : "Connect"}
                </button>
              </div>
              {accounts.length > 0 && (
                <ul className="mt-2 space-y-1.5">
                  {accounts.map((c) => {
                    const isRenaming = renameDraft.id === c.connection_id;
                    return (
                      <li key={c.connection_id} className="flex items-center gap-2 text-xs pl-1" data-testid={`account-row-${c.connection_id}`}>
                        {c.status === "ACTIVE" ? (
                          <Check className="h-3 w-3 text-emerald-400 flex-shrink-0" />
                        ) : (
                          <Loader2 className="h-3 w-3 animate-spin text-amber-400 flex-shrink-0" />
                        )}
                        {isRenaming ? (
                          <input
                            type="text"
                            value={renameDraft.value}
                            onChange={(e) => setRenameDraft((p) => ({ ...p, value: e.target.value }))}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") handleSaveRename();
                              if (e.key === "Escape") setRenameDraft({ id: null, value: "" });
                            }}
                            onBlur={handleSaveRename}
                            autoFocus
                            className="flex-1 px-2 py-0.5 bg-black/30 border border-white/20 rounded text-xs outline-none"
                            data-testid={`rename-input-${c.connection_id}`}
                          />
                        ) : (
                          <span className="flex-1 truncate">
                            {c.nickname || c.account_label || (c.status === "INITIATED" ? "Connecting…" : "Connected")}
                          </span>
                        )}
                        {!isRenaming && c.status === "ACTIVE" && (
                          <button
                            onClick={() => setRenameDraft({ id: c.connection_id, value: c.nickname || "" })}
                            className="opacity-40 hover:opacity-100 transition"
                            title="Rename"
                            data-testid={`rename-${c.connection_id}-btn`}
                          >
                            <Pencil className="h-3 w-3" />
                          </button>
                        )}
                        {c.status === "INITIATED" && (
                          <button
                            onClick={() => handleRefresh(c.connection_id)}
                            className="opacity-60 hover:opacity-100 text-[10px]"
                            data-testid={`check-${c.connection_id}-btn`}
                          >
                            Check
                          </button>
                        )}
                        <button
                          onClick={() => handleDisconnect(c.connection_id)}
                          className="opacity-40 hover:opacity-100 hover:text-red-400 transition"
                          title="Disconnect"
                          data-testid={`disconnect-${c.connection_id}-btn`}
                        >
                          <Trash2 className="h-3 w-3" />
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
              {!hasActive && accounts.length === 0 && (
                <p className="text-[11px] opacity-40 pl-1">No account connected yet.</p>
              )}
            </div>
          );
        })}
      </div>

      <p className="text-[11px] opacity-50">
        Authentication is handled by Composio's hosted OAuth — Foundry never sees your password.
        Disconnecting revokes the token immediately.
      </p>

      {/* Nickname prompt */}
      {nicknameDraft.slug && (
        <NicknameModal
          slug={nicknameDraft.slug}
          value={nicknameDraft.value}
          onChange={(v) => setNicknameDraft((p) => ({ ...p, value: v }))}
          onCancel={() => setNicknameDraft({ slug: null, value: "" })}
          onConfirm={handleConnectConfirm}
          existingCount={(connectionsByToolkit[nicknameDraft.slug] || []).length}
        />
      )}

      {browseOpen && (
        <BrowseAllModal
          orgId={orgId}
          existingSlugs={new Set(connections.map((c) => c.toolkit))}
          onClose={() => { setBrowseOpen(false); load(); }}
          onConnect={(slug) => { setBrowseOpen(false); startConnect(slug); }}
        />
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
function NicknameModal({ slug, value, onChange, onCancel, onConfirm, existingCount }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm" data-testid="nickname-modal">
      <div className="bg-zinc-900 border border-white/15 rounded-2xl p-6 w-full max-w-md mx-4 space-y-3">
        <h3 className="font-medium">
          {existingCount > 0 ? `Add another ${slug}` : `Connect ${slug}`}
        </h3>
        <p className="text-xs opacity-60">
          Give this account a nickname so you can tell it apart from others
          (e.g. "Work", "Personal", "Client X"). Optional.
        </p>
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          autoFocus
          maxLength={60}
          placeholder={existingCount > 0 ? "e.g. Personal" : "e.g. Work"}
          className="w-full px-3 py-2 bg-black/30 border border-white/15 rounded-md text-sm outline-none focus:border-white/40"
          data-testid="nickname-input"
          onKeyDown={(e) => { if (e.key === "Enter") onConfirm(); if (e.key === "Escape") onCancel(); }}
        />
        <div className="flex justify-end gap-2 pt-1">
          <button onClick={onCancel} className="text-xs px-3 py-1.5 rounded-md hover:bg-white/5" data-testid="nickname-cancel">Cancel</button>
          <button onClick={onConfirm} className="text-xs px-3 py-1.5 rounded-md bg-orange-500/90 hover:bg-orange-500 text-white inline-flex items-center gap-1" data-testid="nickname-confirm">
            <ExternalLink className="h-3 w-3" /> Open authorisation
          </button>
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
function BrowseAllModal({ orgId, existingSlugs, onClose, onConnect }) {
  const [q, setQ] = useState("");
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const t = setTimeout(async () => {
      setLoading(true);
      try {
        const { data } = await api.get(`/orgs/${orgId}/composio/catalog`, {
          params: q ? { q } : undefined,
        });
        if (cancelled) return;
        setItems(data.toolkits || []);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }, q ? 200 : 0);
    return () => { cancelled = true; clearTimeout(t); };
  }, [q, orgId]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm" data-testid="browse-all-modal">
      <div className="bg-zinc-900 border border-white/15 rounded-2xl w-full max-w-2xl mx-4 max-h-[80vh] flex flex-col">
        <div className="p-5 border-b border-white/10 flex items-center gap-3">
          <h3 className="font-medium flex-1">Browse all integrations</h3>
          <button onClick={onClose} className="opacity-50 hover:opacity-100" data-testid="browse-close">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="p-4 border-b border-white/10">
          <div className="relative">
            <Search className="absolute left-3 top-2.5 h-3.5 w-3.5 opacity-50" />
            <input
              type="text"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              autoFocus
              placeholder="Search 250+ tools (try 'asana', 'twilio', 'figma')…"
              className="w-full pl-9 pr-3 py-2 bg-black/30 border border-white/15 rounded-md text-sm outline-none focus:border-white/40"
              data-testid="browse-search"
            />
          </div>
        </div>
        <div className="flex-1 overflow-y-auto p-2">
          {loading ? (
            <div className="flex items-center justify-center py-12 opacity-60">
              <Loader2 className="h-5 w-5 animate-spin" />
            </div>
          ) : items.length === 0 ? (
            <p className="text-sm opacity-50 text-center py-12">No tools match "{q}".</p>
          ) : (
            <ul className="divide-y divide-white/5">
              {items.map((t) => {
                const connected = existingSlugs.has(t.slug);
                return (
                  <li key={t.slug} className="flex items-center gap-3 px-3 py-2.5 hover:bg-white/5 rounded-md" data-testid={`catalog-row-${t.slug}`}>
                    {t.logo ? (
                      <img src={t.logo} alt="" className="h-7 w-7 rounded-md bg-white/10 object-contain p-1" />
                    ) : (
                      <div className="h-7 w-7 rounded-md bg-white/10 flex items-center justify-center text-xs">
                        {initialFor(t.slug)}
                      </div>
                    )}
                    <div className="flex-1 min-w-0">
                      <div className="text-sm flex items-center gap-2">
                        {t.name}
                        {t.featured && (
                          <span className="text-[9px] uppercase tracking-wider px-1 rounded bg-orange-500/20 text-orange-300">Featured</span>
                        )}
                      </div>
                      {t.description && (
                        <p className="text-[11px] opacity-50 truncate">{t.description}</p>
                      )}
                    </div>
                    <button
                      onClick={() => onConnect(t.slug)}
                      className="text-xs px-2.5 py-1 rounded-md bg-orange-500/90 hover:bg-orange-500 text-white"
                      data-testid={`catalog-connect-${t.slug}`}
                    >
                      {connected ? "Add account" : "Connect"}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
