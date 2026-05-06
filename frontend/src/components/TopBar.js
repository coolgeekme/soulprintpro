import React, { useState, useEffect } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { useTheme } from "../context/ThemeContext";
import { LogOut, Sun, Moon, MapPin, ChevronDown, HelpCircle, ShieldCheck } from "lucide-react";
import Logo from "./Logo";
import DynamicIntelligencePicker from "./DynamicIntelligencePicker";
import { captureUserLocation, getCachedLocation, setCachedLocation } from "../lib/api";
import { toast } from "sonner";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
} from "./ui/dropdown-menu";

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
    <header
      className="sticky top-0 z-40 border-b"
      style={{
        borderColor: "var(--border-soft)",
        // Pad for the iPhone notification bar when installed as a PWA. In a
        // regular browser window env(safe-area-inset-top) resolves to 0, so
        // this has no effect outside standalone mode.
        paddingTop: "env(safe-area-inset-top)",
        // Mostly-opaque background with a strong blur so content scrolling
        // under the frozen bar is clearly separated without vanishing behind
        // it — the old 88% translucency made the bar feel unanchored.
        background: "color-mix(in srgb, var(--bg) 96%, transparent)",
        backdropFilter: "blur(28px) saturate(160%)",
        WebkitBackdropFilter: "blur(28px) saturate(160%)",
        boxShadow: "0 1px 0 0 rgba(0,0,0,0.04), 0 8px 24px -16px rgba(0,0,0,0.25)",
      }}
    >
      <div>
        <div className="max-w-7xl mx-auto flex items-center justify-between px-4 sm:px-6 py-3 gap-2 sm:gap-4">
          <div className="flex items-center gap-3 min-w-0">
            <Link to="/app" className="flex items-center gap-2 shrink-0" data-testid="topbar-brand" aria-label="SoulPrint Engine Pro home">
              {/* Compact mark on mobile, full wordmark from sm: up so it
                  matches the landing & auth screens. */}
              <Logo variant="mark" className="w-9 h-9 sm:hidden" />
              <Logo variant="wordmark" className="hidden sm:block h-8 w-auto" />
            </Link>
            <div className="h-5 w-px mx-1 hidden sm:block" style={{ background: "var(--border-soft)" }} />
            <div className="truncate text-sm sp-subtle">{children}</div>
          </div>
          <div className="flex items-center gap-2">
            {right}
            {/* Global Dynamic Intelligence indicator. `nowrap` + max-width
                with ellipsis so the pill never wraps and doubles the bar
                height on narrow laptop viewports. */}
            {user && (
              <div className="hidden sm:block whitespace-nowrap overflow-hidden text-ellipsis max-w-[180px]" data-testid="topbar-dynamic-intelligence">
                <DynamicIntelligencePicker compact />
              </div>
            )}
            {/* Icon-only theme toggle — label removed to reclaim space. */}
            <button
              onClick={toggle}
              className="sp-btn sp-btn-ghost text-xs"
              data-testid="theme-toggle"
              aria-label="Toggle theme"
              title={mode === "dark" ? "Switch to light mode" : "Switch to dark mode"}
            >
              {mode === "dark" ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
            </button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  className="sp-btn sp-btn-ghost text-xs"
                  data-testid="topbar-user-menu"
                  title="Account menu"
                >
                  <span className="hidden md:inline-flex items-center gap-2">
                    <span className="w-2 h-2 rounded-full bg-emerald-400/80" />
                    <span className="truncate max-w-[140px]">{user?.name || user?.email}</span>
                    <ChevronDown className="w-3.5 h-3.5 opacity-60" />
                  </span>
                  <span className="md:hidden inline-flex items-center gap-1">
                    <span className="w-2 h-2 rounded-full bg-emerald-400/80" />
                    <ChevronDown className="w-3.5 h-3.5 opacity-60" />
                  </span>
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent
                align="end"
                className="min-w-[220px]"
                data-testid="topbar-user-menu-content"
              >
                <DropdownMenuLabel className="font-normal">
                  <div className="flex flex-col gap-0.5">
                    <span className="text-sm">{user?.name || "Signed in"}</span>
                    {user?.email && (
                      <span className="text-xs sp-subtle truncate">{user.email}</span>
                    )}
                  </div>
                </DropdownMenuLabel>
                <DropdownMenuSeparator />
                {/* Moved from top-bar surface to the menu (UX audit #1):
                    location toggle + About link live here so the nav only
                    carries essentials. */}
                <DropdownMenuItem
                  onSelect={askLocation}
                  data-testid="topbar-menu-location"
                  className="cursor-pointer"
                >
                  <MapPin className={`w-4 h-4 mr-2 ${loc ? "text-orange-400" : ""}`} />
                  {loc ? `Location · ${loc}` : "Share my location"}
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                {user?.is_superadmin && (
                  <>
                    <DropdownMenuItem
                      onSelect={() => nav("/admin")}
                      data-testid="topbar-menu-admin"
                      className="cursor-pointer"
                    >
                      <ShieldCheck className="w-4 h-4 mr-2 text-emerald-400" />
                      Admin dashboard
                    </DropdownMenuItem>
                    <DropdownMenuSeparator />
                  </>
                )}
                <DropdownMenuItem
                  onSelect={() => nav("/about")}
                  data-testid="topbar-menu-about"
                  className="cursor-pointer"
                >
                  <HelpCircle className="w-4 h-4 mr-2" />
                  What is SoulPrint Engine Pro?
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  onSelect={async () => { await logout(); nav("/"); }}
                  data-testid="topbar-menu-logout"
                  className="cursor-pointer"
                >
                  <LogOut className="w-4 h-4 mr-2" />
                  Sign out
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
      </div>
    </header>
  );
}
