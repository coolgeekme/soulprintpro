import React, { useCallback, useEffect, useMemo, useState } from "react";
import api from "../lib/api";
import { toast } from "sonner";
import { Search, Plug, Loader2, Wrench } from "lucide-react";

/**
 * AgentToolPrefsPanel
 * -------------------
 * Lists every agent visible to this org (built-in + this org's custom
 * agents) with a switch that opts each one in/out of Composio tool calling.
 *
 * Default is ON — every agent can use any toolkit the org has actively
 * connected. Owners/admins flip the switch OFF to opt a specific agent
 * out (e.g. "I don't want my Finance Auditor agent touching Stripe").
 * Members get a read-only view.
 */
export default function AgentToolPrefsPanel({ orgId }) {
  const [agents, setAgents] = useState([]);
  const [canEdit, setCanEdit] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(null);
  const [q, setQ] = useState("");
  const [recs, setRecs] = useState([]);
  const [recsDismissed, setRecsDismissed] = useState(false);
  const [applying, setApplying] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [{ data: prefs }, { data: rec }] = await Promise.all([
        api.get(`/orgs/${orgId}/agent-tool-prefs`),
        api.get(`/orgs/${orgId}/agent-tool-recommendations`).catch(() => ({ data: { recommendations: [] } })),
      ]);
      setAgents(prefs.agents || []);
      setCanEdit(Boolean(prefs.can_edit));
      setRecs(rec?.recommendations || []);
    } catch (e) {
      toast.error("Couldn't load agent tool settings");
    } finally {
      setLoading(false);
    }
  }, [orgId]);

  useEffect(() => { load(); }, [load]);

  const applyRecommendations = async () => {
    if (!recs.length) return;
    setApplying(true);
    try {
      const ids = recs.map((r) => r.agent_id);
      await api.post(`/orgs/${orgId}/agent-tool-prefs/apply-recommendations`, {
        agent_ids: ids,
      });
      toast.success(`Tools enabled for ${ids.length} recommended agent${ids.length === 1 ? "" : "s"}.`);
      await load();
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Couldn't apply recommendations");
    } finally {
      setApplying(false);
    }
  };

  const toggle = async (agentId, nextValue) => {
    setBusy(agentId);
    try {
      await api.put(`/orgs/${orgId}/agent-tool-prefs/${agentId}`, {
        tools_enabled: nextValue,
      });
      setAgents((prev) => prev.map((a) =>
        a.agent_id === agentId ? { ...a, tools_enabled: nextValue } : a,
      ));
      toast.success(nextValue ? "Tools enabled for this agent." : "Tools disabled.");
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Couldn't update");
    } finally {
      setBusy(null);
    }
  };

  const filtered = useMemo(() => {
    if (!q.trim()) return agents;
    const lc = q.toLowerCase();
    return agents.filter((a) =>
      (a.name || "").toLowerCase().includes(lc)
      || (a.role || "").toLowerCase().includes(lc)
      || (a.agent_id || "").toLowerCase().includes(lc),
    );
  }, [agents, q]);

  const enabledCount = agents.filter((a) => a.tools_enabled).length;

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm opacity-70" data-testid="agent-tools-loading">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading agents…
      </div>
    );
  }

  return (
    <div className="space-y-3" data-testid="agent-tool-prefs-panel">
      {recs.length > 0 && !recsDismissed && canEdit && (
        <div
          className="rounded-xl border border-orange-500/30 bg-gradient-to-r from-orange-500/10 to-amber-500/5 p-4 flex flex-wrap items-start gap-3"
          data-testid="recommendations-banner"
        >
          <Wrench className="h-4 w-4 text-orange-400 mt-0.5 flex-shrink-0" />
          <div className="flex-1 min-w-[240px]">
            <p className="text-sm font-medium">
              Smart defaults available
            </p>
            <p className="text-xs opacity-70 mt-0.5">
              Based on what you've connected, we suggest enabling tools for{" "}
              <span className="font-medium text-orange-200">
                {recs.length} agent{recs.length === 1 ? "" : "s"}
              </span>
              {" "}whose role matches your toolkit
              {" "}({recs.slice(0, 3).map((r) => r.name).join(", ")}
              {recs.length > 3 ? `, +${recs.length - 3} more` : ""}).
            </p>
          </div>
          <div className="flex gap-2">
            <button
              onClick={() => setRecsDismissed(true)}
              className="text-xs px-2.5 py-1.5 rounded-md hover:bg-white/5 opacity-70 hover:opacity-100"
              data-testid="recs-dismiss"
            >
              Dismiss
            </button>
            <button
              onClick={applyRecommendations}
              disabled={applying}
              className="text-xs px-3 py-1.5 rounded-md bg-orange-500/90 hover:bg-orange-500 text-white disabled:opacity-50 inline-flex items-center gap-1"
              data-testid="recs-apply"
            >
              {applying ? <Loader2 className="h-3 w-3 animate-spin" /> : <Plug className="h-3 w-3" />}
              Apply recommended
            </button>
          </div>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <p className="text-sm opacity-70 flex-1 min-w-[240px]">
          Every agent can use your org&apos;s connected tools by default.
          Flip the switch OFF here only if you want a specific agent to
          stay tools-free.
          {" "}
          <span className="opacity-100 font-medium">{enabledCount}</span>
          {" "}of <span className="font-medium">{agents.length}</span> agents enabled.
        </p>
        <div className="relative">
          <Search className="absolute left-2 top-2 h-3.5 w-3.5 opacity-50" />
          <input
            type="text"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search agents…"
            className="pl-7 pr-2 py-1.5 text-xs rounded-md bg-black/20 border border-white/10 focus:border-white/30 outline-none w-56"
            data-testid="agent-tool-prefs-search"
          />
        </div>
      </div>
      {!canEdit && (
        <p className="text-xs opacity-60">
          Only org owners/admins can change these settings. Showing read-only view.
        </p>
      )}
      <div className="max-h-[420px] overflow-y-auto rounded-xl border border-white/10 bg-black/10 divide-y divide-white/5">
        {filtered.map((a) => (
          <div
            key={a.agent_id}
            className="flex items-center gap-3 px-3 py-2.5 hover:bg-white/5 transition"
            data-testid={`agent-tool-row-${a.agent_id}`}
          >
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 text-sm">
                <span className="truncate">{a.name}</span>
                {a.is_custom && (
                  <span className="text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-orange-500/20 text-orange-300 border border-orange-500/30">
                    custom
                  </span>
                )}
              </div>
              <div className="text-[11px] opacity-50 truncate">
                {a.role || a.agent_id}
              </div>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={a.tools_enabled}
              disabled={!canEdit || busy === a.agent_id}
              onClick={() => toggle(a.agent_id, !a.tools_enabled)}
              className={[
                "relative h-5 w-9 rounded-full transition-colors flex-shrink-0",
                a.tools_enabled ? "bg-emerald-500/80" : "bg-white/15",
                (!canEdit || busy === a.agent_id) ? "opacity-50 cursor-not-allowed" : "cursor-pointer",
              ].join(" ")}
              data-testid={`agent-tool-toggle-${a.agent_id}`}
            >
              <span
                className={[
                  "absolute top-0.5 left-0.5 h-4 w-4 rounded-full bg-white shadow transition-transform",
                  a.tools_enabled ? "translate-x-4" : "translate-x-0",
                ].join(" ")}
              />
            </button>
          </div>
        ))}
        {filtered.length === 0 && (
          <div className="text-xs opacity-50 p-4 text-center">No agents match.</div>
        )}
      </div>
      <p className="text-[11px] opacity-50 inline-flex items-center gap-1">
        <Wrench className="h-3 w-3" />
        Tool budget per turn: 3 calls. Connect accounts under "Your connected accounts" above.
      </p>
    </div>
  );
}
