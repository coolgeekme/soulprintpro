import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import api from "../lib/api";
import { toast } from "sonner";
import { Check, Loader2, Trash2, ExternalLink, RefreshCw, Plus, Search, X, Pencil, Settings2, AlertTriangle, Instagram as InstagramIcon, Facebook as FacebookIcon } from "lucide-react";

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


// "Connecting…" with the elapsed-time tail when we know how long the
// row has been pending. Sourced from the `pending_age_seconds` stamp
// that the backend's `list_connections` puts on every INITIATED row.
// Falls back to plain "Connecting…" when the age isn't available
// (legacy rows or fresh-mint pre-stamp window).
function formatPendingLabel(c) {
  const age = Number(c?.pending_age_seconds || 0);
  if (!age || age < 30) return "Connecting…";
  if (age < 60) return `Connecting… (${age}s)`;
  if (age < 3600) return `Connecting… (${Math.round(age / 60)} min)`;
  return `Connecting… (${Math.round(age / 3600)}h)`;
}

export default function ConnectionsPanel({ orgId }) {
  const [toolkits, setToolkits] = useState([]);
  const [connections, setConnections] = useState([]);
  const [available, setAvailable] = useState(true);
  const [loading, setLoading] = useState(true);
  const [busySlug, setBusySlug] = useState(null);
  const [nicknameDraft, setNicknameDraft] = useState({ slug: null, value: "" });
  const [renameDraft, setRenameDraft] = useState({ id: null, value: "" });
  const [browseOpen, setBrowseOpen] = useState(false);
  // Non-fatal "we couldn't pull your connected accounts but the toolkit
  // catalog still renders" banner — shown inline so users can keep
  // adding new connections even when an existing row is causing the
  // list to crash on the backend.
  const [connectionsError, setConnectionsError] = useState(null);
  const location = useLocation();
  const nav = useNavigate();

  const load = useCallback(async () => {
    try {
      const { data } = await api.get(`/orgs/${orgId}/composio/toolkits`);
      setToolkits(data.toolkits || []);
      setConnections(data.connections || []);
      setAvailable(Boolean(data.available));
      setConnectionsError(data.connections_error || null);
    } catch (err) {
      // Hard failure (the whole endpoint 500'd). Leave any previously
      // loaded toolkits in place so the user doesn't lose the "+ Add"
      // grid mid-session, and surface a single toast + inline banner.
      toast.error("Couldn't load integrations");
      setConnectionsError(
        "Couldn't reach the integrations service. Try Refresh in a moment.",
      );
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
    let autoEnabledTotal = 0;
    // 20 attempts × 3s = 60s post-callback poll window. We used to give
    // up at 30s (10 × 3s), but Composio's INITIATED → ACTIVE webhook
    // can lag 30-45s when Meta is slow to dispatch the OAuth grant
    // event — which left users staring at a "Connecting…" spinner that
    // would never resolve unless they happened to reload the page.
    const MAX_ATTEMPTS = 20;
    const poll = async () => {
      attempts += 1;
      try {
        const { data } = await api.get(`/orgs/${orgId}/composio/toolkits`);
        if (cancelled) return;
        setToolkits(data.toolkits || []);
        setConnections(data.connections || []);
        const stillPending = (data.connections || []).some(
          (c) => c.status === "INITIATED",  // STUCK is terminal — don't keep polling it
        );
        for (const c of data.connections || []) {
          if (c.status === "INITIATED") {
            try {
              const { data: refreshed } = await api.post(`/orgs/${orgId}/composio/connections/${c.connection_id}/refresh`);
              if (refreshed?.auto_enabled_agents) {
                autoEnabledTotal += Number(refreshed.auto_enabled_agents) || 0;
              }
            } catch (_) {/* keep polling */}
          }
        }
        if (!stillPending) {
          if (autoEnabledTotal > 0) {
            toast.success(
              `Account connected. Enabled tools for ${autoEnabledTotal} matching agent${autoEnabledTotal === 1 ? "" : "s"}.`,
            );
          } else {
            toast.success("Account connected.");
          }
          cancelled = true;
          nav(`/app/org/${orgId}#connections`, { replace: true });
          load();
          return;
        }
      } catch (_) {/* keep polling */}
      if (attempts >= MAX_ATTEMPTS) {
        cancelled = true;
        nav(`/app/org/${orgId}#connections`, { replace: true });
        // Tell the user the auto-verify gave up — without this they'd
        // sit on a silent "Connecting…" spinner and have no idea what
        // to do next. Two next-step actions are surfaced on the row
        // itself (Check / Cancel) so the toast just points there.
        toast.warning(
          "Couldn't auto-verify the connection. Click 'Check' on the pending row to retry, or 'Cancel' to start over.",
          { duration: 8000 },
        );
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

  // Distinct from handleDisconnect because the user-mental-model is
  // different: a pending connection isn't "disconnecting an account",
  // it's "cancel the in-flight OAuth attempt and start over". Worded
  // accordingly so the confirm modal doesn't scare users into thinking
  // they'll lose an already-active connection.
  const handleCancelPending = async (connectionId) => {
    if (!window.confirm("Cancel this pending connection and start over? Your existing connected accounts won't be affected.")) return;
    try {
      await api.delete(`/orgs/${orgId}/composio/connections/${connectionId}`);
      toast.success("Pending connection cancelled. Click + Add to retry.");
      load();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't cancel the pending connection");
    }
  };

  // One-click recovery for STUCK rows: delete the failed attempt then
  // immediately fire a fresh OAuth flow for the same toolkit, preserving
  // the user's original nickname so the new row picks up where the old
  // one left off. This is the production-grade retry that makes "stuck"
  // a self-healing state instead of a manual-cleanup dead end.
  const handleRetryStuck = async (conn, slug) => {
    setBusySlug(slug);
    try {
      // 1. Clear the failed row first.
      try {
        await api.delete(`/orgs/${orgId}/composio/connections/${conn.connection_id}`);
      } catch (_) {/* keep going; the connect endpoint also prunes */}
      // 2. Fresh OAuth — same toolkit + preserved nickname.
      const { data } = await api.post(`/orgs/${orgId}/composio/connect`, {
        toolkit: slug,
        nickname: (conn.nickname || "").trim() || null,
      });
      window.open(data.redirect_url, "_blank", "noopener,noreferrer");
      toast.message("Retry started — finish authorisation in the new tab.");
      load();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't retry the connection");
    } finally {
      setBusySlug(null);
    }
  };

  const handleRefresh = async (connectionId) => {
    try {
      const { data } = await api.post(`/orgs/${orgId}/composio/connections/${connectionId}/refresh`);
      // Tell the user what changed. Composio sometimes lags 30-60s
      // after the OAuth grant, so "still pending" is a real outcome
      // worth surfacing instead of silently leaving the spinner on.
      const status = data?.status;
      if (status === "ACTIVE") {
        toast.success("Connection verified — you're good to go.");
      } else if (status === "INITIATED") {
        toast.message("Still pending on Composio's side. Wait 30s and try again, or click Cancel to reset.");
      } else if (status) {
        toast.error(`Connection state: ${status}. Click Cancel and reconnect.`);
      }
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
          Connect your own SaaS accounts so agents can read &amp; write on your behalf.
          Every agent in this org can use your connected tools by default — connect
          Gmail and any agent can send mail, connect HubSpot and any agent can update
          deals. Each connection is scoped to{" "}
          <span className="opacity-100 font-medium">your user</span>.
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

      <ConnectionsHowToGuide />

      {connectionsError && (
        <div
          className="flex items-start gap-2 rounded-lg border border-amber-400/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200"
          data-testid="connections-soft-error"
        >
          <AlertTriangle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
          <span className="leading-relaxed">
            {connectionsError}
            {" "}
            <button
              onClick={load}
              className="underline underline-offset-2 hover:text-amber-100"
              data-testid="connections-soft-error-retry"
            >
              Retry now
            </button>
          </span>
        </div>
      )}

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
                    <React.Fragment key={c.connection_id}>
                      <li className="flex items-center gap-2 text-xs pl-1" data-testid={`account-row-${c.connection_id}`}>
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
                          <span className="flex-1 truncate flex flex-col leading-tight">
                            <span className={"truncate " + (c.status === "STUCK" ? "text-red-300" : "")}>
                              {c.nickname || c.display_label || c.account_label || (
                                c.status === "STUCK"
                                  ? "Connection failed"
                                  : c.status === "INITIATED"
                                    ? formatPendingLabel(c)
                                    : "Connected"
                              )}
                            </span>
                            {c.display_label && c.nickname && c.nickname !== c.display_label && (
                              <span className="text-[10px] opacity-50 truncate">
                                {c.display_label}
                              </span>
                            )}
                            {c.status === "INITIATED" && (c.pending_age_seconds || 0) >= 90 && (
                              <span className="text-[10px] text-amber-300 truncate">
                                Taking longer than usual — click Cancel and reconnect if the OAuth window already closed.
                              </span>
                            )}
                            {c.status === "STUCK" && (
                              <span className="text-[10px] text-red-300 truncate">
                                The OAuth grant didn't complete. Click Retry to start over — your other connections are unaffected.
                              </span>
                            )}
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
                          <>
                            <button
                              onClick={() => handleRefresh(c.connection_id)}
                              className="opacity-60 hover:opacity-100 text-[10px]"
                              title="Re-check this connection's status with Composio"
                              data-testid={`check-${c.connection_id}-btn`}
                            >
                              Check
                            </button>
                            <button
                              onClick={() => handleCancelPending(c.connection_id)}
                              className="text-[10px] px-1.5 py-0.5 rounded border border-amber-400/40 text-amber-300 hover:bg-amber-500/10"
                              title="Cancel this pending connection and start over"
                              data-testid={`cancel-pending-${c.connection_id}-btn`}
                            >
                              Cancel
                            </button>
                          </>
                        )}
                        {c.status === "STUCK" && (
                          <button
                            onClick={() => handleRetryStuck(c, t.slug)}
                            className="text-[10px] px-1.5 py-0.5 rounded border border-red-400/40 text-red-300 hover:bg-red-500/10"
                            title="Wipe this failed attempt and start a fresh OAuth"
                            data-testid={`retry-stuck-${c.connection_id}-btn`}
                          >
                            Retry
                          </button>
                        )}
                        {c.status === "ACTIVE" && (
                          <button
                            onClick={() => handleDisconnect(c.connection_id)}
                            className="opacity-40 hover:opacity-100 hover:text-red-400 transition"
                            title="Disconnect"
                            data-testid={`disconnect-${c.connection_id}-btn`}
                          >
                            <Trash2 className="h-3 w-3" />
                          </button>
                        )}
                      </li>
                      {/* Meta-target picker — surfaced inline under each
                          FB/IG row so the user can see (and change) which
                          Page + IG account this org publishes to. */}
                      {(t.slug === "facebook" || t.slug === "instagram") && c.status === "ACTIVE" && (
                        <MetaTargetRow
                          connection={c}
                          orgId={orgId}
                          onUpdated={load}
                        />
                      )}
                    </React.Fragment>
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
  // Platform-specific gotchas users should know BEFORE they start OAuth.
  // Surfaced inside the nickname modal so it's seen at the moment of
  // intent, not buried in some help doc.
  const PLATFORM_NOTES = {
    instagram: "Composio uses Meta's Graph API, which requires your Instagram to be a Business or Creator account linked to a Facebook Page. Personal accounts won't work — Meta blocks them.",
    facebook:  "You'll be asked to grant permissions for a specific Facebook Page. Choose the page you want agents to post on behalf of.",
    tiktok:    "TikTok requires a developer-approved app — Composio doesn't have managed credentials, so this connection may not work without your own TikTok dev account.",
    twitter:   "Twitter / X requires your own developer app credentials — Composio doesn't have managed credentials for this platform.",
  };
  const note = PLATFORM_NOTES[slug];
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm" data-testid="nickname-modal">
      <div className="sp-glass-strong rounded-2xl p-6 w-full max-w-md mx-4 space-y-3" style={{ color: "var(--fg)" }}>
        <h3 className="font-medium">
          {existingCount > 0 ? `Add another ${slug}` : `Connect ${slug}`}
        </h3>
        {note && (
          <div className="text-xs rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 flex gap-2" data-testid={`platform-note-${slug}`}>
            <span className="text-amber-400 flex-shrink-0">⚠</span>
            <span>{note}</span>
          </div>
        )}
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
          className="w-full px-3 py-2 rounded-md text-sm outline-none focus:border-orange-500/60"
          style={{
            background: "var(--surface, rgba(127,127,127,0.10))",
            border: "1px solid var(--border-soft, rgba(127,127,127,0.25))",
            color: "var(--fg)",
          }}
          data-testid="nickname-input"
          onKeyDown={(e) => { if (e.key === "Enter") onConfirm(); if (e.key === "Escape") onCancel(); }}
        />
        <div className="flex justify-end gap-2 pt-1">
          <button onClick={onCancel} className="text-xs px-3 py-1.5 rounded-md hover:bg-black/5 dark:hover:bg-white/5" data-testid="nickname-cancel">Cancel</button>
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
      <div className="sp-glass-strong rounded-2xl w-full max-w-2xl mx-4 max-h-[80vh] flex flex-col" style={{ color: "var(--fg)" }}>
        <div className="p-5 border-b flex items-center gap-3" style={{ borderColor: "var(--border-soft, rgba(127,127,127,0.18))" }}>
          <h3 className="font-medium flex-1">Browse all integrations</h3>
          <button onClick={onClose} className="opacity-50 hover:opacity-100" data-testid="browse-close">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="p-4 border-b" style={{ borderColor: "var(--border-soft, rgba(127,127,127,0.18))" }}>
          <div className="relative">
            <Search className="absolute left-3 top-2.5 h-3.5 w-3.5 opacity-50" />
            <input
              type="text"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              autoFocus
              placeholder="Search 250+ tools (try 'asana', 'twilio', 'figma')…"
              className="w-full pl-9 pr-3 py-2 rounded-md text-sm outline-none focus:border-orange-500/60"
              style={{
                background: "var(--surface, rgba(127,127,127,0.10))",
                border: "1px solid var(--border-soft, rgba(127,127,127,0.25))",
                color: "var(--fg)",
              }}
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
            <ul className="divide-y" style={{ borderColor: "var(--border-soft, rgba(127,127,127,0.12))" }}>
              {items.map((t) => {
                const connected = existingSlugs.has(t.slug);
                const byoa = Boolean(t.byoa_only);
                return (
                  <li key={t.slug} className="flex items-center gap-3 px-3 py-2.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-md" data-testid={`catalog-row-${t.slug}`}>
                    {t.logo ? (
                      <img src={t.logo} alt="" className={`h-7 w-7 rounded-md object-contain p-1 ${byoa ? "opacity-50" : ""}`} style={{ background: "var(--surface, rgba(127,127,127,0.10))" }} />
                    ) : (
                      <div className={`h-7 w-7 rounded-md flex items-center justify-center text-xs ${byoa ? "opacity-50" : ""}`} style={{ background: "var(--surface, rgba(127,127,127,0.10))" }}>
                        {initialFor(t.slug)}
                      </div>
                    )}
                    <div className="flex-1 min-w-0">
                      <div className="text-sm flex items-center gap-2 flex-wrap">
                        <span className={byoa ? "opacity-60" : ""}>{t.name}</span>
                        {t.featured && (
                          <span className="text-[9px] uppercase tracking-wider px-1 rounded bg-orange-500/20 text-orange-500 dark:text-orange-300">Featured</span>
                        )}
                        {byoa && (
                          <span
                            className="text-[9px] uppercase tracking-wider px-1 rounded border border-amber-500/40 text-amber-600 dark:text-amber-300 bg-amber-500/10"
                            title={t.byoa_reason || "Requires you to bring your own OAuth credentials"}
                          >
                            Bring your own auth
                          </span>
                        )}
                      </div>
                      {byoa ? (
                        <p className="text-[11px] opacity-60 truncate">
                          One-click Connect not supported. {t.name} needs its own OAuth app created on their developer dashboard. Tell us if you need this and we&apos;ll prioritise the BYOA setup flow.
                        </p>
                      ) : t.description ? (
                        <p className="text-[11px] opacity-50 truncate">{t.description}</p>
                      ) : null}
                    </div>
                    <button
                      onClick={() => !byoa && onConnect(t.slug)}
                      disabled={byoa}
                      title={byoa ? "Requires bring-your-own OAuth credentials" : ""}
                      className={`text-xs px-2.5 py-1 rounded-md text-white ${byoa ? "bg-white/15 cursor-not-allowed" : "bg-orange-500/90 hover:bg-orange-500"}`}
                      data-testid={`catalog-connect-${t.slug}`}
                    >
                      {byoa ? "Not available" : (connected ? "Add account" : "Connect")}
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


// ── How-to guide ─────────────────────────────────────────────────────────
// Collapsible "How to use your connections" card. Opens by default the
// first time the user lands on the Connections page (per-browser pref).
// Solves the #1 user complaint: "I connected HubSpot and IG but the
// agents didn't seem to know about them."
function ConnectionsHowToGuide() {
  const STORAGE_KEY = "connections.howto.collapsed";
  const [open, setOpen] = useState(() => {
    try { return localStorage.getItem(STORAGE_KEY) !== "1"; } catch { return true; }
  });
  const toggle = () => {
    setOpen((v) => {
      const next = !v;
      try { localStorage.setItem(STORAGE_KEY, next ? "0" : "1"); } catch {}
      return next;
    });
  };
  return (
    <div
      className="rounded-xl border border-orange-500/30 bg-orange-500/5"
      data-testid="connections-howto"
    >
      <button
        type="button"
        onClick={toggle}
        className="w-full flex items-center justify-between px-4 py-3 text-left"
        data-testid="connections-howto-toggle"
      >
        <div className="flex items-center gap-2">
          <span className="text-orange-300 text-sm">💡</span>
          <span className="text-sm font-medium">How to actually use your connections</span>
        </div>
        <span className="text-xs opacity-60">{open ? "Hide" : "Show"}</span>
      </button>
      {open && (
        <div className="px-4 pb-4 space-y-3 text-sm opacity-90">
          <div>
            <div className="font-medium mb-1">1. Connect the account</div>
            <p className="opacity-80">
              Click <span className="font-mono text-xs px-1 py-0.5 rounded bg-white/10">Connect</span>{" "}
              on a tile below. A new tab opens — sign in to that service
              (Google, HubSpot, Slack, etc.) and approve the permissions.
              When you come back, the tile turns green with a checkmark.
              That&apos;s it — every agent in this org can now use it.
            </p>
          </div>
          <div>
            <div className="font-medium mb-1">2. Just ask in chat</div>
            <p className="opacity-80">
              Go to any chat (General, your Chief of Staff, a Mission)
              and ask the agent to do the thing. Examples:
            </p>
            <ul className="list-disc pl-5 mt-1 space-y-1 text-[13px] opacity-80">
              <li><span className="opacity-100">HubSpot:</span> &quot;Pull my top 10 deals by stage and summarise them.&quot;</li>
              <li><span className="opacity-100">Gmail:</span> &quot;Send Reggie an email confirming the meeting tomorrow at 3pm.&quot;</li>
              <li><span className="opacity-100">Instagram:</span> &quot;Post the new launch image to my IG with the caption I drafted.&quot;</li>
              <li><span className="opacity-100">Slack:</span> &quot;Drop the weekly digest into #general.&quot;</li>
            </ul>
            <p className="opacity-80 mt-1">
              The agent will say which account it&apos;s using (e.g.{" "}
              <em>&quot;sending from your Work Gmail&quot;</em>) and either confirm
              or ask before destructive actions.
            </p>
          </div>
          <div>
            <div className="font-medium mb-1">3. If an agent says it can&apos;t</div>
            <p className="opacity-80">
              First check that the tile here shows{" "}
              <span className="text-emerald-300">Connected</span>. If it
              does, re-state the request more concretely (&quot;use HubSpot to
              find…&quot;). If the agent still refuses, an org owner can
              re-check the per-agent toggle in{" "}
              <span className="font-mono text-xs px-1 py-0.5 rounded bg-white/10">Agent tool prefs</span>{" "}
              — every agent is ON by default but can be individually
              turned OFF.
            </p>
          </div>
          <div>
            <div className="font-medium mb-1">4. Multiple accounts? No problem</div>
            <p className="opacity-80">
              Click <span className="font-mono text-xs px-1 py-0.5 rounded bg-white/10">Add account</span>{" "}
              on any connected tile to add a second (e.g. Personal Gmail
              + Work Gmail). Give each a nickname so agents can pick the
              right one — or ask before sending.
            </p>
          </div>
          <div>
            <div className="font-medium mb-1">5. Instagram &amp; Facebook — extra step</div>
            <p className="opacity-80">
              IG and FB require a connected Facebook Page + an Instagram
              Business / Creator account linked to it. If posts fail, open
              your Meta Business Suite and confirm the IG account is
              linked to a Page you own. The Connections tile shows which
              page/handle we resolved — if the wrong one shows, disconnect
              and reconnect.
            </p>
          </div>
        </div>
      )}
    </div>
  );
}


// ── Meta target picker ─────────────────────────────────────────────────
// Inline row under each Facebook / Instagram connection in the
// Connections panel. Shows the currently-pinned Page + linked IG,
// with a "Change target" button that opens a modal listing every
// Page the user manages plus the IG account linked to each one.
//
// Composio's Meta integration returns one access token that can post
// to multiple Pages — without explicit selection we'd silently pick
// pages[0], which is the root cause of "I connected IG/FB and nothing
// works". This UI surfaces and locks in the user's choice.
export function MetaTargetRow({ connection, orgId, onUpdated }) {
  const [open, setOpen] = useState(false);
  const summary = connection.target_summary || null;
  const hasTarget = Boolean(summary && (summary.page_name || summary.ig_username));
  const isInstagram = connection.toolkit === "instagram";

  // Friendly status copy under each FB/IG tile so the user instantly
  // knows where their posts will land — or what's blocking publishing.
  let body;
  if (hasTarget) {
    body = (
      <span className="opacity-90">
        Posting to{" "}
        {summary.page_name && (
          <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-blue-500/15 text-blue-300">
            <FacebookIcon className="h-3 w-3" /> {summary.page_name}
          </span>
        )}
        {summary.page_name && summary.ig_username && <span className="opacity-50"> · </span>}
        {summary.ig_username ? (
          <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-pink-500/15 text-pink-300">
            <InstagramIcon className="h-3 w-3" /> @{summary.ig_username}
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 text-[10px] text-amber-300">
            <AlertTriangle className="h-3 w-3" /> No linked IG on this Page
          </span>
        )}
      </span>
    );
  } else if (isInstagram) {
    body = (
      <span className="text-amber-300 inline-flex items-center gap-1">
        <AlertTriangle className="h-3 w-3" /> No IG target selected. Pick a Facebook Page first.
      </span>
    );
  } else {
    body = (
      <span className="text-amber-300 inline-flex items-center gap-1">
        <AlertTriangle className="h-3 w-3" /> No Page selected — publishing is blocked.
      </span>
    );
  }

  return (
    <li
      className="flex items-center gap-2 pl-6 text-[11px] opacity-80 pb-1"
      data-testid={`meta-target-row-${connection.connection_id}`}
    >
      <span className="flex-1 truncate">{body}</span>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-md border border-white/15 hover:border-white/30 hover:bg-white/5 transition"
        data-testid={`meta-target-change-${connection.connection_id}`}
      >
        <Settings2 className="h-3 w-3" /> {hasTarget ? "Change target" : "Select target"}
      </button>
      {open && (
        <MetaTargetModal
          orgId={orgId}
          connection={connection}
          onClose={() => setOpen(false)}
          onSaved={() => {
            setOpen(false);
            onUpdated && onUpdated();
          }}
        />
      )}
    </li>
  );
}


export function MetaTargetModal({ orgId, connection, onClose, onSaved }) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [targets, setTargets] = useState({ pages: [], selected_page_id: "" });
  const [saving, setSaving] = useState(false);
  const [picked, setPicked] = useState("");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError("");
      try {
        const { data } = await api.get(
          `/orgs/${orgId}/composio/connections/${connection.connection_id}/available-targets`,
        );
        if (cancelled) return;
        setTargets(data || { pages: [], selected_page_id: "" });
        setPicked(data?.selected_page_id || "");
      } catch (err) {
        if (cancelled) return;
        setError(err?.response?.data?.detail || "Couldn't load your Facebook Pages.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [orgId, connection.connection_id]);

  const save = async () => {
    if (!picked) {
      toast.error("Pick a Page first.");
      return;
    }
    setSaving(true);
    try {
      const { data } = await api.post(
        `/orgs/${orgId}/composio/connections/${connection.connection_id}/select-target`,
        { page_id: picked },
      );
      const pageName = data?.selected_page?.page_name || "this Page";
      const ig = data?.instagram?.username;
      toast.success(
        ig
          ? `Pinned ${pageName} + @${ig} for this org`
          : `Pinned ${pageName} for this org (no IG linked)`,
      );
      onSaved && onSaved();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed to pin target.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" data-testid="meta-target-modal">
      <div
        // Theme-aware modal: in light mode this renders as a white
        // card with dark text; in dark mode it stays the original
        // dark-zinc card. Previous version was hardcoded to
        // `bg-zinc-900 text-white` and looked broken (faint, near-
        // invisible) in light mode where the rest of the app is.
        className="w-full max-w-lg max-h-[80vh] overflow-hidden flex flex-col rounded-xl shadow-2xl border border-black/10 dark:border-white/10 bg-white text-zinc-900 dark:bg-zinc-900 dark:text-white"
      >
        <div className="flex items-center justify-between px-5 py-4 border-b border-black/10 dark:border-white/10">
          <div>
            <div className="text-base font-semibold">Select your Meta target</div>
            <div className="text-[12px] opacity-70 mt-0.5">
              One Facebook Page (and its linked Instagram account) per org. Pick the one this org should publish to.
            </div>
          </div>
          <button
            onClick={onClose}
            className="opacity-60 hover:opacity-100 shrink-0 ml-3"
            data-testid="meta-target-close"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="overflow-auto px-3 py-3 flex-1 min-h-[180px]">
          {loading && (
            <div className="flex flex-col items-center gap-2 px-3 py-10 text-sm opacity-80">
              <Loader2 className="h-5 w-5 animate-spin" />
              <span>Loading your Facebook Pages…</span>
            </div>
          )}
          {error && !loading && (
            <div
              className="mx-2 my-2 px-3 py-3 rounded-lg border border-amber-400/40 bg-amber-50/80 dark:bg-amber-500/10 text-amber-900 dark:text-amber-200 text-sm flex items-start gap-2"
              data-testid="meta-target-error"
            >
              <AlertTriangle className="h-4 w-4 mt-0.5 flex-shrink-0" />
              <div className="flex-1 min-w-0">
                <div className="font-medium mb-1">Couldn't load your Facebook Pages</div>
                <div className="opacity-90 break-words">{error}</div>
                <div className="opacity-70 mt-2 text-[12px]">
                  Most common cause: the Facebook connection lacks the
                  `pages_show_list` permission. Try disconnecting and
                  reconnecting Facebook with full Page-management
                  permissions.
                </div>
              </div>
            </div>
          )}
          {!loading && !error && targets.pages.length === 0 && (
            <div
              className="mx-2 my-2 px-3 py-4 rounded-lg border border-amber-400/40 bg-amber-50/80 dark:bg-amber-500/10 text-amber-900 dark:text-amber-200 text-sm space-y-2"
              data-testid="meta-target-empty"
            >
              <div className="font-medium flex items-center gap-2">
                <AlertTriangle className="h-4 w-4 flex-shrink-0" />
                No Facebook Pages found for this account
              </div>
              <div className="text-[12px] opacity-90 leading-relaxed">
                You need to be the admin (or editor) of at least one
                Facebook Page. Open{" "}
                <a href="https://business.facebook.com" target="_blank" rel="noreferrer" className="underline font-medium">
                  business.facebook.com
                </a>
                , create or claim a Page, then come back, disconnect
                Facebook, and reconnect it — this time make sure to grant{" "}
                <b>Manage your Pages</b> and{" "}
                <b>Publish content</b> permissions when prompted.
              </div>
            </div>
          )}
          {!loading && !error && targets.pages.length > 0 && (
            <ul className="space-y-1.5" data-testid="meta-target-list">
              {targets.pages.map((p) => {
                const checked = picked === p.page_id;
                const ig = p.linked_instagram;
                return (
                  <li
                    key={p.page_id}
                    onClick={() => setPicked(p.page_id)}
                    className={`px-3 py-2.5 rounded-md cursor-pointer border transition ${
                      checked
                        ? "border-orange-500 bg-orange-500/10"
                        : "border-black/10 hover:bg-black/5 dark:border-white/5 dark:hover:bg-white/5"
                    }`}
                    data-testid={`meta-target-page-${p.page_id}`}
                  >
                    <div className="flex items-start gap-3">
                      <input
                        type="radio"
                        name="meta-target-pick"
                        checked={checked}
                        onChange={() => setPicked(p.page_id)}
                        className="mt-1 accent-orange-500"
                        data-testid={`meta-target-page-radio-${p.page_id}`}
                      />
                      <div className="flex-1 min-w-0">
                        <div className="text-sm font-medium flex items-center gap-2">
                          <FacebookIcon className="h-3.5 w-3.5 text-blue-500" />
                          <span className="truncate">{p.page_name}</span>
                          {p.category && (
                            <span className="text-[10px] opacity-50 uppercase tracking-wider truncate">{p.category}</span>
                          )}
                        </div>
                        <div className="mt-1 text-[12px]">
                          {ig ? (
                            <span className="inline-flex items-center gap-1 text-pink-600 dark:text-pink-300">
                              <InstagramIcon className="h-3 w-3" />
                              @{ig.username || ig.ig_user_id} linked
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-1 text-amber-700 dark:text-amber-300/80">
                              <AlertTriangle className="h-3 w-3" />
                              No Instagram Business account linked to this Page
                            </span>
                          )}
                        </div>
                      </div>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
        <div className="px-5 py-3 border-t border-black/10 dark:border-white/10 flex items-center justify-end gap-2">
          <button
            onClick={onClose}
            className="text-xs px-3 py-1.5 rounded-md hover:bg-black/5 dark:hover:bg-white/5"
            data-testid="meta-target-cancel"
          >
            Cancel
          </button>
          <button
            onClick={save}
            disabled={saving || !picked}
            className="text-xs px-3 py-1.5 rounded-md bg-orange-500 hover:bg-orange-600 text-white inline-flex items-center gap-1 disabled:opacity-50 disabled:cursor-not-allowed"
            data-testid="meta-target-save"
          >
            {saving ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />}
            Pin as target
          </button>
        </div>
      </div>
    </div>
  );
}
