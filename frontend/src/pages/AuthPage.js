import React, { useState, useMemo } from "react";
import { useNavigate, useSearchParams, Link } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import api from "../lib/api";
import { toast } from "sonner";
import { ArrowLeft, Mail } from "lucide-react";
import Logo from "../components/Logo";
import { friendlyAuthError } from "../lib/firebase";

// Inline Google "G" mark — official multicolour. Used on the Google
// sign-in button to match Material guidelines without pulling in another
// icon library.
const GoogleGlyph = ({ className = "w-4 h-4" }) => (
  <svg viewBox="0 0 48 48" className={className} aria-hidden="true">
    <path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3c-1.6 4.6-6 8-11.3 8-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.9 1.2 8 3.1l5.7-5.7C34 6.1 29.3 4 24 4 12.95 4 4 12.95 4 24s8.95 20 20 20 20-8.95 20-20c0-1.34-.14-2.65-.4-3.5z" />
    <path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.7 16 18.95 13 24 13c3.1 0 5.9 1.2 8 3.1l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.6 8.4 6.3 14.7z" />
    <path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35 26.7 36 24 36c-5.3 0-9.7-3.4-11.3-8L6.1 33C9.4 39.6 16.1 44 24 44z" />
    <path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.7 2-2 3.8-3.8 5.1l6.2 5.2C42.6 35 44 30 44 24c0-1.34-.14-2.65-.4-3.5z" />
  </svg>
);

