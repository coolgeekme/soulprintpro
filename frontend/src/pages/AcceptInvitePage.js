import React, { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import api from "../lib/api";
import TopBar from "../components/TopBar";
import { toast } from "sonner";

/**
 * AcceptInvitePage handles BOTH invite types transparently:
 *
 *  1) Workspace invite (new, W3) — grants workspace_members row PLUS
 *     explicit org_members rows for each `org_grants` entry. Preview
 *     shows the workspace name + every org the user is about to join.
 *
 *  2) Legacy org invite — single org join, existing `/api/invites/accept`
 *     endpoint. Falls back to this path if workspace peek fails.
 *
 * The user doesn't need to know which type they have; the code alone
 * is the discriminator.
 */
export default function AcceptInvitePage() {
  const { code } = useParams();
  const nav = useNavigate();
  const [phase, setPhase] = useState("peeking"); // peeking | preview | accepting | ok | error
  const [preview, setPreview] = useState(null); // workspace invite preview
  const [error, setError] = useState("");

  // Try workspace peek first; if 404, fall through to legacy org accept.
  useEffect(() => {
    (async () => {
      try {
        const { data } = await api.post("/workspace-invites/peek", { code });
        setPreview(data);
        setPhase("preview");
      } catch (err) {
        // Not a workspace invite OR it's used/expired — try the legacy
        // single-org accept endpoint.
        try {
          const { data } = await api.post("/invites/accept", { code });
          toast.success(data.already_member ? "You're already a member" : "Joined organization");
          setTimeout(() => nav(`/app/org/${data.org_id}`), 600);
          setPhase("ok");
        } catch (err2) {
          setError(err2?.response?.data?.detail || err?.response?.data?.detail
                   || "Invite is invalid or expired");
          setPhase("error");
        }
      }
    })();
  }, [code, nav]);

  const acceptWorkspace = async () => {
    setPhase("accepting");
    try {
      const { data } = await api.post("/workspace-invites/accept", { code });
      toast.success(`Joined ${preview?.workspace_name || "workspace"}`);
      // Land on the first granted org if any, else the org picker.
      const target = data.first_org_id ? `/app/org/${data.first_org_id}` : "/app";
      setTimeout(() => nav(target), 600);
      setPhase("ok");
    } catch (err) {
      setError(err?.response?.data?.detail || "Could not accept invite");
      setPhase("error");
    }
  };

  return (
    <div className="min-h-screen">
      <TopBar>Accept invite</TopBar>
      <div className="max-w-md mx-auto mt-16 sm:mt-24 px-4">
        <div className="sp-glass rounded-2xl p-6 sm:p-8" data-testid="accept-invite-card">
          {phase === "peeking" && (
            <div className="text-center">
              <div className="mx-auto w-8 h-8 border-2 border-white/10 border-t-white/70 rounded-full animate-spin mb-4" />
              <div className="text-white">Verifying your invite…</div>
            </div>
          )}

          {phase === "preview" && preview && (
            <>
              <div className="text-[10px] uppercase tracking-widest sp-subtle">Workspace invitation</div>
              <h2 className="text-2xl font-medium tracking-tight mb-1">
                Join <span className="text-orange-300">{preview.workspace_name}</span>
              </h2>
              <p className="text-sm sp-subtle leading-relaxed mb-5">
                You were invited as a <b>{preview.ws_role}</b>
                {preview.org_grants.length > 0
                  ? " and will gain access to the organizations below."
                  : "."}
              </p>
              {preview.org_grants.length > 0 && (
                <ul className="space-y-1.5 mb-6">
                  {preview.org_grants.map((g) => (
                    <li key={g.org_id} className="flex items-center justify-between text-sm sp-glass rounded-lg px-3 py-2">
                      <span className="truncate">{g.org_name || "Untitled"}</span>
                      <span className="text-[10px] uppercase tracking-widest sp-subtle">{g.role}</span>
                    </li>
                  ))}
                </ul>
              )}
              <div className="flex items-center gap-2">
                <button
                  onClick={acceptWorkspace}
                  className="sp-btn sp-btn-primary flex-1"
                  data-testid="accept-workspace-invite-btn"
                >
                  Accept invitation
                </button>
                <button
                  onClick={() => nav("/app")}
                  className="sp-btn sp-btn-ghost"
                  data-testid="decline-workspace-invite-btn"
                >
                  Decline
                </button>
              </div>
            </>
          )}

          {phase === "accepting" && (
            <div className="text-center">
              <div className="mx-auto w-8 h-8 border-2 border-white/10 border-t-white/70 rounded-full animate-spin mb-4" />
              <div className="text-white">Joining workspace…</div>
            </div>
          )}

          {phase === "ok" && (
            <div className="text-center text-white">Welcome aboard! Redirecting…</div>
          )}

          {phase === "error" && (
            <div className="text-center">
              <div className="text-white mb-2">We couldn't accept that invite</div>
              <div className="text-sm sp-subtle mb-5">{error}</div>
              <button
                onClick={() => nav("/app")}
                className="sp-btn sp-btn-primary"
                data-testid="accept-invite-back-btn"
              >
                Back to home
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
