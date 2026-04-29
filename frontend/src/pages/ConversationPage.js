import React, { useEffect, useMemo, useRef, useState } from "react";
import { useParams, Link, useNavigate } from "react-router-dom";
import api from "../lib/api";
import TopBar from "../components/TopBar";
import { toast } from "sonner";
import { useAuth } from "../context/AuthContext";
import { Send, Trash2, AtSign, Users } from "lucide-react";

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
  }, [messages, sending]);

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
    } catch (err) {
      toast.error("Message failed");
      setMessages((prev) => prev.filter(m => m.id !== optimistic.id));
      setText(toSend);
    } finally { setSending(false); }
  };

  const insertMention = (a) => {
    const at = text.endsWith(" ") || text.length === 0 ? "" : " ";
    setText((prev) => `${prev}${at}@${a.name} `);
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
        <div className="max-w-5xl mx-auto px-6 py-4 flex items-center gap-3 flex-wrap">
          <span className="sp-chip">{conv.type}</span>
          {convAgents.map((a) => (
            <span key={a.id} className="sp-chip !bg-white/5">
              <span className="sp-dot" style={{ background: a.accent }} />
              {a.name} · {a.role}
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
        </div>
      </div>

      {/* Messages */}
      <main ref={scrollerRef} className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-6 py-10 flex flex-col gap-4" data-testid="messages-container">
          {messages.length === 0 && (
            <div className="sp-glass rounded-2xl p-8 text-center">
              <div className="text-white mb-1">You're in.</div>
              <div className="text-sm sp-subtle">
                {isGroup
                  ? <>Type a message. {conv.auto_respond ? "Every agent will respond." : <>Use <span className="text-white">@agent-name</span> to call an agent.</>}</>
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
                <div className={`max-w-[82%] rounded-2xl px-4 py-3 ${mine ? "sp-bubble-user" : "sp-bubble-agent"}`}>
                  <div className="flex items-center gap-2 mb-1">
                    {isAgent && a && <span className="sp-dot" style={{ background: a.accent }} />}
                    <span className="text-xs font-medium text-white/90">
                      {isAgent ? (a?.name || "Agent") : (mine ? "You" : m.sender_name)}
                    </span>
                    {isAgent && a && <span className="text-[10px] sp-subtle uppercase tracking-widest">{a.role}</span>}
                    <span className="text-[10px] sp-subtle ml-auto">{formatTime(m.timestamp)}</span>
                  </div>
                  <div className="whitespace-pre-wrap text-sm leading-relaxed">{m.text}</div>
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
        <form onSubmit={send} className="max-w-3xl mx-auto px-6 py-4">
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
                  <span className="sp-dot" style={{ background: a.accent }} /> {a.name}
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
            <textarea
              ref={inputRef}
              data-testid="message-input"
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(e); }
              }}
              placeholder={isGroup ? "Message the room… use @name to ping an agent" : `Message ${convAgents[0]?.name || "your agent"}`}
              rows={1}
              className="flex-1 bg-white/[0.03] border border-white/10 rounded-2xl px-4 py-3 text-sm text-white placeholder:text-white/30 focus:border-white/30 focus:sp-focus resize-none max-h-40"
            />
            <button
              type="submit"
              disabled={sending || !text.trim()}
              className="sp-btn sp-btn-primary text-sm disabled:opacity-50 shrink-0"
              data-testid="send-message-btn"
            >
              <Send className="w-3.5 h-3.5" />
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
