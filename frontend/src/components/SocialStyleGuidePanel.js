import React, { useEffect, useMemo, useRef, useState } from "react";
import api from "../lib/api";
import { toast } from "sonner";
import {
  Sparkles, Plus, Trash2, RefreshCw, ExternalLink, Save, Pencil, X as XIcon,
  Wand2, Loader2, AlertTriangle, Eye, EyeOff,
} from "lucide-react";

const PLATFORMS = [
  { key: "twitter", label: "X / Twitter" },
  { key: "linkedin", label: "LinkedIn" },
  { key: "instagram", label: "Instagram" },
  { key: "tiktok", label: "TikTok" },
  { key: "youtube", label: "YouTube" },
  { key: "threads", label: "Threads" },
  { key: "bluesky", label: "Bluesky" },
  { key: "mastodon", label: "Mastodon" },
  { key: "pinterest", label: "Pinterest" },
  { key: "reddit", label: "Reddit" },
  { key: "facebook", label: "Facebook" },
  { key: "substack", label: "Substack" },
  { key: "medium", label: "Medium" },
  { key: "other", label: "Other" },
];

const GUIDE_FIELDS = [
  { key: "tone_voice", label: "Tone & voice" },
  { key: "vocabulary", label: "Vocabulary" },
  { key: "do_not_use", label: "Do not use" },
  { key: "post_structure", label: "Post structure" },
  { key: "themes", label: "Themes" },
  { key: "image_video_style", label: "Image / video style" },
  { key: "cadence", label: "Cadence" },
  { key: "summary", label: "Summary" },
];

/**
 * SocialStyleGuidePanel — Profiles + samples → synthesized brand guide that
 * gets auto-injected into every Social Queue draft and every agent system
 * prompt. See `style_guide.py` on the backend.
 */
