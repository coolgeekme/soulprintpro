import React, { useEffect, useState } from "react";
import api from "../lib/api";
import { ExternalLink, Globe } from "lucide-react";

/**
 * In-memory cache so the same URL doesn't re-fetch on every render. The
 * backend additionally caches in MongoDB for 24h.
 */
const memCache = new Map();

const hostnameOf = (url) => {
  try { return new URL(url).hostname.replace(/^www\./, ""); }
  catch { return ""; }
};

/**
 * LinkPreviewCard — minimal inline citation. One line:
 *   [favicon] Page title  ·  hostname  ↗
 *
 * No thumbnails, no description blocks. Hover underlines the title; click
 * opens the source in a new tab.
 */
export default function LinkPreviewCard({ source }) {
  const url = source?.url;
  const fallbackTitle = source?.title || url;
  const [data, setData] = useState(memCache.get(url) || null);
  const [favFailed, setFavFailed] = useState(false);

  useEffect(() => {
    if (!url || data) return;
    let cancelled = false;
    api.get("/unfurl", { params: { url } })
      .then(({ data: d }) => {
        if (cancelled) return;
        memCache.set(url, d);
        setData(d);
      })
      .catch(() => {/* leave null — fallback render kicks in */});
    return () => { cancelled = true; };
  }, [url, data]);

  if (!url) return null;
  const host = (data?.host || hostnameOf(url) || "").toLowerCase();
  const title = (data?.title || fallbackTitle || "").trim();
  const favicon = !favFailed && data?.favicon ? data.favicon : "";

  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      className="group inline-flex items-center gap-2 text-[12px] leading-snug min-w-0"
      data-testid="link-preview-card"
    >
      {favicon ? (
        <img
          src={favicon}
          alt=""
          className="w-3.5 h-3.5 rounded-sm shrink-0 object-contain"
          onError={() => setFavFailed(true)}
        />
      ) : (
        <Globe className="w-3 h-3 opacity-60 shrink-0" />
      )}
      <span
        className="truncate group-hover:underline decoration-white/30"
        style={{ color: "var(--fg)" }}
      >
        {title}
      </span>
      {host && (
        <span className="sp-subtle shrink-0">· {host}</span>
      )}
      <ExternalLink className="w-3 h-3 opacity-40 group-hover:opacity-90 shrink-0" />
    </a>
  );
}
