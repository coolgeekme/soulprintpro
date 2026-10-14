import React, { useEffect, useState, useCallback } from "react";
import { useAuth } from "../context/AuthContext";
import api from "../lib/api";
import { toast } from "sonner";
import { Loader2, KeyRound, LogOut } from "lucide-react";
import Logo from "./Logo";

/**
 * SiteAccessGate
 * --------------
 * Pre-launch full-screen lock that wraps every authenticated route.
 * It calls /api/access-codes/status once on mount and renders one of:
 *   - children (user has access — gate dismissed)
 *   - <Loader/> while checking
 *   - <CodeForm/> blocking input UI
 *
 * Superadmins always see children. Trial users always see children.
 * Anyone else has to redeem a private/trial code first.
 *
 * Why a wrapper rather than a route? The user sees /app/* URLs in
 * their bookmarks; if we shipped a separate /unlock route every deep
 * link would 404 until they redeemed. Wrapping keeps deep links
 * intact post-redeem.
 */
export default function SiteAccessGate({ children }) {
  const { user, refresh, logout } = useAuth();
  const [state, setState] = useState({ loading: true, hasAccess: false });
  const [code, setCode] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const probe = useCallback(async () => {
    try {
      const { data } = await api.get("/access-codes/status");
      setState({
        loading: false,
        hasAccess: !!data?.has_access,
        reason: data?.reason || "none",
      });
    } catch (_err) {
      // Treat probe failure as "needs gate" rather than crashing — the
      // form just won't be redeemable until the backend recovers.
      setState({ loading: false, hasAccess: false, reason: "error" });
    }
  }, []);

  useEffect(() => {
    // Fast path: /auth/me already told us about access status. Use it
    // to skip the extra round-trip whenever possible.
    if (user && (user.has_site_access === true)) {
      setState({ loading: false, hasAccess: true, reason: "auth_me" });
      return;
    }
    probe();
  }, [user, probe]);

  const onSubmit = async (e) => {
    e.preventDefault();
    if (!code.trim()) {
      toast.error("Enter your access code.");
      return;
    }
    setSubmitting(true);
    try {
      await api.post("/access-codes/redeem", { code: code.trim() });
      toast.success("Welcome in.");
      // Re-pull /auth/me so other parts of the app see the trial /
      // access stamps without a refresh.
      try { await refresh(); } catch (_) {}
      await probe();
    } catch (err) {
      const msg = err?.response?.data?.detail || "That code didn't work. Try again.";
      toast.error(msg);
    } finally {
      setSubmitting(false);
    }
  };

  if (state.loading) {
    return (
      <div className="flex items-center justify-center h-screen">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 border-2 border-white/10 border-t-white/70 rounded-full animate-spin" />
          <span className="text-xs tracking-widest uppercase sp-subtle">
            Checking access
          </span>
        </div>
      </div>
    );
  }

  if (state.hasAccess) return children;

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center px-6"
      style={{ background: "var(--bg)" }}
      data-testid="site-access-gate"
    >
      <div className="pointer-events-none absolute inset-0 -z-10">
        <div className="sp-aurora" />
      </div>
      <div className="w-full max-w-md sp-glass-strong rounded-3xl p-8 sp-fade-up">
        <div className="block mb-6">
          <Logo variant="wordmark" className="h-9 w-auto mx-auto" />
        </div>
        <div className="flex items-center justify-center gap-2 mb-3">
          <KeyRound className="w-4 h-4 text-amber-400" />
          <span className="text-[10px] uppercase tracking-widest sp-subtle">
            Pre-launch access
          </span>
        </div>
        <h1 className="sp-display text-3xl text-center mb-1">
          One more step.
        </h1>
        <p className="sp-subtle text-sm text-center mb-6">
          Foundry is currently invite-only. Enter your access
          code or 7-day trial code to continue.
        </p>

        <form onSubmit={onSubmit} className="flex flex-col gap-3">
          <div>
            <label className="text-xs sp-subtle uppercase tracking-widest">
              Access code
            </label>
            <input
              data-testid="access-gate-input"
              autoFocus
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="ABCD-EFGH-JKMN"
              className="w-full mt-1 bg-white/[0.03] border border-white/10 rounded-xl px-4 py-3 text-sm text-white placeholder:text-white/30 focus:border-white/30 focus:sp-focus tracking-widest font-mono"
              autoComplete="off"
              spellCheck={false}
              maxLength={40}
            />
          </div>
          <button
            data-testid="access-gate-submit"
            type="submit"
            disabled={submitting}
            className="sp-btn sp-btn-primary justify-center mt-2 disabled:opacity-60"
          >
            {submitting ? (
              <span className="inline-flex items-center gap-2">
                <Loader2 className="w-3.5 h-3.5 animate-spin" /> Checking…
              </span>
            ) : (
              "Unlock"
            )}
          </button>
        </form>

        <div className="text-center mt-6 text-xs sp-subtle">
          Don't have a code?{" "}
          <a href="/get-audit" className="text-white hover:underline">
            Request a 7-day trial
          </a>
          .
        </div>

        <button
          type="button"
          onClick={logout}
          className="mt-4 w-full inline-flex items-center justify-center gap-2 text-xs sp-subtle hover:text-white"
          data-testid="access-gate-signout"
        >
          <LogOut className="w-3 h-3" /> Sign out
        </button>
      </div>
    </div>
  );
}
