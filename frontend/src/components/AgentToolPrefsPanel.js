import React, { useCallback, useEffect, useMemo, useState } from "react";
import api from "../lib/api";
import { toast } from "sonner";
import { Search, Plug, Loader2, Wrench } from "lucide-react";

/**
 * AgentToolPrefsPanel
 * -------------------
 * Lists every agent visible to this org (built-in + this org's custom
 * agents) with a switch that opts each one in to Composio tool calling.
 *
 * Default is OFF. When ON, the agent can invoke any of the user's
 * connected tools (Gmail, Slack, etc.) up to 3 times per turn. Owners
 * and admins can flip the switch; members get a read-only view.
 */
export default function AgentToolPrefsPanel({ orgId }) {
  const [agents, setAgents] = useState([]);
  const [canEdit, setCanEdit] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(null);
  const [q, setQ] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await api.get(`/orgs/${orgId}/agent-tool-prefs`);
      setAgents(data.agents || []);
      setCanEdit(Boolean(data.can_edit));
    } catch (e) {
      toast.error("Couldn't load agent tool settings");
    } finally {
      setLoading(false);
    }
  }, [orgId]);

  useEffect(() => { load(); }, [load]);

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
      <div className="flex flex-wrap items-center gap-3">
        <p className="text-sm opacity-70 flex-1 min-w-[240px]">
          Pick which agents are allowed to use your team's connected tools.
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
