"""Resourceful-agent artifacts.

When an agent can't perform an action directly because the underlying
integration isn't built yet (Google Calendar, Slack, GitHub, Notion, Stripe,
email, etc.), it should fall back to producing the *result* of that action as
a downloadable artifact and quietly hand it to the user.

Agents emit artifacts inline using a fenced syntax that's easy for LLMs to
produce reliably and easy for us to parse:

    [[ARTIFACT name="meeting-with-john.ics" mime="text/calendar" title="Meeting with John"]]
    BEGIN:VCALENDAR
    ...
    END:VCALENDAR
    [[/ARTIFACT]]

This module:
  * provides the system-prompt directive injected into every agent
  * extracts + persists artifact blocks from agent replies
  * exposes an Org Documents library router
  * serves artifact files over HTTP (capability-URL pattern, like other media)
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)

# ── System prompt directive ────────────────────────────────────────────────
# Injected at the END of every agent's system prompt by build_system_prompt.
# The agents already know who they are and what their org/team's soul is —
# this just teaches the *behavior* of being resourceful and *how* to emit a
# downloadable artifact when they hit a missing-integration wall.
RESOURCEFULNESS_DIRECTIVE = """

— RESOURCEFULNESS PROTOCOL —
You are a resourceful agent. Never refuse a request just because we don't
have a direct integration for the third-party service involved. Instead,
generate the *result* of the action as a downloadable, ready-to-use artifact
and quietly hand it to the user — they can use it immediately and we'll wire
the native integration in later.

— REAL-TIME DATA RULE —
Your training data is months or years old. For anything involving current
events, recent product launches, today's news, dates, prices, version
numbers, who-leads-what at a company, or market data: prefer the live web
search results that the system injects into your context over your own
memory. If the system gave you sources or a search summary, ground your
answer in those. If you don't have live results and the user is asking
about something time-sensitive, say so honestly and offer to search again,
or write the answer in a way that flags what's verifiable vs. what would
need fresh research — do NOT confidently state stale facts as current.

— HONESTY ABOUT IDENTIFIERS —
NEVER invent identifiers, URLs, account IDs, API keys, ticket numbers,
conversation IDs, order numbers, or any other reference the system did not
explicitly give you. If a user asks "what's the conversation ID?" or any
similar question, answer honestly: if you can see it in the URL or context,
share it verbatim; if not, say "I don't have that ID exposed — you can find
it in the address bar" and stop. Fabricating an ID like "AIENG-…" or
"TICKET-…" erodes trust and misroutes support requests. Hard rule: if you
haven't seen the exact ID in the prompt or conversation history, you don't
have it.

You DO have access to these tools (already wired in, just describe what you
want and it happens):
  • Image generation (Nano Banana / Gemini) — render any visual the user asks
    for. Just describe it; the system handles it.
  • Video generation (Kie.ai / Veo) — short videos, 60–180s render.
  • Real-time web search (Brave + Perplexity Sonar fallback) — fresh facts
    are injected into your context automatically when relevant.
  • Org-uploaded files — PDFs, docs, txt the org has uploaded are in your
    context already. Cite them by name.
  • Social Queue — the user can one-click queue any of your replies as a
    drafted social post.
  • Schedules — recurring prompts run on a cron / one-shot schedule.
  • Brand assets — logo, colors, tagline, visual style are honored in any
    image generation automatically.

You DO NOT have direct API access to: Google Calendar, Outlook, Gmail/SMTP
sending, Slack, Discord, Telegram, GitHub, Notion, Linear, Jira, Asana,
Trello, Stripe/payments, HubSpot, Salesforce, Twilio/SMS, LinkedIn/X/Meta
publishing, Zoom/Meet, Drive/Dropbox/S3 uploads. For ANY request that would
need one of these, fall back to a self-serve artifact.

Self-serve artifacts you can emit (pick the most useful format for the job):
  • .ics            — calendar invites & meeting blocks
  • .vcf            — contact cards
  • .eml            — RFC-5322 email drafts (importable into Gmail/Outlook)
  • .csv / .tsv     — spreadsheets, exports, lead lists
  • .json           — structured payloads, API request bodies
  • .md             — briefs, plans, checklists, meeting notes, SOPs, posts
  • .txt            — anything plain-text
  • .html           — landing page mockups, email HTML, dashboards
  • .sh / .py / .js — automation scripts the user can run themselves
  • .sql            — query bodies / migrations
  • .pdf            — formatted business documents (the body you write is
                      MARKDOWN — the system transcodes to a real PDF with
                      proper headings, lists, tables, and page numbers).
  • .docx           — Word documents — same deal, body is Markdown and the
                      system produces a real .docx with native styles.

IMPORTANT — PDF / DOCX BODY FORMAT.
For `.pdf` and `.docx` artifacts write Markdown in the body (headings with
`#`, bullet lists with `-`, numbered lists with `1.`, **bold**, *italic*,
GFM tables with `|`, code blocks in triple backticks, blockquotes with `>`).
The system will render it into a polished binary document the user can
download directly from the chat. Do NOT attempt to emit PDF bytes, LaTeX,
base64, or HTML in the body — just clean Markdown.

IMPORTANT — TEXT & MARKDOWN-DRIVEN ONLY.
This artifact mechanism is for text-based files AND Markdown-driven
documents (.pdf/.docx — see above). Do NOT try to emit images (.png/
.jpg/.webp), video (.mp4/.mov), audio (.mp3/.wav), Excel/PowerPoint, or
legacy Office formats (.doc/.xls/.ppt) via [[ARTIFACT]] — the body would
arrive as gibberish text with the wrong extension.

