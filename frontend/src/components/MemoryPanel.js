import React, { useEffect, useState } from "react";
import { toast } from "sonner";
import {
  Brain, Plus, Trash2, Pencil, Save, X, Archive, ArchiveRestore, Loader2,
  AlertCircle, ArrowRight,
} from "lucide-react";
import api from "../lib/api";

/**
 * MemoryPanel — user-scoped long-term memory editor with STRICT
 * per-organization isolation.
 *
 * Layout:
 *   1. Org picker (top) — choose which organization's memory pool
 *      you're viewing. Memories never leak across orgs.
 *   2. "Needs cleanup" banner — appears only when the user has legacy
 *      untagged memories from before isolation was enforced. Each
 *      untagged memory must be assigned to an org or deleted before
 *      it can be injected into any agent reply.
 *   3. Add-memory form — content + category + importance; the org
 *      is inherited from the picker so a user can't accidentally
 *      tag a memory to the wrong workspace.
 *   4. Filter chips + list — scoped to the selected org.
 *
 * Backend contract: see /app/backend/memory_routes.py.
 */

const IMPORTANCE_COLORS = {
  high: "bg-orange-500/15 text-orange-300 border-orange-500/30",
  medium: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  low: "bg-zinc-500/15 text-zinc-300 border-zinc-500/30",
};

const CATEGORY_LABELS = {
  "brand-voice": "Brand voice",
  pricing: "Pricing",
  icp: "ICP",
  "past-clients": "Past clients",
  playbooks: "Playbooks",
  "agent-feedback": "Agent feedback",
  preferences: "Preferences",
  work: "Work",
  other: "Other",
};

const STORAGE_KEY = "memory.lastOrgId";

