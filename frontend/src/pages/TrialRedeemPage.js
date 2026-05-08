import React, { useEffect, useState } from "react";
import { useSearchParams, Link, useNavigate } from "react-router-dom";
import { Rocket, Loader2, ArrowRight, Mail, Check, Sparkles } from "lucide-react";
import api, { setToken } from "../lib/api";
import Logo from "../components/Logo";
import { useAuth } from "../context/AuthContext";/**
 * /trial/redeem — combined registration + trial-clock-start.
 *
 * The visitor lands here from the email we sent them. Two states:
 *
 *   1. Code already in the URL (?code=ABCD1234) — render the form
 *      pre-filled with the code. They just need email + name +
 *      password to register and start their trial.
 *
 *   2. No code — render the same form with the code field empty.
 *      They paste the code from the email.
 *
 * On successful redeem we drop straight into /app fully signed in,
 * with the 24-hour trial clock already running.
 */
export default function TrialRedeemPage() {
  const [params] = useSearchParams();
  const nav = useNavigate();
  const { refresh } = useAuth();

  const [code, setCode] = useState((params.get("code") || "").toUpperCase());
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [phase, setPhase] = useState("idle"); // idle | submitting | done | error
  const [error, setError] = useState("");

  // Auto-uppercase the code as the user types so they never get a
  // false-negative from "abcd1234" vs "ABCD1234".
  useEffect(() => {
    setCode((c) => c.toUpperCase());
  }, []);

  const onSubmit = async (e) => {
    e.preventDefault();
    setError("");
    if (!code.trim() || !email.trim() || !name.trim() || !password.trim()) {
      setError("All fields are required.");
      return;
    }
    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    setPhase("submitting");
    try {
      const { data } = await api.post("/trial/redeem", {
        code: code.trim().toUpperCase(),
        email: email.trim(),
        name: name.trim(),
        password,
      });
      // Drop the bearer in place so AuthContext.refresh() can hit
      // /api/auth/me with auth, then trigger a refresh so the rest
      // of the SPA sees the freshly-signed-in user.
      setToken(data.access_token);
      try { await refresh?.(); } catch (_) { /* non-fatal */ }
      setPhase("done");
      // Brief moment to show the success state before redirecting.
      setTimeout(() => nav("/app"), 800);
    } catch (err) {
      setError(err?.response?.data?.detail
        || "Could not redeem the code. Please double-check and try again.");
      setPhase("error");
    }
  };

  return (
    <div className="min-h-screen flex flex-col" data-testid="trial-redeem-page">
      <header className="border-b border-white/5">
        <div className="max-w-3xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between">
          <Link to="/" className="inline-flex items-center gap-2">
            <Logo variant="wordmark" className="h-6 w-auto" />
          </Link>
          <Link to="/auth?mode=login" className="sp-btn sp-btn-ghost text-xs">
            Already have an account? Sign in
          </Link>
        </div>
      </header>

      <main className="flex-1 flex items-center justify-center px-4 sm:px-6 py-10 sm:py-16">
        <div className="w-full max-w-md">
          {/* Hero */}
          <div className="text-center mb-7">
            <div className="inline-flex items-center gap-1.5 sp-glass rounded-full px-3 py-1 text-[10px] uppercase tracking-widest sp-subtle mb-4">
              <Rocket className="w-3 h-3" /> Start your 24-hour trial
            </div>
            <h1 className="text-2xl sm:text-3xl font-medium tracking-tight leading-[1.15]">
              The clock starts when you click submit.
            </h1>
            <p className="text-sm sp-subtle leading-relaxed mt-2">
              Pop in the code we emailed you, set up your account, and you'll
              land in a fully provisioned workspace with 24 hours to put SoulPrint
              Engine Pro through its paces. No credit card required.
            </p>
          </div>

          {phase === "done" ? (
            <SuccessState />
          ) : (
            <form
              onSubmit={onSubmit}
              className="sp-glass-strong rounded-2xl p-5 sm:p-6 space-y-3"
              data-testid="trial-redeem-form"
            >
              <Field label="Trial code" hint="Check your inbox for the email we just sent.">
                <input
                  type="text"
                  value={code}
                  onChange={(e) => setCode(e.target.value.toUpperCase())}
                  placeholder="ABCD1234"
                  className="sp-input font-mono tracking-widest uppercase"
                  data-testid="trial-redeem-code"
                  required
                />
              </Field>
              <Field label="Your name">
                <input
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Alex Kim"
                  className="sp-input"
                  data-testid="trial-redeem-name"
                  required
                />
              </Field>
              <Field label="Email" hint="Use the same email you requested the code with.">
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="alex@company.com"
                  className="sp-input"
                  data-testid="trial-redeem-email"
                  required
                />
              </Field>
              <Field label="Password" hint="Minimum 8 characters.">
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  className="sp-input"
                  data-testid="trial-redeem-password"
                  minLength={8}
                  required
                />
              </Field>

              <button
                type="submit"
                disabled={phase === "submitting"}
                className="sp-btn sp-btn-primary text-sm w-full mt-2 disabled:opacity-60"
                data-testid="trial-redeem-submit"
              >
                {phase === "submitting"
                  ? <><Loader2 className="w-4 h-4 animate-spin" /> Starting your trial…</>
                  : <>Start my 24-hour trial <ArrowRight className="w-4 h-4" /></>}
              </button>

              {error && (
                <div className="text-[12px] text-red-400 leading-relaxed pt-1"
                     data-testid="trial-redeem-error">
                  {error}
                </div>
              )}

              <div className="text-[11px] sp-subtle text-center pt-3 border-t border-white/5">
                Don't have a code?{" "}
                <Link to="/auth?mode=register" className="text-orange-300 hover:text-orange-200 underline-offset-2 hover:underline">
                  Sign up the regular way
                </Link>
                {" "}or{" "}
                <a href="mailto:team@archeforge.com" className="text-orange-300 hover:text-orange-200 underline-offset-2 hover:underline">
                  email us
                </a>
                .
              </div>
            </form>
          )}
        </div>
      </main>
    </div>
  );
}


function Field({ label, hint, children }) {
  return (
    <label className="block">
      <div className="text-[10px] uppercase tracking-widest sp-subtle mb-1">{label}</div>
      {children}
      {hint && <div className="text-[10px] sp-subtle mt-1 leading-relaxed">{hint}</div>}
    </label>
  );
}


function SuccessState() {
  return (
    <div
      className="rounded-2xl p-6 border text-center"
      style={{
        background: "linear-gradient(135deg, rgba(74,222,128,0.10), rgba(74,222,128,0.02))",
        borderColor: "rgba(74,222,128,0.3)",
      }}
      data-testid="trial-redeem-success"
    >
      <div className="w-12 h-12 rounded-full mx-auto mb-3 flex items-center justify-center"
           style={{ background: "rgba(74,222,128,0.15)" }}>
        <Check className="w-5 h-5 text-green-300" />
      </div>
      <div className="text-lg font-medium">You're in. Clock started.</div>
      <p className="text-sm sp-subtle mt-2 leading-relaxed">
        Taking you to your workspace…
      </p>
    </div>
  );
}
