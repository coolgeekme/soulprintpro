import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * MessageMarkdown — render agent reply / message bodies as Markdown.
 *
 * Why this component exists: agent replies routinely contain
 * [label](url) hyperlinks and bare URLs (Google Drive folders, asset
 * library, web-search citations). Before this, every message rendered
 * via `<div className="whitespace-pre-wrap">{m.text}</div>` so the
 * markdown leaked through as plain text and users had to copy/paste
 * URLs out by hand.
 *
 * Behaviour:
 *   • [label](url) → clickable hyperlink, opens in a NEW TAB with
 *     `rel="noopener noreferrer"` (security + perf — the link target
 *     can't tamper with `window.opener`).
 *   • Bare http(s)://… URLs in body text → also auto-linked via the
 *     `urlTransform` + `linkify` behaviour of remark-gfm.
 *   • `**bold**`, `*italic*`, lists, headings, fenced code, tables —
 *     all rendered. Inline code keeps the existing chip styling so
 *     filename references (e.g. `brand_kit.pdf`) stay readable.
 *
 * Safety:
 *   • react-markdown disables raw HTML by default (no rehype-raw),
 *     so an agent reply can't inject `<script>` / arbitrary HTML.
 *   • `urlTransform` rejects `javascript:` URLs.
 *
 * Style notes:
 *   • The classNames here mirror what the surrounding chat bubble
 *     used so we don't accidentally change text rhythm. `prose` is
 *     deliberately NOT used — the surrounding bubble has its own
 *     leading/sizing that we want to keep.
 */
const SAFE_URL = /^(https?:|mailto:|tel:|\/|#)/i;

const components = {
  a: ({ href, children, ...rest }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="underline underline-offset-2 decoration-current hover:decoration-2 break-words"
      style={{ color: "var(--accent, #F64000)" }}
      {...rest}
    >
      {children}
    </a>
  ),
  // Inline code: kept compact so it doesn't break the reading line.
  code: ({ inline, className, children, ...rest }) => {
    if (inline) {
      return (
        <code
          className="px-1 py-0.5 rounded text-[0.9em]"
          style={{
            background: "color-mix(in srgb, var(--fg) 8%, transparent)",
            color: "var(--fg)",
            fontFamily: '"JetBrains Mono", "Fira Code", ui-monospace, monospace',
          }}
          {...rest}
        >
          {children}
        </code>
      );
    }
    // Block code — same family but with a soft border so multi-line
    // snippets sit clearly inside the bubble.
    return (
      <pre
        className="px-3 py-2 rounded-lg my-2 overflow-x-auto text-[13px] leading-[1.5]"
        style={{
          background: "color-mix(in srgb, var(--fg) 8%, transparent)",
          border: "1px solid var(--border-soft, rgba(0,0,0,0.08))",
        }}
      >
        <code {...rest}>{children}</code>
      </pre>
    );
  },
  // Paragraphs and lists — preserve the original 16px / 1.6 leading
  // from the chat bubble. `mb-2 last:mb-0` keeps paragraph spacing
  // even when ReactMarkdown wraps a single line in <p>.
  p: ({ children }) => <p className="mb-2 last:mb-0">{children}</p>,
  ul: ({ children }) => <ul className="list-disc pl-5 mb-2 last:mb-0 space-y-1">{children}</ul>,
  ol: ({ children }) => <ol className="list-decimal pl-5 mb-2 last:mb-0 space-y-1">{children}</ol>,
  li: ({ children }) => <li className="leading-[1.55]">{children}</li>,
  h1: ({ children }) => <h3 className="text-lg font-semibold mt-2 mb-1">{children}</h3>,
  h2: ({ children }) => <h3 className="text-base font-semibold mt-2 mb-1">{children}</h3>,
  h3: ({ children }) => <h4 className="text-[15px] font-semibold mt-2 mb-1">{children}</h4>,
  hr: () => <hr className="my-3 opacity-30" />,
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 pl-3 my-2 opacity-90" style={{ borderColor: "var(--accent, #F64000)" }}>
      {children}
    </blockquote>
  ),
  table: ({ children }) => (
    <div className="overflow-x-auto my-2">
      <table className="text-[14px] border-collapse">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th
      className="px-2 py-1 text-left font-semibold border"
      style={{ borderColor: "var(--border-soft, rgba(0,0,0,0.08))" }}
    >
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td
      className="px-2 py-1 border align-top"
      style={{ borderColor: "var(--border-soft, rgba(0,0,0,0.08))" }}
    >
      {children}
    </td>
  ),
};

export default function MessageMarkdown({
  children = "",
  className = "",
  testId,
}) {
  if (!children) return null;
  return (
    <div
      className={`mm-body text-[16px] leading-[1.6] break-words ${className}`}
      data-testid={testId}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        urlTransform={(url) => (SAFE_URL.test(url) ? url : "")}
        components={components}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
