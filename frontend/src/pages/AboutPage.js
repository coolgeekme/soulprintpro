import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import api from "../lib/api";
import { ArrowLeft, Loader2 } from "lucide-react";
import Logo from "../components/Logo";

/**
 * AboutPage — renders /app/memory/ABOUT.md.
 *
 * Same source served at GET /api/about powers both the in-app page (linked
 * from the landing page header and the user menu) and any marketing site
 * surface that wants the canonical copy. Updating the markdown updates
 * both — no redeploy required.
 *
 * Design choice: render server-side markdown as-is (no remark plugins beyond
 * GFM, no sanitization beyond react-markdown defaults) — the source is
 * trusted and authored by us, not user-submitted.
 */
export default function AboutPage() {
  const [md, setMd] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const { data } = await api.get("/about");
        if (alive) setMd(data.markdown || "");
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, []);

  return (
    <div className="min-h-screen" data-testid="about-page">
      <div
        className="max-w-3xl mx-auto px-6"
        style={{
          paddingTop: "calc(env(safe-area-inset-top, 0px) + 2.5rem)",
          paddingBottom: "2.5rem",
          paddingLeft: "max(1.5rem, env(safe-area-inset-left, 0px))",
          paddingRight: "max(1.5rem, env(safe-area-inset-right, 0px))",
        }}
      >
        <div className="flex items-center justify-between mb-8">
          <Link
            to="/"
            className="sp-btn sp-btn-ghost text-xs"
            data-testid="about-back"
          >
            <ArrowLeft className="w-3.5 h-3.5" /> Back
          </Link>
          <Link to="/" aria-label="Foundry home" data-testid="about-brand">
            <Logo variant="wordmark" className="h-7 w-auto" />
          </Link>
        </div>

        {loading ? (
          <div className="flex items-center gap-2 sp-subtle text-sm">
            <Loader2 className="w-4 h-4 animate-spin" /> Loading overview…
          </div>
        ) : (
          <article className="sp-prose" data-testid="about-content">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{md}</ReactMarkdown>
          </article>
        )}
      </div>
    </div>
  );
}
