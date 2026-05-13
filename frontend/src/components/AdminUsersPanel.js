import React, { useCallback, useEffect, useState } from "react";
import api, { errorMessage } from "../lib/api";
import { toast } from "sonner";
import {
  Loader2, Search, ShieldCheck, Plus, Pencil, Trash2, X as XIcon, Save,
  CheckCircle2, Ban, KeyRound, Users as UsersIcon, Clock,
} from "lucide-react";
import { formatLocalDateTime, formatRelativeTime, parseServerDate } from "../lib/datetime";

// Pill that shows "Xh left" while the user is inside their first 24h
// window, and "expired" once they've crossed it. Helps the admin see
// at a glance who still has time before their first-24h offer / SLA
// runs out. Returns null when we don't have a created_at.
function FirstDayBadge({ createdAt }) {
  const d = parseServerDate(createdAt);
  if (!d) return null;
  const ageMs = Date.now() - d.getTime();
  const dayMs = 24 * 60 * 60 * 1000;
  if (ageMs >= dayMs) return null;
  const leftMs = dayMs - ageMs;
  const hoursLeft = Math.floor(leftMs / (60 * 60 * 1000));
  const minsLeft = Math.floor((leftMs % (60 * 60 * 1000)) / (60 * 1000));
  // Tighter "X min left" when we're inside the final hour so the
  // window doesn't read as "0h left" for the last 59 minutes.
  const label = hoursLeft >= 1 ? `${hoursLeft}h left` : `${minsLeft}m left`;
  // Colour ramp — green > 12h, amber 4-12h, red < 4h. Mirrors the
  // urgency the admin actually feels when triaging fresh signups.
  const tone = hoursLeft >= 12
    ? "bg-emerald-500/15 text-emerald-200 border-emerald-500/30"
    : hoursLeft >= 4
      ? "bg-amber-500/15 text-amber-200 border-amber-500/30"
      : "bg-red-500/15 text-red-200 border-red-500/30";
  return (
    <span
      className={`inline-flex items-center gap-1 text-[10px] uppercase tracking-widest px-1.5 py-0.5 rounded-full border ${tone}`}
      title="Time remaining in the first 24 hours after signup"
      data-testid="admin-user-24h-pill"
    >
      <Clock className="w-3 h-3" /> {label}
    </span>
  );
}

/**
 * AdminUsersPanel — superadmin user management (list + add + edit +
 * delete + disable / re-enable). Backed by `/api/admin/users` (GET
 * paginated list) plus the new POST / PATCH / DELETE endpoints in
 * `backend/admin.py`.
 *
 * Behaviours that protect the admin from locking themselves out (also
 * enforced server-side, so a tampered client still 400s):
 * - cannot demote yourself from superadmin
 * - cannot disable yourself
 * - cannot delete yourself
 */
