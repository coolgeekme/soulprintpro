import React, { useEffect, useState } from "react";
import { toast } from "sonner";
import {
  Brain, Plus, Trash2, Pencil, Save, X, Archive, ArchiveRestore, Loader2,
} from "lucide-react";
import api from "../lib/api";

/**
 * MemoryPanel — user-scoped long-term memory editor.
 *
 * Lets the signed-in user inspect, add, edit, archive, and purge the
 * durable facts agents remember about them across all conversations.
 *
 * Backed by /api/memory (see backend/memory_routes.py). Memories are
 * extracted automatically in the background after every agent reply,
 * but users can also seed them manually here on day one.
 *
 * Why a B2B platform ships this:
 *   • Transparency — buyers/security reviewers can see exactly what is
 *     stored.
 *   • Right-to-correct/delete (GDPR/CCPA) — one-click controls.
 *   • Faster onboarding — power users pre-seed brand voice / ICP /
 *     pricing preferences so all 169 agents are on-brand from message 1.
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

export default function MemoryPanel() {
  const [loading, setLoading] = useState(true);
  const [memories, setMemories] = useState([]);
  const [categories, setCategories] = useState([]);
  const [filter, setFilter] = useState("all");
  const [showArchived, setShowArchived] = useState(false);

  const load = async () => {
    try {
      const { data } = await api.get("/memory", {
        params: { include_archived: showArchived },
      });
      setMemories(data.memories || []);
      setCategories(data.categories || []);
    } catch (e) {
      toast.error("Could not load memories.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setLoading(true);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showArchived]);

  const visible = filter === "all"
    ? memories
    : memories.filter((m) => m.category === filter);

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
        stays on-brand. You own this list — review, edit, or purge anything.
        Add memories manually to onboard the platform faster.
      </p>

      <AddMemoryForm
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
            ? "No memories yet. Chat with an agent or add one manually above."
            : "No memories in this category."}
        </div>
      ) : (
        <ul className="space-y-2" data-testid="memory-list">
          {visible.map((m) => (
            <MemoryRow
              key={m.id}
              memory={m}
              categories={categories}
              onUpdated={(updated) =>
                setMemories((prev) =>
                  prev.map((x) => (x.id === updated.id ? { ...x, ...updated } : x))
                )
              }
              onDeleted={(id) =>
                setMemories((prev) => prev.filter((x) => x.id !== id))
              }
            />
          ))}
        </ul>
      )}
    </section>
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

function AddMemoryForm({ categories, onAdded }) {
  const [content, setContent] = useState("");
  const [category, setCategory] = useState("preferences");
  const [importance, setImportance] = useState("medium");
  const [saving, setSaving] = useState(false);
  const [open, setOpen] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (!content.trim()) return;
    setSaving(true);
    try {
      const { data } = await api.post("/memory", {
        content: content.trim(),
        category,
        importance,
      });
      onAdded(data);
      setContent("");
      setOpen(false);
      toast.success("Memory added.");
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
        <Plus className="w-4 h-4" /> Add a memory manually
      </button>
    );
  }

  return (
    <form
      onSubmit={submit}
      className="sp-glass-strong rounded-2xl p-4 space-y-3"
      data-testid="memory-add-form"
    >
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
            disabled={saving || !content.trim()}
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

function MemoryRow({ memory, categories, onUpdated, onDeleted }) {
  const [editing, setEditing] = useState(false);
  const [content, setContent] = useState(memory.content);
  const [category, setCategory] = useState(memory.category);
  const [importance, setImportance] = useState(memory.importance);
  const [saving, setSaving] = useState(false);

  const save = async () => {
    if (!content.trim()) return;
    setSaving(true);
    try {
      await api.patch(`/memory/${memory.id}`, {
        content: content.trim(),
        category,
        importance,
      });
      onUpdated({
        ...memory,
        content: content.trim(),
        category,
        importance,
      });
      setEditing(false);
      toast.success("Memory updated.");
    } catch (err) {
      toast.error("Could not update memory.");
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
          <div className="ml-auto flex gap-2">
            <button
              type="button"
              onClick={() => {
                setEditing(false);
                setContent(memory.content);
                setCategory(memory.category);
                setImportance(memory.importance);
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
