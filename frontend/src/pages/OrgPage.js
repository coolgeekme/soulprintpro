import React, { useEffect, useState } from "react";
import { useParams, Link, useNavigate } from "react-router-dom";
import api from "../lib/api";
import TopBar from "../components/TopBar";
import { toast } from "sonner";
import { Plus, Users, ArrowRight, Copy, UserPlus } from "lucide-react";

export default function OrgPage() {
  const { orgId } = useParams();
  const nav = useNavigate();
  const [org, setOrg] = useState(null);
  const [teams, setTeams] = useState([]);
  const [members, setMembers] = useState([]);
  const [invites, setInvites] = useState([]);
  const [loading, setLoading] = useState(true);
  const [newTeam, setNewTeam] = useState("");
  const [newTeamDesc, setNewTeamDesc] = useState("");
  const [creating, setCreating] = useState(false);
  const [showInviteTools, setShowInviteTools] = useState(false);

  const canInvite = org && (org.role === "owner" || org.role === "admin");
  const origin = typeof window !== "undefined" ? window.location.origin : "";

  const load = async () => {
    setLoading(true);
    try {
      const [orgRes, teamsRes, membersRes] = await Promise.all([
        api.get(`/orgs/${orgId}`),
        api.get(`/orgs/${orgId}/teams`),
        api.get(`/orgs/${orgId}/members`),
      ]);
      setOrg(orgRes.data);
      setTeams(teamsRes.data);
      setMembers(membersRes.data);
      if (orgRes.data.role === "owner" || orgRes.data.role === "admin") {
        try {
          const { data } = await api.get(`/orgs/${orgId}/invites`);
          setInvites(data);
        } catch (_) {}
      }
    } catch (err) {
      toast.error("Failed to load organization");
      nav("/app");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [orgId]);

  const createTeam = async (e) => {
    e.preventDefault();
    if (!newTeam.trim()) return;
    setCreating(true);
    try {
      const { data } = await api.post(`/orgs/${orgId}/teams`, {
        name: newTeam.trim(),
        description: newTeamDesc.trim(),
      });
      toast.success("Team created");
      nav(`/app/team/${data.id}`);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed");
    } finally { setCreating(false); }
  };

  const createInvite = async (role) => {
    try {
      const { data } = await api.post(`/orgs/${orgId}/invites`, { role });
      setInvites((prev) => [{ code: data.code, role, expires_at: data.expires_at }, ...prev]);
      toast.success(`Invite created (${role})`);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed");
    }
  };

  const copy = (text) => {
    navigator.clipboard.writeText(text).then(() => toast.success("Copied"));
  };

  if (loading) {
    return <div className="min-h-screen"><TopBar>Loading…</TopBar></div>;
  }

  return (
    <div className="min-h-screen">
      <TopBar
        right={
          canInvite && (
            <button
              onClick={() => setShowInviteTools((v) => !v)}
              className="sp-btn sp-btn-ghost text-xs"
              data-testid="toggle-invite-tools"
            >
              <UserPlus className="w-3.5 h-3.5" /> Invite
            </button>
          )
        }
      >
        <Link to="/app" className="hover:text-white">Organizations</Link>
        <span className="mx-2 opacity-40">/</span>
        <span className="text-white/90">{org.name}</span>
      </TopBar>

      <main className="max-w-6xl mx-auto px-6 py-10">
        <div className="flex flex-col gap-2 mb-10">
          <span className="sp-chip">{org.role}</span>
          <h1 className="sp-display text-4xl md:text-5xl">{org.name}</h1>
          <p className="sp-subtle">Spin up a team for each squad, brand, or project. Each team has its own SoulPrint.</p>
        </div>

        {showInviteTools && canInvite && (
          <div className="sp-glass-strong rounded-2xl p-6 mb-10" data-testid="invite-tools">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-sm uppercase tracking-widest sp-subtle">Invites</h3>
              <div className="flex items-center gap-2">
                <button className="sp-btn sp-btn-ghost text-xs" onClick={() => createInvite("member")} data-testid="invite-member-btn">+ Member invite</button>
                <button className="sp-btn sp-btn-ghost text-xs" onClick={() => createInvite("admin")} data-testid="invite-admin-btn">+ Admin invite</button>
              </div>
            </div>
            {invites.length === 0 && <div className="text-sm sp-subtle">No active invites yet.</div>}
            <div className="space-y-2">
              {invites.map((inv) => {
                const link = `${origin}/invite/${inv.code}`;
                const mail = `mailto:?subject=${encodeURIComponent(`Join ${org.name} on SoulPrint`)}&body=${encodeURIComponent(`Accept here: ${link}\nOr use invite code: ${inv.code}`)}`;
                return (
                  <div key={inv.code} className="flex flex-wrap items-center gap-2 p-3 rounded-xl sp-glass">
                    <span className="sp-chip">{inv.role}</span>
                    <code className="text-xs text-white/80">{inv.code}</code>
                    <span className="flex-1" />
                    <button onClick={() => copy(inv.code)} className="sp-btn sp-btn-ghost text-xs"><Copy className="w-3 h-3"/> code</button>
                    <button onClick={() => copy(link)} className="sp-btn sp-btn-ghost text-xs"><Copy className="w-3 h-3"/> link</button>
                    <a href={mail} className="sp-btn sp-btn-ghost text-xs">Email</a>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        <div className="grid md:grid-cols-3 gap-4 mb-10">
          {teams.map((t) => (
            <Link
              key={t.id}
              to={`/app/team/${t.id}`}
              className="sp-glass rounded-2xl p-6 hover:border-white/20 transition group"
              data-testid={`team-card-${t.id}`}
            >
              <div className="w-10 h-10 rounded-xl sp-glass-strong flex items-center justify-center mb-4">
                <Users className="w-5 h-5 text-white/90" />
              </div>
              <div className="text-white font-medium text-lg truncate">{t.name}</div>
              {t.description && <div className="text-xs sp-subtle mt-1 line-clamp-2">{t.description}</div>}
              <div className="flex items-center gap-2 mt-4 text-xs sp-subtle group-hover:text-white/80">
                Open workspace <ArrowRight className="w-3.5 h-3.5" />
              </div>
            </Link>
          ))}
          {teams.length === 0 && (
            <div className="sp-glass rounded-2xl p-6 col-span-full text-center text-sm sp-subtle">
              No teams yet — create your first one below.
            </div>
          )}
        </div>

        <div className="grid md:grid-cols-2 gap-4">
          <form onSubmit={createTeam} className="sp-glass rounded-2xl p-6">
            <div className="flex items-center gap-2 mb-3">
              <Plus className="w-4 h-4 text-white/80" />
              <h3 className="text-sm uppercase tracking-widest sp-subtle">New team</h3>
            </div>
            <input
              data-testid="new-team-name"
              value={newTeam}
              onChange={(e) => setNewTeam(e.target.value)}
              placeholder="e.g. Product Marketing"
              className="w-full bg-white/[0.03] border border-white/10 rounded-xl px-4 py-3 text-sm text-white placeholder:text-white/30 focus:border-white/30 focus:sp-focus mb-2"
            />
            <input
              data-testid="new-team-desc"
              value={newTeamDesc}
              onChange={(e) => setNewTeamDesc(e.target.value)}
              placeholder="One-line description (optional)"
              className="w-full bg-white/[0.03] border border-white/10 rounded-xl px-4 py-3 text-sm text-white placeholder:text-white/30 focus:border-white/30 focus:sp-focus"
            />
            <button
              type="submit"
              disabled={creating || !newTeam.trim()}
              className="sp-btn sp-btn-primary w-full justify-center mt-3 disabled:opacity-50"
              data-testid="create-team-btn"
            >
              {creating ? "Creating…" : "Create team"}
            </button>
          </form>

          <div className="sp-glass rounded-2xl p-6">
            <div className="flex items-center gap-2 mb-3">
              <Users className="w-4 h-4 text-white/80" />
              <h3 className="text-sm uppercase tracking-widest sp-subtle">Members ({members.length})</h3>
            </div>
            <div className="flex flex-col gap-2 max-h-60 overflow-y-auto">
              {members.map((m) => (
                <div key={m.user_id} className="flex items-center justify-between p-3 rounded-xl sp-glass">
                  <div className="min-w-0">
                    <div className="text-sm text-white truncate">{m.name}</div>
                    <div className="text-xs sp-subtle truncate">{m.email}</div>
                  </div>
                  <span className="sp-chip">{m.role}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}
