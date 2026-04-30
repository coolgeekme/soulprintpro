import React, { useEffect, useRef, useState } from "react";
import { useParams, Link, useNavigate } from "react-router-dom";
import api, { BACKEND_URL } from "../lib/api";
import TopBar from "../components/TopBar";
import { toast } from "sonner";
import { Plus, Users, ArrowRight, Copy, UserPlus, FileText, Image as ImageIcon, FileBox, Trash2, UploadCloud, Download, File as FileIcon, MessageCircle, Pencil, Save, X as XIcon, RefreshCw } from "lucide-react";
import Logo from "../components/Logo";
import SchedulesPanel from "../components/SchedulesPanel";

const formatBytes = (n) => {
  if (!n) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
};
const KIND_ICONS = { image: ImageIcon, pdf: FileText, doc: FileText, text: FileText, audio: FileBox, video: FileBox, file: FileIcon };

export default function OrgPage() {
  const { orgId } = useParams();
  const nav = useNavigate();
  const [org, setOrg] = useState(null);
  const [teams, setTeams] = useState([]);
  const [members, setMembers] = useState([]);
  const [invites, setInvites] = useState([]);
  const [assets, setAssets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [newTeam, setNewTeam] = useState("");
  const [newTeamDesc, setNewTeamDesc] = useState("");
  const [creating, setCreating] = useState(false);
  const [showInviteTools, setShowInviteTools] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadDesc, setUploadDesc] = useState("");
  const [dragActive, setDragActive] = useState(false);
  const fileRef = useRef(null);

  // Edit-org state (owner/admin only)
  const [editOpen, setEditOpen] = useState(false);
  const [editName, setEditName] = useState("");
  const [editUrl, setEditUrl] = useState("");
  const [editSoul, setEditSoul] = useState("");
  const [savingOrg, setSavingOrg] = useState(false);
  const [regenSoul, setRegenSoul] = useState(false);
  const canEdit = org && (org.role === "owner" || org.role === "admin");
  const canDelete = org && org.role === "owner";

  const openEdit = () => {
    setEditName(org.name);
    setEditUrl(org.website_url || "");
    setEditSoul(org.soul_description || "");
    setRegenSoul(false);
    setEditOpen(true);
  };
  const saveOrg = async (e) => {
    e.preventDefault();
    setSavingOrg(true);
    try {
      const { data } = await api.patch(`/orgs/${orgId}`, {
        name: editName.trim(),
        website_url: editUrl.trim(),
        soul_description: editSoul.trim(),
        regenerate_soul: regenSoul,
      });
      setOrg(data);
      setEditOpen(false);
      toast.success(regenSoul ? "Saved — Soul re-synthesised" : "Saved");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Save failed");
    } finally { setSavingOrg(false); }
  };
  const removeOrg = async () => {
    if (!window.confirm(`Permanently delete "${org.name}" and EVERYTHING in it (teams, conversations, assets, schedules)? This cannot be undone.`)) return;
    try {
      await api.delete(`/orgs/${orgId}`);
      toast.success("Organization deleted");
      nav("/app");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Delete failed");
    }
  };

  const removeTeam = async (t) => {
    if (!window.confirm(`Delete team "${t.name}" and all its conversations? This cannot be undone.`)) return;
    try {
      await api.delete(`/teams/${t.id}`);
      setTeams((prev) => prev.filter((x) => x.id !== t.id));
      toast.success("Team deleted");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Delete failed");
    }
  };

  const canInvite = org && (org.role === "owner" || org.role === "admin");
  const origin = typeof window !== "undefined" ? window.location.origin : "";

  const load = async () => {
    setLoading(true);
    try {
      const [orgRes, teamsRes, membersRes, assetsRes] = await Promise.all([
        api.get(`/orgs/${orgId}`),
        api.get(`/orgs/${orgId}/teams`),
        api.get(`/orgs/${orgId}/members`),
        api.get(`/orgs/${orgId}/assets`),
      ]);
      setOrg(orgRes.data);
      setTeams(teamsRes.data);
      setMembers(membersRes.data);
      setAssets(assetsRes.data);
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

  const uploadFiles = async (files) => {
    if (!files || !files.length) return;
    setUploading(true);
    try {
      const sharedDesc = uploadDesc.trim();
      // Upload sequentially to keep failures clean
      for (const f of files) {
        const fd = new FormData();
        fd.append("file", f);
        if (sharedDesc) fd.append("description", sharedDesc);
        try {
          const { data } = await api.post(`/orgs/${orgId}/assets`, fd, {
            headers: { "Content-Type": "multipart/form-data" },
          });
          setAssets((prev) => [data, ...prev]);
        } catch (err) {
          toast.error(`${f.name}: ${err?.response?.data?.detail || "upload failed"}`);
        }
      }
      setUploadDesc("");
      toast.success("Asset library updated");
    } finally { setUploading(false); }
  };

  const onFileChange = (e) => {
    const files = Array.from(e.target.files || []);
    e.target.value = "";
    uploadFiles(files);
  };

  const onDrop = (e) => {
    e.preventDefault();
    setDragActive(false);
    const files = Array.from(e.dataTransfer?.files || []);
    uploadFiles(files);
  };

  const removeAsset = async (id) => {
    if (!window.confirm("Remove this asset from the library?")) return;
    try {
      await api.delete(`/orgs/${orgId}/assets/${id}`);
      setAssets((prev) => prev.filter((a) => a.id !== id));
      toast.success("Removed");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed");
    }
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

      <main className="max-w-6xl mx-auto px-4 sm:px-6 py-8 sm:py-10">
        <div className="flex flex-col gap-2 mb-8">
          <span className="sp-chip">{org.role}</span>
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <h1 className="sp-display text-4xl md:text-5xl">{org.name}</h1>
            {canEdit && !editOpen && (
              <div className="flex items-center gap-2">
                <button onClick={openEdit} className="sp-btn sp-btn-ghost text-xs" data-testid="edit-org-btn">
                  <Pencil className="w-3.5 h-3.5" /> Edit
                </button>
                {canDelete && (
                  <button onClick={removeOrg} className="sp-btn sp-btn-ghost text-xs hover:!text-red-300" data-testid="delete-org-btn">
                    <Trash2 className="w-3.5 h-3.5" /> Delete
                  </button>
                )}
              </div>
            )}
          </div>
          <p className="sp-subtle">Spin up a team for each squad, brand, or project. Each team has its own SoulPrint.</p>
        </div>

        {editOpen && (
          <form onSubmit={saveOrg} className="sp-glass-strong rounded-2xl p-6 mb-8 space-y-3" data-testid="edit-org-panel">
            <div className="flex items-center justify-between">
              <h3 className="text-sm uppercase tracking-widest sp-subtle">Edit organization</h3>
              <button type="button" onClick={() => setEditOpen(false)} className="text-white/60 hover:text-white"><XIcon className="w-4 h-4" /></button>
            </div>
            <input
              data-testid="edit-org-name"
              value={editName}
              onChange={(e) => setEditName(e.target.value)}
              placeholder="Organization name"
              className="w-full rounded-xl px-4 py-3 text-sm focus:sp-focus"
              style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
            />
            <input
              data-testid="edit-org-url"
              value={editUrl}
              onChange={(e) => setEditUrl(e.target.value)}
              placeholder="Company website (optional)"
              className="w-full rounded-xl px-4 py-3 text-sm focus:sp-focus"
              style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
            />
            <textarea
              data-testid="edit-org-soul"
              value={editSoul}
              onChange={(e) => setEditSoul(e.target.value)}
              placeholder="Soul description / founder hint (optional)"
              rows={3}
              className="w-full rounded-xl px-4 py-3 text-sm resize-none focus:sp-focus"
              style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
            />
            <label className="flex items-center gap-2 text-xs sp-subtle cursor-pointer">
              <input type="checkbox" checked={regenSoul} onChange={(e) => setRegenSoul(e.target.checked)} className="accent-orange-500" data-testid="edit-org-regen" />
              <RefreshCw className="w-3 h-3" /> Re-synthesise the company Soul from these inputs
            </label>
            <div className="flex items-center justify-end gap-2 pt-1">
              <button type="button" onClick={() => setEditOpen(false)} className="sp-btn sp-btn-ghost text-xs">Cancel</button>
              <button type="submit" disabled={savingOrg || !editName.trim()} className="sp-btn sp-btn-primary text-xs disabled:opacity-50" data-testid="save-org-btn">
                <Save className="w-3.5 h-3.5" /> {savingOrg ? "Saving…" : "Save"}
              </button>
            </div>
          </form>
        )}

        {/* General chat — auto-created on org creation. Always-on entry point. */}
        {org.general_team_id && org.general_conversation_id && (
          <Link
            to={`/app/team/${org.general_team_id}/c/${org.general_conversation_id}`}
            className="sp-glass-strong rounded-2xl p-6 mb-6 flex items-center gap-4 hover:border-white/25 transition group"
            data-testid="open-general-chat"
          >
            <div className="w-12 h-12 rounded-xl flex items-center justify-center"
                 style={{ background: "linear-gradient(160deg, rgba(246,64,0,0.7), rgba(246,64,0,0.15))" }}>
              <MessageCircle className="w-5 h-5 text-white" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-white font-medium text-lg">General chat</div>
              <div className="text-xs sp-subtle">Always-on. Just type — the right specialist will respond, and others jump in when the topic shifts.</div>
            </div>
            <ArrowRight className="w-5 h-5 text-white/60 group-hover:text-white transition" />
          </Link>
        )}

        {/* Company Soul — populated from URL scrape + LLM on creation */}
        {(org.soul && (org.soul.mission || org.soul.summary)) && (
          <div className="sp-glass rounded-2xl p-6 mb-8" data-testid="org-soul">
            <div className="flex items-center gap-2 mb-3">
              <Logo className="w-4 h-4 text-white/80" />
              <h3 className="text-sm uppercase tracking-widest sp-subtle">Company Soul</h3>
              {org.website_url && (
                <a href={org.website_url.startsWith("http") ? org.website_url : `https://${org.website_url}`}
                   target="_blank" rel="noreferrer"
                   className="text-[11px] sp-subtle ml-auto hover:text-white truncate max-w-[260px]"
                   title={org.website_url}>
                  {org.website_url.replace(/^https?:\/\//, "")}
                </a>
              )}
            </div>
            {org.soul.summary && (
              <p className="text-base mb-3 leading-relaxed" style={{ color: "var(--fg)" }}>
                {org.soul.summary}
              </p>
            )}
            <div className="grid sm:grid-cols-2 gap-3 text-sm">
              {[
                ["Mission", org.soul.mission],
                ["Audience", org.soul.audience],
                ["Tone", org.soul.tone],
                ["Values", org.soul.values],
                ["North star", org.soul.north_star],
              ].filter(([, v]) => v).map(([label, v]) => (
                <div key={label} className="sp-glass rounded-xl p-3">
                  <div className="text-[10px] uppercase tracking-widest sp-subtle mb-1">{label}</div>
                  <div className="leading-snug">{v}</div>
                </div>
              ))}
            </div>
          </div>
        )}

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
          {teams.filter((t) => t.id !== org.general_team_id).map((t) => (
            <div key={t.id} className="sp-glass rounded-2xl p-6 hover:border-white/20 transition group relative" data-testid={`team-card-${t.id}`}>
              {canEdit && (
                <button
                  onClick={(e) => { e.preventDefault(); e.stopPropagation(); removeTeam(t); }}
                  className="absolute top-3 right-3 sp-btn sp-btn-ghost !p-1.5 opacity-0 group-hover:opacity-100 transition hover:!text-red-300"
                  data-testid={`delete-team-${t.id}`}
                  title="Delete team"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              )}
              <Link to={`/app/team/${t.id}`} className="block">
                <div className="w-10 h-10 rounded-xl sp-glass-strong flex items-center justify-center mb-4">
                  <Users className="w-5 h-5 text-white/90" />
                </div>
                <div className="text-white font-medium text-lg truncate">{t.name}</div>
                {t.description && <div className="text-xs sp-subtle mt-1 line-clamp-2">{t.description}</div>}
                <div className="flex items-center gap-2 mt-4 text-xs sp-subtle group-hover:text-white/80">
                  Open workspace <ArrowRight className="w-3.5 h-3.5" />
                </div>
              </Link>
            </div>
          ))}
          {teams.filter((t) => t.id !== org.general_team_id).length === 0 && (
            <div className="sp-glass rounded-2xl p-6 col-span-full text-center text-sm sp-subtle">
              No specialised teams yet — create one below for a specific squad, brand, or project.
            </div>
          )}
        </div>

        {/* Asset library */}
        <SchedulesPanel scope="org" targetId={orgId} orgId={orgId} />
        <div className="h-6" />
        <section className="sp-glass-strong rounded-2xl p-6 mb-10" data-testid="assets-section">
          <div className="flex items-center justify-between mb-4 flex-wrap gap-3">
            <div className="flex items-center gap-2">
              <UploadCloud className="w-4 h-4" />
              <h3 className="text-sm uppercase tracking-widest sp-subtle">Asset library</h3>
              <span className="sp-chip">{assets.length}</span>
            </div>
            <p className="text-xs sp-subtle max-w-md">
              Upload logos, brand guides, handbooks, photos, copy decks. Every conversation in this org will be able to reference these.
            </p>
          </div>

          <div
            onDragOver={(e) => { e.preventDefault(); setDragActive(true); }}
            onDragLeave={() => setDragActive(false)}
            onDrop={onDrop}
            className={`sp-glass rounded-xl p-6 border-2 border-dashed flex flex-col items-center text-center gap-3 transition ${dragActive ? "border-orange-400/60" : ""}`}
            style={{ borderColor: dragActive ? "rgba(124,92,255,0.6)" : "var(--border-soft)" }}
            data-testid="assets-dropzone"
          >
            <UploadCloud className="w-6 h-6 opacity-70" />
            <div className="text-base">
              Drop files here, or{" "}
              <button
                type="button"
                onClick={() => fileRef.current?.click()}
                className="underline underline-offset-4 hover:opacity-90"
                data-testid="assets-browse-btn"
              >browse</button>
            </div>
            <div className="text-xs sp-subtle">PDF · DOCX · images · text · audio · video · zip · up to 10 MB each</div>
            <input
              type="text"
              value={uploadDesc}
              onChange={(e) => setUploadDesc(e.target.value)}
              placeholder="Optional description (applied to next upload)"
              className="w-full max-w-md rounded-lg px-3 py-2 text-sm focus:sp-focus"
              style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
              data-testid="asset-desc-input"
            />
            <input
              ref={fileRef}
              type="file"
              multiple
              hidden
              onChange={onFileChange}
              data-testid="assets-file-input"
            />
            {uploading && <div className="text-xs sp-subtle">Uploading…</div>}
          </div>

          {assets.length > 0 && (
            <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-3 mt-4">
              {assets.map((a) => {
                const Icon = KIND_ICONS[a.kind] || FileIcon;
                const isImg = a.kind === "image";
                const url = `${BACKEND_URL}${a.url}`;
                return (
                  <div key={a.id} className="sp-glass rounded-xl p-3 flex flex-col gap-2" data-testid={`asset-card-${a.id}`}>
                    <div className="flex items-start gap-2">
                      <div className="w-9 h-9 rounded-lg sp-glass-strong flex items-center justify-center shrink-0">
                        <Icon className="w-4 h-4" />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="text-sm font-medium truncate" title={a.original_name}>{a.original_name}</div>
                        <div className="text-[11px] sp-subtle">
                          {a.kind} · {formatBytes(a.size)} · by {a.uploaded_by_name}
                        </div>
                      </div>
                    </div>
                    {a.description && <div className="text-xs sp-subtle line-clamp-2">{a.description}</div>}
                    {isImg && (
                      <a href={url} target="_blank" rel="noreferrer" className="block rounded-lg overflow-hidden border" style={{ borderColor: "var(--border-soft)" }}>
                        <img src={url} alt={a.original_name} loading="lazy" className="w-full max-h-32 object-cover" />
                      </a>
                    )}
                    <div className="flex items-center justify-end gap-1 mt-auto">
                      <a href={url} target="_blank" rel="noreferrer" className="sp-btn sp-btn-ghost text-xs" download>
                        <Download className="w-3 h-3" /> Open
                      </a>
                      <button onClick={() => removeAsset(a.id)} className="sp-btn sp-btn-ghost text-xs" data-testid={`asset-delete-${a.id}`}>
                        <Trash2 className="w-3 h-3" />
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </section>

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
