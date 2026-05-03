import React, { useEffect, useMemo, useState } from "react";
import api, { BACKEND_URL, errorMessage } from "../lib/api";
import { toast } from "sonner";
import { Loader2, X, Wand2, Check, Image as ImageIcon } from "lucide-react";
import WorkingIndicator from "./WorkingIndicator";

/**
 * AssetPickerModal — browse the org's Media Library and either:
 *   (a) attach a chosen asset as the post's cover image (Phase 2), or
 *   (b) remix it via Nano Banana with a user prompt (Phase 3).
 *
 * Keeps the modal self-contained; caller just supplies the post id +
 * org id and a callback for the refreshed post object.
 */
const CATEGORIES = [
  { v: "", label: "All" },
  { v: "product", label: "Product" },
  { v: "lifestyle", label: "Lifestyle" },
  { v: "team", label: "Team" },
  { v: "venue", label: "Venue" },
  { v: "logo_variant", label: "Logo variant" },
  { v: "other", label: "Other" },
];

const REMIX_PRESETS = [
  { label: "Instagram square", prompt: "Retarget this as a 1:1 square composition ideal for Instagram; keep the subject tight and centered." },
  { label: "Lifestyle scene", prompt: "Place this product into a warm, natural lifestyle scene with soft golden-hour lighting; someone about to use it." },
  { label: "Holiday theme", prompt: "Recomposite onto a festive, seasonal background with subtle holiday cues; keep the subject clean and recognisable." },
  { label: "Studio backdrop", prompt: "Clean studio photography look: soft shadow, minimal pastel backdrop, editorial lighting." },
];

