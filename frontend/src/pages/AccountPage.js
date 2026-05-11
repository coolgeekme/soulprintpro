import React, { useEffect, useState } from "react";
import { toast } from "sonner";
import {
  User, Mail, Phone, Globe2, Lock, Loader2, Save, Eye, EyeOff,
} from "lucide-react";
import api from "../lib/api";
import TopBar from "../components/TopBar";
import { useAuth } from "../context/AuthContext";

/**
 * AccountPage — single, focused profile editor.
 *
 * Two cards:
 *   1. **Profile** — name, email, phone, timezone. PATCH /api/auth/me.
 *      Email change validates uniqueness server-side.
 *   2. **Password** — current + new + confirm. POST /api/auth/me/password.
 *      Hidden entirely for Firebase-auth users (the backend rejects with
 *      a clear "use Google to manage your password" message anyway, but
 *      hiding the form prevents the dead-end UX).
 *
 * Why a dedicated page (not a slide-out modal):
 *   • Email + password changes are high-trust actions; the user should
 *     have a stable URL they can come back to and a clean focus surface.
 *   • Future fields (avatar, MFA, billing email, notification prefs)
 *     all live here without further routing changes.
 */
export default function AccountPage() {
  const { user, refresh } = useAuth();

  // Profile draft — initialised from the auth context's hydrated user.
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [tz, setTz] = useState("");
  const [savingProfile, setSavingProfile] = useState(false);

  // Password draft
  const [curPwd, setCurPwd] = useState("");
  const [newPwd, setNewPwd] = useState("");
  const [confirmPwd, setConfirmPwd] = useState("");
  const [savingPwd, setSavingPwd] = useState(false);
  const [showPwd, setShowPwd] = useState(false);

  // Auth-method detection — Firebase users don't have a password to
  // change here. We infer this from `firebase_uid` on the user doc;
  // the GET /me response strips both password fields so we can't sniff
  // password_hash directly. Render a friendly card instead of the form.
  const isFirebaseOnly = Boolean(user?.firebase_uid) && !user?.has_password;

  useEffect(() => {
    if (!user) return;
    setName(user.name || "");
    setEmail(user.email || "");
    setPhone(user.phone || "");
    setTz(
      user.timezone
      || (Intl?.DateTimeFormat?.()?.resolvedOptions?.()?.timeZone)
      || "",
    );
  }, [user]);

  const saveProfile = async (e) => {
    e?.preventDefault?.();
    setSavingProfile(true);
    try {
      const payload = {};
      if (name !== (user?.name || "")) payload.name = name;
      if (email !== (user?.email || "")) payload.email = email;
      if (phone !== (user?.phone || "")) payload.phone = phone;
      if (tz !== (user?.timezone || "")) payload.timezone = tz;
      if (Object.keys(payload).length === 0) {
        toast.info("No changes to save");
        return;
      }
      await api.patch("/auth/me", payload);
      await refresh?.();
      toast.success("Profile updated");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not save profile");
    } finally {
      setSavingProfile(false);
    }
  };

  const changePassword = async (e) => {
    e?.preventDefault?.();
    if (newPwd.length < 8) {
      toast.error("New password must be at least 8 characters");
      return;
    }
    if (newPwd !== confirmPwd) {
      toast.error("New password and confirmation don't match");
      return;
    }
    setSavingPwd(true);
    try {
      await api.post("/auth/me/password", {
        current_password: curPwd,
        new_password: newPwd,
      });
      toast.success("Password updated");
      setCurPwd(""); setNewPwd(""); setConfirmPwd("");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not change password");
    } finally {
      setSavingPwd(false);
    }
  };

  return (
    <div className="min-h-screen">
      <TopBar>Account</TopBar>
      <main className="max-w-2xl mx-auto px-4 sm:px-6 py-6 sm:py-10" data-testid="account-page">
        <div className="mb-8">
          <div className="text-[10px] uppercase tracking-widest sp-subtle">Account</div>
          <h1 className="text-3xl sm:text-4xl font-medium tracking-tight">Your profile</h1>
          <p className="text-sm sp-subtle mt-1">
            Update how you appear across Foundry and how we reach you.
          </p>
        </div>

        {/* Profile */}
        <form onSubmit={saveProfile} className="sp-glass-strong rounded-2xl p-5 sm:p-6 mb-6" data-testid="profile-card">
          <h2 className="text-sm uppercase tracking-widest sp-subtle mb-4 flex items-center gap-2">
            <User className="w-3.5 h-3.5" /> Profile details
          </h2>
          <div className="grid sm:grid-cols-2 gap-3">
            <Field icon={User} label="Display name" testId="account-name-input">
              <input
                value={name} onChange={(e) => setName(e.target.value)}
                className="sp-input w-full text-sm"
                placeholder="Reggie Dixon"
                data-testid="account-name"
              />
            </Field>
            <Field icon={Mail} label="Email" testId="account-email-input"
                   hint="Used for login and notifications.">
              <input
                type="email" value={email} onChange={(e) => setEmail(e.target.value)}
                className="sp-input w-full text-sm"
                placeholder="you@example.com"
                data-testid="account-email"
              />
            </Field>
            <Field icon={Phone} label="Phone" testId="account-phone-input"
                   hint="Optional. We'll use it for SMS notifications when you opt in.">
              <input
                type="tel" value={phone} onChange={(e) => setPhone(e.target.value)}
                className="sp-input w-full text-sm"
                placeholder="+1 555 555 5555"
                data-testid="account-phone"
              />
            </Field>
            <Field icon={Globe2} label="Timezone" testId="account-tz-input"
                   hint="Used by your agents when scheduling or referring to time.">
              <input
                value={tz} onChange={(e) => setTz(e.target.value)}
                className="sp-input w-full text-sm"
                placeholder="America/New_York"
                data-testid="account-tz"
              />
            </Field>
          </div>
          <div className="mt-5">
            <button
              type="submit" disabled={savingProfile}
              className="sp-btn sp-btn-primary"
              data-testid="account-save-profile"
            >
              {savingProfile ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
              Save profile
            </button>
          </div>
        </form>

        {/* Password */}
        {isFirebaseOnly ? (
          <div className="sp-glass rounded-2xl p-5 sm:p-6" data-testid="account-firebase-notice">
            <h2 className="text-sm uppercase tracking-widest sp-subtle mb-2 flex items-center gap-2">
              <Lock className="w-3.5 h-3.5" /> Password
            </h2>
            <p className="text-sm sp-subtle leading-relaxed">
              You signed in with Google. Manage your password through your{" "}
              <a className="text-orange-300 hover:underline" href="https://myaccount.google.com/security" target="_blank" rel="noreferrer">Google account settings</a>.
            </p>
          </div>
        ) : (
          <form onSubmit={changePassword} className="sp-glass-strong rounded-2xl p-5 sm:p-6" data-testid="password-card">
            <h2 className="text-sm uppercase tracking-widest sp-subtle mb-4 flex items-center gap-2">
              <Lock className="w-3.5 h-3.5" /> Change password
            </h2>
            <div className="space-y-3">
              <Field icon={Lock} label="Current password" testId="account-curpwd-input">
                <PwdInput
                  value={curPwd} onChange={setCurPwd}
                  show={showPwd} onToggle={() => setShowPwd((s) => !s)}
                  placeholder="••••••••"
                  testId="account-curpwd"
                />
              </Field>
              <Field icon={Lock} label="New password" testId="account-newpwd-input"
                     hint="At least 8 characters.">
                <PwdInput
                  value={newPwd} onChange={setNewPwd}
                  show={showPwd} onToggle={() => setShowPwd((s) => !s)}
                  placeholder="••••••••"
                  testId="account-newpwd"
                />
              </Field>
              <Field icon={Lock} label="Confirm new password" testId="account-confirmpwd-input">
                <PwdInput
                  value={confirmPwd} onChange={setConfirmPwd}
                  show={showPwd} onToggle={() => setShowPwd((s) => !s)}
                  placeholder="••••••••"
                  testId="account-confirmpwd"
                />
              </Field>
            </div>
            <div className="mt-5">
              <button
                type="submit" disabled={savingPwd || !curPwd || !newPwd || !confirmPwd}
                className="sp-btn sp-btn-primary"
                data-testid="account-save-password"
              >
                {savingPwd ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                Update password
              </button>
            </div>
          </form>
        )}

        {/* Integrations — currently just GitHub. Per (user, org) so we
            need an active org to render. We pick the first org from
            user.orgs as a default, since this page is org-agnostic. */}
        <Integrations user={user} />
      </main>
    </div>
  );
}


// Lazy import the card so the AccountPage bundle doesn't grow when
// the user never opens the integrations section. Lifted to module
// scope to keep the export profile clean.
import GitHubConnectCard from "../components/GitHubConnectCard";

function Integrations({ user }) {
  // Fetch the caller's orgs directly — useAuth().user doesn't include
  // org membership data, and we need an active org to scope the
  // GitHub connection. We pick the first org returned (stable order:
  // most-recently-joined first per the backend's serializer).
  const [orgId, setOrgId] = useState("");
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { data } = await api.get("/orgs");
        if (cancelled) return;
        const first = (Array.isArray(data) ? data : [])[0];
        if (first?.id) setOrgId(first.id);
      } catch (_) {
        /* user has no orgs — render the join hint */
      }
    })();
    return () => { cancelled = true; };
  }, []);
  if (!orgId) {
    return (
      <div className="sp-glass rounded-2xl p-5 mt-6">
        <div className="text-sm sp-subtle">
          Join or create an organisation to manage integrations.
        </div>
      </div>
    );
  }
  return (
    <section className="mt-8" data-testid="integrations-section">
      <div className="text-[10px] uppercase tracking-widest sp-subtle mb-2">Integrations</div>
      <h2 className="text-xl font-medium tracking-tight mb-3">Connected services</h2>
      <p className="text-sm sp-subtle mb-4 max-w-xl">
        Let mission agents read your repos, open PRs, comment on issues, and push commits on your behalf.
      </p>
      <GitHubConnectCard orgId={orgId} />
    </section>
  );
}

// Compact field renderer to keep the form layout tidy.
function Field({ icon: Icon, label, hint, children }) {
  return (
    <div>
      <label className="text-[10px] uppercase tracking-widest sp-subtle mb-1 flex items-center gap-1.5">
        {Icon && <Icon className="w-3 h-3" />} {label}
      </label>
      {children}
      {hint && <div className="text-[11px] sp-subtle mt-1">{hint}</div>}
    </div>
  );
}

function PwdInput({ value, onChange, show, onToggle, placeholder, testId }) {
  return (
    <div className="relative">
      <input
        type={show ? "text" : "password"}
        value={value} onChange={(e) => onChange(e.target.value)}
        className="sp-input w-full text-sm pr-9"
        placeholder={placeholder}
        autoComplete="new-password"
        data-testid={testId}
      />
      <button
        type="button" onClick={onToggle}
        className="absolute inset-y-0 right-2 flex items-center justify-center text-[10px] sp-subtle hover:text-white"
        title={show ? "Hide password" : "Show password"}
        tabIndex={-1}
      >
        {show ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
      </button>
    </div>
  );
}
