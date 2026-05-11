/**
 * Clipboard sanitiser.
 *
 * When a user copies from Foundry the browser by default
 * captures the rendered styles — meaning a dark-theme page produces a
 * "white text on black/grey" payload that looks awful pasted into Gmail,
 * Notion, Word, Google Docs, etc.
 *
 * This module installs ONE global `copy` listener that:
 *   1. Lets the user's normal selection happen.
 *   2. Walks the selected DOM range, clones it, and strips every
 *      colour / background / outline declaration (both inline `style`
 *      attributes AND CSS rules expressed via `<font color>` etc.).
 *   3. Writes BOTH plain text and the sanitised HTML back to the
 *      ClipboardEvent so the recipient app can render with its own theme.
 *
 * Pasting into Notion / Docs / Gmail now produces black text on
 * whatever background the destination provides.
 *
 * One install per page lifecycle. Idempotent — re-calling does nothing.
 */

let installed = false;

// Style declarations we strip from the cloned tree before writing it
// back to the clipboard. We deliberately keep typography (font-family,
// font-weight, font-style, text-decoration) so emphasis survives.
const STYLE_PROPS_TO_STRIP = [
  "color",
  "background",
  "background-color",
  "background-image",
  "background-clip",
  "-webkit-background-clip",
  "-webkit-text-fill-color",
  "border-color",
  "outline-color",
  "box-shadow",
  "text-shadow",
  "filter",
  "mix-blend-mode",
];

// Tags we drop entirely — they only carry presentational style.
const TAGS_TO_UNWRAP = new Set(["FONT"]);

function stripInlineStyles(node) {
  if (!(node instanceof Element)) return;

  // Drop legacy presentation tags (font / b / etc. carrying color attrs).
  if (TAGS_TO_UNWRAP.has(node.tagName) && node.parentNode) {
    while (node.firstChild) node.parentNode.insertBefore(node.firstChild, node);
    node.parentNode.removeChild(node);
    return;
  }

  // Strip presentational HTML attrs.
  ["color", "bgcolor", "background"].forEach((a) => {
    if (node.hasAttribute(a)) node.removeAttribute(a);
  });

  // Strip targeted style declarations from inline `style="…"`.
  const styleAttr = node.getAttribute("style");
  if (styleAttr) {
    const cleaned = styleAttr
      .split(";")
      .map((s) => s.trim())
      .filter(Boolean)
      .filter((decl) => {
        const idx = decl.indexOf(":");
        if (idx === -1) return true;
        const prop = decl.slice(0, idx).trim().toLowerCase();
        return !STYLE_PROPS_TO_STRIP.includes(prop);
      })
      .join("; ");
    if (cleaned) node.setAttribute("style", cleaned);
    else node.removeAttribute("style");
  }

  // Class-based theming is the bigger problem — Tailwind's `text-white`
  // / `bg-zinc-900` carry colour. Inlining the resolved colour and
  // dropping the class would be exhaustive but expensive; instead we
  // strip class names entirely. The recipient gets unstyled text but
  // keeps the structure (paragraphs, lists, links, headings).
  if (node.hasAttribute("class")) node.removeAttribute("class");
}

function walkAndClean(root) {
  // Iterative pre-order — safer than recursion when the tree is deep.
  const stack = [root];
  while (stack.length) {
    const cur = stack.pop();
    if (!cur) continue;
    if (cur.nodeType === Node.ELEMENT_NODE) {
      stripInlineStyles(cur);
      // Push children in reverse so we process them in order.
      for (let i = cur.childNodes.length - 1; i >= 0; i--) {
        stack.push(cur.childNodes[i]);
      }
    }
  }
}

function buildSanitisedHTML(selection) {
  const range = selection.getRangeAt(0);
  const fragment = range.cloneContents();
  // Wrap in a div so we can walk a single root.
  const wrapper = document.createElement("div");
  wrapper.appendChild(fragment);
  walkAndClean(wrapper);
  return wrapper.innerHTML;
}

function onCopy(event) {
  try {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
      return;  // Programmatic copies, empty selections — leave alone.
    }
    const text = selection.toString();
    if (!text || text.length === 0) return;

    const cleanHTML = buildSanitisedHTML(selection);

    // ClipboardEvent allows us to override what gets copied. We must
    // call preventDefault() AFTER we set the data, otherwise some
    // browsers ignore the override.
    if (event.clipboardData) {
      event.clipboardData.setData("text/plain", text);
      event.clipboardData.setData("text/html", cleanHTML);
      event.preventDefault();
    }
  } catch (err) {
    // Anything goes wrong → fall through to the browser's default
    // copy handling. Better unstyled-but-correct than broken.
    // eslint-disable-next-line no-console
    console.warn("clipboard sanitiser failed, using default copy:", err);
  }
}

export function installClipboardSanitiser() {
  if (installed) return;
  if (typeof window === "undefined" || typeof document === "undefined") return;
  document.addEventListener("copy", onCopy);
  installed = true;
}
