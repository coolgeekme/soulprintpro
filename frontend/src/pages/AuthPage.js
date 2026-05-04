import React, { useState, useMemo } from "react";
import { useNavigate, useSearchParams, Link } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
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
  const [busy, setBusy] = useState(false);
  const [resetting, setResetting] = useState(false);
  const {
    login, register,
    firebaseEnabled,
    loginWithGoogle, loginWithFirebaseEmail, registerWithFirebaseEmail,
    sendPasswordReset,
  } = useAuth();
  const navigate = useNavigate();

  const isRegister = useMemo(() => mode === "register", [mode]);

  // Email/password submit. When Firebase is enabled we use it as the
  // primary credential layer (Firebase manages the password, we sync
  // the resulting identity into Mongo via /auth/me on first hit). We
  // automatically fall back to the legacy bcrypt path for two cases:
  //   - Firebase is not configured at all
  //   - Firebase says "user not found" (existing legacy user — let the
  //     bcrypt password verify them, then we'll prompt them to set up
  //     a Firebase password later)
  const onSubmit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      if (firebaseEnabled) {
        try {
          if (isRegister) {
            await registerWithFirebaseEmail(name || email.split("@")[0], email, password);
          } else {
            await loginWithFirebaseEmail(email, password);
          }
          navigate("/app");
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
      if (isRegister) await register(name || email.split("@")[0], email, password);
      else await login(email, password);
      navigate("/app");
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
      navigate("/app");
    } catch (err) {
      toast.error(friendlyAuthError(err));
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
        <Link to="/" aria-label="SoulPrint Engine Pro home" className="block mb-8">
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
