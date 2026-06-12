import React, { useEffect, useMemo, useRef, useState } from "react";
import { useParams, Link, useNavigate } from "react-router-dom";
import api, { BACKEND_URL, displayMissionGoal, downloadFile, errorMessage, getToken } from "../lib/api";
import { getCurrentIdToken, FIREBASE_ENABLED } from "../lib/firebase";
import TopBar from "../components/TopBar";
import BrowserStreamCanvas from "../components/BrowserStreamCanvas";
import MissionGoalDrafter from "../components/MissionGoalDrafter";
import AvailableMissionToolsPreview from "../components/AvailableMissionToolsPreview";
import { toast } from "sonner";
import {
  Rocket, Play, X, Loader2, ArrowLeft, Search, FileText, Wrench,
  ChevronRight, Check, AlertTriangle, HelpCircle, Send, Download, Image as ImageIcon,
  Trash2, CornerDownRight, UploadCloud, Lock, Flag,
} from "lucide-react";
import { parseServerDate } from "../lib/datetime";

/**
 * MissionsPage — list + detail view for autonomous agent runs ("Missions").
 *
 * Route shapes:
 *   /app/org/:orgId/missions              → list + "Start a mission" dialog
 *   /app/org/:orgId/missions/:missionId   → live log + artifacts + controls
 */
const STATUS_MAP = {
  running: { label: "Running", cls: "!bg-blue-400/15 !text-blue-200" },
  awaiting_user: { label: "Waiting for you", cls: "!bg-amber-400/15 !text-amber-200" },
  paused: { label: "Paused", cls: "!bg-white/10" },
  finished: { label: "Done", cls: "!bg-emerald-400/15 !text-emerald-200" },
  failed: { label: "Failed", cls: "!bg-red-400/15 !text-red-200" },
  cancelled: { label: "Cancelled", cls: "!bg-white/10 opacity-70" },
};

const relTime = (iso) => {
  const d = parseServerDate(iso);
  if (!d) return "";
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return d.toLocaleString();
};

export default function MissionsPage() {
  const { orgId, missionId } = useParams();
  if (missionId) return <MissionDetail orgId={orgId} missionId={missionId} />;
  return <MissionList orgId={orgId} />;
}

