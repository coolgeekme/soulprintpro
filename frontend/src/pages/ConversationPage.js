import React, { useEffect, useMemo, useRef, useState } from "react";
import { useParams, Link, useNavigate } from "react-router-dom";
import api, { BACKEND_URL } from "../lib/api";
import TopBar from "../components/TopBar";
import { toast } from "sonner";
import { useAuth } from "../context/AuthContext";
import { Send, Trash2, AtSign, Users, Repeat, Search, X, Lock, Image as ImageIcon, Film, ExternalLink, Globe, ArrowUp, ArrowDown } from "lucide-react";

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
  const [scrollState, setScrollState] = useState({ canUp: false, canDown: false });

  const onScrollerScroll = () => {
    const el = scrollerRef.current;
    if (!el) return;
    setScrollState({
      canUp: el.scrollTop > 200,
      canDown: el.scrollTop + el.clientHeight < el.scrollHeight - 200,
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

  const [imageMode, setImageMode] = useState(false);
  const [videoMode, setVideoMode] = useState(false);

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

  const send = async (e) => {
    e.preventDefault();
    if (!text.trim() || sending) return;
    setSending(true);
    const optimistic = {
      id: `tmp-${Date.now()}`,
      conversation_id: convId,
      sender_type: "user",
      sender_id: user.id,
      sender_name: user.name,
      text: videoMode ? `🎬 ${text}` : (imageMode ? `🎨 ${text}` : text),
      timestamp: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, optimistic]);
    const toSend = text;
    const wasImage = imageMode;
    const wasVideo = videoMode;
    setText("");
    setImageMode(false);
    setVideoMode(false);
    try {
      if (wasVideo) {
        const { data } = await api.post(`/conversations/${convId}/generate-video`, {
          prompt: toSend, aspect_ratio: "16:9",
        });
        setMessages((prev) => {
          const withoutOptim = prev.filter(m => m.id !== optimistic.id);
          return [...withoutOptim, data.user_message, data.agent_message];
        });
      } else if (wasImage) {
        const { data } = await api.post(`/conversations/${convId}/generate-image`, { prompt: toSend });
        setMessages((prev) => {
          const withoutOptim = prev.filter(m => m.id !== optimistic.id);
          return [...withoutOptim, data.user_message, data.agent_message];
        });
        if (!data.ok) toast.error("Image generation failed");
      } else {
        const { data } = await api.post(`/conversations/${convId}/messages`, { text: toSend });
        setMessages((prev) => {
          const withoutOptim = prev.filter(m => m.id !== optimistic.id);
          return [...withoutOptim, data.user_message, ...data.agent_messages];
        });
        if (data.conversation) setConv(data.conversation);
      }
    } catch (err) {
      toast.error(wasVideo ? "Video request failed" : (wasImage ? "Image request failed" : "Message failed"));
      setMessages((prev) => prev.filter(m => m.id !== optimistic.id));
      setText(toSend);
      setImageMode(wasImage);
      setVideoMode(wasVideo);
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

  // Switch-agent dialog (direct chats only)
  const [showSwitch, setShowSwitch] = useState(false);
  const [switchQ, setSwitchQ] = useState("");
  const [switchDiv, setSwitchDiv] = useState("");
  const [switchBusy, setSwitchBusy] = useState(false);
  const [divisions, setDivisions] = useState([]);
  useEffect(() => {
    if (!showSwitch || divisions.length) return;
    api.get("/agents/divisions").then((r) => setDivisions(r.data)).catch(() => {});
  }, [showSwitch, divisions.length]);

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

      {/* Meta strip */}
      <div className="border-b border-white/5 bg-black/20">
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
        </div>
      </div>

      {/* Messages */}
      <main ref={scrollerRef} onScroll={onScrollerScroll} className="flex-1 overflow-y-auto relative">
        {/* Floating scroll-to-top / scroll-to-bottom arrows */}
        {scrollState.canUp && (
          <button
            type="button"
            onClick={() => scrollTo("top")}
            className="sp-glass-strong rounded-full p-2.5 fixed sm:absolute right-4 top-4 z-20 hover:scale-105 transition shadow-xl"
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
            className="sp-glass-strong rounded-full p-2.5 fixed sm:absolute right-4 bottom-28 z-20 hover:scale-105 transition shadow-xl"
            data-testid="scroll-bottom-btn"
            title="Jump to latest"
          >
            <ArrowDown className="w-4 h-4" />
          </button>
        )}
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
            const mine = m.sender_type === "user" && m.sender_id === user.id;
            const isAgent = m.sender_type === "agent";
            const a = isAgent ? agentMap[m.agent_id] : null;
            return (
              <div key={m.id} className={`flex ${mine ? "justify-end" : "justify-start"} sp-fade-up`}>
                <div className={`max-w-[92%] sm:max-w-[88%] rounded-2xl px-4 sm:px-5 py-3 sm:py-3.5 ${mine ? "sp-bubble-user" : "sp-bubble-agent"}`}>
                  <div className="flex items-center gap-2 mb-1.5">
                    {isAgent && a && <span className="sp-dot" style={{ background: a.accent }} />}
                    <span className="text-sm font-medium">
                      {isAgent ? (a?.name || "Agent") : (mine ? "You" : m.sender_name)}
                    </span>
                    {isAgent && a && <span className="text-[11px] sp-subtle uppercase tracking-widest">{a.division}</span>}
                    <span className="text-[11px] sp-subtle ml-auto">{formatTime(m.timestamp)}</span>
                  </div>
                  <div className="whitespace-pre-wrap text-[16px] leading-[1.6]">{m.text}</div>
                  {Array.isArray(m.web_sources) && m.web_sources.length > 0 && (
                    <div className="mt-3 pt-3 border-t border-white/10" data-testid={`web-sources-${m.id}`}>
                      <div className="flex items-center gap-2 mb-2">
                        <Globe className="w-3 h-3 text-white/70" />
                        <span className="text-[10px] uppercase tracking-widest sp-subtle">Web sources · {m.web_sources.length}</span>
                      </div>
                      <ol className="flex flex-col gap-1.5">
                        {m.web_sources.slice(0, 5).map((s, i) => (
                          <li key={`${m.id}-src-${i}`} className="text-[12px] leading-snug">
                            <a
                              href={s.url}
                              target="_blank"
                              rel="noreferrer"
                              className="inline-flex items-start gap-1.5 text-white/85 hover:text-white"
                            >
                              <ExternalLink className="w-3 h-3 mt-0.5 shrink-0 text-white/50" />
                              <span>
                                <span className="font-medium">{s.title || s.url}</span>
                                {(s.source || s.age) && (
                                  <span className="ml-1 text-white/50">
                                    · {[s.source, s.age].filter(Boolean).join(" · ")}
                                  </span>
                                )}
                              </span>
                            </a>
                          </li>
                        ))}
                      </ol>
                    </div>
                  )}
                  {m.media_type === "image" && m.media_url && (
                    <a
                      href={m.media_url.startsWith("http") ? m.media_url : `${BACKEND_URL}${m.media_url}`}
                      target="_blank" rel="noreferrer"
                      className="block mt-3 rounded-xl overflow-hidden border border-white/10 hover:border-white/30 transition"
                      data-testid={`message-image-${m.id}`}
                    >
                      <img
                        src={m.media_url.startsWith("http") ? m.media_url : `${BACKEND_URL}${m.media_url}`}
                        alt="Generated"
                        referrerPolicy="no-referrer"
                        className="block w-full max-h-[520px] object-contain bg-black/30"
                        loading="lazy"
                      />
                    </a>
                  )}
                  {m.media_type === "video" && m.media_status === "pending" && (
                    <div className="mt-3 rounded-xl overflow-hidden border border-white/10 bg-black/40 aspect-video flex flex-col items-center justify-center gap-2"
                         data-testid={`message-video-pending-${m.id}`}>
                      <Film className="w-8 h-8 opacity-60 animate-pulse" />
                      <div className="text-xs sp-subtle">Rendering video — Veo 3.1 typically takes 60–180s.</div>
                    </div>
                  )}
                  {m.media_type === "video" && m.media_status === "ready" && m.media_url && (
                    <video
                      src={m.media_url}
                      controls
                      preload="metadata"
                      className="block mt-3 rounded-xl overflow-hidden border border-white/10 hover:border-white/30 transition w-full max-h-[480px] bg-black/30"
                      data-testid={`message-video-${m.id}`}
                    />
                  )}
                  {m.media_type === "video" && m.media_status === "failed" && (
                    <div className="mt-3 rounded-xl border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-200/90"
                         data-testid={`message-video-failed-${m.id}`}>
                      Video generation failed. Try a different prompt or run it again.
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
      <div className="border-t border-white/5 bg-black/30 backdrop-blur-xl">
        <form onSubmit={send} className="max-w-5xl mx-auto px-3 sm:px-6 py-4">
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
            <button
              type="button"
              onClick={() => { setImageMode(v => !v); setVideoMode(false); }}
              className={`sp-btn text-xs shrink-0 ${imageMode ? "sp-btn-primary" : "sp-btn-ghost"}`}
              title={imageMode ? "Image mode ON — your next message becomes an image prompt" : "Generate an image instead of a text reply"}
              data-testid="image-mode-toggle"
            >
              <ImageIcon className="w-3.5 h-3.5" />
            </button>
            <button
              type="button"
              onClick={() => { setVideoMode(v => !v); setImageMode(false); }}
              className={`sp-btn text-xs shrink-0 ${videoMode ? "sp-btn-primary" : "sp-btn-ghost"}`}
              title={videoMode ? "Video mode ON — your next message becomes a Veo 3.1 prompt" : "Generate a video (Veo 3.1) instead of a text reply"}
              data-testid="video-mode-toggle"
            >
              <Film className="w-3.5 h-3.5" />
            </button>
            <textarea
              ref={inputRef}
              data-testid="message-input"
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(e); }
              }}
              placeholder={videoMode
                ? `Describe the video — Veo 3.1 will render it (60–180s)`
                : (imageMode
                  ? `Describe the image — ${convAgents[0]?.name || "your agent"} will generate it`
                  : (isGroup ? "Message the room… use @name to ping an agent" : `Message ${convAgents[0]?.name || "your agent"}`))}
              rows={1}
              className="flex-1 sp-glass rounded-2xl px-4 py-3.5 text-[16px] leading-relaxed placeholder:opacity-50 focus:sp-focus resize-none max-h-44"
              style={{ background: "var(--input-bg)", borderColor: "var(--input-border)", color: "var(--fg)" }}
            />
            <button
              type="submit"
              disabled={sending || !text.trim()}
              className="sp-btn sp-btn-primary text-sm disabled:opacity-50 shrink-0"
              data-testid="send-message-btn"
            >
              {videoMode ? <Film className="w-3.5 h-3.5" /> : (imageMode ? <ImageIcon className="w-3.5 h-3.5" /> : <Send className="w-3.5 h-3.5" />)}
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
    </div>
  );
}
