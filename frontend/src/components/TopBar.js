import React from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { useTheme } from "../context/ThemeContext";
import { Sparkles, LogOut, Sun, Moon } from "lucide-react";

export default function TopBar({ children, right = null }) {
  const { user, logout } = useAuth();
  const { mode, toggle } = useTheme();
  const nav = useNavigate();
  return (
    <header className="relative z-10 border-b" style={{ borderColor: "var(--border-soft)" }}>
      <div className="bg-black/0 backdrop-blur-xl">
        <div className="max-w-7xl mx-auto flex items-center justify-between px-6 py-3 gap-4">
          <div className="flex items-center gap-3 min-w-0">
            <Link to="/app" className="flex items-center gap-2 shrink-0" data-testid="topbar-brand">
              <div className="w-8 h-8 rounded-md sp-glass-strong flex items-center justify-center">
                <Sparkles className="w-4 h-4" />
              </div>
              <span className="text-base font-medium tracking-tight hidden sm:block">SoulPrint</span>
            </Link>
            <div className="h-5 w-px mx-1 hidden sm:block" style={{ background: "var(--border-soft)" }} />
            <div className="truncate text-sm sp-subtle">{children}</div>
          </div>
          <div className="flex items-center gap-2">
            {right}
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