export default function AdminUsersPanel({ currentUserId }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [pageSize] = useState(25);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState("");
  const [searchInput, setSearchInput] = useState("");

  const [showCreate, setShowCreate] = useState(false);
  const [editing, setEditing] = useState(null);     // user object being edited or null
  const [deletingId, setDeletingId] = useState(""); // user.id mid-delete

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await api.get("/admin/users", {
        params: { page, page_size: pageSize, search },
      });
      setRows(data?.rows || []);
      setTotal(Number(data?.total || 0));
    } catch (err) {
      toast.error(errorMessage(err, "Failed to load users"));
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, search]);

  useEffect(() => { load(); }, [load]);

  const onSearchSubmit = (e) => {
    e.preventDefault();
    setPage(1);
    setSearch(searchInput.trim());
  };

  const onDelete = async (u) => {
    if (u.id === currentUserId) {
      toast.error("You can't delete your own account.");
      return;
    }
    if (!window.confirm(`Permanently delete ${u.email}?\n\nThis removes their org memberships but preserves their conversation history (anonymised).`)) return;
    setDeletingId(u.id);
    try {
      await api.delete(`/admin/users/${u.id}`);
      toast.success(`Deleted ${u.email}`);
      // If we just nuked the last row on the last page, drop a page.
      if (rows.length === 1 && page > 1) setPage((p) => p - 1);
      else load();
    } catch (err) {
      toast.error(errorMessage(err, "Could not delete user"));
    } finally {
      setDeletingId("");
    }
  };

  const onToggleDisabled = async (u) => {
    if (u.id === currentUserId) {
      toast.error("You can't disable your own account.");
      return;
    }
    const next = !u.is_disabled;
    try {
      await api.patch(`/admin/users/${u.id}`, { is_disabled: next });
      toast.success(next ? `${u.email} disabled` : `${u.email} re-enabled`);
      load();
    } catch (err) {
      toast.error(errorMessage(err, "Update failed"));
    }
  };

  return (
    <div className="space-y-4" data-testid="admin-users-panel">
      <div className="flex flex-wrap items-center gap-2 justify-between">
        <form onSubmit={onSearchSubmit} className="flex items-center gap-2 flex-1 min-w-[260px] max-w-md">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 sp-subtle" />
            <input
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              placeholder="Search by email or name…"
              className="w-full pl-9 pr-3 py-2 rounded-xl text-sm focus:sp-focus"
              style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
              data-testid="admin-users-search"
            />
          </div>
          <button type="submit" className="sp-btn sp-btn-ghost text-xs">Search</button>
        </form>
        <button
          type="button"
          onClick={() => setShowCreate(true)}
          className="sp-btn sp-btn-primary text-xs"
          data-testid="admin-users-add"
        >
          <Plus className="w-3.5 h-3.5" /> Add user
        </button>
      </div>

      <div className="sp-glass rounded-xl overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead style={{ background: "color-mix(in srgb, var(--fg) 6%, transparent)" }}>
              <tr className="text-left text-[10px] uppercase tracking-widest sp-subtle">
                <th className="px-3 py-2">Email</th>
                <th className="px-3 py-2">Name</th>
                <th className="px-3 py-2 text-right">Orgs</th>
                <th className="px-3 py-2">Provider</th>
                <th className="px-3 py-2">Joined</th>
                <th className="px-3 py-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {loading && rows.length === 0 ? (
                <tr><td colSpan={6} className="text-center py-10 sp-subtle">
                  <Loader2 className="w-4 h-4 animate-spin inline mr-2" />Loading users…
                </td></tr>
              ) : rows.length === 0 ? (
                <tr><td colSpan={6} className="text-center py-10 sp-subtle">
                  <UsersIcon className="w-4 h-4 inline mr-2" />No users match this search.
                </td></tr>
              ) : rows.map((u) => (
                <tr
                  key={u.id}
                  className={`border-t hover:bg-white/[0.02] transition ${u.is_disabled ? "opacity-60" : ""}`}
                  style={{ borderColor: "var(--border-soft)" }}
                  data-testid={`admin-user-row-${u.id}`}
                >
                  <td className="px-3 py-2">
                    <span className="flex items-center gap-2">
                      {u.is_superadmin && <ShieldCheck className="w-3.5 h-3.5 text-emerald-400" title="Superadmin" />}
                      {u.is_disabled && <Ban className="w-3.5 h-3.5 text-red-400" title="Disabled" />}
                      <span className="truncate max-w-[260px]" title={u.email}>{u.email}</span>
                    </span>
                  </td>
                  <td className="px-3 py-2 truncate max-w-[180px]" title={u.name}>{u.name || "—"}</td>
                  <td className="px-3 py-2 text-right">{u.org_count}</td>
                  <td className="px-3 py-2 text-xs sp-subtle">
                    {u.firebase_provider
                      ? u.firebase_provider.replace(".com", "")
                      : (u.firebase_uid ? "firebase" : "password")}
                  </td>
                  <td
                    className="px-3 py-2 text-xs sp-subtle whitespace-nowrap"
                    title={u.created_at ? formatLocalDateTime(u.created_at) : ""}
                  >
                    {u.created_at ? (
                      <div className="flex flex-col gap-1 items-start">
                        <div className="flex items-center gap-2">
                          <span className="tabular-nums">{formatLocalDateTime(u.created_at)}</span>
                          <FirstDayBadge createdAt={u.created_at} />
                        </div>
                        <span className="text-[10px] sp-subtle">{formatRelativeTime(u.created_at)}</span>
                      </div>
                    ) : "—"}
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-1 justify-end">
                      <button
                        type="button"
                        onClick={() => setEditing(u)}
                        className="sp-btn sp-btn-ghost text-[11px]"
                        title="Edit user"
                        data-testid={`admin-user-edit-${u.id}`}
                      >
                        <Pencil className="w-3 h-3" /> Edit
                      </button>
                      <button
                        type="button"
                        onClick={() => onToggleDisabled(u)}
                        disabled={u.id === currentUserId}
                        className="sp-btn sp-btn-ghost text-[11px] disabled:opacity-40"
                        title={u.is_disabled ? "Re-enable account" : "Disable account"}
                        data-testid={`admin-user-toggle-${u.id}`}
                      >
                        {u.is_disabled
                          ? <><CheckCircle2 className="w-3 h-3" /> Enable</>
                          : <><Ban className="w-3 h-3" /> Disable</>}
                      </button>
                      <button
                        type="button"
                        onClick={() => onDelete(u)}
                        disabled={u.id === currentUserId || deletingId === u.id}
                        className="sp-btn sp-btn-ghost text-[11px] disabled:opacity-40"
                        title="Delete user"
                        style={{ color: "rgb(239 68 68)" }}
                        data-testid={`admin-user-delete-${u.id}`}
                      >
                        {deletingId === u.id
                          ? <Loader2 className="w-3 h-3 animate-spin" />
                          : <><Trash2 className="w-3 h-3" /> Delete</>}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Pagination */}
      <div className="flex items-center justify-between text-xs sp-subtle">
        <span>{total === 0 ? "No results" : `${(page - 1) * pageSize + 1}–${Math.min(page * pageSize, total)} of ${total}`}</span>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1 || loading}
            className="sp-btn sp-btn-ghost text-[11px] disabled:opacity-40"
            data-testid="admin-users-prev"
          >Prev</button>
          <span>Page {page} / {totalPages}</span>
          <button
            type="button"
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page >= totalPages || loading}
            className="sp-btn sp-btn-ghost text-[11px] disabled:opacity-40"
            data-testid="admin-users-next"
          >Next</button>
        </div>
      </div>

      {showCreate && (
        <UserFormModal
          mode="create"
          onClose={() => setShowCreate(false)}
          onSaved={() => { setShowCreate(false); setPage(1); load(); }}
        />
      )}

      {editing && (
        <UserFormModal
          mode="edit"
          user={editing}
          isSelf={editing.id === currentUserId}
          onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); load(); }}
        />
      )}
    </div>
  );
}

