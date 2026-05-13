import React from "react";
import { Link } from "react-router-dom";
import Logo from "./Logo";
import { ArrowRight } from "lucide-react";

/**
 * Shared chrome for public legal pages (Privacy, Terms, Sub-processors).
 *
 * Keeps the visual treatment identical across all three so prospects
 * who land on one can navigate between them without context switching,
 * and so legal review teams see consistent branding + last-reviewed
 * dates in the same place. Auth-agnostic header (no avatar / no app
 * shell) so the page reads as a public document.
 */
export default function LegalShell({
  eyebrow,
  title,
  intro,
  lastReviewed,
  children,
  testid,
}) {
  return (
    <div className="min-h-screen" data-testid={testid}>
      <header
        className="border-b border-white/5 sticky top-0 z-30 backdrop-blur-md"
        style={{
          background: "color-mix(in srgb, var(--bg) 75%, transparent)",
          paddingTop: "calc(env(safe-area-inset-top, 0px))",
        }}
      >
        <div className="max-w-4xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between">
          <Link to="/" className="inline-flex items-center gap-2">
            <Logo variant="wordmark" className="h-6 w-auto" />
          </Link>
          <nav className="flex items-center gap-1">
            <Link to="/security" className="sp-btn sp-btn-ghost text-xs">
              Security
            </Link>
            <Link to="/auth" className="sp-btn sp-btn-primary text-xs">
              Get started <ArrowRight className="w-3 h-3" />
            </Link>
          </nav>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-4 sm:px-6 py-10 sm:py-14">
        <div className="mb-10">
          {eyebrow && (
            <div className="inline-flex items-center gap-1.5 sp-glass rounded-full px-3 py-1 text-[10px] uppercase tracking-widest sp-subtle mb-4">
              {eyebrow}
            </div>
          )}
          <h1 className="text-3xl sm:text-5xl font-medium tracking-tight leading-[1.05] mb-3">
            {title}
          </h1>
          {intro && (
            <p className="text-base sm:text-lg sp-subtle leading-relaxed max-w-2xl">
              {intro}
            </p>
          )}
          {lastReviewed && (
            <div className="mt-3 text-[11px] sp-subtle">
              Last reviewed: <b>{lastReviewed}</b>
            </div>
          )}
        </div>

        {children}

        {/* Cross-links so prospects can move between legal docs. */}
        <footer className="mt-16 pt-6 border-t border-white/5 flex flex-wrap gap-3 text-xs sp-subtle">
          <Link to="/legal/privacy" className="hover:opacity-100 opacity-80">
            Privacy Policy
          </Link>
          <span>·</span>
          <Link to="/legal/terms" className="hover:opacity-100 opacity-80">
            Terms & Conditions
          </Link>
          <span>·</span>
          <Link to="/legal/sub-processors" className="hover:opacity-100 opacity-80">
            Sub-processors
          </Link>
          <span>·</span>
          <Link to="/security" className="hover:opacity-100 opacity-80">
            Security
          </Link>
          <span className="ml-auto">© {new Date().getFullYear()} ArcheForge, LLC</span>
        </footer>
      </main>
    </div>
  );
}

/** Section heading + body wrapper used in all three legal pages. */
export function Section({ id, title, children }) {
  return (
    <section id={id} className="mb-10 scroll-mt-24">
      <h2 className="text-xl sm:text-2xl font-medium tracking-tight mb-3">{title}</h2>
      <div className="text-sm sp-subtle leading-relaxed space-y-3">{children}</div>
    </section>
  );
}