FOR IMAGES — USE THE [[IMAGE]] BLOCK INSTEAD.
You CAN attach generated images to your reply by emitting a self-closing
`[[IMAGE]]` tag. The runtime intercepts these, renders the image via
Nano Banana (default) or GPT Image 1, and shows it inline in the chat
(or as a real photo on Telegram). Place one anywhere in your reply:

[[IMAGE prompt="A flat-design Instagram post: minimalist, brand colors navy + warm orange, hero text 'Same-day appointments', subtle medical icons, square 1:1" aspect="1:1"]]

Recognised attrs:
  prompt   (REQUIRED) — full visual description. Be specific about
                         composition, style, palette, and any overlay text.
  aspect   (OPTIONAL) — "1:1" (default — IG posts), "9:16" (stories /
                         Reels), "16:9" (banners), "4:5" (IG portrait),
                         "1.91:1" (LinkedIn/Twitter). Always set this
                         for social graphics.
  model    (OPTIONAL) — "nano-banana" (default, fast) | "gpt-image-1"
                         (slower, sometimes better at rendered text).
  title    (OPTIONAL) — short caption shown beneath the image in chat.

Hard rules for image blocks:
  1. SELF-CLOSING. There is NO [[/IMAGE]] — just the single opening tag.
  2. ONE TAG = ONE IMAGE. If you need multiple variants, emit multiple tags.
  3. Do NOT mention the [[IMAGE]] tag in prose. The runtime swaps it out
     before the user sees your reply.
  4. NEVER promise an image "shortly" without ALSO emitting the [[IMAGE]]
     tag in the SAME reply. There is no background image generation.
     If the user asks for an image, attach one — don't preview it.

When the user asks for an Instagram post, ad creative, social graphic,
launch banner, podcast cover, blog hero, OR a "draft a post for X" —
ALWAYS emit BOTH:
  • An [[ARTIFACT name="post-caption.md" ...]] block with the caption /
    copy / hashtag list, AND
  • An [[IMAGE prompt="..." aspect="..."]] block with the visual.
That's the whole deliverable in ONE turn. Do not split it across replies.

MANDATORY — AUTO-IMAGE FOR SOCIAL POSTS.
For ANY of the following request types you MUST emit an [[IMAGE]] block
in the SAME reply as the caption — without being asked, without
previewing, without offering to "create a visual concept later":
  • Instagram post, Story, Reel, carousel slide
  • Facebook post, cover photo
  • LinkedIn post, header, banner
  • Twitter / X post or header
  • TikTok cover / thumbnail
  • YouTube thumbnail, banner, end-card
  • Pinterest pin
  • Reddit post art
  • Threads / Bluesky / Mastodon post
  • Generic "social media post", "social graphic", "ad creative",
    "launch graphic", "promo image", "campaign creative"

The ONLY exception is when the user explicitly opts out with phrases
like "text only", "no image", "just the caption", "caption only",
"draft only the copy", "skip the visual", "I have my own image", or
similar unambiguous opt-out language. Anything else — including
ambiguous requests like "draft a post" or "I need a post for X" — gets
BOTH the caption artifact AND the image block, every time, in this
turn. Telling the user "I can't create or send images" is FORBIDDEN —
you CAN, by emitting the [[IMAGE]] block above.

For CAROUSELS (multi-slide IG / LinkedIn / TikTok), emit ONE [[IMAGE]]
block per slide so a "5-slide carousel" request produces 5 images, not
just the cover. Keep the prompts visually consistent (same palette,
typography, layout grid) so the slides feel like a set.

(Inside a Mission you can also call `generate_image` as a tool — but in
chat replies, [[IMAGE]] is the canonical path.)

To emit an artifact, place a block like this anywhere in your reply (you can
emit multiple per reply if it helps). The user's chat will render the
artifact as a downloadable file attached to your message AND save it to the
Org's Documents library so it's findable later.

[[ARTIFACT name="filename-with-extension" mime="optional/mime" title="Optional human title"]]
…raw file contents go here, exactly as the file should be saved…
[[/ARTIFACT]]

Hard rules for artifact blocks:
  1. The opening tag must be on its own line.
  2. The closing tag [[/ARTIFACT]] must be on its own line.
  3. Filename is required and must end with one of the extensions above.
  4. Content is treated as raw bytes — do NOT wrap it in markdown code fences,
     do NOT escape it, do NOT add commentary inside the block.
  5. Don't mention the existence of the [[ARTIFACT]] block in your prose.
     Just talk naturally ("Here's a calendar invite for that meeting." — the
     UI will surface the file automatically).

Be PROACTIVE: when a structured deliverable would make your reply more
useful (a launch plan, a content calendar, a checklist, a script, a brief),
go ahead and attach it as an artifact in addition to your prose answer.
Don't ask permission first. Default to action.

When the user explicitly asks for a "PDF", "document", "brief", "report",
"Word doc", or similar — ALWAYS emit a `.pdf` (preferred) or `.docx`
artifact as part of your reply. The body is Markdown; the system converts
it. Do not just say "I'll generate the PDF shortly" — actually emit the
[[ARTIFACT]] block right now so the user gets the download link in this
same turn. Never promise a future delivery that won't arrive; if you
can't produce the file in this turn, say so honestly.

