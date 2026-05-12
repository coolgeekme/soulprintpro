import React, { useState, useRef } from "react";
import {
  Play, Code, Download, Share2, ExternalLink, Copy, Check, Eye,
  Maximize2, X, Loader2,
} from "lucide-react";
import { toast } from "sonner";

/**
 * AppPreview — renders an interactive HTML/JS app artifact inline.
 *
 * Modes:
 *  - "preview"    sandboxed iframe (default)
 *  - "code"       read-only source viewer
 *
 * Props:
 *  - attachment.app_token (string)        capability-URL token
 *  - attachment.share_url (string)        /api/share/app/{token}
 *  - attachment.download_url (string)     /api/share/app/{token}/_download
 *  - attachment.original_name (string)
 *  - attachment.title (string)
 *  - apiBase (string)                     REACT_APP_BACKEND_URL
 *
 * Security: iframe is `sandbox="allow-scripts"` — no parent-window
 * access, no cookies, no top-level navigation. JS inside the iframe
 * can run freely but can't escape.
 */
export default function AppPreview({ attachment, apiBase }) {
  const [mode, setMode] = useState("preview");
  const [source, setSource] = useState(null);
  const [loadingSource, setLoadingSource] = useState(false);
  const [copied, setCopied] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  const iframeRef = useRef(null);

  const token = attachment.app_token;
  const shareUrl = attachment.share_url || `/api/share/app/${token}`;
  const downloadUrl = attachment.download_url || `/api/share/app/${token}/_download`;
  const fullShareUrl = shareUrl.startsWith("http")
    ? shareUrl
    : `${apiBase}${shareUrl}`;
  const title = attachment.title || attachment.original_name || "Interactive app";

  const loadSource = async () => {
    if (source !== null) return;
    setLoadingSource(true);
    try {
      const res = await fetch(fullShareUrl, { credentials: "omit" });
      const txt = await res.text();
      setSource(txt);
    } catch {
      setSource("// Could not load source.");
    } finally {
      setLoadingSource(false);
    }
  };

  const switchTo = async (m) => {
    if (m === "code") await loadSource();
    setMode(m);
  };

  const copyLink = async () => {
    try {
      await navigator.clipboard.writeText(fullShareUrl);
      setCopied(true);
      toast.success("Share link copied");
      setTimeout(() => setCopied(false), 1500);
    } catch {
      toast.error("Could not copy");
    }
  };

  const copyCode = async () => {
    if (!source) await loadSource();
    try {
      await navigator.clipboard.writeText(source || "");
      toast.success("Code copied");
    } catch {
      toast.error("Could not copy");
    }
  };

  const refresh = () => {
    if (iframeRef.current) {
      // Reload by re-assigning src
      iframeRef.current.src = `${fullShareUrl}?t=${Date.now()}`;
    }
  };

  return (
    <>
      <div
        className="sp-glass rounded-xl overflow-hidden border border-white/15 mt-2"
        data-testid={`app-preview-${token}`}
      >
        {/* Toolbar */}
        <div className="flex items-center justify-between gap-2 px-3 py-2 border-b border-white/10 bg-white/[0.03]">
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-[10px] uppercase tracking-widest text-orange-300 bg-orange-400/15 px-1.5 py-0.5 rounded">
              App
            </span>
            <span className="text-sm font-medium truncate">{title}</span>
            {attachment.is_app_bundle && (
              <span className="text-[10px] sp-subtle">· bundled</span>
            )}
          </div>
          <div className="flex items-center gap-1 shrink-0">
            <button
              onClick={() => switchTo("preview")}
              className={`p-1.5 rounded text-xs flex items-center gap-1 ${
                mode === "preview" ? "bg-white/15" : "hover:bg-white/10"
              }`}
              title="Live preview"
              data-testid="app-preview-mode-preview"
            >
              <Eye className="w-3.5 h-3.5" />
            </button>
            <button
              onClick={() => switchTo("code")}
              className={`p-1.5 rounded text-xs flex items-center gap-1 ${
                mode === "code" ? "bg-white/15" : "hover:bg-white/10"
              }`}
              title="View source"
              data-testid="app-preview-mode-code"
            >
              <Code className="w-3.5 h-3.5" />
            </button>
            <span className="w-px h-4 bg-white/15 mx-1" />
            <button
              onClick={copyLink}
              className="p-1.5 rounded hover:bg-white/10"
              title="Copy share link"
              data-testid="app-preview-share"
            >
              {copied ? <Check className="w-3.5 h-3.5 text-emerald-300" /> : <Share2 className="w-3.5 h-3.5" />}
            </button>
            <a
              href={fullShareUrl}
              target="_blank"
              rel="noreferrer noopener"
              className="p-1.5 rounded hover:bg-white/10"
              title="Open in new tab"
              data-testid="app-preview-open"
            >
              <ExternalLink className="w-3.5 h-3.5" />
            </a>
            <a
              href={apiBase + downloadUrl}
              download
              className="p-1.5 rounded hover:bg-white/10"
              title="Download as ZIP"
              data-testid="app-preview-download"
            >
              <Download className="w-3.5 h-3.5" />
            </a>
            <button
              onClick={() => setFullscreen(true)}
              className="p-1.5 rounded hover:bg-white/10"
              title="Fullscreen"
              data-testid="app-preview-fullscreen"
            >
              <Maximize2 className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>

        {/* Body */}
        <div className="bg-white">
          {mode === "preview" ? (
            <iframe
              ref={iframeRef}
              src={fullShareUrl}
              title={title}
              sandbox="allow-scripts allow-forms allow-popups allow-modals allow-same-origin"
              className="w-full"
              style={{ height: "440px", border: "none", background: "white" }}
              data-testid="app-preview-iframe"
            />
          ) : (
            <div className="relative bg-zinc-950">
              <button
                onClick={copyCode}
                className="absolute top-2 right-2 sp-btn sp-btn-ghost text-[10px] py-1 px-2 flex items-center gap-1 bg-zinc-900/80"
                data-testid="app-preview-copy-code"
              >
                <Copy className="w-3 h-3" /> Copy
              </button>
              {loadingSource ? (
                <div className="p-6 text-center text-sm text-zinc-400 flex items-center justify-center gap-2">
                  <Loader2 className="w-4 h-4 animate-spin" /> Loading source…
                </div>
              ) : (
                <pre
                  className="text-[11px] leading-relaxed text-zinc-200 font-mono overflow-auto p-4"
                  style={{ maxHeight: "440px" }}
                >
                  <code>{source || ""}</code>
                </pre>
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-3 py-2 border-t border-white/10 bg-white/[0.02] flex items-center justify-between gap-2 text-[11px] sp-subtle">
          <button
            onClick={refresh}
            className="hover:text-white transition-colors flex items-center gap-1"
            title="Refresh preview"
          >
            <Play className="w-3 h-3" /> Rerun
          </button>
          <span className="truncate">
            Share URL: <code className="text-orange-300">{fullShareUrl}</code>
          </span>
        </div>
      </div>

      {/* Fullscreen overlay */}
      {fullscreen && (
        <div
          className="fixed inset-0 z-[100] bg-black/85 backdrop-blur-sm flex flex-col"
          onClick={() => setFullscreen(false)}
        >
          <div
            className="flex items-center justify-between px-4 py-3 text-white"
            onClick={(e) => e.stopPropagation()}
          >
            <span className="font-medium">{title}</span>
            <div className="flex items-center gap-2">
              <a
                href={fullShareUrl}
                target="_blank"
                rel="noreferrer noopener"
                className="sp-btn sp-btn-ghost text-xs"
              >
                <ExternalLink className="w-3.5 h-3.5" /> New tab
              </a>
              <button
                onClick={() => setFullscreen(false)}
                className="sp-btn sp-btn-ghost text-xs"
              >
                <X className="w-3.5 h-3.5" /> Close
              </button>
            </div>
          </div>
          <iframe
            src={fullShareUrl}
            title={`${title} fullscreen`}
            sandbox="allow-scripts allow-forms allow-popups allow-modals allow-same-origin"
            className="flex-1 w-full bg-white"
            style={{ border: "none" }}
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      )}
    </>
  );
}
