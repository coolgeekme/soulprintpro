import React, { useEffect, useMemo, useRef, useState } from "react";
import { useParams, Link, useNavigate } from "react-router-dom";
import api from "../lib/api";
import TopBar from "../components/TopBar";
import { toast } from "sonner";
import { MessageCircle, Users, Plus, Radio, Search, X, Wand2, ArrowRight, Pencil, Save, Trash2, X as XIcon, Loader2, Archive, ArchiveRestore, Paperclip, File as FileIcon } from "lucide-react";
import Logo from "../components/Logo";
import SchedulesPanel from "../components/SchedulesPanel";

const AgentBadge = ({ a, onClick, selected, testPrefix = "agent-badge" }) => (
  <button
    type="button"
    onClick={onClick}
    className={`sp-glass rounded-xl p-4 text-left transition hover:border-white/25 ${selected ? "border-white/40 sp-focus" : ""}`}
    data-testid={`${testPrefix}-${a.id}`}
  >
    <div className="flex items-start justify-between gap-2">
      <div
        className="w-9 h-9 rounded-lg mb-2 flex items-center justify-center text-lg shrink-0"
        style={{ background: `linear-gradient(160deg, ${a.accent}, rgba(255,255,255,0.06))` }}
      >
        <span>{a.emoji || ""}</span>
      </div>
      <span className="sp-chip text-[10px]">{a.division}</span>
    </div>
    <div className="text-white text-sm font-medium line-clamp-1">{a.name}</div>
    <div className="text-[11px] sp-subtle mt-1 line-clamp-2">{a.tagline}</div>
  </button>
);

const AgentFilter = ({ q, setQ, divisions, division, setDivision, total, testPrefix = "" }) => (
  <div className="flex flex-col gap-3 mb-3">
    <div className="flex items-center gap-2">
      <div className="flex-1 flex items-center gap-2 sp-glass rounded-xl px-3 py-2">
        <Search className="w-4 h-4 text-white/60" />
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={`Search ${total} agents by name, role, or division…`}
          className="flex-1 bg-transparent text-sm text-white placeholder:text-white/30 focus:outline-none"
          data-testid={`${testPrefix}agent-search`}
        />
        {q && (
          <button onClick={() => setQ("")} className="text-white/40 hover:text-white" data-testid={`${testPrefix}agent-search-clear`}>
            <X className="w-3.5 h-3.5" />
          </button>
        )}
      </div>
    </div>
    <div className="flex flex-wrap gap-2">
      <button
        onClick={() => setDivision("")}
        className={`sp-chip ${division === "" ? "!bg-white/15 !text-white" : ""}`}
        data-testid={`${testPrefix}div-all`}
      >All ({total})</button>
      {divisions.map((d) => (
        <button
          key={d.key}
          onClick={() => setDivision(d.key === division ? "" : d.key)}
          className={`sp-chip ${division === d.key ? "!bg-white/15 !text-white" : ""}`}
          data-testid={`${testPrefix}div-${d.key}`}
        >{d.label} · {d.count}</button>
      ))}
    </div>
  </div>
);

