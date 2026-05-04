import React, { useState, useMemo } from "react";
import { useNavigate, useSearchParams, Link } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { toast } from "sonner";
import { ArrowLeft } from "lucide-react";
import Logo from "../components/Logo";

export default function AuthPage() {
  const [params] = useSearchParams();
  const initialMode = params.get("mode") === "register" ? "register" : "login";
  const [mode, setMode] = useState(initialMode);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const { login, register } = useAuth();
  const navigate = useNavigate();

  const isRegister = useMemo(() => mode === "register", [mode]);

  const onSubmit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      if (isRegister) await register(name || email.split("@")[0], email, password);
      else await login(email, password);
      navigate("/app");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Something went wrong");
    } finally {
      setBusy(false);
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
            <label className="text-xs sp-subtle uppercase tracking-widest">Password</label>
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