function UserFormModal({ mode, user, isSelf = false, onClose, onSaved }) {
  const isCreate = mode === "create";
  const [name, setName] = useState(user?.name || "");
  const [email, setEmail] = useState(user?.email || "");
  const [password, setPassword] = useState("");
  const [isSuperadmin, setIsSuperadmin] = useState(Boolean(user?.is_superadmin));
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (!email.trim()) return toast.error("Email is required");
    if (isCreate && password.length < 6) return toast.error("Password must be at least 6 characters");
    if (!isCreate && password && password.length < 6) return toast.error("Password must be at least 6 characters");
    setBusy(true);
    try {
      if (isCreate) {
        await api.post("/admin/users", {
          name: name.trim(),
          email: email.trim(),
          password,
          is_superadmin: isSuperadmin,
        });
        toast.success(`Created ${email}`);
      } else {
        const body = {};
        if (name.trim() !== user.name) body.name = name.trim();
        if (email.trim() !== user.email) body.email = email.trim();
        if (password) body.password = password;
        if (isSuperadmin !== Boolean(user.is_superadmin)) body.is_superadmin = isSuperadmin;
        if (Object.keys(body).length === 0) {
          toast.message("No changes to save");
          setBusy(false);
          return;
        }
        await api.patch(`/admin/users/${user.id}`, body);
        toast.success(`Updated ${email}`);
      }
      onSaved();
    } catch (err) {
      toast.error(errorMessage(err, "Save failed"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: "rgba(0,0,0,0.55)", backdropFilter: "blur(6px)" }}
      onClick={onClose}
    >
      <form
        onSubmit={submit}
        onClick={(e) => e.stopPropagation()}
        className="sp-glass-strong rounded-2xl w-full max-w-md p-6 sp-fade-up"
        data-testid={`admin-user-modal-${mode}`}
      >
        <div className="flex items-start justify-between mb-4">
          <div>
            <h2 className="sp-display text-2xl">{isCreate ? "Add user" : "Edit user"}</h2>
            <p className="text-xs sp-subtle mt-1">
              {isCreate
                ? "Create a new account. Send the password to the user out-of-band."
                : "Leave password blank to keep the current one."}
            </p>
          </div>
          <button type="button" onClick={onClose} className="sp-btn sp-btn-ghost text-xs">
            <XIcon className="w-3.5 h-3.5" />
          </button>
        </div>

        <div className="space-y-3">
          <div>
            <label className="text-xs sp-subtle uppercase tracking-widest">Name</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Display name"
              className="w-full mt-1 rounded-xl px-3 py-2 text-sm focus:sp-focus"
              style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
              data-testid="admin-user-form-name"
            />
          </div>
          <div>
            <label className="text-xs sp-subtle uppercase tracking-widest">Email</label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="user@company.com"
              required
              className="w-full mt-1 rounded-xl px-3 py-2 text-sm focus:sp-focus"
              style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
              data-testid="admin-user-form-email"
            />
          </div>
          <div>
            <label className="text-xs sp-subtle uppercase tracking-widest flex items-center gap-1.5">
              <KeyRound className="w-3 h-3" />
              {isCreate ? "Password" : "New password (optional)"}
            </label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={isCreate ? "At least 6 characters" : "Leave blank to keep current"}
              minLength={isCreate ? 6 : undefined}
              autoComplete="new-password"
              className="w-full mt-1 rounded-xl px-3 py-2 text-sm focus:sp-focus"
              style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
              data-testid="admin-user-form-password"
            />
          </div>
          <label
            className="flex items-center gap-2 cursor-pointer pt-1"
            title={isSelf
              ? "Admins cannot demote themselves — ask another superadmin if you need to downgrade your account."
              : "Grant or revoke superadmin access"}
          >
            <input
              type="checkbox"
              checked={isSuperadmin}
              onChange={(e) => setIsSuperadmin(e.target.checked)}
              disabled={isSelf && Boolean(user?.is_superadmin)}
              className="accent-emerald-500"
              data-testid="admin-user-form-superadmin"
            />
            <span className="text-sm flex items-center gap-1.5">
              <ShieldCheck className="w-3.5 h-3.5 text-emerald-400" />
              Superadmin (full access to /admin)
            </span>
          </label>
        </div>

        <div className="flex items-center justify-end gap-2 mt-6">
          <button type="button" onClick={onClose} className="sp-btn sp-btn-ghost text-xs">
            Cancel
          </button>
          <button
            type="submit"
            disabled={busy}
            className="sp-btn sp-btn-primary text-xs disabled:opacity-50"
            data-testid="admin-user-form-save"
          >
            {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
            {busy ? "Saving…" : (isCreate ? "Create user" : "Save changes")}
          </button>
        </div>
      </form>
    </div>
  );
}
