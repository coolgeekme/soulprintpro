import React, { useEffect, useMemo, useState } from "react";
import api from "../lib/api";
import { toast } from "sonner";
import { Plus, Trash2, Play, Pause, Clock, X as XIcon, Save, Calendar, Repeat } from "lucide-react";

const fmt = (iso) => {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
};

const userTimezone = () => {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch { return "UTC"; }
};

// ── Friendly presets — order matters, top items are most-common ───────────
const PRESETS = [
  { label: "Every weekday at 9:00 AM",        cron: "0 9 * * 1-5" },
  { label: "Every Monday at 9:00 AM",         cron: "0 9 * * 1" },
  { label: "Every day at 9:00 AM",            cron: "0 9 * * *" },
  { label: "Every Friday at 5:00 PM",         cron: "0 17 * * 5" },
  { label: "Every hour, on the hour",         cron: "0 * * * *" },
  { label: "Every 30 minutes",                cron: "*/30 * * * *" },
  { label: "1st of every month at 9:00 AM",   cron: "0 9 1 * *" },
  { label: "Last weekday of the month, 5pm",  cron: "0 17 28-31 * 1-5" },
];

const WEEKDAYS = [
  { idx: 1, short: "Mon" },
  { idx: 2, short: "Tue" },
  { idx: 3, short: "Wed" },
  { idx: 4, short: "Thu" },
  { idx: 5, short: "Fri" },
  { idx: 6, short: "Sat" },
  { idx: 0, short: "Sun" },
];

// Compose a 5-field cron string from the friendly builder state.
const buildCron = ({ frequency, time, weekdays, monthDay }) => {
  const [hh, mm] = (time || "09:00").split(":").map((s) => parseInt(s, 10));
  const M = isNaN(mm) ? 0 : mm;
  const H = isNaN(hh) ? 9 : hh;
  if (frequency === "daily") return `${M} ${H} * * *`;
  if (frequency === "monthly") {
    const d = Math.max(1, Math.min(28, parseInt(monthDay, 10) || 1));
    return `${M} ${H} ${d} * *`;
  }
  if (frequency === "weekly") {
    const days = (weekdays || []).slice().sort((a, b) => a - b);
    if (!days.length) return `${M} ${H} * * 1`; // default Mon
    return `${M} ${H} * * ${days.join(",")}`;
  }
  if (frequency === "hourly") return `${M} * * * *`;
  return `${M} ${H} * * *`;
};

