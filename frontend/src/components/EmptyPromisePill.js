import { useState } from "react";
import { Wand2, Loader2 } from "lucide-react";
import { toast } from "sonner";
import api from "../lib/api";

/**
 * EmptyPromisePill — small inline rescue UI rendered under any agent
 * message that the backend flagged with `empty_promise: true`.
 *
 * The flag is set by `/api/conversations/{id}/messages` (POST) when an
 * agent reply contains "I'll send X next" / "stand by" / etc but didn't
 * actually deliver an artifact, AND the auto-retry with
 * force_delivery=True also failed. So this pill represents the THIRD
 * line of defense:
 *   1. system-prompt directive forbidding empty promises
 *   2. server-side auto-retry on detection
 *   3. user-triggered Generate now (this component)
 *
 * One click runs POST /messages/{id}/generate-now → server replays the
 * original user prompt with force_delivery=True → posts a new agent
 * message with the deliverable inline. We append the new message
 * locally (the parent's onSuccess does the splice) so the user sees
 * the rescue land without a refresh.
 */
export default function EmptyPromisePill({ convId, message, onSuccess }) {
  const [busy, setBusy] = useState(false);

  const onClick = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const { data } = await api.post(
        `/conversations/${convId}/messages/${message.id}/generate-now`,
      );
      if (data?.delivered) {
        toast.success("Generated.");
      } else {
        toast.message(
          "We re-ran the agent but it still didn't produce the deliverable. Try rephrasing your ask with explicit details.",
          { duration: 6000 },
        );
      }
      onSuccess?.(data?.message);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't regenerate — please try rephrasing your ask.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="mt-3 flex flex-wrap items-center gap-2 rounded-xl border border-orange-500/30 bg-orange-500/8 px-3 py-2"
      data-testid={`empty-promise-pill-${message.id}`}
    >
      <span className="text-xs text-orange-200/90 leading-relaxed flex-1 min-w-[200px]">
        Looks like the agent promised something but didn't actually attach it. Click to regenerate now.
      </span>
      <button
        type="button"
        onClick={onClick}
        disabled={busy}
        className="sp-btn sp-btn-primary text-xs disabled:opacity-60"
        data-testid={`empty-promise-generate-${message.id}`}
      >
        {busy
          ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Generating…</>
          : <><Wand2 className="w-3.5 h-3.5" /> Generate now</>}
      </button>
    </div>
  );
}
