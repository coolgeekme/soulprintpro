import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import api from "../lib/api";
import { ArrowLeft, Loader2, Download } from "lucide-react";

/**
 * AboutPage — renders /app/memory/ABOUT.md.
 *
 * Same source served at GET /api/about powers both the in-app page (linked
 * from the landing page header and the user menu) and any marketing site
 * surface that wants the canonical copy. Updating the markdown updates
 * both — no redeploy required.
 *
 * Design choice: render server-side markdown as-is (no remark plugins, no
 * sanitization beyond react-markdown defaults) — the source is trusted
 * and authored by us, not user-submitted.
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

  const downloadMd = () => {
    const blob = new Blob([md], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "soulprint-engine-overview.md";
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="min-h-screen" data-testid="about-page">
      <div className="max-w-3xl mx-auto px-6 py-10">
        <div className="flex items-center justify-between mb-8">
          <Link
            to="/"
            className="sp-btn sp-btn-ghost text-xs"
            data-testid="about-back"
          >
            <ArrowLeft className="w-3.5 h-3.5" /> Back
          </Link>
          {md && (
            <button
              type="button"
              onClick={downloadMd}
              className="sp-btn sp-btn-ghost text-xs"
              title="Download this overview as Markdown — useful for marketing material"
              data-testid="about-download"
            >
              <Download className="w-3.5 h-3.5" /> Download as .md
            </button>
          )}
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
