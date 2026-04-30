import React, { useEffect, useMemo, useRef, useState } from "react";
import { useParams, Link, useNavigate } from "react-router-dom";
import api, { BACKEND_URL } from "../lib/api";
import TopBar from "../components/TopBar";
import { toast } from "sonner";
import { useAuth } from "../context/AuthContext";
import { Send, Trash2, AtSign, Users, Repeat, Search, X, Lock, Film, Globe, ArrowUp, ArrowDown, UserPlus, Paperclip, FileText, File as FileIcon, Download, CornerDownRight, Megaphone } from "lucide-react";
import LinkPreviewCard from "../components/LinkPreviewCard";

const formatTime = (ts) => {
  try { return new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
  catch { return ""; }
};

export default function ConversationPage() {
  const { teamId, convId } = useParams();
  const { user } = useAuth();
  const nav = useNavigate();
  const [team, setTeam] = useState(null);
  const [conv, setConv] = useState(null);
  const [agents, setAgents] = useState([]);
  const [messages, setMessages] = useState([]);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [loading, setLoading] = useState(true);
  const [showMentions, setShowMentions] = useState(false);
  const scrollerRef = useRef(null);
  const inputRef = useRef(null);
  const fileInputRef = useRef(null);
  const [uploading, setUploading] = useState(false);
  // Multi-file staging tray. Files picked via the paperclip button accumulate
  // here as { file: File, previewUrl: string, kind: 'image'|'video'|'file', id }
  // until the user hits send. This way you can upload a logo + a background
  // and ask "place the logo on the background" in one shot.
  const [staged, setStaged] = useState([]);
  const [scrollState, setScrollState] = useState({ canUp: false, canDown: false });

  const onScrollerScroll = () => {
    const el = scrollerRef.current;
    if (!el) return;
    setScrollState({
      canUp: el.scrollTop > 60,
      canDown: el.scrollTop + el.clientHeight < el.scrollHeight - 60,
    });
  };
  const scrollTo = (where) => {
    const el = scrollerRef.current;
    if (!el) return;
    el.scrollTo({
      top: where === "top" ? 0 : el.scrollHeight,
      behavior: "smooth",
    });
  };

  const agentMap = useMemo(() => Object.fromEntries(agents.map(a => [a.id, a])), [agents]);

  const load = async () => {
    setLoading(true);
    try {
      const convRes = await api.get(`/conversations/${convId}`);
      const [teamRes, agentsRes, msgsRes] = await Promise.all([
        api.get(`/teams/${convRes.data.team_id}`),
        api.get(`/teams/${convRes.data.team_id}/agents`),
        api.get(`/conversations/${convId}/messages`),
      ]);
      setConv(convRes.data);
      setTeam(teamRes.data);
      setAgents(agentsRes.data);
      setMessages(msgsRes.data);
    } catch (err) {
      toast.error("Failed to load conversation");
      nav(`/app/team/${teamId}`);
    } finally { setLoading(false); }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [convId]);

  useEffect(() => {
    if (scrollerRef.current) {
      scrollerRef.current.scrollTop = scrollerRef.current.scrollHeight;
    }
    onScrollerScroll();
  }, [messages, sending]);

  // Pure "just type" composer: no manual image/video toggles. Backend
  // auto-detects intent (keyword fast-path + LLM fallback) and either
  // renders an image, kicks off a video, or replies with text.

  // Poll the message list every 5s while any video is still rendering.
  // Kie.ai usually finishes within 60-180s so we cap at ~5 min of polling.
  useEffect(() => {
    const hasPending = messages.some(
      (m) => m.media_type === "video" && m.media_status === "pending"
    );
    if (!hasPending) return;
    let cancelled = false;
    let attempts = 0;
    const tick = async () => {
      if (cancelled) return;
      attempts += 1;
      try {
        const { data } = await api.get(`/conversations/${convId}/messages`);
        if (cancelled) return;
        setMessages(data);
      } catch {/* swallow */}
      if (attempts < 60) setTimeout(tick, 5000);
    };
    const t = setTimeout(tick, 5000);
    return () => { cancelled = true; clearTimeout(t); };
  }, [messages, convId]);

  // Shared staging logic — used by the paperclip picker, paste handler, and
  // drag-and-drop. Validates size + count, builds previews, and appends to
  // the staging tray. Returns true when at least one file was staged so the
  // paste handler knows whether to swallow the event.
  const stageFiles = (incoming) => {
    const picked = Array.from(incoming || []).filter(Boolean);
    if (!picked.length) return false;
    const tooBig = picked.find((f) => f.size > 25 * 1024 * 1024);
    if (tooBig) { toast.error(`${tooBig.name} is too large (max 25 MB)`); return false; }
    if (staged.length + picked.length > 6) {
      toast.error("You can attach at most 6 files per message");
      return false;
    }
    const additions = picked.map((f) => ({
      // Paste-from-clipboard images sometimes come through with a generic
      // `image.png` filename — give them a unique-ish name so the user can
      // tell them apart in the staging tray.
      id: `stg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      file: f.name && f.name !== "image.png"
        ? f
        : new File([f], `pasted-${new Date().toISOString().replace(/[:.]/g, "-")}.${(f.type.split("/")[1] || "png")}`, { type: f.type }),
      previewUrl: f.type.startsWith("image/") ? URL.createObjectURL(f) : "",
      kind: f.type.startsWith("image/") ? "image"
            : f.type.startsWith("video/") ? "video"
            : f.type.startsWith("audio/") ? "audio"
            : "file",
    }));
    setStaged((prev) => [...prev, ...additions]);
    return true;
  };

  // The paperclip button now STAGES files instead of uploading immediately.
  // The user can pick multiple at once (or click again to add more), then
  // type a prompt and hit send — all files go up together in one request.
  const onFilesPicked = (e) => {
    const picked = Array.from(e.target.files || []);
    e.target.value = "";  // reset so the same file can be re-picked
    stageFiles(picked);
  };

  // Pasting files (e.g. ⌘V a screenshot, copied PDF, attached image from
  // another app) — `clipboardData.files` is non-empty whenever the OS has
  // actual file blobs on the clipboard. We swallow the event so the textarea
  // doesn't get a stray "[image]" placeholder and instead drop the file(s)
  // into the staging tray, ready to send with the next prompt.
  const onComposerPaste = (e) => {
    const files = Array.from(e.clipboardData?.files || []);
    if (!files.length) return;  // plain-text paste — let the browser handle it
    if (stageFiles(files)) {
      e.preventDefault();
      toast.success(`Pasted ${files.length} file${files.length > 1 ? "s" : ""}`);
    }
  };

  // Drag-and-drop onto the composer — same staging path as paste.
  const [isDragging, setIsDragging] = useState(false);
  const onComposerDragOver = (e) => {
    if (e.dataTransfer?.types?.includes("Files")) {
      e.preventDefault();
      if (!isDragging) setIsDragging(true);
    }
  };
  const onComposerDragLeave = () => setIsDragging(false);
  const onComposerDrop = (e) => {
    if (!e.dataTransfer?.files?.length) return;
    e.preventDefault();
    setIsDragging(false);
    stageFiles(Array.from(e.dataTransfer.files));
  };

  const removeStaged = (id) => {
    setStaged((prev) => {
      const tgt = prev.find((s) => s.id === id);
      if (tgt?.previewUrl) URL.revokeObjectURL(tgt.previewUrl);
      return prev.filter((s) => s.id !== id);
    });
  };
  // Free preview URLs when the conversation unmounts to avoid leaking blobs
  useEffect(() => {
    return () => { staged.forEach((s) => s.previewUrl && URL.revokeObjectURL(s.previewUrl)); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const send = async (e) => {
    e.preventDefault();
    if (sending || uploading) return;
    if (!text.trim() && staged.length === 0) return;

    // ── Multi-file upload path ────────────────────────────────────────────
    if (staged.length > 0) {
      setUploading(true);
      const fd = new FormData();
      fd.append("text", text || "");
      staged.forEach((s) => fd.append("files", s.file));
      const optimistic = {
        id: `tmp-up-${Date.now()}`,
        conversation_id: convId,
        sender_type: "user",
        sender_id: user.id,
        sender_name: user.name,
        text: (text || `📎 ${staged.length} file${staged.length > 1 ? "s" : ""}`).trim(),
        attachments: staged.map((s) => ({
          filename: s.file.name,
          url: s.previewUrl,    // local blob URL for instant preview
          kind: s.kind,
          size: s.file.size,
          original_name: s.file.name,
        })),
        attachment_pending: true,
        timestamp: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, optimistic]);
      const wasText = text;
      const wasStaged = staged;
      setText("");
      setStaged([]);
      try {
        const { data } = await api.post(`/conversations/${convId}/upload`, fd, {
          headers: { "Content-Type": "multipart/form-data" },
        });
        setMessages((prev) => {
          const without = prev.filter((m) => m.id !== optimistic.id);
          return [...without, data.user_message, ...(data.agent_messages || [])];
        });
        if (data.conversation) setConv(data.conversation);
        // Free local blob URLs now that the server-served URLs are in play
        wasStaged.forEach((s) => s.previewUrl && URL.revokeObjectURL(s.previewUrl));
      } catch (err) {
        toast.error(err?.response?.data?.detail || "Upload failed");
        setMessages((prev) => prev.filter((m) => m.id !== optimistic.id));
        setText(wasText);
        setStaged(wasStaged);
      } finally { setUploading(false); }
      return;
    }

    // ── Text-only path (existing behaviour) ───────────────────────────────
    setSending(true);
    const optimistic = {
      id: `tmp-${Date.now()}`,
      conversation_id: convId,
      sender_type: "user",
      sender_id: user.id,
      sender_name: user.name,
      text,
      timestamp: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, optimistic]);
    const toSend = text;
    setText("");
    try {
      const { data } = await api.post(`/conversations/${convId}/messages`, { text: toSend });
      setMessages((prev) => {
        const withoutOptim = prev.filter(m => m.id !== optimistic.id);
        return [...withoutOptim, data.user_message, ...data.agent_messages];
      });
      if (data.conversation) setConv(data.conversation);
    } catch (err) {
      // Surface the actual reason instead of a generic toast so the user
      // can act on it (top up budget, retry, etc.). Special-case the
      // Emergent LLM "Budget has been exceeded" error since the fix is a
      // one-click top-up in the user's profile.
      const detail = err?.response?.data?.detail || err?.message || "";
      const txt = String(detail);
      if (/budget/i.test(txt)) {
        toast.error(
          "Your Emergent LLM key budget is empty. Profile → Universal Key → Add Balance, then try again.",
          { duration: 8000 },
        );
      } else if (txt) {
        toast.error(`Message failed: ${txt}`.slice(0, 220));
      } else {
        toast.error("Message failed — check your connection and try again.");
      }
      setMessages((prev) => prev.filter(m => m.id !== optimistic.id));
      setText(toSend);
    } finally { setSending(false); }
  };

  const slugForMention = (a) => {
    if (!a) return "";
    if (a.division_key && a.id.startsWith(a.division_key + "-")) {
      return a.id.slice(a.division_key.length + 1);
    }
    return a.id;
  };

  const insertMention = (a) => {
    const slug = slugForMention(a);
    const at = text.endsWith(" ") || text.length === 0 ? "" : " ";
    setText((prev) => `${prev}${at}@${slug} `);
    setShowMentions(false);
    inputRef.current?.focus();
  };

  const clearConv = async () => {
    if (!window.confirm("Delete this conversation? This cannot be undone.")) return;
    try {
      await api.delete(`/conversations/${convId}`);
      toast.success("Conversation deleted");
      nav(`/app/team/${teamId}`);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed");
    }
  };

  // ── Fork-to-direct-chat ──────────────────────────────────────────────────
  // Click the "↳ Continue privately" button on any agent bubble to spin up a
  // new direct chat with just that agent, seeded with their reply. Lives in
  // the org's auto-managed "Direct chats" team and shows up in the sidebar
  // like any other thread.
  const [forkingId, setForkingId] = useState("");
  const forkFromMessage = async (m) => {
    if (forkingId) return;
    setForkingId(m.id);
    try {
      const { data } = await api.post(`/conversations/${convId}/fork`, { message_id: m.id });
      toast.success(`Continuing privately with ${data.seed_message.sender_name}`);
      nav(`/app/team/${data.team.id}/c/${data.conversation.id}`);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't fork conversation");
    } finally { setForkingId(""); }
  };

  // ── Queue agent reply for social ─────────────────────────────────────────
  // Sibling of the fork button — drops the agent's reply into the org's
  // Social Queue as a draft post for the user to schedule + publish.
  const [queueingSocialId, setQueueingSocialId] = useState("");
  const queueForSocial = async (m) => {
    if (queueingSocialId) return;
    setQueueingSocialId(m.id);
    try {
      await api.post(`/conversations/${convId}/messages/${m.id}/queue-social`);
      const orgIdFromTeam = team?.org_id;
      toast.success("Added to Social Queue", {
        action: orgIdFromTeam ? {
          label: "Open queue",
          onClick: () => nav(`/app/org/${orgIdFromTeam}/social`),
        } : undefined,
      });
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't queue for social");
    } finally { setQueueingSocialId(""); }
  };

  // Switch-agent dialog (direct chats only)
  const [showSwitch, setShowSwitch] = useState(false);
  const [switchQ, setSwitchQ] = useState("");
  const [switchDiv, setSwitchDiv] = useState("");
  const [switchBusy, setSwitchBusy] = useState(false);
  const [divisions, setDivisions] = useState([]);

  // Manage-agents dialog state (group conversations only)
  const [showAgents, setShowAgents] = useState(false);
  const [agentsQ, setAgentsQ] = useState("");
  const [agentsDiv, setAgentsDiv] = useState("");
  const [agentsBusy, setAgentsBusy] = useState(false);
  useEffect(() => {
    if (!showSwitch && !showAgents) return;
    if (divisions.length) return;
    api.get("/agents/divisions").then((r) => setDivisions(r.data)).catch(() => {});
  }, [showSwitch, showAgents, divisions.length]);

  const filteredSwitchAgents = useMemo(() => {
    let arr = agents.filter((a) => !conv?.agent_ids?.includes(a.id));
    if (switchDiv) arr = arr.filter((a) => a.division_key === switchDiv);
    if (switchQ.trim()) {
      const n = switchQ.toLowerCase();
      arr = arr.filter((a) =>
        a.id.includes(n) ||
        (a.name || "").toLowerCase().includes(n) ||
        (a.description || "").toLowerCase().includes(n) ||
        (a.division || "").toLowerCase().includes(n)
      );
    }
    return arr.slice(0, 60);
  }, [agents, switchQ, switchDiv, conv]);

  const doSwitch = async (newAgentId) => {
    if (switchBusy) return;
    setSwitchBusy(true);
    try {
      const { data } = await api.post(`/conversations/${convId}/switch-agent`, { agent_id: newAgentId });
      setConv(data.conversation);
      setMessages((prev) => [...prev, data.user_message, data.agent_message]);
      setShowSwitch(false); setSwitchQ(""); setSwitchDiv("");
      toast.success(`Handed off to ${data.agent_message.sender_name}`);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Switch failed");
    } finally { setSwitchBusy(false); }
  };

  const filteredPickerAgents = useMemo(() => {
    let arr = agents.filter((a) => !conv?.agent_ids?.includes(a.id));
    if (agentsDiv) arr = arr.filter((a) => a.division_key === agentsDiv);
    if (agentsQ.trim()) {
      const n = agentsQ.toLowerCase();
      arr = arr.filter((a) =>
        a.id.includes(n) ||
        (a.name || "").toLowerCase().includes(n) ||
        (a.description || "").toLowerCase().includes(n) ||
        (a.division || "").toLowerCase().includes(n)
      );
    }
    return arr.slice(0, 60);
  }, [agents, agentsQ, agentsDiv, conv]);

  const addAgent = async (newAgentId) => {
    if (agentsBusy) return;
    setAgentsBusy(true);
    try {
      const { data } = await api.post(`/conversations/${convId}/agents`, { agent_id: newAgentId });
      setConv(data.conversation);
      setMessages((prev) => [...prev, data.system_message]);
      setAgentsQ(""); setAgentsDiv("");
      toast.success(`Added ${data.system_message.text.split("added ")[1]?.replace(" to the room.", "") || "agent"}`);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Add failed");
    } finally { setAgentsBusy(false); }
  };

  const removeAgent = async (aid) => {
    if (agentsBusy) return;
    setAgentsBusy(true);
    try {
      const { data } = await api.delete(`/conversations/${convId}/agents/${aid}`);
      setConv(data.conversation);
      setMessages((prev) => [...prev, data.system_message]);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Remove failed");
    } finally { setAgentsBusy(false); }
  };

  if (loading || !conv || !team) return <div className="min-h-screen"><TopBar>Loading…</TopBar></div>;

  const convAgents = conv.agent_ids.map(id => agentMap[id]).filter(Boolean);
  const isGroup = conv.type === "group";

  return (
    <div className="min-h-screen flex flex-col">
      <TopBar
        right={
          conv.created_by === user.id && (
            <button onClick={clearConv} className="sp-btn sp-btn-ghost text-xs" data-testid="delete-conv-btn">
              <Trash2 className="w-3.5 h-3.5" /> <span className="hidden md:inline">Delete</span>
            </button>
          )
        }
      >
        <Link to="/app" className="hover:text-white">Orgs</Link>
        <span className="mx-2 opacity-40">/</span>
        <Link to={`/app/org/${team.org_id}`} className="hover:text-white">{team.org_id.slice(-6)}</Link>
        <span className="mx-2 opacity-40">/</span>
        <Link to={`/app/team/${teamId}`} className="hover:text-white">{team.name}</Link>
        <span className="mx-2 opacity-40">/</span>
        <span className="text-white/90 truncate">{conv.title}</span>
      </TopBar>

      {/* Meta strip — sticky below the TopBar so "Switch agent" / "Manage
          agents" / privacy badges stay one click away no matter how far the
          user has scrolled. `top` tracks the TopBar's rendered height including
          the PWA notch inset; `z-30` layers below the TopBar's z-40. */}
      <div
        className="sticky z-30 border-b border-white/5 backdrop-blur-xl"
        style={{
          top: "calc(env(safe-area-inset-top) + 61px)",
          background: "color-mix(in srgb, var(--bg) 82%, transparent)",
        }}
      >
        <div className="max-w-5xl mx-auto px-4 sm:px-6 py-3 sm:py-4 flex items-center gap-3 flex-wrap">
          <span className="sp-chip">{conv.type}</span>
          {(() => {
            const others = (conv.member_ids || []).length - 1;
            return others > 0 ? (
              <span className="sp-chip !bg-white/5" data-testid="privacy-badge">
                <Users className="w-3 h-3" /> Shared with {conv.member_ids.length} people
              </span>
            ) : (
              <span className="sp-chip !bg-emerald-400/10 !text-emerald-200/90" data-testid="privacy-badge">
                <Lock className="w-3 h-3" /> Private to you
              </span>
            );
          })()}
          {convAgents.map((a) => (
            <span key={a.id} className="sp-chip !bg-white/5">
              <span className="sp-dot" style={{ background: a.accent }} />
              {a.emoji ? `${a.emoji} ` : ""}{a.name}
              <span className="opacity-60">· {a.division}</span>
            </span>
          ))}
          {isGroup && (
            <span className="sp-chip !bg-white/5">
              <Users className="w-3 h-3" /> {conv.member_ids.length} people
            </span>
          )}
          {isGroup && (
            <span className="sp-chip !bg-white/5">
              {conv.auto_respond ? "Auto-respond ON" : "Respond on @mention"}
            </span>
          )}
          {isGroup && conv.smart_expand && (
            <span className="sp-chip !bg-white/5">Smart expand</span>
          )}
          {!isGroup && (
            <button
              onClick={() => setShowSwitch(true)}
              className="sp-chip hover:!bg-white/15 hover:text-white inline-flex items-center gap-1"
              data-testid="switch-agent-btn"
              title="Hand off to another specialist"
            >
              <Repeat className="w-3 h-3" /> Switch agent
            </button>
          )}
          <button
            onClick={() => setShowAgents(true)}
            className="sp-chip hover:!bg-white/15 hover:text-white inline-flex items-center gap-1"
            data-testid="manage-agents-btn"
            title={isGroup
              ? "Add or remove specialists in this room"
              : "Add another specialist (turns this into a group chat)"}
          >
            <UserPlus className="w-3 h-3" /> {isGroup ? "Manage agents" : "Add agent"}
          </button>
        </div>
      </div>

      {/* Messages */}
      <main ref={scrollerRef} onScroll={onScrollerScroll} className="flex-1 overflow-y-auto relative">
        <div className="max-w-5xl mx-auto px-3 sm:px-6 py-6 sm:py-10 flex flex-col gap-4" data-testid="messages-container">
          {messages.length === 0 && (
            <div className="sp-glass rounded-2xl p-8 text-center">
              <div className="mb-1 text-lg">You're in.</div>
              <div className="text-base sp-subtle">
                {isGroup
                  ? <>Type a message. {conv.auto_respond ? "Every agent will respond." : <>Use <span style={{ color: "var(--fg)" }}>@agent-name</span> to call an agent.</>}</>
                  : <>Ask {convAgents[0]?.name} anything. They already know your team.</>}
              </div>
            </div>
          )}
          {messages.map((m) => {
            // System notifications (agent added/removed etc.) render as a
            // small centered chip rather than a chat bubble.
            if (m.sender_type === "system") {
              return (
                <div key={m.id} className="flex justify-center sp-fade-up" data-testid={`system-msg-${m.id}`}>
                  <span className="sp-chip text-[11px] !bg-white/5">{m.text}</span>
                </div>
              );
            }
            const mine = m.sender_type === "user" && m.sender_id === user.id;
            const isAgent = m.sender_type === "agent";
            const a = isAgent ? agentMap[m.agent_id] : null;
            // Only offer "Continue privately" on agent bubbles in non-direct
            // chats. We also need the agent to still exist (a?.id) and a
            // persisted message (no `tmp-` optimistic id).
            const canFork = isAgent && a && conv?.type !== "direct" && !String(m.id).startsWith("tmp-");
            // "Queue for social" works on any agent reply (incl. direct chats)
            // — anywhere there's content worth posting.
            const canSocial = isAgent && a && !String(m.id).startsWith("tmp-");
            return (
              <div key={m.id} className={`flex ${mine ? "justify-end" : "justify-start"} sp-fade-up group/msg`}>
                <div className="relative max-w-[92%] sm:max-w-[88%]">
                <div className={`rounded-2xl px-4 sm:px-5 py-3 sm:py-3.5 ${mine ? "sp-bubble-user" : "sp-bubble-agent"}`}>
                  <div className="flex items-center gap-2 mb-1.5">
                    {isAgent && a && <span className="sp-dot" style={{ background: a.accent }} />}
                    <span className="text-sm font-medium">
                      {isAgent ? (a?.name || "Agent") : (mine ? "You" : m.sender_name)}
                    </span>
                    {isAgent && a && <span className="text-[11px] sp-subtle uppercase tracking-widest">{a.division}</span>}
                    <span className="text-[11px] sp-subtle ml-auto">{formatTime(m.timestamp)}</span>
                  </div>
                  <div className="whitespace-pre-wrap text-[16px] leading-[1.6]">{m.text}</div>
                  {/* Multi-attachment renderer — handles both the new
                      `attachments` array and the legacy `attachment_*`
                      mirror fields (kept on the message for back-compat). */}
                  {(() => {
                    const list = Array.isArray(m.attachments) && m.attachments.length
                      ? m.attachments
                      : (m.attachment_filename
                          ? [{
                              filename: m.attachment_filename,
                              url: m.attachment_url,
                              kind: m.attachment_kind,
                              size: m.attachment_size,
                              original_name: m.attachment_original_name,
                            }]
                          : []);
                    if (!list.length) return null;
                    const imgs = list.filter((a) => a.kind === "image");
                    const others = list.filter((a) => a.kind !== "image");
                    const resolveUrl = (a) => {
                      if (!a.url) return "";
                      // local blob (optimistic) → use as-is; server URL → prefix backend
                      return a.url.startsWith("blob:") || a.url.startsWith("http")
                        ? a.url : `${BACKEND_URL}${a.url}`;
                    };
                    return (
                      <div className="mt-3 flex flex-col gap-2" data-testid={`attachments-${m.id}`}>
                        {imgs.length > 0 && (
                          <div className={imgs.length === 1
                            ? "rounded-xl overflow-hidden border border-white/10"
                            : "grid grid-cols-2 sm:grid-cols-3 gap-2"}>
                            {imgs.map((a, i) => (
                              <div key={`${m.id}-img-${i}`} className="relative group/media">
                                <a
                                  href={resolveUrl(a)}
                                  target="_blank" rel="noreferrer"
                                  className={imgs.length === 1
                                    ? "block hover:border-white/30 transition"
                                    : "block rounded-xl overflow-hidden border border-white/10 hover:border-white/30 transition aspect-square bg-black/30"}
                                  data-testid={`attachment-image-${m.id}-${i}`}
                                >
                                  <img
                                    src={resolveUrl(a)}
                                    alt={a.original_name || "attachment"}
                                    referrerPolicy="no-referrer"
                                    loading="lazy"
                                    className={imgs.length === 1
                                      ? "block w-full max-h-[480px] object-contain bg-black/30"
                                      : "w-full h-full object-cover"}
                                  />
                                </a>
                                {!a.url?.startsWith("blob:") && (
                                  <a
                                    href={resolveUrl(a)}
                                    download={a.original_name || ""}
                                    className="absolute top-2 right-2 sp-glass-strong rounded-full p-2 opacity-0 group-hover/media:opacity-100 hover:scale-105 transition shadow-lg"
                                    data-testid={`download-attachment-${m.id}-${i}`}
                                    title="Download"
                                  >
                                    <Download className="w-3.5 h-3.5" />
                                  </a>
                                )}
                              </div>
                            ))}
                          </div>
                        )}
                        {others.map((a, i) => {
                          if (a.kind === "video") return (
                            <div key={`${m.id}-v-${i}`} className="relative group/media">
                              <video
                                src={resolveUrl(a)}
                                controls preload="metadata"
                                className="block rounded-xl overflow-hidden border border-white/10 w-full max-h-[480px] bg-black/30"
                                data-testid={`attachment-video-${m.id}-${i}`}
                              />
                              {!a.url?.startsWith("blob:") && (
                                <a
                                  href={resolveUrl(a)}
                                  download={a.original_name || ""}
                                  className="absolute top-2 right-2 sp-glass-strong rounded-full p-2 opacity-0 group-hover/media:opacity-100 hover:scale-105 transition shadow-lg"
                                  data-testid={`download-attachment-video-${m.id}-${i}`}
                                  title="Download"
                                >
                                  <Download className="w-3.5 h-3.5" />
                                </a>
                              )}
                            </div>
                          );
                          if (a.kind === "audio") return (
                            <div key={`${m.id}-a-${i}`} className="flex items-center gap-2">
                              <audio
                                src={resolveUrl(a)}
                                controls preload="metadata"
                                className="flex-1"
                                data-testid={`attachment-audio-${m.id}-${i}`}
                              />
                              {!a.url?.startsWith("blob:") && (
                                <a
                                  href={resolveUrl(a)}
                                  download={a.original_name || ""}
                                  className="sp-btn sp-btn-ghost !p-2"
                                  data-testid={`download-attachment-audio-${m.id}-${i}`}
                                  title="Download"
                                >
                                  <Download className="w-3.5 h-3.5" />
                                </a>
                              )}
                            </div>
                          );
                          return (
                            <a
                              key={`${m.id}-f-${i}`}
                              href={resolveUrl(a)}
                              download={a.original_name || ""}
                              target="_blank" rel="noreferrer"
                              className="flex items-center gap-3 rounded-xl border border-white/10 hover:border-white/30 px-3 py-2.5 transition"
                              data-testid={`attachment-file-${m.id}-${i}`}
                            >
                              <div className="w-9 h-9 rounded-lg flex items-center justify-center sp-glass-strong shrink-0">
                                {a.kind === "pdf" || a.kind === "doc" || a.kind === "text"
                                  ? <FileText className="w-4 h-4" />
                                  : <FileIcon className="w-4 h-4" />}
                              </div>
                              <div className="min-w-0 flex-1">
                                <div className="text-sm font-medium truncate">{a.original_name || "Attachment"}</div>
                                <div className="text-[11px] sp-subtle">
                                  {(a.kind || "FILE").toUpperCase()}
                                  {a.size ? ` · ${(a.size / 1024).toFixed(0)} KB` : ""}
                                  {m.attachment_pending ? " · uploading…" : ""}
                                </div>
                              </div>
                              <Download className="w-4 h-4 opacity-50 shrink-0" />
                            </a>
                          );
                        })}
                      </div>
                    );
                  })()}
                  {Array.isArray(m.web_sources) && m.web_sources.length > 0 && (
                    <div className="mt-3 pt-3 border-t border-white/10" data-testid={`web-sources-${m.id}`}>
                      <div className="flex items-center gap-2 mb-1.5">
                        <Globe className="w-3 h-3 text-white/70" />
                        <span className="text-[10px] uppercase tracking-widest sp-subtle">Sources · {m.web_sources.length}</span>
                      </div>
                      <ol className="flex flex-col gap-1">
                        {m.web_sources.slice(0, 5).map((s, i) => (
                          <li key={`${m.id}-src-${i}`} className="min-w-0">
                            <LinkPreviewCard source={s} />
                          </li>
                        ))}
                      </ol>
                    </div>
                  )}
                  {m.media_type === "image" && m.media_url && (
                    <div className="mt-3 relative group/media" data-testid={`message-image-${m.id}`}>
                      <a
                        href={m.media_url.startsWith("http") ? m.media_url : `${BACKEND_URL}${m.media_url}`}
                        target="_blank" rel="noreferrer"
                        className="block rounded-xl overflow-hidden border border-white/10 hover:border-white/30 transition"
                      >
                        <img
                          src={m.media_url.startsWith("http") ? m.media_url : `${BACKEND_URL}${m.media_url}`}
                          alt="Generated"
                          referrerPolicy="no-referrer"
                          className="block w-full max-h-[520px] object-contain bg-black/30"
                          loading="lazy"
                        />
                      </a>
                      <a
                        href={m.media_url.startsWith("http") ? m.media_url : `${BACKEND_URL}${m.media_url}`}
                        download
                        className="absolute top-2 right-2 sp-glass-strong rounded-full p-2 opacity-0 group-hover/media:opacity-100 hover:scale-105 transition shadow-lg"
                        data-testid={`download-image-${m.id}`}
                        title="Download image"
                      >
                        <Download className="w-3.5 h-3.5" />
                      </a>
                    </div>
                  )}
                  {m.media_type === "video" && m.media_status === "pending" && (
                    <div className="mt-3 rounded-xl overflow-hidden border border-white/10 bg-black/40 aspect-video flex flex-col items-center justify-center gap-2"
                         data-testid={`message-video-pending-${m.id}`}>
                      <Film className="w-8 h-8 opacity-60 animate-pulse" />
                      <div className="text-xs sp-subtle">Rendering video — Veo 3.1 typically takes 60–180s.</div>
                    </div>
                  )}
                  {m.media_type === "video" && m.media_status === "ready" && m.media_url && (
                    <div className="mt-3 relative group/media" data-testid={`message-video-${m.id}`}>
                      <video
                        src={m.media_url}
                        controls
                        preload="metadata"
                        className="block rounded-xl overflow-hidden border border-white/10 w-full max-h-[480px] bg-black/30"
                      />
                      <a
                        href={m.media_url}
                        download
                        target="_blank" rel="noreferrer"
                        className="absolute top-2 right-2 sp-glass-strong rounded-full p-2 opacity-0 group-hover/media:opacity-100 hover:scale-105 transition shadow-lg"
                        data-testid={`download-video-${m.id}`}
                        title="Download video"
                      >
                        <Download className="w-3.5 h-3.5" />
                      </a>
                    </div>
                  )}
                  {m.media_type === "video" && m.media_status === "failed" && (
                    <div className="mt-3 rounded-xl border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-200/90"
                         data-testid={`message-video-failed-${m.id}`}>
                      Video generation failed. Try a different prompt or run it again.
                    </div>
                  )}
                </div>
                {(canFork || canSocial) && (
                  <div className="absolute -bottom-2 right-3 flex items-center gap-1.5 opacity-0 group-hover/msg:opacity-100 focus-within:opacity-100 transition">
                    {canSocial && (
                      <button
                        type="button"
                        onClick={() => queueForSocial(m)}
                        disabled={queueingSocialId === m.id}
                        title="Queue for social"
                        aria-label="Queue this reply for social"
                        data-testid={`social-msg-${m.id}`}
                        className="inline-flex items-center gap-1 sp-glass-strong rounded-full px-2.5 py-1 text-[11px] font-medium shadow-lg hover:scale-105 transition disabled:opacity-60"
                        style={{ color: "var(--fg)" }}
                      >
                        <Megaphone className="w-3 h-3" />
                        {queueingSocialId === m.id ? "Queuing…" : "Queue for social"}
                      </button>
                    )}
                    {canFork && (
                      <button
                        type="button"
                        onClick={() => forkFromMessage(m)}
                        disabled={forkingId === m.id}
                        title={`Continue privately with ${a.name}`}
                        aria-label={`Continue privately with ${a.name}`}
                        data-testid={`fork-msg-${m.id}`}
                        className="inline-flex items-center gap-1 sp-glass-strong rounded-full px-2.5 py-1 text-[11px] font-medium shadow-lg hover:scale-105 transition disabled:opacity-60"
                        style={{ color: "var(--fg)" }}
                      >
                        <CornerDownRight className="w-3 h-3" />
                        {forkingId === m.id ? "Forking…" : "Continue privately"}
                      </button>
                    )}
                  </div>
                )}
                </div>
              </div>
            );
          })}
          {sending && (
            <div className="flex justify-start">
              <div className="sp-bubble-agent rounded-2xl px-4 py-3">
                <div className="sp-typing"><span/><span/><span/></div>
              </div>
            </div>
          )}
        </div>
      </main>

      {/* Composer */}
      <div className="border-t border-white/5 bg-black/30 backdrop-blur-xl relative">
        {/* Floating scroll-to-top / scroll-to-latest arrows.
            They live in the composer wrapper (absolute, bottom-aligned) so
            they always pin to the viewport above the input bar regardless
            of conversation length, and never scroll with the messages. */}
        {scrollState.canUp && (
          <button
            type="button"
            onClick={() => scrollTo("top")}
            className="absolute right-3 sm:right-4 -top-12 sm:-top-14 z-20 sp-glass-strong rounded-full p-2.5 hover:scale-105 transition shadow-xl"
            data-testid="scroll-top-btn"
            title="Jump to top"
          >
            <ArrowUp className="w-4 h-4" />
          </button>
        )}
        {scrollState.canDown && (
          <button
            type="button"
            onClick={() => scrollTo("bottom")}
            className="absolute right-3 sm:right-4 -top-12 sm:-top-14 z-20 sp-glass-strong rounded-full p-2.5 hover:scale-105 transition shadow-xl"
            style={{ right: scrollState.canUp ? "4.25rem" : "0.75rem" }}
            data-testid="scroll-bottom-btn"
            title="Jump to latest"
          >
            <ArrowDown className="w-4 h-4" />
          </button>
        )}
        <form
          onSubmit={send}
          onDragOver={onComposerDragOver}
          onDragLeave={onComposerDragLeave}
          onDrop={onComposerDrop}
          className="relative max-w-5xl mx-auto px-3 sm:px-6 py-4"
        >
          {isDragging && (
            <div
              className="absolute inset-0 z-30 rounded-xl border-2 border-dashed flex items-center justify-center pointer-events-none"
              style={{
                borderColor: "rgba(246,64,0,0.7)",
                background: "color-mix(in srgb, var(--bg) 80%, transparent)",
                backdropFilter: "blur(8px)",
                WebkitBackdropFilter: "blur(8px)",
              }}
              data-testid="drop-overlay"
            >
              <div className="flex items-center gap-2 text-sm font-medium" style={{ color: "var(--fg)" }}>
                <Paperclip className="w-4 h-4" /> Drop to attach
              </div>
            </div>
          )}
          {staged.length > 0 && (
            <div
              className="flex flex-wrap items-stretch gap-2 mb-3 px-1"
              data-testid="staged-tray"
            >
              {staged.map((s) => (
                <div
                  key={s.id}
                  className="relative sp-glass rounded-xl overflow-hidden flex items-center gap-2 pr-2 group"
                  data-testid={`staged-${s.id}`}
                >
                  {s.kind === "image" && s.previewUrl ? (
                    <img src={s.previewUrl} alt={s.file.name}
                         className="w-14 h-14 object-cover" />
                  ) : (
                    <div className="w-14 h-14 flex items-center justify-center sp-glass-strong">
                      {s.kind === "video" ? <Film className="w-4 h-4" /> :
                        s.kind === "audio" ? <Paperclip className="w-4 h-4" /> :
                        <FileText className="w-4 h-4" />}
                    </div>
                  )}
                  <div className="text-[11px] leading-tight max-w-[140px]">
                    <div className="truncate" title={s.file.name}>{s.file.name}</div>
                    <div className="sp-subtle">{(s.file.size / 1024).toFixed(0)} KB</div>
                  </div>
                  <button
                    type="button"
                    onClick={() => removeStaged(s.id)}
                    className="ml-1 p-1 rounded hover:bg-white/10 hover:text-red-300 shrink-0"
                    data-testid={`remove-staged-${s.id}`}
                    title="Remove from upload"
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                </div>
              ))}
              <div className="flex items-center text-[11px] sp-subtle pl-1">
                {staged.length} file{staged.length > 1 ? "s" : ""} ready · type your prompt and send
              </div>
            </div>
          )}
          {showMentions && isGroup && (
            <div className="mb-2 sp-glass rounded-xl p-2 flex flex-wrap gap-2">
              {convAgents.map(a => (
                <button
                  type="button"
                  key={a.id}
                  onClick={() => insertMention(a)}
                  className="sp-chip hover:!bg-white/15 hover:text-white"
                  data-testid={`mention-${a.id}`}
                >
                  <span className="sp-dot" style={{ background: a.accent }} />
                  {a.emoji ? `${a.emoji} ` : ""}{a.name}
                </button>
              ))}
            </div>
          )}
          <div className="flex items-end gap-2">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept="image/*,video/*,audio/*,.pdf,.doc,.docx,.txt,.md,.csv,.json,.log,.xlsx,.pptx,.rtf"
              className="hidden"
              onChange={onFilesPicked}
              data-testid="file-input"
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={uploading}
              className="sp-btn sp-btn-ghost text-xs shrink-0 disabled:opacity-50"
              title={uploading ? "Uploading…" : "Attach files (images, PDF, doc, audio, video — up to 25 MB each, 6 files)"}
              data-testid="attach-file-btn"
            >
              <Paperclip className="w-3.5 h-3.5" />
            </button>
            {isGroup && (
              <button
                type="button"
                onClick={() => setShowMentions(v => !v)}
                className="sp-btn sp-btn-ghost text-xs shrink-0"
                title="Mention an agent"
                data-testid="mention-picker-btn"
              >
                <AtSign className="w-3.5 h-3.5" />
              </button>
            )}
            <textarea
              ref={inputRef}
              data-testid="message-input"
              value={text}
              onChange={(e) => setText(e.target.value)}
              onPaste={onComposerPaste}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(e); }
              }}
              placeholder={staged.length > 0
                ? `Describe what to do with ${staged.length === 1 ? "this file" : `these ${staged.length} files`}…`
                : (isGroup
                  ? "Message the room… use @name to ping an agent. Ask for an image or a video and they'll just make it."
                  : `Message ${convAgents[0]?.name || "your agent"} — ask for posts, logos, or videos and they'll just make them.`)}
              rows={1}
              className="flex-1 sp-glass rounded-2xl px-4 py-3.5 text-[16px] leading-relaxed placeholder:opacity-50 focus:sp-focus resize-none max-h-44"
              style={{ background: "var(--input-bg)", borderColor: "var(--input-border)", color: "var(--fg)" }}
            />
            <button
              type="submit"
              disabled={sending || uploading || (!text.trim() && staged.length === 0)}
              className="sp-btn sp-btn-primary text-sm disabled:opacity-50 shrink-0"
              data-testid="send-message-btn"
            >
              <Send className="w-3.5 h-3.5" />
            </button>
          </div>
        </form>
      </div>

      {/* Switch-agent dialog */}
      {showSwitch && (
        <div className="fixed inset-0 z-40 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm" onClick={() => setShowSwitch(false)}>
          <div className="sp-glass-strong rounded-2xl w-full max-w-2xl p-6 sp-fade-up max-h-[85vh] overflow-hidden flex flex-col" onClick={(e) => e.stopPropagation()} data-testid="switch-agent-dialog">
            <div className="flex items-center justify-between mb-1">
              <h3 className="sp-display text-2xl">Switch agent</h3>
              <button onClick={() => setShowSwitch(false)} className="text-white/60 hover:text-white"><X className="w-4 h-4" /></button>
            </div>
            <p className="text-xs sp-subtle mb-4">
              Currently with <span className="text-white">{convAgents[0]?.name}</span>.
              Pick a different specialist — they'll see the full transcript and pick up from where we are.
            </p>
            <div className="flex items-center gap-2 sp-glass rounded-xl px-3 py-2 mb-3">
              <Search className="w-4 h-4 text-white/60" />
              <input
                value={switchQ}
                onChange={(e) => setSwitchQ(e.target.value)}
                placeholder={`Search ${agents.length} agents…`}
                className="flex-1 bg-transparent text-sm text-white placeholder:text-white/30 focus:outline-none"
                data-testid="switch-search"
              />
            </div>
            <div className="flex flex-wrap gap-2 mb-3">
              <button onClick={() => setSwitchDiv("")} className={`sp-chip ${switchDiv === "" ? "!bg-white/15 !text-white" : ""}`}>All</button>
              {divisions.map(d => (
                <button key={d.key} onClick={() => setSwitchDiv(d.key === switchDiv ? "" : d.key)} className={`sp-chip ${switchDiv === d.key ? "!bg-white/15 !text-white" : ""}`}>
                  {d.label} · {d.count}
                </button>
              ))}
            </div>
            <div className="grid sm:grid-cols-2 gap-2 overflow-y-auto pr-1 flex-1 min-h-0">
              {filteredSwitchAgents.map(a => (
                <button
                  key={a.id}
                  onClick={() => doSwitch(a.id)}
                  disabled={switchBusy}
                  className="sp-glass rounded-xl p-3 text-left hover:border-white/25 disabled:opacity-50"
                  data-testid={`switch-pick-${a.id}`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="w-8 h-8 rounded-lg flex items-center justify-center text-base shrink-0" style={{ background: `linear-gradient(160deg, ${a.accent}, rgba(255,255,255,0.06))` }}>
                      <span>{a.emoji || ""}</span>
                    </div>
                    <span className="sp-chip text-[10px]">{a.division}</span>
                  </div>
                  <div className="text-white text-sm font-medium line-clamp-1 mt-2">{a.name}</div>
                  <div className="text-[11px] sp-subtle line-clamp-2">{a.tagline}</div>
                </button>
              ))}
              {filteredSwitchAgents.length === 0 && (
                <div className="col-span-full sp-glass rounded-xl p-4 text-center text-xs sp-subtle">No agents match.</div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Manage-agents dialog (group conversations) */}
      {showAgents && (
        <div className="fixed inset-0 z-40 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm" onClick={() => setShowAgents(false)}>
          <div className="sp-glass-strong rounded-2xl w-full max-w-3xl p-6 sp-fade-up max-h-[85vh] overflow-hidden flex flex-col" onClick={(e) => e.stopPropagation()} data-testid="manage-agents-dialog">
            <div className="flex items-center justify-between mb-1">
              <h3 className="sp-display text-2xl">Manage agents</h3>
              <button onClick={() => setShowAgents(false)} className="text-white/60 hover:text-white"><X className="w-4 h-4" /></button>
            </div>
            <p className="text-xs sp-subtle mb-4">
              {convAgents.length} specialist{convAgents.length === 1 ? "" : "s"} in this room. Add anyone from the catalogue, or remove someone you don't need anymore.
            </p>

            {/* In-room agents (with remove × button each) */}
            <div className="mb-4">
              <div className="text-[10px] uppercase tracking-widest sp-subtle mb-2">In this room</div>
              <div className="flex flex-wrap gap-2" data-testid="manage-agents-current">
                {convAgents.map(a => (
                  <span key={a.id} className="sp-chip !bg-white/10 inline-flex items-center gap-1.5">
                    <span className="sp-dot" style={{ background: a.accent }} />
                    {a.emoji ? `${a.emoji} ` : ""}{a.name}
                    <span className="opacity-60">· {a.division}</span>
                    {convAgents.length > 1 && (
                      <button
                        onClick={() => removeAgent(a.id)}
                        disabled={agentsBusy}
                        className="ml-1 -mr-1 p-0.5 rounded hover:bg-white/10 hover:text-red-300 disabled:opacity-50"
                        title={`Remove ${a.name}`}
                        data-testid={`remove-agent-${a.id}`}
                      >
                        <X className="w-3 h-3" />
                      </button>
                    )}
                  </span>
                ))}
              </div>
              {convAgents.length === 1 && (
                <div className="text-[11px] sp-subtle mt-2">
                  Add another specialist before you can remove this one — a group must keep at least one agent.
                </div>
              )}
            </div>

            {/* Add picker */}
            <div className="text-[10px] uppercase tracking-widest sp-subtle mb-2">Add a specialist</div>
            <div className="flex items-center gap-2 sp-glass rounded-xl px-3 py-2 mb-3">
              <Search className="w-4 h-4 text-white/60" />
              <input
                value={agentsQ}
                onChange={(e) => setAgentsQ(e.target.value)}
                placeholder={`Search ${agents.length} agents…`}
                className="flex-1 bg-transparent text-sm text-white placeholder:text-white/30 focus:outline-none"
                data-testid="manage-agents-search"
              />
            </div>
            <div className="flex flex-wrap gap-2 mb-3">
              <button onClick={() => setAgentsDiv("")} className={`sp-chip ${agentsDiv === "" ? "!bg-white/15 !text-white" : ""}`}>All</button>
              {divisions.map(d => (
                <button key={d.key} onClick={() => setAgentsDiv(d.key === agentsDiv ? "" : d.key)} className={`sp-chip ${agentsDiv === d.key ? "!bg-white/15 !text-white" : ""}`}>
                  {d.label} · {d.count}
                </button>
              ))}
            </div>
            <div className="grid sm:grid-cols-2 gap-2 overflow-y-auto pr-1 flex-1 min-h-0">
              {filteredPickerAgents.map(a => (
                <button
                  key={a.id}
                  onClick={() => addAgent(a.id)}
                  disabled={agentsBusy}
                  className="sp-glass rounded-xl p-3 text-left hover:border-white/25 disabled:opacity-50"
                  data-testid={`add-agent-${a.id}`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="w-8 h-8 rounded-lg flex items-center justify-center text-base shrink-0" style={{ background: `linear-gradient(160deg, ${a.accent}, rgba(255,255,255,0.06))` }}>
                      <span>{a.emoji || ""}</span>
                    </div>
                    <span className="sp-chip text-[10px]">{a.division}</span>
                  </div>
                  <div className="text-white text-sm font-medium line-clamp-1 mt-2">{a.name}</div>
                  <div className="text-[11px] sp-subtle line-clamp-2">{a.tagline}</div>
                </button>
              ))}
              {filteredPickerAgents.length === 0 && (
                <div className="col-span-full sp-glass rounded-xl p-4 text-center text-xs sp-subtle">
                  {agentsQ || agentsDiv ? "No agents match that search." : "Every agent is already in this room."}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
