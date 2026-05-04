import React, { useEffect, useState } from "react";

/**
 * SoulPrint Engine Pro brand mark.
 *
 * Variants:
 *   • `mark` (default) — the orange swirl glyph alone. Renders well on
 *     ANY background, no theme branching needed.
 *   • `wordmark` — the full "swirl + SOULPRINT ENGINE PRO" lockup. The
 *     wordmark text auto-switches between a white-text version (for dark
 *     surfaces) and a charcoal-text version (for light surfaces) by
 *     subscribing to the document's `data-theme` attribute, which the
 *     ThemeContext keeps in sync with the user's preference.
 *
 * Tailwind size classes (`w-9`, `h-8 w-auto`, …) are honoured via className.
 */
function useDocumentTheme() {
  const [theme, setTheme] = useState(() => {
    if (typeof document === "undefined") return "dark";
    return document.documentElement.getAttribute("data-theme") || "dark";
  });
  useEffect(() => {
    if (typeof document === "undefined") return;
    const root = document.documentElement;
    const update = () =>
      setTheme(root.getAttribute("data-theme") || "dark");
    update();
    const obs = new MutationObserver(update);
    obs.observe(root, { attributes: true, attributeFilter: ["data-theme"] });
    return () => obs.disconnect();
  }, []);
  return theme;
}

export default function Logo({
  className = "w-4 h-4",
  variant = "mark",
  alt = "SoulPrint Engine Pro",
}) {
  const theme = useDocumentTheme();
  let src;
  if (variant === "wordmark") {
    // Light surfaces need the dark-text version so the wordmark stays
    // readable against the cream/white page background.
    src = theme === "light" ? "/soulprint-logo-dark.png" : "/soulprint-logo.png";
  } else {
    // The orange swirl reads cleanly on any background.
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
