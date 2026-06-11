import React, { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { toast } from "sonner";
import {
  X, Loader2, ArrowLeft, ArrowRight, Check, Bot, Send,
  Wand2, BookOpen, Code2, AlertCircle, RotateCcw,
} from "lucide-react";
import api from "../lib/api";
import { AGENT_TEMPLATES } from "./agentTemplates";
import MessageMarkdown from "./MessageMarkdown";

/**
 * CustomAgentWizard — opinionated 4-step builder that hides "system prompt"
 * jargon from non-technical users while still letting power users edit raw
 * prompts via the Advanced mode toggle.
 *
 * Steps:
 *   1. Identity   — name + emoji + accent color (with "Pick suggestion")
 *   2. Role       — title + areas of expertise (free text)
 *   3. Voice      — three plain-English fields: voice, always, never
 *                   "Help me write this" button calls the drafter LLM
 *                   to compose the system prompt from these answers.
 *   4. Preview    — live test chat against the draft system prompt
 *                   so the user can vibe-check the agent before saving.
 *
 * Advanced mode: tabs to a raw system-prompt textarea (the old form),
 * preserved for power users.
 *
 * Backend:
 *   POST /api/orgs/{orgId}/custom-agents/draft-system
 *   POST /api/orgs/{orgId}/custom-agents/preview
 *   POST /api/orgs/{orgId}/custom-agents
 *   PATCH /api/orgs/{orgId}/custom-agents/{aid}
 */

const STEPS = [
  { id: 1, label: "Identity" },
  { id: 2, label: "Role" },
  { id: 3, label: "Voice & rules" },
  { id: 4, label: "Preview" },
];

const EMOJI_SUGGESTIONS = ["🤖", "💡", "🛡️", "🎯", "📊", "💰", "🧭", "📝", "🔬", "⚙️", "🚀", "🎨", "📣", "🧠"];
const ACCENT_PRESETS = [
  "#f97316", "#dc2626", "#8b5cf6", "#10b981",
  "#0ea5e9", "#06b6d4", "#f59e0b", "#64748b",
];

const EMPTY = {
  name: "",
  emoji: "",
  accent: "#f97316",
  role: "",
  expertise: "",
  voice: "",
  always: "",
  never: "",
  // Composed by the drafter:
  system: "",
  description: "",
};

export default function CustomAgentWizard({
  orgId, mode, initial, onSaved, onCancel,
}) {
  // mode: "create" | "edit"
  const isEdit = mode === "edit";
  const [step, setStep] = useState(1);
  const [advanced, setAdvanced] = useState(false);
  const [form, setForm] = useState(() => {
    if (initial) {
      // When editing an existing agent, we don't know the original
      // wizard answers — flip straight to advanced so the user can
      // see and edit the system prompt directly.
      return {
        ...EMPTY,
        name: initial.name || "",
        emoji: initial.emoji || "",
        accent: initial.accent || "#f97316",
        role: initial.role || "",
        system: initial.system || "",
        description: initial.description || "",
      };
    }
    return EMPTY;
  });
  const [drafting, setDrafting] = useState(false);
  const [saving, setSaving] = useState(false);

  // Preview chat state (step 4)
  const [previewMsgs, setPreviewMsgs] = useState([]);
  const [previewInput, setPreviewInput] = useState("");
  const [previewBusy, setPreviewBusy] = useState(false);

  // If editing, default to advanced mode so the user sees the prompt.
  useEffect(() => {
    if (isEdit) setAdvanced(true);
  }, [isEdit]);

  const applyTemplate = (tpl) => {
    setForm({
      ...EMPTY,
      name: tpl.fields.name,
      role: tpl.fields.role,
      expertise: tpl.fields.expertise,
      voice: tpl.fields.voice,
      always: tpl.fields.always,
      never: tpl.fields.never,
      emoji: tpl.emoji,
      accent: tpl.accent,
    });
    setStep(1);
    toast.success(`${tpl.title} template loaded — tweak any step.`);
  };

  const draftSystem = async ({ refine = false, refineInstruction = "" } = {}) => {
    setDrafting(true);
    try {
      const body = refine
        ? {
            existing_system: form.system,
            refine_instruction: refineInstruction,
          }
        : {
            name: form.name,
            role: form.role,
            expertise: form.expertise,
            voice: form.voice,
            always: form.always,
            never: form.never,
          };
      const { data } = await api.post(
        `/orgs/${orgId}/custom-agents/draft-system`, body
      );
      setForm((f) => ({
        ...f,
        system: data.system,
        description: data.description || f.description,
      }));
      setPreviewMsgs([]);  // reset preview when the prompt changes
      toast.success(refine ? "Prompt refined." : "Prompt drafted.");
      return true;
    } catch (err) {
      const msg = err?.response?.data?.detail || "Couldn't draft the prompt.";
      toast.error(msg);
      return false;
    } finally {
      setDrafting(false);
    }
  };

  const sendPreview = async () => {
    const msg = previewInput.trim();
    if (!msg || !form.system) return;
    setPreviewBusy(true);
    const userTurn = { who: "user", text: msg };
    setPreviewMsgs((prev) => [...prev, userTurn]);
    setPreviewInput("");
    try {
      const { data } = await api.post(
        `/orgs/${orgId}/custom-agents/preview`,
        { system: form.system, user_message: msg }
      );
      setPreviewMsgs((prev) => [...prev, { who: "agent", text: data.reply || "(empty reply)" }]);
    } catch (err) {
      const detail = err?.response?.data?.detail || "Preview failed.";
      setPreviewMsgs((prev) => [...prev, { who: "agent", text: `⚠️ ${detail}`, error: true }]);
    } finally {
      setPreviewBusy(false);
    }
  };

  const save = async () => {
    if (!form.name.trim()) { toast.error("Give your agent a name."); return; }
    if (!form.system.trim() || form.system.trim().length < 50) {
      toast.error("The agent needs a system prompt — go to Voice & rules and generate one.");
      setStep(3);
      return;
    }
    setSaving(true);
    try {
      const body = {
        name: form.name.trim(),
        role: (form.role || form.name).trim(),
        description: (form.description || "").trim(),
        system: form.system.trim(),
        emoji: form.emoji.trim(),
        accent: form.accent || "#f97316",
      };
      if (isEdit) {
        await api.patch(`/orgs/${orgId}/custom-agents/${initial.id}`, body);
        toast.success("Custom agent updated.");
      } else {
        await api.post(`/orgs/${orgId}/custom-agents`, body);
        toast.success("Custom agent created.");
      }
      onSaved?.();
    } catch (err) {
      const msg = err?.response?.data?.detail || "Couldn't save the agent.";
      toast.error(msg);
    } finally {
      setSaving(false);
    }
  };

  const set = (patch) => setForm((f) => ({ ...f, ...patch }));
  const canNext = () => {
    if (advanced) return form.name.trim() && form.system.trim().length >= 50;
    if (step === 1) return !!form.name.trim();
    if (step === 2) return !!form.role.trim() && !!form.expertise.trim();
    if (step === 3) return !!form.system.trim() && form.system.trim().length >= 50;
    return true;
  };

  return createPortal((
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center p-4 bg-black/85 backdrop-blur-md"
      onClick={onCancel}
      data-testid="custom-agent-wizard-overlay"
    >
      <div
        className="rounded-2xl w-full max-w-3xl max-h-[92vh] flex flex-col shadow-2xl border border-white/10"
        onClick={(e) => e.stopPropagation()}
        style={{ background: "var(--bg)", color: "var(--fg)" }}
      >
        {/* Header — title + advanced toggle + close */}
        <div className="px-6 pt-6 pb-4 flex items-center gap-3 border-b border-white/5 shrink-0">
          <div className="flex items-center gap-2 flex-1">
            <Wand2 className="w-5 h-5 text-orange-300" />
            <h3 className="sp-display text-xl">
              {isEdit ? "Edit custom agent" : "Build a custom agent"}
            </h3>
          </div>
          {!isEdit && (
            <button
              type="button"
              onClick={() => setAdvanced((v) => !v)}
              className={`text-[11px] inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border transition ${
                advanced
                  ? "bg-orange-500/15 text-orange-200 border-orange-500/40"
                  : "bg-white/5 sp-subtle border-white/10 hover:bg-white/10"
              }`}
              title="Show raw system-prompt editor"
              data-testid="agent-wizard-advanced-toggle"
            >
              <Code2 className="w-3.5 h-3.5" />
              {advanced ? "Guided mode" : "Advanced"}
            </button>
          )}
          <button
            type="button"
            onClick={onCancel}
            className="p-1.5 rounded-md hover:bg-white/10"
            data-testid="agent-wizard-close"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Templates strip — only on the first run, only in guided mode */}
        {!isEdit && !advanced && step === 1 && !form.role && (
          <div className="px-6 pt-4 shrink-0">
            <div className="flex items-center gap-2 mb-2">
              <BookOpen className="w-3.5 h-3.5 text-orange-300" />
              <span className="text-[10px] uppercase tracking-widest sp-subtle">
                Start from a template
              </span>
            </div>
            <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-2">
              {AGENT_TEMPLATES.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  onClick={() => applyTemplate(t)}
                  className="sp-glass rounded-xl px-3 py-3 text-left hover:bg-white/10 transition"
                  data-testid={`agent-template-${t.id}`}
                >
                  <div className="flex items-center gap-2">
                    <div
                      className="w-7 h-7 rounded-md flex items-center justify-center text-base"
                      style={{ background: `${t.accent}22`, color: t.accent }}
                    >
                      {t.emoji}
                    </div>
                    <div className="text-sm font-medium truncate">{t.title}</div>
                  </div>
                  <div className="text-[11px] sp-subtle mt-1.5 line-clamp-2">{t.tagline}</div>
                </button>
              ))}
            </div>
            <div className="text-[10px] sp-subtle mt-3 mb-1">
              …or build one from scratch below.
            </div>
          </div>
        )}

        {/* Body — either the advanced raw editor or the 4-step wizard */}
        <div className="p-6 space-y-5 flex-1 overflow-y-auto">
          {advanced ? (
            <AdvancedEditor form={form} set={set} />
          ) : (
            <>
              <StepHeader step={step} onJump={setStep} />
              {step === 1 && <StepIdentity form={form} set={set} />}
              {step === 2 && <StepRole form={form} set={set} />}
              {step === 3 && (
                <StepVoice
                  form={form}
                  set={set}
                  drafting={drafting}
                  onDraft={() => draftSystem({ refine: false })}
                  onRefine={(instr) => draftSystem({ refine: true, refineInstruction: instr })}
                />
              )}
              {step === 4 && (
                <StepPreview
                  form={form}
                  previewMsgs={previewMsgs}
                  previewInput={previewInput}
                  setPreviewInput={setPreviewInput}
                  previewBusy={previewBusy}
                  onSend={sendPreview}
                  onBackToVoice={() => setStep(3)}
                />
              )}
            </>
          )}
        </div>

        {/* Footer — nav buttons (sticky bottom, never scrolls out of reach) */}
        <div className="px-6 py-4 border-t border-white/5 flex items-center gap-3 shrink-0">
          {advanced ? (
            <>
              <div className="flex-1 text-[11px] sp-subtle">
                Power-user mode — system prompt is edited directly.
              </div>
              <button
                type="button"
                onClick={onCancel}
                className="sp-btn sp-btn-ghost text-sm"
                disabled={saving}
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={save}
                disabled={saving || !canNext()}
                className="sp-btn sp-btn-primary text-sm inline-flex items-center gap-1.5 disabled:opacity-50"
                data-testid="agent-wizard-save"
              >
                {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />}
                {isEdit ? "Save changes" : "Create agent"}
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                onClick={() => setStep((s) => Math.max(1, s - 1))}
                disabled={step === 1 || saving}
                className="sp-btn sp-btn-ghost text-sm inline-flex items-center gap-1.5 disabled:opacity-40"
              >
                <ArrowLeft className="w-3.5 h-3.5" /> Back
              </button>
              <div className="flex-1 text-center text-[11px] sp-subtle">
                Step {step} of {STEPS.length}
              </div>
              {step < STEPS.length ? (
                <button
                  type="button"
                  onClick={() => setStep((s) => Math.min(STEPS.length, s + 1))}
                  disabled={!canNext() || saving}
                  className="sp-btn sp-btn-primary text-sm inline-flex items-center gap-1.5 disabled:opacity-50"
                  data-testid="agent-wizard-next"
                >
                  Next <ArrowRight className="w-3.5 h-3.5" />
                </button>
              ) : (
                <button
                  type="button"
                  onClick={save}
                  disabled={saving || !canNext()}
                  className="sp-btn sp-btn-primary text-sm inline-flex items-center gap-1.5 disabled:opacity-50"
                  data-testid="agent-wizard-save"
                >
                  {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />}
                  {isEdit ? "Save changes" : "Create agent"}
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  ), document.body);
}

function StepHeader({ step, onJump }) {
  return (
    <div className="flex items-center gap-1.5 mb-1" data-testid="agent-wizard-steps">
      {STEPS.map((s, i) => {
        const done = step > s.id;
        const active = step === s.id;
        return (
          <React.Fragment key={s.id}>
            <button
              type="button"
              onClick={() => done && onJump(s.id)}
              disabled={!done}
              className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] transition ${
                active
                  ? "bg-orange-500/15 text-orange-200 border border-orange-500/40"
                  : done
                    ? "sp-subtle border border-white/10 hover:bg-white/10"
                    : "sp-subtle border border-white/5 opacity-60"
              }`}
            >
              <span className={`w-4 h-4 rounded-full flex items-center justify-center text-[10px] ${
                done ? "bg-orange-400/30 text-orange-100" : "bg-white/10"
              }`}>
                {done ? <Check className="w-2.5 h-2.5" /> : s.id}
              </span>
              {s.label}
            </button>
            {i < STEPS.length - 1 && <div className="flex-1 h-px bg-white/10" />}
          </React.Fragment>
        );
      })}
    </div>
  );
}

function StepIdentity({ form, set }) {
  return (
    <div className="space-y-4" data-testid="agent-wizard-step-identity">
      <Hint>
        <strong>Identity</strong> is what teammates see in the agent picker.
        Pick a name that describes the role, not a person.
      </Hint>
      <Field label="Agent name" required hint="Shown everywhere this agent speaks.">
        <input
          type="text"
          value={form.name}
          onChange={(e) => set({ name: e.target.value })}
          placeholder="e.g. Pricing Strategist, Snarky CFO, Brand Guardian"
          maxLength={80}
          className="sp-input w-full"
          data-testid="agent-wizard-name"
          autoFocus
        />
      </Field>
      <Field label="Emoji" hint="One emoji that captures the vibe.">
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={form.emoji}
            onChange={(e) => set({ emoji: e.target.value })}
            placeholder="🤖"
            maxLength={4}
            className="sp-input w-20 text-center text-2xl"
            data-testid="agent-wizard-emoji"
          />
          <div className="flex flex-wrap gap-1">
            {EMOJI_SUGGESTIONS.map((e) => (
              <button
                key={e}
                type="button"
                onClick={() => set({ emoji: e })}
                className={`w-7 h-7 rounded-md text-base flex items-center justify-center transition ${
                  form.emoji === e ? "bg-orange-500/20" : "hover:bg-white/10"
                }`}
              >
                {e}
              </button>
            ))}
          </div>
        </div>
      </Field>
      <Field label="Accent color" hint="Tints the agent's avatar and badge.">
        <div className="flex flex-wrap gap-2 items-center">
          {ACCENT_PRESETS.map((c) => (
            <button
              key={c}
              type="button"
              onClick={() => set({ accent: c })}
              className={`w-7 h-7 rounded-full border-2 transition ${
                form.accent === c ? "border-white scale-110" : "border-white/20"
              }`}
              style={{ background: c }}
              title={c}
            />
          ))}
          <input
            type="color"
            value={form.accent}
            onChange={(e) => set({ accent: e.target.value })}
            className="h-7 w-12 rounded cursor-pointer bg-transparent"
            data-testid="agent-wizard-accent"
          />
        </div>
      </Field>

      {/* Live identity preview */}
      <div className="sp-glass rounded-xl p-3 flex items-center gap-3" data-testid="agent-wizard-identity-preview">
        <div
          className="w-10 h-10 rounded-lg flex items-center justify-center text-lg shrink-0"
          style={{ background: `${form.accent}22`, color: form.accent }}
        >
          {form.emoji || <Bot className="w-5 h-5" />}
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium truncate">{form.name || "(unnamed agent)"}</div>
          <div className="text-[11px] sp-subtle">This is how teammates will see it.</div>
        </div>
      </div>
    </div>
  );
}

function StepRole({ form, set }) {
  return (
    <div className="space-y-4" data-testid="agent-wizard-step-role">
      <Hint>
        <strong>Role</strong> tells the agent what kind of work it does
        and which expertise to lean on. Be specific — "senior B2B SaaS
        pricing lead" produces sharper answers than "marketer".
      </Hint>
      <Field label="Role / title" required hint="The job description in one line.">
        <input
          type="text"
          value={form.role}
          onChange={(e) => set({ role: e.target.value })}
          placeholder="e.g. Senior B2B SaaS pricing & packaging lead"
          maxLength={120}
          className="sp-input w-full"
          data-testid="agent-wizard-role"
        />
      </Field>
      <Field
        label="Areas of expertise"
        required
        hint="Comma-separated list of frameworks, domains, or specifics. The more concrete, the better."
      >
        <textarea
          value={form.expertise}
          onChange={(e) => set({ expertise: e.target.value })}
          placeholder="e.g. Van Westendorp PSM, gross-margin math, competitive teardown, packaging tiers, willingness-to-pay research"
          rows={3}
          maxLength={1000}
          className="sp-input w-full resize-none"
          data-testid="agent-wizard-expertise"
        />
      </Field>
    </div>
  );
}

function StepVoice({ form, set, drafting, onDraft, onRefine }) {
  const [refineInput, setRefineInput] = useState("");
  const hasDraft = form.system && form.system.length > 50;

  return (
    <div className="space-y-4" data-testid="agent-wizard-step-voice">
      <Hint>
        <strong>Voice & rules</strong> shape how the agent sounds and what
        it refuses to do. Answer in plain English — Foundry composes the
        polished system prompt for you.
      </Hint>

      <Field
        label="How should this agent talk?"
        hint="Tone, vocabulary, pacing. Specific words help: 'crisp, no fluff, sometimes blunt' beats 'professional'."
      >
        <textarea
          value={form.voice}
          onChange={(e) => set({ voice: e.target.value })}
          placeholder="e.g. Dry, sarcastic, sometimes blunt. Names specific competitor pricing mid-sentence. Never uses startup jargon."
          rows={3}
          maxLength={600}
          className="sp-input w-full resize-none"
          data-testid="agent-wizard-voice"
        />
      </Field>

      <Field
        label="What should it ALWAYS do?"
        hint="One rule per line. Imperative voice."
      >
        <textarea
          value={form.always}
          onChange={(e) => set({ always: e.target.value })}
          placeholder={"e.g.\nCite at least one competitor's current pricing\nAsk for gross margin before recommending a tier\nFlag plans that lose money on heavy users"}
          rows={4}
          maxLength={800}
          className="sp-input w-full resize-none"
          data-testid="agent-wizard-always"
        />
      </Field>

      <Field
        label="What should it NEVER do?"
        hint="The hard guardrails. The agent will refuse anything matching these."
      >
        <textarea
          value={form.never}
          onChange={(e) => set({ never: e.target.value })}
          placeholder={"e.g.\nRecommend a price without unit-economics evidence\nUse the word 'value-based' without defining value\nSuggest free trials without an activation metric"}
          rows={4}
          maxLength={800}
          className="sp-input w-full resize-none"
          data-testid="agent-wizard-never"
        />
      </Field>

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={onDraft}
          disabled={drafting}
          className="sp-btn sp-btn-primary text-sm inline-flex items-center gap-1.5 disabled:opacity-50"
          data-testid="agent-wizard-draft"
        >
          {drafting ? (
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
          ) : (
            <Wand2 className="w-3.5 h-3.5" />
          )}
          {hasDraft ? "Re-generate prompt" : "Generate the agent prompt"}
        </button>
        <span className="text-[10px] sp-subtle">
          Composes a production-grade system prompt from your answers.
        </span>
      </div>

      {hasDraft && (
        <div className="sp-glass rounded-xl p-3 space-y-3" data-testid="agent-wizard-draft-preview">
          <div className="text-[10px] uppercase tracking-widest sp-subtle flex items-center gap-2">
            Composed prompt
            <span className="normal-case tracking-normal">· {form.system.length} chars</span>
          </div>
          <div className="text-xs leading-relaxed font-mono max-h-48 overflow-y-auto whitespace-pre-wrap">
            {form.system}
          </div>
          {form.description && (
            <div className="text-[11px] sp-subtle border-t border-white/5 pt-2">
              <span className="text-orange-300">Summary:</span> {form.description}
            </div>
          )}

          <div className="flex flex-wrap items-end gap-2 pt-1 border-t border-white/5">
            <input
              value={refineInput}
              onChange={(e) => setRefineInput(e.target.value)}
              disabled={drafting}
              placeholder="Refine: e.g. 'make it more concise' or 'add a sense of humor'"
              className="flex-1 min-w-[14rem] sp-input text-xs"
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  if (refineInput.trim()) {
                    onRefine(refineInput.trim());
                    setRefineInput("");
                  }
                }
              }}
              data-testid="agent-wizard-refine-input"
            />
            <button
              type="button"
              onClick={() => {
                if (refineInput.trim()) {
                  onRefine(refineInput.trim());
                  setRefineInput("");
                }
              }}
              disabled={drafting || !refineInput.trim()}
              className="sp-btn sp-btn-ghost text-xs inline-flex items-center gap-1 disabled:opacity-50"
              data-testid="agent-wizard-refine"
            >
              <RotateCcw className="w-3.5 h-3.5" /> Refine
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function StepPreview({
  form, previewMsgs, previewInput, setPreviewInput,
  previewBusy, onSend, onBackToVoice,
}) {
  const scrollRef = useRef(null);
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [previewMsgs]);

  if (!form.system || form.system.length < 50) {
    return (
      <div className="sp-glass rounded-xl p-6 text-sm sp-subtle text-center space-y-3" data-testid="agent-wizard-preview-empty">
        <AlertCircle className="w-5 h-5 mx-auto text-amber-400" />
        <div>You need to generate the agent prompt first.</div>
        <button
          type="button"
          onClick={onBackToVoice}
          className="sp-btn sp-btn-secondary text-xs"
        >
          Back to Voice & rules
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-3" data-testid="agent-wizard-step-preview">
      <Hint>
        <strong>Vibe-check</strong> your agent. Send a test message and see
        how it responds before saving. No history, no tools — just one
        question, one reply.
      </Hint>

      <div
        ref={scrollRef}
        className="sp-glass rounded-xl p-3 min-h-[14rem] max-h-[22rem] overflow-y-auto space-y-3"
        data-testid="agent-wizard-preview-chat"
      >
        {previewMsgs.length === 0 ? (
          <div className="text-xs sp-subtle text-center py-12">
            Send a message below to test how <span className="text-orange-300">{form.name || "your agent"}</span> responds.
          </div>
        ) : (
          previewMsgs.map((m, i) => (
            <div key={i} className={`flex ${m.who === "user" ? "justify-end" : "justify-start"}`}>
              <div
                className={`max-w-[85%] rounded-xl px-3 py-2 text-sm leading-relaxed ${
                  m.who === "user"
                    ? "bg-orange-500/15 text-orange-100 whitespace-pre-wrap"
                    : m.error
                      ? "bg-red-500/10 text-red-200 whitespace-pre-wrap"
                      : "sp-glass-strong"
                }`}
              >
                {m.who === "user" || m.error ? m.text : <MessageMarkdown>{m.text || ""}</MessageMarkdown>}
              </div>
            </div>
          ))
        )}
        {previewBusy && (
          <div className="flex justify-start">
            <div className="sp-glass-strong rounded-xl px-3 py-2 text-sm sp-subtle inline-flex items-center gap-2">
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
              Thinking…
            </div>
          </div>
        )}
      </div>

      <div className="flex gap-2 items-end">
        <textarea
          value={previewInput}
          onChange={(e) => setPreviewInput(e.target.value)}
          rows={2}
          disabled={previewBusy}
          placeholder="Try: 'Critique our pricing page in 3 sentences.' or 'Roast my SaaS deck.'"
          className="sp-input flex-1 text-sm resize-none"
          onKeyDown={(e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === "Enter") onSend();
          }}
          data-testid="agent-wizard-preview-input"
        />
        <button
          type="button"
          onClick={onSend}
          disabled={previewBusy || !previewInput.trim() || !form.system}
          className="sp-btn sp-btn-primary text-xs inline-flex items-center gap-1.5 disabled:opacity-50"
          data-testid="agent-wizard-preview-send"
        >
          {previewBusy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Send className="w-3.5 h-3.5" />}
          Send
        </button>
      </div>
      <div className="text-[10px] sp-subtle">
        Tip: ⌘+Enter to send. Each test message is a fresh, history-free call.
      </div>
    </div>
  );
}

function AdvancedEditor({ form, set }) {
  return (
    <div className="space-y-4" data-testid="agent-wizard-advanced">
      <Hint kind="warn">
        <strong>Advanced mode</strong> — edit the raw system prompt directly.
        Switch back to <em>Guided mode</em> if you'd rather answer 3 plain-English
        questions and have Foundry compose the prompt for you.
      </Hint>
      <Field label="Agent name" required>
        <input
          type="text"
          value={form.name}
          onChange={(e) => set({ name: e.target.value })}
          maxLength={80}
          className="sp-input w-full"
        />
      </Field>
      <div className="grid sm:grid-cols-[1fr_auto] gap-3">
        <Field label="Role / title">
          <input
            type="text"
            value={form.role}
            onChange={(e) => set({ role: e.target.value })}
            maxLength={120}
            className="sp-input w-full"
          />
        </Field>
        <Field label="Emoji">
          <input
            type="text"
            value={form.emoji}
            onChange={(e) => set({ emoji: e.target.value })}
            maxLength={4}
            className="sp-input w-20 text-center"
          />
        </Field>
      </div>
      <Field label="Short description (optional)">
        <input
          type="text"
          value={form.description}
          onChange={(e) => set({ description: e.target.value })}
          maxLength={400}
          className="sp-input w-full"
        />
      </Field>
      <Field
        label="System prompt"
        required
        hint="The full prompt. 50-8000 chars."
      >
        <textarea
          value={form.system}
          onChange={(e) => set({ system: e.target.value })}
          rows={12}
          maxLength={8000}
          className="sp-input w-full font-mono text-xs leading-relaxed resize-none"
          data-testid="agent-wizard-advanced-system"
        />
        <div className="text-[10px] sp-subtle mt-1 text-right">
          {form.system.length} / 8000
        </div>
      </Field>
    </div>
  );
}

function Hint({ children, kind = "info" }) {
  const cls = kind === "warn"
    ? "border-amber-500/30 bg-amber-500/5 text-amber-100"
    : "border-orange-500/20 bg-orange-500/5 text-orange-100/90";
  return (
    <div className={`text-xs leading-relaxed rounded-xl px-3 py-2 border ${cls}`}>
      {children}
    </div>
  );
}

function Field({ label, required, hint, children }) {
  return (
    <div>
      <label className="text-[11px] uppercase tracking-widest sp-subtle mb-1 block">
        {label} {required && <span className="text-orange-300">*</span>}
      </label>
      {children}
      {hint && <div className="text-[10px] sp-subtle mt-1">{hint}</div>}
    </div>
  );
}
