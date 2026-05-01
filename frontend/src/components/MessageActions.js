import React, { useState } from "react";
import { Copy, ThumbsUp, ThumbsDown, Check } from "lucide-react";
import { toast } from "sonner";
import api from "../lib/api";

/**
 * MessageActions — hover-revealed row of buttons under every message
 * (user OR agent). Offers:
 *   - Copy    → writes the message body (and any attachment URLs) to the
 *               clipboard. Fires a subtle "copied" acknowledgement.
 *   - 👍 / 👎 → per-user thumbs; clicking the same thumb toggles it off,
 *               clicking the opposite thumb flips. The tally + your vote
 *               are persisted via POST /messages/{id}/feedback.
 *
 * The component is intentionally cheap — it uses local state for the
 * optimistic flip, then reconciles with the server's authoritative
 * counts. No extra fetches are made on mount.
 */
export function MessageActions({ convId, message, alignRight = false }) {
  const [copied, setCopied] = useState(false);
  const [mine, setMine] = useState(message.my_feedback || "");
  const [ups, setUps] = useState(message.up_count || 0);
  const [downs, setDowns] = useState(message.down_count || 0);

  const copyText = async () => {
    // Build a clean copyable payload: the text plus any attachment
    // original names on their own lines. Markdown-ish so it pastes well.
    const parts = [message.text || ""];
    if (Array.isArray(message.attachments) && message.attachments.length) {
      for (const a of message.attachments) {
        parts.push(`[${a.original_name || a.filename || "file"}]`);
      }
    }
    const payload = parts.filter(Boolean).join("\n").trim();
    if (!payload) return;
    try {
      await navigator.clipboard.writeText(payload);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      // Fallback for non-HTTPS previews where Clipboard API is blocked
      const ta = document.createElement("textarea");
      ta.value = payload;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); setCopied(true); setTimeout(() => setCopied(false), 1400); }
      catch { toast.error("Copy blocked by browser"); }
      finally { document.body.removeChild(ta); }
    }
  };

  const vote = async (val) => {
    // Optimistic: flip client-side immediately, rollback on error.
    const prior = { mine, ups, downs };
    const next = mine === val ? "" : val; // clicking the same vote clears
    let nextUps = ups, nextDowns = downs;
    if (mine === "up") nextUps -= 1;
    if (mine === "down") nextDowns -= 1;
    if (next === "up") nextUps += 1;
    if (next === "down") nextDowns += 1;
    setMine(next); setUps(nextUps); setDowns(nextDowns);
    try {
      const { data } = await api.post(
        `/conversations/${convId}/messages/${message.id}/feedback`,
        { value: next },
      );
      // Reconcile with authoritative counts from the server.
      setMine(data.my_feedback || "");
      setUps(data.up_count || 0);
      setDowns(data.down_count || 0);
    } catch (err) {
      setMine(prior.mine); setUps(prior.ups); setDowns(prior.downs);
      toast.error(err?.response?.data?.detail || "Couldn't save feedback");
    }
  };

  const baseBtn =
    "inline-flex items-center gap-1 text-[11px] px-1.5 py-1 rounded-md hover:bg-white/10 transition";
  const activeUp = mine === "up" ? "text-emerald-300 bg-emerald-400/10" : "sp-subtle";
  const activeDown = mine === "down" ? "text-red-300 bg-red-400/10" : "sp-subtle";

  return (
    <div
      className={`flex items-center gap-0.5 mt-1 opacity-0 group-hover/msg:opacity-100 focus-within:opacity-100 transition ${alignRight ? "justify-end" : ""}`}
      data-testid={`message-actions-${message.id}`}
    >
      <button
        type="button"
        onClick={copyText}
        className={`${baseBtn} sp-subtle`}
        title="Copy message"
        data-testid={`msg-copy-${message.id}`}
      >
        {copied ? <Check className="w-3 h-3 text-emerald-300" /> : <Copy className="w-3 h-3" />}
        {copied ? "Copied" : "Copy"}
      </button>
      <button
        type="button"
        onClick={() => vote("up")}
        className={`${baseBtn} ${activeUp}`}
        title="Good response"
        data-testid={`msg-thumbs-up-${message.id}`}
      >
        <ThumbsUp className="w-3 h-3" />
        {ups > 0 ? ups : ""}
      </button>
      <button
        type="button"
        onClick={() => vote("down")}
        className={`${baseBtn} ${activeDown}`}
        title="Needs improvement"
        data-testid={`msg-thumbs-down-${message.id}`}
      >
        <ThumbsDown className="w-3 h-3" />
        {downs > 0 ? downs : ""}
      </button>
    </div>
  );
}

export default MessageActions;
