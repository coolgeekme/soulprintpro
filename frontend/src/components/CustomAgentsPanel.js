import React, { useEffect, useState } from "react";
import api from "../lib/api";
import { Plus, Trash2, Pencil, Bot, Loader2 } from "lucide-react";
import { toast } from "sonner";
import CustomAgentWizard from "./CustomAgentWizard";

/**
 * CustomAgentsPanel — CRUD for per-org custom agents.
 *
 * Owners/admins can create, edit, delete custom agents. Members see the
 * read-only list. Created agents are immediately usable in every team's
 * agent picker (mention, manage-agents dialog, smart-routing).
 *
 * The actual create/edit flow now lives in `CustomAgentWizard` — a
 * 4-step guided builder (Identity → Role → Voice & rules → Preview)
 * with a templates strip on step 1 and an Advanced mode escape hatch
 * for power users. The hard work (composing the system prompt from
 * plain-English answers + running a live LLM preview) happens server-
 * side at:
 *
 *   POST /api/orgs/{org_id}/custom-agents/draft-system
 *   POST /api/orgs/{org_id}/custom-agents/preview
 *
 * Endpoints powering the CRUD itself (see /app/backend/custom_agents.py):
 *   - GET    /api/orgs/{org_id}/custom-agents
 *   - POST   /api/orgs/{org_id}/custom-agents
 *   - PATCH  /api/orgs/{org_id}/custom-agents/{aid}
 *   - DELETE /api/orgs/{org_id}/custom-agents/{aid}
 */

export default function CustomAgentsPanel({ orgId, canEdit = false }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  // null when closed, "new" when creating, the full agent object when editing
  const [editing, setEditing] = useState(null);

  const load = async () => {
    setLoading(true);
    try {
      const { data } = await api.get(`/orgs/${orgId}/custom-agents`);
      setItems(Array.isArray(data) ? data : []);
    } catch (err) {
      toast.error("Could not load custom agents");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [orgId]);

  const remove = async (a) => {
    if (!window.confirm(
      `Delete "${a.name}"? Existing chats keep their history but the agent will no longer be summonable.`
    )) return;
    try {
      await api.delete(`/orgs/${orgId}/custom-agents/${a.id}`);
      toast.success("Custom agent deleted");
      await load();
    } catch (err) {
      toast.error("Could not delete");
    }
  };

  return (
    <div className="space-y-4" data-testid="custom-agents-panel">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div>
          <h3 className="sp-display text-xl flex items-center gap-2">
            <Bot className="w-5 h-5 text-orange-300" />
            Custom agents
          </h3>
          <p className="text-xs sp-subtle mt-1">
            Build agents that match your team's exact role, voice, and playbook.
            Available in every team's chat picker.
          </p>
        </div>
        {canEdit && (
          <button
            type="button"
            onClick={() => setEditing("new")}
            className="sp-btn sp-btn-primary text-xs"
            data-testid="custom-agent-new"
          >
            <Plus className="w-3.5 h-3.5" /> New agent
          </button>
        )}
      </div>

      {loading ? (
        <div className="sp-glass rounded-xl p-6 text-center text-sm sp-subtle">
          <Loader2 className="w-4 h-4 animate-spin inline mr-2" />
          Loading…
        </div>
      ) : items.length === 0 ? (
        <div className="sp-glass rounded-xl p-6 text-center text-sm sp-subtle">
          No custom agents yet.{" "}
          {canEdit
            ? <>Click <span className="text-orange-300">New agent</span> to build your first one.</>
            : <>Ask an admin to create one for your org.</>}
        </div>
      ) : (
        <div className="grid sm:grid-cols-2 gap-3">
          {items.map((a) => (
            <div
              key={a.id}
              className="sp-glass rounded-xl p-4 flex flex-col gap-2"
              data-testid={`custom-agent-card-${a.id}`}
            >
              <div className="flex items-start gap-2">
                <div
                  className="w-9 h-9 rounded-lg flex items-center justify-center text-base shrink-0"
                  style={{ background: `${a.accent}22`, color: a.accent }}
                >
                  {a.emoji || <Bot className="w-4 h-4" />}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="font-medium text-sm truncate">{a.name}</div>
                  <div className="text-[11px] sp-subtle truncate">{a.role}</div>
                </div>
                {canEdit && (
                  <div className="flex items-center gap-1 opacity-70 hover:opacity-100">
                    <button
                      type="button"
                      onClick={() => setEditing(a)}
                      className="p-1.5 rounded hover:bg-white/10"
                      data-testid={`custom-agent-edit-${a.id}`}
                      title="Edit"
                    >
                      <Pencil className="w-3.5 h-3.5" />
                    </button>
                    <button
                      type="button"
                      onClick={() => remove(a)}
                      className="p-1.5 rounded hover:bg-red-500/15 text-red-300"
                      data-testid={`custom-agent-delete-${a.id}`}
                      title="Delete"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                )}
              </div>
              {a.description && (
                <p className="text-xs sp-subtle line-clamp-2">{a.description}</p>
              )}
            </div>
          ))}
        </div>
      )}

      {editing !== null && (
        <CustomAgentWizard
          orgId={orgId}
          mode={editing === "new" ? "create" : "edit"}
          initial={editing === "new" ? null : editing}
          onCancel={() => setEditing(null)}
          onSaved={() => { setEditing(null); load(); }}
        />
      )}
    </div>
  );
}