export default function SocialStyleGuidePanel({ orgId }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [showAddProfile, setShowAddProfile] = useState(false);
  const [synthing, setSynthing] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({});
  const [showSamples, setShowSamples] = useState(false);
  const [samplesDraft, setSamplesDraft] = useState("");
  const [samplesDirty, setSamplesDirty] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const { data: d } = await api.get(`/orgs/${orgId}/style-guide`);
      setData(d);
      setSamplesDraft(d.samples || "");
      setSamplesDirty(false);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed to load style guide");
    } finally { setLoading(false); }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [orgId]);

  const profiles = data?.profiles || [];
  const guide = data?.guide || {};
  const hasGuide = useMemo(
    () => GUIDE_FIELDS.some((f) => (guide[f.key] || "").trim()),
    [guide],
  );
  const canSynthesize = profiles.length > 0 || (samplesDraft || "").trim().length > 30;

  const removeProfile = async (id) => {
    try {
      const { data: d } = await api.delete(`/orgs/${orgId}/style-guide/profiles/${id}`);
      setData(d);
      toast.success("Removed");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't remove");
    }
  };

  const saveSamples = async () => {
    try {
      const { data: d } = await api.put(`/orgs/${orgId}/style-guide/samples`, {
        samples: samplesDraft,
      });
      setData(d);
      setSamplesDirty(false);
      toast.success("Samples saved");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Save failed");
    }
  };

  const synthesize = async () => {
    if (synthing) return;
    if (samplesDirty) await saveSamples();
    setSynthing(true);
    try {
      const { data: d } = await api.post(`/orgs/${orgId}/style-guide/synthesize`);
      setData(d);
      if (d.warning) toast.message(d.warning);
      else toast.success("Style guide synthesized — agents are updated");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Synthesis failed");
    } finally { setSynthing(false); }
  };

  const startEdit = () => {
    const seed = {};
    GUIDE_FIELDS.forEach((f) => { seed[f.key] = guide[f.key] || ""; });
    setDraft(seed);
    setEditing(true);
  };
  const saveEdit = async () => {
    try {
      const { data: d } = await api.patch(`/orgs/${orgId}/style-guide`, draft);
      setData(d);
      setEditing(false);
      toast.success("Saved");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Save failed");
    }
  };

  const generatedLabel = useMemo(() => {
    if (!data?.generated_at) return "";
    try {
      return new Date(data.generated_at).toLocaleString([], {
        month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
      });
    } catch { return ""; }
  }, [data]);

  return (
    <section className="sp-glass-strong rounded-2xl p-6 mb-8" data-testid="style-guide-panel">
      <div className="flex items-start justify-between gap-4 mb-4 flex-wrap">
        <div className="min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <Wand2 className="w-4 h-4 text-white/80" />
            <h3 className="text-sm uppercase tracking-widest sp-subtle">Social Style Guide</h3>
            {hasGuide && (
              <span className="sp-chip text-[10px]" data-testid="style-guide-status">
                Active
              </span>
            )}
            {data?.is_manually_edited && (
              <span className="sp-chip text-[10px] !bg-white/5">Edited</span>
            )}
          </div>
          <p className="text-xs sp-subtle max-w-2xl">
            Connect your social profiles (or ones you want to emulate), drop a few sample posts,
            and we'll synthesize a brand voice + visual style guide. Every Social Queue draft and
            every agent answer will follow it automatically.
          </p>
          {generatedLabel && (
            <div className="text-[11px] sp-subtle mt-1">Last synthesized {generatedLabel}</div>
          )}
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {hasGuide && !editing && (
            <button
              onClick={startEdit}
              className="sp-btn sp-btn-ghost text-xs"
              data-testid="style-guide-edit-btn"
            >
              <Pencil className="w-3.5 h-3.5" /> Edit
            </button>
          )}
          <button
            onClick={synthesize}
            disabled={!canSynthesize || synthing}
            className="sp-btn sp-btn-primary text-xs disabled:opacity-50"
            data-testid="style-guide-synthesize-btn"
          >
            {synthing
              ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Synthesizing…</>
              : <><Sparkles className="w-3.5 h-3.5" /> {hasGuide ? "Re-synthesize" : "Synthesize"}</>}
          </button>
        </div>
      </div>

      {loading ? (
        <div className="sp-subtle text-sm py-6 text-center">Loading style guide…</div>
      ) : (
        <>
          {/* ── Profiles ─────────────────────────────────────────────── */}
          <div className="mb-5">
            <div className="flex items-center justify-between mb-2">
              <div className="text-[10px] uppercase tracking-widest sp-subtle">
                Profiles {profiles.length > 0 ? `· ${profiles.length}` : ""}
              </div>
              <button
                onClick={() => setShowAddProfile(true)}
                className="text-[11px] sp-subtle hover:text-white inline-flex items-center gap-1"
                data-testid="style-guide-add-profile"
              >
                <Plus className="w-3 h-3" /> Add profile
              </button>
            </div>
            {profiles.length === 0 ? (
              <div className="sp-glass rounded-xl p-3 text-xs sp-subtle">
                No profiles yet. Add your accounts (Own) and accounts you want to emulate (Inspiration).
              </div>
            ) : (
              <ul className="grid sm:grid-cols-2 gap-2" data-testid="style-guide-profiles">
                {profiles.map((p) => (
                  <li
                    key={p.id}
                    className="sp-glass rounded-xl p-2.5 flex items-center gap-3"
                    data-testid={`style-guide-profile-${p.id}`}
                  >
                    <span
                      className={`sp-chip text-[10px] shrink-0 ${
                        p.role === "own" ? "!bg-emerald-400/10 !text-emerald-200/90"
                                          : "!bg-blue-400/10 !text-blue-200/90"
                      }`}
                    >
                      {p.role === "own" ? "Own" : "Inspiration"}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="text-sm font-medium truncate">
                        {p.platform.toUpperCase()} · {p.handle ? `@${p.handle}` : new URL(p.url || "https://x").hostname}
                      </div>
                      <div className="text-[11px] sp-subtle truncate">{p.url || "(no URL)"}</div>
                    </div>
                    {p.url && (
                      <a
                        href={p.url}
                        target="_blank" rel="noreferrer"
                        className="p-1.5 rounded hover:bg-white/10"
                        title="Open"
                      >
                        <ExternalLink className="w-3.5 h-3.5" />
                      </a>
                    )}
                    <button
                      onClick={() => removeProfile(p.id)}
                      className="p-1.5 rounded hover:bg-white/10 hover:text-red-300"
                      data-testid={`style-guide-profile-remove-${p.id}`}
                      title="Remove"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {/* ── Samples ───────────────────────────────────────────────── */}
          <div className="mb-5">
            <div className="flex items-center justify-between mb-2">
              <div className="text-[10px] uppercase tracking-widest sp-subtle">
                Sample posts {samplesDraft.trim() ? `· ${samplesDraft.trim().split(/\n{2,}/).length}` : ""}
              </div>
              <button
                onClick={() => setShowSamples((v) => !v)}
                className="text-[11px] sp-subtle hover:text-white inline-flex items-center gap-1"
                data-testid="style-guide-toggle-samples"
              >
                {showSamples ? <EyeOff className="w-3 h-3" /> : <Eye className="w-3 h-3" />}
                {showSamples ? "Hide" : "Show / edit"}
              </button>
            </div>
            {showSamples && (
              <div className="sp-glass rounded-xl p-3" data-testid="style-guide-samples-editor">
                <textarea
                  value={samplesDraft}
                  onChange={(e) => { setSamplesDraft(e.target.value); setSamplesDirty(true); }}
                  rows={6}
                  placeholder={`Paste a few of your posts (or posts you'd like to emulate), separated by a blank line.\n\nUseful when a platform blocks automated lookups, or when you don't have a presence yet.`}
                  className="w-full bg-transparent text-[13px] leading-relaxed placeholder:opacity-40 focus:outline-none resize-none font-mono"
                  data-testid="style-guide-samples-input"
                />
                {samplesDirty && (
                  <div className="flex justify-end mt-2">
                    <button
                      onClick={saveSamples}
                      className="sp-btn sp-btn-ghost text-xs"
                      data-testid="style-guide-samples-save"
                    >
                      <Save className="w-3.5 h-3.5" /> Save samples
                    </button>
                  </div>
                )}
              </div>
            )}
            {!showSamples && samplesDraft.trim() && (
              <div className="text-[11px] sp-subtle truncate">
                {samplesDraft.replace(/\n+/g, " · ").slice(0, 160)}…
              </div>
            )}
          </div>

          {!data?.perplexity_available && profiles.length > 0 && (
            <div className="sp-glass rounded-xl p-3 text-[12px] sp-subtle mb-5 flex items-start gap-2">
              <AlertTriangle className="w-4 h-4 text-amber-300/80 shrink-0 mt-0.5" />
              <div>
                Perplexity isn't configured, so profile lookups will be skipped. Paste sample posts above and synthesize from those instead.
              </div>
            </div>
          )}

          {/* ── Generated guide ──────────────────────────────────────── */}
          {hasGuide || editing ? (
            <div className="grid sm:grid-cols-2 gap-2" data-testid="style-guide-fields">
              {GUIDE_FIELDS.map((f) => (
                <div
                  key={f.key}
                  className="sp-glass rounded-xl p-3"
                  data-testid={`style-guide-field-${f.key}`}
                >
                  <div className="text-[10px] uppercase tracking-widest sp-subtle mb-1.5">
                    {f.label}
                  </div>
                  {editing ? (
                    <textarea
                      value={draft[f.key] ?? ""}
                      onChange={(e) => setDraft((d) => ({ ...d, [f.key]: e.target.value }))}
                      rows={f.key === "summary" ? 3 : 2}
                      className="w-full bg-transparent text-[13px] leading-relaxed focus:outline-none resize-none"
                      data-testid={`style-guide-input-${f.key}`}
                    />
                  ) : (
                    <div className="text-[13px] leading-relaxed whitespace-pre-wrap opacity-90">
                      {(guide[f.key] || "").trim() || <span className="sp-subtle">—</span>}
                    </div>
                  )}
                </div>
              ))}
              {editing && (
                <div className="sm:col-span-2 flex items-center justify-end gap-2 mt-1">
                  <button
                    onClick={() => setEditing(false)}
                    className="sp-btn sp-btn-ghost text-xs"
                    data-testid="style-guide-cancel-edit"
                  >Cancel</button>
                  <button
                    onClick={saveEdit}
                    className="sp-btn sp-btn-primary text-xs"
                    data-testid="style-guide-save-edit"
                  >
                    <Save className="w-3.5 h-3.5" /> Save
                  </button>
                </div>
              )}
            </div>
          ) : (
            <div className="sp-glass rounded-xl p-4 text-center text-xs sp-subtle">
              {canSynthesize
                ? `Hit "Synthesize" to build the guide from ${profiles.length || 0} profile${profiles.length === 1 ? "" : "s"} and your samples.`
                : "Add a profile or paste a few sample posts above, then synthesize."}
            </div>
          )}
        </>
      )}

      {showAddProfile && (
        <AddProfileDialog
          orgId={orgId}
          onClose={() => setShowAddProfile(false)}
          onSaved={(d) => { setData(d); setShowAddProfile(false); }}
        />
      )}
    </section>
  );
}

// ── Add-profile dialog (own / inspiration) ─────────────────────────────────
function AddProfileDialog({ orgId, onClose, onSaved }) {
  const [platform, setPlatform] = useState("twitter");
  const [url, setUrl] = useState("");
  const [handle, setHandle] = useState("");
  const [role, setRole] = useState("own");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const ref = useRef(null);
  useEffect(() => { ref.current?.focus(); }, []);

  const save = async () => {
    if (!url.trim() && !handle.trim()) {
      toast.error("Provide a URL or a handle");
      return;
    }
    setBusy(true);
    try {
      const { data } = await api.post(`/orgs/${orgId}/style-guide/profiles`, {
        platform, url: url.trim(), handle: handle.trim().replace(/^@/, ""),
        role, notes: notes.trim(),
      });
      onSaved(data);
      toast.success("Profile added");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't add profile");
    } finally { setBusy(false); }
  };

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm"
      onClick={onClose}
      data-testid="style-guide-add-profile-dialog"
    >
      <div
        className="sp-glass-strong rounded-2xl w-full max-w-md p-6 sp-fade-up"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h3 className="sp-display text-2xl">Add profile</h3>
          <button onClick={onClose} className="text-white/60 hover:text-white">
            <XIcon className="w-4 h-4" />
          </button>
        </div>

        <div className="grid grid-cols-2 gap-2 mb-3">
          <button
            type="button"
            onClick={() => setRole("own")}
            className={`sp-glass rounded-xl p-2.5 text-left text-sm ${role === "own" ? "border-emerald-400/40" : ""}`}
            data-testid="role-own"
          >
            <div className="font-medium">Own</div>
            <div className="text-[11px] sp-subtle">Your account</div>
          </button>
          <button
            type="button"
            onClick={() => setRole("inspiration")}
            className={`sp-glass rounded-xl p-2.5 text-left text-sm ${role === "inspiration" ? "border-blue-400/40" : ""}`}
            data-testid="role-inspiration"
          >
            <div className="font-medium">Inspiration</div>
            <div className="text-[11px] sp-subtle">Voice to emulate</div>
          </button>
        </div>

        <label className="text-[10px] uppercase tracking-widest sp-subtle">Platform</label>
        <select
          value={platform}
          onChange={(e) => setPlatform(e.target.value)}
          className="w-full sp-glass rounded-xl px-3 py-2 text-sm mb-3 focus:outline-none"
          style={{ background: "var(--input-bg)", color: "var(--fg)" }}
          data-testid="profile-platform"
        >
          {PLATFORMS.map((p) => (
            <option key={p.key} value={p.key} style={{ color: "#000" }}>{p.label}</option>
          ))}
        </select>

        <label className="text-[10px] uppercase tracking-widest sp-subtle">Profile URL</label>
        <input
          ref={ref}
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://twitter.com/yourhandle"
          className="w-full sp-glass rounded-xl px-3 py-2 text-sm mb-3 focus:outline-none"
          style={{ background: "var(--input-bg)", color: "var(--fg)" }}
          data-testid="profile-url"
        />

        <label className="text-[10px] uppercase tracking-widest sp-subtle">Handle (optional)</label>
        <input
          value={handle}
          onChange={(e) => setHandle(e.target.value)}
          placeholder="yourhandle"
          className="w-full sp-glass rounded-xl px-3 py-2 text-sm mb-3 focus:outline-none"
          style={{ background: "var(--input-bg)", color: "var(--fg)" }}
          data-testid="profile-handle"
        />

        <label className="text-[10px] uppercase tracking-widest sp-subtle">Notes (optional)</label>
        <input
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="What to focus on (e.g. 'their hook patterns')"
          className="w-full sp-glass rounded-xl px-3 py-2 text-sm mb-4 focus:outline-none"
          style={{ background: "var(--input-bg)", color: "var(--fg)" }}
          data-testid="profile-notes"
        />

        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            disabled={busy}
            className="sp-btn sp-btn-ghost text-xs"
          >Cancel</button>
          <button
            onClick={save}
            disabled={busy}
            className="sp-btn sp-btn-primary text-xs disabled:opacity-60"
            data-testid="profile-save"
          >
            {busy
              ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Adding…</>
              : <><Plus className="w-3.5 h-3.5" /> Add</>}
          </button>
        </div>
      </div>
    </div>
  );
}