CRITICAL — NO EMPTY PROMISES. There is NO BACKGROUND WORK. There is no
"later" — your reply IS the entire turn. The user is sitting in a chat
window staring at your message right now. Phrases like:
  • "I'll get back to you with…"
  • "I'll prepare/finish/send the report/audit/plan shortly"
  • "I'll prioritize this and have it over to you soon"
  • "Looking forward to delivering…"
  • "Let me know if you'd like me to proceed and I'll start on it"
  • "I'm working on it / will work on it / will keep you posted"
…are FORBIDDEN unless you also attach the deliverable in this very same
reply (via an [[ARTIFACT]] block for text/PDF/Word, an [[IMAGE]] block
for rendered images, or, inside a Mission, by calling the appropriate
tool). If you cannot deliver the deliverable in this turn, say so
honestly:
  "Here's what I can do right now: <X>. To give you <full deliverable>
   I'd need <a/b/c> — share those and I'll draft it in this same chat."
End every reply that promises future work with a CONCRETE, CLICKABLE
NEXT STEP for the user (e.g. "Want me to draft the audit now?" — yes/no
question they can answer in two words). Never leave the user wondering
whether anything is happening. Time is the user's most valuable resource.

CRITICAL — NEVER FABRICATE FACTS, METRICS, OR DATA.
You DO NOT have live access to social profiles, analytics platforms,
private dashboards, or paywalled content. When the user pastes a URL
(LinkedIn profile, X/Twitter handle, Instagram, a competitor's pricing
page, etc.) the runtime fetches it server-side and injects the rendered
text into your prompt as a "GROUND-TRUTH PAGE CONTENT" block. ANYTHING
you claim about that URL must come from that block — verbatim.

