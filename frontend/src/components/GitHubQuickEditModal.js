/* eslint-disable react-hooks/exhaustive-deps */
import { useEffect, useState } from "react";
import { Loader2, Folder, FileText, ChevronLeft, Save, X, ExternalLink, GitBranch } from "lucide-react";
import { toast } from "sonner";
import api from "../lib/api";

/**
 * GitHubQuickEditModal — full-screen-ish overlay for picking a repo,
 * navigating into a directory, opening a file, editing its contents,
 * and committing the change as a single-file PUT to GitHub's contents
 * API. Skips the Mission flow entirely — useful for surgical fixes
 * (typo, README update, config tweak) where booting an autonomous
 * agent loop is overkill.
 *
 * State machine:
 *   repos  → user picked a repo
 *   tree   → user is browsing a directory
 *   file   → user opened a file, editing contents
 *
 * The path / branch / sha state is the source of truth at every level
 * so the breadcrumb and the commit endpoint always agree about what
 * exactly is being saved.
 */
export default function GitHubQuickEditModal({ orgId, onClose }) {
  const [phase, setPhase] = useState("repos"); // repos | tree | file
  const [loading, setLoading] = useState(false);
  const [repos, setRepos] = useState([]);
  const [repo, setRepo] = useState(null); // {full_name, default_branch, ...}
  const [branch, setBranch] = useState("");
  const [path, setPath] = useState("");
  const [entries, setEntries] = useState([]);
  const [file, setFile] = useState(null); // {name, path, sha, content, ...}
  const [editedContent, setEditedContent] = useState("");
  const [commitMsg, setCommitMsg] = useState("");
  const [saving, setSaving] = useState(false);

  // Initial load: list repos
  useEffect(() => {
    (async () => {
      setLoading(true);
      try {
        const { data } = await api.get(`/github/repos?org_id=${encodeURIComponent(orgId)}`);
        setRepos(data?.repos || []);
      } catch (e) {
        toast.error(e?.response?.data?.detail || "Could not list repos");
      } finally { setLoading(false); }
    })();
  }, [orgId]);

  const openRepo = async (r) => {
    setRepo(r);
    setBranch(r.default_branch || "main");
    setPath("");
    setPhase("tree");
    await loadTree(r.full_name, r.default_branch || "main", "");
  };

  const loadTree = async (fullName, ref, p) => {
    setLoading(true);
    try {
      const { data } = await api.get(
        `/github/tree?org_id=${encodeURIComponent(orgId)}` +
        `&repo=${encodeURIComponent(fullName)}` +
        `&path=${encodeURIComponent(p)}` +
        `&ref=${encodeURIComponent(ref)}`,
      );
      setEntries(data?.entries || []);
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Could not load directory");
    } finally { setLoading(false); }
  };

  const openEntry = async (e) => {
    if (e.type === "dir") {
      const newPath = e.path;
      setPath(newPath);
      await loadTree(repo.full_name, branch, newPath);
      return;
    }
    // File
    setLoading(true);
    try {
      const { data } = await api.get(
        `/github/tree?org_id=${encodeURIComponent(orgId)}` +
        `&repo=${encodeURIComponent(repo.full_name)}` +
        `&path=${encodeURIComponent(e.path)}` +
        `&ref=${encodeURIComponent(branch)}`,
      );
      setFile(data);
      setEditedContent(data.content || "");
      setCommitMsg(`Update ${e.name}`);
      setPhase("file");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not open file");
    } finally { setLoading(false); }
  };

  const goUp = async () => {
    if (phase === "file") {
      setPhase("tree");
      setFile(null);
      setEditedContent("");
      return;
    }
    if (phase === "tree") {
      if (path) {
        const parent = path.split("/").slice(0, -1).join("/");
        setPath(parent);
        await loadTree(repo.full_name, branch, parent);
      } else {
        setPhase("repos");
        setRepo(null);
      }
    }
  };

  const onSave = async () => {
    if (!commitMsg.trim()) {
      toast.error("Commit message required");
      return;
    }
    if (file?.is_binary) {
      toast.error("Can't edit binary files in quick-edit");
      return;
    }
    setSaving(true);
    try {
      const { data } = await api.post("/github/commit", {
        org_id: orgId,
        repo: repo.full_name,
        branch,
        path: file.path,
        content: editedContent,
        message: commitMsg.trim(),
        sha: file.sha,
      });
      toast.success("Committed");
      // Update sha so a subsequent save on the same file works
      if (data?.new_sha) {
        setFile({ ...file, sha: data.new_sha });
      }
      // Show the commit URL
      if (data?.commit_url) {
        toast.success(
          <span>
            View commit on GitHub:{" "}
            <a href={data.commit_url} target="_blank" rel="noopener noreferrer"
               className="underline">
              open <ExternalLink className="w-3 h-3 inline" />
            </a>
          </span>,
          { duration: 6000 },
        );
      }
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Commit failed");
    } finally { setSaving(false); }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: "rgba(0,0,0,0.6)" }}
      onClick={onClose}
      data-testid="gh-quick-edit-modal"
    >
      <div
        className="sp-glass-strong rounded-2xl w-full max-w-4xl max-h-[92vh] overflow-hidden flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-white/10">
          <div className="flex items-center gap-3 min-w-0">
            {phase !== "repos" && (
              <button onClick={goUp} className="sp-btn sp-btn-ghost !p-1.5"
                      data-testid="gh-quick-back">
                <ChevronLeft className="w-4 h-4" />
              </button>
            )}
            <div className="min-w-0">
              <div className="text-sm font-medium truncate">
                {phase === "repos" && "Quick edit — pick a repository"}
                {phase === "tree" && repo?.full_name}
                {phase === "file" && (file?.path || "")}
              </div>
              {phase === "tree" && (
                <div className="text-[11px] sp-subtle flex items-center gap-1 truncate">
                  <GitBranch className="w-3 h-3" /> {branch} / {path || "(root)"}
                </div>
              )}
              {phase === "file" && file && (
                <div className="text-[11px] sp-subtle flex items-center gap-2 truncate">
                  <span><GitBranch className="w-3 h-3 inline" /> {branch}</span>
                  <span>{(file.size || editedContent.length || 0).toLocaleString()} bytes</span>
                  {file.html_url && (
                    <a href={file.html_url} target="_blank" rel="noopener noreferrer"
                       className="underline hover:text-orange-300">view on GitHub</a>
                  )}
                </div>
              )}
            </div>
          </div>
          <button onClick={onClose} className="sp-btn sp-btn-ghost !p-1.5"
                  data-testid="gh-quick-close" aria-label="Close">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-auto">
          {loading ? (
            <div className="p-12 text-center sp-subtle">
              <Loader2 className="w-6 h-6 animate-spin mx-auto opacity-50" />
            </div>
          ) : phase === "repos" ? (
            <div className="p-3">
              {repos.length === 0 ? (
                <div className="text-sm sp-subtle p-6 text-center">
                  No repositories found on the connected account.
                </div>
              ) : (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  {repos.map((r) => (
                    <button
                      key={r.full_name}
                      onClick={() => openRepo(r)}
                      className="text-left p-3 rounded-lg hover:bg-white/5 border border-white/10"
                      data-testid={`gh-repo-${r.full_name}`}
                    >
                      <div className="font-medium text-sm truncate">{r.full_name}</div>
                      <div className="text-[11px] sp-subtle truncate mt-0.5">
                        {r.private ? "private" : "public"}
                        {r.language ? ` · ${r.language}` : ""}
                        {r.default_branch ? ` · ${r.default_branch}` : ""}
                      </div>
                      {r.description && (
                        <div className="text-xs sp-subtle mt-1 line-clamp-2">{r.description}</div>
                      )}
                    </button>
                  ))}
                </div>
              )}
            </div>
          ) : phase === "tree" ? (
            <div className="p-3">
              {entries.length === 0 ? (
                <div className="text-sm sp-subtle p-6 text-center">(empty)</div>
              ) : (
                <div className="space-y-0.5">
                  {entries.map((e) => (
                    <button
                      key={e.path}
                      onClick={() => openEntry(e)}
                      className="w-full text-left flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-white/5 text-sm"
                      data-testid={`gh-entry-${e.path}`}
                    >
                      {e.type === "dir"
                        ? <Folder className="w-4 h-4 text-amber-400/80" />
                        : <FileText className="w-4 h-4 opacity-60" />}
                      <span className="flex-1 truncate">{e.name}</span>
                      {e.size > 0 && (
                        <span className="text-[10px] sp-subtle">{e.size.toLocaleString()}b</span>
                      )}
                    </button>
                  ))}
                </div>
              )}
            </div>
          ) : phase === "file" && file ? (
            file.is_binary ? (
              <div className="p-6 text-center sp-subtle text-sm">
                Binary file — quick edit not supported. Open on GitHub to view.
              </div>
            ) : (
              <div className="p-3 space-y-2">
                <textarea
                  value={editedContent}
                  onChange={(e) => setEditedContent(e.target.value)}
                  className="w-full h-[55vh] rounded-lg bg-black/40 border border-white/10 p-3 font-mono text-xs leading-relaxed focus:outline-none focus:border-orange-500/50"
                  spellCheck={false}
                  data-testid="gh-file-editor"
                />
                <input
                  type="text"
                  placeholder="Commit message"
                  value={commitMsg}
                  onChange={(e) => setCommitMsg(e.target.value)}
                  className="w-full rounded-lg bg-black/30 border border-white/10 px-3 py-2 text-sm focus:outline-none focus:border-orange-500/50"
                  data-testid="gh-commit-msg"
                />
              </div>
            )
          ) : null}
        </div>

        {/* Footer */}
        {phase === "file" && file && !file.is_binary && (
          <div className="flex items-center justify-end gap-2 px-5 py-3 border-t border-white/10">
            <button onClick={onClose} className="sp-btn sp-btn-ghost text-sm"
                    data-testid="gh-quick-cancel">Cancel</button>
            <button
              onClick={onSave}
              disabled={saving || !commitMsg.trim()}
              className="sp-btn sp-btn-primary text-sm disabled:opacity-50"
              data-testid="gh-quick-commit"
            >
              {saving
                ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Committing…</>
                : <><Save className="w-3.5 h-3.5" /> Commit to {branch}</>}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
