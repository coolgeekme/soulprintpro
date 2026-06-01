/**
 * NotesPage — per-org Notes tab. Lists, searches, creates, edits,
 * deletes notes. Voice capture via the MediaRecorder Web API; falls
 * back to text-only when the browser doesn't support audio recording
 * (e.g. macOS Safari without mic permission).
 *
 * Wires into the org page as a tab. The companion floating "+ Note"
 * button lives in `GlobalNoteButton.js` and can be dropped on any org
 * screen so users can capture mid-thought without leaving context.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Loader2, Mic, MicOff, Send, Trash2, Pencil, X, Search, NotebookPen, Users, Lock, Globe2, Bot, EyeOff } from "lucide-react";
import { toast } from "sonner";
import api from "../lib/api";

const SCOPE_META = {
  personal: { label: "Personal", icon: Lock,    hint: "Only you" },
  team:     { label: "Team",     icon: Users,   hint: "All team members" },
  org:      { label: "Org",      icon: Globe2,  hint: "All org members" },
};

export default function NotesPage({ orgId }) {
  const [notes, setNotes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState(null);  // note object or "new"
  const [debouncedQ, setDebouncedQ] = useState("");

  useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(query), 250);
    return () => clearTimeout(t);
  }, [query]);

  const fetchNotes = async () => {
    setLoading(true);
    try {
      const params = debouncedQ ? `?q=${encodeURIComponent(debouncedQ)}` : "";
      const { data } = await api.get(`/orgs/${orgId}/notes${params}`);
      setNotes(data || []);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't load notes");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!orgId) return;
    fetchNotes();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orgId, debouncedQ]);

  // Honor `#notes:new` hash so the floating "+ New note" FAB on the
  // org page can pop the editor without needing a parent ref.
  useEffect(() => {
    const handleHash = () => {
      if (typeof window === "undefined") return;
      if (window.location.hash === "#notes:new") {
        setEditing("new");
        // Clear so subsequent re-renders don't keep re-opening.
        window.history.replaceState(null, "", window.location.pathname + window.location.search);
      }
    };
    handleHash();
    window.addEventListener("hashchange", handleHash);
    return () => window.removeEventListener("hashchange", handleHash);
  }, []);

  const upsertLocal = (note) => {
    setNotes((prev) => {
      const idx = prev.findIndex((n) => n.id === note.id);
      if (idx === -1) return [note, ...prev];
      const next = [...prev]; next[idx] = note; return next;
    });
  };

  const deleteLocal = (id) => setNotes((prev) => prev.filter((n) => n.id !== id));

  return (
    <div className="space-y-4" data-testid="notes-page">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h1 className="sp-display text-2xl flex items-center gap-2">
            <NotebookPen className="w-5 h-5 text-orange-300" /> Notes
          </h1>
          <p className="text-xs sp-subtle mt-0.5">
            Capture your thoughts, brand musings, and meeting takeaways. Agents read them as context unless you mark a note private.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setEditing("new")}
          className="sp-btn sp-btn-primary text-sm"
          data-testid="new-note-btn"
        >
          + New note
        </button>
      </div>

      <div className="relative">
        <Search className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 sp-subtle pointer-events-none" />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search title, body, or tags…"
          className="w-full sp-glass rounded-xl pl-9 pr-3 py-2 text-sm focus:sp-focus"
          style={{ background: "var(--input-bg)", color: "var(--fg)" }}
          data-testid="notes-search-input"
        />
      </div>

      {loading ? (
        <div className="flex justify-center py-12"><Loader2 className="w-6 h-6 animate-spin opacity-50" /></div>
      ) : notes.length === 0 ? (
        <div className="sp-glass rounded-2xl p-10 text-center">
          <NotebookPen className="w-8 h-8 mx-auto opacity-40 mb-3" />
          <div className="text-sm sp-subtle">
            {debouncedQ ? "No notes match your search." : "No notes yet. Drop your first idea above."}
          </div>
        </div>
      ) : (
        <ul className="space-y-2" data-testid="notes-list">
          {notes.map((n) => (
            <NoteRow
              key={n.id}
              note={n}
              onEdit={() => setEditing(n)}
              onDelete={() => deleteLocal(n.id)}
            />
          ))}
        </ul>
      )}

      {editing && (
        <NoteEditor
          orgId={orgId}
          initial={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={(note) => { upsertLocal(note); setEditing(null); }}
        />
      )}
    </div>
  );
}

function NoteRow({ note, onEdit, onDelete }) {
  const ScopeIcon = (SCOPE_META[note.scope] || SCOPE_META.personal).icon;
  const scopeLabel = (SCOPE_META[note.scope] || SCOPE_META.personal).label;

  const del = async () => {
    if (!window.confirm("Delete this note? This can't be undone.")) return;
    try {
      await api.delete(`/orgs/${note.org_id}/notes/${note.id}`);
      onDelete();
      toast.success("Note deleted");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't delete");
    }
  };

  return (
    <li
      className="sp-glass rounded-xl p-4 flex flex-col gap-2"
      data-testid={`note-row-${note.id}`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="font-medium truncate" style={{ color: "var(--fg)" }}>
            {note.title}
          </div>
          {note.body && (
            <div className="text-sm sp-subtle mt-1 whitespace-pre-wrap line-clamp-4">
              {note.body}
            </div>
          )}
        </div>
        {note.can_edit && (
          <div className="flex items-center gap-1 shrink-0">
            <button type="button" onClick={onEdit} className="sp-btn sp-btn-ghost !p-1.5 text-xs"
                    data-testid={`note-edit-${note.id}`} title="Edit">
              <Pencil className="w-3.5 h-3.5" />
            </button>
            <button type="button" onClick={del} className="sp-btn sp-btn-ghost !p-1.5 text-xs hover:!text-red-300"
                    data-testid={`note-delete-${note.id}`} title="Delete">
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          </div>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-1.5 text-[10px] uppercase tracking-widest sp-subtle">
        <span className="inline-flex items-center gap-1">
          <ScopeIcon className="w-3 h-3" /> {scopeLabel}
        </span>
        <span>·</span>
        {note.share_with_agents ? (
          <span className="inline-flex items-center gap-1 text-orange-300/80">
            <Bot className="w-3 h-3" /> Shared with agents
          </span>
        ) : (
          <span className="inline-flex items-center gap-1">
            <EyeOff className="w-3 h-3" /> Private from agents
          </span>
        )}
        {note.source === "voice" && (
          <>
            <span>·</span>
            <span className="inline-flex items-center gap-1">
              <Mic className="w-3 h-3" /> Voice
            </span>
          </>
        )}
        {(note.tags || []).slice(0, 4).map((t) => (
          <span key={t} className="sp-chip !py-0.5 !px-1.5 !text-[9px]">#{t}</span>
        ))}
      </div>
    </li>
  );
}

/**
 * NoteEditor — modal-style sheet for create + edit. Surfaces a voice
 * recorder when supported, otherwise falls back to text-only.
 *
 * Exported (named) so the floating GlobalNoteButton can mount its own
 * editor without depending on NotesPage being currently rendered (the
 * NotesPage component is inside a CollapsibleSection on OrgPage that
 * unmounts its children when collapsed).
 */
