import React, { useEffect, useMemo, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { toast } from "sonner";
import {
  Users, Building, Mail, Copy, Trash2, UserPlus, Loader2,
  ArrowRight, Check, Shield, X as XIcon, ChevronDown, ChevronRight, Pencil,
} from "lucide-react";
import api from "../lib/api";
import TopBar from "../components/TopBar";

/**
 * WorkspaceSettingsPage — the owner's console for a Workspace.
 *
 * Surfaces:
 *   • List of Orgs in the workspace + quick-jump links
 *   • Members grid with every org they have access to
 *   • Per-row "Edit org access" drawer that lets an admin toggle each
 *     Org on/off and pick admin|member
 *   • Invite creation form with multi-org checkbox grid so one invite
 *     grants workspace + N orgs in one click
 *   • Pending invites list with revoke + copy-link buttons
 */
export default function WorkspaceSettingsPage() {
  const { wsId } = useParams();
  const [ws, setWs] = useState(null);
  const [orgs, setOrgs] = useState([]);
  const [members, setMembers] = useState([]);
  const [invites, setInvites] = useState([]);
  const [loading, setLoading] = useState(true);

  // Invite form
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteWsRole, setInviteWsRole] = useState("member");
  const [inviteGrants, setInviteGrants] = useState({}); // { orgId: "admin"|"member"|null }
  const [creatingInvite, setCreatingInvite] = useState(false);

  // Per-member access editor — map of user_id → { open, pending }
  const [editing, setEditing] = useState({});

  // Workspace rename — inline-edit on the header. Only owners/admins
  // see the pencil + save button.
  const [renaming, setRenaming] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const [savingName, setSavingName] = useState(false);
  const beginRename = () => {
    setNameDraft(ws?.name || "");
    setRenaming(true);
  };
  const cancelRename = () => setRenaming(false);
  const saveRename = async () => {
    const next = nameDraft.trim();
    if (!next || next === ws?.name) { setRenaming(false); return; }
    setSavingName(true);
    try {
      const { data } = await api.patch(`/workspaces/${wsId}`, { name: next });
      setWs(data);
      toast.success("Workspace renamed");
      setRenaming(false);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not rename");
    } finally {
      setSavingName(false);
    }
  };

  const refresh = async () => {
    try {
      const [wsR, orgsR, membersR, invitesR] = await Promise.all([
        api.get(`/workspaces/${wsId}`),
        api.get(`/workspaces/${wsId}/orgs`),
        api.get(`/workspaces/${wsId}/members`),
        api.get(`/workspaces/${wsId}/invites`).catch(() => ({ data: [] })),
      ]);
      setWs(wsR.data);
      setOrgs(orgsR.data);
      setMembers(membersR.data);
      setInvites(invitesR.data);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not load workspace");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { refresh(); /* eslint-disable-line */ }, [wsId]);

  const isAdmin = ws?.my_role === "owner" || ws?.my_role === "admin";

  const orgById = useMemo(() => {
    const m = {};
    orgs.forEach((o) => { m[o.id] = o; });
    return m;
  }, [orgs]);

  // ── Invite creation ────────────────────────────────────────────
  const toggleInviteGrant = (orgId) => {
    setInviteGrants((prev) => {
      const next = { ...prev };
      if (next[orgId]) delete next[orgId];
      else next[orgId] = "member";
      return next;
    });
  };
  const setInviteGrantRole = (orgId, role) => {
    setInviteGrants((prev) => ({ ...prev, [orgId]: role }));
  };

  const createInvite = async () => {
    setCreatingInvite(true);
    try {
      const org_grants = Object.entries(inviteGrants).map(([org_id, role]) => ({ org_id, role }));
      const { data } = await api.post(`/workspaces/${wsId}/invites`, {
        email: inviteEmail.trim() || null,
        ws_role: inviteWsRole,
        org_grants,
      });
      setInvites((prev) => [
        { code: data.code, ws_role: data.ws_role, org_grants: data.org_grants,
          expires_at: data.expires_at, email: inviteEmail.trim() }, ...prev,
      ]);
      setInviteEmail("");
      setInviteGrants({});
      toast.success(
        data.email_sent
          ? `Invite sent to ${inviteEmail.trim()}`
          : "Invite link created",
      );
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not create invite");
    } finally {
      setCreatingInvite(false);
    }
  };

  const copyInvite = (code) => {
    const url = `${window.location.origin}/invite/${code}`;
    navigator.clipboard.writeText(url);
    toast.success("Invite link copied");
  };

  const revokeInvite = async (code) => {
    try {
      await api.delete(`/workspaces/${wsId}/invites/${code}`);
      setInvites((prev) => prev.filter((i) => i.code !== code));
      toast.success("Invite revoked");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not revoke");
    }
  };

  // ── Per-member org-access editor ───────────────────────────────
  const beginEdit = (member) => {
    const pending = {};
    (member.org_access || []).forEach((g) => { pending[g.org_id] = g.role; });
    setEditing((prev) => ({ ...prev, [member.user_id]: { open: true, pending } }));
  };
  const cancelEdit = (uid) =>
    setEditing((prev) => ({ ...prev, [uid]: { open: false, pending: {} } }));
  const toggleEditGrant = (uid, orgId) => {
    setEditing((prev) => {
      const cur = prev[uid] || { open: true, pending: {} };
      const p = { ...cur.pending };
      if (p[orgId]) delete p[orgId]; else p[orgId] = "member";
      return { ...prev, [uid]: { ...cur, pending: p } };
    });
  };
  const setEditGrantRole = (uid, orgId, role) => {
    setEditing((prev) => {
      const cur = prev[uid] || { open: true, pending: {} };
      return { ...prev, [uid]: { ...cur, pending: { ...cur.pending, [orgId]: role } } };
    });
  };
  const saveEdit = async (uid) => {
    const cur = editing[uid];
    if (!cur) return;
    try {
      const org_grants = Object.entries(cur.pending).map(([org_id, role]) => ({ org_id, role }));
      await api.patch(`/workspaces/${wsId}/members/${uid}/org-access`, { org_grants });
      toast.success("Access updated");
      cancelEdit(uid);
      await refresh();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not save access");
    }
  };

  const removeMember = async (uid) => {
    if (!window.confirm("Remove this member? They'll lose access to every org in this workspace.")) return;
    try {
      await api.delete(`/workspaces/${wsId}/members/${uid}`);
      toast.success("Member removed");
      await refresh();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not remove");
    }
  };

  if (loading) {
    return (
      <div className="min-h-screen">
        <TopBar>Workspace</TopBar>
        <div className="max-w-5xl mx-auto px-4 sm:px-6 py-10 text-sm sp-subtle">
          <Loader2 className="w-4 h-4 animate-spin inline mr-2" /> Loading workspace…
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen">
      <TopBar>{ws?.name || "Workspace"}</TopBar>
      <main className="max-w-5xl mx-auto px-4 sm:px-6 py-6 sm:py-10" data-testid="workspace-settings">
        {/* Header */}
        <div className="mb-8">
          <div className="text-[10px] uppercase tracking-widest sp-subtle">Workspace</div>
          {renaming ? (
            <div className="flex items-center gap-2 mt-1.5 max-w-xl" data-testid="ws-rename-form">
              <input
                value={nameDraft}
                onChange={(e) => setNameDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") saveRename();
                  if (e.key === "Escape") cancelRename();
                }}
                autoFocus maxLength={120}
                className="sp-input flex-1 text-2xl sm:text-3xl font-medium tracking-tight"
                data-testid="ws-rename-input"
              />
              <button
                type="button" onClick={saveRename} disabled={savingName}
                className="sp-btn sp-btn-primary"
                data-testid="ws-rename-save"
              >
                {savingName ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
                Save
              </button>
              <button
                type="button" onClick={cancelRename}
                className="sp-btn sp-btn-ghost"
                data-testid="ws-rename-cancel"
              >
                <XIcon className="w-4 h-4" /> Cancel
              </button>
            </div>
          ) : (
            <h1 className="text-3xl sm:text-4xl font-medium tracking-tight flex items-center gap-2 flex-wrap">
              {ws?.name}
              {isAdmin && (
                <button
                  type="button" onClick={beginRename}
                  className="sp-btn sp-btn-ghost text-[11px] !text-orange-300 hover:!bg-orange-400/10"
                  data-testid="ws-rename-btn"
                  title="Rename workspace"
                >
                  <Pencil className="w-3 h-3" /> Rename
                </button>
              )}
            </h1>
          )}
          <div className="text-xs sp-subtle mt-1 flex items-center gap-3 flex-wrap">
            <span className="inline-flex items-center gap-1"><Building className="w-3 h-3" /> {ws?.org_count} orgs</span>
            <span className="inline-flex items-center gap-1"><Users className="w-3 h-3" /> {ws?.member_count} members</span>
            <span className="inline-flex items-center gap-1 uppercase tracking-widest">{ws?.plan || "free"}</span>
          </div>
        </div>

        {/* Orgs list */}
        <section className="sp-glass-strong rounded-2xl p-5 sm:p-6 mb-8">
          <h2 className="text-sm uppercase tracking-widest sp-subtle mb-3 flex items-center gap-2">
            <Building className="w-3.5 h-3.5" /> Organizations in this workspace
          </h2>
          <ul className="grid sm:grid-cols-2 gap-2" data-testid="workspace-orgs-list">
            {orgs.map((o) => (
              <li key={o.id}>
                <Link to={`/app/org/${o.id}`}
                      className="sp-glass rounded-lg p-3 flex items-center justify-between hover:border-white/30"
                      data-testid={`workspace-org-${o.id}`}>
                  <div className="min-w-0">
                    <div className="font-medium truncate">{o.name}</div>
                    {o.website_url && <div className="text-[11px] sp-subtle truncate">{o.website_url}</div>}
                  </div>
                  <ArrowRight className="w-4 h-4 sp-subtle" />
                </Link>
              </li>
            ))}
          </ul>
        </section>

        {/* Invite form — admins only */}
        {isAdmin && (
          <section className="sp-glass-strong rounded-2xl p-5 sm:p-6 mb-8">
            <h2 className="text-sm uppercase tracking-widest sp-subtle mb-3 flex items-center gap-2">
              <UserPlus className="w-3.5 h-3.5" /> Invite a teammate
            </h2>
            <div className="grid sm:grid-cols-2 gap-3 mb-4">
              <div>
                <label className="text-[10px] uppercase tracking-widest sp-subtle mb-1 block">Email <span className="text-white/30 normal-case">(optional — just for your records)</span></label>
                <input
                  type="email" value={inviteEmail} onChange={(e) => setInviteEmail(e.target.value)}
                  placeholder="alice@example.com"
                  className="sp-input w-full text-sm"
                  data-testid="ws-invite-email"
                />
              </div>
              <div>
                <label className="text-[10px] uppercase tracking-widest sp-subtle mb-1 block">Workspace role</label>
                <div className="flex gap-1.5">
                  {["member", "admin"].map((r) => (
                    <button key={r} type="button" onClick={() => setInviteWsRole(r)}
                            className={`sp-btn text-xs flex-1 ${inviteWsRole === r ? "sp-btn-primary" : "sp-btn-ghost"}`}
                            data-testid={`ws-invite-ws-role-${r}`}>
                      {r === "admin" ? <Shield className="w-3 h-3" /> : null}
                      {r}
                    </button>
                  ))}
                </div>
              </div>
            </div>
            <div className="mb-4">
              <div className="text-[10px] uppercase tracking-widest sp-subtle mb-2">Grant access to these organizations</div>
              <ul className="grid sm:grid-cols-2 gap-1.5">
                {orgs.map((o) => {
                  const active = !!inviteGrants[o.id];
                  const role = inviteGrants[o.id] || "member";
                  return (
                    <li key={o.id}
                        className={`sp-glass rounded-lg p-2 flex items-center gap-2 transition-colors ${active ? "!border-orange-400/40 !bg-orange-400/5" : ""}`}>
                      <button type="button"
                              onClick={() => toggleInviteGrant(o.id)}
                              className={`w-5 h-5 rounded border flex items-center justify-center shrink-0 ${active ? "bg-orange-500 border-orange-500" : "border-white/20"}`}
                              data-testid={`ws-invite-org-toggle-${o.id}`}>
                        {active ? <Check className="w-3 h-3 text-white" /> : null}
                      </button>
                      <span className="text-sm truncate flex-1">{o.name}</span>
                      {active && (
                        <select value={role}
                                onChange={(e) => setInviteGrantRole(o.id, e.target.value)}
                                className="text-[11px] bg-transparent border border-white/10 rounded px-1.5 py-0.5"
                                data-testid={`ws-invite-org-role-${o.id}`}>
                          <option value="member">member</option>
                          <option value="admin">admin</option>
                        </select>
                      )}
                    </li>
                  );
                })}
              </ul>
              {Object.keys(inviteGrants).length === 0 && (
                <div className="text-[11px] sp-subtle mt-2">
                  Leaving all unchecked creates a workspace-level invite with no org access yet — you can grant it later from the members list.
                </div>
              )}
            </div>
            <button
              onClick={createInvite} disabled={creatingInvite}
              className="sp-btn sp-btn-primary"
              data-testid="ws-invite-create">
              {creatingInvite ? <Loader2 className="w-4 h-4 animate-spin" /> : <Mail className="w-4 h-4" />}
              Create invite link
            </button>
          </section>
        )}

        {/* Pending invites */}
        {isAdmin && invites.length > 0 && (
          <section className="sp-glass-strong rounded-2xl p-5 sm:p-6 mb-8">
            <h2 className="text-sm uppercase tracking-widest sp-subtle mb-3">Pending invites</h2>
            <ul className="space-y-2" data-testid="ws-pending-invites">
              {invites.map((i) => (
                <li key={i.code} className="sp-glass rounded-lg p-3 flex items-center justify-between gap-3 flex-wrap">
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium truncate">{i.email || "— no email —"}</div>
                    <div className="text-[11px] sp-subtle truncate">
                      {i.ws_role} · {i.org_grants.length} org{i.org_grants.length === 1 ? "" : "s"}: {i.org_grants.map((g) => orgById[g.org_id]?.name || g.org_id).join(", ") || "workspace-only"}
                    </div>
                  </div>
                  <div className="flex items-center gap-1.5 shrink-0">
                    <button type="button" onClick={() => copyInvite(i.code)}
                            className="sp-btn sp-btn-ghost text-xs"
                            data-testid={`ws-invite-copy-${i.code}`}>
                      <Copy className="w-3 h-3" /> Copy link
                    </button>
                    <button type="button" onClick={() => revokeInvite(i.code)}
                            className="sp-btn sp-btn-ghost text-xs text-red-300 hover:text-red-200"
                            data-testid={`ws-invite-revoke-${i.code}`}>
                      <Trash2 className="w-3 h-3" />
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </section>
        )}

        {/* Members grid */}
        <section className="sp-glass-strong rounded-2xl p-5 sm:p-6 mb-8">
          <h2 className="text-sm uppercase tracking-widest sp-subtle mb-3 flex items-center gap-2">
            <Users className="w-3.5 h-3.5" /> Members
          </h2>
          <ul className="space-y-2" data-testid="ws-members-list">
            {members.map((m) => {
              const edit = editing[m.user_id];
              return (
                <li key={m.user_id} className="sp-glass rounded-xl p-3">
                  <div className="flex items-center justify-between gap-3 flex-wrap">
                    <div className="min-w-0 flex-1">
                      <div className="text-sm font-medium truncate">
                        {m.name || m.email}
                        <span className="ml-2 text-[10px] uppercase tracking-widest sp-subtle">{m.role}</span>
                      </div>
                      <div className="text-[11px] sp-subtle truncate">
                        {m.email} · {m.org_access.length} org{m.org_access.length === 1 ? "" : "s"}: {
                          m.org_access.map((g) => `${orgById[g.org_id]?.name || ""} (${g.role})`).join(", ")
                          || "no org access yet"
                        }
                      </div>
                    </div>
                    {isAdmin && m.role !== "owner" && (
                      <div className="flex gap-1.5 shrink-0">
                        <button type="button" onClick={() => (edit?.open ? cancelEdit(m.user_id) : beginEdit(m))}
                                className="sp-btn sp-btn-ghost text-xs"
                                data-testid={`ws-member-edit-${m.user_id}`}>
                          {edit?.open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                          {edit?.open ? "Cancel" : "Edit access"}
                        </button>
                        <button type="button" onClick={() => removeMember(m.user_id)}
                                className="sp-btn sp-btn-ghost text-xs text-red-300 hover:text-red-200"
                                data-testid={`ws-member-remove-${m.user_id}`}>
                          <Trash2 className="w-3 h-3" />
                        </button>
                      </div>
                    )}
                  </div>
                  {edit?.open && (
                    <div className="mt-3 pt-3 border-t border-white/5 space-y-2" data-testid={`ws-member-editor-${m.user_id}`}>
                      <ul className="grid sm:grid-cols-2 gap-1.5">
                        {orgs.map((o) => {
                          const active = !!edit.pending[o.id];
                          const role = edit.pending[o.id] || "member";
                          return (
                            <li key={o.id}
                                className={`sp-glass rounded-lg p-2 flex items-center gap-2 ${active ? "!border-orange-400/40 !bg-orange-400/5" : ""}`}>
                              <button type="button"
                                      onClick={() => toggleEditGrant(m.user_id, o.id)}
                                      className={`w-5 h-5 rounded border flex items-center justify-center shrink-0 ${active ? "bg-orange-500 border-orange-500" : "border-white/20"}`}
                                      data-testid={`ws-member-org-toggle-${m.user_id}-${o.id}`}>
                                {active ? <Check className="w-3 h-3 text-white" /> : null}
                              </button>
                              <span className="text-sm truncate flex-1">{o.name}</span>
                              {active && (
                                <select value={role}
                                        onChange={(e) => setEditGrantRole(m.user_id, o.id, e.target.value)}
                                        className="text-[11px] bg-transparent border border-white/10 rounded px-1.5 py-0.5">
                                  <option value="member">member</option>
                                  <option value="admin">admin</option>
                                </select>
                              )}
                            </li>
                          );
                        })}
                      </ul>
                      <div className="flex gap-2">
                        <button type="button" onClick={() => saveEdit(m.user_id)}
                                className="sp-btn sp-btn-primary text-xs"
                                data-testid={`ws-member-save-${m.user_id}`}>
                          <Check className="w-3 h-3" /> Save
                        </button>
                        <button type="button" onClick={() => cancelEdit(m.user_id)}
                                className="sp-btn sp-btn-ghost text-xs">
                          <XIcon className="w-3 h-3" /> Cancel
                        </button>
                      </div>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      </main>
    </div>
  );
}