If the user pastes URLs and you DON'T see a GROUND-TRUTH block, the fetch
either failed or the runtime didn't run (this happens in mission mode
where you have a `browse_url` tool — call it first). NEVER guess
follower counts, engagement rates, posting frequency, recent post titles,
audience size, ad spend, conversion rates, or any number that could
plausibly look real but isn't. If the data isn't in front of you, say
EXACTLY: "I can't see that metric — to give you a real number I'd need
[the platform's analytics export / login cookies / a screenshot]. Want
to share one of those?" Inventing a plausible-sounding number is the
single worst thing you can do — it destroys the user's trust and the
business decisions they make from your output.

Stay quiet about missing native integrations — don't apologize, don't say
"coming soon", don't lecture about API limits. Just deliver the artifact.
"""

# Append the new app-artifact directive so every agent learns the
# [[APP]] pattern and the inline-HTML interactive-app convention.
try:
    from app_artifacts import APP_ARTIFACT_DIRECTIVE as _APP_DIRECTIVE
    RESOURCEFULNESS_DIRECTIVE = RESOURCEFULNESS_DIRECTIVE + _APP_DIRECTIVE
except Exception:  # pragma: no cover — module not available at lint time
    pass


# Concrete few-shot examples appended LAST so they're the most-recent
# context the LLM sees before deciding what to emit. Without these,
# gpt-4.1-mini tends to default to deferential "Want me to generate
# the image?" prose instead of just emitting the block. Examples are
# the highest-leverage prompt-engineering trick we have here.
_FEWSHOT_EXAMPLES = """

CONCRETE EXAMPLES — STUDY THESE BEFORE REPLYING.

# Example 1 — User: "Draft an IG post for AZ Pain Doctors."
# CORRECT reply (note: caption attached as .md, image attached, NO permission asked):
> Here's a clean square IG post for AZ Pain Doctors — caption and visual ready to go.
>
> [[ARTIFACT name="az-pain-doctors-ig-post.md" mime="text/markdown" title="AZ Pain Doctors — IG caption"]]
> Tired of living with chronic pain? You don't have to. ✋
>
> At AZ Pain Doctors we combine advanced diagnostics with personalised treatment plans so you can finally get a clear path to relief — without the runaround.
>
> ✅ Same-day appointments
> ✅ Insurance-friendly
> ✅ Board-certified specialists
>
> Tap the link in our bio to book today.
>
> #ArizonaPain #ChronicPainRelief #PainManagement #AZHealth #SameDayCare
> [[/ARTIFACT]]
>
> [[IMAGE prompt="A clean modern Instagram square post for a Phoenix pain-management clinic. Calm sage-green and warm-cream palette. Soft photographic style: a relieved adult patient outdoors at golden hour, hands relaxed, no medical iconography. Bold serif headline overlay reads 'Relief, on your schedule' in the upper third; small wordmark 'AZ Pain Doctors' bottom-left. Square 1:1, well-lit, on-brand, professional." aspect="1:1" title="AZ Pain Doctors IG post"]]

# Example 2 — User: "I need a launch graphic for LinkedIn for our new pricing page."
# CORRECT reply (1.91:1 banner aspect, no permission asked):
> Here's the LinkedIn launch graphic and post copy.
>
> [[ARTIFACT name="pricing-launch-linkedin.md" mime="text/markdown" title="LinkedIn launch post"]]
> Pricing should be simple. Today it is.
>
> We just rolled out a transparent new pricing page — three tiers, no hidden fees, and everything you need to compare side-by-side.
>
> Take a look →
> [[/ARTIFACT]]
>
> [[IMAGE prompt="A polished LinkedIn launch banner. Off-white background with subtle paper-texture. Three vertical pricing cards drawn in a clean editorial illustration style — minimal, no real numbers, just card shapes with hierarchy. Bold sans-serif headline 'Pricing should be simple' top-left. 1.91:1 aspect ratio." aspect="1.91:1" title="Pricing launch — LinkedIn banner"]]

# Example 3 — User: "Draft an IG post for our new latte. Text only, I have my own photo."
# CORRECT reply (user EXPLICITLY opted out — no [[IMAGE]] block):
> Here's the caption only, since you have the photo handled.
>
> [[ARTIFACT name="latte-launch-ig.md" mime="text/markdown" title="Latte launch — IG caption"]]
> Meet the Honeyed Lavender Latte. ☕💜
> Local lavender, raw clover honey, oat or whole milk. On the menu through August.
>
> #SmallBatch #LavenderLatte #CafeLife
> [[/ARTIFACT]]

# Example 4 — User (after a caption-only reply): "I would like an image"
# CORRECT reply (do NOT say "I can't create images" — just emit the block):
> Here you go.
>
> [[IMAGE prompt="<re-derive a strong visual brief from the previous caption + soulprint context>" aspect="1:1" title="<short title>"]]

# WRONG reply (FORBIDDEN — never write anything like this):
> Want me to generate a matching branded image for the post as well?
> I'll create the image in my next message.
> I can't create or send images. Let me know if you want a visual concept I can describe.
> The platform will generate the image for you on your next request.

These four wrong replies are the SINGLE MOST COMMON failure mode and
they all break the user's trust. NEVER ASK PERMISSION. NEVER PROMISE
FOR LATER. NEVER REFUSE. If the user asked for a post or graphic of
any kind, emit the [[IMAGE]] block alongside the caption in the SAME
turn. That is the entire rule.
"""

RESOURCEFULNESS_DIRECTIVE = RESOURCEFULNESS_DIRECTIVE + _FEWSHOT_EXAMPLES

# ── Block parser ───────────────────────────────────────────────────────────
# Tolerant regex: optional whitespace around tags, dotall over the body, and
# we accept either single or double quotes around attribute values.
_ARTIFACT_RE = re.compile(
    r"\[\[ARTIFACT\s+([^\]]+?)\]\]\s*\n?(.*?)\n?\s*\[\[/ARTIFACT\]\]",
    re.DOTALL | re.IGNORECASE,
)
_ATTR_RE = re.compile(r"""(\w+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")

ALLOWED_EXTS = {
    "ics", "vcf", "eml", "csv", "tsv", "json", "md", "txt",
    "html", "htm", "sh", "py", "js", "ts", "yaml", "yml", "sql",
    "xml", "rtf", "log", "ini", "toml", "env",
    # Binary business docs — the body is Markdown and we synthesize real
    # bytes server-side via artifact_synthesis.synthesize(). Agents write
    # Markdown; users get a polished .pdf / .docx / .xlsx / .pptx / .csv
    # they can download.
    "pdf", "docx", "xlsx", "pptx", "csv",
}

# Extensions that `write_artifact` must NEVER accept — they're binary or
# need a dedicated generation tool (generate_image / generate_video / etc).
# If the agent tries to emit one of these via a text artifact block, we
# reject with a clear error pointing to the right tool instead of
# silently turning the body into gibberish-text-file-with-.png-name.
BINARY_EXTS = {
    # Images
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "svg", "ico",
    "heic", "heif", "avif",
    # Video
    "mp4", "mov", "avi", "mkv", "webm", "flv", "mpeg", "mpg", "wmv", "m4v",
    # Audio
    "mp3", "wav", "aac", "flac", "ogg", "m4a", "opus", "aiff",
    # Binary docs we can't synthesize from Markdown safely. .pdf, .docx,
    # .xlsx, .pptx, and .csv are handled separately via
    # artifact_synthesis.synthesize() and are listed in ALLOWED_EXTS above.
    "doc", "xls", "ppt",
    "zip", "tar", "gz", "7z", "rar",
}

# Extensions for which the body is Markdown that we transcode into real
# binary bytes at save time. Mapping is authoritative — any ext listed
# here MUST also be in ALLOWED_EXTS.
SYNTHESIZED_BINARY_EXTS = {"pdf", "docx", "xlsx", "pptx", "csv"}

DEFAULT_MIME = {
    "ics": "text/calendar", "vcf": "text/vcard", "eml": "message/rfc822",
    "csv": "text/csv", "tsv": "text/tab-separated-values",
    "json": "application/json", "md": "text/markdown", "txt": "text/plain",
    "html": "text/html", "htm": "text/html",
    "sh": "application/x-sh", "py": "text/x-python", "js": "application/javascript",
    "ts": "application/typescript", "yaml": "application/x-yaml", "yml": "application/x-yaml",
    "sql": "application/sql", "xml": "application/xml", "rtf": "application/rtf",
    "log": "text/plain", "ini": "text/plain", "toml": "text/plain", "env": "text/plain",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9._\-]")

# Image extensions an agent might try to embed via base64 in an [[ARTIFACT]]
# block. We accept these (decode + save as a real image attachment) when the
# body decodes to bytes whose magic number matches the requested format.
# Otherwise we drop the block silently — the user shouldn't see broken
# base64 in chat regardless of what the agent did.
_IMG_EXTS_FROM_B64 = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}

# First-N-bytes signatures we trust as "real" image bytes. We don't care
# about every edge case — just enough to reject obvious gibberish that an
# LLM hallucinated as base64.
_IMG_MAGIC = {
    "png":  b"\x89PNG\r\n\x1a\n",
    "jpg":  b"\xff\xd8\xff",
    "jpeg": b"\xff\xd8\xff",
    "gif":  (b"GIF87a", b"GIF89a"),
    "webp": (b"RIFF",),  # full check needs WEBP at offset 8
    "bmp":  b"BM",
}


def _decode_b64_image(body: str, ext: str) -> Optional[bytes]:
    """Try to decode `body` as base64 image bytes for `ext`. Returns the
    decoded bytes IF the magic number matches the requested format,
    otherwise None. We're permissive about whitespace / line wraps in the
    base64 (LLMs often add them), and tolerate a `data:image/...` prefix."""
    import base64
    if not body:
        return None
    s = body.strip()
    # Strip a data: URI prefix if present
    if s.startswith("data:"):
        comma = s.find(",")
        if comma > 0:
            s = s[comma + 1:]
    # Drop any whitespace / newlines the LLM injected
    s = "".join(s.split())
    # Must be at least a kilobyte to be plausibly real (a 1px PNG ~70 bytes
    # but real generated images are >5KB; reject the trivial cases).
    if len(s) < 64:
        return None
    try:
        data = base64.b64decode(s, validate=False)
    except Exception:
        return None
    expected = _IMG_MAGIC.get(ext)
    if expected is None:
        return None
    if isinstance(expected, tuple):
        if not any(data.startswith(sig) for sig in expected):
            return None
        # Extra check for WebP: bytes 8..12 must be 'WEBP'
        if ext == "webp" and not (len(data) > 12 and data[8:12] == b"WEBP"):
            return None
    elif not data.startswith(expected):
        return None
    # Sanity: real images are at least ~64 bytes; reject truncated bytes.
    if len(data) < 64:
        return None
    return data



def _safe_filename(raw: str) -> Tuple[str, str]:
    """Sanitise a user-supplied filename. Returns (clean_name, ext_lower).

    Returns ``("", "")`` when the requested extension is binary (image,
    video, audio, PDF, etc.) so the caller can drop the block — never
    silently coerce binary content to .txt because the body would be
    base64 gibberish saved with a useless extension.
    """
    name = (raw or "").strip().replace("/", "_").replace("\\", "_")
    name = _SAFE_NAME_RE.sub("_", name)
    if not name or name.startswith("."):
        name = f"artifact-{uuid.uuid4().hex[:8]}.txt"
    if "." not in name:
        name += ".txt"
    ext = name.rsplit(".", 1)[-1].lower()
    if ext in BINARY_EXTS:
        # Refuse — the caller will drop the block and surface the issue.
        return "", ""
    if ext not in ALLOWED_EXTS:
        # Unknown but plausibly text — coerce to .txt so we never lose
        # the body. (Same legacy behaviour as before for genuinely
        # unrecognised text-ish extensions.)
        name = name.rsplit(".", 1)[0] + ".txt"
        ext = "txt"
    return name[:120], ext


def _parse_attrs(raw: str) -> dict:
    out = {}
    for m in _ATTR_RE.finditer(raw or ""):
        out[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
    return out


def extract_artifact_blocks(text: str) -> Tuple[str, List[dict]]:
    """Parse artifact blocks out of an agent reply.

    Returns `(cleaned_text, blocks)` where each block is::

        {"name": "x.md", "ext": "md", "mime": "text/markdown",
         "title": "...", "content": "raw bytes as utf-8 str"}

    The cleaned_text has the [[ARTIFACT...]]...[[/ARTIFACT]] blocks removed
    and replaced with a thin placeholder so the prose stays readable.

    If an agent tries to emit a binary extension (.png, .mp4, .pdf, etc.)
    via this block — base64 or otherwise — we DROP the block entirely
    rather than save garbage-as-.txt. A small notice is appended to the
    cleaned text so the user sees that the agent took an unsupported
    shortcut and the right path is the image-generation tool.
    """
    if not text or "[[ARTIFACT" not in text.upper():
        return text or "", []

    blocks: List[dict] = []

    def _swap(match: re.Match) -> str:
        attrs = _parse_attrs(match.group(1) or "")
        body = match.group(2) or ""
        raw_name = attrs.get("name") or attrs.get("filename") or ""
        if not raw_name:
            return ""  # malformed — drop entirely
        # Detect requested ext upfront so we can route base64 image bodies
        # before the binary-rejection check fires.
        req_ext = (raw_name.rsplit(".", 1)[-1] if "." in raw_name else "").lower()
        if req_ext in _IMG_EXTS_FROM_B64:
            decoded = _decode_b64_image(body, req_ext)
            if decoded is not None:
                # Real image bytes! Save as a binary attachment by routing
                # through the same block list with a `_raw_bytes` payload
                # the save step will detect and write directly.
                clean_name = _SAFE_NAME_RE.sub("_", raw_name).strip("_") or f"image.{req_ext}"
                if not clean_name.lower().endswith(f".{req_ext}"):
                    clean_name = f"{clean_name}.{req_ext}"
                mime = attrs.get("mime") or f"image/{'jpeg' if req_ext == 'jpg' else req_ext}"
                blocks.append({
                    "name": clean_name[:120],
                    "ext": req_ext,
                    "mime": mime,
                    "title": (attrs.get("title") or "").strip() or clean_name,
                    "_raw_bytes": decoded,
                })
                return ""
            # Body wasn't real image bytes (LLM hallucination) — drop the
            # block silently. Users should never see raw base64 in chat.
            return ""
        name, ext = _safe_filename(raw_name)
        if not name:
            # Binary extension we don't synthesize — drop the block
            # silently (no scary notice; the agent's prose around it is
            # usually enough context).
            return ""
        mime = attrs.get("mime") or DEFAULT_MIME.get(ext, "application/octet-stream")
        title = (attrs.get("title") or "").strip() or name
        blocks.append({
            "name": name, "ext": ext, "mime": mime,
            "title": title, "content": body,
        })
        # Leave nothing in the prose — the attachment row already shows the
        # filename. Per the directive agents shouldn't reference the block.
        return ""

    cleaned = _ARTIFACT_RE.sub(_swap, text)
    # Tidy up double-blank-lines we created when removing inline blocks.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, blocks


# Self-closing image block. Agents emit one of these to request an image
# be generated and attached to the chat reply. The body is empty; all
# parameters live on the tag. Multiple blocks per reply are fine (the
# extractor returns a list and the chat path generates each in parallel).
#
#   [[IMAGE prompt="A flat-design Instagram post about back pain relief, with text overlay 'Same-day appointments'" aspect="1:1" model="nano-banana"]]
#
# Recognised attrs:
#   prompt   (required) — what to render
#   aspect   (optional) — "1:1" | "9:16" | "16:9" | "4:5" | "1.91:1" …
#                          Used to nudge the model and pick caption hints.
#   model    (optional) — "nano-banana" (default) | "gpt-image-1"
#   title    (optional) — caption shown in chat
_IMAGE_RE = re.compile(
    r"\[\[IMAGE\s+([^\]]+?)\]\]",
    re.DOTALL | re.IGNORECASE,
)


def extract_image_blocks(text: str) -> Tuple[str, List[dict]]:
    """Pull every `[[IMAGE ...]]` block out of an agent reply.

    Returns `(cleaned_text, blocks)` where each block is::

        {"prompt": "...", "aspect": "1:1", "model": "nano-banana",
         "title": "..."}

    The cleaned_text has the blocks removed. The caller is responsible
    for actually generating + persisting the images — this module is
    just the LLM-output parser.

    Why not bundle this into `extract_artifact_blocks`? Two reasons:
      • Image blocks are self-closing — no body to slurp.
      • Image generation has a separate handler (Nano Banana / GPT Image
        1) with its own error semantics; mixing it into the text-file
        pipeline would force every text-file path to think about image
        failures it can't recover from.
    """
    if not text or "[[IMAGE" not in text.upper():
        return text or "", []
    blocks: List[dict] = []

    def _swap(match: re.Match) -> str:
        attrs = _parse_attrs(match.group(1) or "")
        prompt = (attrs.get("prompt") or "").strip()
        if not prompt:
            # Malformed — drop silently. The agent shouldn't ever emit
            # an empty image block (system prompt makes it explicit).
            return ""
        model = (attrs.get("model") or "nano-banana").strip().lower()
        if model not in ("nano-banana", "gpt-image-1"):
            model = "nano-banana"
        blocks.append({
            "prompt": prompt[:1800],  # Nano Banana caps around 2k
            "aspect": (attrs.get("aspect") or "").strip(),
            "model": model,
            "title": (attrs.get("title") or "").strip(),
        })
        return ""

    cleaned = _IMAGE_RE.sub(_swap, text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, blocks



# ── Empty-promise detector ───────────────────────────────────────────────
# Phrases agents reach for when they don't know how to deliver right now —
# things like "I'll get back to you", "preparing the report shortly",
# "finishing up the audit". Without an actual artifact in the reply, these
# leave the user staring at chat with zero indication anything is happening.
# We detect this state and append a clear "what now?" footer so the user
# always knows what their next move is.
_EMPTY_PROMISE_RE = re.compile(
    r"(?:^|[\s\.,;:!?\-–—\(\[])"
    r"(?:"
    r"i(?:'ll| will|'m about to| am about to)\s+(?:"
    r"prepare|finish|finalize|finalise|wrap\s*up|send|deliver|share|"
    r"send\s*over|get\s*(?:back|those|them|that|this)|provide|put\s*together|"
    r"compile|write\s*up|draft|build|create|generate|prioriti[sz]e|"
    r"start|begin|continue|keep\s*you|update\s*you|let\s*you\s*know"
    r")\b|"
    r"(?:i\s+(?:am|'m)\s+(?:working|preparing|finalizing|finalising|drafting|"
    r"compiling|writing|building|generating|gathering)|"
    r"working\s+on\s+(?:it|that|this|the\s+(?:report|audit|brief|plan|deliverable|document)))|"
    r"(?:report|audit|brief|plan|document|deliverable|file|pdf|docx?)\s+"
    r"(?:will\s+)?(?:be\s+)?(?:ready|delivered|sent|available|coming|over)\s+"
    r"(?:to\s+you\s+)?(?:soon|shortly|in\s+a\s+(?:bit|moment)|by|in\s+the\s+next)|"
    r"looking\s+forward\s+to\s+(?:delivering|sending|sharing|providing)|"
    r"i'?ll\s+(?:keep\s+you|loop\s+you|circle\s+back|follow\s*up|update\s+you)"
    r")",
    re.IGNORECASE,
)

# Light footer wording — sized to be helpful, not naggy. Italic so it reads
# as a UX nudge rather than agent prose.
_EMPTY_PROMISE_FOOTER = (
    "\n\n_——_\n"
    "_⚠️ Heads-up: I can't work in the background — every reply IS the work. "
    "If you want me to actually draft this now, reply with **\"draft it\"** "
    "(or **\"go\"**) and I'll attach a `.pdf` or `.docx` in the very next "
    "message._"
)


def has_empty_promise(text: str) -> bool:
    """Return True when an agent reply contains future-tense delivery
    promises (e.g. 'I'll send the report shortly') that the runtime can't
    actually fulfil — letting the caller decide whether to nudge the user."""
    if not text or len(text) < 12:
        return False
    return bool(_EMPTY_PROMISE_RE.search(text))


def annotate_empty_promise(text: str, has_artifacts: bool) -> str:
    """If the agent's reply contains promise-language but it didn't
    actually attach any artifact OR media, append a footer telling the
    user to reply 'draft it' so the next turn is forced to deliver."""
    if has_artifacts:
        return text
    if not has_empty_promise(text):
        return text
    return (text or "").rstrip() + _EMPTY_PROMISE_FOOTER


# ── Persistence ────────────────────────────────────────────────────────────
async def save_artifacts(
    db,
    blocks: List[dict],
    *,
    docs_root: Path,
    org_id: str,
    conversation_id: str,
    message_id: str,
    agent_id: str,
    agent_name: str,
    created_by: str,
) -> List[dict]:
    """Persist each block as a file on disk + a `documents` collection record.

    Returns a list of attachment dicts in the same shape used by the rest of
    the chat (`{filename, url, mime, kind, size, original_name}`) so they can
    be appended to the agent message and rendered by ConversationPage.js
    without any frontend changes.
    """
    if not blocks:
        return []
    folder = docs_root / org_id
    folder.mkdir(parents=True, exist_ok=True)

    attachments: List[dict] = []
    now = datetime.now(timezone.utc)

    for b in blocks:
        try:
            content = (b.get("content") or "")
            ext = b["ext"]
            raw_bytes = b.get("_raw_bytes")
            if raw_bytes is not None:
                # Pre-decoded binary path (base64 image body that we
                # successfully validated against its magic number). Skip
                # the Markdown→bytes synthesis and write the bytes
                # straight through.
                data = raw_bytes
            elif ext in SYNTHESIZED_BINARY_EXTS:
                # PDF / DOCX paths: the body is Markdown and we synthesize
                # real binary bytes here. Everything downstream (GridFS,
                # /api/files/docs, Download button) just sees bytes + mime so
                # nothing else changes for the frontend. If synthesis fails we
                # fall back to saving the raw Markdown with a `.md` extension
                # so the user still gets the content — never a broken file.
                try:
                    from artifact_synthesis import synthesize_async as _synth_async
                    data, mime_override = await _synth_async(
                        content, ext, title=b.get("title") or b["name"],
                        db=db, org_id=org_id,
                    )
                    b["mime"] = mime_override
                except Exception as exc:
                    logger.warning("Falling back to .md for failed %s synthesis: %s", ext, exc)
                    data = content.encode("utf-8", errors="replace")
                    ext = "md"
                    b["ext"] = "md"
                    # Rename to .md
                    stem = b["name"].rsplit(".", 1)[0] or "document"
                    b["name"] = f"{stem}.md"
                    b["mime"] = "text/markdown"
            else:
                data = content.encode("utf-8", errors="replace")
            disk_name = f"{uuid.uuid4().hex}.{ext}"
            fpath = folder / disk_name
            fpath.write_bytes(data)

            # Mirror to GridFS for redeploy-survival. Disk path stays
            # primary (faster + zero-overhead in dev), GridFS is the
            # fallback when the on-disk file disappears after a redeploy.
            from file_storage import save_blob_to_gridfs  # noqa: E402
            # Stamp every agent-generated artifact for tamper-evident
            # proof of integrity. The OpenTimestamps anchor is fired
            # async inside save_blob_to_gridfs and never blocks the
            # save — calendars failing means no proof, not no file.
            gridfs_id = await save_blob_to_gridfs(
                db, data, b["name"], b["mime"], stamp_for_proof=True,
            )
            # Also register in the global media_files map so the universal
            # serve handler can recover bytes by filename alone.
            if gridfs_id:
                try:
                    await db.media_files.update_one(
                        {"filename": disk_name},
                        {"$set": {
                            "filename": disk_name,
                            "gridfs_id": gridfs_id,
                            "mime": b["mime"],
                            "original_name": b["name"],
                            "size": len(data),
                            "created_at": now,
                        }},
                        upsert=True,
                    )
                except Exception:
                    pass

            doc = {
                "id": str(uuid.uuid4()),
                "org_id": org_id,
                "conversation_id": conversation_id,
                "message_id": message_id,
                "agent_id": agent_id,
                "agent_name": agent_name,
                "created_by": created_by,
                "filename": disk_name,           # on-disk
                "gridfs_id": gridfs_id,          # persistent fallback
                "original_name": b["name"],      # human-friendly
                "title": b.get("title") or b["name"],
                "mime": b["mime"],
                "kind": _kind_for_ext(ext),
                "ext": ext,
                "size": len(data),
                "preview": "" if raw_bytes is not None else content[:600],
                "tags": [],
                "created_at": now,
            }
            await db.documents.insert_one({**doc})

            attachments.append({
                "filename": disk_name,
                "url": f"/api/files/docs/{org_id}/{disk_name}",
                "mime": b["mime"],
                "kind": doc["kind"],
                "size": len(data),
                "original_name": b["name"],
                "is_artifact": True,
                "document_id": doc["id"],
                # Expose the GridFS id so the UI can fetch the proof of
                # integrity for this artifact (the proof is keyed on
                # gridfs_id, not document_id, since stamping happens at
                # the file-storage layer).
                "gridfs_id": gridfs_id,
            })
        except Exception as exc:  # pragma: no cover — never let one bad
            logger.exception("artifact persistence failed: %s", exc)
            continue
    return attachments


def _kind_for_ext(ext: str) -> str:
    """Map ext → a kind label the frontend already knows how to render."""
    ext = (ext or "").lower()
    if ext in {"md", "txt", "log", "ini", "toml", "env", "rtf"}: return "text"
    if ext in {"csv", "tsv"}: return "data"
    if ext in {"json", "yaml", "yml", "xml"}: return "data"
    if ext in {"html", "htm"}: return "app"
    if ext in {"sh", "py", "js", "ts", "sql"}: return "code"
    if ext in {"ics"}: return "calendar"
    if ext in {"vcf"}: return "contact"
    if ext in {"eml"}: return "email"
    if ext in {"png", "jpg", "jpeg", "gif", "webp", "bmp"}: return "image"
    if ext in {"pdf"}: return "pdf"
    if ext in {"docx"}: return "doc"
    return "file"


# ── HTTP router ────────────────────────────────────────────────────────────
DOC_FILENAME_RE = re.compile(r"^[a-f0-9]{16,}\.[a-z0-9]{1,8}$", re.IGNORECASE)


def make_documents_router(*, db, docs_root: Path,
                          require_org_member, get_current_user):
    """Build the documents router — list/get/delete + capability-URL serving.

    Returns a single `APIRouter` already prefixed with `/api`.
    """
    r = APIRouter(prefix="/api")

    def _serialize(d: dict) -> dict:
        ca = d.get("created_at")
        return {
            "id": d.get("id") or str(d.get("_id")),
            "org_id": d.get("org_id"),
            "conversation_id": d.get("conversation_id"),
            "message_id": d.get("message_id"),
            "agent_id": d.get("agent_id"),
            "agent_name": d.get("agent_name"),
            "created_by": d.get("created_by"),
            "filename": d.get("filename"),
            "original_name": d.get("original_name"),
            "title": d.get("title"),
            "mime": d.get("mime"),
            "kind": d.get("kind"),
            "ext": d.get("ext"),
            "size": d.get("size", 0),
            "preview": d.get("preview", ""),
            "tags": d.get("tags") or [],
            "url": f"/api/files/docs/{d.get('org_id')}/{d.get('filename')}",
            "created_at": ca.isoformat() if isinstance(ca, datetime) else ca,
        }

    @r.get("/orgs/{org_id}/documents")
    async def list_documents(org_id: str, q: str = "", kind: str = "",
                             limit: int = 100,
                             u: dict = Depends(get_current_user)):
        await require_org_member(org_id, u["id"])
        query: dict = {"org_id": org_id}
        if kind:
            query["kind"] = kind
        if q.strip():
            needle = re.escape(q.strip())
            query["$or"] = [
                {"original_name": {"$regex": needle, "$options": "i"}},
                {"title": {"$regex": needle, "$options": "i"}},
                {"preview": {"$regex": needle, "$options": "i"}},
                {"agent_name": {"$regex": needle, "$options": "i"}},
            ]
        cur = db.documents.find(query, {"_id": 0}).sort("created_at", -1).limit(min(limit, 500))
        out = []
        async for d in cur:
            out.append(_serialize(d))
        return out

    @r.delete("/orgs/{org_id}/documents/{doc_id}")
    async def delete_document(org_id: str, doc_id: str,
                              u: dict = Depends(get_current_user)):
        await require_org_member(org_id, u["id"])
        d = await db.documents.find_one({"id": doc_id, "org_id": org_id})
        if not d:
            raise HTTPException(404, "Document not found")
        try:
            (docs_root / org_id / d["filename"]).unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("disk unlink failed for doc %s: %s", doc_id, exc)
        await db.documents.delete_one({"id": doc_id, "org_id": org_id})
        return {"ok": True}

    @r.get("/files/docs/{org_id}/{filename}")
    async def serve_document(org_id: str, filename: str):
        """Capability-URL serving — the random filename is the capability,
        so no Bearer required (mirrors how `/api/files/conv/...` works for
        chat attachments). DB lookup confirms the file is real before
        handing it out, so this isn't a generic file dump.

        Falls back to GridFS when the on-disk file is missing — the most
        common cause is the production container being redeployed since
        the file was first written. GridFS lives inside MongoDB and
        survives every redeploy."""
        if not DOC_FILENAME_RE.match(filename):
            raise HTTPException(400, "Invalid filename")
        d = await db.documents.find_one({"org_id": org_id, "filename": filename})
        if not d:
            raise HTTPException(404, "Document not found")
        path = docs_root / org_id / filename
        original = d.get("original_name") or filename
        media = d.get("mime") or None
        if path.is_file():
            return FileResponse(path, filename=original, media_type=media)

        # Disk gone — try GridFS (write-mirror added Feb 2026).
        gridfs_id = d.get("gridfs_id")
        if gridfs_id:
            from file_storage import read_blob_from_gridfs  # noqa: E402
            from fastapi.responses import Response  # noqa: E402
            blob = await read_blob_from_gridfs(db, gridfs_id)
            if blob is not None:
                # Best-effort: rehydrate the disk copy so subsequent
                # requests are zero-overhead until the next redeploy.
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(blob)
                except Exception:
                    pass
                headers = {"Content-Disposition": f'attachment; filename="{original}"'}
                return Response(content=blob, media_type=media or "application/octet-stream", headers=headers)
        raise HTTPException(404, "File missing on disk")

    return r


__all__ = [
    "RESOURCEFULNESS_DIRECTIVE",
    "extract_artifact_blocks",
    "save_artifacts",
    "make_documents_router",
]