// ────────────────────────────────────────────────────────────────────────────
function MissionList({ orgId }) {
  const [missions, setMissions] = useState(null);
  const [startOpen, setStartOpen] = useState(false);
  const nav = useNavigate();

  const load = async () => {
    try {
      const { data } = await api.get(`/orgs/${orgId}/missions`);
      setMissions(data || []);
    } catch (err) {
      setMissions([]);
      toast.error(errorMessage(err, "Failed to load missions"));
    }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [orgId]);

  const startMission = async (goal, attachmentIds = []) => {
    try {
      const { data } = await api.post(`/orgs/${orgId}/missions`, {
        goal, attachment_ids: attachmentIds,
      });
      setStartOpen(false);
      nav(`/app/org/${orgId}/missions/${data.id}`);
    } catch (err) {
      toast.error(errorMessage(err, "Couldn't start mission"));
    }
  };

  const deleteMission = async (id, e) => {
    if (e) { e.preventDefault(); e.stopPropagation(); }
    if (!window.confirm("Delete this mission, its work log, and any artifacts it produced? This can't be undone.")) return;
    try {
      await api.delete(`/missions/${id}`);
      setMissions((prev) => (prev || []).filter((m) => m.id !== id));
      toast.success("Mission deleted");
    } catch (err) {
      toast.error(errorMessage(err, "Delete failed"));
    }
  };

  return (
    <div className="min-h-screen" style={{ background: "var(--bg)", color: "var(--fg)" }}>
      <TopBar />
      <main className="max-w-5xl mx-auto px-6 py-10" data-testid="missions-page">
        <div className="flex items-start justify-between mb-6 gap-3 flex-wrap">
          <div>
            <Link to={`/app/org/${orgId}`} className="text-xs sp-subtle hover:text-white inline-flex items-center gap-1 mb-2">
              <ArrowLeft className="w-3 h-3" /> Back to Org
            </Link>
            <h1 className="sp-display text-4xl flex items-center gap-3">
              <Rocket className="w-7 h-7 text-orange-400" />
              Missions
            </h1>
            <p className="sp-subtle text-sm mt-2 max-w-2xl">
              Kick off an autonomous run. The lead agent picks tools (search, read org docs, write artifacts, generate images) and ships real deliverables. You can pause, answer, or cancel anytime.
            </p>
          </div>
          <button
            className="sp-btn sp-btn-primary text-sm"
            onClick={() => setStartOpen(true)}
            data-testid="start-mission-btn"
          >
            <Play className="w-3.5 h-3.5" /> Start a mission
          </button>
        </div>

        {missions === null ? (
          <div className="sp-subtle text-sm py-8 text-center">Loading…</div>
        ) : missions.length === 0 ? (
          <div className="sp-glass rounded-2xl p-8 text-center">
            <Rocket className="w-6 h-6 mx-auto mb-2 text-white/70" />
            <div className="text-sm font-medium">No missions yet</div>
            <div className="text-xs sp-subtle mt-1 max-w-md mx-auto">
              Try: <span className="text-white/80">"Research the top 5 B2B SaaS launches this month and write a one-page brief."</span>
            </div>
          </div>
        ) : (
          <ul className="grid md:grid-cols-2 gap-3" data-testid="missions-list">
            {missions.map((m) => {
              const s = STATUS_MAP[m.status] || STATUS_MAP.running;
              return (
                <li key={m.id}>
                  <Link
                    to={`/app/org/${orgId}/missions/${m.id}`}
                    className="sp-glass rounded-xl p-4 block hover:border-white/25 transition group relative"
                    data-testid={`mission-card-${m.id}`}
                  >
                    <button
                      onClick={(e) => deleteMission(m.id, e)}
                      className="absolute top-2 right-2 sp-btn sp-btn-ghost !p-1.5 opacity-0 group-hover:opacity-100 transition hover:!text-red-300"
                      data-testid={`delete-mission-${m.id}`}
                      title="Delete mission"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                    <div className="flex items-start gap-3">
                      <div className="w-9 h-9 rounded-lg sp-glass-strong flex items-center justify-center shrink-0">
                        <Rocket className="w-4 h-4" />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2 mb-1">
                          <span className={`sp-chip text-[10px] ${s.cls}`}>{s.label}</span>
                          <span className="text-[10px] sp-subtle">{m.iteration_count || 0} steps</span>
                        </div>
                        <div className="text-sm font-medium line-clamp-2">{displayMissionGoal(m.goal)}</div>
                        <div className="text-[11px] sp-subtle mt-1 truncate">
                          {m.lead_agent_name || "Lead"} · {relTime(m.started_at)}
                          {m.artifacts?.length ? ` · ${m.artifacts.length} artifact${m.artifacts.length === 1 ? "" : "s"}` : ""}
                        </div>
                      </div>
                      <ChevronRight className="w-4 h-4 text-white/40 group-hover:text-white transition shrink-0 mt-2" />
                    </div>
                  </Link>
                </li>
              );
            })}
          </ul>
        )}
      </main>
      {startOpen && <StartMissionDialog orgId={orgId} onClose={() => setStartOpen(false)} onSubmit={startMission} />}
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────────
function StartMissionDialog({ orgId, onClose, onSubmit }) {
  const [goal, setGoal] = useState("");
  const [busy, setBusy] = useState(false);
  const [attachments, setAttachments] = useState([]);  // uploaded records
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const taRef = useRef(null);
  const fileRef = useRef(null);
  useEffect(() => { taRef.current?.focus(); }, []);

  const addFiles = async (files) => {
    if (!files || !files.length) return;
    if (attachments.length + files.length > 6) {
      toast.error("Up to 6 files per mission.");
      return;
    }
    setUploading(true);
    try {
      const fd = new FormData();
      Array.from(files).forEach((f) => fd.append("files", f));
      const { data } = await api.post(`/orgs/${orgId}/missions/uploads`, fd, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      setAttachments((prev) => [...prev, ...(data.attachments || [])]);
    } catch (err) {
      toast.error(errorMessage(err, "Upload failed"));
    } finally { setUploading(false); }
  };

  const removeAttachment = async (attId) => {
    try {
      await api.delete(`/orgs/${orgId}/missions/uploads/${attId}`);
    } catch {/* best effort — even if delete fails, drop from UI */}
    setAttachments((prev) => prev.filter((a) => a.id !== attId));
  };

  // Clean up any uploaded-but-not-used attachments when the dialog closes
  // without starting a mission. Prevents orphaned disk space.
  const cleanupAndClose = async () => {
    for (const a of attachments) {
      try { await api.delete(`/orgs/${orgId}/missions/uploads/${a.id}`); }
      catch {/* ignore */}
    }
    onClose();
  };

  const submit = async () => {
    const g = goal.trim();
    if (g.length < 6) { toast.error("Describe the goal in a sentence or two."); return; }
    setBusy(true);
    await onSubmit(g, attachments.map((a) => a.id));
    setBusy(false);
  };

  const onDrop = (e) => {
    e.preventDefault();
    setDragging(false);
    addFiles(e.dataTransfer.files);
  };

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm"
      onClick={cleanupAndClose}
      data-testid="start-mission-dialog"
    >
      <div className="sp-glass-strong rounded-2xl w-full max-w-lg p-6 sp-fade-up max-h-[90vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h3 className="sp-display text-2xl">Start a mission</h3>
          <button onClick={cleanupAndClose} className="text-white/60 hover:text-white"><X className="w-4 h-4" /></button>
        </div>
        <p className="text-xs sp-subtle mb-2">
          Describe a goal in plain English. The lead agent will pick tools, iterate, and ship a deliverable when done.
        </p>
        <div className="mb-3">
          <MissionGoalDrafter
            currentGoal={goal}
            onAccept={(polished) => {
              setGoal(polished);
              // Re-focus + scroll caret to end so the user sees the
              // new text without having to click into the textarea.
              requestAnimationFrame(() => {
                const ta = taRef.current;
                if (!ta) return;
                ta.focus();
                ta.setSelectionRange(polished.length, polished.length);
              });
            }}
            disabled={busy}
          />
        </div>
        <textarea
          ref={taRef}
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          rows={5}
          disabled={busy}
          placeholder={`Examples:\n• Research the top 10 competitors and write a one-page teardown.\n• Draft 5 LinkedIn posts about our Q1 launch and save each as markdown.\n• Read the attached brief and produce a 3-page summary deck.\n• Read our brand guide and generate 3 hero images for the landing page.`}
          className="w-full sp-glass rounded-xl px-3 py-2 text-sm focus:outline-none resize-none"
          style={{ background: "var(--input-bg)", color: "var(--fg)" }}
          data-testid="mission-goal-input"
          onKeyDown={(e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") submit(); }}
        />

        {/* Preview the Composio tools the lead agent can call once the
            mission runs. Self-hides on deployments without Composio,
            and on orgs with zero active connections it shows a friendly
            pointer to the Connections panel. */}
        <AvailableMissionToolsPreview orgId={orgId} />

        {/* Drag-drop attachment zone */}
        <div
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          className={`mt-3 rounded-xl border-2 border-dashed p-3 transition ${
            dragging ? "border-orange-400 bg-orange-500/10" : "border-white/10 bg-white/[0.02]"
          }`}
          data-testid="mission-dropzone"
        >
          {attachments.length === 0 ? (
            <div className="flex items-center gap-3 text-xs sp-subtle">
              <UploadCloud className="w-4 h-4 text-white/40" />
              <span>Drag files here to attach (images, PDFs, docs — up to 6, 25 MB each)</span>
              <button type="button" onClick={() => fileRef.current?.click()}
                      className="sp-btn sp-btn-ghost !py-0.5 !px-2 text-[11px] ml-auto">
                Browse
              </button>
            </div>
          ) : (
            <div className="space-y-1.5">
              {attachments.map((a) => (
                <div key={a.id} className="flex items-center gap-2 text-xs py-0.5"
                     data-testid={`attachment-row-${a.id}`}>
                  <FileText className="w-3 h-3 text-white/50 shrink-0" />
                  <span className="truncate text-white/85">{a.original_name}</span>
                  <span className="sp-subtle shrink-0">({Math.round(a.size / 1024)} KB)</span>
                  <button type="button" onClick={() => removeAttachment(a.id)}
                          className="ml-auto text-white/40 hover:text-red-300 shrink-0"
                          data-testid={`attachment-remove-${a.id}`}>
                    <X className="w-3 h-3" />
                  </button>
                </div>
              ))}
              {attachments.length < 6 && (
                <button type="button" onClick={() => fileRef.current?.click()}
                        className="sp-btn sp-btn-ghost !py-0.5 !px-2 text-[11px] mt-1"
                        data-testid="add-more-attachments">
                  <UploadCloud className="w-3 h-3" /> Add more
                </button>
              )}
            </div>
          )}
          {uploading && (
            <div className="flex items-center gap-2 text-[11px] text-sky-200 mt-2">
              <Loader2 className="w-3 h-3 animate-spin" /> Uploading…
            </div>
          )}
          <input type="file" ref={fileRef} multiple className="hidden"
                 onChange={(e) => { addFiles(e.target.files); e.target.value = ""; }}
                 data-testid="mission-file-input" />
        </div>

        <div className="flex justify-end gap-2 mt-4">
          <button onClick={cleanupAndClose} disabled={busy} className="sp-btn sp-btn-ghost text-xs">Cancel</button>
          <button onClick={submit} disabled={busy || goal.trim().length < 6 || uploading}
                  className="sp-btn sp-btn-primary text-xs disabled:opacity-50"
                  data-testid="mission-submit-btn">
            {busy ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Starting…</>
                  : <><Play className="w-3.5 h-3.5" /> Start mission</>}
          </button>
        </div>
        <div className="text-[10px] sp-subtle mt-2 text-right">⌘/Ctrl + Enter to start</div>
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────────
function MissionDetail({ orgId, missionId }) {
  const [mission, setMission] = useState(null);
  const [events, setEvents] = useState([]);
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const logRef = useRef(null);

  const load = async () => {
    try {
      const [{ data: m }, { data: ev }] = await Promise.all([
        api.get(`/missions/${missionId}`),
        api.get(`/missions/${missionId}/events`),
      ]);
      setMission(m);
      setEvents(ev || []);
    } catch (err) {
      toast.error(errorMessage(err, "Couldn't load mission"));
    }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [missionId]);

  // Live-ish polling — 2s while running, 5s when idle, pause when tab hidden.
  useEffect(() => {
    let t;
    const tick = async () => {
      if (document.hidden) { t = setTimeout(tick, 4000); return; }
      try {
        const { data: m } = await api.get(`/missions/${missionId}`);
        setMission(m);
        if (events.length > 0) {
          const last = events[events.length - 1]?.ts;
          const { data: more } = await api.get(`/missions/${missionId}/events`, { params: { after: last } });
          if (more && more.length) setEvents((prev) => [...prev, ...more]);
        } else {
          const { data: all } = await api.get(`/missions/${missionId}/events`);
          setEvents(all || []);
        }
        const live = ["running", "awaiting_user"].includes(m.status);
        t = setTimeout(tick, live ? 2000 : 6000);
      } catch { t = setTimeout(tick, 5000); }
    };
    t = setTimeout(tick, 2000);
    return () => clearTimeout(t);
    // eslint-disable-next-line
  }, [missionId, events.length, mission?.status]);

  // Auto-scroll log when new events arrive if user is already near the bottom
  useEffect(() => {
    const el = logRef.current;
    if (!el) return;
    const near = el.scrollHeight - (el.scrollTop + el.clientHeight) < 240;
    if (near) el.scrollTop = el.scrollHeight;
  }, [events.length]);

  const isLive = ["running", "awaiting_user"].includes(mission?.status);

  // Stall detection — a "running" mission with no event in 2+ minutes is
  // very likely stuck on a slow tool call or a degraded model provider.
  // We surface a warning + "Force cancel" affordance so users always have
  // an escape hatch instead of staring at a frozen spinner.
  const lastActivityIso =
    mission?.last_activity_at ||
    (events.length ? events[events.length - 1].ts : mission?.started_at);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!isLive) return;
    const id = setInterval(() => setNow(Date.now()), 5000);
    return () => clearInterval(id);
  }, [isLive]);
  const stalledSeconds = (() => {
    if (!isLive || !lastActivityIso) return 0;
    const d = parseServerDate(lastActivityIso);
    if (!d) return 0;
    return Math.max(0, Math.floor((now - d.getTime()) / 1000));
  })();
  const isStalled = isLive && stalledSeconds >= 120;

  const cancel = async () => {
    if (!window.confirm("Cancel this mission?")) return;
    try {
      await api.post(`/missions/${missionId}/cancel`);
      await load();
      toast.success("Mission cancelled");
    } catch (err) {
      toast.error(errorMessage(err, "Cancel failed"));
    }
  };

  const nav = useNavigate();
  const remove = async () => {
    if (!window.confirm("Delete this mission, its work log, and any artifacts it produced? This can't be undone.")) return;
    try {
      await api.delete(`/missions/${missionId}`);
      toast.success("Mission deleted");
      nav(`/app/org/${orgId}/missions`);
    } catch (err) {
      toast.error(errorMessage(err, "Delete failed"));
    }
  };

  // Continue a finished/cancelled/failed mission with new instructions.
  // Most common use: "go deeper, browse each app's site and add pricing."
  const [continueOpen, setContinueOpen] = useState(false);
  const [continueText, setContinueText] = useState("");
  const [continueBusy, setContinueBusy] = useState(false);
  // ChatGPT Agent Mode: live remote browser stream. Only meaningful while
  // the mission is running. Auth token must match what the backend's
  // `_require_mission_member_ws` accepts — that's either the legacy
  // bcrypt JWT we persist under `soulprint_token` OR a freshly-minted
  // Firebase ID token. We resolve it asynchronously so Google sign-in
  // users also get a working stream.
  const [showBrowser, setShowBrowser] = useState(false);
  const [wsToken, setWsToken] = useState("");
  useEffect(() => {
    let cancelled = false;
    (async () => {
      // Prefer Firebase ID token when the user signed in with Google
      // (legacy token may be empty in that case). Otherwise fall back
      // to the bcrypt JWT in localStorage.
      let token = "";
      if (FIREBASE_ENABLED) {
        try { token = await getCurrentIdToken(); } catch { /* ignore */ }
      }
      if (!token) token = getToken();
      if (!cancelled) setWsToken(token || "");
    })();
    return () => { cancelled = true; };
  }, []);

  // B3: when the agent hits a login wall, auto-open the live browser
  // panel so the "Take control" button is one click away.
  useEffect(() => {
    if (mission?.needs_login_for) setShowBrowser(true);
  }, [mission?.needs_login_for]);
  const continueRun = async () => {
    setContinueBusy(true);
    try {
      await api.post(`/missions/${missionId}/continue`, { instructions: continueText.trim() });
      setContinueText("");
      setContinueOpen(false);
      toast.success("Picking up where the mission left off…");
      await load();
    } catch (err) {
      toast.error(errorMessage(err, "Continue failed"));
    } finally { setContinueBusy(false); }
  };

  const sendAnswer = async () => {
    const a = answer.trim();
    if (!a) return;
    setBusy(true);
    try {
      await api.post(`/missions/${missionId}/answer`, { answer: a });
      setAnswer("");
      await load();
    } catch (err) {
      toast.error(errorMessage(err, "Couldn't send answer"));
    } finally { setBusy(false); }
  };

  // Escape hatch when the agent keeps re-asking the same clarifying
  // question even after the user has said "proceed". Server flips a
  // `user_declined_more_info` flag that blocks ask_user for the rest
  // of the mission, then resumes the loop. The agent must now deliver
  // from public data or finish with the gap documented.
  const forceFinish = async () => {
    if (!window.confirm(
      "Stop asking and finish with what's already gathered?\n\n" +
      "The agent will be told to deliver from public sources or " +
      "finish honestly noting any data gap. It will not ask another " +
      "question on this mission."
    )) return;
    setBusy(true);
    try {
      await api.post(`/missions/${missionId}/force-finish`);
      setAnswer("");
      toast.success("Agent told to finish without more questions.");
      await load();
    } catch (err) {
      toast.error(errorMessage(err, "Couldn't force-finish the mission"));
    } finally { setBusy(false); }
  };

  const status = mission?.status ? STATUS_MAP[mission.status] : null;

  return (
    <div className="min-h-screen" style={{ background: "var(--bg)", color: "var(--fg)" }}>
      <TopBar />
      <main className="max-w-5xl mx-auto px-6 py-8" data-testid="mission-detail">
        <Link to={`/app/org/${orgId}/missions`} className="text-xs sp-subtle hover:text-white inline-flex items-center gap-1 mb-3">
          <ArrowLeft className="w-3 h-3" /> All missions
        </Link>
        {mission && (
          <div className="sp-glass-strong rounded-2xl p-5 mb-6">
            <div className="flex items-start gap-3 flex-wrap">
              <div className="w-10 h-10 rounded-xl sp-glass flex items-center justify-center shrink-0">
                {isLive
                  ? <Loader2 className="w-4 h-4 animate-spin" />
                  : <Rocket className="w-4 h-4" />}
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 mb-1 flex-wrap">
                  {status && <span className={`sp-chip text-[10px] ${status.cls}`} data-testid="mission-status">{status.label}</span>}
                  {isStalled && (
                    <span className="sp-chip text-[10px] !bg-amber-400/15 !text-amber-200 flex items-center gap-1" data-testid="mission-stalled-chip">
                      <AlertTriangle className="w-3 h-3" />
                      Stalled · no activity for {Math.floor(stalledSeconds / 60)}m {stalledSeconds % 60}s
                    </span>
                  )}
                  {mission?.needs_login_for && (
                    <button
                      type="button"
                      onClick={() => setShowBrowser(true)}
                      className="sp-chip text-[10px] !bg-orange-400/20 !text-orange-200 flex items-center gap-1 hover:!bg-orange-400/30"
                      data-testid="mission-needs-login-chip"
                      title="Open the live browser and click Take control to log in"
                    >
                      <Lock className="w-3 h-3" />
                      Login required for {mission.needs_login_for} · click to open
                    </button>
                  )}
                  <span className="text-[11px] sp-subtle">
                    {mission.lead_agent_name} · {mission.iteration_count || 0}/{25} steps
                  </span>
                </div>
                <div className="text-base font-medium leading-snug">{displayMissionGoal(mission.goal)}</div>
                {(mission.attachments || []).length > 0 && (
                  <div className="mt-2 flex flex-wrap items-center gap-1.5" data-testid="mission-attachments-strip">
                    <span className="text-[10px] uppercase tracking-widest sp-subtle mr-1">Attached</span>
                    {mission.attachments.map((a) => (
                      <a key={a.id}
                         href={a.url?.startsWith("http") ? a.url : `${BACKEND_URL}${a.url}`}
                         target="_blank" rel="noreferrer"
                         title={`${a.original_name} (${Math.round((a.size || 0) / 1024)} KB)`}
                         className="inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full bg-white/5 border border-white/10 text-white/75 hover:text-white hover:bg-white/10"
                         data-testid={`mission-attachment-${a.id}`}>
                        {a.kind === "image"
                          ? <ImageIcon className="w-3 h-3" />
                          : <FileText className="w-3 h-3" />}
                        <span className="truncate max-w-[160px]">{a.original_name}</span>
                      </a>
                    ))}
                  </div>
                )}
                {mission.conversation_id && (
                  <ConvBacklink convId={mission.conversation_id} />
                )}
                {mission.error && (
                  <div className="text-xs mt-2 text-red-300 flex items-center gap-1"><AlertTriangle className="w-3 h-3" /> {mission.error}</div>
                )}
                {isStalled && (
                  <div className="text-xs mt-3 sp-glass rounded-lg px-3 py-2 border border-amber-400/30 text-amber-100 leading-relaxed" data-testid="mission-stalled-banner">
                    The agent hasn't logged activity in over 2 minutes. This usually means a tool or LLM call is hung. Click <span className="font-medium">Cancel</span> on the right to stop it — you can then start a fresh mission, or use <span className="font-medium">Continue</span> to resume from where it left off.
                  </div>
                )}
              </div>
              {isLive && (
                <button onClick={cancel} className="sp-btn sp-btn-ghost text-xs" data-testid="mission-cancel-btn">
                  <X className="w-3.5 h-3.5" /> Cancel
                </button>
              )}
              {!isLive && mission?.status && ["finished", "cancelled", "failed"].includes(mission.status) && (
                <button onClick={() => setContinueOpen(true)} className="sp-btn sp-btn-primary text-xs" data-testid="mission-continue-btn" title="Run again with more direction">
                  <Play className="w-3.5 h-3.5" /> Continue
                </button>
              )}
              <button onClick={remove} className="sp-btn sp-btn-ghost text-xs hover:!text-red-300" data-testid="mission-delete-btn" title="Delete this mission">
                <Trash2 className="w-3.5 h-3.5" /> Delete
              </button>
            </div>
          </div>
        )}

        {/* ChatGPT Agent Mode — live remote browser stream. Only shown
            while the mission is actively running so users don't see a
            stale Chromium frame on a finished run. The "Watch live"
            toggle lets users open the stream on demand to avoid
            burning bandwidth on every detail-page view. */}
        {isLive && (
          <div className="mb-6">
            {!showBrowser ? (
              <button
                type="button"
                onClick={() => setShowBrowser(true)}
                className="sp-btn sp-btn-ghost text-xs"
                data-testid="mission-watch-browser-btn"
                title="Open the remote browser feed and take control to log in"
              >
                <Search className="w-3.5 h-3.5" /> Watch live browser
              </button>
            ) : (
              <BrowserStreamCanvas
                missionId={missionId}
                token={wsToken}
                needsLoginFor={mission?.needs_login_for || ""}
                onClose={() => setShowBrowser(false)}
              />
            )}
          </div>
        )}

        {/* Outcome — the agent's final summary, surfaced as the FIRST
            thing users see on a finished mission. Without this card, the
            summary would only live as a faint subtitle under the goal,
            which led to "I can't see the results" complaints. */}
        {mission?.summary && ["finished", "cancelled", "failed"].includes(mission.status) && (
          <div className="sp-glass-strong rounded-2xl p-5 mb-6" data-testid="mission-outcome">
            <div className="flex items-center justify-between gap-2 mb-3 flex-wrap">
              <div className="flex items-center gap-2">
                <div className="w-8 h-8 rounded-lg sp-glass flex items-center justify-center">
                  <Check className="w-4 h-4 text-emerald-300" />
                </div>
                <div>
                  <div className="text-[10px] uppercase tracking-widest sp-subtle">Outcome</div>
                  <div className="text-base font-medium" style={{ color: "var(--fg)" }}>Final report</div>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <Link
                  to={`/app/org/${orgId}#documents`}
                  className="sp-btn sp-btn-ghost text-[11px]"
                  data-testid="mission-outcome-docs-link"
                  title="Jump to your Documents library on the Org page"
                >
                  <FileText className="w-3.5 h-3.5" /> Open in Documents
                </Link>
              </div>
            </div>
            <div
              className="text-sm leading-relaxed whitespace-pre-wrap"
              style={{ color: "var(--fg)" }}
              data-testid="mission-outcome-text"
            >
              {mission.summary}
            </div>
          </div>
        )}

        {/* Artifacts sidebar — surface deliverables prominently */}
        {mission?.artifacts?.length > 0 && (
          <div className="mb-6" data-testid="mission-artifacts">
            <div className="text-[10px] uppercase tracking-widest sp-subtle mb-2">Deliverables</div>
            <ul className="grid md:grid-cols-2 gap-2">
              {mission.artifacts.map((a, i) => {
                const url = a.url?.startsWith("http") ? a.url : `${BACKEND_URL}${a.url}`;
                const isImg = (a.mime || "").startsWith("image/");
                // Prefer artifact id / url over array-index — protects
                // against subtle state loss if a mission re-emits its
                // artifact list out-of-order on a refresh poll.
                const key = a.document_id || a.id || a.url || `artifact-${i}`;
                return (
                  <li key={key} className="sp-glass rounded-xl p-3 flex flex-col gap-2">
                    <div className="flex items-start gap-2.5">
                      <div className="w-8 h-8 rounded-lg sp-glass-strong flex items-center justify-center shrink-0">
                        {isImg ? <ImageIcon className="w-3.5 h-3.5" /> : <FileText className="w-3.5 h-3.5" />}
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="text-sm font-medium truncate">{a.original_name}</div>
                        <div className="text-[11px] sp-subtle truncate">{a.mime}</div>
                      </div>
                      <a href={url} download={a.original_name} className="sp-btn sp-btn-ghost text-[11px]"
                         onClick={async (ev) => {
                           ev.preventDefault();
                           try { await downloadFile(a.url || url, a.original_name || "deliverable"); }
                           catch (err) { toast.error(err?.message || "Download failed"); }
                         }}>
                        <Download className="w-3 h-3" /> Get
                      </a>
                    </div>
                    {isImg && (
                      <a href={url} target="_blank" rel="noreferrer" className="block rounded-lg overflow-hidden border border-white/10">
                        <img src={url} alt={a.original_name} loading="lazy" className="w-full max-h-48 object-cover bg-black/40" />
                      </a>
                    )}
                  </li>
                );
              })}
            </ul>
          </div>
        )}

        {/* Ask-user panel */}
        {mission?.status === "awaiting_user" && (
          <div className="sp-glass-strong rounded-2xl p-4 mb-6 border border-amber-400/40" data-testid="mission-ask-user">
            <div className="flex items-start gap-3">
              <div className="w-9 h-9 rounded-lg sp-glass flex items-center justify-center shrink-0">
                <HelpCircle className="w-4 h-4 text-amber-300" />
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-[10px] uppercase tracking-widest sp-subtle mb-1">Agent is asking</div>
                <div className="text-sm mb-3">{mission.question}</div>
                <div className="flex items-end gap-2">
                  <textarea
                    value={answer}
                    onChange={(e) => setAnswer(e.target.value)}
                    rows={2}
                    disabled={busy}
                    placeholder="Type your answer…"
                    className="flex-1 sp-glass rounded-xl px-3 py-2 text-sm focus:outline-none resize-none"
                    style={{ background: "var(--input-bg)", color: "var(--fg)" }}
                    data-testid="mission-answer-input"
                    onKeyDown={(e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") sendAnswer(); }}
                  />
                  <button onClick={sendAnswer} disabled={busy || !answer.trim()}
                          className="sp-btn sp-btn-primary text-xs disabled:opacity-50"
                          data-testid="mission-answer-submit">
                    {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Send className="w-3.5 h-3.5" />}
                    Send
                  </button>
                </div>
                <div className="mt-3 flex items-center gap-2">
                  <button
                    onClick={forceFinish}
                    disabled={busy}
                    className="text-[11px] sp-subtle hover:text-amber-200 inline-flex items-center gap-1.5 transition disabled:opacity-50"
                    title="Tell the agent to stop asking and deliver from what it already has."
                    data-testid="mission-force-finish"
                  >
                    <Flag className="w-3.5 h-3.5" />
                    Just finish with what you have
                  </button>
                  <span className="text-[10px] sp-subtle">
                    — use this if the agent is asking the same thing on repeat.
                  </span>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Live log */}
        <div className="text-[10px] uppercase tracking-widest sp-subtle mb-2">Work log</div>
        <div
          ref={logRef}
          className="sp-glass rounded-2xl p-4 max-h-[60vh] overflow-y-auto space-y-2 font-mono text-[12.5px] leading-relaxed"
          data-testid="mission-log"
        >
          {events.length === 0 ? (
            <div className="sp-subtle text-center py-4">Thinking…</div>
          ) : events.map((e) => <EventRow key={e.id} e={e} />)}
        </div>

        {continueOpen && (
          <div
            className="fixed inset-0 z-40 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm"
            onClick={() => !continueBusy && setContinueOpen(false)}
            data-testid="mission-continue-dialog"
          >
            <div className="sp-glass-strong rounded-2xl w-full max-w-lg p-6 sp-fade-up" onClick={(e) => e.stopPropagation()}>
              <div className="flex items-center justify-between mb-4">
                <div>
                  <h3 className="sp-display text-2xl">Continue the mission</h3>
                  <div className="text-xs sp-subtle mt-1">Tell the agent how to deepen, refine, or extend this run. The original work log is preserved.</div>
                </div>
                <button onClick={() => setContinueOpen(false)} className="text-white/60 hover:text-white"><X className="w-4 h-4" /></button>
              </div>
              <textarea
                value={continueText}
                onChange={(e) => setContinueText(e.target.value)}
                rows={4}
                disabled={continueBusy}
                placeholder={`Examples:\n• Use browse_url on each of the 5 apps' websites and add real pricing.\n• Add a comparison table with feature checkmarks.\n• Generate a hero image for the brief.`}
                className="w-full sp-glass rounded-xl px-3 py-2 text-sm focus:outline-none resize-none"
                style={{ background: "var(--input-bg)", color: "var(--fg)" }}
                data-testid="mission-continue-input"
                onKeyDown={(e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") continueRun(); }}
              />
              <div className="flex justify-end gap-2 mt-4">
                <button onClick={() => setContinueOpen(false)} disabled={continueBusy} className="sp-btn sp-btn-ghost text-xs">Cancel</button>
                <button onClick={continueRun} disabled={continueBusy} className="sp-btn sp-btn-primary text-xs disabled:opacity-50" data-testid="mission-continue-submit">
                  {continueBusy ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Continuing…</> : <><Play className="w-3.5 h-3.5" /> Continue</>}
                </button>
              </div>
              <div className="text-[10px] sp-subtle mt-2 text-right">⌘/Ctrl + Enter to submit</div>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────────
function EventRow({ e }) {
  const iter = e.iteration ? `${e.iteration}` : "·";
  if (e.kind === "think") {
    return (
      <div className="flex gap-2">
        <span className="sp-subtle w-8 shrink-0">[{iter}]</span>
        <span className="opacity-90">💭 {e.text}</span>
      </div>
    );
  }
  if (e.kind === "tool_call") {
    return (
      <div className="flex gap-2">
        <span className="sp-subtle w-8 shrink-0">[{iter}]</span>
        <span><Wrench className="w-3 h-3 inline mr-1 text-blue-300" /><span className="text-blue-200">{e.tool}</span> <span className="sp-subtle">→ {JSON.stringify(e.args).slice(0, 160)}</span></span>
      </div>
    );
  }
  if (e.kind === "tool_result") {
    return (
      <div className="flex gap-2">
        <span className="sp-subtle w-8 shrink-0">[{iter}]</span>
        <span className="sp-subtle">└ {e.tool}: {(e.result_preview || "").slice(0, 200)}</span>
      </div>
    );
  }
  if (e.kind === "artifact") {
    const a = e.artifact || {};
    const isImg = (a.mime || "").startsWith("image/");
    const url = a.url ? (a.url.startsWith("http") ? a.url : `${BACKEND_URL}${a.url}`) : "";
    return (
      <div className="flex gap-2 items-start">
        <span className="sp-subtle w-8 shrink-0">[{iter}]</span>
        <div className="min-w-0 flex-1">
          <span className="text-emerald-200">📎 saved {a.original_name}</span>
          {isImg && url && (
            <a href={url} target="_blank" rel="noreferrer" className="block mt-2 max-w-md rounded-lg overflow-hidden border border-white/10 hover:border-white/30 transition">
              <img src={url} alt={a.original_name} loading="lazy" className="w-full max-h-72 object-contain bg-black/40" />
            </a>
          )}
        </div>
      </div>
    );
  }
  if (e.kind === "question") {
    return (
      <div className="flex gap-2">
        <span className="sp-subtle w-8 shrink-0">[{iter}]</span>
        <span className="text-amber-200">❓ {e.text}</span>
      </div>
    );
  }
  if (e.kind === "finish") {
    return (
      <div className="flex gap-2">
        <span className="sp-subtle w-8 shrink-0">[{iter}]</span>
        <span className="text-emerald-200"><Check className="w-3 h-3 inline mr-1" /> {e.text}</span>
      </div>
    );
  }
  return (
    <div className="flex gap-2 opacity-70">
      <span className="sp-subtle w-8 shrink-0">[{iter}]</span>
      <span className="sp-subtle">{e.text}</span>
    </div>
  );
}


function ConvBacklink({ convId }) {
  // The mission row only stores `conversation_id`. To build a link to
  // /app/team/:teamId/c/:convId we need the team_id, so resolve it lazily
  // via the conversation API. Falls back to no-op if the API errors (e.g.
  // the conversation was deleted).
  // We also peek at the conversation's title so we can show a clearer
  // label than the misleading "Started in chat" text — if the link points
  // to the org's Chief of Staff chat (the most common case for missions
  // started outside of a real conversation), users were confused
  // ("results in chat, but I never started a chat"). The new wording
  // makes it clear this is a *working chat* tied to the mission, not
  // something the user themselves opened.
  const [conv, setConv] = useState(null);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { data } = await api.get(`/conversations/${convId}`);
        if (!cancelled) setConv(data);
      } catch { /* conv gone — silent */ }
    })();
    return () => { cancelled = true; };
  }, [convId]);
  if (!conv?.team_id) return null;
  const isGeneral = !!conv.is_general;
  const label = isGeneral
    ? `Working chat · Chief of Staff`
    : `Working chat · ${(conv.title || "Open conversation").slice(0, 40)}`;
  return (
    <Link
      to={`/app/team/${conv.team_id}/c/${convId}`}
      className="text-xs sp-subtle hover:text-white inline-flex items-center gap-1 mt-1"
      data-testid="mission-conv-backlink"
      title="Open the chat thread the mission ran in"
    >
      <CornerDownRight className="w-3 h-3" />
      {label}
    </Link>
  );
}
