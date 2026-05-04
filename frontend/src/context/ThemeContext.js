import React, { createContext, useContext, useEffect, useState, useCallback } from "react";

const ThemeContext = createContext(null);
const STORAGE_KEY = "soulprint_theme";

const apply = (mode) => {
  if (typeof document === "undefined") return;
  document.documentElement.setAttribute("data-theme", mode);
  document.documentElement.style.colorScheme = mode;
};

export const ThemeProvider = ({ children }) => {
  const [mode, setMode] = useState(() => {
    if (typeof window === "undefined") return "dark";
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved === "light" || saved === "dark") {
        // Apply BEFORE React paints so the first render (and any <img>
        // subscribed to data-theme) picks up the right variant on mount.
        apply(saved);
        return saved;
      }
    } catch {/* ignore */}
    const prefersLight = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches;
    const initial = prefersLight ? "light" : "dark";
    apply(initial);
    return initial;
  });

  useEffect(() => { apply(mode); }, [mode]);

  const setTheme = useCallback((next) => {
    setMode(next);
    try { localStorage.setItem(STORAGE_KEY, next); } catch {/* ignore */}
  }, []);

  const toggle = useCallback(() => {
    setMode((m) => {
      const next = m === "dark" ? "light" : "dark";
      try { localStorage.setItem(STORAGE_KEY, next); } catch {/* ignore */}
      return next;
    });
  }, []);

  return (
    <ThemeContext.Provider value={{ mode, setTheme, toggle }}>
      {children}
    </ThemeContext.Provider>
  );
};

export const useTheme = () => useContext(ThemeContext);
