import React from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { Sparkles, LogOut } from "lucide-react";

export default function TopBar({ children, right = null }) {
  const { user, logout } = useAuth();
  const nav = useNavigate();
  return (
    <header className="relative z-10 border-b border-white/5 bg-black/20 backdrop-blur-xl">
      <div className="max-w-7xl mx-auto flex items-center justify-between px-6 py-3 gap-4">
        <div className="flex items-center gap-3 min-w-0">
          <Link to="/app" className="flex items-center gap-2 shrink-0" data-testid="topbar-brand">
            <div className="w-7 h-7 rounded-md sp-glass-strong flex items-center justify-center">
              <Sparkles className="w-3.5 h-3.5 text-white/90" />
            </div>
            <span className="text-sm font-medium tracking-tight text-white/95 hidden sm:block">SoulPrint</span>
          </Link>
          <div className="h-5 w-px bg-white/10 mx-1 hidden sm:block" />
          <div className="truncate text-sm sp-subtle">{children}</div>
        </div>
        <div className="flex items-center gap-2">
          {right}
          <div className="hidden md:flex items-center gap-2 px-3 py-1.5 rounded-full sp-glass text-xs">
            <span className="w-2 h-2 rounded-full bg-emerald-400/80" />
            <span className="text-white/80">{user?.name || user?.email}</span>
          </div>
          <button
            onClick={async () => { await logout(); nav("/"); }}
            className="sp-btn sp-btn-ghost text-xs"
            data-testid="topbar-logout"
            title="Sign out"
          >
            <LogOut className="w-3.5 h-3.5" />
            <span className="hidden sm:inline">Sign out</span>
          </button>
        </div>
      </div>
    </header>
  );
}
