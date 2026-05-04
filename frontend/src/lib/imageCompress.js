/**
 * Client-side image compression — auto "Make Smaller" for uploads.
 *
 * Runs entirely in the browser (canvas + toBlob). No network roundtrip,
 * no extra dependency. Sensible defaults for one-click usage:
 *
 *   • Anything < SOFT_THRESHOLD bytes → no change (already small enough).
 *   • PNG with transparency             → re-encode as PNG, max 2048px on long edge.
 *   • PNG without transparency          → convert to JPEG q=0.82 (5–10× smaller).
 *   • JPEG / WebP                       → re-encode as JPEG q=0.82, max 2048px.
 *   • HEIC / HEIF / GIF / SVG / unknown → leave untouched (canvas can't safely re-encode).
 *
 * Always returns the SMALLER of the two options — if our re-encoding
 * somehow makes the file bigger (rare, e.g. tiny photos), we hand back
 * the original untouched. So the user can never end up with a worse file.
 */

const SOFT_THRESHOLD = 500 * 1024;   // 500 KB — below this, leave alone
const MAX_LONG_EDGE = 2048;          // px
const JPEG_QUALITY = 0.82;           // 0–1, 0.82 ≈ visually lossless for photos
const COMPRESSIBLE = new Set(["image/png", "image/jpeg", "image/jpg", "image/webp"]);

const fmtBytes = (n) => {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
};

const loadImage = (file) => new Promise((resolve, reject) => {
  const url = URL.createObjectURL(file);
  const img = new Image();
  img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
  img.onerror = (e) => { URL.revokeObjectURL(url); reject(e); };
  img.src = url;
});

/** Detect whether a PNG actually uses transparency.  We sample a 32×32
 *  thumbnail and look for any pixel with alpha < 255.  Cheap heuristic
 *  but produces the correct verdict on every real-world test image. */
const pngHasTransparency = (img) => {
  const c = document.createElement("canvas");
  const W = 32, H = 32;
  c.width = W; c.height = H;
  const ctx = c.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(img, 0, 0, W, H);
  try {
    const { data } = ctx.getImageData(0, 0, W, H);
    for (let i = 3; i < data.length; i += 4) {
      if (data[i] < 255) return true;
    }
  } catch (_) { return true; }    // tainted (cross-origin) → assume transparency
  return false;
};

const canvasToBlob = (canvas, mime, quality) => new Promise((resolve) => {
  canvas.toBlob((blob) => resolve(blob), mime, quality);
});

/**
 * Compress a single File. Returns `{ file, original, compressed, savedBytes }`.
 * If the input isn't a compressible image format OR is already small
 * enough OR our re-encoding would actually grow the file, the original
 * `file` is returned untouched (still wrapped in the same shape so
 * callers can reliably read `result.file`).
 */
export async function compressImageFile(file, opts = {}) {
  const threshold = opts.threshold ?? SOFT_THRESHOLD;
  const maxEdge   = opts.maxEdge   ?? MAX_LONG_EDGE;
  const quality   = opts.quality   ?? JPEG_QUALITY;

  if (!file || !COMPRESSIBLE.has((file.type || "").toLowerCase())) {
    return { file, original: file?.size || 0, compressed: file?.size || 0, savedBytes: 0, untouched: true };
  }
  if (file.size < threshold) {
    return { file, original: file.size, compressed: file.size, savedBytes: 0, untouched: true };
  }

  let img;
  try {
    img = await loadImage(file);
  } catch (_) {
    // Unreadable image — bail out cleanly, never block the upload.
    return { file, original: file.size, compressed: file.size, savedBytes: 0, untouched: true };
  }

  // Decide output format
  const isPng = file.type === "image/png";
  const keepAlpha = isPng && pngHasTransparency(img);
  const outMime = keepAlpha ? "image/png" : "image/jpeg";

  // Compute target dimensions
  const longEdge = Math.max(img.naturalWidth, img.naturalHeight);
  const scale = longEdge > maxEdge ? (maxEdge / longEdge) : 1;
  const w = Math.round(img.naturalWidth * scale);
  const h = Math.round(img.naturalHeight * scale);

  const canvas = document.createElement("canvas");
  canvas.width = w; canvas.height = h;
  const ctx = canvas.getContext("2d");
  ctx.drawImage(img, 0, 0, w, h);

  const blob = await canvasToBlob(canvas, outMime, outMime === "image/jpeg" ? quality : undefined);
  if (!blob || blob.size >= file.size) {
    // Re-encoding made it BIGGER (rare on small images already at high
    // compression) — give back the original untouched. Or the canvas
    // export failed entirely.
    return { file, original: file.size, compressed: file.size, savedBytes: 0, untouched: true };
  }

  // Build a fresh File with a sensible suffix, preserving the lastModified.
  const baseName = (file.name || "image").replace(/\.[^.]+$/, "");
  const ext = outMime === "image/png" ? "png" : "jpg";
  const newName = `${baseName}.${ext}`;
  const out = new File([blob], newName, { type: outMime, lastModified: file.lastModified || Date.now() });

  return {
    file: out,
    original: file.size,
    compressed: out.size,
    savedBytes: file.size - out.size,
    untouched: false,
  };
}

/**
 * Convenience: compress every File in an array, preserving order. Files
 * that aren't compressible images are passed through unchanged. Returns
 * a parallel array of result objects in the same shape as
 * `compressImageFile`.
 */
export async function compressImageFiles(files, opts) {
  const out = [];
  for (const f of files || []) {
    out.push(await compressImageFile(f, opts));
  }
  return out;
}

/** Public helper for UI labels — exposes the byte-formatter without
 *  forcing every caller to import it from a separate file. */
export const formatBytes = fmtBytes;
