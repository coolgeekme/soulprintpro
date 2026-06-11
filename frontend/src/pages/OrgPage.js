import React, { useCallback, useEffect, useRef, useState } from "react";
import { useParams, Link, useNavigate, useLocation } from "react-router-dom";
import api, { BACKEND_URL } from "../lib/api";
import TopBar from "../components/TopBar";
import { toast } from "sonner";
import { Plus, Users, ArrowRight, Copy, UserPlus, FileText, Image as ImageIcon, FileBox, Trash2, UploadCloud, Download, File as FileIcon, MessageCircle, Pencil, Save, X as XIcon, RefreshCw, Megaphone, Paperclip, Send, Loader2, Rocket, Network, Minimize2, ChevronRight, BarChart3, Link as LinkIcon, ExternalLink, Search } from "lucide-react";
import BrandAssetsPanel from "../components/BrandAssetsPanel";
import NotesPage from "./NotesPage";
import GlobalNoteButton from "../components/GlobalNoteButton";
import CustomAgentsPanel from "../components/CustomAgentsPanel";
import ActivityOverview from "../components/ActivityOverview";
import DocumentsPanel from "../components/DocumentsPanel";
import SocialStyleGuidePanel from "../components/SocialStyleGuidePanel";
import SpendDashboard from "../components/SpendDashboard";
import RecentConversations from "../components/RecentConversations";
import CollapsibleSection from "../components/CollapsibleSection";
import Logo from "../components/Logo";
import SchedulesPanel from "../components/SchedulesPanel";
import SoulSuggestionsPanel from "../components/SoulSuggestionsPanel";
import ConnectionsPanel from "../components/ConnectionsPanel";
import AgentToolPrefsPanel from "../components/AgentToolPrefsPanel";
import WorkingIndicator from "../components/WorkingIndicator";
import { compressImageFile } from "../lib/imageCompress";
import { parseServerDate } from "../lib/datetime";

const formatBytes = (n) => {
  if (!n) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
};
const KIND_ICONS = { image: ImageIcon, pdf: FileText, doc: FileText, text: FileText, audio: FileBox, video: FileBox, file: FileIcon, link: LinkIcon };

