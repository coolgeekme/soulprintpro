import React, { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { Link, useParams } from "react-router-dom";
import api, { BACKEND_URL } from "../lib/api";
import TopBar from "../components/TopBar";
import { toast } from "sonner";
import {
  Send, Wand2, Calendar, CheckCircle2, Trash2, Copy, ExternalLink, Download, Save, X, Loader2, Megaphone, Image as ImageIcon, Zap, KeyRound, AlertTriangle, Rocket, Pencil, UploadCloud, ShieldCheck, ChevronRight, Lightbulb, RefreshCcw, FolderOpen, Plug
} from "lucide-react";
import AssetPickerModal from "../components/AssetPickerModal";
import WorkingIndicator from "../components/WorkingIndicator";
import { MetaTargetModal } from "../components/ConnectionsPanel";
import { parseServerDate, formatLocalDateTime } from "../lib/datetime";

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
      `https://www.facebook.com/sharer/sharer.php?quote=${encodeURIComponent(joinPostText(p))}&u=${encodeURIComponent(p.link_url || "https://foundryagents.ai")}`,
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

// Platforms where Mission-driven autoposting is supported (public web
// composer available + kept in sync with backend SUPPORTED_AUTOPOST_PLATFORMS).
// Instagram / TikTok / YouTube are NOT here — no public composer we can drive
// reliably headlessly, and file-upload flows aren't wired yet.
const AUTOPOST_PLATFORMS = [
  "twitter", "linkedin", "facebook",
  "threads", "bluesky", "mastodon", "reddit",
];

const joinPostText = (p) => {
  const tags = (p.hashtags || []).filter(Boolean).join(" ");
  return [p.text, tags].filter(Boolean).join("\n\n");
};

const formatScheduleLocal = (iso) => {
  return formatLocalDateTime(iso, { dateStyle: "medium", timeStyle: "short" }) || iso || "";
};

// `<input type="datetime-local">` wants `YYYY-MM-DDTHH:mm` in local time —
// strip seconds and timezone from the ISO we get back from the API.
const isoToLocalInput = (iso) => {
  const d = parseServerDate(iso);
  if (!d) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
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
  // Topic suggestions — "what should I post about?"-style helper. Cached in
  // local state so a user can scroll through the list without regenerating.
  const [topics, setTopics] = useState([]);
  const [topicsOpen, setTopicsOpen] = useState(false);
  const [topicsLoading, setTopicsLoading] = useState(false);
  const [topicAngle, setTopicAngle] = useState("");  // "" = mix
  // Per-platform connection state (Composio). Drives the "Publish via
  // Foundry" button — only shown when at least one platform is connected.
  // `meta` carries FB/IG target-pin state so we can render a "Posting to
  // <Page>" chip + Change-target picker inline on each post.
  const [publishablePlatforms, setPublishablePlatforms] = useState({
    supported: [], connected: {}, meta: {},
  });
  // When the user clicks "Change target" on any FB/IG post or the
  // publishable summary card, we open the same MetaTargetModal used
  // inside the Connections panel — keyed by connection_id so it works
  // for either FB or IG.
  const [metaTargetEdit, setMetaTargetEdit] = useState(null);

  const reload = async () => {
    try {
      const [orgRes, postsRes, pubRes] = await Promise.all([
        api.get(`/orgs/${orgId}`),
        api.get(`/orgs/${orgId}/social/posts`),
        api.get(`/orgs/${orgId}/social/publishable-platforms`).catch(() => ({ data: { supported: [], connected: {}, meta: {} } })),
      ]);
      setOrg(orgRes.data);
      setPosts(postsRes.data);
      setPublishablePlatforms(pubRes.data || { supported: [], connected: {}, meta: {} });
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed to load social queue");
    } finally { setLoading(false); }
  };
  useEffect(() => { reload(); /* eslint-disable-next-line */ }, [orgId]);

  const counts = useMemo(() => {
    const c = { draft: 0, queued: 0, published: 0, failed: 0 };
    posts.forEach((p) => {
      // "partial" rolls into the Published tab — at least some
      // platforms made it through.
      if (p.status === "partial") c.published += 1;
      else if (c[p.status] !== undefined) c[p.status] += 1;
    });
    return c;
  }, [posts]);
  // Tabs include "failed" so that posts whose publish attempt blew up
  // don't silently disappear from the queue. Previously, `status=failed`
  // posts matched none of the three visible tabs (draft/queued/published)
  // and users saw "Publishing failed on 1 platform." with no way to find
  // the post afterwards — the error-chip + "Why did this fail?" UI on the
  // card was unreachable. We also keep "partial" rolled into the
  // Published tab (some platforms did succeed) so the user still sees
  // those wins; the per-platform chip carries the per-platform error.
  const TABS = [
    ["draft", "Drafts"],
    ["queued", "Scheduled"],
    ["published", "Published"],
    ["failed", "Failed"],
  ];
  const visible = useMemo(
    () => posts.filter((p) => (
      tab === "published"
        ? (p.status === "published" || p.status === "partial")
        : p.status === tab
    )),
    [posts, tab],
  );

  // Poll for any post whose cover image is being generated in the
  // background (image_status === "pending"). After /social/draft
  // returns, the first post's image lands ~6-15s later — without this,
  // the user sees a stale skeleton until they manually refresh.
  // Stops polling automatically once nothing is pending or after 90s.
  useEffect(() => {
    const hasPending = posts.some((p) => p.image_status === "pending");
    if (!hasPending) return undefined;
    let cancelled = false;
    let attempts = 0;
    const interval = setInterval(async () => {
      attempts += 1;
      if (attempts > 23 /* ~90s at 4s ticks */) {
        clearInterval(interval);
        return;
      }
      try {
        const { data } = await api.get(`/orgs/${orgId}/social/posts`);
        if (cancelled) return;
        setPosts(data);
        // Stop the interval as soon as nothing is pending — the next
        // useEffect tick will re-evaluate `hasPending` and return early.
        if (!data.some((p) => p.image_status === "pending")) {
          clearInterval(interval);
        }
      } catch { /* swallow — next tick will retry */ }
    }, 4000);
    return () => { cancelled = true; clearInterval(interval); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [posts.some((p) => p.image_status === "pending"), orgId]);

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
      // Friendlier copy: most users hit "Draft posts" expecting magic in
      // <10s. The text response now lands fast (~8s) but the cover image
      // is still being generated in the background, so we tell them so
      // they don't think the cover is missing.
      const pending = fresh.filter((p) => p.image_status === "pending").length;
      if (pending > 0) {
        toast.success(
          `Drafted ${fresh.length} post${fresh.length === 1 ? "" : "s"} — cover image generating in background…`,
          { duration: 4500 }
        );
      } else {
        toast.success(`Drafted ${fresh.length} post${fresh.length === 1 ? "" : "s"}`);
      }
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not draft posts");
    } finally { setComposing(false); }
  };

  // Ask the backend to synthesise 6 on-brand topic ideas from the org's
  // Soul + live news. User clicks a topic to prefill the compose prompt.
  const loadTopics = async () => {
    setTopicsLoading(true);
    try {
      const { data } = await api.post(`/orgs/${orgId}/social/suggest-topics`, {
        count: 6,
        angle: topicAngle || "",
        hint: prompt.trim().slice(0, 300),  // reuse whatever the user typed as a hint
      });
      setTopics(data.topics || []);
      setTopicsOpen(true);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not suggest topics");
    } finally { setTopicsLoading(false); }
  };

  const useTopic = (topic) => {
    // Build a composer prompt that preserves the topic's angle + pitch so
    // the downstream draft agent has enough context to produce real copy.
    const parts = [topic.headline];
    if (topic.pitch) parts.push(topic.pitch);
    if (topic.source_url) parts.push(`(Inspired by: ${topic.source_url})`);
    setPrompt(parts.join(" — "));
    setTopicsOpen(false);
    toast.success("Topic loaded — tweak the prompt and hit Draft posts.");
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

  // Replace a post in local state with a fresh server-returned doc — used
  // by the Asset Picker (use-asset / remix-asset) so the new cover image
  // and image_prompt show up immediately without a wasted PATCH round-trip.
  const replacePost = (data) => {
    if (!data?.id) return;
    setPosts((prev) => prev.map((p) => (p.id === data.id ? data : p)));
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
            <Wand2 className="w-4 h-4 text-orange-400" />
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
              type="button"
              onClick={loadTopics}
              disabled={topicsLoading}
              data-testid="suggest-topics-btn"
              title="Have an agent suggest on-brand topic ideas using current news + your org's Soul"
              className="sp-btn sp-btn-ghost text-xs disabled:opacity-50"
            >
              {topicsLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Lightbulb className="w-3.5 h-3.5" />}
              {topicsLoading ? "Thinking…" : "Suggest topics"}
            </button>
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

        {/* Topic suggestions panel — shown after 'Suggest topics' is clicked */}
        {topicsOpen && (
          <TopicSuggestions
            topics={topics}
            loading={topicsLoading}
            angle={topicAngle}
            setAngle={setTopicAngle}
            onRefresh={loadTopics}
            onUse={useTopic}
            onClose={() => setTopicsOpen(false)}
          />
        )}

        {/* Social sessions — cookies per platform so autopost Missions can log in */}
        <SocialSessionsPanel orgId={orgId} />

        {/* Tabs */}
        <div className="flex items-center gap-1 mb-4" role="tablist">
          {TABS.map(([id, label]) => (
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
            {tab === "failed" && "No failed posts. Anything that hits an error from a platform will show up here so you can see the reason and retry."}
          </div>
        ) : (
          <div className="space-y-3">
            {visible.map((p) => (
              <PostCard key={p.id} post={p} orgId={orgId} onUpdate={updatePost} onReplace={replacePost} onDelete={deletePost} publishable={publishablePlatforms} onAfterPublish={reload} onOpenMetaTarget={(connId) => setMetaTargetEdit(connId)} onJumpToTab={setTab} />
            ))}
          </div>
        )}
      </main>
      {metaTargetEdit && publishable_meta_connection_lookup(publishablePlatforms, metaTargetEdit) && (
        <MetaTargetModal
          orgId={orgId}
          connection={publishable_meta_connection_lookup(publishablePlatforms, metaTargetEdit)}
          onClose={() => setMetaTargetEdit(null)}
          onSaved={() => { setMetaTargetEdit(null); reload(); }}
        />
      )}
    </div>
  );
}

// Resolve a synthetic "connection" object compatible with the
// MetaTargetModal shape (just needs connection_id) from the publishable
// summary returned by the backend. We pass connection_id either as the
// FB or the IG connection id so the modal can read targets.
function publishable_meta_connection_lookup(publishable, connection_id) {
  if (!connection_id || !publishable?.meta) return null;
  const fb = publishable.meta.facebook || {};
  const ig = publishable.meta.instagram || {};
  if (fb.connection_id === connection_id) return { connection_id, toolkit: "facebook" };
  if (ig.connection_id === connection_id) return { connection_id, toolkit: "instagram" };
  return null;
}

// ── Meta target hint (FB / IG only) ───────────────────────────────────────
// Renders directly below the platform pills on a post card whenever the
// post targets Facebook or Instagram. Shows the currently-pinned Page +
// linked IG so the user instantly sees where the post will land, and
// includes a Change link that opens the same picker modal used in the
// Connections panel.
function MetaTargetHint({ platforms, publishable, orgId, onChange, postId }) {
  const wantsFb = (platforms || []).includes("facebook");
  const wantsIg = (platforms || []).includes("instagram");
  if (!wantsFb && !wantsIg) return null;
  const meta = publishable?.meta || {};
  const fb = meta.facebook || {};
  const ig = meta.instagram || {};
  // We only render the chip when there is at least one ACTIVE FB or IG
  // connection — if not, the user already sees the amber "connect first"
  // chip from the existing connect-hint above and we don't want to duplicate.
  if (!(fb.connected || ig.connected)) return null;
  const ref_conn = fb.connection_id || ig.connection_id;
  if (!ref_conn) return null;
  const pageName = fb.page_name || "";
  const igHandle = ig.ig_username || "";
  const targetMissing = (wantsFb && !pageName) || (wantsIg && !igHandle);
  return (
    <button
      type="button"
      onClick={() => onChange && onChange(ref_conn)}
      className={`text-[11px] inline-flex items-center gap-1 px-2 py-0.5 rounded-full border transition ${
        targetMissing
          ? "border-amber-500/40 bg-amber-500/10 hover:bg-amber-500/15 text-amber-300"
          : "border-white/15 hover:bg-white/5 sp-subtle"
      }`}
      title={targetMissing
        ? "Pick which Page / IG account this post should go to before publishing."
        : "Click to change which Page / IG account this org posts to."}
      data-testid={`meta-target-hint-${postId}`}
    >
      {targetMissing ? (
        <>⚠️ Select Meta target</>
      ) : (
        <>
          {pageName && <>Posting to <strong className="font-medium">{pageName}</strong></>}
          {pageName && igHandle && <span className="opacity-50"> · </span>}
          {igHandle && <>@<strong className="font-medium">{igHandle}</strong></>}
          <span className="opacity-50">· Change</span>
        </>
      )}
    </button>
  );
}

// ── Post card ─────────────────────────────────────────────────────────────
function PostCard({ post, orgId, onUpdate, onReplace, onDelete, publishable, onAfterPublish, onOpenMetaTarget, onJumpToTab }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(post.text);
  const [hashtags, setHashtags] = useState((post.hashtags || []).join(" "));
  const [scheduledLocal, setScheduledLocal] = useState(isoToLocalInput(post.scheduled_for));
  const [platforms, setPlatforms] = useState(post.platforms || []);
  const [saving, setSaving] = useState(false);
  const [publishing, setPublishing] = useState(post.status === "publishing");

  // Autopost state — lets the user ask a Mission to actually publish the
  // post on the scheduled time instead of just reminding them.
  const [autopostEnabled, setAutopostEnabled] = useState(Boolean(post.autopost_enabled));
  const [autopostPlatform, setAutopostPlatform] = useState(
    post.autopost_platform || (post.platforms || []).find((p) => AUTOPOST_PLATFORMS.includes(p)) || "twitter"
  );
  const [autopostBusy, setAutopostBusy] = useState(false);

  // Re-sync if upstream post changes (e.g. after schedule auto-flips status)
  useEffect(() => {
    if (!editing) {
      setText(post.text);
      setHashtags((post.hashtags || []).join(" "));
      setScheduledLocal(isoToLocalInput(post.scheduled_for));
      setPlatforms(post.platforms || []);
      setAutopostEnabled(Boolean(post.autopost_enabled));
      if (post.autopost_platform) setAutopostPlatform(post.autopost_platform);
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
    // If autopost was toggled on AND we have a scheduled time, hand off to
    // the autopost endpoint so APScheduler registers the Mission trigger.
    // If toggled off, cancel any pending autopost.
    if (ok) {
      try {
        if (autopostEnabled && scheduledLocal) {
          await api.post(`/social/posts/${post.id}/schedule-autopost`, {
            scheduled_for: new Date(scheduledLocal).toISOString(),
            platform: autopostPlatform,
          });
        } else if (!autopostEnabled && post.autopost_enabled) {
          await api.delete(`/social/posts/${post.id}/schedule-autopost`);
        }
      } catch (err) {
        toast.error(err?.response?.data?.detail || "Autopost update failed");
      }
    }
    setSaving(false);
    if (ok) {
      toast.success("Saved");
      setEditing(false);
    }
  };

  // Quick toggle used from the status row (outside edit mode).
  const toggleAutopostFromCard = async () => {
    if (!post.scheduled_for) {
      toast.error("Schedule a time first — open the card and pick a date/time.");
      return;
    }
    setAutopostBusy(true);
    try {
      if (post.autopost_enabled) {
        await api.delete(`/social/posts/${post.id}/schedule-autopost`);
        toast.success("Autopost cancelled");
      } else {
        const platform = autopostPlatform || (post.platforms || []).find((p) => AUTOPOST_PLATFORMS.includes(p));
        if (!platform) {
          toast.error("Add a supported platform first (Twitter, LinkedIn, Threads, Bluesky, Mastodon, Reddit, Facebook)");
          return;
        }
        await api.post(`/social/posts/${post.id}/schedule-autopost`, {
          scheduled_for: post.scheduled_for,
          platform,
        });
        toast.success("Autopost scheduled — a Mission will run at the set time");
      }
      // Force parent to refresh — easiest path: bounce through onUpdate with a
      // no-op patch that returns the fresh post.
      await onUpdate(post.id, {});
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Autopost toggle failed");
    } finally {
      setAutopostBusy(false);
    }
  };

  const markPublished = () => onUpdate(post.id, { status: "published" });
  const unpublish     = () => onUpdate(post.id, { status: "draft" });
  const moveToDraft   = () => onUpdate(post.id, { status: "draft", scheduled_for: "" });

  // Composio-backed "Publish via Foundry" — fires a background task on
  // the backend and polls until the post status flips to published /
  // partial / failed.
  //
  // Render rules (fix C — Feb 2026):
  //  • If at least one of the post's platforms has an ACTIVE Composio
  //    connection → enabled primary button.
  //  • If the post has platforms but none are connected → disabled
  //    button with a tooltip explaining what to connect, AND a small
  //    "connect to publish" chip near the platform pills that links
  //    straight to the Connections panel.
  //  • If the platform isn't a supported direct-publish toolkit at all
  //    (e.g. youtube, pinterest, bluesky, mastodon, threads) → hide
  //    everything; those still flow through Copy / Mark posted.
  const supportedSet = useMemo(() => new Set(publishable?.supported || []), [publishable?.supported]);
  const connectedSet = useMemo(() => publishable?.connected || {}, [publishable?.connected]);
  const platformsSupported = useMemo(
    () => (post.platforms || []).filter((p) => supportedSet.has(p)),
    [post.platforms, supportedSet],
  );
  const platformsConnected = useMemo(
    () => platformsSupported.filter((p) => connectedSet[p]),
    [platformsSupported, connectedSet],
  );
  const platformsMissing = useMemo(
    () => platformsSupported.filter((p) => !connectedSet[p]),
    [platformsSupported, connectedSet],
  );
  const canPublishViaFoundry = platformsConnected.length > 0;
  const hasUnconnectedSupported = platformsMissing.length > 0;

  const publishViaFoundry = async () => {
    if (publishing) return;
    setPublishing(true);
    try {
      await api.post(`/social/posts/${post.id}/publish`);
      toast.message("Publishing via Foundry…");
      // Poll the post until status leaves "publishing".
      let attempts = 0;
      const tick = async () => {
        attempts += 1;
        try {
          const { data: fresh } = await api.get(`/orgs/${orgId}/social/posts`);
          const updated = (fresh || []).find((x) => x.id === post.id);
          if (updated && updated.status !== "publishing") {
            onReplace?.(updated);
            const ok = updated.published_count || 0;
            const bad = updated.failed_count || 0;
            // Pull the most useful per-platform error text so the
            // toast itself carries actionable detail — not just
            // "publishing failed on 1 platform". The error chip on
            // the card always has the full message; the toast is
            // a faster summary the user sees without scrolling.
            const failedEntries = Object.entries(updated.publish_results || {})
              .filter(([, r]) => !r.ok);
            const firstError = failedEntries[0]?.[1]?.error || "";
            const firstPlatform = failedEntries[0]?.[0] || "";
            if (updated.status === "published") {
              toast.success(`Published to ${ok} platform${ok === 1 ? "" : "s"}.`);
              onJumpToTab?.("published");
            } else if (updated.status === "partial") {
              toast(
                `Posted to ${ok}, failed on ${bad}.` +
                (firstError ? ` ${firstPlatform}: ${firstError.slice(0, 140)}` : " See errors on the card."),
                { icon: "⚠️", duration: 8000 },
              );
              onJumpToTab?.("published");
            } else {
              toast.error(
                `Publishing failed on ${bad} platform${bad === 1 ? "" : "s"}.` +
                (firstError ? ` ${firstPlatform}: ${firstError.slice(0, 200)}` : " Open the Failed tab for details."),
                { duration: 10000 },
              );
              // Auto-jump to the Failed tab so the user can actually
              // see the card with its error chip and "Why did this
              // fail?" details — previously the post just vanished.
              onJumpToTab?.("failed");
            }
            setPublishing(false);
            onAfterPublish?.();
            return;
          }
        } catch (_) {/* keep polling */}
        if (attempts < 20) setTimeout(tick, 2000);
        else setPublishing(false);
      };
      setTimeout(tick, 1500);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Failed to publish");
      setPublishing(false);
    }
  };

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
          const supported = supportedSet.has(id);
          const connected = Boolean(connectedSet[id]);
          // Compact connection indicator next to each platform pill so
          // the user instantly knows whether one-click publishing will
          // work for that platform. ✓ green = connected, • amber =
          // supported-but-not-connected, no badge = direct publishing
          // unsupported (copy-only).
          return (
            <span
              key={id}
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] border border-white/15"
              style={{ borderColor: `${meta.accent}66`, color: meta.accent }}
              title={
                supported
                  ? connected
                    ? `${meta.label} connected — one-click publish ready.`
                    : `${meta.label} supported but not connected. Connect it under your org's Connections panel to enable one-click publishing.`
                  : `${meta.label}: direct publishing isn't supported yet. Use "Copy text" then paste into the ${meta.label} composer.`
              }
              data-testid={`platform-pill-${post.id}-${id}`}
            >
              {meta.label}
              {supported && connected && (
                <CheckCircle2 className="w-3 h-3" data-testid={`pill-connected-${post.id}-${id}`} />
              )}
              {supported && !connected && (
                <span
                  className="inline-block w-1.5 h-1.5 rounded-full"
                  style={{ background: "rgb(245 158 11)" }}
                  data-testid={`pill-unconnected-${post.id}-${id}`}
                />
              )}
            </span>
          );
        })}
        {post.status !== "published" && hasUnconnectedSupported && (
          <Link
            to={`/app/org/${orgId}#connections`}
            className="text-[11px] inline-flex items-center gap-1 px-2 py-0.5 rounded-full border border-amber-500/40 bg-amber-500/10 hover:bg-amber-500/15 transition"
            style={{ color: "rgb(245 158 11)" }}
            title="Open the Connections panel for this org"
            data-testid={`connect-hint-${post.id}`}
          >
            <Plug className="w-3 h-3" />
            Connect {platformsMissing.map((p) => PLATFORMS[p]?.label || p).join(" + ")} to publish from here
          </Link>
        )}
        <MetaTargetHint
          platforms={post.platforms}
          publishable={publishable}
          orgId={orgId}
          onChange={onOpenMetaTarget}
          postId={post.id}
        />
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
        {post.status === "publishing" && (
          <span className="ml-auto text-[11px] flex items-center gap-1 text-orange-300">
            <Loader2 className="w-3 h-3 animate-spin" /> Publishing…
          </span>
        )}
        {(post.status === "partial" || post.status === "failed") && (
          <span className={`ml-auto text-[11px] flex items-center gap-1 ${post.status === "partial" ? "text-amber-300" : "text-red-300"}`}>
            <AlertTriangle className="w-3 h-3" />
            {post.published_count || 0}/{(post.published_count || 0) + (post.failed_count || 0)} succeeded
          </span>
        )}
        <AutopostStatusPill post={post} />
      </div>

      {/* Per-platform publish chips — surface results of the live publish
          so users see exactly which platforms succeeded and which need
          re-trying. Hidden until a publish has happened. */}
      {post.publish_results && Object.keys(post.publish_results).length > 0 && (
        <div className="flex flex-wrap gap-1.5 mt-2" data-testid={`publish-results-${post.id}`}>
          {Object.entries(post.publish_results).map(([platform, r]) => (
            <span
              key={platform}
              className={`inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full border ${
                r.ok
                  ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
                  : "border-red-500/30 bg-red-500/10 text-red-200 cursor-help"
              }`}
              title={r.error || (r.ok ? "Posted" : "Failed")}
              onClick={() => {
                if (!r.ok) {
                  alert(`${platform.toUpperCase()} failed:\n\n${r.error || "Unknown error"}\n\nTool: ${r.tool || "—"}\n\nRaw response:\n${JSON.stringify(r.response, null, 2)?.slice(0, 1500) || "(no response captured)"}`);
                }
              }}
            >
              {r.ok ? <CheckCircle2 className="w-2.5 h-2.5" /> : <AlertTriangle className="w-2.5 h-2.5" />}
              {platform}
            </span>
          ))}
        </div>
      )}
      {/* Fallback details button when status is failed/partial but
          publish_results is somehow empty (e.g. early-return path or
          stale serialization). Forces the user to see *something*
          actionable instead of a silent "1 platform failed". */}
      {(post.status === "failed" || post.status === "partial")
       && (!post.publish_results || Object.keys(post.publish_results).length === 0) && (
        <button
          type="button"
          onClick={async () => {
            try {
              const { data } = await api.get(`/orgs/${orgId}/social/posts?status=failed,partial,publishing,published`);
              const fresh = (data || []).find((x) => x.id === post.id);
              const r = fresh?.publish_results || {};
              if (Object.keys(r).length === 0) {
                alert(`No per-platform details captured for this post.\n\nstatus: ${fresh?.status}\nfailed_count: ${fresh?.failed_count}\nplatforms: ${(fresh?.platforms || []).join(", ")}\n\nCheck backend logs for "social-publish FAIL post=${post.id}".`);
              } else {
                alert(Object.entries(r).map(([p, x]) => `${p.toUpperCase()}: ${x.ok ? "OK" : `FAIL — ${x.error || "Unknown"}`}`).join("\n\n"));
              }
            } catch (e) {
              alert("Couldn't fetch details: " + (e?.response?.data?.detail || e.message));
            }
          }}
          className="text-[11px] underline opacity-70 hover:opacity-100 mt-1 self-start"
          data-testid={`publish-details-${post.id}`}
        >
          Why did this fail?
        </button>
      )}

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
                <button type="button" onClick={() => setScheduledLocal("")} className="sp-btn sp-btn-ghost !py-0.5 !px-1.5 text-[11px]">
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

          {/* Autopost via Mission — only shown when a schedule time is set */}
          {scheduledLocal && (
            <div className="rounded-xl p-3 border border-orange-500/20 bg-orange-500/[0.04] flex flex-wrap items-center gap-3"
                 data-testid={`autopost-row-${post.id}`}>
              <label className="flex items-center gap-2 text-xs cursor-pointer">
                <input
                  type="checkbox"
                  checked={autopostEnabled}
                  onChange={(e) => setAutopostEnabled(e.target.checked)}
                  className="accent-orange-500"
                  data-testid={`autopost-toggle-${post.id}`}
                />
                <Zap className="w-3.5 h-3.5 text-orange-400" />
                <span className="text-white/90 font-medium">Auto-post via Mission</span>
                <span
                  className="ml-1 px-1.5 py-0.5 rounded-full text-[9px] uppercase tracking-wider font-semibold"
                  style={{
                    background: "color-mix(in srgb, var(--primary-bg) 18%, transparent)",
                    color: "var(--primary-bg)",
                    border: "1px solid color-mix(in srgb, var(--primary-bg) 30%, transparent)",
                  }}
                  title="Autoposting is in beta — your post may not always go through. Always check the platform after."
                  data-testid={`autopost-beta-pill-${post.id}`}
                >
                  Beta
                </span>
              </label>
              <span className="text-[11px] sp-subtle">
                An agent will open the platform and try to publish this for you.
              </span>
              {autopostEnabled && (
                <div className="flex items-center gap-1.5 ml-auto">
                  <span className="text-[11px] sp-subtle">Target:</span>
                  <select
                    value={autopostPlatform}
                    onChange={(e) => setAutopostPlatform(e.target.value)}
                    className="rounded-lg px-2 py-1 text-xs"
                    style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
                    data-testid={`autopost-platform-${post.id}`}
                  >
                    {AUTOPOST_PLATFORMS.map((p) => (
                      <option key={p} value={p}>{PLATFORMS[p]?.label || p}</option>
                    ))}
                  </select>
                </div>
              )}
              {autopostEnabled && (
                <p className="basis-full text-[11px] sp-subtle italic mt-1">
                  Beta — autoposting can fail if the platform updates its UI or your login session
                  expires. We'll mark the post as failed if that happens, but please verify the
                  post landed by checking the platform afterwards.
                </p>
              )}
            </div>
          )}
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
                // Use the media URL (which is unique per attachment) as the
                // React key — array-index would cause <img>/<video>
                // elements to be reused incorrectly if the user re-attaches
                // different media without a full post reload.
                const key = m.url || `media-${i}`;
                return m.kind === "image" ? (
                  <img key={key} src={url} alt="" className="w-full h-32 object-cover rounded-lg border border-white/10" />
                ) : (
                  <video key={key} src={url} controls className="w-full h-32 object-cover rounded-lg border border-white/10" />
                );
              })}
            </div>
          )}

          {/* Auto-generated cover image with logo overlay + 5-corner picker. */}
          <PostCoverImage post={post} orgId={orgId} onUpdate={onUpdate} onReplace={onReplace} />

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
              {/* Live publish via Composio — visible whenever the post
                  targets at least one supported direct-publish platform.
                  Enabled when connected; otherwise disabled with a
                  tooltip + click → scroll to Connections panel. Falls
                  back to "Mark posted" for posts on un-supported
                  platforms (Bluesky, Mastodon, Threads, etc.). */}
              {post.status !== "published" && canPublishViaFoundry && (
                <button type="button" onClick={publishViaFoundry}
                        disabled={publishing}
                        className="sp-btn sp-btn-primary text-xs disabled:opacity-50"
                        data-testid={`publish-via-foundry-${post.id}`}>
                  {publishing ? (
                    <>
                      <Loader2 className="w-3 h-3 animate-spin" /> Publishing…
                    </>
                  ) : (
                    <>
                      <Send className="w-3 h-3" /> Publish via Foundry
                    </>
                  )}
                </button>
              )}
              {post.status !== "published" && !canPublishViaFoundry && hasUnconnectedSupported && (
                <button
                  type="button"
                  onClick={() => {
                    // Send them to the Connections panel on the Org page.
                    // Hash anchor #connections is already used by the
                    // OAuth-callback handler, so the panel scrolls into
                    // view automatically.
                    window.location.href = `/app/org/${orgId}#connections`;
                  }}
                  className="sp-btn sp-btn-ghost text-xs opacity-70"
                  title={`Connect ${platformsMissing.map((p) => PLATFORMS[p]?.label || p).join(", ")} first to enable one-click publishing.`}
                  data-testid={`publish-via-foundry-disabled-${post.id}`}
                >
                  <Send className="w-3 h-3" /> Publish via Foundry
                </button>
              )}
              {post.status !== "published" && post.status !== "publishing" && (
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
              {post.status === "queued" && post.scheduled_for && (
                <button type="button" onClick={toggleAutopostFromCard}
                        disabled={autopostBusy || post.autopost_status === "running"}
                        title={post.autopost_enabled
                          ? "Cancel the Mission-based autopost"
                          : "Have a Mission open the platform at the scheduled time and publish automatically"}
                        className={`sp-btn text-xs ${post.autopost_enabled ? "sp-btn-ghost" : "sp-btn-primary"} disabled:opacity-50`}
                        data-testid={`autopost-quick-${post.id}`}>
                  {autopostBusy
                    ? <Loader2 className="w-3 h-3 animate-spin" />
                    : post.autopost_enabled ? <X className="w-3 h-3" /> : <Rocket className="w-3 h-3" />}
                  {post.autopost_enabled ? "Cancel autopost" : "Autopost via Mission"}
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


// ── Autopost status pill — sits next to the schedule badge in the card ────
function AutopostStatusPill({ post }) {
  const status = post.autopost_status || "";
  if (!status && !post.autopost_enabled) return null;
  const styleFor = (s) => {
    if (s === "posted")    return { bg: "bg-emerald-500/15", border: "border-emerald-400/30", text: "text-emerald-200" };
    if (s === "running")   return { bg: "bg-sky-500/15",     border: "border-sky-400/30",     text: "text-sky-200" };
    if (s === "pending")   return { bg: "bg-orange-500/15",  border: "border-orange-400/30",  text: "text-orange-200" };
    if (s === "failed")    return { bg: "bg-red-500/15",     border: "border-red-400/30",     text: "text-red-200" };
    if (s === "cancelled") return { bg: "bg-white/5",         border: "border-white/15",       text: "text-white/60" };
    return { bg: "bg-white/5", border: "border-white/15", text: "text-white/60" };
  };
  const sty = styleFor(status);
  const label = {
    posted:    "Autoposted",
    running:   "Autoposting…",
    pending:   "Autopost pending",
    failed:    "Autopost failed",
    cancelled: "Autopost cancelled",
  }[status] || "Autopost";
  const Icon = status === "failed" ? AlertTriangle
            : status === "running" ? Loader2
            : status === "posted"  ? CheckCircle2
            : Zap;
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] border ${sty.bg} ${sty.border} ${sty.text}`}
      title={post.autopost_error || (post.autopost_platform ? `Target: ${post.autopost_platform}` : "")}
      data-testid={`autopost-status-${post.id}`}
    >
      <Icon className={`w-3 h-3 ${status === "running" ? "animate-spin" : ""}`} />
      {label}
      {post.autopost_platform && (
        <span className="opacity-70 ml-1">· {post.autopost_platform}</span>
      )}
    </span>
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

function PostCoverImage({ post, orgId, onUpdate, onReplace }) {
  const [generating, setGenerating] = useState(false);
  const [reposing, setReposing] = useState("");
  const [applyingLogo, setApplyingLogo] = useState(false);
  const [showPromptEditor, setShowPromptEditor] = useState(false);
  const [showAssetPicker, setShowAssetPicker] = useState(false);
  // The image prompt that was actually sent to Nano Banana for this post.
  // We prefer the persisted `image_prompt` (set on every successful
  // generation post-fix). For posts generated BEFORE the persistence
  // fix landed, fall back to `post.text` — that's what the backend
  // would have used as the body anyway, so showing it gives the user
  // a real starting point rather than an empty textarea.
  const initialPrompt = (post.image_prompt && post.image_prompt.trim())
    || (post.text || "").trim()
    || "";
  const [promptDraft, setPromptDraft] = useState(initialPrompt);

  // Keep the local draft in sync if the parent re-fetches and the
  // persisted `image_prompt` on this post changes under us (e.g. another
  // tab edited it, or generate overwrote it). Same fallback applies.
  useEffect(() => {
    setPromptDraft(
      (post.image_prompt && post.image_prompt.trim())
        || (post.text || "").trim()
        || ""
    );
  }, [post.image_prompt, post.text]);

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

  // Apply the org's current primary_logo to an existing image without re-
  // generating (cheap PIL paste via reposition_logo at the current position).
  // Surfaces when the image was generated BEFORE a primary_logo existed,
  // so the user doesn't have to burn an LLM call to get their branding on it.
  const applyLogo = async () => {
    if (applyingLogo) return;
    setApplyingLogo(true);
    try {
      const pos = post.logo_position || "bottom-right";
      await api.patch(`/social/posts/${post.id}/logo-position?position=${encodeURIComponent(pos)}`);
      toast.success("Logo applied");
      onUpdate(post.id, { status: post.status });
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't apply logo");
    } finally { setApplyingLogo(false); }
  };

  // Background-gen pending state — rendered when the /draft endpoint
  // queued the cover image and we're waiting on Nano Banana. SocialPage
  // polls every 4s and patches `post.image_status` to "ready" once the
  // image lands. Without this skeleton, the post would render the same
  // "No cover image yet" CTA bar as a manually-pending one and confuse
  // the user — the system IS working, just async.
  if (post.image_status === "pending") {
    return (
      <div className="mt-2">
        <div
          className="rounded-xl overflow-hidden border border-orange-500/20 aspect-square max-w-[420px] flex flex-col items-center justify-center gap-3 relative bg-black/30"
          data-testid={`post-cover-pending-${post.id}`}
        >
          {/* Sweep shimmer — the same global .sp-shimmer keyframe used
              by the chat image-pending placeholder. */}
          <span aria-hidden className="absolute inset-0 sp-shimmer pointer-events-none" />
          <Loader2 className="w-8 h-8 animate-spin opacity-80" />
          <div className="text-xs sp-subtle px-3 text-center max-w-[260px] leading-relaxed">
            Generating your on-brand cover image —<br />usually 8–25s.
          </div>
        </div>
      </div>
    );
  }

  if (!post.image) {
    return (
      <div className="mt-2">
        <div className="flex items-center justify-between sp-glass rounded-xl px-4 py-2.5 border border-white/10 gap-2 flex-wrap">
          <div className="flex items-center gap-2 text-xs sp-subtle">
            <ImageIcon className="w-3.5 h-3.5" />
            No cover image yet
          </div>
          <div className="flex items-center gap-1.5 shrink-0 flex-wrap">
            <button
              type="button"
              onClick={() => setShowAssetPicker(true)}
              disabled={generating}
              className="sp-btn sp-btn-ghost text-xs disabled:opacity-50"
              data-testid={`use-asset-${post.id}`}
              title="Pick an existing photo from your Media Library, or remix one with AI"
            >
              <FolderOpen className="w-3.5 h-3.5" /> Use a photo
            </button>
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
        {generating && (
          <WorkingIndicator label="Generating cover image" hints={[
            "Briefing Flux Kontext Pro with your post + brand voice…",
            "Composing — typical wait is 15–30s for a polished cover.",
            "Almost there… complex scenes can take up to a minute.",
          ]} />
        )}
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
        {showAssetPicker && (
          <AssetPickerModal
            orgId={orgId}
            postId={post.id}
            onClose={() => setShowAssetPicker(false)}
            onPicked={(data) => onReplace?.(data)}
          />
        )}
      </div>
    );
  }

  const url = post.image.url?.startsWith("http") ? post.image.url : `${BACKEND_URL}${post.image.url}`;
  // Pick a sensible filename for the download so users don't get
  // "84f2..jpeg" — use the post's first 40 chars of text as a slug.
  const slug = (post.text || "social-cover")
    .toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40) || "social-cover";
  const ext = (post.image.filename || "").split(".").pop() || "png";
  const downloadName = `${slug}.${ext}`;
  return (
    <div className="space-y-2 mt-2">
      <div className="relative rounded-xl overflow-hidden border border-white/10 group/img">
        <img src={url} alt="" className="w-full max-h-96 object-contain bg-black/20"
             data-testid={`post-image-${post.id}`} />
        {!post.image.with_logo && (
          <button
            type="button"
            onClick={applyLogo}
            disabled={applyingLogo}
            title="Re-composite the org's primary logo onto this image (no LLM call)"
            className="absolute top-2 left-2 sp-glass-strong rounded-full px-2.5 py-1 text-[11px] flex items-center gap-1.5 hover:bg-white/20 transition disabled:opacity-50"
            data-testid={`apply-logo-${post.id}`}
          >
            {applyingLogo ? <Loader2 className="w-3 h-3 animate-spin" /> : <ImageIcon className="w-3 h-3" />}
            {post.image.raw_filename ? "Apply primary logo" : "Upload a primary logo on the Org page"}
          </button>
        )}
        {/* Hover overlay: quick-access Edit prompt + Download directly on the image */}
        <div className="absolute top-2 right-2 flex items-center gap-1.5 opacity-0 group-hover/img:opacity-100 transition">
          <button
            type="button"
            onClick={() => setShowPromptEditor(true)}
            disabled={generating}
            title="Edit the prompt used to render this image"
            className="sp-glass-strong rounded-full w-8 h-8 flex items-center justify-center hover:bg-white/20 transition disabled:opacity-50"
            data-testid={`edit-image-prompt-overlay-${post.id}`}
          >
            <Pencil className="w-3.5 h-3.5" />
          </button>
          <a
            href={url}
            download={downloadName}
            target="_blank"
            rel="noreferrer"
            title="Download this image"
            className="sp-glass-strong rounded-full w-8 h-8 flex items-center justify-center hover:bg-white/20 transition"
            data-testid={`download-image-overlay-${post.id}`}
          >
            <Download className="w-3.5 h-3.5" />
          </a>
        </div>
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
        {/* Tiny model-attribution chip — tells the user which engine
            produced this cover so the "edit prompt" / regen flow makes
            sense (Flux Kontext is great at composition; Nano Banana is
            the editing fallback). */}
        {post.image.model_used && (
          <span
            className="text-[10px] sp-subtle px-2 py-0.5 rounded-full border border-white/10 bg-black/20"
            title={`Cover image generated by ${post.image.model_used}`}
            data-testid={`image-model-${post.id}`}
          >
            via {post.image.model_used.replace("kie:", "")}
          </span>
        )}
        <a href={url} download={downloadName} target="_blank" rel="noreferrer"
           className="sp-btn sp-btn-ghost text-xs ml-auto"
           data-testid={`download-image-${post.id}`}
           title="Download the generated cover image">
          <Download className="w-3.5 h-3.5" /> Download
        </a>
        <button type="button" onClick={() => setShowAssetPicker(true)} disabled={generating}
                className="sp-btn sp-btn-ghost text-xs disabled:opacity-50"
                data-testid={`use-asset-${post.id}`}
                title="Replace with an existing photo from your Media Library — or remix one with AI">
          <FolderOpen className="w-3.5 h-3.5" /> Use a photo
        </button>
        <button type="button" onClick={() => setShowPromptEditor(true)} disabled={generating}
                className="sp-btn sp-btn-ghost text-xs disabled:opacity-50"
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
      {generating && (
        <WorkingIndicator label="Working on a new cover" hints={[
          "Briefing Nano Banana with your post + brand voice…",
          "Composing — Nano Banana usually takes 8-25s for social covers.",
          "Almost there… complex scenes can take up to a minute.",
        ]} />
      )}
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
      {showAssetPicker && (
        <AssetPickerModal
          orgId={orgId}
          postId={post.id}
          onClose={() => setShowAssetPicker(false)}
          onPicked={(data) => onReplace?.(data)}
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
  // Detect whether the draft has diverged from the auto-derived starting
  // point. If so, "Reset" puts us back to the post copy (which is the
  // backend's default body when no override was ever authored).
  const hasOverride = draft.trim() !== (postText || "").trim();
  return (
    <div className="mt-2 sp-glass rounded-xl p-3 border border-orange-500/20" data-testid={`social-prompt-editor-${postId}`}>
      <div className="flex items-center justify-between mb-2 gap-2">
        <div className="text-[11px] sp-subtle uppercase tracking-widest">
          Image prompt — what Nano Banana actually renders
        </div>
        {postText && hasOverride && (
          <button
            type="button"
            onClick={() => setDraft((postText || "").trim())}
            className="text-[11px] sp-subtle hover:text-white shrink-0"
            title="Restore the auto-derived prompt (the post copy)"
            data-testid={`social-prompt-reset-${postId}`}
          >
            Reset to post copy
          </button>
        )}
      </div>
      <textarea
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        placeholder="Describe the image you want — subject, mood, composition, lighting…"
        className="w-full min-h-[110px] rounded-lg bg-black/30 border border-white/10 px-3 py-2 text-sm resize-y focus:outline-none focus:border-orange-500/50"
        data-testid={`social-prompt-textarea-${postId}`}
      />
      <div className="text-[11px] sp-subtle mt-1.5 leading-relaxed">
        Your company's brand Soul + visual style are prepended automatically — this textarea is the scene / direction.
        {postText && (
          <>
            {" "}
            <span className="opacity-80">
              Tip: this defaults to your post copy. Edit freely to focus on the visual you want.
            </span>
          </>
        )}
      </div>
      <div className="flex items-center justify-end gap-2 mt-3">
        <button type="button" onClick={onCancel} disabled={generating}
                className="sp-btn sp-btn-ghost text-xs disabled:opacity-50"
                data-testid={`social-prompt-cancel-${postId}`}>
          Cancel
        </button>
        <button type="button"
                onClick={() => onSubmit(draft.trim())}
                disabled={generating || !draft.trim()}
                className="sp-btn sp-btn-primary text-xs disabled:opacity-50"
                data-testid={`social-prompt-submit-${postId}`}>
          {generating ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Wand2 className="w-3.5 h-3.5" />}
          {generating ? "Generating…" : "Generate with this prompt"}
        </button>
      </div>
    </div>
  );
}



// ── Per-platform 3-step wizard config ─────────────────────────────────────
// For each supported platform we spell out the exact domain the user
// should visit and which cookies.txt extension we recommend. Keeping
// these in data (not prose) lets us render them compactly and swap
// recommendations later without touching render code.
const PLATFORM_WIZARD = {
  twitter:  { domain: "x.com",          label: "X (Twitter)",  home: "https://x.com/home" },
  linkedin: { domain: "linkedin.com",   label: "LinkedIn",     home: "https://www.linkedin.com/feed/" },
  facebook: { domain: "facebook.com",   label: "Facebook",     home: "https://www.facebook.com/" },
  threads:  { domain: "threads.net",    label: "Threads",      home: "https://www.threads.net/" },
  bluesky:  { domain: "bsky.app",       label: "Bluesky",      home: "https://bsky.app/" },
  mastodon: { domain: "mastodon.social",label: "Mastodon",     home: "https://mastodon.social/home" },
  reddit:   { domain: "reddit.com",     label: "Reddit",       home: "https://www.reddit.com/" },
};
const COOKIES_EXTENSION_URL =
  "https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc";


// ── Topic suggestions panel ───────────────────────────────────────────────
// Shows up after the user clicks "Suggest topics". Compact list of 6 topic
// ideas — each click loads the topic into the compose prompt so the draft
// agent can spin it into N platform-specific posts.
const ANGLE_OPTIONS = [
  { id: "",                  label: "Mix"              },
  { id: "news",              label: "News-reactive"    },
  { id: "evergreen",         label: "Evergreen"        },
  { id: "thought-leadership",label: "Thought-leader"   },
  { id: "community",         label: "Community"        },
  { id: "promotional",       label: "Promotional"      },
];
const ANGLE_STYLE = {
  "news":              { bg: "bg-sky-500/15",     border: "border-sky-400/30",     text: "text-sky-200"     },
  "evergreen":         { bg: "bg-emerald-500/15", border: "border-emerald-400/30", text: "text-emerald-200" },
  "thought-leadership":{ bg: "bg-violet-500/15",  border: "border-violet-400/30",  text: "text-violet-200"  },
  "community":         { bg: "bg-pink-500/15",    border: "border-pink-400/30",    text: "text-pink-200"    },
  "promotional":       { bg: "bg-orange-500/15",  border: "border-orange-400/30",  text: "text-orange-200"  },
};
function TopicSuggestions({ topics, loading, angle, setAngle, onRefresh, onUse, onClose }) {
  return (
    <div className="sp-glass rounded-2xl p-5 mb-6" data-testid="topics-panel">
      <div className="flex items-center gap-2 mb-3 flex-wrap">
        <Lightbulb className="w-4 h-4 text-orange-400" />
        <h3 className="text-sm font-medium">Topic ideas</h3>
        <span className="text-[11px] sp-subtle">tuned to your Soul + this week's news</span>
        <div className="ml-auto flex items-center gap-1">
          {ANGLE_OPTIONS.map((a) => (
            <button
              key={a.id || "mix"}
              type="button"
              onClick={() => setAngle(a.id)}
              className={`text-[11px] px-2 py-1 rounded-full border transition ${
                angle === a.id
                  ? "bg-white/10 border-white/25 text-white"
                  : "border-white/10 sp-subtle hover:text-white hover:border-white/20"
              }`}
              data-testid={`topic-angle-${a.id || "mix"}`}
            >
              {a.label}
            </button>
          ))}
          <button
            type="button"
            onClick={onRefresh}
            disabled={loading}
            title="Regenerate the topic list"
            className="sp-btn sp-btn-ghost !py-1 !px-2 text-[11px] disabled:opacity-50"
            data-testid="topics-refresh"
          >
            {loading ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCcw className="w-3 h-3" />}
          </button>
          <button
            type="button"
            onClick={onClose}
            className="sp-btn sp-btn-ghost !py-1 !px-2 text-[11px]"
            data-testid="topics-close"
          >
            <X className="w-3 h-3" />
          </button>
        </div>
      </div>
      {loading && topics.length === 0 ? (
        <div className="text-xs sp-subtle italic">Researching current events + pulling from your Soul…</div>
      ) : topics.length === 0 ? (
        <div className="text-xs sp-subtle italic">No topics yet — click refresh.</div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          {topics.map((t) => {
            const sty = ANGLE_STYLE[t.angle] || ANGLE_STYLE.evergreen;
            return (
              <button
                key={t.id}
                type="button"
                onClick={() => onUse(t)}
                className="text-left rounded-xl p-3 transition bg-white/[0.02] border border-white/10 hover:bg-white/[0.06] hover:border-orange-400/30 group"
                data-testid={`topic-card-${t.id}`}
              >
                <div className="flex items-start gap-2">
                  <div className="flex-1">
                    <div className="text-sm font-medium leading-snug text-white/90 group-hover:text-white">
                      {t.headline}
                    </div>
                    {t.pitch && (
                      <div className="text-xs sp-subtle mt-1.5 leading-relaxed">
                        {t.pitch}
                      </div>
                    )}
                    <div className="flex items-center gap-1.5 mt-2 flex-wrap">
                      <span className={`text-[10px] px-1.5 py-0.5 rounded-full border ${sty.bg} ${sty.border} ${sty.text}`}>
                        {t.angle}
                      </span>
                      {(t.tags || []).slice(0, 3).map((tag) => (
                        <span key={tag} className="text-[10px] px-1.5 py-0.5 rounded-full bg-white/5 text-white/60">
                          #{tag}
                        </span>
                      ))}
                      {t.source_url && (
                        <a
                          href={t.source_url}
                          target="_blank"
                          rel="noreferrer"
                          onClick={(e) => e.stopPropagation()}
                          className="ml-auto text-[10px] sp-subtle inline-flex items-center gap-1 hover:text-white"
                          title={t.source_title || t.source_url}
                          data-testid={`topic-source-${t.id}`}
                        >
                          <ExternalLink className="w-2.5 h-2.5" /> source
                        </a>
                      )}
                    </div>
                  </div>
                </div>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}


// ── Social Sessions wizard ────────────────────────────────────────────────
// Goal: make the "upload your login cookies" step approachable for people
// who've never seen a terminal. Flow:
//   Step 1: pick platform
//   Step 2: open the platform, install/use the recommended extension,
//           export cookies for that domain
//   Step 3: drag-and-drop (or paste) the exported file — we accept
//           cookies.txt, storage_state JSON, or a raw cookie JSON array
//           and convert it server-side. Followed by a one-click
//           "Test my session" that actually logs in via headless
//           browser so the user knows their upload works BEFORE
//           trusting the autoposter with real content.
function SocialSessionsPanel({ orgId }) {
  const [open, setOpen] = useState(false);
  const [sessions, setSessions] = useState([]);
  const [loading, setLoading] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const { data } = await api.get(`/orgs/${orgId}/social/sessions`);
      setSessions(data || []);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not load sessions");
    } finally { setLoading(false); }
  };
  useEffect(() => { if (open) load(); /* eslint-disable-next-line */ }, [open, orgId]);

  const remove = async (p) => {
    if (!window.confirm(`Delete ${p} session? Autopost will fail until you re-upload.`)) return;
    try {
      await api.delete(`/orgs/${orgId}/social/sessions/${p}`);
      await load();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Delete failed");
    }
  };

  const testExisting = async (platform) => {
    const t = toast.loading(`Testing ${platform} session…`);
    try {
      const { data } = await api.post(`/orgs/${orgId}/social/sessions/${platform}/test`, {});
      toast.dismiss(t);
      if (data.ok) toast.success(data.message);
      else toast.error(data.message);
    } catch (err) {
      toast.dismiss(t);
      toast.error(err?.response?.data?.detail || "Test failed");
    }
  };

  return (
    <div className="mb-6" data-testid="social-sessions-panel">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="sp-btn sp-btn-ghost text-xs"
        data-testid="social-sessions-toggle"
      >
        <KeyRound className="w-3.5 h-3.5" />
        {open ? "Hide" : "Manage"} login sessions ({sessions.length || 0})
        <span
          className="ml-1.5 px-1.5 py-0.5 rounded-full text-[9px] uppercase tracking-wider font-semibold"
          style={{
            background: "color-mix(in srgb, var(--primary-bg) 18%, transparent)",
            color: "var(--primary-bg)",
            border: "1px solid color-mix(in srgb, var(--primary-bg) 30%, transparent)",
          }}
          title="Login sessions power autoposting, which is in beta."
        >
          Beta
        </span>
      </button>
      {open && (
        <div className="sp-glass rounded-2xl p-5 mt-3 space-y-4">
          <div>
            <h3 className="text-sm font-medium mb-1 flex items-center gap-2">
              <KeyRound className="w-4 h-4 text-orange-400" /> Login sessions for autoposting
              <span
                className="px-1.5 py-0.5 rounded-full text-[9px] uppercase tracking-wider font-semibold"
                style={{
                  background: "color-mix(in srgb, var(--primary-bg) 18%, transparent)",
                  color: "var(--primary-bg)",
                  border: "1px solid color-mix(in srgb, var(--primary-bg) 30%, transparent)",
                }}
              >
                Beta
              </span>
            </h3>
            <p className="text-xs sp-subtle leading-relaxed">
              Upload one-time browser cookies per platform so agents can publish on your behalf.
              We walk you through it — no technical skills required. Your cookies are stored opaquely
              and never returned by the API; only the headless browser sees them when a Mission runs.
              <br />
              <strong className="text-[var(--fg)]">Heads up — autoposting is in beta.</strong>{" "}
              Sessions can expire, platforms can change their UI, and a post may occasionally
              fail to publish. Always verify on the platform after a scheduled autopost.
            </p>
          </div>

          {loading ? (
            <div className="text-xs sp-subtle">Loading sessions…</div>
          ) : sessions.length === 0 ? (
            <div className="text-xs sp-subtle italic">No sessions uploaded yet.</div>
          ) : (
            <div className="space-y-1">
              {sessions.map((s) => (
                <div key={s.id} className="flex items-center gap-3 text-xs py-1.5 border-t border-white/5"
                     data-testid={`session-row-${s.platform}`}>
                  <span className="font-medium text-white/90">{PLATFORMS[s.platform]?.label || s.platform}</span>
                  {s.label && <span className="sp-subtle">· {s.label}</span>}
                  <span className="sp-subtle">
                    {s.cookies_count} cookie{s.cookies_count === 1 ? "" : "s"}
                  </span>
                  <span className="ml-auto sp-subtle">
                    updated {formatScheduleLocal(s.updated_at)}
                  </span>
                  <button type="button" onClick={() => testExisting(s.platform)}
                          title="Open this platform in a headless browser and verify cookies work"
                          className="sp-btn sp-btn-ghost !py-0.5 !px-1.5 text-[11px]"
                          data-testid={`session-test-${s.platform}`}>
                    <ShieldCheck className="w-3 h-3" /> Test
                  </button>
                  <button type="button" onClick={() => remove(s.platform)}
                          className="sp-btn sp-btn-ghost !py-0.5 !px-1.5 text-[11px] hover:!text-red-300"
                          data-testid={`session-delete-${s.platform}`}>
                    <Trash2 className="w-3 h-3" />
                  </button>
                </div>
              ))}
            </div>
          )}

          <div className="flex items-center justify-end pt-4 border-t border-white/10">
            <button
              type="button"
              onClick={() => setWizardOpen(true)}
              className="sp-btn sp-btn-primary text-xs"
              data-testid="session-add-btn"
            >
              <UploadCloud className="w-3.5 h-3.5" />
              Add a login session
            </button>
          </div>

          {wizardOpen && (
            <SessionWizard
              orgId={orgId}
              onClose={() => { setWizardOpen(false); load(); }}
            />
          )}
        </div>
      )}
    </div>
  );
}


// ── The 3-step add-session modal ──────────────────────────────────────────
function SessionWizard({ orgId, onClose }) {
  const [step, setStep] = useState(1);
  const [platform, setPlatform] = useState("twitter");
  const [label, setLabel] = useState("");
  const [raw, setRaw] = useState("");          // uploaded file contents
  const [fileName, setFileName] = useState("");
  const [dragging, setDragging] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);  // {ok, message}
  const [saving, setSaving] = useState(false);

  // ESC to close — convention-aligns the wizard with other modals in the app.
  useEffect(() => {
    const handler = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  const wiz = PLATFORM_WIZARD[platform] || PLATFORM_WIZARD.twitter;

  const handleFiles = async (files) => {
    const file = files?.[0];
    if (!file) return;
    // Accept anything text-ish — .txt / .json are the common ones but
    // some browsers hand us application/octet-stream for cookies.txt.
    if (file.size > 2 * 1024 * 1024) {
      toast.error("That file is bigger than 2 MB — cookies files are tiny; please pick the right one.");
      return;
    }
    const text = await file.text();
    setRaw(text);
    setFileName(file.name);
    setTestResult(null);  // invalidate any previous test
  };

  const onDrop = (e) => {
    e.preventDefault();
    setDragging(false);
    handleFiles(e.dataTransfer.files);
  };

  const runTest = async () => {
    if (!raw.trim()) { toast.error("Upload or paste your cookies first."); return; }
    setTesting(true);
    setTestResult(null);
    try {
      const { data } = await api.post(
        `/orgs/${orgId}/social/sessions/${platform}/test`,
        { cookies_text: raw },
      );
      setTestResult(data);
      if (!data.ok) toast.error(data.message);
      else toast.success(data.message);
    } catch (err) {
      const msg = err?.response?.data?.detail || "Test failed";
      setTestResult({ ok: false, message: msg });
      toast.error(msg);
    } finally { setTesting(false); }
  };

  const save = async () => {
    if (!raw.trim()) { toast.error("Upload your cookies first."); return; }
    setSaving(true);
    try {
      await api.post(`/orgs/${orgId}/social/sessions`, {
        platform,
        cookies_text: raw,
        label: label.trim(),
      });
      toast.success(`${wiz.label} session saved`);
      onClose();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Save failed");
    } finally { setSaving(false); }
  };

  return createPortal(
    <div className="fixed inset-0 z-[100] flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm"
         onClick={(e) => e.target === e.currentTarget && onClose()}
         data-testid="session-wizard">
      <div className="sp-glass-strong rounded-2xl w-full max-w-xl max-h-[90vh] overflow-y-auto p-6 space-y-5"
           style={{ border: "1px solid rgba(255,255,255,0.12)" }}>
        <div className="flex items-start gap-3">
          <div className="w-9 h-9 rounded-xl bg-orange-500/15 flex items-center justify-center shrink-0">
            <KeyRound className="w-4 h-4 text-orange-400" />
          </div>
          <div className="flex-1">
            <h3 className="text-base font-medium">Add a login session</h3>
            <p className="text-xs sp-subtle">
              Step {step} of 3 — {step === 1 ? "pick the platform" : step === 2 ? "export your cookies" : "upload & verify"}
            </p>
          </div>
          <button type="button" onClick={onClose}
                  className="sp-btn sp-btn-ghost !p-1.5"
                  data-testid="wizard-close">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Progress bar */}
        <div className="flex gap-1">
          {[1, 2, 3].map((n) => (
            <div key={n} className={`h-1 flex-1 rounded-full transition-colors ${
              step >= n ? "bg-orange-500" : "bg-white/10"
            }`} />
          ))}
        </div>

        {/* STEP 1: platform */}
        {step === 1 && (
          <div className="space-y-3" data-testid="wizard-step-1">
            <label className="text-xs sp-subtle uppercase tracking-widest">Which platform?</label>
            <div className="grid grid-cols-2 gap-2">
              {AUTOPOST_PLATFORMS.map((p) => (
                <button
                  key={p}
                  type="button"
                  onClick={() => setPlatform(p)}
                  className={`rounded-xl px-3 py-2.5 text-left text-sm transition border ${
                    platform === p
                      ? "bg-orange-500/15 border-orange-400/40 text-white"
                      : "bg-white/[0.02] border-white/10 text-white/70 hover:bg-white/[0.05]"
                  }`}
                  data-testid={`wizard-platform-${p}`}
                >
                  <div className="font-medium">{PLATFORM_WIZARD[p]?.label || p}</div>
                  <div className="text-[11px] sp-subtle mt-0.5">{PLATFORM_WIZARD[p]?.domain}</div>
                </button>
              ))}
            </div>
            <input value={label} onChange={(e) => setLabel(e.target.value)}
                   placeholder={`Label (optional) — e.g. "${wiz.label} brand account"`}
                   className="w-full rounded-lg px-3 py-2 text-sm"
                   style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
                   data-testid="wizard-label-input"
            />
          </div>
        )}

        {/* STEP 2: export instructions */}
        {step === 2 && (
          <div className="space-y-3 text-sm" data-testid="wizard-step-2">
            <div className="text-white/80">
              You're going to export browser cookies for <b>{wiz.label}</b>. It's a 3-click process:
            </div>
            <ol className="space-y-2.5 text-sm list-decimal pl-5 text-white/80">
              <li>
                Install the free <b>Get cookies.txt LOCALLY</b> Chrome extension if you don't have it yet.
                <div className="mt-1">
                  <a href={COOKIES_EXTENSION_URL} target="_blank" rel="noreferrer"
                     className="inline-flex items-center gap-1 text-[11px] px-2 py-1 rounded-md bg-white/10 hover:bg-white/20 transition"
                     data-testid="wizard-extension-link">
                    <ExternalLink className="w-3 h-3" /> Open in Chrome Web Store
                  </a>
                </div>
              </li>
              <li>
                Make sure you're <b>logged in to {wiz.label}</b>. Open{" "}
                <a href={wiz.home} target="_blank" rel="noreferrer"
                   className="underline decoration-orange-400/60 hover:text-orange-300"
                   data-testid="wizard-platform-link">
                  {wiz.home}
                </a>{" "}
                in a new tab.
              </li>
              <li>
                Click the extension icon → pick <b>Current Site</b> (or enter <code className="px-1 bg-white/10 rounded">{wiz.domain}</code>) → <b>Export</b>. A <code className="px-1 bg-white/10 rounded">.txt</code> file downloads.
              </li>
            </ol>
            <div className="text-[11px] sp-subtle italic pt-2">
              Your cookies never leave this browser until you drop them here on the next step. We never log or re-serve them.
            </div>
          </div>
        )}

        {/* STEP 3: drag-drop + test */}
        {step === 3 && (
          <div className="space-y-3" data-testid="wizard-step-3">
            <div
              onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={onDrop}
              className={`rounded-xl border-2 border-dashed p-6 text-center transition ${
                dragging
                  ? "border-orange-400 bg-orange-500/10"
                  : raw
                    ? "border-emerald-400/40 bg-emerald-500/5"
                    : "border-white/15 bg-white/[0.02]"
              }`}
              data-testid="wizard-dropzone"
            >
              {raw ? (
                <div className="space-y-1">
                  <CheckCircle2 className="w-6 h-6 mx-auto text-emerald-400" />
                  <div className="text-sm font-medium">{fileName || "Pasted content"}</div>
                  <div className="text-[11px] sp-subtle">
                    {(raw.length / 1024).toFixed(1)} KB ready to upload
                  </div>
                  <button type="button"
                          onClick={() => { setRaw(""); setFileName(""); setTestResult(null); }}
                          className="sp-btn sp-btn-ghost text-xs mt-2"
                          data-testid="wizard-clear-file">
                    <X className="w-3 h-3" /> Remove
                  </button>
                </div>
              ) : (
                <>
                  <UploadCloud className="w-8 h-8 mx-auto text-white/40" />
                  <div className="text-sm mt-2 text-white/80">
                    Drag the <code className="px-1 bg-white/10 rounded">.txt</code> file here, or
                  </div>
                  <label className="sp-btn sp-btn-ghost text-xs mt-2 cursor-pointer inline-flex">
                    <UploadCloud className="w-3.5 h-3.5" /> Choose a file
                    <input type="file" className="hidden" accept=".txt,.json,text/plain,application/json"
                           onChange={(e) => handleFiles(e.target.files)}
                           data-testid="wizard-file-input" />
                  </label>
                  <details className="mt-3 text-[11px] sp-subtle">
                    <summary className="cursor-pointer hover:text-white/80">or paste the text manually</summary>
                    <textarea
                      onChange={(e) => { setRaw(e.target.value); setFileName("pasted"); setTestResult(null); }}
                      placeholder="Paste the cookies.txt contents or a storage_state JSON here"
                      rows={4}
                      className="w-full rounded-lg px-2 py-1.5 text-[11px] font-mono resize-y mt-2"
                      style={{ background: "var(--input-bg)", border: "1px solid var(--input-border)", color: "var(--fg)" }}
                      data-testid="wizard-paste-input"
                    />
                  </details>
                </>
              )}
            </div>

            {raw && (
              <div className="flex items-center gap-2">
                <button type="button" onClick={runTest} disabled={testing}
                        className="sp-btn sp-btn-ghost text-xs disabled:opacity-50"
                        data-testid="wizard-test-btn"
                        title="Headlessly open the platform and verify these cookies work">
                  {testing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <ShieldCheck className="w-3.5 h-3.5" />}
                  {testing ? "Testing…" : "Test my session"}
                </button>
                {testResult && (
                  <div className={`text-xs flex-1 ${testResult.ok ? "text-emerald-300" : "text-orange-300"}`}
                       data-testid="wizard-test-result">
                    {testResult.ok ? "✓" : "⚠"} {testResult.message}
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* Navigation */}
        <div className="flex items-center justify-between pt-4 border-t border-white/10">
          <button
            type="button"
            onClick={() => step > 1 ? setStep(step - 1) : onClose()}
            className="sp-btn sp-btn-ghost text-xs"
            data-testid="wizard-back"
          >
            {step === 1 ? "Cancel" : "Back"}
          </button>
          {step < 3 ? (
            <button
              type="button"
              onClick={() => setStep(step + 1)}
              className="sp-btn sp-btn-primary text-xs"
              data-testid="wizard-next"
            >
              Next <ChevronRight className="w-3.5 h-3.5" />
            </button>
          ) : (
            <button
              type="button"
              onClick={save}
              disabled={saving || !raw.trim()}
              className="sp-btn sp-btn-primary text-xs disabled:opacity-50"
              data-testid="wizard-save"
            >
              {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
              {saving ? "Saving…"
                : testResult && !testResult.ok ? "Save anyway"
                : "Save session"}
            </button>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
