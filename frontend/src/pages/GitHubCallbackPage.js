/* eslint-disable react-hooks/exhaustive-deps */
import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Loader2, Github, AlertCircle, CheckCircle2 } from "lucide-react";
import { toast } from "sonner";
import api from "../lib/api";

/**
 * /auth/github/callback — finishes the OAuth round-trip.
 *
 * GitHub redirects here with `code` and `state`. We POST both to the
 * backend, which validates state, exchanges the code for a token, and
 * persists an encrypted connection row. We surface the result and
 * redirect back to the Integrations panel.
 */
export default function GitHubCallbackPage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [phase, setPhase] = useState("processing"); // processing | done | error
  const [error, setError] = useState("");
  const [conn, setConn] = useState(null);

  useEffect(() => {
    const code = params.get("code");
    const state = params.get("state");
    const ghError = params.get("error");
    if (ghError) {
      setError(params.get("error_description") || ghError);
      setPhase("error");
      return;
    }
    if (!code || !state) {
      setError("Missing authorization code from GitHub.");
      setPhase("error");
      return;
    }
    (async () => {
      try {
        const { data } = await api.post("/github/oauth/callback", { code, state });
        setConn(data?.connection);
        setPhase("done");
        toast.success(`GitHub connected — @${data?.connection?.github_username}`);
        // Bounce back to the Integrations panel after a beat so the
        // user sees the success state before the redirect.
        setTimeout(() => navigate("/app", { replace: true }), 1500);
      } catch (e) {
        setError(e?.response?.data?.detail || "Could not finish GitHub connection.");
        setPhase("error");
      }
    })();
  }, []);

  return (
    <div className="min-h-screen flex items-center justify-center px-6">
      <div className="sp-glass rounded-2xl p-8 max-w-md w-full text-center"
           data-testid="github-callback-card">
        <Github className="w-10 h-10 mx-auto mb-4 opacity-80" />
        {phase === "processing" && (
          <>
            <h1 className="text-xl font-medium mb-2">Connecting your GitHub account…</h1>
            <p className="sp-subtle text-sm mb-4">
              Validating the authorization code with GitHub.
            </p>
            <Loader2 className="w-6 h-6 animate-spin mx-auto opacity-50" />
          </>
        )}
        {phase === "done" && (
          <>
            <CheckCircle2 className="w-8 h-8 text-green-400 mx-auto mb-3" />
            <h1 className="text-xl font-medium mb-2">Connected!</h1>
            <p className="sp-subtle text-sm">
              {conn?.github_username
                ? <>Signed in as <b className="text-white/90">@{conn.github_username}</b>.</>
                : "Your GitHub account is now linked."}{" "}
              Redirecting…
            </p>
          </>
        )}
        {phase === "error" && (
          <>
            <AlertCircle className="w-8 h-8 text-red-400 mx-auto mb-3" />
            <h1 className="text-xl font-medium mb-2">Couldn't finish that</h1>
            <p className="sp-subtle text-sm mb-4">{error}</p>
            <button
              onClick={() => navigate("/app", { replace: true })}
              className="sp-btn sp-btn-ghost text-sm"
              data-testid="github-callback-back"
            >
              Back to app
            </button>
          </>
        )}
      </div>
    </div>
  );
}
