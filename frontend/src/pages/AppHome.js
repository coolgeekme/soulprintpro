import React, { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import api from "../lib/api";
import TopBar from "../components/TopBar";
import { toast } from "sonner";
import { Plus, ArrowRight, Ticket, MessageCircle, Settings, Users, LayoutGrid } from "lucide-react";

export default function AppHome() {
  const [orgs, setOrgs] = useState([]);
  const [workspaces, setWorkspaces] = useState([]);
  const [loading, setLoading] = useState(true);
  const [name, setName] = useState("");
  const [websiteUrl, setWebsiteUrl] = useState("");
  const [soulDesc, setSoulDesc] = useState("");
  const [creating, setCreating] = useState(false);
  const [code, setCode] = useState("");
  const [joining, setJoining] = useState(false);
  const nav = useNavigate();

  const load = async () => {
    setLoading(true);
    try {
      const [orgsR, wsR] = await Promise.all([
        api.get("/orgs"),
        api.get("/workspaces").catch(() => ({ data: [] })),
      ]);
      setOrgs(orgsR.data);
      setWorkspaces(wsR.data);
    } catch (e) {
      toast.error("Could not load organizations");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const createOrg = async (e) => {
    e.preventDefault();
    if (!name.trim()) return;
    setCreating(true);
    try {
      const { data } = await api.post("/orgs", {
        name: name.trim(),
        website_url: websiteUrl.trim(),
        soul_description: soulDesc.trim(),
      });
      toast.success("Organization created — Soul captured");
      // Land on the org workspace page so the user can review the captured
      // Soul, see the General chat card, the Social Queue, and any teams
      // before diving into a conversation. They can click the General chat
      // card from there to start chatting.
      nav(`/app/org/${data.id}`);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed to create");
    } finally {
      setCreating(false);
    }
  };

  const acceptInvite = async (e) => {
    e.preventDefault();
    if (!code.trim()) return;
    setJoining(true);
    try {
      // Workspace invites (W3) are richer than org invites and should
      // show the user which orgs they're about to join. Route them
      // through the dedicated AcceptInvitePage which handles both types.
      nav(`/invite/${code.trim()}`);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Invalid invite");
    } finally {
      setJoining(false);
    }
  };

  return (
    <div className="min-h-screen">
      <TopBar>Your organizations</TopBar>

      <main className="max-w-5xl mx-auto px-4 sm:px-6 py-5 sm:py-7" data-testid="app-home">
        {/* Compact header (UX audit #2) — single row title + workspace
            stats + primary actions, replacing the previous huge display
            block that pushed the org grid below the fold. */}
        <div className="flex items-center justify-between gap-3 mb-5 flex-wrap" data-testid="apphome-compact-header">
          <div className="min-w-0">
            <span className="sp-chip">Workspaces</span>
            <h1 className="text-2xl sm:text-3xl font-medium tracking-tight mt-1.5">
              Your organizations
              {workspaces.length > 0 && (
                <span className="ml-2 text-xs sp-subtle font-normal">
                  · {workspaces.reduce((a, w) => a + (w.org_count || 0), 0)} orgs · {workspaces.reduce((a, w) => a + (w.member_count || 0), 0)} members
                </span>
              )}
            </h1>
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            <a href="#new-org" className="sp-btn sp-btn-primary text-xs" data-testid="apphome-new-org-shortcut">
              <Plus className="w-3.5 h-3.5" /> New org
            </a>
            <a href="#join-invite" className="sp-btn sp-btn-ghost text-xs" data-testid="apphome-join-shortcut">
              <Ticket className="w-3.5 h-3.5" /> Join
            </a>
          </div>
        </div>

        {/* Workspace strip — shows every workspace the user belongs to
            (owner or admin) with a quick jump to the manage page where
            they invite teammates and grant per-org access. Hidden if
            the user has no workspaces yet (new accounts). */}
        {workspaces.length > 0 && (
          <section className="mb-6" data-testid="workspace-strip">
            <div className="text-[10px] uppercase tracking-widest sp-subtle mb-2">Your workspaces</div>
            <div className="flex flex-wrap gap-2">
              {workspaces.map((w) => (
                <Link key={w.id} to={`/app/ws/${w.id}`}
                      className="sp-glass rounded-xl px-3 py-2 inline-flex items-center gap-2 text-sm hover:border-white/30"
                      data-testid={`workspace-link-${w.id}`}>
                  <Settings className="w-3.5 h-3.5 shrink-0" />
                  <span className="font-medium truncate max-w-[180px]">{w.name}</span>
                  <span className="text-[10px] uppercase tracking-widest sp-subtle">
                    {w.org_count} org{w.org_count === 1 ? "" : "s"} · {w.member_count} mbr{w.member_count === 1 ? "" : "s"}
                  </span>
                </Link>
              ))}
            </div>
          </section>
        )}

        <div className="grid md:grid-cols-3 gap-4 mb-8">
          {loading && [0,1,2].map((i) => <div key={i} className="sp-glass rounded-2xl p-5 h-36 animate-pulse" />)}

          {!loading && orgs.map((o) => {
            // Whole card is one tap target (UX audit #3) — clicking
            // anywhere lands on the org dashboard. Inside, the small
            // "Chief of Staff" shortcut is a nested-but-stop-prop link.
            const primaryHref = `/app/org/${o.id}`;
            const chatHref = (o.general_team_id && o.general_conversation_id)
              ? `/app/team/${o.general_team_id}/c/${o.general_conversation_id}`
              : null;
            const mission = o?.soul?.mission || "";
            const subtitle = mission
              ? mission.length > 110 ? `${mission.slice(0, 107)}…` : mission
              : "";
            const initials = (o.name || "?")
              .split(/\s+/).filter(Boolean).slice(0, 2)
              .map((s) => s[0].toUpperCase()).join("") || "•";
            // Status: minimal heuristic for now — Active when org has a
            // soul.mission, Setup otherwise. Future statuses (Paused
            // etc.) can land here when we add a real org.status field.
            const status = mission
              ? { label: "Active", cls: "!bg-emerald-400/15 !text-emerald-200" }
              : { label: "Setup",  cls: "!bg-orange-400/20 !text-orange-200" };
            return (
              <Link
                to={primaryHref}
                key={o.id}
                className="sp-glass rounded-2xl p-5 hover:border-white/30 hover:bg-white/[0.04] transition cursor-pointer flex flex-col group"
                data-testid={`org-card-${o.id}`}
              >
                <div className="flex items-center justify-between mb-3 gap-2">
                  <div className="w-9 h-9 rounded-lg sp-glass-strong flex items-center justify-center shrink-0 text-[12px] font-medium">
                    {o.brand_assets?.primary_logo?.url
                      ? <img src={o.brand_assets.primary_logo.url} alt={o.name} className="w-9 h-9 rounded-lg object-cover" />
                      : <span className="text-white/85">{initials}</span>}
                  </div>
                  <div className="flex items-center gap-1">
                    <span className={`sp-chip text-[10px] ${status.cls}`} data-testid={`org-card-status-${o.id}`}>
                      {status.label}
                    </span>
                    <span className="sp-chip text-[10px]">{o.role}</span>
                  </div>
                </div>
                <div className="text-white font-medium text-base sm:text-lg truncate group-hover:text-white">
                  {o.name}
                </div>
                {/* UX audit #7: pull mission from soul as the subtitle so
                    every org has a distinct identity instead of "Manage
                    your organization" boilerplate. */}
                {subtitle ? (
                  <div className="text-[12px] sp-subtle leading-snug mt-1 line-clamp-2">
                    {subtitle}
                  </div>
                ) : (
                  <div className="text-[12px] text-orange-300/90 mt-1">
                    Add a mission statement to complete your Company Soul →
                  </div>
                )}
                <div className="flex items-center gap-3 mt-3 text-[11px] sp-subtle">
                  <span className="inline-flex items-center gap-1"><Users className="w-3 h-3" />{o.member_count ?? 1}</span>
                  <span className="inline-flex items-center gap-1"><LayoutGrid className="w-3 h-3" />{o.team_count ?? 0} teams</span>
                </div>
                <div className="mt-auto pt-4 flex items-center justify-between">
                  <span className="inline-flex items-center gap-1.5 text-[11px] sp-subtle group-hover:text-white">
                    Open organization <ArrowRight className="w-3 h-3" />
                  </span>
                  {chatHref && (
                    <Link
                      to={chatHref}
                      onClick={(e) => e.stopPropagation()}
                      className="inline-flex items-center gap-1 text-[11px] sp-subtle hover:text-orange-300 transition"
                      data-testid={`org-card-chat-${o.id}`}
                    >
                      <MessageCircle className="w-3 h-3" /> Chief of Staff
                    </Link>
                  )}
                </div>
              </Link>
            );
          })}

          {!loading && orgs.length === 0 && (
            <div className="sp-glass rounded-2xl p-6 col-span-full text-center text-sm sp-subtle">
              No organizations yet. Create one below to get started.
            </div>
          )}
        </div>

        <div className="grid md:grid-cols-2 gap-4">
          <form onSubmit={createOrg} id="new-org" className="sp-glass rounded-2xl p-6 scroll-mt-20">
            <div className="flex items-center gap-2 mb-3">
              <Plus className="w-4 h-4 text-white/80" />
              <h3 className="text-sm uppercase tracking-widest sp-subtle">New organization</h3>
            </div>
            <input
              data-testid="new-org-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Nebula Labs"
              className="w-full bg-white/[0.03] border border-white/10 rounded-xl px-4 py-3 text-sm text-white placeholder:text-white/30 focus:border-white/30 focus:sp-focus mb-2"
            />
            <input
              data-testid="new-org-url"
              type="text"
              inputMode="url"
              autoCapitalize="off"
              autoCorrect="off"
              value={websiteUrl}
              onChange={(e) => setWebsiteUrl(e.target.value)}
              placeholder="Company website (optional) — e.g. disciplinedos.com"
              className="w-full bg-white/[0.03] border border-white/10 rounded-xl px-4 py-3 text-sm text-white placeholder:text-white/30 focus:border-white/30 focus:sp-focus mb-2"
            />
            <textarea
              data-testid="new-org-soul"
              value={soulDesc}
              onChange={(e) => setSoulDesc(e.target.value)}
              placeholder="Optional — describe your company in one sentence (mission, audience, vibe). We'll use this if you skip the URL."
              rows={2}
              className="w-full bg-white/[0.03] border border-white/10 rounded-xl px-4 py-3 text-sm text-white placeholder:text-white/30 focus:border-white/30 focus:sp-focus resize-none"
            />
            <div className="text-[11px] sp-subtle mt-2">
              We synthesise your company "Soul" — mission, audience, voice — and inject it into every agent. A Chief of Staff chat is created instantly so you can start talking.
            </div>
            <button
              data-testid="create-org-btn"
              type="submit"
              disabled={creating || !name.trim()}
              className="sp-btn sp-btn-primary w-full justify-center mt-3 disabled:opacity-50"
            >
              {creating ? "Capturing your Soul…" : "Create organization"}
            </button>
          </form>

          <form onSubmit={acceptInvite} id="join-invite" className="sp-glass rounded-2xl p-6 scroll-mt-20">
            <div className="flex items-center gap-2 mb-3">
              <Ticket className="w-4 h-4 text-white/80" />
              <h3 className="text-sm uppercase tracking-widest sp-subtle">Join with invite code</h3>
            </div>
            <input
              data-testid="invite-code-input"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="Paste invite code"
              className="w-full bg-white/[0.03] border border-white/10 rounded-xl px-4 py-3 text-sm text-white placeholder:text-white/30 focus:border-white/30 focus:sp-focus"
            />
            <button
              data-testid="accept-invite-btn"
              type="submit"
              disabled={joining || !code.trim()}
              className="sp-btn sp-btn-ghost w-full justify-center mt-3 disabled:opacity-50"
            >
              {joining ? "Joining…" : "Accept invite"}
            </button>
          </form>
        </div>
      </main>
    </div>
  );
}
