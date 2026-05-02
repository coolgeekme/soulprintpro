import React, { useEffect, useState } from "react";
import api, { BACKEND_URL, displayMissionGoal, downloadFile } from "../lib/api";
import { Link } from "react-router-dom";
import {
  Loader2, Rocket, Check, AlertTriangle, ChevronDown, ChevronUp,
  ExternalLink, FileText, Image as ImageIcon, X as XIcon, Download,
} from "lucide-react";
import { toast } from "sonner";

/**
 * MissionCard — embedded inside a conversation thread, shows the live
 * status of a mission that was started from this chat. Polls the mission
 * + its events while the run is active, then settles into a "done /
 * failed / cancelled" terminal state with an artifacts strip.
 *
 * Toggle "Show work log" to expand the full think → tool_call → tool_result
 * event stream — same data the dedicated /missions detail page shows.
 */
const STATUS_LABEL = {
  running: "Running",
  awaiting_user: "Waiting for you",
  paused: "Paused",
  finished: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
};
const STATUS_CLS = {
  running: "!bg-blue-400/15 !text-blue-200",
  awaiting_user: "!bg-amber-400/15 !text-amber-200",
  paused: "!bg-white/10",
  finished: "!bg-emerald-400/15 !text-emerald-200",
  failed: "!bg-red-400/15 !text-red-200",
  cancelled: "!bg-white/10 opacity-70",
};