export default function TeamPage() {
  const { teamId } = useParams();
  const nav = useNavigate();
  const [team, setTeam] = useState(null);
  const [agents, setAgents] = useState([]);
  const [divisions, setDivisions] = useState([]);
  const [conversations, setConversations] = useState([]);
  const [showArchived, setShowArchived] = useState(false);
  const [members, setMembers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [savingSP, setSavingSP] = useState(false);
  const [sp, setSp] = useState({ mission: "", tone: "", audience: "", values: "", north_star: "" });

  const [orgRole, setOrgRole] = useState("");
  const [editTeamOpen, setEditTeamOpen] = useState(false);
  const [editTeamName, setEditTeamName] = useState("");
  const [editTeamDesc, setEditTeamDesc] = useState("");
  const [savingTeam, setSavingTeam] = useState(false);
  const canEditTeam = orgRole === "owner" || orgRole === "admin";
  const canDeleteTeam = canEditTeam && team && !team.is_general && !(/^general$/i).test(team.name || "");

  const openEditTeam = () => {
    setEditTeamName(team.name);
    setEditTeamDesc(team.description || "");
    setEditTeamOpen(true);
  };
  const saveTeam = async (e) => {
    e?.preventDefault?.();
    setSavingTeam(true);
    try {
      const { data } = await api.patch(`/teams/${teamId}`, {
        name: editTeamName.trim(),
        description: editTeamDesc.trim(),
      });
      setTeam(data);
      setEditTeamOpen(false);
      toast.success("Team saved");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Save failed");
    } finally { setSavingTeam(false); }
  };
  const removeTeam = async () => {
    if (!window.confirm(`Delete team "${team.name}" and all its conversations? This cannot be undone.`)) return;
    try {
      await api.delete(`/teams/${teamId}`);
      toast.success("Team deleted");
      nav(`/app/org/${team.org_id}`);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Delete failed");
    }
  };

  // Per-conversation delete (creator or org owner — backend enforces).
  // Spinner state is keyed by conv id so multi-clicks don't race.
  const [deletingConvId, setDeletingConvId] = useState("");
  const deleteConversation = async (e, c) => {
    e.preventDefault();
    e.stopPropagation();
    if (!c?.can_delete || deletingConvId) return;
    if (!window.confirm(`Permanently DELETE "${c.title || "this conversation"}"? This cannot be undone. (Use Archive if you only want to hide it.)`)) return;
    setDeletingConvId(c.id);
    try {
      await api.delete(`/conversations/${c.id}`);
      setConversations((prev) => prev.filter((x) => x.id !== c.id));
      toast.success("Conversation deleted");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not delete this conversation");
    } finally {
      setDeletingConvId("");
    }
  };

  // Per-conversation archive / unarchive — same permission rule.
  const [archivingConvId, setArchivingConvId] = useState("");
  const archiveConversation = async (e, c) => {
    e.preventDefault();
    e.stopPropagation();
    if (!c?.can_delete || archivingConvId) return;
    setArchivingConvId(c.id);
    try {
      await api.post(`/conversations/${c.id}/archive`);
      setConversations((prev) => prev.filter((x) => x.id !== c.id));
      toast.success("Archived — view under \"Archived\" toggle");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not archive this conversation");
    } finally {
      setArchivingConvId("");
    }
  };
  const unarchiveConversation = async (e, c) => {
    e.preventDefault();
    e.stopPropagation();
    if (!c?.can_delete || archivingConvId) return;
    setArchivingConvId(c.id);
    try {
      await api.post(`/conversations/${c.id}/unarchive`);
      setConversations((prev) => prev.filter((x) => x.id !== c.id));
      toast.success("Restored");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not restore this conversation");
    } finally {
      setArchivingConvId("");
    }
  };

  // Crew browser filters
  const [crewQ, setCrewQ] = useState("");
  const [crewDiv, setCrewDiv] = useState("");

  // new conversation state
  const [showNew, setShowNew] = useState(false);
  const [convType, setConvType] = useState("direct");
  const [convTitle, setConvTitle] = useState("");
  const [selAgents, setSelAgents] = useState([]);
  const [selMembers, setSelMembers] = useState([]);
  const [autoRespond, setAutoRespond] = useState(false);
  const [smartExpand, setSmartExpand] = useState(true);
  const [creatingConv, setCreatingConv] = useState(false);
  const [dlgQ, setDlgQ] = useState("");
  const [dlgDiv, setDlgDiv] = useState("");

  const load = async () => {
    setLoading(true);
    try {
      const teamRes = await api.get(`/teams/${teamId}`);
      const archParam = showArchived ? "true" : "false";
      const [agentsRes, convsRes, membersRes, divsRes] = await Promise.all([
        api.get(`/teams/${teamId}/agents`),
        api.get(`/teams/${teamId}/conversations`, { params: { archived: archParam } }),
        api.get(`/orgs/${teamRes.data.org_id}/members`),
        api.get(`/agents/divisions`),
      ]);
      setTeam(teamRes.data);
      setSp(teamRes.data.soulprint || sp);
      setAgents(agentsRes.data);
      setConversations(convsRes.data);
      setMembers(membersRes.data);
      setDivisions(divsRes.data);
      // Org role drives edit/delete permissions on the team
      try {
        const orgRes = await api.get(`/orgs/${teamRes.data.org_id}`);
        setOrgRole(orgRes.data.role || "");
      } catch (_) { setOrgRole(""); }
    } catch (err) {
      toast.error("Failed to load team");
      nav("/app");
    } finally { setLoading(false); }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [teamId, showArchived]);

  const saveSoulPrint = async () => {
    setSavingSP(true);
    try {
      const { data } = await api.put(`/teams/${teamId}/soulprint`, sp);
      setTeam(data);
      setSp(data.soulprint || sp);
      toast.success("SoulPrint saved");
    } catch (err) { toast.error("Failed to save"); }
    finally { setSavingSP(false); }
  };

  const toggleAgent = (id) => {
    if (convType === "direct") setSelAgents([id]);
    else setSelAgents((prev) => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  };

  const toggleMember = (id) => {
    setSelMembers((prev) => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  };

  const resetDialog = () => {
    setShowNew(false); setConvType("direct"); setConvTitle("");
    setSelAgents([]); setSelMembers([]); setAutoRespond(false); setSmartExpand(true);
    setDlgQ(""); setDlgDiv("");
  };

  const createConv = async () => {
    if (selAgents.length === 0) { toast.error("Pick at least one agent"); return; }
    if (convType === "direct" && selAgents.length !== 1) { toast.error("Direct chat needs exactly one agent"); return; }
    setCreatingConv(true);
    try {
      const { data } = await api.post(`/teams/${teamId}/conversations`, {
        title: convTitle,
        type: convType,
        agent_ids: selAgents,
        member_ids: convType === "group" ? selMembers : [],
        auto_respond: convType === "group" ? autoRespond : false,
        smart_expand: convType === "group" ? smartExpand : false,
      });
      toast.success("Conversation created");
      nav(`/app/team/${teamId}/c/${data.id}`);
    } catch (err) { toast.error(err?.response?.data?.detail || "Failed"); }
    finally { setCreatingConv(false); }
  };

  const agentMap = useMemo(() => Object.fromEntries(agents.map(a => [a.id, a])), [agents]);

  const filterAgents = (list, q, div) => {
    let arr = list;
    if (div) arr = arr.filter(a => a.division_key === div);
    if (q && q.trim()) {
      const n = q.trim().toLowerCase();
      arr = arr.filter(a =>
        a.id.includes(n) ||
        (a.name || "").toLowerCase().includes(n) ||
        (a.description || "").toLowerCase().includes(n) ||
        (a.division || "").toLowerCase().includes(n)
      );
    }
    return arr;
  };

  const crew = useMemo(() => filterAgents(agents, crewQ, crewDiv), [agents, crewQ, crewDiv]);
  const dlgAgents = useMemo(() => filterAgents(agents, dlgQ, dlgDiv), [agents, dlgQ, dlgDiv]);

  const CREW_LIMIT = 36;
  const crewShown = crew.slice(0, CREW_LIMIT);
  const DLG_LIMIT = 60;
  const dlgShown = dlgAgents.slice(0, DLG_LIMIT);

  // Smart-start composer
  const [smartText, setSmartText] = useState("");
  const [smartBusy, setSmartBusy] = useState(false);
  // File staging for the Smart-Start composer. Mirrors the lighter
  // staging tray used on the Org page's Chief-of-Staff card — paperclip
  // picker + drag-and-drop + paste-from-clipboard, capped at 6 files
  // × 25 MB each (server enforces the upper bound).
  const [smartStaged, setSmartStaged] = useState([]);
  const [smartDragging, setSmartDragging] = useState(false);
  const smartFileRef = useRef(null);

  const stageSmartFiles = (incoming) => {
    const picked = Array.from(incoming || []).filter(Boolean);
    if (!picked.length) return false;
    if (smartStaged.length + picked.length > 6) {
      toast.error("Up to 6 files per message");
      return false;
    }
    const tooBig = picked.find((f) => f.size > 25 * 1024 * 1024);
    if (tooBig) {
      toast.error(`${tooBig.name} is too large (max 25 MB)`);
      return false;
    }
    setSmartStaged((prev) => [
      ...prev,
      ...picked.map((f) => ({
        id: `stg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        file: f,
        kind: f.type?.startsWith("image/") ? "image"
              : f.type?.startsWith("video/") ? "video"
              : f.type?.startsWith("audio/") ? "audio"
              : "file",
        previewUrl: f.type?.startsWith("image/") ? URL.createObjectURL(f) : "",
      })),
    ]);
    return true;
  };
  const removeSmartStaged = (id) => setSmartStaged((prev) => {
    const tgt = prev.find((s) => s.id === id);
    if (tgt?.previewUrl) URL.revokeObjectURL(tgt.previewUrl);
    return prev.filter((s) => s.id !== id);
  });
  // Free preview blob URLs on unmount.
  useEffect(() => () => smartStaged.forEach(
    (s) => s.previewUrl && URL.revokeObjectURL(s.previewUrl),
  ), /* eslint-disable-next-line react-hooks/exhaustive-deps */ []);
  const onSmartDragOver = (e) => {
    if (e.dataTransfer?.types?.includes("Files")) {
      e.preventDefault();
      if (!smartDragging) setSmartDragging(true);
    }
  };
  const onSmartDrop = (e) => {
    if (!e.dataTransfer?.files?.length) return;
    e.preventDefault();
    setSmartDragging(false);
    stageSmartFiles(Array.from(e.dataTransfer.files));
  };
  const onSmartPaste = (e) => {
    const files = Array.from(e.clipboardData?.files || []);
    if (files.length && stageSmartFiles(files)) {
      e.preventDefault();
      toast.success(`Pasted ${files.length} file${files.length > 1 ? "s" : ""}`);
    }
  };
  const onSmartFilePick = (e) => {
    const picked = Array.from(e.target.files || []);
    e.target.value = "";
    stageSmartFiles(picked);
  };

  const onSmartStart = async (e) => {
    e?.preventDefault?.();
    const trimmed = smartText.trim();
    if (smartBusy) return;
    if (!trimmed && smartStaged.length === 0) return;
    setSmartBusy(true);
    try {
      if (smartStaged.length > 0) {
        // Files path: multipart endpoint that creates the conv +
        // routes the agent + posts the user message with attachments
        // + triggers the agent reply, all in one shot.
        const fd = new FormData();
        fd.append("text", smartText);
        smartStaged.forEach((s) => fd.append("files", s.file));
        const { data } = await api.post(`/teams/${teamId}/start-with-files`, fd, {
          headers: { "Content-Type": "multipart/form-data" },
        });
        toast.success(`Routed to ${agentMap[data.routed_agent_id]?.name || data.routed_agent_id}`);
        smartStaged.forEach((s) => s.previewUrl && URL.revokeObjectURL(s.previewUrl));
        setSmartStaged([]);
        setSmartText("");
        nav(`/app/team/${teamId}/c/${data.conversation.id}`);
      } else {
        const { data } = await api.post(`/teams/${teamId}/start`, { text: trimmed });
        toast.success(`Routed to ${agentMap[data.routed_agent_id]?.name || data.routed_agent_id}`);
        setSmartText("");
        nav(`/app/team/${teamId}/c/${data.conversation.id}`);
      }
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't route this — try again");
    } finally { setSmartBusy(false); }
  };

  if (loading || !team) return <div className="min-h-screen"><TopBar>Loading…</TopBar></div>;

  return (
    <div className="min-h-screen">
      <TopBar
        right={
          <button onClick={() => setShowNew(true)} className="sp-btn sp-btn-primary text-xs" data-testid="new-conversation-btn">
            <Plus className="w-3.5 h-3.5" /> New conversation
          </button>
        }
      >
        <Link to="/app" className="hover:text-white">Organizations</Link>
        <span className="mx-2 opacity-40">/</span>
        <Link to={`/app/org/${team.org_id}`} className="hover:text-white">{team.org_name || "Org"}</Link>
        <span className="mx-2 opacity-40">/</span>
        <span className="text-white/90">{team.name}</span>
      </TopBar>

      <main className="max-w-7xl mx-auto px-4 sm:px-6 py-8 sm:py-10">
        <div className="flex flex-col gap-2 mb-8">
          <span className="sp-chip">Team workspace</span>
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <h1 className="sp-display text-4xl md:text-5xl">{team.name}</h1>
            {canEditTeam && !editTeamOpen && (
              <div className="flex items-center gap-2">
                <button onClick={openEditTeam} className="sp-btn sp-btn-ghost text-xs" data-testid="edit-team-btn">
                  <Pencil className="w-3.5 h-3.5" /> Edit
                </button>
                {canDeleteTeam && (
                  <button onClick={removeTeam} className="sp-btn sp-btn-ghost text-xs hover:!text-red-300" data-testid="delete-team-btn">
                    <Trash2 className="w-3.5 h-3.5" /> Delete
                  </button>
                )}
              </div>
            )}
          </div>
          {team.description && <p className="sp-subtle">{team.description}</p>}
        </div>

        {editTeamOpen && (
          <form onSubmit={saveTeam} className="sp-glass-strong rounded-2xl p-6 mb-8 space-y-3" data-testid="edit-team-panel">
            <div className="flex items-center justify-between">
              <h3 className="text-sm uppercase tracking-widest sp-subtle">Edit team</h3>
              <button type="button" onClick={() => setEditTeamOpen(false)} className="text-white/60 hover:text-white"><XIcon className="w-4 h-4" /></button>
            </div>
            <input
              data-testid="edit-team-name"
              value={editTeamName}
              onChange={(e) => setEditTeamName(e.target.value)}
              placeholder="Team name"
              className="w-full rounded-xl px-4 py-3 text-sm focus:sp-focus"
              style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
            />
            <input
              data-testid="edit-team-desc"
              value={editTeamDesc}
              onChange={(e) => setEditTeamDesc(e.target.value)}
              placeholder="One-line description (optional)"
              className="w-full rounded-xl px-4 py-3 text-sm focus:sp-focus"
              style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
            />
            <div className="flex items-center justify-end gap-2 pt-1">
              <button type="button" onClick={() => setEditTeamOpen(false)} className="sp-btn sp-btn-ghost text-xs">Cancel</button>
              <button type="submit" disabled={savingTeam || !editTeamName.trim()} className="sp-btn sp-btn-primary text-xs disabled:opacity-50" data-testid="save-team-btn">
                <Save className="w-3.5 h-3.5" /> {savingTeam ? "Saving…" : "Save"}
              </button>
            </div>
          </form>
        )}

        <div className="grid lg:grid-cols-[1.4fr_1fr] gap-6">
          <div className="flex flex-col gap-6">
            {/* Smart Start composer */}
            <section
              className="relative sp-glass-strong rounded-2xl p-6"
              onDragOver={onSmartDragOver}
              onDragLeave={() => setSmartDragging(false)}
              onDrop={onSmartDrop}
            >
              {smartDragging && (
                <div
                  className="absolute inset-0 z-10 rounded-2xl border-2 border-dashed flex items-center justify-center pointer-events-none"
                  style={{
                    borderColor: "rgba(246,64,0,0.7)",
                    background: "color-mix(in srgb, var(--bg) 80%, transparent)",
                    backdropFilter: "blur(8px)",
                    WebkitBackdropFilter: "blur(8px)",
                  }}
                  data-testid="smart-start-drop-overlay"
                >
                  <div className="flex items-center gap-2 text-sm font-medium" style={{ color: "var(--fg)" }}>
                    <Paperclip className="w-4 h-4" /> Drop to attach to your new chat
                  </div>
                </div>
              )}
              <div className="flex items-center gap-2 mb-2">
                <Wand2 className="w-4 h-4 text-white/80" />
                <h2 className="text-sm uppercase tracking-widest sp-subtle">Just start typing</h2>
                <span className="ml-auto text-xs sp-subtle">We'll pick the right specialist for you</span>
              </div>
              <form onSubmit={onSmartStart} className="flex flex-col gap-3">
                {smartStaged.length > 0 && (
                  <div className="flex flex-wrap gap-2" data-testid="smart-start-staged-tray">
                    {smartStaged.map((s) => (
                      <div
                        key={s.id}
                        className="sp-glass rounded-lg px-3 py-1.5 flex items-center gap-2 text-xs"
                        data-testid={`smart-start-staged-${s.id}`}
                      >
                        {s.previewUrl ? (
                          <img src={s.previewUrl} alt="" className="w-6 h-6 rounded object-cover" />
                        ) : (
                          <FileIcon className="w-3.5 h-3.5 sp-subtle" />
                        )}
                        <span className="max-w-[180px] truncate" title={s.file.name}>{s.file.name}</span>
                        <button
                          type="button"
                          onClick={() => removeSmartStaged(s.id)}
                          className="sp-subtle hover:text-white"
                          data-testid={`smart-start-remove-${s.id}`}
                        >
                          <XIcon className="w-3 h-3" />
                        </button>
                      </div>
                    ))}
                  </div>
                )}
                <textarea
                  data-testid="smart-start-input"
                  value={smartText}
                  onChange={(e) => setSmartText(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) onSmartStart(e); }}
                  onPaste={onSmartPaste}
                  placeholder="What's on your mind? (e.g. 'Draft 3 LinkedIn hooks for our launch' or 'Why is our React dashboard slow?')"
                  rows={3}
                  className="w-full bg-white/[0.03] border border-white/10 rounded-xl px-4 py-3 text-sm text-white placeholder:text-white/30 focus:border-white/30 focus:sp-focus resize-none"
                />
                <input
                  ref={smartFileRef}
                  type="file"
                  multiple
                  hidden
                  onChange={onSmartFilePick}
                  data-testid="smart-start-file-input"
                />
                <div className="flex items-center justify-between gap-2 flex-wrap">
                  <div className="flex items-center gap-3">
                    <button
                      type="button"
                      onClick={() => smartFileRef.current?.click()}
                      className="sp-btn sp-btn-ghost text-xs"
                      title="Attach a file (paperclip · drag · paste)"
                      data-testid="smart-start-attach-btn"
                    >
                      <Paperclip className="w-3.5 h-3.5" /> Attach
                    </button>
                    <button
                      type="button"
                      onClick={() => setShowNew(true)}
                      className="text-xs sp-subtle hover:text-white inline-flex items-center gap-1"
                      data-testid="open-manual-picker"
                    >
                      Or pick the agent manually <ArrowRight className="w-3 h-3" />
                    </button>
                  </div>
                  <button
                    type="submit"
                    disabled={(!smartText.trim() && smartStaged.length === 0) || smartBusy}
                    className="sp-btn sp-btn-primary text-sm disabled:opacity-50"
                    data-testid="smart-start-btn"
                  >
                    {smartBusy ? "Routing…" : <>Start conversation <ArrowRight className="w-3.5 h-3.5" /></>}
                  </button>
                </div>
              </form>
            </section>

            <section>
              <div className="flex items-center justify-between mb-3 gap-2 flex-wrap">
                <h2 className="text-sm uppercase tracking-widest sp-subtle flex items-center gap-2">
                  <MessageCircle className="w-4 h-4" /> Conversations
                </h2>
                <div className="inline-flex rounded-lg overflow-hidden border border-white/10" role="tablist" aria-label="Conversation filter">
                  <button
                    type="button"
                    role="tab"
                    aria-selected={!showArchived}
                    onClick={() => setShowArchived(false)}
                    className={`text-xs px-3 py-1.5 transition ${!showArchived ? "bg-white/10 text-white" : "sp-subtle hover:bg-white/5"}`}
                    data-testid="conv-filter-active"
                  >
                    Active
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={showArchived}
                    onClick={() => setShowArchived(true)}
                    className={`text-xs px-3 py-1.5 transition inline-flex items-center gap-1 ${showArchived ? "bg-white/10 text-white" : "sp-subtle hover:bg-white/5"}`}
                    data-testid="conv-filter-archived"
                  >
                    <Archive className="w-3 h-3" /> Archived
                  </button>
                </div>
              </div>
              <div className="grid sm:grid-cols-2 gap-3">
                {conversations.map((c) => {
                  const ag = c.agent_ids.map(id => agentMap[id]?.name).filter(Boolean).join(", ");
                  return (
                    <div key={c.id} className={`relative group/conv ${c.is_archived ? "opacity-75" : ""}`}>
                      <Link
                        to={`/app/team/${teamId}/c/${c.id}`}
                        className="sp-glass rounded-xl p-4 hover:border-white/20 transition block"
                        data-testid={`conv-card-${c.id}`}
                      >
                        <div className="flex items-center justify-between mb-1 gap-2">
                          <div className="text-white text-sm font-medium truncate pr-16 flex items-center gap-1.5">
                            {c.is_archived && (
                              <Archive className="w-3 h-3 shrink-0 sp-subtle" aria-label="Archived" />
                            )}
                            {c.title || "Untitled"}
                          </div>
                          {(c.member_ids?.length || 0) > 1 ? (
                            <span className="sp-chip !bg-white/5">Shared · {c.member_ids.length}</span>
                          ) : (
                            <span className="sp-chip !bg-emerald-400/10 !text-emerald-200/90">Private</span>
                          )}
                        </div>
                        <div className="text-xs sp-subtle truncate">{ag || "—"}</div>
                        {c.type === "group" && (
                          <div className="text-[10px] sp-subtle mt-1 uppercase tracking-widest">
                            {c.auto_respond ? "auto-respond" : "@mention to trigger"}
                            {c.smart_expand ? " · smart-expand" : ""}
                          </div>
                        )}
                      </Link>
                      {c.can_delete && (
                        <div className="absolute top-2 right-2 flex items-center gap-1 opacity-0 group-hover/conv:opacity-100 focus-within:opacity-100 transition">
                          {c.is_archived ? (
                            <button
                              type="button"
                              onClick={(e) => unarchiveConversation(e, c)}
                              disabled={archivingConvId === c.id}
                              className="rounded-md p-1.5 hover:bg-emerald-500/15 disabled:opacity-60 text-emerald-300"
                              title="Restore this conversation"
                              aria-label={`Restore conversation ${c.title}`}
                              data-testid={`conv-card-unarchive-${c.id}`}
                            >
                              {archivingConvId === c.id
                                ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                : <ArchiveRestore className="w-3.5 h-3.5" />}
                            </button>
                          ) : (
                            <button
                              type="button"
                              onClick={(e) => archiveConversation(e, c)}
                              disabled={archivingConvId === c.id}
                              className="rounded-md p-1.5 hover:bg-amber-500/15 disabled:opacity-60 sp-subtle hover:text-amber-300"
                              title="Archive — hides from default lists but keeps memory"
                              aria-label={`Archive conversation ${c.title}`}
                              data-testid={`conv-card-archive-${c.id}`}
                            >
                              {archivingConvId === c.id
                                ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                : <Archive className="w-3.5 h-3.5" />}
                            </button>
                          )}
                          <button
                            type="button"
                            onClick={(e) => deleteConversation(e, c)}
                            disabled={deletingConvId === c.id}
                            className="rounded-md p-1.5 hover:bg-red-500/15 disabled:opacity-60"
                            style={{ color: "rgb(239 68 68)" }}
                            title={c.is_creator ? "Permanently delete" : "Permanently delete (as org owner)"}
                            aria-label={`Delete conversation ${c.title}`}
                            data-testid={`conv-card-delete-${c.id}`}
                          >
                            {deletingConvId === c.id
                              ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                              : <Trash2 className="w-3.5 h-3.5" />}
                          </button>
                        </div>
                      )}
                    </div>
                  );
                })}
                {conversations.length === 0 && (
                  <div className="sp-glass rounded-xl p-6 col-span-full text-center text-sm sp-subtle">
                    {showArchived
                      ? "No archived conversations yet. Archive a thread to keep its memory but hide it from the active list."
                      : "Start a new conversation to meet your crew."}
                  </div>
                )}
              </div>
            </section>

            <SchedulesPanel scope="team" targetId={teamId} />

            <section>
              <h2 className="text-sm uppercase tracking-widest sp-subtle flex items-center gap-2 mb-3">
                <Logo className="w-4 h-4" /> Your crew
                <span className="ml-auto text-white/60">{crew.length} of {agents.length}</span>
              </h2>
              <AgentFilter
                q={crewQ} setQ={setCrewQ}
                divisions={divisions}
                division={crewDiv} setDivision={setCrewDiv}
                total={agents.length}
                testPrefix="crew-"
              />
              <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {crewShown.map(a => <AgentBadge key={a.id} a={a} testPrefix="agent-crew" />)}
              </div>
              {crew.length > CREW_LIMIT && (
                <div className="text-xs sp-subtle mt-3 text-center">
                  Showing first {CREW_LIMIT} of {crew.length} — refine your search.
                </div>
              )}
              {crew.length === 0 && (
                <div className="sp-glass rounded-xl p-6 text-center text-sm sp-subtle">
                  No agents match that filter.
                </div>
              )}
            </section>
          </div>

          <aside className="sp-glass-strong rounded-2xl p-6 h-fit sticky top-6">
            <div className="flex items-center gap-2 mb-1">
              <Radio className="w-4 h-4 text-white/80" />
              <h3 className="text-sm uppercase tracking-widest sp-subtle">Team SoulPrint</h3>
            </div>
            <p className="text-xs sp-subtle mb-4">Brief your agents once. Every conversation on this team uses these signals.</p>
            <div className="flex flex-col gap-3">
              {[
                ["mission", "Mission", "Why does this team exist?"],
                ["audience", "Audience", "Who are we speaking to?"],
                ["tone", "Tone of voice", "Playful, precise, founder-forward…"],
                ["values", "Values", "What we stand for"],
                ["north_star", "North star", "The one metric / outcome"],
              ].map(([key, label, ph]) => (
                <div key={key}>
                  <label className="text-[11px] sp-subtle uppercase tracking-widest">{label}</label>
                  <textarea
                    data-testid={`sp-${key}`}
                    value={sp[key] || ""}
                    onChange={(e) => setSp((s) => ({ ...s, [key]: e.target.value }))}
                    placeholder={ph}
                    rows={key === "mission" || key === "values" ? 3 : 2}
                    className="w-full mt-1 bg-white/[0.03] border border-white/10 rounded-xl px-3 py-2 text-sm text-white placeholder:text-white/30 focus:border-white/30 focus:sp-focus resize-none"
                  />
                </div>
              ))}
            </div>
            <button
              onClick={saveSoulPrint}
              disabled={savingSP}
              className="sp-btn sp-btn-primary w-full justify-center mt-4 disabled:opacity-50"
              data-testid="save-soulprint-btn"
            >
              {savingSP ? "Saving…" : "Save SoulPrint"}
            </button>
          </aside>
        </div>
      </main>

      {showNew && (
        <div className="fixed inset-0 z-40 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm" onClick={resetDialog}>
          <div className="sp-glass-strong rounded-2xl w-full max-w-3xl p-6 sp-fade-up max-h-[90vh] overflow-hidden flex flex-col" onClick={(e) => e.stopPropagation()} data-testid="new-conv-dialog">
            <h3 className="sp-display text-2xl mb-1">New conversation</h3>
            <p className="text-xs sp-subtle mb-4">Chat 1:1 with an agent, or spin up a group with teammates. You have {agents.length} specialists to pick from.</p>

            <div className="flex items-center gap-2 mb-4">
              <button
                onClick={() => { setConvType("direct"); setSelAgents(selAgents.slice(0, 1)); }}
                className={`sp-btn text-xs ${convType === "direct" ? "sp-btn-primary" : "sp-btn-ghost"}`}
                data-testid="conv-type-direct"
              >Direct</button>
              <button
                onClick={() => setConvType("group")}
                className={`sp-btn text-xs ${convType === "group" ? "sp-btn-primary" : "sp-btn-ghost"}`}
                data-testid="conv-type-group"
              ><Users className="w-3 h-3" /> Group</button>
            </div>

            <label className="text-[11px] sp-subtle uppercase tracking-widest">Title (optional)</label>
            <input
              data-testid="new-conv-title"
              value={convTitle}
              onChange={(e) => setConvTitle(e.target.value)}
              placeholder={convType === "direct" ? "e.g. Launch copy review" : "e.g. Q3 planning"}
              className="w-full mt-1 mb-4 bg-white/[0.03] border border-white/10 rounded-xl px-4 py-2.5 text-sm text-white placeholder:text-white/30 focus:border-white/30"
            />

            {/* Scrollable middle — agent picker + group invite. The action
                buttons below stay pinned to the bottom of the dialog so they
                are always reachable, even on short viewports. */}
            <div className="flex-1 min-h-0 overflow-y-auto pr-1 -mr-2 flex flex-col">
            {selAgents.length > 0 && (
              <div className="flex flex-wrap gap-2 mb-3">
                {selAgents.map(id => {
                  const a = agentMap[id];
                  if (!a) return null;
                  return (
                    <span key={id} className="sp-chip !bg-white/10 !text-white" data-testid={`selected-agent-${id}`}>
                      <span className="sp-dot" style={{ background: a.accent }} /> {a.name}
                      <button onClick={() => toggleAgent(id)} className="ml-1 text-white/60 hover:text-white"><X className="w-3 h-3" /></button>
                    </span>
                  );
                })}
              </div>
            )}

            <label className="text-[11px] sp-subtle uppercase tracking-widest mb-2">
              {convType === "direct" ? "Pick one agent" : `Pick agents (${selAgents.length} selected)`}
            </label>
            <AgentFilter
              q={dlgQ} setQ={setDlgQ}
              divisions={divisions}
              division={dlgDiv} setDivision={setDlgDiv}
              total={agents.length}
              testPrefix="dlg-"
            />

            <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-2 pb-1">
              {dlgShown.map(a => (
                <AgentBadge key={a.id} a={a} onClick={() => toggleAgent(a.id)} selected={selAgents.includes(a.id)} />
              ))}
              {dlgShown.length === 0 && (
                <div className="col-span-full sp-glass rounded-xl p-4 text-center text-xs sp-subtle">No agents match.</div>
              )}
            </div>
            {dlgAgents.length > DLG_LIMIT && (
              <div className="text-[11px] sp-subtle text-center mt-2">
                Showing first {DLG_LIMIT} of {dlgAgents.length} — refine your search to see more.
              </div>
            )}

            {convType === "group" && (
              <div className="mt-4 border-t border-white/5 pt-4">
                <label className="text-[11px] sp-subtle uppercase tracking-widest">Invite teammates</label>
                <div className="flex flex-wrap gap-2 mt-2 mb-3">
                  {members.map(m => (
                    <button
                      key={m.user_id}
                      onClick={() => toggleMember(m.user_id)}
                      className={`sp-chip hover:text-white ${selMembers.includes(m.user_id) ? "!bg-white/15 !text-white" : ""}`}
                      data-testid={`select-member-${m.user_id}`}
                    >
                      {m.name}
                    </button>
                  ))}
                  {members.length <= 1 && <span className="text-xs sp-subtle">Only you are in this org. Invite teammates from the org page.</span>}
                </div>

                <label className="flex items-center gap-3 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={autoRespond}
                    onChange={(e) => setAutoRespond(e.target.checked)}
                    className="accent-orange-500"
                    data-testid="auto-respond-toggle"
                  />
                  <span className="text-sm text-white">Agents auto-respond on every message</span>
                  <span className="text-xs sp-subtle">(otherwise @mention the agent)</span>
                </label>
                <label className="flex items-center gap-3 cursor-pointer mt-2">
                  <input
                    type="checkbox"
                    checked={smartExpand}
                    onChange={(e) => setSmartExpand(e.target.checked)}
                    className="accent-orange-500"
                    data-testid="smart-expand-toggle"
                  />
                  <span className="text-sm text-white">Auto-add new specialists when the topic shifts</span>
                  <span className="text-xs sp-subtle">(smart expansion)</span>
                </label>
              </div>
            )}
            </div>

            <div className="flex items-center justify-end gap-2 mt-4 border-t border-white/5 pt-4 shrink-0">
              <button onClick={resetDialog} className="sp-btn sp-btn-ghost text-sm" data-testid="cancel-new-conv">Cancel</button>
              <button
                onClick={createConv}
                disabled={creatingConv || selAgents.length === 0}
                className="sp-btn sp-btn-primary text-sm disabled:opacity-50"
                data-testid="submit-new-conv"
              >
                {creatingConv ? "Creating…" : `Start ${convType} conversation`}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
