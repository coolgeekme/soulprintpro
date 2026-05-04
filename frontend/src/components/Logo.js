import React, { useEffect, useState } from "react";
import { useTheme } from "../context/ThemeContext";

/**
 * SoulPrint Engine Pro brand mark.
 *
 * Variants:
 *   • `mark` (default) — orange swirl glyph. Reads on ANY background.
 *   • `wordmark` — full "swirl + SOULPRINT ENGINE PRO" lockup. The wordmark
 *     text auto-switches between white-on-dark and charcoal-on-light by
 *     reading the active ThemeContext. A MutationObserver fallback also
 *     tracks direct `data-theme` mutations (e.g. the preview-mode theme
 *     switcher that sets the attribute without going through React).
 */
function useActiveTheme() {
  const ctx = useTheme();
  // Observe `documentElement[data-theme]` as a fallback so we pick up
  // direct-DOM theme flips (preview, tests, PWA startup paints). We then
  // prefer the live context value over the observed value — it updates
  // one tick earlier and avoids a brief mismatch on first paint.
  const [attrTheme, setAttrTheme] = useState(() => {
    if (typeof document === "undefined") return "dark";
    return document.documentElement.getAttribute("data-theme") || "dark";
  });
  useEffect(() => {
    if (typeof document === "undefined") return;
    const root = document.documentElement;
    const update = () =>
      setAttrTheme(root.getAttribute("data-theme") || "dark");
    update();
    const obs = new MutationObserver(update);
    obs.observe(root, { attributes: true, attributeFilter: ["data-theme"] });
    return () => obs.disconnect();
  }, []);
  return (ctx && ctx.mode) || attrTheme;
}

export default function Logo({
  className = "w-4 h-4",
  variant = "mark",
  alt = "SoulPrint Engine Pro",
}) {
  const theme = useActiveTheme();
  let src;
  if (variant === "wordmark") {
    src = theme === "light" ? "/soulprint-logo-dark.png" : "/soulprint-logo.png";
  } else {
    src = "/soulprint-mark.png";
  }
  return (
    <img
      src={src}
      alt={alt}
      draggable={false}
      className={`${className} object-contain select-none`}
    />
  );
}