export function MissionCard({ orgId, missionId }) {
  const [mission, setMission] = useState(null);
  const [events, setEvents] = useState([]);
  const [showLog, setShowLog] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    let timer = null;
    const tick = async () => {
      if (cancelled) return;
      try {
        const [{ data: m }, { data: ev }] = await Promise.all([
          api.get(`/missions/${missionId}`),
          api.get(`/missions/${missionId}/events`),
        ]);
        if (cancelled) return;
        setMission(m);
        setEvents(ev || []);
        const live = ["running", "awaiting_user"].includes(m.status);
        timer = setTimeout(tick, live ? 3000 : 8000);
      } catch (err) {
        if (cancelled) return;
        setError(err?.response?.data?.detail || "Couldn't load mission");
        timer = setTimeout(tick, 6000);
      }
    };
    tick();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [missionId]);

  if (error && !mission) {
    return (
      <div className="sp-glass rounded-xl px-4 py-3 mt-3 text-xs text-red-300 flex items-center gap-2"
           data-testid={`mission-card-error-${missionId}`}>
        <AlertTriangle className="w-3.5 h-3.5" /> {error}
      </div>
    );
  }
  if (!mission) {
    return (
      <div className="sp-glass rounded-xl px-4 py-3 mt-3 text-xs sp-subtle flex items-center gap-2"
           data-testid={`mission-card-loading-${missionId}`}>
        <Loader2 className="w-3.5 h-3.5 animate-spin" /> Loading mission…
      </div>
    );
  }

  const isLive = ["running", "awaiting_user"].includes(mission.status);
  const isDone = mission.status === "finished";
  const isFailed = ["failed", "cancelled"].includes(mission.status);
  const lastEvent = events.length ? events[events.length - 1] : null;

  // Friendlier, verb-forward status phrases — makes the card feel alive
  // instead of "Loading…". We key the phrasing off the tool_call so the
  // user gets real "what's happening right now" feedback.
  const PRETTY_TOOL = {
    browse_url:        "Reading the web",
    browse_click:      "Clicking on a page",
    browse_fill:       "Filling out a form",
    browse_submit:     "Submitting a form",
    screenshot_url:    "Taking a screenshot",
    web_search:        "Searching the web",
    generate_image:    "Rendering an image",
    write_artifact:    "Saving a file",
    read_org_document: "Reading your docs",
    compose_email:     "Drafting an email",
  };
  const lastEventLabel = (() => {
    if (!lastEvent) return "Booting up…";
    if (lastEvent.kind === "think") {
      const txt = (lastEvent.text || "").slice(0, 110);
      return txt ? `Thinking: ${txt}${(lastEvent.text || "").length > 110 ? "…" : ""}` : "Thinking…";
    }
    if (lastEvent.kind === "tool_call") {
      const pretty = PRETTY_TOOL[lastEvent.tool] || `Running ${lastEvent.tool}`;
      const url = lastEvent.args?.url;
      if (url && lastEvent.tool === "browse_url") {
        try {
          return `${pretty} — ${new URL(url).hostname}`;
        } catch { /* ignore */ }
      }
      const q = lastEvent.args?.query;
      if (q && lastEvent.tool === "web_search") return `${pretty} — "${String(q).slice(0, 60)}"`;
      return pretty + "…";
    }
    if (lastEvent.kind === "tool_result") {
      const pretty = PRETTY_TOOL[lastEvent.tool] || lastEvent.tool;
      return `${pretty} done — deciding next step…`;
    }
    if (lastEvent.kind === "artifact") return `Saved ${lastEvent.artifact?.original_name || "a file"}`;
    if (lastEvent.kind === "question") return "Awaiting your reply";
    if (lastEvent.kind === "system") return (lastEvent.text || "").slice(0, 110);
    return "Working…";
  })();

  return (
    <div className="sp-glass-strong rounded-2xl px-4 py-3.5 mt-3 border border-white/10"
         data-testid={`mission-card-${missionId}`}>
      <div className="flex items-start gap-3">
        <div className="w-9 h-9 rounded-xl sp-glass flex items-center justify-center shrink-0">
          {isLive ? <Loader2 className="w-4 h-4 animate-spin" />
                  : isFailed ? <AlertTriangle className="w-4 h-4 text-red-300" />
                  : <Check className="w-4 h-4 text-emerald-300" />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap mb-1">
            <span className={`sp-chip text-[10px] ${STATUS_CLS[mission.status] || ""}`}
                  data-testid={`mission-card-status-${missionId}`}>
              {STATUS_LABEL[mission.status] || mission.status}
            </span>
            <span className="text-[11px] sp-subtle">
              {mission.lead_agent_name} · step {mission.iteration_count || 0}/25
            </span>
            <Link
              to={`/app/org/${orgId}/missions/${missionId}`}
              className="text-[11px] sp-subtle hover:text-white inline-flex items-center gap-1 ml-auto"
              data-testid={`mission-card-open-${missionId}`}
            >
              Open <ExternalLink className="w-3 h-3" />
            </Link>
          </div>
          <div className="text-sm font-medium leading-snug" data-testid={`mission-card-goal-${missionId}`}>
            <Rocket className="w-3.5 h-3.5 inline mr-1.5 -mt-0.5 opacity-70" />
            {displayMissionGoal(mission.goal)}
          </div>
          {isLive && (
            <div className="mt-2 flex items-start gap-2 text-xs" data-testid={`mission-card-last-event-${missionId}`}>
              <span className="inline-flex items-center gap-1 pt-0.5 shrink-0">
                <span className="relative flex h-2 w-2">
                  <span className="absolute inline-flex h-full w-full rounded-full bg-sky-400 opacity-60 animate-ping"></span>
                  <span className="relative inline-flex h-2 w-2 rounded-full bg-sky-300"></span>
                </span>
              </span>
              <div className="flex-1 min-w-0">
                <div className="sp-subtle leading-snug">
                  {lastEventLabel}
                  <span className="mission-dots" aria-hidden="true"><span>.</span><span>.</span><span>.</span></span>
                </div>
              </div>
            </div>
          )}
          {isDone && mission.summary && (
            <div className="text-sm sp-subtle mt-2 leading-relaxed"
                 data-testid={`mission-card-summary-${missionId}`}>
              {mission.summary}
            </div>
          )}
          {/* Surface an upload-cookies CTA when a social-post mission
              failed due to a missing or expired login session. */}
          {isDone && mission.social_platform && (
            (mission.summary || "").toUpperCase().startsWith("FAILED:") &&
            /login|cookie|session/i.test(mission.summary || "")
          ) && (
            <Link
              to={`/app/org/${mission.org_id}/social`}
              className="mt-2 inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full bg-orange-500/15 border border-orange-400/30 text-orange-200 hover:bg-orange-500/25 transition"
              data-testid={`mission-card-upload-session-${missionId}`}
            >
              Upload {mission.social_platform} login cookies →
            </Link>
          )}
          {isFailed && mission.error && (
            <div className="text-xs text-red-300 mt-2 flex items-start gap-1">
              <AlertTriangle className="w-3 h-3 mt-0.5" /> {mission.error}
            </div>
          )}
          {isFailed && mission.status === "cancelled" && !mission.error && (
            <div className="text-xs sp-subtle mt-2">Mission was cancelled.</div>
          )}
          {/* Artifacts strip */}
          {(mission.artifacts || []).length > 0 && (
            <ul className="mt-3 grid sm:grid-cols-2 gap-2"
                data-testid={`mission-card-artifacts-${missionId}`}>
              {mission.artifacts.map((a, i) => {
                const url = a.url?.startsWith("http") ? a.url : `${BACKEND_URL}${a.url}`;
                const isImg = (a.mime || "").startsWith("image/");
                const name = a.original_name || a.name || "deliverable";
                const onDownload = async (ev) => {
                  // Prevent the surrounding <a> from also navigating.
                  ev.preventDefault();
                  ev.stopPropagation();
                  try {
                    await downloadFile(a.url || url, name);
                  } catch (err) {
                    toast.error(err?.message || "Download failed");
                  }
                };
                return (
                  <li key={i} className="sp-glass rounded-lg p-2.5 flex items-center gap-2.5">
                    <div className="w-7 h-7 rounded-md sp-glass-strong flex items-center justify-center shrink-0">
                      {isImg ? <ImageIcon className="w-3 h-3" /> : <FileText className="w-3 h-3" />}
                    </div>
                    <a href={url} target="_blank" rel="noreferrer"
                       className="text-xs truncate hover:underline flex-1"
                       title={isImg ? "Open in a new tab" : "Open in a new tab — use the download button to save the file"}
                       data-testid={`mission-card-artifact-${missionId}-${i}`}>
                      {name}
                    </a>
                    <button
                      type="button"
                      onClick={onDownload}
                      className="sp-btn sp-btn-ghost text-[11px] !px-2 !py-1 shrink-0"
                      title={`Download ${name}`}
                      data-testid={`mission-card-artifact-download-${missionId}-${i}`}
                    >
                      <Download className="w-3 h-3" />
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          {/* Show / Hide work log toggle */}
          <button
            type="button"
            onClick={() => setShowLog((v) => !v)}
            className="mt-3 text-[11px] sp-subtle hover:text-white inline-flex items-center gap-1"
            data-testid={`mission-card-toggle-log-${missionId}`}
          >
            {showLog ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
            {showLog ? "Hide work log" : `Show work log (${events.length})`}
          </button>
          {showLog && (
            <div className="mt-2 sp-glass rounded-lg p-3 max-h-72 overflow-y-auto text-[11px] font-mono leading-relaxed"
                 data-testid={`mission-card-log-${missionId}`}>
              {events.length === 0 && <div className="sp-subtle">No events yet…</div>}
              {events.map((e) => (
                <MissionEventRow key={e.id} e={e} />
              ))}
            </div>
          )}
          {/* Quick cancel for live missions */}
          {isLive && (
            <button
              type="button"
              onClick={async () => {
                if (!window.confirm("Cancel this mission?")) return;
                try {
                  await api.post(`/missions/${missionId}/cancel`);
                  toast.success("Mission cancelled");
                } catch (err) {
                  toast.error(err?.response?.data?.detail || "Cancel failed");
                }
              }}
              className="ml-3 text-[11px] sp-subtle hover:text-red-300 inline-flex items-center gap-1"
              data-testid={`mission-card-cancel-${missionId}`}
            >
              <XIcon className="w-3 h-3" /> Cancel
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function MissionEventRow({ e }) {
  const iter = e.iteration ?? 0;
  if (e.kind === "think")
    return <div>[{iter}] 💭 {e.text}</div>;
  if (e.kind === "tool_call")
    return <div className="text-blue-200">[{iter}] {e.tool} <span className="sp-subtle">{JSON.stringify(e.args).slice(0, 120)}</span></div>;
  if (e.kind === "tool_result")
    return <div className="sp-subtle">[{iter}] └ {(e.result_preview || "").slice(0, 160)}</div>;
  if (e.kind === "artifact")
    return <div className="text-emerald-200">[{iter}] 📎 {e.artifact?.original_name || "artifact"}</div>;
  if (e.kind === "question")
    return <div className="text-amber-200">[{iter}] ❓ {e.text}</div>;
  if (e.kind === "finish")
    return <div className="text-emerald-200">[{iter}] ✓ {e.text}</div>;
  if (e.kind === "system")
    return <div className="sp-subtle">[{iter}] · {e.text}</div>;
  return null;
}

export default MissionCard;
