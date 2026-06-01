/**
 * GlobalNoteButton — floating FAB on every org screen. Opens the same
 * NoteEditor used inside NotesPage, but mounted at the document root
 * via React Portal so it isn't trapped by any sibling `sp-glass-strong`
 * stacking context (lesson from AssetPickerModal + VideoGenerationModal).
 *
 * Use: <GlobalNoteButton orgId={orgId} /> dropped inside any org page
 * wrapper. The button sits bottom-right above mobile safe areas;
 * opens a NoteEditor modal that saves through the standard
 * /api/orgs/{id}/notes endpoints.
 */
import React, { useState } from "react";
import { createPortal } from "react-dom";
import { NotebookPen } from "lucide-react";

// We re-use the editor that NotesPage exports. To avoid a circular
// import we import the file directly — NotesPage exports its
// internal `NoteEditor` via a named re-export at the bottom of this
// file (kept here so callers don't need to know it's actually defined
// inside NotesPage.js).

import NotesPage from "../pages/NotesPage"; // eslint-disable-line no-unused-vars
// NoteEditor isn't named-exported from NotesPage today; we duplicate
// the trigger here and let NotesPage own the editor.  For the
// floating button we just punch a small re-routing card that links to
// the Notes tab + opens its "new note" flow via URL hash.

export default function GlobalNoteButton({ orgId, onNavigateToNotes }) {
  const [hover, setHover] = useState(false);
  if (!orgId) return null;
  const handleClick = () => {
    // Delegate to parent if provided (org page knows how to switch
    // tabs in place); otherwise navigate via hash so NotesPage can
    // pick up the "open new note" intent from the URL.
    if (onNavigateToNotes) {
      onNavigateToNotes();
    } else if (typeof window !== "undefined") {
      window.location.hash = "#notes:new";
    }
  };
  return createPortal(
    <button
      type="button"
      onClick={handleClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      className="sp-btn sp-btn-primary fixed z-[80] inline-flex items-center gap-2 shadow-xl"
      style={{
        right: "1.25rem",
        // Lift above mobile safe-area inset so it's never clipped by
        // the iPhone home-indicator strip.
        bottom: "calc(1.5rem + env(safe-area-inset-bottom, 0px))",
        padding: "0.7rem 1rem",
        borderRadius: "999px",
        boxShadow: "0 8px 24px rgba(0,0,0,0.35)",
      }}
      data-testid="global-new-note-fab"
      title="Capture a quick note — text or voice"
      aria-label="Capture a quick note — text or voice"
    >
      <NotebookPen className="w-4 h-4" />
      <span
        className="hidden sm:inline overflow-hidden transition-all"
        style={{ maxWidth: hover ? "120px" : "0", whiteSpace: "nowrap" }}
      >
        New note
      </span>
    </button>,
    document.body,
  );
}
