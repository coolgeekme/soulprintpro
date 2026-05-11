import React, { useEffect, useState } from "react";
import api from "../lib/api";
import { Plus, Trash2, Pencil, X, Bot, Loader2 } from "lucide-react";
import { toast } from "sonner";

/**
 * CustomAgentsPanel — CRUD for per-org custom agents.
 *
 * Owners/admins can create, edit, delete custom agents. Members see
 * read-only list. Created agents are immediately usable in every team's
 * agent picker (mention, manage-agents dialog, smart-routing).
 *
 * Endpoints (see /app/backend/custom_agents.py):
 *   - GET  /api/orgs/{org_id}/custom-agents
 *   - POST /api/orgs/{org_id}/custom-agents
 *   - PATCH /api/orgs/{org_id}/custom-agents/{aid}
 *   - DELETE /api/orgs/{org_id}/custom-agents/{aid}
 */

const EMPTY = {
  name: "",
  role: "",
  description: "",
  system: "",
  emoji: "",
  accent: "#f97316",
};

export default function CustomAgentsPanel({ orgId, canEdit = false }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(null); // null | "new" | aid
  const [form, setForm] = useState(EMPTY);
  const [saving, setSaving] = useState(false);

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

  const openNew = () => { setForm(EMPTY); setEditing("new"); };
  const openEdit = (a) => {
    setForm({
      name: a.name || "",
      role: a.role || "",
      description: a.description || "",
      system: a.system || "",
      emoji: a.emoji || "",
      accent: a.accent || "#f97316",
    });
    setEditing(a.id);
  };
  const close = () => { setEditing(null); setForm(EMPTY); };

  const submit = async () => {
    if (!form.name.trim()) { toast.error("Name is required"); return; }
    if (form.system.trim().length < 10) {
      toast.error("System prompt must be at least 10 characters"); return;
    }
    setSaving(true);
    try {
      const body = {
        name: form.name.trim(),
        role: form.role.trim() || form.name.trim(),
        description: form.description.trim(),
        system: form.system.trim(),
        emoji: form.emoji.trim(),
        accent: form.accent || "#f97316",
      };
      if (editing === "new") {
        await api.post(`/orgs/${orgId}/custom-agents`, body);
        toast.success("Custom agent created");
      } else {
        await api.patch(`/orgs/${orgId}/custom-agents/${editing}`, body);
        toast.success("Custom agent updated");
      }
      close();
      await load();
    } catch (err) {
      const msg = err?.response?.data?.detail || "Could not save";
      toast.error(msg);
    } finally {
      setSaving(false);
    }
  };

  const remove = async (a) => {
    if (!window.confirm(`Delete "${a.name}"? Existing chats keep their history but the agent will no longer be summonable.`)) return;
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
            onClick={openNew}
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
                      onClick={() => openEdit(a)}
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

      {/* Editor modal */}
      {editing !== null && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm"
          onClick={close}
          data-testid="custom-agent-editor-overlay"
        >
          <div
            className="sp-glass-strong rounded-2xl w-full max-w-2xl max-h-[90vh] overflow-y-auto p-6 space-y-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between">
              <h3 className="sp-display text-xl">
                {editing === "new" ? "Create custom agent" : "Edit custom agent"}
              </h3>
              <button onClick={close} className="p-1 rounded hover:bg-white/10">
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="grid sm:grid-cols-[1fr_auto_auto] gap-3">
              <div>
                <label className="text-[11px] uppercase tracking-widest sp-subtle">Name *</label>
                <input
                  type="text"
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  className="sp-input w-full mt-1"
                  placeholder="e.g. Senior Pricing Strategist"
                  maxLength={80}
                  data-testid="custom-agent-name"
                />
              </div>
              <div>
                <label className="text-[11px] uppercase tracking-widest sp-subtle">Emoji</label>
                <input
                  type="text"
                  value={form.emoji}
                  onChange={(e) => setForm({ ...form, emoji: e.target.value })}
                  className="sp-input w-16 mt-1 text-center"
                  placeholder="💰"
                  maxLength={4}
                  data-testid="custom-agent-emoji"
                />
              </div>
              <div>
                <label className="text-[11px] uppercase tracking-widest sp-subtle">Accent</label>
                <input
                  type="color"
                  value={form.accent}
                  onChange={(e) => setForm({ ...form, accent: e.target.value })}
                  className="h-9 w-16 mt-1 rounded cursor-pointer bg-transparent"
                  data-testid="custom-agent-accent"
                />
              </div>
            </div>

            <div>
              <label className="text-[11px] uppercase tracking-widest sp-subtle">Role / title</label>
              <input
                type="text"
                value={form.role}
                onChange={(e) => setForm({ ...form, role: e.target.value })}
                className="sp-input w-full mt-1"
                placeholder="e.g. SaaS pricing & packaging lead"
                maxLength={120}
                data-testid="custom-agent-role"
              />
            </div>

            <div>
              <label className="text-[11px] uppercase tracking-widest sp-subtle">Short description</label>
              <input
                type="text"
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                className="sp-input w-full mt-1"
                placeholder="One-line summary shown in the agent picker"
                maxLength={400}
                data-testid="custom-agent-description"
              />
            </div>

            <div>
              <label className="text-[11px] uppercase tracking-widest sp-subtle">System prompt *</label>
              <textarea
                value={form.system}
                onChange={(e) => setForm({ ...form, system: e.target.value })}
                className="sp-input w-full mt-1 font-mono text-xs leading-relaxed"
                rows={10}
                maxLength={8000}
                placeholder={
                  "Define this agent's personality, expertise, decision rules, and tone.\n\n" +
                  "Example:\n" +
                  "You are a senior B2B SaaS pricing strategist with 15 years of experience.\n" +
                  "Always think in unit economics, gross margin, and competitive comps.\n" +
                  "Reject feature-count gating without market evidence.\n" +
                  "Cite specific competitor pricing when comparing tiers."
                }
                data-testid="custom-agent-system"
              />
              <div className="text-[10px] sp-subtle mt-1 flex justify-between">
                <span>This becomes the agent's "brain". Foundry's org Soul is appended automatically.</span>
                <span>{form.system.length} / 8000</span>
              </div>
            </div>

            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                onClick={close}
                className="sp-btn sp-btn-ghost text-sm"
                disabled={saving}
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={submit}
                className="sp-btn sp-btn-primary text-sm"
                disabled={saving}
                data-testid="custom-agent-submit"
              >
                {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
                {editing === "new" ? "Create agent" : "Save changes"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
