import React, { useEffect, useState } from "react";
import api from "../lib/api";
import { toast } from "sonner";
import { Plus, Trash2, Play, Pause, Clock, X as XIcon, Save } from "lucide-react";

const fmt = (iso) => {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
};

const userTimezone = () => {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch { return "UTC"; }
};

/**
 * Schedules panel — works at three scopes:
 *   - conversation: shown inside ConversationPage
 *   - team:         shown on TeamPage
 *   - org:          shown on OrgPage / dashboard (lists everything)
 *
 * Props: scope, targetId, orgId (for org scope only).
 */
export default function SchedulesPanel({ scope = "conversation", targetId, orgId, conversationOptions = [] }) {
  const [list, setList] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showNew, setShowNew] = useState(false);
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [mode, setMode] = useState("cron"); // 'cron' | 'once'
  const [cron, setCron] = useState("0 9 * * 1");  // every Monday 09:00
  const [runAt, setRunAt] = useState("");
  const [tz, setTz] = useState(userTimezone());
  const [saving, setSaving] = useState(false);

  // For org-scope selection — pick which conversation to post into
  const [pickedTargetId, setPickedTargetId] = useState("");
  const [pickedScope, setPickedScope] = useState(scope);

  const load = async () => {
    setLoading(true);
    try {
      const params = scope === "org"
        ? { org_id: orgId }
        : { scope, target_id: targetId };
      const { data } = await api.get("/schedules", { params });
      setList(data);
    } catch (err) {
      toast.error("Failed to load schedules");
    } finally { setLoading(false); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [scope, targetId, orgId]);

  const reset = () => {
    setTitle(""); setPrompt(""); setMode("cron");
    setCron("0 9 * * 1"); setRunAt(""); setTz(userTimezone());
    setPickedTargetId(""); setPickedScope(scope); setShowNew(false);
  };

  const create = async (e) => {
    e.preventDefault();
    if (!title.trim() || !prompt.trim()) { toast.error("Title + prompt required"); return; }
    if (mode === "once" && !runAt) { toast.error("Pick a date/time"); return; }
    if (mode === "cron" && !cron.trim()) { toast.error("Enter a cron expression"); return; }
    const finalScope = scope === "org" ? (pickedScope || "conversation") : scope;
    const finalTargetId = scope === "org" ? (pickedTargetId || orgId) : targetId;
    if (!finalTargetId) { toast.error("Pick a target"); return; }
    setSaving(true);
    try {
      const body = {
        title: title.trim(),
        prompt: prompt.trim(),
        scope: finalScope,
        target_id: finalTargetId,
        timezone: tz,
        active: true,
      };
      if (mode === "cron") body.cron = cron.trim();
      else body.run_at = new Date(runAt).toISOString();
      const { data } = await api.post("/schedules", body);
      setList((prev) => [data, ...prev]);
      reset();
      toast.success("Schedule created");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Create failed");
    } finally { setSaving(false); }
  };

  const toggle = async (s) => {
    try {
      const { data } = await api.patch(`/schedules/${s.id}`, { active: !s.active });
      setList((prev) => prev.map((x) => x.id === s.id ? data : x));
    } catch (err) { toast.error("Toggle failed"); }
  };

  const remove = async (s) => {
    if (!window.confirm(`Delete schedule "${s.title}"?`)) return;
    try {
      await api.delete(`/schedules/${s.id}`);
      setList((prev) => prev.filter((x) => x.id !== s.id));
    } catch (err) { toast.error("Delete failed"); }
  };

  const runNow = async (s) => {
    try {
      await api.post(`/schedules/${s.id}/run-now`);
      toast.success("Running now — replies will appear in the chat");
    } catch (err) { toast.error("Run failed"); }
  };

  return (
    <section className="sp-glass-strong rounded-2xl p-6" data-testid="schedules-panel">
      <div className="flex items-center justify-between mb-4 flex-wrap gap-3">
        <div className="flex items-center gap-2">
          <Clock className="w-4 h-4" />
          <h3 className="text-sm uppercase tracking-widest sp-subtle">Scheduled tasks</h3>
          <span className="sp-chip">{list.length}</span>
        </div>
        {!showNew && (
          <button onClick={() => setShowNew(true)} className="sp-btn sp-btn-primary text-xs" data-testid="new-schedule-btn">
            <Plus className="w-3.5 h-3.5" /> New schedule
          </button>
        )}
      </div>

      {showNew && (
        <form onSubmit={create} className="sp-glass rounded-xl p-4 mb-4 space-y-3" data-testid="new-schedule-form">
          <div className="flex items-center justify-between">
            <span className="text-xs uppercase tracking-widest sp-subtle">New schedule</span>
            <button type="button" onClick={reset} className="text-white/60 hover:text-white"><XIcon className="w-4 h-4" /></button>
          </div>
          <input
            data-testid="schedule-title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Title (e.g. Weekly digest)"
            className="w-full rounded-lg px-3 py-2 text-sm focus:sp-focus"
            style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
          />
          <textarea
            data-testid="schedule-prompt"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="What should the agent do? (e.g. 'Summarise this week's product priorities and post 3 LinkedIn hooks.')"
            rows={3}
            className="w-full rounded-lg px-3 py-2 text-sm resize-none focus:sp-focus"
            style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
          />

          {scope === "org" && (
            <div className="grid sm:grid-cols-2 gap-2">
              <select
                value={pickedScope}
                onChange={(e) => { setPickedScope(e.target.value); setPickedTargetId(""); }}
                className="rounded-lg px-3 py-2 text-sm"
                style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
                data-testid="schedule-scope"
              >
                <option value="conversation">A specific conversation</option>
                <option value="org">The org General chat</option>
              </select>
              {pickedScope === "conversation" && (
                <select
                  value={pickedTargetId}
                  onChange={(e) => setPickedTargetId(e.target.value)}
                  className="rounded-lg px-3 py-2 text-sm"
                  style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
                  data-testid="schedule-target-conv"
                >
                  <option value="">— Pick a conversation —</option>
                  {conversationOptions.map((c) => (
                    <option key={c.id} value={c.id}>{c.title || "Untitled"} ({c.team_name || ""})</option>
                  ))}
                </select>
              )}
            </div>
          )}

          <div className="flex items-center gap-2 text-xs">
            <button type="button" onClick={() => setMode("cron")} className={`sp-chip ${mode === "cron" ? "!bg-white/15 !text-white" : ""}`} data-testid="mode-cron">Recurring (cron)</button>
            <button type="button" onClick={() => setMode("once")} className={`sp-chip ${mode === "once" ? "!bg-white/15 !text-white" : ""}`} data-testid="mode-once">Run once</button>
          </div>

          {mode === "cron" ? (
            <div className="grid sm:grid-cols-2 gap-2">
              <input
                value={cron}
                onChange={(e) => setCron(e.target.value)}
                placeholder="Cron e.g. '0 9 * * 1' = every Mon 09:00"
                className="rounded-lg px-3 py-2 text-sm font-mono focus:sp-focus"
                style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
                data-testid="schedule-cron"
              />
              <input
                value={tz}
                onChange={(e) => setTz(e.target.value)}
                placeholder="Timezone (auto-detected)"
                className="rounded-lg px-3 py-2 text-sm focus:sp-focus"
                style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
                data-testid="schedule-tz"
              />
            </div>
          ) : (
            <div className="grid sm:grid-cols-2 gap-2">
              <input
                type="datetime-local"
                value={runAt}
                onChange={(e) => setRunAt(e.target.value)}
                className="rounded-lg px-3 py-2 text-sm focus:sp-focus"
                style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
                data-testid="schedule-run-at"
              />
              <input
                value={tz}
                onChange={(e) => setTz(e.target.value)}
                placeholder="Timezone"
                className="rounded-lg px-3 py-2 text-sm focus:sp-focus"
                style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
                data-testid="schedule-tz"
              />
            </div>
          )}
          <div className="flex items-center justify-between text-[11px] sp-subtle">
            <span>
              Cron format: minute hour day-of-month month day-of-week.
              Examples: <code>0 9 * * 1</code> = Mondays 9am · <code>*/30 * * * *</code> = every 30 min · <code>0 18 1 * *</code> = 1st of month 6pm.
            </span>
          </div>
          <div className="flex items-center justify-end gap-2">
            <button type="button" onClick={reset} className="sp-btn sp-btn-ghost text-xs">Cancel</button>
            <button type="submit" disabled={saving} className="sp-btn sp-btn-primary text-xs disabled:opacity-50" data-testid="save-schedule-btn">
              <Save className="w-3.5 h-3.5" /> {saving ? "Saving…" : "Create schedule"}
            </button>
          </div>
        </form>
      )}

      {loading ? (
        <div className="text-xs sp-subtle">Loading…</div>
      ) : list.length === 0 ? (
        <div className="text-xs sp-subtle">No schedules yet — automate a recurring digest, weekly research, monthly report, or one-shot reminder.</div>
      ) : (
        <ul className="flex flex-col gap-2">
          {list.map((s) => (
            <li key={s.id} className="sp-glass rounded-xl p-3 flex items-start gap-3" data-testid={`schedule-${s.id}`}>
              <div className={`w-2 h-2 rounded-full mt-2 shrink-0 ${s.active ? "bg-emerald-400" : "bg-white/30"}`} />
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium truncate">{s.title}</div>
                <div className="text-[11px] sp-subtle line-clamp-2">{s.prompt}</div>
                <div className="text-[11px] sp-subtle mt-1 flex flex-wrap gap-x-3 gap-y-1">
                  {s.cron && <span>⏱ <code>{s.cron}</code> ({s.timezone})</span>}
                  {s.run_at && <span>📅 once · {fmt(s.run_at)}</span>}
                  {s.next_run_at && <span>next: {fmt(s.next_run_at)}</span>}
                  {s.last_run_at && <span>last: {fmt(s.last_run_at)} {s.last_status && `(${s.last_status})`}</span>}
                </div>
              </div>
              <div className="flex items-center gap-1 shrink-0">
                <button onClick={() => runNow(s)} className="sp-btn sp-btn-ghost !p-1.5" title="Run now" data-testid={`run-now-${s.id}`}>
                  <Play className="w-3.5 h-3.5" />
                </button>
                <button onClick={() => toggle(s)} className="sp-btn sp-btn-ghost !p-1.5" title={s.active ? "Pause" : "Resume"} data-testid={`toggle-${s.id}`}>
                  {s.active ? <Pause className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
                </button>
                <button onClick={() => remove(s)} className="sp-btn sp-btn-ghost !p-1.5 hover:!text-red-300" title="Delete" data-testid={`delete-${s.id}`}>
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
