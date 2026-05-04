import React, { useEffect, useRef, useState } from "react";
import api, { BACKEND_URL } from "../lib/api";
import { toast } from "sonner";
import { Sparkles, UploadCloud, Trash2, Save, Image as ImageIcon, X as XIcon, Lock } from "lucide-react";

// One self-contained component for the org's brand kit:
// - primary logo (used for social-image overlay)
// - alt logo (light/dark variant — currently informational, queued for v2)
// - brand colors (palette injected into image generation prompt)
// - tagline (bakes the org's voice into draft prompts)
//
// All edits are owner-only on the backend. This panel mirrors that on the
// client so non-owners see a tidy read-only view with a "Owner-only" notice
// instead of being able to click upload buttons that 403. Members who need
// a logo change should ask the org owner directly (in chat or on a call).
export default function BrandAssetsPanel({ orgId, initial, isOwner = false, onChange }) {
  const [brand, setBrand] = useState(initial || { primary_logo: null, alt_logo: null, brand_colors: [], tagline: "", visual_style: "" });
  const [tagline, setTagline] = useState((initial?.tagline) || "");
  const [visualStyle, setVisualStyle] = useState((initial?.visual_style) || "");
  const [colorsText, setColorsText] = useState(((initial?.brand_colors) || []).join(", "));
  const [savingMeta, setSavingMeta] = useState(false);
  const [uploadingKind, setUploadingKind] = useState("");
  const primaryRef = useRef(null);
  const altRef = useRef(null);

  useEffect(() => {
    if (initial) {
      setBrand(initial);
      setTagline(initial.tagline || "");
      setVisualStyle(initial.visual_style || "");
      setColorsText((initial.brand_colors || []).join(", "));
    }
  }, [initial]);

  const uploadLogo = async (kind, file) => {
    if (!file) return;
    if (!isOwner) {
      toast.error("Only the organization owner can change brand logos. Ask the owner to update it.");
      return;
    }
    if (file.size > 8 * 1024 * 1024) {
      toast.error("Logo must be 8 MB or smaller. Tip: shrink it in any image-attachment composer first, then upload here.");
      return;
    }
    setUploadingKind(kind);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const { data } = await api.post(`/orgs/${orgId}/brand/logo?kind=${kind}`, fd, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      setBrand(data);
      onChange?.(data);
      toast.success(`${kind === "primary" ? "Primary" : "Alt"} logo updated`);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Upload failed");
    } finally { setUploadingKind(""); }
  };

  const removeLogo = async (kind) => {
    if (!isOwner) {
      toast.error("Only the organization owner can remove brand logos.");
      return;
    }
    if (!window.confirm(`Remove the ${kind} logo?`)) return;
    try {
      const { data } = await api.delete(`/orgs/${orgId}/brand/logo/${kind}`);
      setBrand(data);
      onChange?.(data);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed");
    }
  };

  const saveMeta = async () => {
    if (!isOwner) {
      toast.error("Only the organization owner can change brand colors, tagline, or visual style.");
      return;
    }
    setSavingMeta(true);
    const colors = colorsText.split(/[\s,]+/).map((c) => c.trim()).filter(Boolean);
    try {
      const { data } = await api.patch(`/orgs/${orgId}/brand`, {
        tagline: tagline.trim(),
        brand_colors: colors,
        visual_style: visualStyle.trim(),
      });
      setBrand(data);
      // Backend strips invalid hex codes — sync the field back so the user
      // sees what was kept.
      setColorsText((data.brand_colors || []).join(", "));
      onChange?.(data);
      toast.success("Brand details saved");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Save failed");
    } finally { setSavingMeta(false); }
  };

  const renderLogoSlot = (kind, ref, doc, label, helper) => (
    <div className="sp-glass rounded-xl p-4 flex flex-col items-stretch gap-3" data-testid={`brand-${kind}-slot`}>
      <div className="flex items-center justify-between">
        <span className="text-xs uppercase tracking-widest sp-subtle">{label}</span>
        {doc && isOwner && (
          <button type="button" onClick={() => removeLogo(kind)} className="sp-subtle hover:text-red-400 text-[11px]" data-testid={`brand-remove-${kind}`}>
            <Trash2 className="w-3 h-3 inline-block mr-1" /> Remove
          </button>
        )}
      </div>
      <div className="flex items-center justify-center h-32 rounded-lg" style={{ background: "color-mix(in srgb, var(--bg-soft) 80%, transparent)" }}>
        {doc ? (
          <img
            /* Cache-bust with the filename itself (logos get new filenames
               on every upload so the cache-key naturally changes). Adding
               ?v=... is belt-and-braces in case a CDN along the way
               ignored the new path. */
            src={`${BACKEND_URL}${doc.url}${doc.url.includes("?") ? "&" : "?"}v=${encodeURIComponent(doc.filename || "")}`}
            alt={`${kind} logo`}
            className="max-h-28 max-w-full object-contain"
            data-testid={`brand-${kind}-img`}
            onError={(e) => {
              // If the image 404s on a reload (e.g. file was cleaned up or
              // the path encoding is off), hide the <img> and show the
              // placeholder icon so the user doesn't see the browser's
              // broken-image glyph. They can re-upload via the button below.
              e.currentTarget.style.display = "none";
              const parent = e.currentTarget.parentElement;
              if (parent && !parent.querySelector("[data-logo-err]")) {
                const fb = document.createElement("div");
                fb.setAttribute("data-logo-err", "1");
                fb.className = "text-[11px] text-red-300 text-center leading-snug px-2";
                fb.innerHTML = "Couldn't load this logo — please re-upload.";
                parent.appendChild(fb);
              }
            }}
          />
        ) : (
          <ImageIcon className="w-7 h-7 sp-subtle opacity-50" />
        )}
      </div>
      <input
        type="file"
        accept="image/png,image/jpeg,image/webp,image/svg+xml,image/gif"
        ref={ref}
        className="hidden"
        onChange={(e) => uploadLogo(kind, e.target.files?.[0])}
        data-testid={`brand-${kind}-input`}
      />
      <button
        type="button"
        onClick={() => isOwner && ref.current?.click()}
        disabled={!isOwner || uploadingKind === kind}
        className="sp-btn sp-btn-ghost text-xs justify-center"
        data-testid={`brand-${kind}-upload`}
        title={isOwner ? "" : "Only the organization owner can change brand logos"}
      >
        {isOwner ? (
          <>
            <UploadCloud className="w-3.5 h-3.5" />
            {uploadingKind === kind ? "Uploading…" : (doc ? "Replace" : "Upload")}
          </>
        ) : (
          <>
            <Lock className="w-3.5 h-3.5" /> Owner-only
          </>
        )}
      </button>
      <div className="text-[11px] sp-subtle">{helper}</div>
    </div>
  );

  return (
    <section className="sp-glass-strong rounded-2xl p-5 sm:p-6 mb-6" data-testid="brand-assets-panel">
      <div className="flex items-center gap-2 mb-4">
        <Sparkles className="w-4 h-4 text-orange-400" />
        <h3 className="text-sm uppercase tracking-widest sp-subtle">Brand assets</h3>
        <span className="text-[11px] sp-subtle ml-2">Used by the social-image generator</span>
        {!isOwner && (
          <span
            className="ml-auto inline-flex items-center gap-1.5 sp-chip text-[10px]"
            data-testid="brand-readonly-badge"
            title="Only the organization owner can change brand assets"
          >
            <Lock className="w-3 h-3" /> Owner-only
          </span>
        )}
      </div>

      {!isOwner && (
        <div
          className="sp-glass rounded-xl px-4 py-3 mb-4 text-xs leading-relaxed sp-subtle"
          data-testid="brand-readonly-notice"
        >
          You can view the brand kit, but only the <strong>organization owner</strong>
          {" "}can change the logo, colors, tagline, or visual style. Need an update?
          Ping the owner in chat — they can update it in seconds.
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-4">
        {renderLogoSlot("primary", primaryRef, brand.primary_logo, "Primary logo",
          "Auto-overlaid on every social post image (default lower-right corner; pick any of 5 corners per post).")}
        {renderLogoSlot("alt", altRef, brand.alt_logo, "Alt logo (light/dark variant)",
          "Optional — for now this is just stored; future versions will pick the best variant per image background.")}
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div className="space-y-1">
          <label className="text-[11px] sp-subtle uppercase tracking-widest">Brand colors</label>
          <input
            value={colorsText}
            onChange={(e) => setColorsText(e.target.value)}
            placeholder="#F64000, #1B1B1B, #FFFFFF"
            disabled={!isOwner}
            className="w-full rounded-lg px-3 py-2 text-sm focus:sp-focus disabled:opacity-50 disabled:cursor-not-allowed"
            style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
            data-testid="brand-colors-input"
          />
          {brand.brand_colors?.length > 0 && (
            <div className="flex flex-wrap gap-2 pt-1">
              {brand.brand_colors.map((c) => (
                <span key={c} className="inline-flex items-center gap-1.5 text-[11px] sp-subtle" data-testid={`brand-color-chip-${c.replace("#","")}`}>
                  <span className="inline-block w-4 h-4 rounded-md border border-white/15" style={{ background: c }} />
                  <code>{c}</code>
                </span>
              ))}
            </div>
          )}
        </div>

        <div className="space-y-1">
          <label className="text-[11px] sp-subtle uppercase tracking-widest">Tagline</label>
          <input
            value={tagline}
            onChange={(e) => setTagline(e.target.value)}
            placeholder="Distil the Soul of every business."
            disabled={!isOwner}
            className="w-full rounded-lg px-3 py-2 text-sm focus:sp-focus disabled:opacity-50 disabled:cursor-not-allowed"
            style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
            maxLength={240}
            data-testid="brand-tagline-input"
          />
        </div>
      </div>

      {/* Visual style — feeds directly into the social-image generator prompt
          so every cover image shares a consistent aesthetic. Presets give
          users a friendly starting point without having to know prompt-craft. */}
      <div className="mt-3 space-y-2">
        <label className="text-[11px] sp-subtle uppercase tracking-widest flex items-center justify-between">
          <span>Visual style / aesthetic</span>
          <span className="opacity-60 normal-case tracking-normal text-[10px]">drives every social image</span>
        </label>
        <div className="flex flex-wrap gap-1.5" data-testid="brand-style-presets">
          {[
            ["Editorial photography",   "Rich editorial photography. Natural daylight, shallow depth of field, subtle film grain, warm tones. Real human moments, unposed, documentary feel."],
            ["Minimalist vector",       "Clean flat vector illustration. Thick strokes, rounded geometric shapes, generous negative space, limited 3-color palette. Modern and restrained."],
            ["Bold 3D isometric",       "Bold 3D isometric illustration. Playful shapes, soft shadows, gradient highlights, pastel accents. Friendly, vibrant, tech-forward."],
            ["Cinematic moody",         "Cinematic moody rendering. Deep shadows, volumetric lighting, teal-and-orange grading, slight anamorphic lens flare. Dramatic and premium."],
            ["Hand-sketched",           "Loose hand-drawn sketch. Paper texture, ink bleeds, watercolor washes. Imperfect and human."],
            ["Retro Bauhaus",           "Retro Bauhaus-inspired composition. Bold primary shapes, limited palette, grid alignment, mid-century typography accents."],
          ].map(([label, prompt]) => (
            <button
              key={label}
              type="button"
              onClick={() => isOwner && setVisualStyle(prompt)}
              disabled={!isOwner}
              className={`sp-chip text-[11px] disabled:opacity-50 disabled:cursor-not-allowed ${visualStyle === prompt ? "!bg-orange-500/20 !text-white !border-orange-400" : ""}`}
              data-testid={`style-preset-${label.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`}
              title={prompt}
            >
              {label}
            </button>
          ))}
          {visualStyle && (
            <button type="button" onClick={() => setVisualStyle("")}
                    className="sp-chip text-[11px] hover:!text-red-400"
                    data-testid="clear-visual-style">
              Clear
            </button>
          )}
        </div>
        <textarea
          value={visualStyle}
          onChange={(e) => setVisualStyle(e.target.value)}
          placeholder="Describe the look & feel of images for this brand. Pick a preset above or write your own. Examples: 'rich editorial photography, natural light, warm tones' / 'bold flat vectors, black + orange, generous whitespace'."
          rows={3}
          disabled={!isOwner}
          className="w-full rounded-lg px-3 py-2 text-sm resize-none focus:sp-focus disabled:opacity-50 disabled:cursor-not-allowed"
          style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
          maxLength={600}
          data-testid="brand-visual-style-input"
        />
      </div>

      {isOwner && (
        <div className="flex items-center justify-end pt-3">
          <button
            type="button"
            onClick={saveMeta}
            disabled={savingMeta}
            className="sp-btn sp-btn-primary text-xs disabled:opacity-50"
            data-testid="save-brand-btn"
          >
            <Save className="w-3.5 h-3.5" /> {savingMeta ? "Saving…" : "Save brand details"}
          </button>
        </div>
      )}
    </section>
  );
}
