import React, { useState, useEffect } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { useTheme } from "../context/ThemeContext";
import { LogOut, Sun, Moon, MapPin } from "lucide-react";
import Logo from "./Logo";
import { captureUserLocation, getCachedLocation, setCachedLocation } from "../lib/api";
import { toast } from "sonner";

export default function TopBar({ children, right = null }) {
  const { user, logout } = useAuth();
  const { mode, toggle } = useTheme();
  const nav = useNavigate();
  const [loc, setLoc] = useState(getCachedLocation());

  useEffect(() => {
    // Re-read on mount in case another tab updated localStorage
    setLoc(getCachedLocation());
  }, []);

  const askLocation = async () => {
    if (loc) {
      // Already on — clicking again clears the consent
      setCachedLocation("");
      setLoc("");
      toast.success("Location turned off");
      return;
    }
    const result = await captureUserLocation();
    if (result) { setLoc(result); toast.success(`Location: ${result}`); }
    else toast.error("Location unavailable or denied");
  };
  return (
    <header className="relative z-10 border-b" style={{ borderColor: "var(--border-soft)" }}>
      <div className="bg-black/0 backdrop-blur-xl">
        <div className="max-w-7xl mx-auto flex items-center justify-between px-4 sm:px-6 py-3 gap-2 sm:gap-4">
          <div className="flex items-center gap-3 min-w-0">
            <Link to="/app" className="flex items-center gap-2 shrink-0" data-testid="topbar-brand">
              <div className="w-9 h-9 rounded-lg sp-glass-strong flex items-center justify-center p-0.5">
                <Logo className="w-full h-full" />
              </div>
              <span className="text-base font-medium tracking-tight hidden sm:block">SoulPrint</span>
            </Link>
            <div className="h-5 w-px mx-1 hidden sm:block" style={{ background: "var(--border-soft)" }} />
            <div className="truncate text-sm sp-subtle">{children}</div>
          </div>
          <div className="flex items-center gap-2">
            {right}
            <button
              onClick={askLocation}
              className={`sp-btn text-xs ${loc ? "sp-btn-primary" : "sp-btn-ghost"}`}
              data-testid="location-toggle"
              title={loc ? `Sharing ${loc} with agents — click to stop` : "Share my location with agents (for accurate local time / regional answers)"}
            >
              <MapPin className="w-4 h-4" />
              <span className="hidden md:inline truncate max-w-[120px]">{loc || "Use my location"}</span>
            </button>
            <button
              onClick={toggle}
              className="sp-btn sp-btn-ghost text-xs"
              data-testid="theme-toggle"
              aria-label="Toggle theme"
              title={mode === "dark" ? "Switch to light mode" : "Switch to dark mode"}
            >
              {mode === "dark" ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
            </button>
            <div className="hidden md:flex items-center gap-2 px-3 py-1.5 rounded-full sp-glass text-xs">
              <span className="w-2 h-2 rounded-full bg-emerald-400/80" />
              <span>{user?.name || user?.email}</span>
            </div>
            <button
              onClick={async () => { await logout(); nav("/"); }}
              className="sp-btn sp-btn-ghost text-xs"
              data-testid="topbar-logout"
              title="Sign out"
            >
              <LogOut className="w-4 h-4" />
              <span className="hidden sm:inline">Sign out</span>
            </button>
          </div>
        </div>
      </div>
    </header>
  );
}
