/**
 * GlobalNoteButton — floating "+ New note" FAB on every org screen.
 *
 * Why this lives at the document root via React Portal:
 *   • Parent containers use `backdrop-filter: blur` (the `sp-glass-strong`
 *     class) which creates a strict CSS stacking context. Any absolutely
 *     positioned descendant gets trapped *under* sibling glass cards.
 *   • Mounting at `document.body` puts the FAB above every glass surface
 *     without needing absurd z-index gymnastics on every parent.
 *
 * Why the editor lives inside this component (not delegated to NotesPage):
 *   • NotesPage is rendered inside a CollapsibleSection on OrgPage that
 *     UNMOUNTS its children when collapsed (which is the default state
 *     for a fully-configured org). If we delegate via a URL hash, the
 *     hash event has no listener and the modal never opens.
 *   • Mounting the editor here makes the FAB self-sufficient: it works
 *     on every screen, regardless of which sections are expanded.
 */
import React, { useState } from "react";
import { createPortal } from "react-dom";
import { NotebookPen } from "lucide-react";
import { NoteEditor } from "../pages/NotesPage";
import { toast } from "sonner";

export default function GlobalNoteButton({ orgId }) {
  const [hover, setHover] = useState(false);
  const [open, setOpen] = useState(false);

  if (!orgId) return null;

  const fab = createPortal(
    <button
      type="button"
      onClick={() => setOpen(true)}
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

  return (
    <>
      {fab}
      {open && (
        <NoteEditor
          orgId={orgId}
          initial={null}
          onClose={() => setOpen(false)}
          onSaved={() => {
            setOpen(false);
            toast.success("Note saved");
          }}
        />
      )}
    </>
  );
}