// Reverse a cron expression into a plain-English summary so the user can
// double-check what they're saving. Keeps it simple — falls back to "Custom
// schedule" for anything we don't recognise.
const cronToHuman = (cron) => {
  if (!cron) return "Custom schedule";
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return "Custom schedule";
  const [m, h, dom, mon, dow] = parts;
  const ampm = (H) => {
    const n = parseInt(H, 10);
    if (isNaN(n)) return H;
    const period = n >= 12 ? "PM" : "AM";
    const display = ((n + 11) % 12) + 1;
    return `${display}:${(parseInt(m, 10) || 0).toString().padStart(2, "0")} ${period}`;
  };
  if (m.startsWith("*/")) return `Every ${m.slice(2)} minutes`;
  if (h === "*" && m !== "*" && !m.startsWith("*/")) return `Every hour at minute ${m}`;
  // Weekly
  if (dom === "*" && mon === "*" && dow !== "*") {
    const lookup = { 0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat" };
    if (dow === "1-5") return `Every weekday at ${ampm(h)}`;
    if (dow === "0,6" || dow === "6,0") return `Every weekend at ${ampm(h)}`;
    const days = dow.split(",").map((d) => lookup[d.trim()] || d).join(", ");
    return `Every ${days} at ${ampm(h)}`;
  }
  // Daily
  if (dom === "*" && mon === "*" && dow === "*") return `Every day at ${ampm(h)}`;
  // Monthly on a specific day
  if (/^\d+$/.test(dom) && mon === "*" && dow === "*") {
    const d = parseInt(dom, 10);
    const suffix = d % 10 === 1 && d !== 11 ? "st" : d % 10 === 2 && d !== 12 ? "nd" : d % 10 === 3 && d !== 13 ? "rd" : "th";
    return `${d}${suffix} of every month at ${ampm(h)}`;
  }
  return `Custom schedule (${cron})`;
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
  // Friendly builder state. The actual cron string is computed from this on
  // submit. `customCron` holds a raw expression when the user picks "Custom"
  // or toggles Advanced mode.
  const [frequency, setFrequency] = useState("weekly"); // weekly | daily | monthly | hourly | custom
  const [scheduleTime, setScheduleTime] = useState("09:00");
  const [weekdays, setWeekdays] = useState([1]); // Mon by default
  const [monthDay, setMonthDay] = useState(1);
  const [customCron, setCustomCron] = useState("0 9 * * 1");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [runAt, setRunAt] = useState("");
  const [tz, setTz] = useState(userTimezone());
  const [saving, setSaving] = useState(false);

  // For org-scope selection — pick which conversation to post into
  const [pickedTargetId, setPickedTargetId] = useState("");
  const [pickedScope, setPickedScope] = useState(scope);

  // Compute the active cron expression — either built from the friendly UI
  // or the user's raw input when they're in Custom / Advanced mode.
  const computedCron = useMemo(() => {
    if (frequency === "custom" || showAdvanced) return customCron.trim();
    return buildCron({ frequency, time: scheduleTime, weekdays, monthDay });
  }, [frequency, scheduleTime, weekdays, monthDay, customCron, showAdvanced]);

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
    setFrequency("weekly"); setScheduleTime("09:00"); setWeekdays([1]); setMonthDay(1);
    setCustomCron("0 9 * * 1"); setShowAdvanced(false);
    setRunAt(""); setTz(userTimezone());
    setPickedTargetId(""); setPickedScope(scope); setShowNew(false);
  };

  const create = async (e) => {
    e.preventDefault();
    if (!title.trim() || !prompt.trim()) { toast.error("Title + prompt required"); return; }
    if (mode === "once" && !runAt) { toast.error("Pick a date/time"); return; }
    if (mode === "cron" && !computedCron) { toast.error("Pick a frequency"); return; }
    if (mode === "cron" && computedCron.trim().split(/\s+/).length !== 5) {
      toast.error("Cron must have 5 space-separated fields"); return;
    }
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
      if (mode === "cron") body.cron = computedCron;
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
            <div className="space-y-3">
              {/* Quick presets — one click and you're done */}
              <div className="flex flex-wrap gap-1.5" data-testid="schedule-presets">
                {PRESETS.map((p) => (
                  <button
                    key={p.cron}
                    type="button"
                    onClick={() => { setFrequency("custom"); setCustomCron(p.cron); setShowAdvanced(false); }}
                    className={`sp-chip text-[11px] ${customCron === p.cron && frequency === "custom" ? "!bg-orange-500/20 !text-white !border-orange-400" : ""}`}
                    data-testid={`preset-${p.cron.replace(/[^a-z0-9]/gi, "-")}`}
                  >
                    {p.label}
                  </button>
                ))}
              </div>

              {/* Frequency picker — drives the day/time controls below */}
              <div className="grid grid-cols-2 sm:grid-cols-5 gap-1.5">
                {[
                  ["weekly",  "Weekly"],
                  ["daily",   "Daily"],
                  ["monthly", "Monthly"],
                  ["hourly",  "Hourly"],
                  ["custom",  "Custom"],
                ].map(([v, lbl]) => (
                  <button
                    key={v}
                    type="button"
                    onClick={() => { setFrequency(v); setShowAdvanced(false); }}
                    className={`sp-chip justify-center ${frequency === v ? "!bg-white/15 !text-white" : ""}`}
                    data-testid={`freq-${v}`}
                  >
                    {lbl}
                  </button>
                ))}
              </div>

              {/* Frequency-specific controls */}
              {frequency === "weekly" && (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs sp-subtle">On</span>
                  {WEEKDAYS.map((d) => {
                    const on = weekdays.includes(d.idx);
                    return (
                      <button
                        key={d.idx}
                        type="button"
                        onClick={() => setWeekdays((prev) => on ? prev.filter((x) => x !== d.idx) : [...prev, d.idx])}
                        className={`px-3 py-1.5 rounded-full text-[11px] border transition ${on ? "bg-orange-500/20 border-orange-400 text-white" : "border-white/15 sp-subtle hover:text-white"}`}
                        data-testid={`weekday-${d.short.toLowerCase()}`}
                      >
                        {d.short}
                      </button>
                    );
                  })}
                </div>
              )}

              {frequency === "monthly" && (
                <div className="flex items-center gap-2 text-xs sp-subtle">
                  On day
                  <input
                    type="number" min={1} max={28}
                    value={monthDay}
                    onChange={(e) => setMonthDay(Math.max(1, Math.min(28, parseInt(e.target.value, 10) || 1)))}
                    className="w-16 rounded-lg px-2 py-1.5 text-sm"
                    style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
                    data-testid="month-day"
                  />
                  of every month
                </div>
              )}

              {(frequency === "weekly" || frequency === "daily" || frequency === "monthly") && (
                <div className="flex items-center gap-3 text-xs sp-subtle flex-wrap">
                  <label className="flex items-center gap-2">
                    At
                    <input
                      type="time"
                      value={scheduleTime}
                      onChange={(e) => setScheduleTime(e.target.value)}
                      className="rounded-lg px-2 py-1.5 text-sm"
                      style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
                      data-testid="schedule-time"
                    />
                  </label>
                  <label className="flex items-center gap-2">
                    in
                    <input
                      value={tz}
                      onChange={(e) => setTz(e.target.value)}
                      placeholder="Timezone (auto)"
                      className="rounded-lg px-2 py-1.5 text-sm w-44"
                      style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
                      data-testid="schedule-tz"
                    />
                  </label>
                </div>
              )}

              {frequency === "hourly" && (
                <div className="flex items-center gap-2 text-xs sp-subtle">
                  At minute
                  <input
                    type="number" min={0} max={59}
                    value={parseInt(scheduleTime.split(":")[1] || "0", 10)}
                    onChange={(e) => {
                      const mm = Math.max(0, Math.min(59, parseInt(e.target.value, 10) || 0));
                      setScheduleTime(`00:${mm.toString().padStart(2, "0")}`);
                    }}
                    className="w-16 rounded-lg px-2 py-1.5 text-sm"
                    style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
                    data-testid="hourly-minute"
                  />
                  of every hour
                </div>
              )}

              {/* Advanced / custom cron — power users only */}
              {(frequency === "custom" || showAdvanced) && (
                <div className="space-y-1">
                  <input
                    value={customCron}
                    onChange={(e) => { setCustomCron(e.target.value); setFrequency("custom"); }}
                    placeholder="Cron e.g. '0 9 * * 1' = every Mon 09:00"
                    className="w-full rounded-lg px-3 py-2 text-sm font-mono focus:sp-focus"
                    style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
                    data-testid="schedule-cron"
                  />
                  <div className="text-[11px] sp-subtle">
                    Format: <code>minute hour day-of-month month day-of-week</code>. Use <code>*</code> for "every", <code>*/N</code> for "every N", <code>1-5</code> for ranges.
                  </div>
                </div>
              )}

              {/* Live human-readable preview + advanced toggle */}
              <div className="flex items-center justify-between gap-3 text-xs flex-wrap">
                <div className="flex items-center gap-2">
                  <Repeat className="w-3.5 h-3.5 text-orange-400" />
                  <span style={{ color: "var(--fg)" }} data-testid="schedule-preview">
                    {cronToHuman(computedCron)} <span className="sp-subtle">({tz})</span>
                  </span>
                </div>
                {frequency !== "custom" && (
                  <button
                    type="button"
                    onClick={() => {
                      // When toggling on, copy the friendly-built cron into
                      // the raw field so the user sees what they had — and
                      // can tweak it without losing context.
                      if (!showAdvanced) setCustomCron(buildCron({ frequency, time: scheduleTime, weekdays, monthDay }));
                      setShowAdvanced((v) => !v);
                    }}
                    className="sp-subtle hover:text-white text-[11px]"
                    data-testid="toggle-advanced-cron"
                  >
                    {showAdvanced ? "Hide raw cron" : "Show raw cron"}
                  </button>
                )}
              </div>
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
                  {s.cron && (
                    <span title={s.cron}>
                      <Repeat className="w-3 h-3 inline-block mr-1" />
                      {cronToHuman(s.cron)} <span className="opacity-70">({s.timezone})</span>
                    </span>
                  )}
                  {s.run_at && <span><Calendar className="w-3 h-3 inline-block mr-1" /> once · {fmt(s.run_at)}</span>}
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
