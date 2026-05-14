import React, { useState } from "react";
import { Loader2, Check, X, RotateCcw, Wand2 } from "lucide-react";
import { toast } from "sonner";
import api from "../lib/api";

/**
 * MissionGoalDrafter — inline panel that turns a rough idea into a
 * polished mission goal following Foundry's mission best practices.
 *
 * Flow:
 *   1. User clicks the "Help me write this" button next to the textarea.
 *   2. Panel slides open with three fields:
 *        • Goal (rough — what you want done)
 *        • Deliverable (optional — the file/format you want back)
 *        • Success looks like (optional — what 'done' means)
 *   3. Generate → POST /api/missions/draft-goal — gets a polished goal back.
 *   4. The polished goal replaces the parent textarea content.
 *   5. From there, a "Refine" affordance lets the user say "make it
 *      shorter" / "focus more on competitive analysis" without re-typing.
 *
 * The drafter LLM enforces Foundry-specific best practices: concrete
 * deliverable with a real filename + extension, 3-5 measurable success
 * criteria, ≥5 source citations for research missions, no .txt outputs,
 * no empty-promise verbs, no vague qualifiers.
 */
export default function MissionGoalDrafter({ currentGoal, onAccept, disabled }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  // Inputs
  const [rough, setRough] = useState("");
  const [deliverable, setDeliverable] = useState("");
  const [success, setSuccess] = useState("");

  // Output
  const [draft, setDraft] = useState("");
  const [draftMeta, setDraftMeta] = useState(null);  // { char_count, char_target }
  const [refineInput, setRefineInput] = useState("");

  const reset = () => {
    setRough(""); setDeliverable(""); setSuccess("");
    setDraft(""); setDraftMeta(null); setRefineInput("");
  };

  const close = () => { setOpen(false); reset(); };

  const generate = async () => {
    if (!rough.trim() || rough.trim().length < 6) {
      toast.error("Tell me what you want done in a sentence or two.");
      return;
    }
    setBusy(true);
    try {
      const { data } = await api.post("/missions/draft-goal", {
        goal: rough.trim(),
        deliverable: deliverable.trim(),
        success_looks_like: success.trim(),
      });
      setDraft(data.goal);
      setDraftMeta({ char_count: data.char_count, char_target: data.char_target });
    } catch (err) {
      const msg = err?.response?.data?.detail || "Couldn't draft a goal — try again.";
      toast.error(msg);
    } finally {
      setBusy(false);
    }
  };

  const refine = async () => {
    if (!refineInput.trim()) {
      toast.error("What should change about the draft?");
      return;
    }
    setBusy(true);
    try {
      const { data } = await api.post("/missions/draft-goal", {
        goal: draft,
        refine_instruction: refineInput.trim(),
      });
      setDraft(data.goal);
      setDraftMeta({ char_count: data.char_count, char_target: data.char_target });
      setRefineInput("");
    } catch (err) {
      const msg = err?.response?.data?.detail || "Couldn't refine the goal — try again.";
      toast.error(msg);
    } finally {
      setBusy(false);
    }
  };

  const accept = () => {
    if (!draft) return;
    onAccept(draft);
    close();
    toast.success("Mission goal updated.");
  };

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => {
          // Seed the rough-goal field with whatever the user already
          // typed in the parent textarea — saves a re-type.
          if (currentGoal?.trim()) setRough(currentGoal.trim());
          setOpen(true);
        }}
        disabled={disabled}
        className="text-[11px] sp-subtle hover:text-orange-300 inline-flex items-center gap-1.5 transition disabled:opacity-50"
        title="Turn a rough idea into a polished mission goal."
        data-testid="mission-drafter-open"
      >
        <Wand2 className="w-3.5 h-3.5" />
        Help me write this
      </button>
    );
  }

  return (
    <div
      className="mt-2 sp-glass-strong rounded-2xl p-4 border border-orange-500/30"
      data-testid="mission-drafter-panel"
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Wand2 className="w-4 h-4 text-orange-300" />
          Mission goal drafter
        </div>
        <button
          type="button"
          onClick={close}
          className="text-white/50 hover:text-white"
          data-testid="mission-drafter-close"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* Stage 1: structured input */}
      {!draft && (
        <div className="space-y-3">
          <Field
            label="What do you want done? (rough is fine)"
            required
            testId="drafter-rough"
          >
            <textarea
              value={rough}
              onChange={(e) => setRough(e.target.value)}
              rows={2}
              disabled={busy}
              placeholder="e.g. research top productivity apps for marketing teams"
              className="w-full sp-glass rounded-xl px-3 py-2 text-sm focus:outline-none resize-none"
              style={{ background: "var(--input-bg)", color: "var(--fg)" }}
              data-testid="drafter-rough-input"
            />
          </Field>
          <Field
            label="Deliverable format (optional)"
            hint="e.g. 'one-page PDF brief', 'CSV with 10 rows', '5 LinkedIn posts'"
            testId="drafter-deliverable"
          >
            <input
              value={deliverable}
              onChange={(e) => setDeliverable(e.target.value)}
              disabled={busy}
              placeholder="leave blank and the drafter picks a sensible default"
              className="w-full sp-glass rounded-xl px-3 py-2 text-sm focus:outline-none"
              style={{ background: "var(--input-bg)", color: "var(--fg)" }}
              data-testid="drafter-deliverable-input"
            />
          </Field>
          <Field
            label="Success looks like… (optional)"
            hint="Concrete criteria: 'pricing for each app', 'cite ≥5 sources', 'include a comparison table'"
            testId="drafter-success"
          >
            <input
              value={success}
              onChange={(e) => setSuccess(e.target.value)}
              disabled={busy}
              placeholder="what 'done' means — agents stop earlier without this"
              className="w-full sp-glass rounded-xl px-3 py-2 text-sm focus:outline-none"
              style={{ background: "var(--input-bg)", color: "var(--fg)" }}
              data-testid="drafter-success-input"
            />
          </Field>
          <div className="flex items-center gap-2 pt-1">
            <button
              type="button"
              onClick={generate}
              disabled={busy || !rough.trim()}
              className="sp-btn sp-btn-primary text-xs inline-flex items-center gap-1.5 disabled:opacity-50"
              data-testid="drafter-generate"
            >
              {busy ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <Wand2 className="w-3.5 h-3.5" />
              )}
              Generate goal
            </button>
            <span className="text-[10px] sp-subtle">
              200-1,200 chars · best-practice template enforced
            </span>
          </div>
        </div>
      )}

      {/* Stage 2: review the draft, refine, accept */}
      {draft && (
        <div className="space-y-3">
          <div className="text-[10px] uppercase tracking-widest sp-subtle flex items-center gap-2">
            Polished mission goal
            {draftMeta && (
              <span className="sp-subtle normal-case tracking-normal">
                · {draftMeta.char_count} chars
              </span>
            )}
          </div>
          <div
            className="rounded-xl px-3 py-3 text-sm whitespace-pre-wrap leading-relaxed"
            style={{ background: "var(--input-bg)", color: "var(--fg)" }}
            data-testid="drafter-output"
          >
            {draft}
          </div>

          <div className="flex flex-wrap items-end gap-2 pt-1">
            <input
              value={refineInput}
              onChange={(e) => setRefineInput(e.target.value)}
              disabled={busy}
              placeholder="Refine: e.g. 'make it shorter' or 'focus on B2B SaaS'"
              className="flex-1 min-w-[12rem] sp-glass rounded-xl px-3 py-2 text-sm focus:outline-none"
              style={{ background: "var(--input-bg)", color: "var(--fg)" }}
              onKeyDown={(e) => {
                if (e.key === "Enter") { e.preventDefault(); refine(); }
              }}
              data-testid="drafter-refine-input"
            />
            <button
              type="button"
              onClick={refine}
              disabled={busy || !refineInput.trim()}
              className="sp-btn sp-btn-ghost text-xs inline-flex items-center gap-1.5 disabled:opacity-50"
              data-testid="drafter-refine"
            >
              {busy ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <RotateCcw className="w-3.5 h-3.5" />
              )}
              Refine
            </button>
            <button
              type="button"
              onClick={accept}
              disabled={busy}
              className="sp-btn sp-btn-primary text-xs inline-flex items-center gap-1.5 disabled:opacity-50"
              data-testid="drafter-accept"
            >
              <Check className="w-3.5 h-3.5" />
              Use this goal
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function Field({ label, hint, required, testId, children }) {
  return (
    <div data-testid={testId}>
      <label className="text-[10px] uppercase tracking-widest sp-subtle mb-1 block">
        {label} {required && <span className="text-orange-300">*</span>}
      </label>
      {children}
      {hint && <div className="text-[10px] sp-subtle mt-1">{hint}</div>}
    </div>
  );
}