export default function OrgPage() {
  const { orgId } = useParams();
  const nav = useNavigate();
  const location = useLocation();
  // When the user arrives via `Open in Documents` (link with #documents),
  // React Router doesn't auto-scroll. We do it manually after the panel
  // has had a chance to mount + load. Small retry loop so it works even
  // if the data is still fetching when we land.
  useEffect(() => {
    const hash = (location.hash || "").replace("#", "");
    if (!hash) return undefined;
    // Several deep-link anchors (#connections, #agent-tools, #org-soul) live
    // INSIDE the "Org setup & insights" accordion which is collapsed by
    // default. If the user lands with one of those hashes, force the
    // accordion open by pre-seeding its localStorage key before the
    // CollapsibleSection mounts. Idempotent — only writes when needed.
    const INSIDE_ADMIN_ACCORDION = new Set([
      "connections", "agent-tools", "org-soul", "documents",
    ]);
    if (INSIDE_ADMIN_ACCORDION.has(hash)) {
      try {
        const key = `sp-collapse:org-admin-${orgId}`;
        if (localStorage.getItem(key) !== "1") {
          localStorage.setItem(key, "1");
        }
      } catch (_) { /* ignore quota / privacy mode */ }
    }
    let attempts = 0;
    const tick = () => {
      const el = document.getElementById(hash);
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "start" });
        // Brief highlight ring so the user notices where the page jumped.
        el.classList.add("sp-anchor-flash");
        setTimeout(() => el.classList.remove("sp-anchor-flash"), 1800);
        return;
      }
      if (++attempts < 12) setTimeout(tick, 250);  // ~3 seconds total
    };
    tick();
  }, [location.hash, orgId]);
  const [org, setOrg] = useState(null);
  const [teams, setTeams] = useState([]);
  const [members, setMembers] = useState([]);
  const [invites, setInvites] = useState([]);
  const [assets, setAssets] = useState([]);
  // ── Asset library — collapsed-by-default beyond a soft cap so the
  // org page doesn't get noisy once teams have uploaded a lot of files.
  const ASSETS_INITIAL_LIMIT = 6;
  const [assetsExpanded, setAssetsExpanded] = useState(false);
  const [loading, setLoading] = useState(true);
  // Per-user PRIVATE Chief of Staff threads. The backend auto-creates the
  // first thread on demand; users can spin up additional threads via
  // the "+ New chat" button so long-running CoS conversations don't get
  // tangled into a single endless message log.
  const [myCos, setMyCos] = useState(null);
  const [myCosThreads, setMyCosThreads] = useState([]);
  const [cosExpanded, setCosExpanded] = useState(false);
  const [cosCreating, setCosCreating] = useState(false);
  const [newTeam, setNewTeam] = useState("");
  const [newTeamDesc, setNewTeamDesc] = useState("");
  const [newTeamVisibility, setNewTeamVisibility] = useState("open");
  const [creating, setCreating] = useState(false);
  const [showInviteTools, setShowInviteTools] = useState(false);
  // Ref to the invite panel so the TopBar "Invite" button can scroll
  // it into view + flash a focus ring. Without this, clicking Invite
  // from the bottom of a long org page toggled state silently because
  // the panel lived hundreds of pixels above the viewport — looked
  // like "nothing happens".
  const inviteToolsRef = useRef(null);
  const createInviteRef = useRef(null);
  const openInviteTools = useCallback(() => {
    setShowInviteTools(true);
    // Auto-mint a first invite if none exist — same UX as the
    // inline "Create invite" CTA, so the topbar action always
    // produces a tangible result on first click.
    if (Array.isArray(invites) && invites.length === 0) {
      createInviteRef.current?.("member");
    }
    // Scroll on next paint so the panel is in the DOM. Use a small
    // timeout instead of requestAnimationFrame because the panel may
    // be mounted for the FIRST time on this click and React's render
    // hasn't flushed yet on a single rAF.
    setTimeout(() => {
      inviteToolsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 60);
  }, [invites]);
  // Seat policy (soft enforcement) — fetched when the invite UI opens
  // and refreshed every time an invite is created. Drives the in-product
  // banner that nudges over-cap admins toward upgrading or buying seats.
  const [seatStatus, setSeatStatus] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [uploadDesc, setUploadDesc] = useState("");
  const [uploadCategory, setUploadCategory] = useState(""); // product|lifestyle|team|venue|logo_variant|other
  const [dragActive, setDragActive] = useState(false);
  const fileRef = useRef(null);
  // External link adder (Google Drive / Dropbox / Notion / Figma …)
  const [linkUrl, setLinkUrl] = useState("");
  const [linkName, setLinkName] = useState("");
  const [linkBusy, setLinkBusy] = useState(false);
  // In-library quick search: keyword filter over name, description and
  // extracted text excerpts. Empty = show all. We hit the backend
  // /assets/search endpoint when the user types so excerpts get
  // matched too (not just filenames).
  const [assetQuery, setAssetQuery] = useState("");
  const [assetSearchHits, setAssetSearchHits] = useState(null); // null = idle (show all)
  const [assetSearchBusy, setAssetSearchBusy] = useState(false);
  useEffect(() => {
    const q = assetQuery.trim();
    if (!q) { setAssetSearchHits(null); return; }
    const id = setTimeout(async () => {
      setAssetSearchBusy(true);
      try {
        const { data } = await api.get(`/orgs/${orgId}/assets/search`, { params: { q, limit: 50 } });
        setAssetSearchHits(data.results || []);
      } catch (err) {
        // Silent — toast would spam on every keystroke.
        setAssetSearchHits([]);
      } finally { setAssetSearchBusy(false); }
    }, 220);
    return () => clearTimeout(id);
  }, [assetQuery, orgId]);

  // Edit-org state (owner/admin only)
  const [editOpen, setEditOpen] = useState(false);
  const [editName, setEditName] = useState("");
  const [editUrl, setEditUrl] = useState("");
  const [editSoul, setEditSoul] = useState("");
  const [editArchiveIdle, setEditArchiveIdle] = useState(0);
  const [savingOrg, setSavingOrg] = useState(false);
  const [regenSoul, setRegenSoul] = useState(false);
  const canEdit = org && (org.role === "owner" || org.role === "admin");
  const canDelete = org && org.role === "owner";
  const isOwner = org && org.role === "owner";

  const openEdit = () => {
    setEditName(org.name);
    setEditUrl(org.website_url || "");
    setEditSoul(org.soul_description || "");
    setEditArchiveIdle(Number(org.archive_idle_days || 0));
    setRegenSoul(false);
    setEditOpen(true);
  };
  const saveOrg = async (e) => {
    e.preventDefault();
    setSavingOrg(true);
    try {
      const body = {
        name: editName.trim(),
        website_url: editUrl.trim(),
      };
      // Soul fields are owner-only — backend enforces this too. Don't even
      // send them for admins so we don't trigger a 403 on otherwise-valid
      // name / website edits.
      if (isOwner) {
        body.soul_description = editSoul.trim();
        body.regenerate_soul = regenSoul;
        body.archive_idle_days = Number(editArchiveIdle) || 0;
      }
      const { data } = await api.patch(`/orgs/${orgId}`, body);
      setOrg(data);
      setEditOpen(false);
      toast.success(isOwner && regenSoul ? "Saved — Soul re-synthesised" : "Saved");
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
      // Use allSettled so a transient failure on members/teams/assets
      // doesn't blow up the whole page. The primary /orgs/{id} call is
      // the only hard prerequisite — if it fails, we bounce; otherwise
      // we render with whatever we got and surface a specific error.
      const [orgRes, teamsRes, membersRes, assetsRes] = await Promise.allSettled([
        api.get(`/orgs/${orgId}`),
        api.get(`/orgs/${orgId}/teams`),
        api.get(`/orgs/${orgId}/members`),
        api.get(`/orgs/${orgId}/assets`),
      ]);
      if (orgRes.status === "rejected") {
        const err = orgRes.reason;
        const code = err?.response?.status;
        const detail = err?.response?.data?.detail || err?.message || "Unknown error";
        console.error("Failed /orgs/" + orgId, { code, detail, err });
        if (code === 404) {
          toast.error("Organization not found.");
        } else if (code === 403) {
          toast.error("You don't have access to this organization.");
        } else {
          toast.error(`Failed to load organization (${code || "network"}): ${detail}`);
        }
        nav("/app");
        return;
      }
      setOrg(orgRes.value.data);
      // Sub-resource failures degrade gracefully — log + toast but
      // keep the page renderable. This avoids a single dodgy member
      // record or asset blob taking down the whole org page.
      if (teamsRes.status === "fulfilled") {
        setTeams(teamsRes.value.data);
      } else {
        console.warn("Failed to load teams:", teamsRes.reason);
        setTeams([]);
      }
      if (membersRes.status === "fulfilled") {
        setMembers(membersRes.value.data);
      } else {
        console.warn("Failed to load members:", membersRes.reason);
        setMembers([]);
      }
      if (assetsRes.status === "fulfilled") {
        setAssets(assetsRes.value.data);
      } else {
        console.warn("Failed to load assets:", assetsRes.reason);
        setAssets([]);
      }
      // If any sub-resource failed, surface ONE consolidated toast so the
      // user knows the page is partially loaded (not "everything's fine").
      const failedSubs = [
        ["teams", teamsRes],
        ["members", membersRes],
        ["assets", assetsRes],
      ].filter(([, r]) => r.status === "rejected");
      if (failedSubs.length > 0) {
        const names = failedSubs.map(([n]) => n).join(", ");
        toast.error(`Couldn't load: ${names}. Page is partially loaded.`, { duration: 6000 });
      }
      if (orgRes.value.data.role === "owner" || orgRes.value.data.role === "admin") {
        try {
          const { data } = await api.get(`/orgs/${orgId}/invites`);
          setInvites(data);
        } catch (err) {
          // Soft-fail — invite list is a nice-to-have on the org page
          // and we don't want to block the whole page render on a 403
          // / 500 from the invites endpoint. Log so it shows up in the
          // browser console for debugging without alarming the user.
          console.warn("Failed to load invites:", err?.message);
        }
      }
      // Lazy-create the caller's PRIVATE Chief of Staff thread for this
      // org and fetch the full list of their threads in parallel. The
      // /my-cos call is idempotent — first call provisions, every call
      // after returns the most-recent thread.
      try {
        const [cosRes, threadsRes] = await Promise.all([
          api.get(`/orgs/${orgId}/my-cos`),
          api.get(`/orgs/${orgId}/my-cos/threads`).catch(() => ({ data: { threads: [] } })),
        ]);
        setMyCos(cosRes.data);
        setMyCosThreads(threadsRes.data?.threads || []);
      } catch (_) { /* non-fatal — user can still use the shared CoS */ }
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
        visibility: newTeamVisibility,
      });
      toast.success(`${data.visibility === "closed" ? "Closed" : "Open"} team created`);
      nav(`/app/team/${data.id}`);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed");
    } finally { setCreating(false); }
  };

  // Open team self-join — non-members click "Join team" on the card and
  // are immediately added to `member_user_ids`. Closed teams reject this
  // server-side; the UI only shows the button when `can_join=true`.
  const joinTeam = async (t) => {
    try {
      await api.post(`/teams/${t.id}/join`);
      const { data } = await api.get(`/orgs/${orgId}/teams`);
      setTeams(data);
      toast.success(`Joined ${t.name}`);
      nav(`/app/team/${t.id}`);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not join this team");
    }
  };

  // Optional email address — when filled, the API fires a Resend email
  // to the invitee with the accept link. When blank, we just persist
  // the invite and the owner copies the link manually.
  const [inviteEmail, setInviteEmail] = useState("");

  const refreshSeatStatus = useCallback(async () => {
    if (!orgId || !canInvite) return;
    try {
      const { data } = await api.get(`/orgs/${orgId}/seat-status`);
      setSeatStatus(data);
    } catch (e) {
      // Don't block the UI on seat-status errors — soft enforcement
      // is purely informational. Failing silently is the right call.
    }
  }, [orgId, canInvite]);

  // Load seat status whenever the invite tools first open. We rely on
  // refreshSeatStatus from inside createInvite to keep it fresh.
  useEffect(() => {
    if (showInviteTools) refreshSeatStatus();
  }, [showInviteTools, refreshSeatStatus]);

  const createInvite = async (role) => {
    try {
      const emailRaw = inviteEmail.trim().toLowerCase();
      const { data } = await api.post(`/orgs/${orgId}/invites`, {
        role, email: emailRaw || undefined,
      });
      setInvites((prev) => [{
        code: data.code, role, expires_at: data.expires_at,
        email: emailRaw || "",
      }, ...prev]);
      setInviteEmail("");
      toast.success(
        data.email_sent
          ? `Invite sent to ${emailRaw}`
          : `Invite created (${role})`,
      );
      // Backend already computed post-insert seat status — adopt it
      // so the banner updates without a round-trip.
      if (data.seat_status) {
        setSeatStatus({ ...data.seat_status, warning: data.seat_warning });
      } else {
        refreshSeatStatus();
      }
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed");
    }
  };
  // Mirror `createInvite` into a ref so `openInviteTools` (declared
  // earlier — React hook ordering forces it to live above this) can
  // invoke the latest closure without a temporal-dead-zone error.
  useEffect(() => {
    createInviteRef.current = createInvite;
  });

  const copy = (text) => {
    navigator.clipboard.writeText(text).then(() => toast.success("Copied"));
  };

  const uploadFiles = async (files) => {
    if (!files || !files.length) return;
    setUploading(true);
    try {
      const sharedDesc = uploadDesc.trim();
      const sharedCat = uploadCategory.trim();
      // Upload sequentially to keep failures clean
      for (const f of files) {
        const fd = new FormData();
        fd.append("file", f);
        if (sharedDesc) fd.append("description", sharedDesc);
        if (sharedCat) fd.append("category", sharedCat);
        // Images default to usable-in-social; user can flip per-asset later.
        fd.append("usable_in_social", "true");
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

  // Save an external URL (Google Drive / Dropbox / Notion / Figma /
  // generic) into the Asset Library. Provider is auto-detected on the
  // backend from the URL host so the user doesn't need to pick one.
  const addAssetLink = async (e) => {
    e?.preventDefault?.();
    const url = linkUrl.trim();
    if (!url || linkBusy) return;
    if (!/^https?:\/\//i.test(url)) {
      toast.error("URL must start with http:// or https://");
      return;
    }
    setLinkBusy(true);
    try {
      const { data } = await api.post(`/orgs/${orgId}/asset-links`, {
        url,
        name: linkName.trim(),
        description: uploadDesc.trim(),
        category: uploadCategory.trim(),
      });
      setAssets((prev) => [data, ...prev]);
      setLinkUrl("");
      setLinkName("");
      toast.success(`Saved ${data.link_provider || "link"} to library`);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't save link");
    } finally { setLinkBusy(false); }
  };

  if (loading) {
    return <div className="min-h-screen"><TopBar>Loading…</TopBar></div>;
  }

  return (
    <div className="min-h-screen">
      <GlobalNoteButton orgId={orgId} onNavigateToNotes={() => {
        // Scroll to the Notes section + open the editor by setting
        // hash NotesPage will read on mount.
        if (typeof document !== "undefined") {
          document.getElementById("notes")?.scrollIntoView({ behavior: "smooth", block: "start" });
        }
        if (typeof window !== "undefined") window.location.hash = "#notes:new";
      }} />
      <TopBar
        right={
          canInvite && (
            <button
              onClick={openInviteTools}
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
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div className="flex items-center gap-3 flex-wrap min-w-0">
              <h1 className="sp-display text-4xl md:text-5xl truncate">{org.name}</h1>
              {/* UX audit #6: OWNER role rendered as a small inline pill
                  next to the org name instead of a full-width banner.
                  Members aren't shown a role chip (they only see one if
                  they're admin or owner). */}
              {(org.role === "owner" || org.role === "admin") && (
                <span
                  className={`sp-chip text-[10px] !px-2 !py-0.5 ${
                    org.role === "owner"
                      ? "!bg-orange-400/20 !text-orange-200 !border-orange-400/30"
                      : "!bg-sky-400/15 !text-sky-200 !border-sky-400/30"
                  }`}
                  data-testid="org-role-badge"
                >
                  {org.role.toUpperCase()}
                </span>
              )}
            </div>
            {canEdit && !editOpen && (
              <div className="flex items-center gap-2">
                <button onClick={openEdit} className="sp-btn sp-btn-ghost text-xs" data-testid="edit-org-btn">
                  <Pencil className="w-3.5 h-3.5" /> Edit
                </button>
                {canDelete && (
                  /* UX audit #4: Delete is destructive — keep it ghost-
                     weight (not equal to Edit), stamp it with a red
                     outline to read clearly as dangerous, and require
                     an explicit window.confirm before firing. */
                  <button
                    onClick={() => {
                      const ok = window.confirm(
                        `Delete "${org.name}"? This cannot be undone — every team, conversation, and document will be permanently removed.`,
                      );
                      if (ok) removeOrg();
                    }}
                    className="sp-btn sp-btn-ghost text-xs !text-red-300 !border-red-400/30 hover:!bg-red-400/10 hover:!text-red-200"
                    data-testid="delete-org-btn"
                  >
                    <Trash2 className="w-3.5 h-3.5" /> Delete
                  </button>
                )}
              </div>
            )}
          </div>
          {/* UX audit #7: pull the org's actual Mission from Company
              Soul as the subtitle. Falls back to a prompt encouraging
              the owner to fill it in. */}
          <p className="sp-subtle text-sm leading-relaxed max-w-3xl">
            {org?.soul?.mission
              ? (org.soul.mission.length > 140 ? `${org.soul.mission.slice(0, 137)}…` : org.soul.mission)
              : (canEdit
                ? "Add a mission statement to complete your Company Soul →"
                : "Spin up a team for each squad, brand, or project.")}
          </p>
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
              type="text"
              inputMode="url"
              autoCapitalize="off"
              autoCorrect="off"
              value={editUrl}
              onChange={(e) => setEditUrl(e.target.value)}
              placeholder="Company website — e.g. disciplinedos.com (optional)"
              className="w-full rounded-xl px-4 py-3 text-sm focus:sp-focus"
              style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
            />
            <textarea
              data-testid="edit-org-soul"
              value={editSoul}
              onChange={(e) => setEditSoul(e.target.value)}
              placeholder="Soul description / founder hint (optional)"
              rows={3}
              className="w-full rounded-xl px-4 py-3 text-sm resize-none focus:sp-focus disabled:opacity-50 disabled:cursor-not-allowed"
              style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
              disabled={!isOwner}
              title={isOwner ? "" : "Only the org owner can change the company Soul. Submit a suggestion via the Soul change-requests panel below."}
            />
            {!isOwner && (
              <p className="text-[11px] sp-subtle italic -mt-1">
                Soul changes are owner-only. Use the <strong>Soul change requests</strong> panel below to propose updates.
              </p>
            )}
            {isOwner && (
              <label className="flex items-center gap-2 text-xs sp-subtle cursor-pointer">
                <input type="checkbox" checked={regenSoul} onChange={(e) => setRegenSoul(e.target.checked)} className="accent-orange-500" data-testid="edit-org-regen" />
                <RefreshCw className="w-3 h-3" /> Re-synthesise the company Soul from these inputs
              </label>
            )}
            {isOwner && (
              <div className="pt-1">
                <label htmlFor="edit-org-archive-idle" className="text-[11px] uppercase tracking-widest sp-subtle block mb-1">
                  Auto-archive idle conversations
                </label>
                <select
                  id="edit-org-archive-idle"
                  value={editArchiveIdle}
                  onChange={(e) => setEditArchiveIdle(Number(e.target.value))}
                  data-testid="edit-org-archive-idle"
                  className="w-full bg-white/[0.03] border border-white/10 rounded-xl px-4 py-3 text-sm text-white focus:border-white/30 focus:sp-focus"
                >
                  <option value={0}>Off — only manual archive</option>
                  <option value={30}>After 30 days of no activity</option>
                  <option value={60}>After 60 days of no activity</option>
                  <option value={90}>After 90 days of no activity</option>
                </select>
                <p className="text-[11px] sp-subtle mt-1">
                  Archived threads stay searchable and keep feeding agent memory. The always-on Chief of Staff thread is exempt.
                </p>
              </div>
            )}
            <div className="flex items-center justify-end gap-2 pt-1">
              <button type="button" onClick={() => setEditOpen(false)} className="sp-btn sp-btn-ghost text-xs">Cancel</button>
              <button type="submit" disabled={savingOrg || !editName.trim()} className="sp-btn sp-btn-primary text-xs disabled:opacity-50" data-testid="save-org-btn">
                <Save className="w-3.5 h-3.5" /> {savingOrg ? "Saving…" : "Save"}
              </button>
            </div>
            {savingOrg && isOwner && regenSoul && (
              <WorkingIndicator
                label="Re-synthesising your company Soul"
                hints={[
                  "Reading your website, description, and any uploaded assets…",
                  "Distilling mission, audience, tone, and north-star — usually 12-25s.",
                  "Aligning every agent's brain with the new Soul…",
                  "Almost there — saving the Soul so all 169 agents stay on-brand.",
                ]}
                testId="soul-resynth-working-indicator"
              />
            )}
          </form>
        )}

        {/* ────────────────────────────────────────────────────────────
            WORK FIRST — everything above the fold is "get started now".
            Admin / setup panels live inside the collapsible section below.
            ──────────────────────────────────────────────────────────── */}

        {/* General chat — auto-created on org creation. Always-on entry point.
            Doubles as a quick-upload zone: drop, paste, or paperclip-pick
            files (and an optional prompt) to post them straight into General
            without leaving the org page. */}
        {org.general_team_id && org.general_conversation_id && (
          <GeneralChatCard
            orgId={orgId}
            teamId={org.general_team_id}
            convId={org.general_conversation_id}
          />
        )}

        {/* Social Queue — jump straight in. */}
        <Link
          to={`/app/org/${orgId}/social`}
          className="sp-glass rounded-2xl p-5 mb-4 flex items-center gap-4 hover:border-white/25 transition group"
          data-testid="open-social-queue"
        >
          <div className="w-10 h-10 rounded-xl flex items-center justify-center"
               style={{ background: "linear-gradient(160deg, rgba(246,64,0,0.5), rgba(246,64,0,0.1))" }}>
            <Megaphone className="w-4 h-4 text-white" />
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-white font-medium">Social Queue</div>
            <div className="text-xs sp-subtle">Generate posts with your agents, schedule them, then one-click open in any platform's web composer.</div>
          </div>
          <ArrowRight className="w-4 h-4 text-white/60 group-hover:text-white transition" />
        </Link>

        {/* Business audits — automated 90-second business analyses. */}
        <Link
          to="/app/audits"
          className="sp-glass rounded-2xl p-5 mb-4 flex items-center gap-4 hover:border-white/25 transition group"
          data-testid="open-audits"
        >
          <div className="w-10 h-10 rounded-xl flex items-center justify-center"
               style={{ background: "linear-gradient(160deg, rgba(251,146,60,0.45), rgba(251,146,60,0.08))" }}>
            <BarChart3 className="w-4 h-4 text-white" />
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-white font-medium">Business audits</div>
            <div className="text-xs sp-subtle">Drop a website — get a scored brief with gaps, opportunities, and a downloadable PDF in ~90 seconds. Great for prospecting and client onboarding.</div>
          </div>
          <ArrowRight className="w-4 h-4 text-white/60 group-hover:text-white transition" />
        </Link>

        {/* Missions — autonomous multi-step agent runs. */}
        <Link
          to={`/app/org/${orgId}/missions`}
          className="sp-glass rounded-2xl p-5 mb-4 flex items-center gap-4 hover:border-white/25 transition group"
          data-testid="open-missions"
        >
          <div className="w-10 h-10 rounded-xl flex items-center justify-center"
               style={{ background: "linear-gradient(160deg, rgba(96,165,250,0.45), rgba(96,165,250,0.08))" }}>
            <Rocket className="w-4 h-4 text-white" />
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-white font-medium">Missions</div>
            <div className="text-xs sp-subtle">Give an agent a goal — it picks tools, researches, writes artifacts, and ships deliverables autonomously. Pause or answer anytime.</div>
          </div>
          <ArrowRight className="w-4 h-4 text-white/60 group-hover:text-white transition" />
        </Link>

        {/* Search everywhere — unified search across the local Asset
            Library + each Composio-connected external storage source
            (Google Drive, Dropbox, Notion, OneDrive). Fans out in
            parallel; sources you haven't connected stay greyed-out
            with a "connect to search" CTA. */}
        <Link
          to={`/app/org/${orgId}/search`}
          className="sp-glass rounded-2xl p-5 mb-4 flex items-center gap-4 hover:border-white/25 transition group"
          data-testid="open-org-search"
        >
          <div className="w-10 h-10 rounded-xl flex items-center justify-center"
               style={{ background: "linear-gradient(160deg, rgba(168,85,247,0.45), rgba(168,85,247,0.08))" }}>
            <Search className="w-4 h-4 text-white" />
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-white font-medium">Search everywhere</div>
            <div className="text-xs sp-subtle">One search across your uploaded Asset Library, Google Drive, Dropbox, Notion, OneDrive — whichever you've connected. Live, never stored.</div>
          </div>
          <ArrowRight className="w-4 h-4 text-white/60 group-hover:text-white transition" />
        </Link>

        {/* Your private Chief of Staff — per-user threads visible only
            to you (and the org owner for audit). Always shown, even for
            the owner (they get their own private CoS too). Multi-thread
            picker: tap the card to expand the thread list, or click
            "+ New chat" to start a fresh conversation rather than piling
            everything into one ever-growing thread. */}
        {myCos && (
          <MyChiefOfStaffCard
            orgId={orgId}
            primary={myCos}
            threads={myCosThreads}
            expanded={cosExpanded}
            onToggle={() => setCosExpanded((v) => !v)}
            creating={cosCreating}
            onNewThread={async () => {
              if (cosCreating) return;
              setCosCreating(true);
              try {
                const { data } = await api.post(`/orgs/${orgId}/my-cos/new`);
                // Refresh the thread list so the picker reflects the
                // new conversation, then jump straight into it.
                setMyCosThreads((prev) => [{
                  conversation_id: data.conversation_id,
                  team_id: data.team_id,
                  title: data.title,
                  created_at: new Date().toISOString(),
                  last_message_at: new Date().toISOString(),
                }, ...prev]);
                setMyCos(data);
                nav(data.deeplink);
              } catch (err) {
                toast.error(err?.response?.data?.detail || "Could not start a new thread");
              } finally { setCosCreating(false); }
            }}
          />
        )}

        {/* Mind map — live picture of the org's brain (teams, agents,
            missions, posts, time-spent). Owner-only by default; owners
            can grant access to specific members from the Mind Map page. */}
        {org.can_view_mind_map && (
          <Link
            to={`/app/org/${orgId}/map`}
            className="sp-glass rounded-2xl p-5 mb-6 flex items-center gap-4 hover:border-white/25 transition group"
            data-testid="open-mind-map"
          >
            <div className="w-10 h-10 rounded-xl flex items-center justify-center"
                 style={{ background: "linear-gradient(160deg, rgba(168,85,247,0.45), rgba(168,85,247,0.08))" }}>
              <Network className="w-4 h-4 text-white" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-white font-medium">Mind map</div>
              <div className="text-xs sp-subtle">A live picture of how teams, agents, missions, and posts connect. Click any node to drill in. Filter by 24h / 7d / 30d / 90d to see where time and money are flowing.</div>
            </div>
            <ArrowRight className="w-4 h-4 text-white/60 group-hover:text-white transition" />
          </Link>
        )}

        {/* Pick up where you left off — recent conversations strip */}
        <RecentConversations orgId={orgId} />

        {/* Documents library — every artifact agents have generated
            (calendar invites, briefs, CSVs, scripts, emails). Searchable,
            previewable, downloadable, deletable. */}
        <DocumentsPanel orgId={orgId} />

        {/* Teams — specialized workspaces. Owner's "New team" form moves
            into the collapsible admin section below to keep this tidy. */}
        <div className="mb-6">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm uppercase tracking-widest sp-subtle flex items-center gap-2">
              <Users className="w-3.5 h-3.5" /> Teams
            </h3>
            <span className="text-[11px] sp-subtle">
              {teams.filter((t) => t.id !== org.general_team_id).length} specialised
            </span>
          </div>
          <div className="grid md:grid-cols-3 gap-4">
            {teams.filter((t) => t.id !== org.general_team_id).map((t) => {
              const isClosed = t.visibility === "closed";
              const canJoin = t.can_join;
              // UX audit #8: full-card link target. The whole card
              // navigates to the team workspace; a chevron on the right
              // signals "tap to open" universally. Inner buttons (Join,
              // Delete) stop propagation so they don't trigger the
              // outer navigation.
              const cardClasses = "sp-glass rounded-2xl p-5 hover:border-white/30 hover:bg-white/[0.04] transition group relative flex flex-col cursor-pointer";
              const Inner = (
                <>
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
                  <div className="flex items-start justify-between gap-2 mb-3">
                    <div className="w-9 h-9 rounded-lg sp-glass-strong flex items-center justify-center shrink-0">
                      <Users className="w-4 h-4 text-white/90" />
                    </div>
                    <span
                      className="text-[9px] uppercase tracking-widest px-1.5 py-0.5 rounded"
                      style={{
                        background: isClosed ? "rgba(239,68,68,0.15)" : "rgba(16,185,129,0.15)",
                        color: isClosed ? "rgb(252,165,165)" : "rgb(110,231,183)",
                        border: `1px solid ${isClosed ? "rgba(239,68,68,0.35)" : "rgba(16,185,129,0.35)"}`,
                      }}
                      title={isClosed
                        ? "Invite-only — only members can see and chat in this team"
                        : "Open — anyone in the org can join"}
                    >
                      {isClosed ? "Closed" : "Open"}
                    </span>
                  </div>
                  <div className="text-white font-medium text-base truncate">{t.name}</div>
                  {t.description && (
                    <div className="text-xs sp-subtle mt-1 line-clamp-2">{t.description}</div>
                  )}
                  <div className="flex items-center gap-3 mt-3 text-[11px] sp-subtle">
                    {typeof t.member_count === "number" && (
                      <span className="inline-flex items-center gap-1"><Users className="w-3 h-3" />{t.member_count}</span>
                    )}
                    {typeof t.agent_count === "number" && t.agent_count > 0 && (
                      <span className="inline-flex items-center gap-1">{t.agent_count} agents</span>
                    )}
                  </div>
                  <div className="mt-auto pt-3 flex items-center justify-between gap-2">
                    {canJoin ? (
                      <button
                        type="button"
                        onClick={(e) => { e.preventDefault(); e.stopPropagation(); joinTeam(t); }}
                        className="sp-btn sp-btn-primary !py-1 !px-3 text-xs"
                        data-testid={`join-team-${t.id}`}
                      >
                        Join team <ArrowRight className="w-3.5 h-3.5" />
                      </button>
                    ) : (
                      <span className="inline-flex items-center gap-1 text-[11px] sp-subtle group-hover:text-white">
                        Open workspace
                      </span>
                    )}
                    {!canJoin && (
                      <ChevronRight className="w-4 h-4 sp-subtle group-hover:text-white shrink-0" />
                    )}
                  </div>
                </>
              );
              return canJoin ? (
                <div key={t.id} className={cardClasses + " !cursor-default"} data-testid={`team-card-${t.id}`}>
                  {Inner}
                </div>
              ) : (
                <Link
                  key={t.id}
                  to={`/app/team/${t.id}`}
                  className={cardClasses}
                  data-testid={`team-card-${t.id}`}
                >
                  {Inner}
                </Link>
              );
            })}
            {teams.filter((t) => t.id !== org.general_team_id).length === 0 && (
              <div className="sp-glass rounded-2xl p-6 col-span-full text-center text-sm sp-subtle">
                No specialised teams yet — create one in <span className="text-white/80">Org setup</span> below.
              </div>
            )}
          </div>
        </div>

        {canInvite && !showInviteTools && (
          <div className="sp-glass rounded-2xl p-5 mb-6 flex items-start gap-4" data-testid="invite-teammates-card">
            <div className="w-10 h-10 rounded-xl sp-glass-strong flex items-center justify-center shrink-0">
              <UserPlus className="w-5 h-5 text-white/90" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="font-medium text-sm">Invite your teammates to {org.name}</div>
              <div className="text-xs sp-subtle mt-0.5">
                Generate a one-time invite code or a shareable link. Members can post to your teams and chat with agents; admins can also manage org settings.
              </div>
            </div>
            <button
              onClick={() => { setShowInviteTools(true); if (invites.length === 0) { createInvite("member"); } }}
              className="sp-btn sp-btn-primary text-xs shrink-0"
              data-testid="invite-teammates-btn"
            >
              <UserPlus className="w-3.5 h-3.5" /> Create invite
            </button>
          </div>
        )}

        {showInviteTools && canInvite && (
          <div ref={inviteToolsRef} className="sp-glass-strong rounded-2xl p-6 mb-10" data-testid="invite-tools">
            {seatStatus && seatStatus.warning && (
              <SeatWarningBanner status={seatStatus} />
            )}
            {seatStatus && seatStatus.boost_source === "access_code" && !seatStatus.warning && (
              <div
                className="text-xs rounded-xl px-3 py-2 mb-4 border border-emerald-500/40 bg-emerald-500/15"
                style={{ color: "var(--fg)" }}
                data-testid="seat-boost-banner"
              >
                <strong className="text-emerald-700 dark:text-emerald-200">Access-code boost active</strong> — you've got
                Temper-equivalent seats ({seatStatus.seats_used} of{" "}
                {seatStatus.seats_included}) included while your code is
                active. No extra charge.
              </div>
            )}
            <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
              <h3 className="text-sm uppercase tracking-widest sp-subtle">Invites</h3>
              <div className="flex items-center gap-2 flex-wrap">
                <input
                  type="email" value={inviteEmail}
                  onChange={(e) => setInviteEmail(e.target.value)}
                  placeholder="email (optional — we'll send the link)"
                  className="sp-input text-xs"
                  data-testid="invite-email-input"
                />
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
                    {inv.email && <span className="text-xs sp-subtle truncate">→ {inv.email}</span>}
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

        {/* ────────────────────────────────────────────────────────────
            ORG SETUP & ADMIN — collapsible. Auto-expands for a brand-new
            org (no Soul + no logo) so the owner can set things up. Once
            configured, stays collapsed by default; the toggle state is
            remembered per-org in localStorage so returning users land on
            their preferred view.
            ──────────────────────────────────────────────────────────── */}
        {(() => {
          const hasSoul = !!(org.soul && (org.soul.mission || org.soul.summary));
          const hasLogo = !!(org.brand_assets && org.brand_assets.primary_logo);
          const isSetUp = hasSoul && hasLogo;
          const unconfiguredCount = [!hasSoul, !hasLogo].filter(Boolean).length;
          const subtitle = isSetUp
            ? "Brand assets, Soul, style guide, schedules, files, activity — plus team management."
            : `Finish setup (${unconfiguredCount} item${unconfiguredCount === 1 ? "" : "s"} left) — brand assets, Soul, team management.`;
          return (
            <CollapsibleSection
              title={isSetUp ? "Org setup & insights" : "Finish setting up this org"}
              subtitle={subtitle}
              storageKey={`org-admin-${orgId}`}
              defaultOpen={!isSetUp}
              badge={!isSetUp ? `${unconfiguredCount} to go` : null}
              testId="org-admin-section"
            >
              {/* Brand Assets — owner-only edits. Members get a read-only
                  view + a clear notice telling them to ask the owner. */}
              <BrandAssetsPanel orgId={orgId} initial={org.brand_assets} isOwner={isOwner} onChange={(ba) => setOrg((prev) => prev ? { ...prev, brand_assets: ba } : prev)} />

              {/* Company Soul — read-only display when populated */}
              {(org.soul && (org.soul.mission || org.soul.summary)) && (
                <div className="sp-glass rounded-2xl p-6 mb-8" data-testid="org-soul">
                  <div className="flex items-center gap-2 mb-3">
                    <Logo className="w-4 h-4 text-white/80" />
                    <h3 className="text-sm uppercase tracking-widest sp-subtle">Company Soul</h3>
                    {/* UX audit #11: Owners + admins get an "Edit Soul"
                        button right in the section header so updating the
                        company's mission/values is one click, not buried
                        in settings. */}
                    {(isOwner || canEdit) && (
                      <button
                        type="button"
                        onClick={openEdit}
                        className="sp-btn sp-btn-ghost text-[11px] !text-orange-300 hover:!bg-orange-400/10"
                        data-testid="edit-soul-btn"
                        title="Open the org settings drawer to update Soul fields"
                      >
                        <Pencil className="w-3 h-3" /> Edit Soul
                      </button>
                    )}
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

              {/* Soul change requests — anyone can suggest, only owner can
                  directly mutate. Owner/admins can approve/reject here. */}
              <SoulSuggestionsPanel
                orgId={orgId}
                role={org.role}
                currentSoul={org.soul || {}}
                onSoulApplied={load}
              />

              {/* Social Style Guide */}
              <SocialStyleGuidePanel orgId={orgId} />

              {/* Spend dashboard — Phase A billing visibility (owners/admins only). */}
              {canEdit && (
                <div className="mb-6">
                  <SpendDashboard orgId={orgId} />
                </div>
              )}

              {/* Per-org Notes — text + voice capture with optional
                  agent-context injection. */}
              <div className="sp-glass-strong rounded-2xl p-6 mb-6" id="notes" data-testid="notes-section">
                <NotesPage orgId={orgId} />
              </div>

              {/* Connections — each member connects their own SaaS accounts
                  (Gmail, Slack, Notion, HubSpot, Calendar) via Composio's
                  managed OAuth. Per-user scope, visible to every member. */}
              <div className="sp-glass-strong rounded-2xl p-6 mb-6" id="connections" data-testid="connections-section">
                <div className="flex items-center gap-2 mb-4">
                  <h3 className="text-sm uppercase tracking-widest sp-subtle">Your connected accounts</h3>
                </div>
                <ConnectionsPanel orgId={orgId} />
              </div>

              {/* Per-agent Composio tool toggles — admin-gated. Lives next
                  to Connections so the "connect → enable agents" flow
                  reads as one coherent journey. */}
              <div className="sp-glass-strong rounded-2xl p-6 mb-6" id="agent-tools" data-testid="agent-tools-section">
                <div className="flex items-center gap-2 mb-4">
                  <h3 className="text-sm uppercase tracking-widest sp-subtle">Agent tool access</h3>
                </div>
                <AgentToolPrefsPanel orgId={orgId} />
              </div>

              {/* Custom agents (per-org, owner/admin manageable) */}
              <div className="sp-glass-strong rounded-2xl p-6 mb-6" data-testid="custom-agents-section">
                <CustomAgentsPanel orgId={orgId} canEdit={canEdit} />
              </div>

              {/* Schedules */}
              <SchedulesPanel scope="org" targetId={orgId} orgId={orgId} />
              <div className="h-6" />

              {/* Asset library (PDFs / docs / raw files) */}
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
                    placeholder="Optional caption (e.g. 'Studio shot of our ceramic mug in oat') — helps agents pick this for the right post"
                    className="w-full max-w-md rounded-lg px-3 py-2 text-sm focus:sp-focus"
                    style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
                    data-testid="asset-desc-input"
                  />
                  <div className="flex flex-wrap justify-center gap-1 max-w-md">
                    {[
                      { v: "", label: "Uncategorised" },
                      { v: "product", label: "Product" },
                      { v: "lifestyle", label: "Lifestyle" },
                      { v: "team", label: "Team" },
                      { v: "venue", label: "Venue" },
                      { v: "logo_variant", label: "Logo variant" },
                      { v: "other", label: "Other" },
                    ].map((c) => (
                      <button
                        key={c.v || "none"}
                        type="button"
                        onClick={() => setUploadCategory(c.v)}
                        className={`sp-chip text-[10px] ${uploadCategory === c.v ? "!bg-orange-500/20 !border-orange-400/40 !text-white" : ""}`}
                        data-testid={`asset-cat-${c.v || "none"}`}
                      >
                        {c.label}
                      </button>
                    ))}
                  </div>
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

                {/* External link adder — Google Drive / Dropbox / Notion /
                    Figma / etc. Any org member can add. Auto-detects the
                    provider on the backend so users don't need to pick one. */}
                <form
                  onSubmit={addAssetLink}
                  className="sp-glass rounded-xl p-4 mt-4 flex flex-col sm:flex-row gap-2"
                  data-testid="asset-link-form"
                >
                  <div className="flex items-center gap-2 shrink-0">
                    <LinkIcon className="w-4 h-4 sp-subtle" />
                    <span className="text-xs uppercase tracking-widest sp-subtle hidden sm:inline">Add a link</span>
                  </div>
                  <input
                    type="text"
                    inputMode="url"
                    value={linkUrl}
                    onChange={(e) => setLinkUrl(e.target.value)}
                    placeholder="Paste a Google Drive, Dropbox, Notion, Figma, etc. URL"
                    className="flex-1 rounded-lg px-3 py-2 text-sm focus:sp-focus"
                    style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
                    data-testid="asset-link-url"
                  />
                  <input
                    type="text"
                    value={linkName}
                    onChange={(e) => setLinkName(e.target.value)}
                    placeholder="Label (optional)"
                    className="sm:w-48 rounded-lg px-3 py-2 text-sm focus:sp-focus"
                    style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
                    data-testid="asset-link-name"
                  />
                  <button
                    type="submit"
                    disabled={!linkUrl.trim() || linkBusy}
                    className="sp-btn sp-btn-primary text-xs disabled:opacity-50"
                    data-testid="asset-link-submit"
                  >
                    {linkBusy ? "Saving…" : <>Add link</>}
                  </button>
                </form>

                {/* Quick keyword search across this org's library. Empty
                    state shows the standard recent-first list. When the
                    user types we hit /assets/search so matches against
                    extracted body text (`text_excerpt`) light up too,
                    not just filenames. */}
                <div className="flex items-center gap-2 mt-4 mb-2">
                  <Search className="w-4 h-4 sp-subtle" />
                  <input
                    type="text"
                    value={assetQuery}
                    onChange={(e) => setAssetQuery(e.target.value)}
                    placeholder="Search this library — names, descriptions, extracted text…"
                    className="flex-1 rounded-lg px-3 py-2 text-sm focus:sp-focus"
                    style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
                    data-testid="asset-search-input"
                  />
                  {assetQuery && (
                    <button
                      type="button"
                      onClick={() => setAssetQuery("")}
                      className="text-xs sp-subtle hover:text-white"
                      data-testid="asset-search-clear"
                      title="Clear search"
                    >
                      <XIcon className="w-3.5 h-3.5" />
                    </button>
                  )}
                  {assetSearchBusy && <Loader2 className="w-3.5 h-3.5 animate-spin sp-subtle" />}
                </div>

                {assets.length > 0 && (() => {
                  // When a search query is active, render the matched
                  // subset (including snippets). Otherwise render the
                  // standard recent list with the existing "show more"
                  // pagination.
                  const isSearching = assetQuery.trim().length > 0;
                  const baseList = isSearching ? (assetSearchHits || []) : assets;
                  const visible = isSearching
                    ? baseList
                    : (assetsExpanded ? assets : assets.slice(0, ASSETS_INITIAL_LIMIT));
                  const hidden = isSearching ? 0 : (assets.length - visible.length);
                  return (
                    <>
                      <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-3 mt-4">
                        {visible.map((a) => {
                          const Icon = KIND_ICONS[a.kind] || FileIcon;
                          const isImg = a.kind === "image";
                          const isLink = a.kind === "link";
                          // For uploaded files the served URL is relative
                          // to the backend; link assets store the full
                          // external URL directly in `a.url`.
                          const url = isLink ? a.url : `${BACKEND_URL}${a.url}`;
                          return (
                            <div key={a.id} className="sp-glass rounded-xl p-3 flex flex-col gap-2" data-testid={`asset-card-${a.id}`}>
                              <div className="flex items-start gap-2">
                                <div className="w-9 h-9 rounded-lg sp-glass-strong flex items-center justify-center shrink-0">
                                  <Icon className="w-4 h-4" />
                                </div>
                                <div className="min-w-0 flex-1">
                                  <div className="text-sm font-medium truncate" title={a.original_name}>{a.original_name}</div>
                                  <div className="text-[11px] sp-subtle flex items-center gap-1.5 flex-wrap">
                                    {isLink ? (
                                      <>
                                        <span className="sp-chip !py-0 !px-1.5 text-[10px]" data-testid={`asset-link-provider-${a.id}`}>{a.link_provider || "Link"}</span>
                                        <span>· by {a.uploaded_by_name}</span>
                                      </>
                                    ) : (
                                      <>{a.kind} · {formatBytes(a.size)} · by {a.uploaded_by_name}</>
                                    )}
                                  </div>
                                </div>
                              </div>
                              {a.description && !a.snippet && <div className="text-xs sp-subtle line-clamp-2">{a.description}</div>}
                              {a.snippet && (
                                <div
                                  className="text-xs sp-subtle leading-relaxed line-clamp-3"
                                  data-testid={`asset-snippet-${a.id}`}
                                >
                                  {a.snippet}
                                </div>
                              )}
                              {isImg && (
                                <a href={url} target="_blank" rel="noreferrer" className="block rounded-lg overflow-hidden border" style={{ borderColor: "var(--border-soft)" }}>
                                  <img src={url} alt={a.original_name} loading="lazy" className="w-full max-h-32 object-cover" />
                                </a>
                              )}
                              {isLink && (
                                <div className="text-[11px] sp-subtle truncate" title={a.link_url}>
                                  {a.link_url}
                                </div>
                              )}
                              <div className="flex items-center justify-end gap-1 mt-auto">
                                {isLink ? (
                                  <a
                                    href={url}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="sp-btn sp-btn-ghost text-xs"
                                    data-testid={`asset-link-open-${a.id}`}
                                  >
                                    <ExternalLink className="w-3 h-3" /> Open
                                  </a>
                                ) : (
                                  <a href={url} target="_blank" rel="noreferrer" className="sp-btn sp-btn-ghost text-xs" download>
                                    <Download className="w-3 h-3" /> Open
                                  </a>
                                )}
                                <button onClick={() => removeAsset(a.id)} className="sp-btn sp-btn-ghost text-xs" data-testid={`asset-delete-${a.id}`}>
                                  <Trash2 className="w-3 h-3" />
                                </button>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                      {isSearching && visible.length === 0 && (
                        <div
                          className="text-center text-xs sp-subtle py-6"
                          data-testid="asset-search-no-results"
                        >
                          No matches for "{assetQuery}". Try a different keyword
                          or upload the doc to make it searchable.
                        </div>
                      )}
                      {/* Show more / Show less toggle — only renders when
                          there's something to hide. Keeps the org page calm
                          even after teams stack up dozens of brand assets.
                          Suppressed during active search (results are already
                          capped server-side). */}
                      {!isSearching && assets.length > ASSETS_INITIAL_LIMIT && (
                        <div className="flex justify-center mt-4">
                          <button
                            type="button"
                            onClick={() => setAssetsExpanded((v) => !v)}
                            className="sp-btn sp-btn-ghost text-xs"
                            data-testid="assets-toggle-more"
                          >
                            {assetsExpanded
                              ? `Show fewer (collapse to ${ASSETS_INITIAL_LIMIT})`
                              : `Show all ${assets.length} (${hidden} more)`}
                          </button>
                        </div>
                      )}
                    </>
                  );
                })()}
              </section>

              {/* Activity overview — metrics panel. Set aside for owners
                  and managers; not the first thing casual users need. */}
              <ActivityOverview orgId={orgId} />

              {/* New team form + Members list */}
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
                  {/* Open vs Closed toggle — controls who can see + join the team */}
                  <div className="grid grid-cols-2 gap-2 mt-3">
                    {[
                      { v: "open",   label: "Open",   sub: "Anyone in the org can join" },
                      { v: "closed", label: "Closed", sub: "Invite-only" },
                    ].map(({ v, label, sub }) => (
                      <button
                        type="button"
                        key={v}
                        onClick={() => setNewTeamVisibility(v)}
                        className="text-left rounded-xl px-3 py-2.5 transition"
                        style={{
                          background: newTeamVisibility === v ? "color-mix(in srgb, var(--accent) 14%, transparent)" : "var(--input-bg)",
                          border: `1px solid ${newTeamVisibility === v ? "var(--accent)" : "var(--input-border)"}`,
                          color: "var(--fg)",
                        }}
                        data-testid={`new-team-vis-${v}`}
                      >
                        <div className="text-xs font-medium">{label}</div>
                        <div className="text-[10px] sp-subtle">{sub}</div>
                      </button>
                    ))}
                  </div>
                  <button
                    type="submit"
                    disabled={creating || !newTeam.trim()}
                    className="sp-btn sp-btn-primary w-full justify-center mt-3 disabled:opacity-50"
                    data-testid="create-team-btn"
                  >
                    {creating ? "Creating…" : `Create ${newTeamVisibility === "closed" ? "closed" : "open"} team`}
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
            </CollapsibleSection>
          );
        })()}
      </main>
    </div>
  );
}


// ── Soft-enforcement seat-cap banner ──────────────────────────────────────
// Rendered inside the invite-tools card when the org is over (or about
// to go over) its plan's seat allowance. Soft-warn only — invites still
// go through. See backend/seat_policy.py for the policy.
function SeatWarningBanner({ status }) {
  const isSpark = (status.effective_plan || status.plan) === "spark";
  return (
    <div
      className="rounded-xl px-3 py-3 mb-4 border border-amber-500/50 bg-amber-500/15 text-xs leading-relaxed"
      style={{ color: "var(--fg)" }}
      data-testid="seat-warning-banner"
    >
      <div className="flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <div className="font-semibold mb-1 text-amber-700 dark:text-amber-200">
            Seat allowance exceeded
          </div>
          <div className="opacity-90">{status.warning}</div>
        </div>
        <div className="flex flex-col gap-1.5 shrink-0">
          {isSpark ? (
            <>
              <Link
                to="/pricing#seat-addon"
                className="sp-btn sp-btn-secondary text-[11px] whitespace-nowrap"
                data-testid="seat-addon-btn"
              >
                + Add a seat ($30/mo)
              </Link>
              <Link
                to="/pricing"
                className="sp-btn sp-btn-primary text-[11px] whitespace-nowrap"
                data-testid="seat-upgrade-btn"
              >
                Upgrade to Temper
              </Link>
            </>
          ) : (
            <a
              href="mailto:hello@foundryagents.ai?subject=Refine%20tier%20enquiry"
              className="sp-btn sp-btn-primary text-[11px] whitespace-nowrap"
            >
              Talk to us
            </a>
          )}
          <Link
            to="/pricing"
            className="sp-btn sp-btn-ghost text-[11px] whitespace-nowrap text-center"
          >
            See pricing
          </Link>
        </div>
      </div>
    </div>
  );
}


// ── Private Chief of Staff card with multi-thread picker ──────────────────
// Replaces the old single-link CoS card so users can keep multiple
// long-running threads cleanly separated instead of piling every
// conversation onto one endless message log. The most recent thread
// is the primary click target (matches old behaviour); expanding the
// picker reveals every thread with relative timestamps + a "+ New chat"
// button to start fresh.
function MyChiefOfStaffCard({ orgId: _orgId, primary, threads, expanded, onToggle, creating, onNewThread }) {
  const list = (threads && threads.length) ? threads : [
    // Fall back to the single primary thread when the threads endpoint
    // hasn't loaded yet (or returned an empty list — shouldn't happen
    // since /my-cos auto-creates one, but defensive).
    {
      conversation_id: primary.conversation_id,
      team_id: primary.team_id,
      title: primary.title,
      last_message_at: null,
    },
  ];
  const top = list[0];
  const others = list.slice(1);
  const count = list.length;

  const fmtRel = (iso) => {
    const d = parseServerDate(iso);
    if (!d) return "";
    const diff = Math.max(0, Date.now() - d.getTime());
    const m = Math.floor(diff / 60000);
    if (m < 1) return "just now";
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ago`;
    const days = Math.floor(h / 24);
    if (days < 7) return `${days}d ago`;
    return d.toLocaleDateString();
  };

  return (
    <div className="sp-glass rounded-2xl mb-3" data-testid="my-chief-of-staff-card">
      <div className="flex items-center gap-4 p-5">
        <Link
          to={`/app/team/${top.team_id}/c/${top.conversation_id}`}
          className="flex-1 min-w-0 flex items-center gap-4 group"
          data-testid="my-cos-open-latest"
        >
          <div className="w-10 h-10 rounded-xl flex items-center justify-center shrink-0"
               style={{ background: "linear-gradient(160deg, rgba(251,146,60,0.55), rgba(251,146,60,0.08))" }}>
            <MessageCircle className="w-4 h-4 text-white" />
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-white font-medium">Your Chief of Staff</span>
              <span className="text-[9px] uppercase tracking-widest px-1.5 py-0.5 rounded sp-glass-strong">Private</span>
              {count > 1 && (
                <span className="text-[10px] sp-subtle">{count} threads</span>
              )}
            </div>
            <div className="text-xs sp-subtle truncate">
              {top.title || "My Chief of Staff"}
              {top.last_message_at && <span className="ml-2 opacity-70">· {fmtRel(top.last_message_at)}</span>}
            </div>
          </div>
          <ArrowRight className="w-4 h-4 text-white/60 group-hover:text-white transition shrink-0" />
        </Link>
        <div className="flex items-center gap-1 shrink-0">
          <button
            type="button"
            onClick={onNewThread}
            disabled={creating}
            className="sp-btn sp-btn-ghost text-[11px] px-2 py-1 disabled:opacity-50"
            title="Start a fresh Chief of Staff thread (your existing threads are preserved)"
            data-testid="my-cos-new-thread-btn"
          >
            {creating ? <Loader2 className="w-3 h-3 animate-spin" /> : <Plus className="w-3 h-3" />}
            New chat
          </button>
          {others.length > 0 && (
            <button
              type="button"
              onClick={onToggle}
              className="sp-btn sp-btn-ghost text-[11px] px-1.5 py-1"
              title={expanded ? "Hide other threads" : `Show ${others.length} other thread${others.length > 1 ? "s" : ""}`}
              data-testid="my-cos-toggle-threads"
            >
              <ChevronRight className={`w-3 h-3 transition-transform ${expanded ? "rotate-90" : ""}`} />
            </button>
          )}
        </div>
      </div>
      {expanded && others.length > 0 && (
        <div className="border-t border-white/10 px-5 py-3">
          <div className="text-[10px] uppercase tracking-widest sp-subtle mb-2">Other threads</div>
          <div className="flex flex-col gap-1.5">
            {others.map((t) => (
              <Link
                key={t.conversation_id}
                to={`/app/team/${t.team_id}/c/${t.conversation_id}`}
                className="sp-glass rounded-xl px-3 py-2 flex items-center gap-3 hover:border-white/20 transition group"
                data-testid={`my-cos-thread-${t.conversation_id}`}
              >
                <MessageCircle className="w-3.5 h-3.5 sp-subtle shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-white truncate">{t.title || "My Chief of Staff"}</div>
                </div>
                {t.last_message_at && (
                  <div className="text-[10px] sp-subtle tabular-nums shrink-0">{fmtRel(t.last_message_at)}</div>
                )}
                <ArrowRight className="w-3 h-3 sp-subtle group-hover:text-white transition shrink-0" />
              </Link>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}


// ── General chat card with inline upload ───────────────────────────────────
// A self-contained mini-composer that posts files (and an optional prompt)
// straight into the org's General chat without leaving the org page.
// Supports paperclip-pick, drag-and-drop, and clipboard paste — same UX as
// the full ConversationPage composer.
function GeneralChatCard({ orgId, teamId, convId }) {
  const nav = useNavigate();
  const fileInputRef = useRef(null);
  const taRef = useRef(null);
  const [staged, setStaged] = useState([]);
  const [text, setText] = useState("");
  const [uploading, setUploading] = useState(false);
  const [isDragging, setIsDragging] = useState(false);

  // Auto-grow the inline composer textarea as the user types. Caps at ~30dvh
  // so the org page stays usable; past the cap the textarea scrolls
  // internally so users can swipe/scroll up and down to edit a long message.
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    const max = Math.max(120, Math.round(window.innerHeight * 0.30));
    const next = Math.min(ta.scrollHeight, max);
    ta.style.height = `${next}px`;
    ta.style.overflowY = ta.scrollHeight > max ? "auto" : "hidden";
  }, [text]);

  const stageFiles = (incoming) => {
    const picked = Array.from(incoming || []).filter(Boolean);
    if (!picked.length) return false;
    if (staged.length + picked.length > 6) {
      toast.error("Up to 6 files per upload"); return false;
    }
    // Images can stage up to 100 MB so the user has room to click Shrink.
    // Non-images keep the 25 MB cap (no client-side compression available).
    const tooBig = picked.find((f) => {
      const cap = f.type?.startsWith("image/") ? 100 * 1024 * 1024 : 25 * 1024 * 1024;
      return f.size > cap;
    });
    if (tooBig) {
      toast.error(tooBig.type?.startsWith("image/")
        ? `${tooBig.name} is over 100 MB — please shrink it externally first.`
        : `${tooBig.name} is too large (max 25 MB)`);
      return false;
    }
    setStaged((prev) => [
      ...prev,
      ...picked.map((f) => {
        const finalFile = f.name && f.name !== "image.png" ? f : new File(
          [f],
          `pasted-${new Date().toISOString().replace(/[:.]/g, "-")}.${(f.type.split("/")[1] || "png")}`,
          { type: f.type });
        return {
          id: `stg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          file: finalFile,
          kind: f.type.startsWith("image/") ? "image"
                : f.type.startsWith("video/") ? "video"
                : f.type.startsWith("audio/") ? "audio"
                : "file",
          previewUrl: f.type.startsWith("image/") ? URL.createObjectURL(finalFile) : "",
          originalSize: 0,
          savedBytes: 0,
        };
      }),
    ]);
    return true;
  };

  // On-demand shrink, triggered by the chip-level "Shrink" button.
  const shrinkStaged = async (id) => {
    const tgt = staged.find((s) => s.id === id);
    if (!tgt || tgt.kind !== "image" || tgt.savedBytes > 0) return;
    try {
      const r = await compressImageFile(tgt.file);
      if (r.untouched || r.savedBytes <= 0) {
        toast.message("Already as small as it gets.");
        return;
      }
      const newPreview = URL.createObjectURL(r.file);
      setStaged((prev) => prev.map((s) => {
        if (s.id !== id) return s;
        if (s.previewUrl) URL.revokeObjectURL(s.previewUrl);
        return { ...s, file: r.file, previewUrl: newPreview, originalSize: r.original, savedBytes: r.savedBytes };
      }));
      toast.success(`Shrunk — saved ${formatBytes(r.savedBytes)}`);
    } catch (_) {
      toast.error("Couldn't shrink this image.");
    }
  };

  // Free preview blob URLs when the card unmounts
  useEffect(() => () => staged.forEach((s) => s.previewUrl && URL.revokeObjectURL(s.previewUrl)),
    /* eslint-disable-next-line react-hooks/exhaustive-deps */ []);

  const onPick = (e) => {
    const picked = Array.from(e.target.files || []);
    e.target.value = "";
    stageFiles(picked);
  };
  const onPaste = (e) => {
    const files = Array.from(e.clipboardData?.files || []);
    if (!files.length) return;
    if (stageFiles(files)) {
      e.preventDefault();
      toast.success(`Pasted ${files.length} file${files.length > 1 ? "s" : ""}`);
    }
  };
  const onDragOver = (e) => {
    if (e.dataTransfer?.types?.includes("Files")) {
      e.preventDefault();
      if (!isDragging) setIsDragging(true);
    }
  };
  const onDrop = (e) => {
    if (!e.dataTransfer?.files?.length) return;
    e.preventDefault();
    setIsDragging(false);
    stageFiles(Array.from(e.dataTransfer.files));
  };
  const removeStaged = (id) => setStaged((prev) => {
    const tgt = prev.find((s) => s.id === id);
    if (tgt?.previewUrl) URL.revokeObjectURL(tgt.previewUrl);
    return prev.filter((s) => s.id !== id);
  });

  const submit = async (e) => {
    e?.preventDefault?.();
    if (uploading) return;
    const trimmed = (text || "").trim();
    if (staged.length === 0 && !trimmed) return;
    // Send-time hard cap (server enforces 25 MB per file). Clean error
    // here so a too-big un-shrunk image doesn't 413 server-side.
    const stillBig = staged.find((s) => s.file.size > 25 * 1024 * 1024);
    if (stillBig) {
      toast.error(stillBig.kind === "image"
        ? `${stillBig.file.name} is over 25 MB — click Shrink on the chip first, then send.`
        : `${stillBig.file.name} is too large (max 25 MB)`);
      return;
    }
    setUploading(true);
    try {
      if (staged.length > 0) {
        // Files (with optional prompt) — multipart upload path
        const fd = new FormData();
        fd.append("text", text || "");
        staged.forEach((s) => fd.append("files", s.file));
        await api.post(`/conversations/${convId}/upload`, fd, {
          headers: { "Content-Type": "multipart/form-data" },
        });
        staged.forEach((s) => s.previewUrl && URL.revokeObjectURL(s.previewUrl));
        setStaged([]);
        setText("");
        // Take the user straight into the live conversation. The Chief
        // of Staff reply will stream into the chat on the next page
        // load (initial fetch picks up both the user message and the
        // agent's reply).
        nav(`/app/team/${teamId}/c/${convId}`);
      } else {
        // Text-only — post a normal chat message and let agents respond.
        await api.post(`/conversations/${convId}/messages`, { text: trimmed });
        setText("");
        nav(`/app/team/${teamId}/c/${convId}`);
      }
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Send failed");
    } finally { setUploading(false); }
  };

  return (
    <div
      className="relative sp-glass-strong rounded-2xl p-6 mb-6"
      onDragOver={onDragOver}
      onDragLeave={() => setIsDragging(false)}
      onDrop={onDrop}
      data-testid="open-general-chat"
    >
      {isDragging && (
        <div className="absolute inset-0 z-10 rounded-2xl border-2 border-dashed flex items-center justify-center pointer-events-none"
             style={{ borderColor: "rgba(246,64,0,0.7)", background: "color-mix(in srgb, var(--bg) 80%, transparent)", backdropFilter: "blur(8px)", WebkitBackdropFilter: "blur(8px)" }}
             data-testid="general-drop-overlay">
          <div className="flex items-center gap-2 text-sm font-medium" style={{ color: "var(--fg)" }}>
            <Paperclip className="w-4 h-4" /> Drop to attach to Chief of Staff
          </div>
        </div>
      )}

      <Link
        to={`/app/team/${teamId}/c/${convId}`}
        className="flex items-center gap-4 group"
        data-testid="open-general-chat-link"
      >
        <div className="w-12 h-12 rounded-xl flex items-center justify-center shrink-0"
             style={{ background: "linear-gradient(160deg, rgba(246,64,0,0.7), rgba(246,64,0,0.15))" }}>
          <MessageCircle className="w-5 h-5 text-white" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="font-medium text-lg" style={{ color: "var(--fg)" }}>Chief of Staff chat</div>
          <div className="text-xs sp-subtle">Always-on. Just type — the right specialist will respond, and others jump in when the topic shifts.</div>
        </div>
        <ArrowRight className="w-5 h-5 sp-subtle group-hover:opacity-100 opacity-70 transition" />
      </Link>

      {/* Inline upload row — paperclip + textarea + send */}
      <form onSubmit={submit} className="mt-4 pt-4 border-t border-white/10">
        {staged.length > 0 && (
          <div className="flex flex-wrap gap-2 mb-3" data-testid="general-staged-tray">
            {staged.map((s) => (
              <div key={s.id} className="sp-glass rounded-lg px-3 py-1.5 flex items-center gap-2 text-xs" data-testid={`general-staged-${s.id}`}>
                {s.previewUrl ? (
                  <img src={s.previewUrl} alt="" className="w-6 h-6 rounded object-cover" />
                ) : (
                  <FileIcon className="w-3.5 h-3.5 sp-subtle" />
                )}
                <span className="max-w-[180px] truncate" title={s.file.name}>{s.file.name}</span>
                <span className="sp-subtle">{formatBytes(s.file.size)}</span>
                {s.savedBytes > 0 && (
                  <span className="text-emerald-300/80 text-[10px]" title={`Shrunk from ${formatBytes(s.originalSize)}`}>
                    saved {formatBytes(s.savedBytes)}
                  </span>
                )}
                {s.kind === "image" && s.savedBytes === 0 && s.file.size > 500 * 1024 && (
                  <button
                    type="button"
                    onClick={() => shrinkStaged(s.id)}
                    className="sp-btn sp-btn-ghost text-[10px] px-1.5 py-0.5 shrink-0"
                    title="Re-encode this image at ~80% quality (max 2048px) before sending"
                    data-testid={`general-shrink-staged-${s.id}`}
                  >
                    <Minimize2 className="w-3 h-3" />
                    Shrink
                  </button>
                )}
                <button type="button" onClick={() => removeStaged(s.id)} className="sp-subtle hover:text-white"
                        data-testid={`general-remove-staged-${s.id}`}>
                  <XIcon className="w-3 h-3" />
                </button>
              </div>
            ))}
          </div>
        )}
        <div className="flex items-center gap-2">
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept="image/*,video/*,audio/*,.pdf,.doc,.docx,.rtf,.txt,.md,.markdown,.rst,.csv,.tsv,.json,.yaml,.yml,.toml,.xml,.rss,.atom,.svg,.html,.htm,.log,.xlsx,.xls,.pptx,.ppt,.zip,.tar,.gz,.tgz,.7z,.py,.js,.ts,.css,.sql,.sh,.env"
            className="hidden"
            onChange={onPick}
            data-testid="general-file-input"
          />
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            className="sp-btn sp-btn-ghost text-xs shrink-0"
            title="Attach files (or paste / drop them anywhere on this card)"
            data-testid="general-paperclip-btn"
          >
            <Paperclip className="w-3.5 h-3.5" />
          </button>
          <textarea
            ref={taRef}
            rows={1}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onPaste={onPaste}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(e); }
            }}
            placeholder={staged.length > 0
              ? `Optional — what should the agents do with ${staged.length === 1 ? "this file" : "these files"}?`
              : "Message your Chief of Staff — type or drop / paste / pick files"}
            className="flex-1 rounded-xl px-4 py-2.5 text-sm focus:sp-focus resize-none leading-relaxed"
            style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)", minHeight: "42px" }}
            data-testid="general-prompt-input"
          />
          <button
            type="submit"
            disabled={uploading || (staged.length === 0 && !text.trim())}
            className="sp-btn sp-btn-primary text-xs shrink-0 disabled:opacity-50"
            data-testid="general-send-btn"
            title={staged.length === 0 && !text.trim() ? "Type a message or attach a file" : "Send to Chief of Staff"}
          >
            {uploading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Send className="w-3.5 h-3.5" />}
            {uploading ? "Sending…" : "Send"}
          </button>
        </div>
      </form>
    </div>
  );
}
