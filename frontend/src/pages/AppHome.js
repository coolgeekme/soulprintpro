import React, { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import api from "../lib/api";
import TopBar from "../components/TopBar";
import { toast } from "sonner";
import { Plus, Building2, ArrowRight, Ticket } from "lucide-react";

export default function AppHome() {
  const [orgs, setOrgs] = useState([]);
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
      const { data } = await api.get("/orgs");
      setOrgs(data);
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
      // Jump straight into the auto-created General chat
      if (data.general_team_id && data.general_conversation_id) {
        nav(`/app/team/${data.general_team_id}/c/${data.general_conversation_id}`);
      } else {
        nav(`/app/org/${data.id}`);
      }
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
      const { data } = await api.post("/invites/accept", { code: code.trim() });
      toast.success(data.already_member ? "Already a member" : "Joined organization");
      nav(`/app/org/${data.org_id}`);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Invalid invite");
    } finally {
      setJoining(false);
    }
  };

  return (
    <div className="min-h-screen">
      <TopBar>Your organizations</TopBar>

      <main className="max-w-5xl mx-auto px-6 py-12">
        <div className="flex flex-col gap-3 mb-10">
          <span className="sp-chip">Workspaces</span>
          <h1 className="sp-display text-4xl md:text-5xl">Where do you want to work today?</h1>
          <p className="sp-subtle max-w-xl">Pick an organization below, create a new one for a different company, or accept a teammate's invite.</p>
        </div>

        <div className="grid md:grid-cols-3 gap-4 mb-10">
          {loading && [0,1,2].map((i) => <div key={i} className="sp-glass rounded-2xl p-6 h-36 animate-pulse" />)}

          {!loading && orgs.map((o) => (
            <Link
              key={o.id}
              to={`/app/org/${o.id}`}
              className="sp-glass rounded-2xl p-6 hover:border-white/20 transition group"
              data-testid={`org-card-${o.id}`}
            >
              <div className="flex items-center justify-between mb-4">
                <div className="w-10 h-10 rounded-xl sp-glass-strong flex items-center justify-center">
                  <Building2 className="w-5 h-5 text-white/90" />
                </div>
                <span className="sp-chip">{o.role}</span>
              </div>
              <div className="text-white font-medium text-lg truncate">{o.name}</div>
              <div className="flex items-center gap-2 mt-4 text-xs sp-subtle group-hover:text-white/80">
                Open <ArrowRight className="w-3.5 h-3.5" />
              </div>
            </Link>
          ))}

          {!loading && orgs.length === 0 && (
            <div className="sp-glass rounded-2xl p-6 col-span-full text-center text-sm sp-subtle">
              No organizations yet. Create one below to get started.
            </div>
          )}
        </div>

        <div className="grid md:grid-cols-2 gap-4">
          <form onSubmit={createOrg} className="sp-glass rounded-2xl p-6">
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
              type="url"
              value={websiteUrl}
              onChange={(e) => setWebsiteUrl(e.target.value)}
              placeholder="Company website (optional) — we'll read it to capture your Soul"
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
              We synthesise your company "Soul" — mission, audience, voice — and inject it into every agent. A General chat is created instantly so you can start talking.
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

          <form onSubmit={acceptInvite} className="sp-glass rounded-2xl p-6">
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
