import React from "react";

/**
 * SoulPrint Engine logo. Drop-in replacement for the Lucide `Sparkles` icon.
 * Tailwind size classes (`w-4 h-4`, `w-5 h-5`, …) are honoured via className.
 */
export default function Logo({ className = "w-4 h-4", alt = "SoulPrint Engine" }) {
  return (
    <img
      src="/soulprint-logo.png"
      alt={alt}
      draggable={false}
      className={`${className} object-contain select-none`}
    />
  );
}