export default function AuthPage() {
  const [params] = useSearchParams();
  const initialMode = params.get("mode") === "register" ? "register" : "login";
  const [mode, setMode] = useState(initialMode);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  // Pre-launch access code — required for new sign-ups so we can keep
  // the platform invite-only without forcing existing users through a
  // post-auth gate.
  const [accessCode, setAccessCode] = useState(params.get("code") || "");
  const [busy, setBusy] = useState(false);
  const [resetting, setResetting] = useState(false);
  const {
    login, register,
    firebaseEnabled,
    loginWithGoogle, loginWithFirebaseEmail, registerWithFirebaseEmail,
    sendPasswordReset, refresh,
  } = useAuth();
  const navigate = useNavigate();

  const isRegister = useMemo(() => mode === "register", [mode]);

  // Optional `?plan=spark|temper|contact` from /pricing → see landAfterAuth.
  const planIntentParam = params.get("plan");
  if (planIntentParam === "spark" || planIntentParam === "temper" || planIntentParam === "contact") {
    try { window.localStorage.setItem("foundry:planIntent", planIntentParam); } catch {}
  }

  // Optional `?invite={code}` carried from /invite/{code} when the
  // invitee wasn't logged in. We persist to localStorage so a
  // Firebase OAuth round-trip (which loses query params) still
  // resumes correctly, then auto-accept inside landAfterAuth.
  const inviteParam = params.get("invite");
  if (inviteParam) {
    try { window.localStorage.setItem("foundry:inviteCode", inviteParam); } catch {}
  }

  /** Land the user after a successful auth. Priority order:
   *    1. Invite code (auto-accept + drop into org)
   *    2. Plan intent (deep-link to billing w/ consent modal armed)
   *    3. Default /app
   */
  const landAfterAuth = async () => {
    let inviteCode = "";
    let plan = "";
    try {
      inviteCode = window.localStorage.getItem("foundry:inviteCode") || "";
      plan = window.localStorage.getItem("foundry:planIntent") || "";
    } catch {}

    if (inviteCode) {
      try { window.localStorage.removeItem("foundry:inviteCode"); } catch {}
      // Try workspace invite first (richer), fall back to legacy org invite.
      let accepted = false;
      try {
        await api.post("/workspace-invites/accept", { code: inviteCode });
        accepted = true;
      } catch { /* not a workspace invite — try the legacy org path */ }
      if (!accepted) {
        try {
          await api.post("/invites/accept", { code: inviteCode });
          accepted = true;
        } catch { /* invite was bad — fall through to /app */ }
      }
      if (accepted) {
        // Don't burn the plan intent if there is one — they can still
        // upgrade later from /app/billing.
        navigate("/app");
        return;
      }
    }

    if (plan === "spark" || plan === "temper" || plan === "contact") {
      try { window.localStorage.removeItem("foundry:planIntent"); } catch {}
      if (plan === "contact") {
        navigate(`/app/billing?contact=1`);
      } else {
        navigate(`/app/billing?subscribe=${plan}`);
      }
      return;
    }

    navigate("/app");
  };

  // Email/password submit. When Firebase is enabled we use it as the
  // primary credential layer (Firebase manages the password, we sync
  // the resulting identity into Mongo via /auth/me on first hit). We
  // automatically fall back to the legacy bcrypt path for two cases:
  //   - Firebase is not configured at all
  //   - Firebase says "user not found" (existing legacy user — let the
  //     bcrypt password verify them, then we'll prompt them to set up
  //     a Firebase password later)
  //
  // For REGISTRATION specifically: we pre-validate the access code
  // BEFORE touching Firebase so a bad code doesn't leave the user with
  // a half-baked Firebase account they then can't use. After Firebase
  // creates the user, we redeem the code via the backend to stamp
  // `access_granted_at` on their Mongo row.
  const onSubmit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      // Gate check first for registration — fail fast if the code is bad.
      if (isRegister) {
        const code = accessCode.trim();
        if (!code) {
          toast.error("An access code is required to sign up.");
          return;
        }
        try {
          await api.post("/access-codes/validate", { code });
        } catch (err) {
          // Show the backend's reason when present, the raw axios
          // error message when not. Falling back to the generic
          // "didn't check out" line hid a silent header-encoding
          // bug for weeks (Feb 2026) — make sure ANY future surprise
          // surfaces a useful diagnostic instead of vanishing.
          const detail = err?.response?.data?.detail;
          const fallback = err?.message
            ? `Couldn't validate that code: ${err.message}`
            : "That access code didn't check out.";
          toast.error(detail || fallback);
          return;
        }
      }
      if (firebaseEnabled) {
        try {
          if (isRegister) {
            await registerWithFirebaseEmail(name || email.split("@")[0], email, password);
            // Burn the code now that the Firebase + Mongo user rows
            // exist. Refresh /auth/me so `has_site_access` flips to
            // true before we land on /app and the gate evaluates.
            try {
              await api.post("/access-codes/redeem", { code: accessCode.trim() });
              await refresh();
            } catch (_redeemErr) {
              // Redeem failed but the user IS registered. The gate
              // will catch them post-auth and let them try again
              // there — don't block navigation.
            }
          } else {
            await loginWithFirebaseEmail(email, password);
          }
          await landAfterAuth();
          return;
        } catch (err) {
          // Fall back to legacy bcrypt for existing users that pre-date
          // Firebase. Any other error → surface and stop.
          const code = err?.code || "";
          const fallbackable =
            !isRegister &&
            (code === "auth/user-not-found" || code === "auth/invalid-credential");
          if (!fallbackable) {
            toast.error(friendlyAuthError(err));
            return;
          }
          // intentional fall-through to legacy login below
        }
      }
      if (isRegister) await register(name || email.split("@")[0], email, password, accessCode.trim());
      else await login(email, password);
      await landAfterAuth();
    } catch (err) {
      toast.error(err?.response?.data?.detail || friendlyAuthError(err));
    } finally {
      setBusy(false);
    }
  };

  const onGoogle = async () => {
    if (!firebaseEnabled) {
      toast.error("Google sign-in isn't configured on this deployment.");
      return;
    }
    setBusy(true);
    try {
      await loginWithGoogle();
      await landAfterAuth();
    } catch (err) {
      // Backend rejection (e.g. account disabled, token verification
      // failed) — surface its detail. Otherwise the Firebase popup error.
      const backendDetail = err?.response?.data?.detail;
      toast.error(backendDetail || friendlyAuthError(err));
    } finally {
      setBusy(false);
    }
  };

  const onForgot = async () => {
    if (!firebaseEnabled) {
      toast.error("Password reset is not configured on this deployment.");
      return;
    }
    if (!email.trim()) {
      toast.error("Enter your email above first, then click Forgot password.");
      return;
    }
    setResetting(true);
    try {
      await sendPasswordReset(email.trim());
      toast.success(`Password reset email sent to ${email}`);
    } catch (err) {
      toast.error(friendlyAuthError(err));
    } finally {
      setResetting(false);
    }
  };

  return (
    <div className="relative min-h-screen flex items-center justify-center px-6">
      <div className="pointer-events-none fixed inset-0 -z-10"><div className="sp-aurora" /></div>
      <Link
        to="/"
        className="absolute inline-flex items-center gap-2 text-sm sp-subtle hover:text-white"
        style={{
          top: "calc(env(safe-area-inset-top, 0px) + 1.5rem)",
          left: "calc(env(safe-area-inset-left, 0px) + 1.5rem)",
        }}
        data-testid="back-home"
      >
        <ArrowLeft className="w-4 h-4" /> Home
      </Link>
      <div className="w-full max-w-md sp-glass-strong rounded-3xl p-8 sp-fade-up">
        <Link to="/" aria-label="Foundry home" className="block mb-8">
          <Logo variant="wordmark" className="h-10 w-auto mx-auto" />
        </Link>

        <h1 className="sp-display text-3xl mb-1">
          {isRegister ? "Create your SoulPrint" : "Welcome back"}
        </h1>
        <p className="sp-subtle text-sm mb-6">
          {isRegister ? "A working crew of AI agents is a few seconds away." : "Sign in to your workspace."}
        </p>

        {firebaseEnabled && (
          <>
            <button
              type="button"
              onClick={onGoogle}
              disabled={busy}
              className="w-full inline-flex items-center justify-center gap-3 rounded-xl px-4 py-3 text-sm font-medium transition disabled:opacity-60"
              style={{
                background: "var(--input-bg)",
                border: "1px solid var(--input-border)",
                color: "var(--fg)",
              }}
              data-testid="auth-google"
            >
              <GoogleGlyph className="w-4 h-4 shrink-0" />
              Continue with Google
            </button>
            <div className="flex items-center gap-3 my-5">
              <span className="flex-1 h-px" style={{ background: "var(--border-soft)" }} />
              <span className="text-[10px] uppercase tracking-widest sp-subtle">or with email</span>
              <span className="flex-1 h-px" style={{ background: "var(--border-soft)" }} />
            </div>
          </>
        )}

        <form onSubmit={onSubmit} className="flex flex-col gap-3">
          {isRegister && (
            <div>
              <label className="text-xs sp-subtle uppercase tracking-widest">Name</label>
              <input
                data-testid="auth-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Your full name"
                className="w-full mt-1 bg-white/[0.03] border border-white/10 rounded-xl px-4 py-3 text-sm text-white placeholder:text-white/30 focus:border-white/30 focus:sp-focus"
                autoComplete="name"
              />
            </div>
          )}
          {isRegister && (
            <div>
              <label className="text-xs sp-subtle uppercase tracking-widest">
                Access code
              </label>
              <input
                data-testid="auth-access-code"
                value={accessCode}
                onChange={(e) => setAccessCode(e.target.value.toUpperCase())}
                placeholder="ABCD-EFGH-JKMN"
                className="w-full mt-1 bg-white/[0.03] border border-white/10 rounded-xl px-4 py-3 text-sm text-white placeholder:text-white/30 focus:border-white/30 focus:sp-focus tracking-widest font-mono"
                autoComplete="off"
                spellCheck={false}
                maxLength={40}
                required
              />
              <p className="text-[10px] sp-subtle mt-1 leading-snug">
                Invite-only during pre-launch. Don't have a code?{" "}
                <Link to="/get-audit" className="text-white underline">
                  request a free 7-day trial
                </Link>
                .
              </p>
            </div>
          )}
          <div>
            <label className="text-xs sp-subtle uppercase tracking-widest">Email</label>
            <input
              data-testid="auth-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@company.com"
              className="w-full mt-1 bg-white/[0.03] border border-white/10 rounded-xl px-4 py-3 text-sm text-white placeholder:text-white/30 focus:border-white/30 focus:sp-focus"
              autoComplete="email"
              required
            />
          </div>
          <div>
            <div className="flex items-center justify-between">
              <label className="text-xs sp-subtle uppercase tracking-widest">Password</label>
              {!isRegister && firebaseEnabled && (
                <button
                  type="button"
                  onClick={onForgot}
                  disabled={resetting}
                  className="text-[11px] sp-subtle hover:text-white inline-flex items-center gap-1 disabled:opacity-50"
                  data-testid="auth-forgot"
                  title="Send a password-reset email"
                >
                  <Mail className="w-3 h-3" /> {resetting ? "Sending…" : "Forgot password?"}
                </button>
              )}
            </div>
            <input
              data-testid="auth-password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="•••••••"
              minLength={6}
              className="w-full mt-1 bg-white/[0.03] border border-white/10 rounded-xl px-4 py-3 text-sm text-white placeholder:text-white/30 focus:border-white/30 focus:sp-focus"
              autoComplete={isRegister ? "new-password" : "current-password"}
              required
            />
          </div>
          <button
            data-testid="auth-submit"
            type="submit"
            disabled={busy}
            className="sp-btn sp-btn-primary justify-center mt-2 disabled:opacity-60"
          >
            {busy ? "Please wait…" : isRegister ? "Create account" : "Sign in"}
          </button>
        </form>

        <div className="text-center mt-6 text-sm sp-subtle">
          {isRegister ? (
            <>Already have an account?{" "}
              <button data-testid="auth-toggle-login" onClick={() => setMode("login")} className="text-white hover:underline">Sign in</button>
            </>
          ) : (
            <>New here?{" "}
              <button data-testid="auth-toggle-register" onClick={() => setMode("register")} className="text-white hover:underline">Create account</button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
