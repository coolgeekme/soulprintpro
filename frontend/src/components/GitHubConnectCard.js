/* eslint-disable react-hooks/exhaustive-deps */
import { useEffect, useState } from "react";
import { Github, Loader2, Trash2, ShieldCheck, Building2, User, Pencil } from "lucide-react";
import { toast } from "sonner";
import api from "../lib/api";
import GitHubQuickEditModal from "./GitHubQuickEditModal";

/**
 * GitHubConnectCard — list current connections for an org and let the
 * caller (a) connect their personal account, (b) connect/share an
 * organisation-wide account if they're an admin.
 *
 * Embedded inside the Integrations panel. Self-disables (renders a
 * gentle hint instead of the buttons) when the backend reports
 * `configured: false` — so a deploy without GITHUB_CLIENT_ID set
 * doesn't show clickable but-broken UX.
 */
export default function GitHubConnectCard({ orgId }) {
  const [loading, setLoading] = useState(true);
  const [configured, setConfigured] = useState(true);
  const [connections, setConnections] = useState([]);
  const [isAdmin, setIsAdmin] = useState(false);
  const [busy, setBusy] = useState("");
  const [editorOpen, setEditorOpen] = useState(false);

  const refresh = async () => {
    if (!orgId) return;
    try {
      const { data } = await api.get(`/github/status?org_id=${encodeURIComponent(orgId)}`);
      setConfigured(!!data?.configured);
      setConnections(data?.connections || []);
      setIsAdmin(!!data?.is_admin);
    } catch (_) {
      setConfigured(false);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { refresh(); }, [orgId]);

  const startConnect = async (scopeType) => {
    setBusy(scopeType);
    try {
      const { data } = await api.post("/github/oauth/start", {
        org_id: orgId, scope_type: scopeType,
      });
      if (data?.authorize_url) {
        window.location.href = data.authorize_url;
      } else {
        toast.error("Could not generate the GitHub authorization URL.");
      }
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Could not start GitHub connection.");
    } finally {
      setBusy("");
    }
  };

  const disconnect = async (id) => {
    if (!window.confirm("Disconnect this GitHub account? Agents will lose access immediately.")) return;
    try {
      await api.delete(`/github/connections/${id}?org_id=${encodeURIComponent(orgId)}`);
      toast.success("Disconnected");
      await refresh();
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Could not disconnect.");
    }
  };

  if (loading) {
    return (
      <div className="sp-glass rounded-xl p-5 flex items-center gap-3">
        <Loader2 className="w-4 h-4 animate-spin opacity-60" />
        <span className="text-sm sp-subtle">Loading GitHub status…</span>
      </div>
    );
  }

  if (!configured) {
    return (
      <div className="sp-glass rounded-xl p-5 border border-amber-500/30 bg-amber-500/5">
        <div className="flex items-center gap-2 mb-2">
          <Github className="w-4 h-4" />
          <span className="text-sm font-medium">GitHub integration</span>
        </div>
        <p className="text-xs sp-subtle leading-relaxed">
          The platform admin hasn't configured GitHub OAuth yet. Once they set
          <code className="mx-1 px-1 rounded bg-black/30">GITHUB_CLIENT_ID</code>
          and the secret, you'll be able to connect your account here.
        </p>
      </div>
    );
  }

  const personal = connections.find((c) => c.scope === "user");
  const orgConn = connections.find((c) => c.scope === "org");

  return (
    <div className="sp-glass rounded-xl p-5" data-testid="github-connect-card">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <Github className="w-4 h-4" />
          <span className="text-sm font-medium">GitHub</span>
        </div>
        <div className="text-[10px] uppercase tracking-widest sp-subtle">
          {connections.length} connection{connections.length === 1 ? "" : "s"}
        </div>
      </div>

      {/* Personal connection */}
      <ConnectionRow
        icon={User}
        label="Your personal GitHub"
        sub="Used when missions act 'as you'."
        connection={personal}
        onConnect={() => startConnect("user")}
        onDisconnect={() => disconnect(personal?.id)}
        connectBusy={busy === "user"}
      />

      {/* Org connection — only admins see the connect button, but
          everyone sees the "shared" state if it exists */}
      {(isAdmin || orgConn) && (
        <div className="mt-4">
          <ConnectionRow
            icon={Building2}
            label="Shared org GitHub"
            sub={isAdmin
              ? "Available to every member of this org for missions."
              : "Set up by an admin — visible to your missions as a fallback."}
            connection={orgConn}
            onConnect={isAdmin ? () => startConnect("org") : null}
            onDisconnect={isAdmin && orgConn ? () => disconnect(orgConn?.id) : null}
            connectBusy={busy === "org"}
          />
        </div>
      )}

      <div className="mt-4 pt-4 border-t border-white/10 flex items-center gap-3 flex-wrap">
        <span className="inline-flex items-center gap-1.5 text-[11px] sp-subtle">
          <ShieldCheck className="w-3 h-3 text-green-400/80" />
          Tokens are encrypted at rest. Disconnect anytime.
        </span>
        {connections.length > 0 && (
          <button
            type="button"
            onClick={() => setEditorOpen(true)}
            className="sp-btn sp-btn-ghost text-xs ml-auto"
            data-testid="gh-quick-edit-btn"
          >
            <Pencil className="w-3 h-3" /> Quick-edit a file
          </button>
        )}
      </div>

      {editorOpen && (
        <GitHubQuickEditModal orgId={orgId} onClose={() => setEditorOpen(false)} />
      )}
    </div>
  );
}


function ConnectionRow({ icon: Icon, label, sub, connection, onConnect, onDisconnect, connectBusy }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="flex items-start gap-3 min-w-0">
        <Icon className="w-4 h-4 opacity-70 shrink-0 mt-1" />
        <div className="min-w-0">
          <div className="text-sm font-medium">{label}</div>
          {connection ? (
            <div className="text-xs sp-subtle">
              Connected as <b className="text-white/90">@{connection.github_username}</b>
            </div>
          ) : (
            <div className="text-xs sp-subtle leading-relaxed">{sub}</div>
          )}
        </div>
      </div>
      {connection ? (
        <button
          type="button"
          onClick={onDisconnect}
          className="sp-btn sp-btn-ghost text-xs shrink-0"
          data-testid={`gh-disconnect-${connection.scope}`}
        >
          <Trash2 className="w-3 h-3" /> Disconnect
        </button>
      ) : onConnect ? (
        <button
          type="button"
          onClick={onConnect}
          disabled={connectBusy}
          className="sp-btn sp-btn-primary text-xs shrink-0 disabled:opacity-60"
          data-testid={`gh-connect-${label.toLowerCase().includes("org") ? "org" : "user"}`}
        >
          {connectBusy
            ? <Loader2 className="w-3 h-3 animate-spin" />
            : <Github className="w-3 h-3" />}
          Connect
        </button>
      ) : null}
    </div>
  );
}
