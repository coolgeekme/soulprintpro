import React, { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import api from "../lib/api";
import TopBar from "../components/TopBar";
import { toast } from "sonner";

export default function AcceptInvitePage() {
  const { code } = useParams();
  const nav = useNavigate();
  const [status, setStatus] = useState("accepting");
  const [orgId, setOrgId] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    (async () => {
      try {
        const { data } = await api.post("/invites/accept", { code });
        setOrgId(data.org_id);
        setStatus("ok");
        toast.success(data.already_member ? "You're already a member" : "Joined organization");
        setTimeout(() => nav(`/app/org/${data.org_id}`), 700);
      } catch (err) {
        setError(err?.response?.data?.detail || "Invite is invalid or expired");
        setStatus("error");
      }
    })();
  }, [code, nav]);

  return (
    <div className="min-h-screen">
      <TopBar>Accepting invite…</TopBar>
      <div className="max-w-md mx-auto mt-24 px-6 sp-glass rounded-2xl p-8 text-center">
        {status === "accepting" && (
          <>
            <div className="mx-auto w-8 h-8 border-2 border-white/10 border-t-white/70 rounded-full animate-spin mb-4" />
            <div className="text-white">Verifying your invite…</div>
          </>
        )}
        {status === "ok" && (
          <div className="text-white">Welcome aboard! Redirecting…</div>
        )}
        {status === "error" && (
          <>
            <div className="text-white mb-2">We couldn't accept that invite</div>
            <div className="text-sm sp-subtle mb-5">{error}</div>
            <button onClick={() => nav("/app")} className="sp-btn sp-btn-primary">Back to home</button>
          </>
        )}
      </div>
    </div>
  );
}
