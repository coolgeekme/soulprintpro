import React, { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import api, { BACKEND_URL } from "../lib/api";
import TopBar from "../components/TopBar";
import { toast } from "sonner";
import {
  Send, Sparkles, Calendar, CheckCircle2, Trash2, Copy, ExternalLink,
  Download, Save, X, Loader2, Megaphone, Image as ImageIcon, Wand2,
} from "lucide-react";

// ── Platform metadata ─────────────────────────────────────────────────────
// Each platform: a friendly label, the colour we tint pills with, and a
// `composeUrl(post)` that returns either a deep-link URL or "" if the
// platform has no web composer (in which case we show "Copy text" instead).
const PLATFORMS = {
  twitter: {
    label: "X / Twitter",
    accent: "#1DA1F2",
    composeUrl: (p) =>
      `https://twitter.com/intent/tweet?text=${encodeURIComponent(joinPostText(p))}`,
  },
  linkedin: {
    label: "LinkedIn",
    accent: "#0A66C2",
    composeUrl: (p) =>
      `https://www.linkedin.com/feed/?shareActive=true&text=${encodeURIComponent(joinPostText(p))}`,
  },
  facebook: {
    label: "Facebook",
    accent: "#1877F2",
    composeUrl: (p) =>
      `https://www.facebook.com/sharer/sharer.php?quote=${encodeURIComponent(joinPostText(p))}&u=${encodeURIComponent(p.link_url || "https://soulprintengine.ai")}`,
  },
  threads: {
    label: "Threads",
    accent: "#101010",
    composeUrl: (p) =>
      `https://www.threads.net/intent/post?text=${encodeURIComponent(joinPostText(p))}`,
  },
  bluesky: {
    label: "Bluesky",
    accent: "#0085FF",
    composeUrl: (p) =>
      `https://bsky.app/intent/compose?text=${encodeURIComponent(joinPostText(p))}`,
  },
  reddit: {
    label: "Reddit",
    accent: "#FF4500",
    composeUrl: (p) =>
      `https://www.reddit.com/submit?title=${encodeURIComponent(p.text.slice(0, 110))}&text=${encodeURIComponent(joinPostText(p))}`,
  },
  pinterest: {
    label: "Pinterest",
    accent: "#E60023",
    composeUrl: (p) => {
      const media = p.media?.[0]?.url || "";
      const mediaUrl = media.startsWith("http") ? media : (media ? `${BACKEND_URL}${media}` : "");
      return `https://www.pinterest.com/pin-builder/?description=${encodeURIComponent(joinPostText(p))}${mediaUrl ? `&media=${encodeURIComponent(mediaUrl)}` : ""}`;
    },
  },
  mastodon: {
    label: "Mastodon",
    accent: "#6364FF",
    composeUrl: (p) =>
      `https://mastodon.social/share?text=${encodeURIComponent(joinPostText(p))}`,
  },
  // No-deeplink platforms — UI swaps these to "Copy + Download"
  instagram: { label: "Instagram", accent: "#E1306C", composeUrl: () => "" },
  tiktok:    { label: "TikTok",    accent: "#FE2C55", composeUrl: () => "" },
  youtube:   { label: "YouTube",   accent: "#FF0000", composeUrl: () => "" },
};
const PLATFORM_ORDER = [
  "twitter", "linkedin", "instagram", "facebook", "threads", "bluesky",
  "tiktok", "youtube", "pinterest", "reddit", "mastodon",
];

const joinPostText = (p) => {
  const tags = (p.hashtags || []).filter(Boolean).join(" ");
  return [p.text, tags].filter(Boolean).join("\n\n");
};

const formatScheduleLocal = (iso) => {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  } catch { return iso; }
};

// `<input type="datetime-local">` wants `YYYY-MM-DDTHH:mm` in local time —
// strip seconds and timezone from the ISO we get back from the API.
const isoToLocalInput = (iso) => {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  } catch { return ""; }
};