export function NoteEditor({ orgId, initial, onClose, onSaved }) {
  const isNew = !initial;
  const [title, setTitle] = useState(initial?.title || "");
  const [body, setBody] = useState(initial?.body || "");
  const [scope, setScope] = useState(initial?.scope || "personal");
  const [shareWithAgents, setShareWithAgents] = useState(
    initial?.share_with_agents !== false,
  );
  const [tagsInput, setTagsInput] = useState((initial?.tags || []).join(", "));
  const [submitting, setSubmitting] = useState(false);

  // Voice capture state
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const recorderRef = useRef(null);
  const chunksRef = useRef([]);

  const supportsMic = useMemo(() => {
    try {
      return Boolean(navigator?.mediaDevices?.getUserMedia && window.MediaRecorder);
    } catch {
      return false;
    }
  }, []);

  const startRecording = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mime = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm"
                : MediaRecorder.isTypeSupported("audio/mp4") ? "audio/mp4"
                : "";
      const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
      chunksRef.current = [];
      rec.ondataavailable = (e) => { if (e.data && e.data.size > 0) chunksRef.current.push(e.data); };
      rec.onstop = async () => {
        // Stop the mic so the OS-level red dot goes away.
        try { stream.getTracks().forEach((t) => t.stop()); } catch {}
        const blob = new Blob(chunksRef.current, { type: rec.mimeType || "audio/webm" });
        if (blob.size === 0) {
          toast.error("Empty recording — try again.");
          return;
        }
        setTranscribing(true);
        try {
          const fd = new FormData();
          fd.append("audio", blob, `voice.${(rec.mimeType || "audio/webm").includes("mp4") ? "m4a" : "webm"}`);
          fd.append("scope", scope);
          fd.append("share_with_agents", String(shareWithAgents));
          if (title.trim()) fd.append("title", title.trim());
          if (tagsInput.trim()) fd.append("tags_csv", tagsInput.trim());
          const { data } = await api.post(`/orgs/${orgId}/notes/voice`, fd);
          onSaved(data);
          toast.success("Voice note saved");
        } catch (err) {
          toast.error(err?.response?.data?.detail || "Couldn't transcribe — try again.");
        } finally {
          setTranscribing(false);
        }
      };
      rec.start();
      recorderRef.current = rec;
      setRecording(true);
    } catch (err) {
      toast.error("Microphone access denied or unavailable.");
    }
  };

  const stopRecording = () => {
    try { recorderRef.current?.stop(); } catch {}
    setRecording(false);
  };

  // Stop mic on unmount so it doesn't keep recording after the modal closes.
  useEffect(() => () => { try { recorderRef.current?.stop(); } catch {} }, []);

  const submit = async (e) => {
    e?.preventDefault();
    if (submitting) return;
    const t = title.trim();
    const b = body.trim();
    if (!t && !b) { toast.error("Add a title or body."); return; }
    setSubmitting(true);
    try {
      const payload = {
        title: t,
        body: b,
        scope,
        share_with_agents: shareWithAgents,
        tags: tagsInput.split(",").map((s) => s.trim()).filter(Boolean),
      };
      const url = isNew
        ? `/orgs/${orgId}/notes`
        : `/orgs/${orgId}/notes/${initial.id}`;
      const { data } = isNew
        ? await api.post(url, payload)
        : await api.patch(url, payload);
      onSaved(data);
      toast.success(isNew ? "Note saved" : "Note updated");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't save");
    } finally {
      setSubmitting(false);
    }
  };

  return createPortal(
    <div
      className="fixed inset-0 z-[1000] flex items-center justify-center p-4"
      style={{ background: "rgba(0,0,0,0.72)" }}
      onClick={onClose}
      data-testid="note-editor-modal"
    >
      <form
        onSubmit={submit}
        className="sp-glass-strong rounded-2xl p-5 w-full max-w-xl max-h-[90vh] overflow-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h3 className="sp-display text-xl">
            {isNew ? "New note" : "Edit note"}
          </h3>
          <button type="button" onClick={onClose} className="sp-btn sp-btn-ghost !p-2">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="space-y-3">
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Title (optional — we'll derive one from the body)"
            disabled={submitting || transcribing}
            className="w-full sp-glass rounded-xl px-3 py-2 text-sm"
            style={{ background: "var(--input-bg)", color: "var(--fg)" }}
            data-testid="note-title-input"
          />

          <textarea
            value={body}
            onChange={(e) => setBody(e.target.value)}
            placeholder="What's on your mind? You can also tap the mic to dictate."
            rows={6}
            disabled={submitting || transcribing}
            className="w-full sp-glass rounded-xl px-3 py-2 text-sm resize-y"
            style={{ background: "var(--input-bg)", color: "var(--fg)", minHeight: "120px" }}
            data-testid="note-body-input"
          />

          {/* Voice recorder */}
          {supportsMic && isNew && (
            <div className="flex items-center gap-2 flex-wrap">
              {!recording && !transcribing && (
                <button
                  type="button"
                  onClick={startRecording}
                  className="sp-btn sp-btn-ghost text-xs"
                  data-testid="note-mic-start"
                >
                  <Mic className="w-3.5 h-3.5" /> Voice note
                </button>
              )}
              {recording && (
                <button
                  type="button"
                  onClick={stopRecording}
                  className="sp-btn text-xs"
                  style={{ background: "linear-gradient(180deg,#ef4444,#b91c1c)", color: "#fff", border: "1px solid rgba(220,38,38,0.55)" }}
                  data-testid="note-mic-stop"
                >
                  <MicOff className="w-3.5 h-3.5" /> Stop & transcribe
                </button>
              )}
              {transcribing && (
                <span className="inline-flex items-center gap-2 text-xs sp-subtle">
                  <Loader2 className="w-3.5 h-3.5 animate-spin" /> Transcribing…
                </span>
              )}
              {recording && (
                <span className="text-[10px] uppercase tracking-widest text-red-300 inline-flex items-center gap-1">
                  ● Recording — tap stop when done
                </span>
              )}
            </div>
          )}

          <input
            value={tagsInput}
            onChange={(e) => setTagsInput(e.target.value)}
            placeholder="Tags, comma-separated (e.g. brand, spring, ideas)"
            disabled={submitting || transcribing}
            className="w-full sp-glass rounded-xl px-3 py-2 text-xs"
            style={{ background: "var(--input-bg)", color: "var(--fg)" }}
            data-testid="note-tags-input"
          />

          <div>
            <div className="text-[10px] uppercase tracking-widest sp-subtle mb-1.5">Visibility</div>
            <div className="flex gap-1.5 flex-wrap">
              {Object.entries(SCOPE_META).map(([key, meta]) => {
                const Icon = meta.icon;
                const active = scope === key;
                return (
                  <button
                    key={key}
                    type="button"
                    onClick={() => setScope(key)}
                    disabled={submitting || transcribing}
                    className={`sp-chip text-xs ${active ? "!bg-orange-500/20 !border-orange-400/40 !text-white" : ""}`}
                    data-testid={`note-scope-${key}`}
                    title={meta.hint}
                  >
                    <Icon className="w-3 h-3" /> {meta.label}
                  </button>
                );
              })}
            </div>
          </div>

          <label className="flex items-start gap-2 text-xs cursor-pointer">
            <input
              type="checkbox"
              checked={shareWithAgents}
              onChange={(e) => setShareWithAgents(e.target.checked)}
              disabled={submitting || transcribing}
              className="mt-0.5"
              data-testid="note-share-with-agents"
            />
            <span>
              <span style={{ color: "var(--fg)" }}>Share with agents</span>
              <span className="sp-subtle"> — let agents in this org read this note as context when generating replies. Turn off for diary-style entries.</span>
            </span>
          </label>
        </div>

        <div className="flex justify-end gap-2 mt-5">
          <button type="button" onClick={onClose} disabled={submitting || transcribing}
                  className="sp-btn sp-btn-ghost text-xs disabled:opacity-50">Cancel</button>
          <button type="submit" disabled={submitting || transcribing || recording}
                  className="sp-btn sp-btn-primary text-xs disabled:opacity-50 inline-flex items-center gap-1.5"
                  data-testid="note-save-btn">
            {submitting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Send className="w-3.5 h-3.5" />}
            {isNew ? "Save note" : "Save changes"}
          </button>
        </div>
      </form>
    </div>,
    document.body
  );
}