export default function AssetPickerModal({ orgId, postId, onClose, onPicked }) {
  const [assets, setAssets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [category, setCategory] = useState("");
  const [picked, setPicked] = useState(null);     // asset object
  const [remix, setRemix] = useState(false);      // show the remix prompt UI
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState("");           // "use" | "remix" | ""

  useEffect(() => {
    (async () => {
      setLoading(true);
      try {
        const { data } = await api.get(`/orgs/${orgId}/assets`);
        // Only image assets are usable for post covers.
        const imgs = (data || []).filter((a) => a.kind === "image");
        setAssets(imgs);
      } finally { setLoading(false); }
    })();
  }, [orgId]);

  const filtered = useMemo(
    () => !category ? assets : assets.filter((a) => a.category === category),
    [assets, category],
  );

  const pickAndUse = async () => {
    if (!picked || busy) return;
    setBusy("use");
    try {
      const { data } = await api.post(`/social/posts/${postId}/use-asset`, { asset_id: picked.id });
      toast.success("Asset attached to post");
      onPicked(data);
      onClose();
    } catch (err) {
      toast.error(errorMessage(err, "Couldn't attach asset"));
    } finally { setBusy(""); }
  };

  const pickAndRemix = async () => {
    if (!picked || busy || !prompt.trim()) return;
    setBusy("remix");
    try {
      const { data } = await api.post(`/social/posts/${postId}/remix-asset`, {
        asset_id: picked.id,
        prompt: prompt.trim(),
      });
      toast.success("Remix done");
      onPicked(data);
      onClose();
    } catch (err) {
      toast.error(errorMessage(err, "Remix failed"));
    } finally { setBusy(""); }
  };

  return (
    <div
      className="fixed inset-0 z-[90] flex items-center justify-center p-4"
      style={{ background: "rgba(0,0,0,0.72)" }}
      onClick={onClose}
      data-testid="asset-picker-modal"
    >
      <div
        className="sp-glass-strong rounded-2xl p-5 max-w-4xl w-full max-h-[90vh] overflow-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <div>
            <h3 className="sp-display text-xl">Use a photo from your Media Library</h3>
            <p className="text-xs sp-subtle mt-0.5">
              Attach an existing image or remix it with AI — keeps your post on-brand in seconds.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="sp-btn sp-btn-ghost !p-2"
            data-testid="asset-picker-close"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="flex flex-wrap gap-1 mb-3">
          {CATEGORIES.map((c) => (
            <button
              key={c.v || "all"}
              type="button"
              onClick={() => { setCategory(c.v); setPicked(null); setRemix(false); }}
              className={`sp-chip text-[10px] ${category === c.v ? "!bg-orange-500/20 !border-orange-400/40 !text-white" : ""}`}
              data-testid={`asset-picker-cat-${c.v || "all"}`}
            >
              {c.label}
            </button>
          ))}
        </div>

        {loading ? (
          <div className="flex items-center gap-2 py-10 justify-center sp-subtle"><Loader2 className="w-4 h-4 animate-spin" /> Loading…</div>
        ) : filtered.length === 0 ? (
          <div className="py-10 text-center sp-subtle text-sm italic">
            {assets.length === 0
              ? "No photos yet. Head to the Org page → Asset library to upload product shots, lifestyle photos, or team pictures."
              : "No photos in this category."}
          </div>
        ) : (
          <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 gap-2 mb-4">
            {filtered.map((a) => {
              const url = a.url?.startsWith("http") ? a.url : `${BACKEND_URL}${a.url}`;
              const sel = picked?.id === a.id;
              return (
                <button
                  key={a.id}
                  type="button"
                  onClick={() => { setPicked(a); setRemix(false); }}
                  className={`relative aspect-square rounded-lg overflow-hidden border-2 transition ${sel ? "border-orange-400" : "border-transparent hover:border-white/20"}`}
                  style={{ background: "var(--chip-bg)" }}
                  data-testid={`asset-picker-item-${a.id}`}
                  title={a.description || a.original_name}
                >
                  <img src={url} alt={a.original_name} className="w-full h-full object-cover" loading="lazy" />
                  {sel && (
                    <span className="absolute top-1 right-1 w-5 h-5 rounded-full bg-orange-500 flex items-center justify-center">
                      <Check className="w-3 h-3 text-white" />
                    </span>
                  )}
                  {a.category && (
                    <span className="absolute bottom-1 left-1 px-1.5 py-0.5 rounded text-[9px] uppercase tracking-wider font-semibold bg-black/60 text-white">
                      {a.category}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        )}

        {picked && (
          <div className="sp-glass rounded-xl p-3 mb-3 flex items-start gap-3">
            <img
              src={picked.url?.startsWith("http") ? picked.url : `${BACKEND_URL}${picked.url}`}
              alt={picked.original_name}
              className="w-16 h-16 rounded-lg object-cover shrink-0"
            />
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 text-sm font-medium">
                <ImageIcon className="w-3 h-3 shrink-0" />
                <span className="truncate">{picked.original_name}</span>
              </div>
              {picked.description && <p className="text-xs sp-subtle mt-0.5">{picked.description}</p>}
            </div>
          </div>
        )}

        {picked && remix && (
          <div className="sp-glass rounded-xl p-3 mb-3 space-y-2" data-testid="asset-remix-panel">
            <div className="text-xs sp-subtle leading-relaxed">
              Nano Banana will use your photo as the base and generate a new branded variation.
              Try one of these quick starters or write your own direction:
            </div>
            <div className="flex flex-wrap gap-1">
              {REMIX_PRESETS.map((p) => (
                <button
                  key={p.label}
                  type="button"
                  onClick={() => setPrompt(p.prompt)}
                  className="sp-chip text-[10px] hover:!bg-white/10"
                  data-testid={`asset-remix-preset-${p.label.toLowerCase().replace(/\s+/g, "-")}`}
                >
                  {p.label}
                </button>
              ))}
            </div>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="How should the remix look? e.g. 'Place on a serene desk setup for a productivity post'"
              rows={3}
              className="w-full rounded-lg px-3 py-2 text-sm"
              style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
              data-testid="asset-remix-prompt"
            />
            {busy === "remix" && (
              <WorkingIndicator
                label="Remixing your photo"
                hints={[
                  "Sending your photo + direction to Nano Banana…",
                  "Compositing — image-to-image takes 10-30s for most prompts.",
                  "Almost there — final pass adds your logo if one's set.",
                ]}
                testId="asset-remix-working"
              />
            )}
          </div>
        )}

        <div className="flex gap-2 justify-end flex-wrap">
          <button
            type="button"
            onClick={onClose}
            className="sp-btn sp-btn-ghost text-xs"
            data-testid="asset-picker-cancel"
          >Cancel</button>
          {picked && !remix && (
            <button
              type="button"
              onClick={() => setRemix(true)}
              className="sp-btn sp-btn-ghost text-xs"
              data-testid="asset-picker-open-remix"
            >
              <Wand2 className="w-3.5 h-3.5" /> Remix instead
            </button>
          )}
          {picked && remix ? (
            <button
              type="button"
              onClick={pickAndRemix}
              disabled={!prompt.trim() || busy === "remix"}
              className="sp-btn sp-btn-primary text-xs disabled:opacity-50"
              data-testid="asset-picker-remix-submit"
            >
              {busy === "remix" ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Wand2 className="w-3.5 h-3.5" />}
              {busy === "remix" ? "Remixing…" : "Remix & use"}
            </button>
          ) : (
            <button
              type="button"
              onClick={pickAndUse}
              disabled={!picked || busy === "use"}
              className="sp-btn sp-btn-primary text-xs disabled:opacity-50"
              data-testid="asset-picker-use"
            >
              {busy === "use" ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />}
              {busy === "use" ? "Attaching…" : "Use this photo"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