// ──────────────────────────────────────────────────────────────────────────
export default function SocialPage() {
  const { orgId } = useParams();
  const [org, setOrg] = useState(null);
  const [posts, setPosts] = useState([]);
  const [tab, setTab] = useState("draft");          // draft | queued | published
  const [loading, setLoading] = useState(true);

  // Compose form state
  const [prompt, setPrompt] = useState("");
  const [count, setCount] = useState(3);
  const [composePlatforms, setComposePlatforms] = useState(["twitter", "linkedin"]);
  const [composing, setComposing] = useState(false);

  const reload = async () => {
    try {
      const [orgRes, postsRes] = await Promise.all([
        api.get(`/orgs/${orgId}`),
        api.get(`/orgs/${orgId}/social/posts`),
      ]);
      setOrg(orgRes.data);
      setPosts(postsRes.data);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed to load social queue");
    } finally { setLoading(false); }
  };
  useEffect(() => { reload(); /* eslint-disable-next-line */ }, [orgId]);

  const counts = useMemo(() => {
    const c = { draft: 0, queued: 0, published: 0 };
    posts.forEach((p) => { if (c[p.status] !== undefined) c[p.status] += 1; });
    return c;
  }, [posts]);
  const visible = useMemo(
    () => posts.filter((p) => p.status === tab),
    [posts, tab],
  );

  const togglePlatform = (id) => {
    setComposePlatforms((prev) =>
      prev.includes(id) ? prev.filter((p) => p !== id) : [...prev, id]
    );
  };

  const compose = async (e) => {
    e.preventDefault();
    if (!prompt.trim()) return;
    if (!composePlatforms.length) { toast.error("Pick at least one platform"); return; }
    setComposing(true);
    try {
      const { data } = await api.post(`/orgs/${orgId}/social/draft`, {
        prompt: prompt.trim(),
        platforms: composePlatforms,
        count,
      });
      const fresh = data.posts || [];
      setPosts((prev) => [...fresh, ...prev]);
      setPrompt("");
      setTab("draft");
      toast.success(`Drafted ${fresh.length} post${fresh.length === 1 ? "" : "s"}`);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not draft posts");
    } finally { setComposing(false); }
  };

  const updatePost = async (id, patch) => {
    try {
      const { data } = await api.patch(`/social/posts/${id}`, patch);
      setPosts((prev) => prev.map((p) => (p.id === id ? data : p)));
      return data;
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Update failed");
      return null;
    }
  };

  const deletePost = async (id) => {
    if (!window.confirm("Delete this post?")) return;
    try {
      await api.delete(`/social/posts/${id}`);
      setPosts((prev) => prev.filter((p) => p.id !== id));
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Delete failed");
    }
  };

  if (loading) return <div className="p-12 text-center sp-subtle">Loading social queue…</div>;
  if (!org) return null;

  return (
    <div>
      <TopBar>
        <Link to="/app" className="hover:text-white">Organizations</Link>
        <span className="mx-2 opacity-40">/</span>
        <Link to={`/app/org/${orgId}`} className="hover:text-white">{org.name}</Link>
        <span className="mx-2 opacity-40">/</span>
        <span className="text-white/90">Social Queue</span>
      </TopBar>

      <main className="max-w-6xl mx-auto px-4 sm:px-6 py-8 sm:py-10">
        <div className="flex flex-col gap-2 mb-8">
          <span className="sp-chip"><Megaphone className="w-3 h-3" /> Generate &amp; queue</span>
          <h1 className="sp-display text-4xl md:text-5xl">Social Queue</h1>
          <p className="sp-subtle">Draft posts with your agents, schedule them, then one-click publish to each platform's web composer. No accounts to connect, no API keys to wrangle.</p>
        </div>

        {/* Compose form */}
        <form onSubmit={compose} className="sp-glass-strong rounded-2xl p-6 mb-8 space-y-3" data-testid="social-compose">
          <div className="flex items-center gap-2 mb-1">
            <Sparkles className="w-4 h-4 text-orange-400" />
            <h3 className="text-sm uppercase tracking-widest sp-subtle">Draft with an agent</h3>
          </div>
          <textarea
            data-testid="social-compose-prompt"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="What should the post be about? e.g. 'Announce our Summer Cold Brew launch with a friendly hook'"
            rows={3}
            className="w-full rounded-xl px-4 py-3 text-sm resize-none focus:sp-focus"
            style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
          />
          <div className="flex flex-wrap gap-1.5">
            {PLATFORM_ORDER.map((id) => {
              const meta = PLATFORMS[id];
              const on = composePlatforms.includes(id);
              return (
                <button
                  key={id}
                  type="button"
                  onClick={() => togglePlatform(id)}
                  data-testid={`compose-platform-${id}`}
                  className={`px-3 py-1.5 rounded-full text-xs border transition ${on ? "bg-orange-500/20 border-orange-400 text-white" : "border-white/15 sp-subtle hover:text-white hover:border-white/30"}`}
                >
                  {meta.label}
                </button>
              );
            })}
          </div>
          <div className="flex items-center gap-3 flex-wrap">
            <label className="flex items-center gap-2 text-xs sp-subtle">
              How many drafts?
              <input
                type="number" min={1} max={10} value={count}
                onChange={(e) => setCount(Math.max(1, Math.min(10, Number(e.target.value) || 1)))}
                className="w-16 rounded-lg px-2 py-1 text-sm"
                style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
                data-testid="compose-count"
              />
            </label>
            <button
              type="submit"
              disabled={composing || !prompt.trim()}
              data-testid="compose-submit"
              className="sp-btn sp-btn-primary text-xs ml-auto disabled:opacity-50"
            >
              {composing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Send className="w-3.5 h-3.5" />}
              {composing ? "Drafting…" : "Draft posts"}
            </button>
          </div>
        </form>

        {/* Tabs */}
        <div className="flex items-center gap-1 mb-4" role="tablist">
          {[
            ["draft", "Drafts"],
            ["queued", "Scheduled"],
            ["published", "Published"],
          ].map(([id, label]) => (
            <button
              key={id}
              role="tab"
              aria-selected={tab === id}
              data-testid={`tab-${id}`}
              onClick={() => setTab(id)}
              className={`px-4 py-2 rounded-full text-sm border transition ${tab === id ? "bg-white/10 border-white/25 text-white" : "border-white/10 sp-subtle hover:text-white hover:border-white/20"}`}
            >
              {label} <span className="ml-1.5 text-[11px] opacity-70">{counts[id]}</span>
            </button>
          ))}
        </div>

        {/* Post list */}
        {visible.length === 0 ? (
          <div className="sp-glass rounded-2xl p-10 text-center sp-subtle" data-testid="social-empty">
            {tab === "draft" && "No drafts yet. Use the form above to generate some."}
            {tab === "queued" && "Nothing scheduled. Open a draft and pick a date/time."}
            {tab === "published" && "Nothing published yet. Mark posts as published once you've posted them."}
          </div>
        ) : (
          <div className="space-y-3">
            {visible.map((p) => (
              <PostCard key={p.id} post={p} onUpdate={updatePost} onDelete={deletePost} />
            ))}
          </div>
        )}
      </main>
    </div>
  );
}

