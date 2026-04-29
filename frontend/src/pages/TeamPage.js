import React, { useEffect, useMemo, useState } from "react";
import { useParams, Link, useNavigate } from "react-router-dom";
import api from "../lib/api";
import TopBar from "../components/TopBar";
import { toast } from "sonner";
import { Sparkles, MessageCircle, Users, Plus, Radio } from "lucide-react";

const AgentBadge = ({ a, onClick, selected, testPrefix = "agent-badge" }) => (
  <button
    type="button"
    onClick={onClick}
    className={`sp-glass rounded-xl p-4 text-left transition hover:border-white/25 ${selected ? "border-white/40 sp-focus" : ""}`}
    data-testid={`${testPrefix}-${a.id}`}
  >
    <div className="flex items-start justify-between">
      <div className="w-8 h-8 rounded-lg mb-2" style={{ background: `linear-gradient(160deg, ${a.accent}, rgba(255,255,255,0.06))` }} />
      <span className="sp-chip">{a.role}</span>
    </div>
    <div className="text-white text-sm font-medium">{a.name}</div>
    <div className="text-[11px] sp-subtle mt-1 line-clamp-2">{a.tagline}</div>
  </button>
);

export default function TeamPage() {
  const { teamId } = useParams();
  const nav = useNavigate();
  const [team, setTeam] = useState(null);
  const [agents, setAgents] = useState([]);
  const [conversations, setConversations] = useState([]);
  const [members, setMembers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [savingSP, setSavingSP] = useState(false);
  const [sp, setSp] = useState({ mission: "", tone: "", audience: "", values: "", north_star: "" });

  // new conversation state
  const [showNew, setShowNew] = useState(false);
  const [convType, setConvType] = useState("direct"); // direct | group
  const [convTitle, setConvTitle] = useState("");
  const [selAgents, setSelAgents] = useState([]);
  const [selMembers, setSelMembers] = useState([]);
  const [autoRespond, setAutoRespond] = useState(false);
  const [creatingConv, setCreatingConv] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const teamRes = await api.get(`/teams/${teamId}`);
      const [agentsRes, convsRes, membersRes] = await Promise.all([
        api.get(`/teams/${teamId}/agents`),
        api.get(`/teams/${teamId}/conversations`),
        api.get(`/orgs/${teamRes.data.org_id}/members`),
      ]);
      setTeam(teamRes.data);
      setSp(teamRes.data.soulprint || sp);
      setAgents(agentsRes.data);
      setConversations(convsRes.data);
      setMembers(membersRes.data);
    } catch (err) {
      toast.error("Failed to load team");
      nav("/app");
    } finally { setLoading(false); }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [teamId]);

  const saveSoulPrint = async () => {
    setSavingSP(true);
    try {
      const { data } = await api.put(`/teams/${teamId}/soulprint`, sp);
      setTeam(data);
      setSp(data.soulprint || sp);
      toast.success("SoulPrint saved");
    } catch (err) {
      toast.error("Failed to save");
    } finally { setSavingSP(false); }
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
    setSelAgents([]); setSelMembers([]); setAutoRespond(false);
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
      });
      toast.success("Conversation created");
      nav(`/app/team/${teamId}/c/${data.id}`);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed");
    } finally { setCreatingConv(false); }
  };

  const agentMap = useMemo(() => Object.fromEntries(agents.map(a => [a.id, a])), [agents]);

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
        <Link to={`/app/org/${team.org_id}`} className="hover:text-white">Org</Link>
        <span className="mx-2 opacity-40">/</span>
        <span className="text-white/90">{team.name}</span>
      </TopBar>

      <main className="max-w-7xl mx-auto px-6 py-10">
        <div className="flex flex-col gap-2 mb-8">
          <span className="sp-chip">Team workspace</span>
          <h1 className="sp-display text-4xl md:text-5xl">{team.name}</h1>
          {team.description && <p className="sp-subtle">{team.description}</p>}
        </div>

        <div className="grid lg:grid-cols-[1.4fr_1fr] gap-6">
          {/* Left: Conversations & Agents */}
          <div className="flex flex-col gap-6">
            <section>
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-sm uppercase tracking-widest sp-subtle flex items-center gap-2"><MessageCircle className="w-4 h-4" /> Conversations</h2>
              </div>
              <div className="grid sm:grid-cols-2 gap-3">
                {conversations.map((c) => {
                  const ag = c.agent_ids.map(id => agentMap[id]?.name).filter(Boolean).join(", ");
                  return (
                    <Link
                      key={c.id}
                      to={`/app/team/${teamId}/c/${c.id}`}
                      className="sp-glass rounded-xl p-4 hover:border-white/20 transition"
                      data-testid={`conv-card-${c.id}`}
                    >
                      <div className="flex items-center justify-between mb-1">
                        <div className="text-white text-sm font-medium truncate">{c.title || "Untitled"}</div>
                        <span className="sp-chip">{c.type}</span>
                      </div>
                      <div className="text-xs sp-subtle truncate">{ag || "—"}</div>
                      {c.type === "group" && (
                        <div className="text-[10px] sp-subtle mt-1 uppercase tracking-widest">
                          {c.auto_respond ? "auto-respond" : "@mention to trigger"}
                        </div>
                      )}
                    </Link>
                  );
                })}
                {conversations.length === 0 && (
                  <div className="sp-glass rounded-xl p-6 col-span-full text-center text-sm sp-subtle">
                    Start a new conversation to meet your crew.
                  </div>
                )}
              </div>
            </section>

            <section>
              <h2 className="text-sm uppercase tracking-widest sp-subtle flex items-center gap-2 mb-3"><Sparkles className="w-4 h-4" /> Your crew</h2>
              <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {agents.map(a => <AgentBadge key={a.id} a={a} testPrefix="agent-crew" />)}
              </div>
            </section>
          </div>

          {/* Right: SoulPrint editor */}
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

      {/* New conversation modal */}
      {showNew && (
        <div className="fixed inset-0 z-40 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm" onClick={resetDialog}>
          <div className="sp-glass-strong rounded-2xl w-full max-w-2xl p-6 sp-fade-up" onClick={(e) => e.stopPropagation()} data-testid="new-conv-dialog">
            <h3 className="sp-display text-2xl mb-1">New conversation</h3>
            <p className="text-xs sp-subtle mb-4">Chat 1:1 with an agent, or spin up a group with teammates.</p>

            <div className="flex items-center gap-2 mb-4">
              <button
                onClick={() => { setConvType("direct"); setSelAgents([]); }}
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

            <label className="text-[11px] sp-subtle uppercase tracking-widest">{convType === "direct" ? "Pick one agent" : "Pick agents"}</label>
            <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-2 mt-2 mb-4">
              {agents.map(a => (
                <AgentBadge key={a.id} a={a} onClick={() => toggleAgent(a.id)} selected={selAgents.includes(a.id)} />
              ))}
            </div>

            {convType === "group" && (
              <>
                <label className="text-[11px] sp-subtle uppercase tracking-widest">Invite teammates</label>
                <div className="flex flex-wrap gap-2 mt-2 mb-4 max-h-40 overflow-y-auto">
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

                <label className="flex items-center gap-3 mb-4 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={autoRespond}
                    onChange={(e) => setAutoRespond(e.target.checked)}
                    className="accent-violet-500"
                    data-testid="auto-respond-toggle"
                  />
                  <span className="text-sm text-white">Agents auto-respond on every message</span>
                  <span className="text-xs sp-subtle">(otherwise @mention the agent by name)</span>
                </label>
              </>
            )}

            <div className="flex items-center justify-end gap-2 mt-2">
              <button onClick={resetDialog} className="sp-btn sp-btn-ghost text-sm" data-testid="cancel-new-conv">Cancel</button>
              <button
                onClick={createConv}
                disabled={creatingConv || selAgents.length === 0}
                className="sp-btn sp-btn-primary text-sm disabled:opacity-50"
                data-testid="submit-new-conv"
              >
                {creatingConv ? "Creating…" : "Start conversation"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
