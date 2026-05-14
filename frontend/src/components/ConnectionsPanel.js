import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import api from "../lib/api";
import { toast } from "sonner";
import { Link2, Check, Loader2, Trash2, ExternalLink, Plug, RefreshCw } from "lucide-react";

/**
 * ConnectionsPanel
 * ----------------
 * Lets a workspace member connect their own SaaS accounts (Gmail, Slack,
 * Notion, HubSpot, Google Calendar) through Composio's managed OAuth.
 *
 * Mental model:
 *   1. Click "Connect Gmail" → backend returns Composio's hosted-OAuth URL
 *   2. User opens that URL in a new tab → authorises on Google
 *   3. Composio redirects them back to /app/org/:orgId#connections with
 *      ?composio_callback=1 in the query string
 *   4. We detect the callback, poll `/refresh` for ~20s, and flip the card
 *      from "Connecting…" to "Connected"
 *
 * Per-user scope: each org member has their own connections. Owners do
 * NOT see other members' connections (mirrors how we treat individual
 * GitHub access in the existing AccountPage flow).
 */
const TOOLKIT_LOGO_BG = {
  gmail: "from-red-500/20 to-orange-500/20",
  googlecalendar: "from-blue-500/20 to-sky-500/20",
  slack: "from-purple-500/20 to-fuchsia-500/20",
  notion: "from-zinc-300/20 to-zinc-500/20",
  hubspot: "from-orange-500/20 to-amber-500/20",
};

const TOOLKIT_INITIAL = {
  gmail: "G",
  googlecalendar: "C",
  slack: "S",
  notion: "N",
  hubspot: "H",
};

export default function ConnectionsPanel({ orgId }) {
  const [toolkits, setToolkits] = useState([]);
  const [connections, setConnections] = useState([]);
  const [available, setAvailable] = useState(true);
  const [loading, setLoading] = useState(true);
  const [busySlug, setBusySlug] = useState(null);
  const [pendingId, setPendingId] = useState(null);
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

  // When the user returns from Composio's OAuth flow we land on
  // /app/org/:orgId?composio_callback=1. Poll `/refresh` on every pending
  // connection for up to ~30s so the card flips to "Connected" without
  // a manual refresh. We strip the query param once polling completes.
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
        // Force-refresh each pending connection — Composio doesn't push us,
        // so we explicitly ask it to re-sync status from the upstream.
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
          // Strip the query param so subsequent reloads don't re-poll.
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

  const connectionByToolkit = useMemo(() => {
    const m = {};
    for (const c of connections) {
      // If a user has both INITIATED and ACTIVE rows, ACTIVE wins.
      const prev = m[c.toolkit];
      if (!prev || c.status === "ACTIVE") m[c.toolkit] = c;
    }
    return m;
  }, [connections]);

  const handleConnect = async (slug) => {
    setBusySlug(slug);
    try {
      const { data } = await api.post(`/orgs/${orgId}/composio/connect`, { toolkit: slug });
      setPendingId(data.connection_id);
      // Open Composio's hosted OAuth in a new tab so we don't lose React state.
      window.open(data.redirect_url, "_blank", "noopener,noreferrer");
      toast.message("Finish authorisation in the new tab — we'll auto-detect when done.");
      // Pre-populate so the UI shows "Connecting…" immediately.
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
      <div className="flex items-baseline justify-between gap-3">
        <p className="text-sm opacity-70 max-w-xl">
          Connect your own SaaS accounts so agents can read & write on your behalf —
          send Gmail, post Slack, update HubSpot, etc. Each connection is scoped
          to <span className="opacity-100 font-medium">your user</span>, not shared with the rest of the org.
        </p>
        <button
          onClick={load}
          className="text-xs opacity-60 hover:opacity-100 inline-flex items-center gap-1"
          data-testid="connections-refresh-btn"
        >
          <RefreshCw className="h-3 w-3" /> Refresh
        </button>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {toolkits.map((t) => {
          const conn = connectionByToolkit[t.slug];
          const status = conn?.status;
          const isBusy = busySlug === t.slug;
          const bg = TOOLKIT_LOGO_BG[t.slug] || "from-zinc-500/20 to-zinc-700/20";
          return (
            <div
              key={t.slug}
              className="group flex items-center gap-3 rounded-xl border border-white/10 bg-black/20 p-4 hover:border-white/20 transition"
              data-testid={`connection-card-${t.slug}`}
            >
              <div className={`h-10 w-10 rounded-lg bg-gradient-to-br ${bg} flex items-center justify-center text-base font-semibold tracking-tight border border-white/10`}>
                {TOOLKIT_INITIAL[t.slug] || t.slug[0].toUpperCase()}
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-medium">{t.name}</span>
                  {status === "ACTIVE" && (
                    <span className="inline-flex items-center gap-1 text-[11px] uppercase tracking-wide px-1.5 py-0.5 rounded-full bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">
                      <Check className="h-3 w-3" /> Connected
                    </span>
                  )}
                  {status === "INITIATED" && (
                    <span className="inline-flex items-center gap-1 text-[11px] uppercase tracking-wide px-1.5 py-0.5 rounded-full bg-amber-500/20 text-amber-300 border border-amber-500/30">
                      <Loader2 className="h-3 w-3 animate-spin" /> Connecting
                    </span>
                  )}
                </div>
                <p className="text-xs opacity-60 truncate">
                  {conn?.account_label || t.description}
                </p>
              </div>
              {status === "ACTIVE" ? (
                <button
                  onClick={() => handleDisconnect(conn.connection_id)}
                  className="text-xs opacity-60 hover:opacity-100 hover:text-red-400 transition inline-flex items-center gap-1 px-2 py-1.5 rounded-md hover:bg-red-500/10"
                  data-testid={`disconnect-${t.slug}-btn`}
                  title="Disconnect"
                >
                  <Trash2 className="h-3.5 w-3.5" /> <span className="hidden sm:inline">Disconnect</span>
                </button>
              ) : status === "INITIATED" ? (
                <button
                  onClick={() => handleRefresh(conn.connection_id)}
                  className="text-xs opacity-60 hover:opacity-100 inline-flex items-center gap-1 px-2 py-1.5 rounded-md hover:bg-white/5"
                  data-testid={`check-${t.slug}-btn`}
                >
                  Check status
                </button>
              ) : (
                <button
                  onClick={() => handleConnect(t.slug)}
                  disabled={isBusy}
                  className="text-xs px-3 py-1.5 rounded-md bg-orange-500/90 hover:bg-orange-500 text-white inline-flex items-center gap-1 disabled:opacity-50"
                  data-testid={`connect-${t.slug}-btn`}
                >
                  {isBusy ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <ExternalLink className="h-3.5 w-3.5" />
                  )}
                  Connect
                </button>
              )}
            </div>
          );
        })}
      </div>

      <p className="text-[11px] opacity-50">
        Authentication is handled by Composio's hosted OAuth — Foundry never sees your password.
        Disconnecting revokes the token and stops agent access immediately.
      </p>
    </div>
  );
}