// ── Post card ─────────────────────────────────────────────────────────────
function PostCard({ post, onUpdate, onDelete }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(post.text);
  const [hashtags, setHashtags] = useState((post.hashtags || []).join(" "));
  const [scheduledLocal, setScheduledLocal] = useState(isoToLocalInput(post.scheduled_for));
  const [platforms, setPlatforms] = useState(post.platforms || []);
  const [saving, setSaving] = useState(false);

  // Re-sync if upstream post changes (e.g. after schedule auto-flips status)
  useEffect(() => {
    if (!editing) {
      setText(post.text);
      setHashtags((post.hashtags || []).join(" "));
      setScheduledLocal(isoToLocalInput(post.scheduled_for));
      setPlatforms(post.platforms || []);
    }
  }, [post, editing]);

  const togglePlatform = (id) => {
    setPlatforms((prev) =>
      prev.includes(id) ? prev.filter((p) => p !== id) : [...prev, id]
    );
  };

  const save = async () => {
    setSaving(true);
    const tags = hashtags
      .split(/[\s,]+/).map((t) => t.trim()).filter(Boolean)
      .map((t) => (t.startsWith("#") ? t : `#${t}`));
    const patch = {
      text: text.trim(),
      hashtags: tags,
      platforms,
    };
    if (scheduledLocal) {
      // datetime-local is naive local time; convert to a UTC ISO string
      patch.scheduled_for = new Date(scheduledLocal).toISOString();
    } else {
      patch.scheduled_for = "";
    }
    const ok = await onUpdate(post.id, patch);
    setSaving(false);
    if (ok) {
      toast.success("Saved");
      setEditing(false);
    }
  };

  const markPublished = () => onUpdate(post.id, { status: "published" });
  const unpublish     = () => onUpdate(post.id, { status: "draft" });
  const moveToDraft   = () => onUpdate(post.id, { status: "draft", scheduled_for: "" });

  const copyText = (postLike) => {
    const blob = joinPostText(postLike);
    try {
      navigator.clipboard.writeText(blob);
      toast.success("Copied to clipboard");
    } catch {
      toast.error("Copy failed");
    }
  };

  return (
    <div className="sp-glass rounded-2xl p-5 space-y-3" data-testid={`social-post-${post.id}`}>
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs sp-subtle">drafted by</span>
        <span className="sp-chip text-[11px]">{post.agent_name || "Agent"}</span>
        {post.platforms.map((id) => {
          const meta = PLATFORMS[id];
          if (!meta) return null;
          return (
            <span key={id} className="px-2 py-0.5 rounded-full text-[11px] border border-white/15"
                  style={{ borderColor: `${meta.accent}66`, color: meta.accent }}>
              {meta.label}
            </span>
          );
        })}
        {post.status === "queued" && post.scheduled_for && (
          <span className="ml-auto text-[11px] sp-subtle flex items-center gap-1">
            <Calendar className="w-3 h-3" /> {formatScheduleLocal(post.scheduled_for)}
          </span>
        )}
        {post.status === "published" && post.published_at && (
          <span className="ml-auto text-[11px] sp-subtle flex items-center gap-1">
            <CheckCircle2 className="w-3 h-3 text-emerald-400" /> Published {formatScheduleLocal(post.published_at)}
          </span>
        )}
      </div>

      {editing ? (
        <div className="space-y-2">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={4}
            className="w-full rounded-xl px-4 py-3 text-sm resize-none focus:sp-focus"
            style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
            data-testid={`edit-text-${post.id}`}
          />
          <input
            value={hashtags}
            onChange={(e) => setHashtags(e.target.value)}
            placeholder="Hashtags, space- or comma-separated"
            className="w-full rounded-xl px-4 py-2.5 text-sm focus:sp-focus"
            style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
            data-testid={`edit-hashtags-${post.id}`}
          />
          <div className="flex flex-wrap gap-1.5">
            {PLATFORM_ORDER.map((id) => {
              const meta = PLATFORMS[id];
              const on = platforms.includes(id);
              return (
                <button
                  key={id}
                  type="button"
                  onClick={() => togglePlatform(id)}
                  className={`px-2.5 py-1 rounded-full text-[11px] border transition ${on ? "bg-orange-500/20 border-orange-400 text-white" : "border-white/15 sp-subtle hover:text-white"}`}
                >
                  {meta.label}
                </button>
              );
            })}
          </div>
          <div className="flex items-center gap-3 flex-wrap">
            <label className="flex items-center gap-2 text-xs sp-subtle">
              <Calendar className="w-3 h-3" /> Schedule for
              <input
                type="datetime-local"
                value={scheduledLocal}
                onChange={(e) => setScheduledLocal(e.target.value)}
                className="rounded-lg px-2 py-1 text-sm"
                style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
                data-testid={`edit-schedule-${post.id}`}
              />
              {scheduledLocal && (
                <button type="button" onClick={() => setScheduledLocal("")} className="sp-subtle hover:text-white">
                  <X className="w-3 h-3" />
                </button>
              )}
            </label>
            <div className="ml-auto flex items-center gap-2">
              <button type="button" onClick={() => setEditing(false)} className="sp-btn sp-btn-ghost text-xs">Cancel</button>
              <button type="button" onClick={save} disabled={saving || !text.trim() || !platforms.length}
                      className="sp-btn sp-btn-primary text-xs disabled:opacity-50"
                      data-testid={`save-post-${post.id}`}>
                <Save className="w-3.5 h-3.5" /> {saving ? "Saving…" : "Save"}
              </button>
            </div>
          </div>
        </div>
      ) : (
        <>
          <div className="whitespace-pre-wrap text-[15px] leading-[1.55]" style={{ color: "var(--fg)" }}>{post.text}</div>
          {post.hashtags?.length > 0 && (
            <div className="text-[13px] sp-subtle">{post.hashtags.join(" ")}</div>
          )}
          {post.media?.length > 0 && (
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 mt-2">
              {post.media.slice(0, 3).map((m, i) => {
                const url = m.url?.startsWith("http") ? m.url : `${BACKEND_URL}${m.url}`;
                return m.kind === "image" ? (
                  <img key={i} src={url} alt="" className="w-full h-32 object-cover rounded-lg border border-white/10" />
                ) : (
                  <video key={i} src={url} controls className="w-full h-32 object-cover rounded-lg border border-white/10" />
                );
              })}
            </div>
          )}

          {/* Auto-generated cover image with logo overlay + 5-corner picker. */}
          <PostCoverImage post={post} onUpdate={onUpdate} />

          <div className="flex flex-wrap items-center gap-2 pt-1">
            {post.platforms.map((id) => {
              const meta = PLATFORMS[id];
              if (!meta) return null;
              const url = meta.composeUrl(post);
              if (url) {
                return (
                  <a key={id} href={url} target="_blank" rel="noreferrer"
                     data-testid={`open-${id}-${post.id}`}
                     className="sp-btn sp-btn-ghost text-xs">
                    <ExternalLink className="w-3 h-3" /> Open in {meta.label}
                  </a>
                );
              }
              // No-deeplink platforms: copy + download
              return (
                <button key={id} type="button" onClick={() => copyText(post)}
                        data-testid={`copy-${id}-${post.id}`}
                        className="sp-btn sp-btn-ghost text-xs">
                  <Copy className="w-3 h-3" /> Copy for {meta.label}
                </button>
              );
            })}
            <button type="button" onClick={() => copyText(post)}
                    title="Copy post text + hashtags"
                    data-testid={`copy-text-${post.id}`}
                    className="sp-btn sp-btn-ghost text-xs">
              <Copy className="w-3 h-3" /> Copy text
            </button>
            {post.media?.[0]?.url && (
              <a href={post.media[0].url.startsWith("http") ? post.media[0].url : `${BACKEND_URL}${post.media[0].url}`}
                 download target="_blank" rel="noreferrer"
                 className="sp-btn sp-btn-ghost text-xs"
                 data-testid={`download-media-${post.id}`}>
                <Download className="w-3 h-3" /> Media
              </a>
            )}
            <div className="ml-auto flex items-center gap-2">
              {post.status !== "published" && (
                <button type="button" onClick={markPublished} className="sp-btn sp-btn-ghost text-xs"
                        data-testid={`mark-published-${post.id}`}>
                  <CheckCircle2 className="w-3 h-3 text-emerald-400" /> Mark posted
                </button>
              )}
              {post.status === "published" && (
                <button type="button" onClick={unpublish} className="sp-btn sp-btn-ghost text-xs">
                  Move back to drafts
                </button>
              )}
              {post.status === "queued" && (
                <button type="button" onClick={moveToDraft} className="sp-btn sp-btn-ghost text-xs">
                  Unschedule
                </button>
              )}
              <button type="button" onClick={() => setEditing(true)} className="sp-btn sp-btn-ghost text-xs"
                      data-testid={`edit-post-${post.id}`}>
                Edit
              </button>
              <button type="button" onClick={() => onDelete(post.id)} className="sp-btn sp-btn-ghost text-xs hover:!text-red-300"
                      data-testid={`delete-post-${post.id}`}>
                <Trash2 className="w-3 h-3" />
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}


// ── Cover image with 5-corner logo picker ─────────────────────────────────
const LOGO_POSITIONS = [
  { id: "top-left",     label: "TL" },
  { id: "top-right",    label: "TR" },
  { id: "center",       label: "·" },
  { id: "bottom-left",  label: "BL" },
  { id: "bottom-right", label: "BR" },
];

function PostCoverImage({ post, onUpdate }) {
  const [generating, setGenerating] = useState(false);
  const [reposing, setReposing] = useState("");
  const [showPromptEditor, setShowPromptEditor] = useState(false);
  const [promptDraft, setPromptDraft] = useState(post.image_prompt || "");

  // Keep the local draft in sync if the parent re-fetches and the
  // persisted `image_prompt` on this post changes under us (e.g. another
  // tab edited it, or generate overwrote it).
  useEffect(() => { setPromptDraft(post.image_prompt || ""); }, [post.image_prompt]);

  const generate = async (customPrompt = null) => {
    if (generating) return;
    setGenerating(true);
    try {
      const body = customPrompt !== null ? { prompt: customPrompt } : {};
      const { data } = await api.post(`/social/posts/${post.id}/generate-image`, body);
      Object.assign(post, data);
      toast.success(customPrompt ? "Image regenerated with your prompt" : "Image generated");
      onUpdate(post.id, { status: data.status });
      setShowPromptEditor(false);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Generation failed");
    } finally { setGenerating(false); }
  };

  const reposition = async (pos) => {
    if (reposing || pos === post.logo_position) return;
    setReposing(pos);
    try {
      await api.patch(`/social/posts/${post.id}/logo-position?position=${encodeURIComponent(pos)}`);
      onUpdate(post.id, { status: post.status });  // trigger parent re-fetch
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Reposition failed");
    } finally { setReposing(""); }
  };

  if (!post.image) {
    return (
      <div className="mt-2">
        <div className="flex items-center justify-between sp-glass rounded-xl px-4 py-2.5 border border-white/10">
          <div className="flex items-center gap-2 text-xs sp-subtle">
            <ImageIcon className="w-3.5 h-3.5" />
            No cover image yet
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            <button
              type="button"
              onClick={() => setShowPromptEditor(true)}
              disabled={generating}
              className="sp-btn sp-btn-ghost text-xs disabled:opacity-50"
              data-testid={`edit-image-prompt-${post.id}`}
              title="Write or edit the prompt before generating"
            >
              <Pencil className="w-3.5 h-3.5" /> Edit prompt
            </button>
            <button type="button" onClick={() => generate()} disabled={generating}
                    className="sp-btn sp-btn-primary text-xs disabled:opacity-50"
                    data-testid={`generate-image-${post.id}`}>
              {generating ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Wand2 className="w-3.5 h-3.5" />}
              {generating ? "Generating…" : "Generate cover image"}
            </button>
          </div>
        </div>
        {showPromptEditor && (
          <SocialPromptEditor
            initial={promptDraft}
            postText={post.text}
            onSubmit={(p) => generate(p)}
            onCancel={() => setShowPromptEditor(false)}
            generating={generating}
            postId={post.id}
          />
        )}
      </div>
    );
  }

  const url = post.image.url?.startsWith("http") ? post.image.url : `${BACKEND_URL}${post.image.url}`;
  return (
    <div className="space-y-2 mt-2">
      <div className="relative rounded-xl overflow-hidden border border-white/10 group/img">
        <img src={url} alt="" className="w-full max-h-96 object-contain bg-black/20"
             data-testid={`post-image-${post.id}`} />
        {!post.image.with_logo && (
          <div className="absolute top-2 right-2 sp-glass-strong rounded-full px-2.5 py-1 text-[11px]">
            No logo overlay — upload a primary logo on the Org page
          </div>
        )}
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[11px] sp-subtle uppercase tracking-widest">Logo position</span>
        <div className="inline-flex items-center gap-1 sp-glass rounded-full p-1" data-testid={`logo-picker-${post.id}`}>
          {LOGO_POSITIONS.map((p) => (
            <button
              key={p.id}
              type="button"
              onClick={() => reposition(p.id)}
              disabled={!post.image.with_logo || reposing === p.id}
              title={p.id.replace("-", " ")}
              data-testid={`logo-pos-${p.id}-${post.id}`}
              className={`w-7 h-7 rounded-full text-[11px] transition ${
                post.logo_position === p.id
                  ? "bg-orange-500/30 text-white"
                  : "sp-subtle hover:text-white hover:bg-white/5"
              } disabled:opacity-40`}
            >
              {reposing === p.id ? <Loader2 className="w-3 h-3 animate-spin mx-auto" /> : p.label}
            </button>
          ))}
        </div>
        <button type="button" onClick={() => setShowPromptEditor(true)} disabled={generating}
                className="sp-btn sp-btn-ghost text-xs ml-auto disabled:opacity-50"
                data-testid={`edit-image-prompt-${post.id}`}
                title="Edit the prompt used to render this image">
          <Pencil className="w-3.5 h-3.5" /> Edit prompt
        </button>
        <button type="button" onClick={() => generate()} disabled={generating}
                className="sp-btn sp-btn-ghost text-xs disabled:opacity-50"
                data-testid={`regenerate-image-${post.id}`}>
          {generating ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Wand2 className="w-3.5 h-3.5" />}
          {generating ? "Generating…" : "Regenerate"}
        </button>
      </div>
      {showPromptEditor && (
        <SocialPromptEditor
          initial={promptDraft}
          postText={post.text}
          onSubmit={(p) => generate(p)}
          onCancel={() => setShowPromptEditor(false)}
          generating={generating}
          postId={post.id}
        />
      )}
    </div>
  );
}

/**
 * SocialPromptEditor — inline modal for authoring the image prompt before
 * generation/regeneration. Shows the original post copy for reference so
 * users can reference it without guessing what the default prompt was.
 * A visible "Reset to post copy" shortcut clears the override and falls
 * back to the auto-derived prompt ({Soul}+{post.text}).
 */
function SocialPromptEditor({ initial, postText, onSubmit, onCancel, generating, postId }) {
  const [draft, setDraft] = useState(initial || "");
  return (
    <div className="mt-2 sp-glass rounded-xl p-3 border border-orange-500/20" data-testid={`social-prompt-editor-${postId}`}>
      <div className="flex items-center justify-between mb-2">
        <div className="text-[11px] sp-subtle uppercase tracking-widest">Image prompt</div>
        {postText && (
          <button
            type="button"
            onClick={() => setDraft("")}
            className="text-[11px] sp-subtle hover:text-white"
            title="Clear the override and use the post copy as the prompt body"
          >
            Reset to post copy
          </button>
        )}
      </div>
      <textarea
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        placeholder={postText ? `Leave blank to use the post copy:\n"${(postText || "").slice(0, 140)}"` : "Describe the image you want…"}
        className="w-full min-h-[90px] rounded-lg bg-black/30 border border-white/10 px-3 py-2 text-sm resize-y focus:outline-none focus:border-orange-500/50"
        data-testid={`social-prompt-textarea-${postId}`}
      />
      <div className="text-[11px] sp-subtle mt-1.5 leading-relaxed">
        Your company's brand Soul + visual style are prepended automatically; this is the scene / direction.
      </div>
      <div className="flex items-center justify-end gap-2 mt-3">
        <button type="button" onClick={onCancel} disabled={generating}
                className="sp-btn sp-btn-ghost text-xs disabled:opacity-50"
                data-testid={`social-prompt-cancel-${postId}`}>
          Cancel
        </button>
        <button type="button"
                onClick={() => onSubmit(draft)}
                disabled={generating}
                className="sp-btn sp-btn-primary text-xs disabled:opacity-50"
                data-testid={`social-prompt-submit-${postId}`}>
          {generating ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Wand2 className="w-3.5 h-3.5" />}
          {generating ? "Generating…" : "Generate with this prompt"}
        </button>
      </div>
    </div>
  );
}
