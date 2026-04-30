import React, { useEffect, useState } from "react";
import api from "../lib/api";
import { ExternalLink, Globe } from "lucide-react";

/**
 * Lightweight in-memory cache so we don't refetch on every render. The
 * backend additionally caches in MongoDB for 24h.
 */
const memCache = new Map();

const hostnameOf = (url) => {
  try { return new URL(url).hostname.replace(/^www\./, ""); }
  catch { return ""; }
};

/**
 * LinkPreviewCard — Slack/Discord-style rich preview for a cited web source.
 * Fetches `/api/unfurl?url=<url>` once on mount, then renders a card with:
 *   • favicon + site name + url
 *   • title (1 line)
 *   • description (2 lines, clamped)
 *   • thumbnail on the right (only if the page has og:image)
 *
 * Falls back gracefully (just a styled `<a>` with hostname) on fetch failure.
 */
export default function LinkPreviewCard({ source }) {
  const url = source?.url;
  const fallbackTitle = source?.title || url;
  const [data, setData] = useState(memCache.get(url) || null);
  const [imgFailed, setImgFailed] = useState(false);
  const [favFailed, setFavFailed] = useState(false);

  useEffect(() => {
    if (!url || data) return;
    let cancelled = false;
    api.get("/unfurl", { params: { url } }).then(({ data: d }) => {
      if (cancelled) return;
      memCache.set(url, d);
      setData(d);
    }).catch(() => {/* leave data null — fallback render will kick in */});
    return () => { cancelled = true; };
  }, [url, data]);

  if (!url) return null;
  const host = (data?.host || hostnameOf(url) || "").toLowerCase();
  const title = data?.title || fallbackTitle;
  const description = data?.description || "";
  const image = !imgFailed && data?.image ? data.image : "";
  const favicon = !favFailed && data?.favicon ? data.favicon : "";
  const siteName = data?.site_name || host;

  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      className="group block rounded-xl overflow-hidden border transition hover:border-white/30"
      style={{ borderColor: "var(--border-soft)", background: "var(--card-soft, rgba(255,255,255,0.02))" }}
      data-testid="link-preview-card"
    >
      <div className="flex items-stretch">
        <div className="flex-1 min-w-0 p-3 sm:p-3.5">
          <div className="flex items-center gap-1.5 mb-1.5 text-[11px] sp-subtle">
            {favicon ? (
              <img
                src={favicon}
                alt=""
                className="w-3.5 h-3.5 rounded-sm object-contain"
                onError={() => setFavFailed(true)}
              />
            ) : (
              <Globe className="w-3 h-3 opacity-70" />
            )}
            <span className="truncate">{siteName || host || "Link"}</span>
            <ExternalLink className="w-3 h-3 ml-auto opacity-50 group-hover:opacity-90 transition" />
          </div>
          <div
            className="text-sm font-medium leading-snug line-clamp-1 group-hover:underline decoration-white/30"
            style={{ color: "var(--fg)" }}
          >
            {title}
          </div>
          {description && (
            <div className="text-[12px] sp-subtle mt-1 line-clamp-2 leading-snug">
              {description}
            </div>
          )}
          {(source?.source || source?.age) && (
            <div className="text-[10px] sp-subtle mt-2 uppercase tracking-widest">
              {[source?.source, source?.age].filter(Boolean).join(" · ")}
            </div>
          )}
        </div>
        {image && (
          <div className="w-20 sm:w-28 shrink-0 relative bg-black/20">
            <img
              src={image}
              alt=""
              className="absolute inset-0 w-full h-full object-cover"
              loading="lazy"
              referrerPolicy="no-referrer"
              onError={() => setImgFailed(true)}
            />
          </div>
        )}
      </div>
    </a>
  );
}
