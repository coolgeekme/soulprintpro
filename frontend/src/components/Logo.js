import React, { useEffect, useState } from "react";
import { useTheme } from "../context/ThemeContext";

/**
 * Foundry brand mark.
 *
 * Variants:
 *   • `mark` (default) — orange swirl glyph. Reads on ANY background.
 *     We keep the existing iconic swirl until the next visual identity
 *     update — it's brand-agnostic enough to outlive the rename.
 *   • `wordmark` — typographic "FOUNDRY" lockup + tiny "by SoulPrint
 *     Engine" subline. Text-based intentionally — placeholder for the
 *     final hand-drawn wordmark while keeping page rhythm intact.
 *     Auto-switches between white-on-dark and charcoal-on-light by
 *     reading the active ThemeContext.
 *
 * Why text-based wordmark instead of an SVG file:
 *   - Zero asset round-trip on first paint (no LCP hit)
 *   - Adapts to dark/light theme + container size automatically
 *   - Easy swap-in when the final wordmark art lands — just replace
 *     this component's wordmark JSX with an <img src=".../wordmark.svg" />
 */
function useActiveTheme() {
  const ctx = useTheme();
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
  alt = "Foundry",
  showSubline = true,
}) {
  const theme = useActiveTheme();
  // Keep the swirl glyph as the icon — neutral, ownable, and survives
  // the rename. We render it via <img> for crisp pixel art at small sizes.
  const markSrc = "/soulprint-mark.png";

  if (variant !== "wordmark") {
    return (
      <img
        src={markSrc}
        alt={alt}
        draggable={false}
        className={`${className} object-contain select-none`}
      />
    );
  }

  // Wordmark — typographic lockup. We size it relative to the
  // container so callers can keep using `className="h-9 w-auto"` etc.
  const inkClass = theme === "light" ? "text-stone-900" : "text-stone-50";
  const subClass = theme === "light" ? "text-stone-500" : "text-stone-400";
  return (
    <div
      className={`${className} inline-flex items-center gap-2 select-none`}
      aria-label={alt}
      data-testid="brand-wordmark"
    >
      <img
        src={markSrc}
        alt=""
        draggable={false}
        className="h-full w-auto object-contain"
      />
      <span className="flex flex-col leading-none">
        <span
          className={`${inkClass} font-semibold tracking-[0.18em] uppercase`}
          style={{
            fontFamily:
              '"Cormorant Garamond", "EB Garamond", ui-serif, serif',
            fontSize: "1.15em",
          }}
        >
          Foundry
        </span>
        {showSubline && (
          <span
            className={`${subClass} uppercase mt-0.5`}
            style={{
              fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif",
              fontSize: "0.42em",
              letterSpacing: "0.32em",
            }}
          >
            built for agency teams
          </span>
        )}
      </span>
    </div>
  );
}
