import React, { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import api from "../lib/api";
import TopBar from "../components/TopBar";
import { useAuth } from "../context/AuthContext";
import { toast } from "sonner";
import { Loader2, Users, LogIn, UserPlus, ArrowRight, Shield } from "lucide-react";

/**
 * AcceptInvitePage handles every invite path, authed or not:
 *
 *   1) **Workspace invite** (new, W3) — multi-org grant.
 *      Peeked via `/api/workspace-invites/peek`.
 *   2) **Legacy org invite** — single org join. Peeked via
 *      `/api/invites/peek` (public — no auth required).
 *   3) **Unauthenticated invitee** — we show a public preview of the
 *      org + inviter + role and offer sign-in / register CTAs that
 *      preserve the invite code through auth (localStorage round-trip
 *      handled by AuthPage). After auth, `landAfterAuth()` auto-calls
 *      `/api/invites/accept` and lands the user in the org.
 *
 * The user doesn't need to know which path they're on; the code alone
 * is the discriminator.
 */
export default function AcceptInvitePage() {
  const { code } = useParams();
  const nav = useNavigate();
  const { user, loading: authLoading } = useAuth();
  const [phase, setPhase] = useState("peeking"); // peeking | preview-ws | preview-org | accepting | ok | error
  const [wsPreview, setWsPreview] = useState(null);
  const [orgPreview, setOrgPreview] = useState(null);
  const [error, setError] = useState("");

  // Step 1 — Always peek BEFORE deciding what UI to show. Peeks are
  // public (workspace-invites/peek + invites/peek both work unauth'd)
  // so an unauthenticated invitee sees who invited them and what to.
  useEffect(() => {
    if (authLoading) return;
    let alive = true;
    (async () => {
      // Try workspace invite first (newer + richer preview).
      try {
        const { data } = await api.post("/workspace-invites/peek", { code });
        if (!alive) return;
        setWsPreview(data);
        setPhase("preview-ws");
        return;
      } catch { /* fall through to legacy org invite */ }
      try {
        const { data } = await api.post("/invites/peek", { code });
        if (!alive) return;
        setOrgPreview(data);
        setPhase("preview-org");
      } catch (err) {
        if (!alive) return;
        setError(err?.response?.data?.detail || "Invite is invalid or expired.");
        setPhase("error");
      }
    })();
    return () => { alive = false; };
  }, [code, authLoading]);

  // When an AUTHENTICATED user landed here with a legacy org invite,
  // they can accept it immediately. When unauthed, show sign-in CTAs.
  const acceptOrg = async () => {
    setPhase("accepting");
    try {
      const { data } = await api.post("/invites/accept", { code });
      if (data.already_member) {
        toast.message("You're already a member of that organization.");
      } else {
        toast.success("Joined organization", { duration: 4500 });
      }
      setTimeout(() => nav("/app"), 700);
      setPhase("ok");
    } catch (err) {
      setError(err?.response?.data?.detail || "Could not accept invite");
      setPhase("error");
    }
  };

  const acceptWorkspace = async () => {
    setPhase("accepting");
    try {
      const { data } = await api.post("/workspace-invites/accept", { code });
      const added = data.orgs_added || [];
      const already = data.orgs_already_member || [];
      const wsName = wsPreview?.workspace_name || "workspace";
      if (added.length > 0) {
        const names = added.map((o) => o.name || "Untitled").join(", ");
        toast.success(
          `Joined ${wsName} — ${added.length} new org${added.length > 1 ? "s" : ""} added: ${names}`,
          { duration: 6000 },
        );
      } else if (already.length > 0) {
        toast.message(
          `You already have access to all ${already.length} org${already.length > 1 ? "s" : ""} in this invite.`,
          { duration: 5000 },
        );
      } else {
        toast.success(`Joined ${wsName}`);
      }
      setTimeout(() => nav("/app"), 700);
      setPhase("ok");
    } catch (err) {
      setError(err?.response?.data?.detail || "Could not accept invite");
      setPhase("error");
    }
  };

  // Routes the unauthenticated invitee to register/sign-in with the
  // invite code preserved through the auth round-trip.
  const goAuth = (mode) => {
    try { window.localStorage.setItem("foundry:inviteCode", code); } catch { /* noop */ }
    nav(`/auth?mode=${mode}&invite=${encodeURIComponent(code)}`);
  };

  return (
    <div className="min-h-screen">
      <TopBar>Invite</TopBar>
      <div className="max-w-md mx-auto mt-16 sm:mt-24 px-4">
        <div className="sp-glass rounded-2xl p-6 sm:p-8" data-testid="accept-invite-card">
          {(phase === "peeking" || authLoading) && (
            <div className="text-center">
              <Loader2 className="w-6 h-6 sp-subtle animate-spin mx-auto mb-4" />
              <div className="text-white">Verifying your invite…</div>
            </div>
          )}

          {phase === "preview-ws" && wsPreview && (
            <>
              <div className="text-[10px] uppercase tracking-widest sp-subtle">Workspace invitation</div>
              <h2 className="text-2xl font-medium tracking-tight mb-1">
                Join <span className="text-orange-300">{wsPreview.workspace_name}</span>
              </h2>
              <p className="text-sm sp-subtle leading-relaxed mb-5">
                You were invited as a <b>{wsPreview.ws_role}</b>
                {wsPreview.org_grants.length > 0
                  ? " and will gain access to the organizations below."
                  : "."}
              </p>
              {wsPreview.org_grants.length > 0 && (
                <ul className="space-y-1.5 mb-6">
                  {wsPreview.org_grants.map((g) => (
                    <li key={g.org_id} className="flex items-center justify-between text-sm sp-glass rounded-lg px-3 py-2">
                      <span className="truncate">{g.org_name || "Untitled"}</span>
                      <span className="text-[10px] uppercase tracking-widest sp-subtle">{g.role}</span>
                    </li>
                  ))}
                </ul>
              )}
              {user ? (
                <div className="flex items-center gap-2">
                  <button onClick={acceptWorkspace} className="sp-btn sp-btn-primary flex-1"
                          data-testid="accept-workspace-invite-btn">
                    Accept invitation
                  </button>
                  <button onClick={() => nav("/app")} className="sp-btn sp-btn-ghost"
                          data-testid="decline-workspace-invite-btn">Decline</button>
                </div>
              ) : (
                <UnauthedCTAs onSignIn={() => goAuth("login")} onRegister={() => goAuth("register")} />
              )}
            </>
          )}

          {phase === "preview-org" && orgPreview && (
            <>
              <div className="text-[10px] uppercase tracking-widest sp-subtle flex items-center gap-2">
                <Users className="w-3.5 h-3.5" /> Organization invitation
              </div>
              <h2 className="text-2xl font-medium tracking-tight mb-1 mt-1">
                Join <span className="text-orange-300">{orgPreview.org_name}</span>
              </h2>
              <p className="text-sm sp-subtle leading-relaxed mb-4">
                <b>{orgPreview.inviter_name}</b> invited you to join as a{" "}
                <b>{orgPreview.role}</b>.
              </p>

              {/* What invitees actually pay — short + crystal clear */}
              <div className="sp-glass rounded-xl p-3 text-[12px] sp-subtle leading-relaxed mb-5 flex items-start gap-2">
                <Shield className="w-3.5 h-3.5 mt-0.5 flex-shrink-0 text-emerald-300" />
                <div>
                  <b className="text-white/80">Free to join.</b> Foundry accounts
                  are free. The owner of {orgPreview.org_name} covers the
                  organization's subscription and agent compute — you won't be
                  charged for using it.
                </div>
              </div>

              {user ? (
                <div className="flex items-center gap-2">
                  <button onClick={acceptOrg} className="sp-btn sp-btn-primary flex-1"
                          data-testid="accept-org-invite-btn">
                    Accept invitation <ArrowRight className="w-4 h-4" />
                  </button>
                  <button onClick={() => nav("/app")} className="sp-btn sp-btn-ghost"
                          data-testid="decline-org-invite-btn">Decline</button>
                </div>
              ) : (
                <UnauthedCTAs onSignIn={() => goAuth("login")} onRegister={() => goAuth("register")} />
              )}
            </>
          )}

          {phase === "accepting" && (
            <div className="text-center">
              <Loader2 className="w-6 h-6 sp-subtle animate-spin mx-auto mb-4" />
              <div className="text-white">Joining…</div>
            </div>
          )}

          {phase === "ok" && (
            <div className="text-center text-white">Welcome aboard! Redirecting…</div>
          )}

          {phase === "error" && (
            <div className="text-center">
              <div className="text-white mb-2">We couldn't accept that invite</div>
              <div className="text-sm sp-subtle mb-5">{error}</div>
              <button onClick={() => nav("/app")} className="sp-btn sp-btn-primary"
                      data-testid="accept-invite-back-btn">
                Back to home
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function UnauthedCTAs({ onSignIn, onRegister }) {
  return (
    <div className="space-y-2" data-testid="invite-unauthed-ctas">
      <button onClick={onRegister} className="sp-btn sp-btn-primary w-full"
              data-testid="invite-register-btn">
        <UserPlus className="w-4 h-4" /> Create account &amp; join
      </button>
      <button onClick={onSignIn} className="sp-btn sp-btn-ghost w-full"
              data-testid="invite-signin-btn">
        <LogIn className="w-4 h-4" /> I already have an account
      </button>
      <p className="text-[11px] sp-subtle text-center mt-2 leading-relaxed">
        Your invite is saved — we'll add you to the organization automatically
        right after you sign in.
      </p>
    </div>
  );
}