export default function MemoryPanel() {
  const [orgs, setOrgs] = useState([]);
  const [orgId, setOrgId] = useState("");
  const [loading, setLoading] = useState(true);
  const [memories, setMemories] = useState([]);
  const [untagged, setUntagged] = useState([]);
  const [categories, setCategories] = useState([]);
  const [filter, setFilter] = useState("all");
  const [showArchived, setShowArchived] = useState(false);

  // Load the user's orgs once on mount, then pick the last-used org
  // (persisted in localStorage) or fall back to the first one.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { data } = await api.get("/orgs");
        const list = Array.isArray(data) ? data : [];
        if (cancelled) return;
        setOrgs(list);
        const remembered = localStorage.getItem(STORAGE_KEY);
        const chosen =
          list.find((o) => o.id === remembered)?.id || list[0]?.id || "";
        setOrgId(chosen);
        if (!list.length) setLoading(false);
      } catch (e) {
        if (!cancelled) {
          setOrgs([]);
          setLoading(false);
        }
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // (Re)load memories whenever the picked org or archived toggle changes.
  useEffect(() => {
    if (!orgId) return;
    let cancelled = false;
    setLoading(true);
    (async () => {
      try {
        const [{ data: tagged }, { data: ut }] = await Promise.all([
          api.get("/memory", {
            params: { org_id: orgId, include_archived: showArchived },
          }),
          api.get("/memory/untagged"),
        ]);
        if (cancelled) return;
        setMemories(tagged.memories || []);
        setUntagged(ut.memories || []);
        setCategories(tagged.categories || []);
      } catch (e) {
        if (!cancelled) toast.error("Could not load memories.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [orgId, showArchived]);

  const pickOrg = (id) => {
    setOrgId(id);
    if (id) localStorage.setItem(STORAGE_KEY, id);
  };

  const visible = filter === "all"
    ? memories
    : memories.filter((m) => m.category === filter);

  const currentOrgName =
    orgs.find((o) => o.id === orgId)?.name || "this organization";

  return (
    <section className="mt-8" data-testid="memory-section">
      <div className="text-[10px] uppercase tracking-widest sp-subtle mb-2">
        Memory
      </div>
      <h2 className="text-xl font-medium tracking-tight mb-1 flex items-center gap-2">
        <Brain className="w-4 h-4" /> What agents remember about you
      </h2>
      <p className="text-sm sp-subtle mb-4 max-w-xl">
        Foundry quietly captures durable facts from your chats so every agent
        stays on-brand. Each memory is scoped to one organization — facts from{" "}
        <em>Client A</em> never leak into <em>Client B</em>'s agent replies.
      </p>

      {orgs.length === 0 ? (
        <div className="sp-glass rounded-2xl p-5 text-sm sp-subtle" data-testid="memory-no-orgs">
          Join or create an organization to start using Memory.
        </div>
      ) : (
        <>
          <OrgPicker orgs={orgs} value={orgId} onChange={pickOrg} />

          {untagged.length > 0 && (
            <UntaggedMigrationBanner
              untagged={untagged}
              orgs={orgs}
              activeOrgId={orgId}
              onUntaggedChange={setUntagged}
              onMigratedToActive={(memory) => {
                // If a memory was assigned to the currently-active org,
                // splice it into the active list so the user sees the
                // result immediately.
                setMemories((prev) => [memory, ...prev]);
              }}
            />
          )}

          <AddMemoryForm
            orgId={orgId}
            orgName={currentOrgName}
            categories={categories}
            onAdded={(m) => setMemories((prev) => [m, ...prev])}
          />

          <div className="flex flex-wrap gap-2 mt-5 mb-3 items-center" data-testid="memory-filters">
            <FilterChip
              active={filter === "all"}
              onClick={() => setFilter("all")}
              label={`All (${memories.length})`}
              testId="memory-filter-all"
            />
            {categories.map((cat) => {
              const n = memories.filter((m) => m.category === cat).length;
              if (n === 0) return null;
              return (
                <FilterChip
                  key={cat}
                  active={filter === cat}
                  onClick={() => setFilter(cat)}
                  label={`${CATEGORY_LABELS[cat] || cat} (${n})`}
                  testId={`memory-filter-${cat}`}
                />
              );
            })}
            <label className="ml-auto text-xs sp-subtle flex items-center gap-2 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={showArchived}
                onChange={(e) => setShowArchived(e.target.checked)}
                className="rounded"
                data-testid="memory-show-archived"
              />
              Show archived
            </label>
          </div>

          {loading ? (
            <div className="sp-glass rounded-2xl p-6 text-center text-sm sp-subtle flex items-center justify-center gap-2">
              <Loader2 className="w-4 h-4 animate-spin" /> Loading memories…
            </div>
          ) : visible.length === 0 ? (
            <div
              className="sp-glass rounded-2xl p-6 text-sm sp-subtle text-center"
              data-testid="memory-empty"
            >
              {memories.length === 0
                ? `No memories yet for ${currentOrgName}. Chat with an agent in this workspace or add one manually above.`
                : "No memories in this category."}
            </div>
          ) : (
            <ul className="space-y-2" data-testid="memory-list">
              {visible.map((m) => (
                <MemoryRow
                  key={m.id}
                  memory={m}
                  categories={categories}
                  orgs={orgs}
                  onUpdated={(updated) =>
                    setMemories((prev) =>
                      prev.map((x) => (x.id === updated.id ? { ...x, ...updated } : x))
                    )
                  }
                  onMovedOut={(id) =>
                    // Memory was reassigned to a different org — drop
                    // it from the current view.
                    setMemories((prev) => prev.filter((x) => x.id !== id))
                  }
                  onDeleted={(id) =>
                    setMemories((prev) => prev.filter((x) => x.id !== id))
                  }
                />
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}

function OrgPicker({ orgs, value, onChange }) {
  return (
    <div className="sp-glass-strong rounded-2xl p-4 mb-4 flex flex-wrap items-center gap-3" data-testid="memory-org-picker">
      <label className="text-[10px] uppercase tracking-widest sp-subtle">
        Viewing memory for
      </label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="sp-input text-sm py-1.5 min-w-[12rem]"
        data-testid="memory-org-select"
      >
        {orgs.map((o) => (
          <option key={o.id} value={o.id}>
            {o.name}
          </option>
        ))}
      </select>
      <span className="text-[11px] sp-subtle">
        Strict isolation — memories never cross between organizations.
      </span>
    </div>
  );
}

function UntaggedMigrationBanner({ untagged, orgs, activeOrgId, onUntaggedChange, onMigratedToActive }) {
  return (
    <div
      className="rounded-2xl p-4 mb-4 border border-amber-500/40 bg-amber-500/10"
      data-testid="memory-untagged-banner"
    >
      <div className="flex items-start gap-3">
        <AlertCircle className="w-4 h-4 text-amber-400 mt-0.5 shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-amber-200">
            {untagged.length} memor{untagged.length === 1 ? "y" : "ies"} need{untagged.length === 1 ? "s" : ""} an
            organization
          </div>
          <p className="text-xs text-amber-200/80 mt-0.5">
            These were captured before we enforced strict org isolation.
            Untagged memories are <strong>not</strong> injected into any agent
            reply until you assign or delete them.
          </p>
        </div>
      </div>
      <ul className="mt-3 space-y-2" data-testid="memory-untagged-list">
        {untagged.map((m) => (
          <UntaggedRow
            key={m.id}
            memory={m}
            orgs={orgs}
            activeOrgId={activeOrgId}
            onAssigned={(targetOrgId) => {
              const updated = { ...m, org_id: targetOrgId };
              onUntaggedChange(untagged.filter((x) => x.id !== m.id));
              if (targetOrgId === activeOrgId) onMigratedToActive(updated);
            }}
            onDeleted={() =>
              onUntaggedChange(untagged.filter((x) => x.id !== m.id))
            }
          />
        ))}
      </ul>
    </div>
  );
}

function UntaggedRow({ memory, orgs, activeOrgId, onAssigned, onDeleted }) {
  const [target, setTarget] = useState(activeOrgId || (orgs[0]?.id ?? ""));
  const [busy, setBusy] = useState(false);

  const assign = async () => {
    if (!target) return;
    setBusy(true);
    try {
      await api.patch(`/memory/${memory.id}`, { org_id: target });
      const orgName = orgs.find((o) => o.id === target)?.name || "that org";
      toast.success(`Assigned to ${orgName}.`);
      onAssigned(target);
    } catch (err) {
      const msg = err?.response?.data?.detail || "Could not assign memory.";
      toast.error(msg);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!window.confirm("Delete this untagged memory permanently?")) return;
    setBusy(true);
    try {
      await api.delete(`/memory/${memory.id}`);
      onDeleted();
    } catch (err) {
      toast.error("Could not delete memory.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="rounded-xl p-3 bg-black/20 flex flex-wrap items-start gap-3" data-testid={`memory-untagged-row-${memory.id}`}>
      <div className="flex-1 min-w-0">
        <div className="text-sm leading-relaxed">{memory.content}</div>
        <div className="text-[10px] uppercase tracking-wider sp-subtle mt-1">
          {CATEGORY_LABELS[memory.category] || memory.category} · {memory.importance}
        </div>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <select
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          className="sp-input text-xs py-1"
          data-testid={`memory-untagged-target-${memory.id}`}
        >
          {orgs.map((o) => (
            <option key={o.id} value={o.id}>{o.name}</option>
          ))}
        </select>
        <button
          type="button"
          onClick={assign}
          disabled={busy || !target}
          className="sp-btn sp-btn-primary text-xs inline-flex items-center gap-1"
          data-testid={`memory-untagged-assign-${memory.id}`}
        >
          {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <ArrowRight className="w-3.5 h-3.5" />}
          Assign
        </button>
        <button
          type="button"
          onClick={remove}
          disabled={busy}
          className="p-1.5 rounded-md hover:bg-red-500/15 sp-subtle hover:text-red-300 transition"
          title="Delete permanently"
          data-testid={`memory-untagged-delete-${memory.id}`}
        >
          <Trash2 className="w-3.5 h-3.5" />
        </button>
      </div>
    </li>
  );
}

function FilterChip({ active, onClick, label, testId }) {
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid={testId}
      className={`text-[11px] px-2.5 py-1 rounded-full border transition ${
        active
          ? "bg-orange-500/20 text-orange-200 border-orange-500/40"
          : "bg-white/5 sp-subtle border-white/10 hover:bg-white/10"
      }`}
    >
      {label}
    </button>
  );
}

function AddMemoryForm({ orgId, orgName, categories, onAdded }) {
  const [content, setContent] = useState("");
  const [category, setCategory] = useState("preferences");
  const [importance, setImportance] = useState("medium");
  const [saving, setSaving] = useState(false);
  const [open, setOpen] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (!content.trim() || !orgId) return;
    setSaving(true);
    try {
      const { data } = await api.post("/memory", {
        content: content.trim(),
        org_id: orgId,
        category,
        importance,
      });
      onAdded(data);
      setContent("");
      setOpen(false);
      toast.success(`Memory added to ${orgName}.`);
    } catch (err) {
      const msg = err?.response?.data?.detail || "Could not add memory.";
      toast.error(msg);
    } finally {
      setSaving(false);
    }
  };

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="sp-btn sp-btn-secondary text-sm inline-flex items-center gap-2"
        data-testid="memory-add-toggle"
      >
        <Plus className="w-4 h-4" /> Add a memory to {orgName}
      </button>
    );
  }

  return (
    <form
      onSubmit={submit}
      className="sp-glass-strong rounded-2xl p-4 space-y-3"
      data-testid="memory-add-form"
    >
      <div className="text-[10px] uppercase tracking-widest sp-subtle">
        Adding to: <span className="text-orange-300">{orgName}</span>
      </div>
      <textarea
        value={content}
        onChange={(e) => setContent(e.target.value)}
        className="sp-input w-full text-sm"
        rows={3}
        placeholder="e.g. We never use the words 'unleash' or 'supercharge' in marketing copy."
        autoFocus
        data-testid="memory-add-content"
      />
      <div className="flex flex-wrap gap-3 items-center">
        <label className="text-xs sp-subtle flex items-center gap-2">
          Category
          <select
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            className="sp-input text-xs py-1"
            data-testid="memory-add-category"
          >
            {categories.map((c) => (
              <option key={c} value={c}>
                {CATEGORY_LABELS[c] || c}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs sp-subtle flex items-center gap-2">
          Importance
          <select
            value={importance}
            onChange={(e) => setImportance(e.target.value)}
            className="sp-input text-xs py-1"
            data-testid="memory-add-importance"
          >
            <option value="low">Low</option>
            <option value="medium">Medium</option>
            <option value="high">High</option>
          </select>
        </label>
        <div className="ml-auto flex gap-2">
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              setContent("");
            }}
            className="sp-btn sp-btn-ghost text-xs"
            data-testid="memory-add-cancel"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={saving || !content.trim() || !orgId}
            className="sp-btn sp-btn-primary text-xs inline-flex items-center gap-1.5"
            data-testid="memory-add-submit"
          >
            {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
            Save memory
          </button>
        </div>
      </div>
    </form>
  );
}

function MemoryRow({ memory, categories, orgs, onUpdated, onMovedOut, onDeleted }) {
  const [editing, setEditing] = useState(false);
  const [content, setContent] = useState(memory.content);
  const [category, setCategory] = useState(memory.category);
  const [importance, setImportance] = useState(memory.importance);
  const [targetOrg, setTargetOrg] = useState(memory.org_id || "");
  const [saving, setSaving] = useState(false);

  const save = async () => {
    if (!content.trim()) return;
    setSaving(true);
    try {
      const payload = {
        content: content.trim(),
        category,
        importance,
      };
      const movingOrg = targetOrg && targetOrg !== memory.org_id;
      if (movingOrg) payload.org_id = targetOrg;
      await api.patch(`/memory/${memory.id}`, payload);
      if (movingOrg) {
        toast.success("Memory moved to a different organization.");
        onMovedOut(memory.id);
      } else {
        onUpdated({
          ...memory,
          content: content.trim(),
          category,
          importance,
        });
        toast.success("Memory updated.");
      }
      setEditing(false);
    } catch (err) {
      const msg = err?.response?.data?.detail || "Could not update memory.";
      toast.error(msg);
    } finally {
      setSaving(false);
    }
  };

  const archive = async () => {
    try {
      await api.patch(`/memory/${memory.id}`, {
        is_archived: !memory.is_archived,
      });
      onUpdated({ ...memory, is_archived: !memory.is_archived });
      toast.success(memory.is_archived ? "Memory restored." : "Memory archived.");
    } catch (err) {
      toast.error("Could not archive memory.");
    }
  };

  const remove = async () => {
    if (!window.confirm("Permanently delete this memory? Agents will no longer use it.")) return;
    try {
      await api.delete(`/memory/${memory.id}`);
      onDeleted(memory.id);
      toast.success("Memory deleted.");
    } catch (err) {
      toast.error("Could not delete memory.");
    }
  };

  if (editing) {
    return (
      <li className="sp-glass-strong rounded-xl p-3 space-y-2" data-testid={`memory-row-${memory.id}`}>
        <textarea
          value={content}
          onChange={(e) => setContent(e.target.value)}
          className="sp-input w-full text-sm"
          rows={2}
          data-testid={`memory-edit-content-${memory.id}`}
        />
        <div className="flex flex-wrap gap-2 items-center">
          <select
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            className="sp-input text-xs py-1"
          >
            {categories.map((c) => (
              <option key={c} value={c}>
                {CATEGORY_LABELS[c] || c}
              </option>
            ))}
          </select>
          <select
            value={importance}
            onChange={(e) => setImportance(e.target.value)}
            className="sp-input text-xs py-1"
          >
            <option value="low">Low</option>
            <option value="medium">Medium</option>
            <option value="high">High</option>
          </select>
          {orgs.length > 1 && (
            <select
              value={targetOrg}
              onChange={(e) => setTargetOrg(e.target.value)}
              className="sp-input text-xs py-1"
              title="Move this memory to a different organization"
              data-testid={`memory-edit-org-${memory.id}`}
            >
              {orgs.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name}{o.id === memory.org_id ? " (current)" : ""}
                </option>
              ))}
            </select>
          )}
          <div className="ml-auto flex gap-2">
            <button
              type="button"
              onClick={() => {
                setEditing(false);
                setContent(memory.content);
                setCategory(memory.category);
                setImportance(memory.importance);
                setTargetOrg(memory.org_id || "");
              }}
              className="sp-btn sp-btn-ghost text-xs inline-flex items-center gap-1"
            >
              <X className="w-3.5 h-3.5" /> Cancel
            </button>
            <button
              type="button"
              onClick={save}
              disabled={saving || !content.trim()}
              className="sp-btn sp-btn-primary text-xs inline-flex items-center gap-1"
              data-testid={`memory-edit-save-${memory.id}`}
            >
              {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
              Save
            </button>
          </div>
        </div>
      </li>
    );
  }

  return (
    <li
      className={`sp-glass rounded-xl p-3 flex items-start gap-3 ${memory.is_archived ? "opacity-60" : ""}`}
      data-testid={`memory-row-${memory.id}`}
    >
      <div className="flex-1 min-w-0">
        <div className="text-sm leading-relaxed">{memory.content}</div>
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[10px] uppercase tracking-wider">
          <span className="sp-subtle">{CATEGORY_LABELS[memory.category] || memory.category}</span>
          <span className="sp-subtle">·</span>
          <span className={`px-1.5 py-0.5 rounded border ${IMPORTANCE_COLORS[memory.importance] || ""}`}>
            {memory.importance}
          </span>
          <span className="sp-subtle">·</span>
          <span className="sp-subtle">
            {memory.source === "extracted" ? "auto-captured" : "manual"}
          </span>
          {memory.is_archived && (
            <>
              <span className="sp-subtle">·</span>
              <span className="text-zinc-400">archived</span>
            </>
          )}
        </div>
      </div>
      <div className="flex gap-1 shrink-0">
        <button
          type="button"
          onClick={() => setEditing(true)}
          className="p-1.5 rounded-md hover:bg-white/10 sp-subtle hover:text-white transition"
          title="Edit"
          data-testid={`memory-edit-${memory.id}`}
        >
          <Pencil className="w-3.5 h-3.5" />
        </button>
        <button
          type="button"
          onClick={archive}
          className="p-1.5 rounded-md hover:bg-white/10 sp-subtle hover:text-white transition"
          title={memory.is_archived ? "Restore" : "Archive"}
          data-testid={`memory-archive-${memory.id}`}
        >
          {memory.is_archived ? <ArchiveRestore className="w-3.5 h-3.5" /> : <Archive className="w-3.5 h-3.5" />}
        </button>
        <button
          type="button"
          onClick={remove}
          className="p-1.5 rounded-md hover:bg-red-500/15 sp-subtle hover:text-red-300 transition"
          title="Delete permanently"
          data-testid={`memory-delete-${memory.id}`}
        >
          <Trash2 className="w-3.5 h-3.5" />
        </button>
      </div>
    </li>
  );
}
