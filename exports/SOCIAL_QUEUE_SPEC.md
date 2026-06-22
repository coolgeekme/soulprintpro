# Social Queue — Feature Specification

> **Paste this into a new project chat and tell the agent: "Implement this feature."**
> It assumes a FastAPI + React + MongoDB stack but the design translates cleanly to other stacks.
> Skim the **Critical UX Rules** section before changing anything — those are the rules users care about most.

---

## 1. What it is

A **Social Queue** is a unified drafting + scheduling + publishing surface for short-form social posts across multiple platforms (Twitter/X, LinkedIn, Facebook, Instagram, TikTok, YouTube, Threads, Bluesky, Mastodon, Reddit, Pinterest).

Users can create drafts in three ways:

1. **Brief → AI image** — type "draft 3 posts about our cold-brew launch", AI writes copy + generates a cover image per platform.
2. **Your image → caption** — upload your own image, a vision LLM looks at it and writes per-platform captions; image stays untouched.
3. **From a chat conversation** — convert any AI-written paragraph in a normal chat into a queued draft.

Drafts land in a queue. Users can edit, schedule, or publish to connected channels.

---

## 2. User stories

```
As a small-business owner, I can:
  • Drop 5 photos from a product shoot into the queue,
    have AI write platform-tuned captions for each,
    and schedule them across Mon/Wed/Fri.

As a marketing manager, I can:
  • Type "Announce our Q3 webinar" → get 3 drafts per platform
    with cover images on-brand to my style guide,
    review/edit, schedule.

As a solo creator, I can:
  • Upload an infographic I designed in Figma → the system
    writes captions per platform WITHOUT touching my image,
    files it into my Asset Library for reuse later.
```

---

## 3. Critical UX rules (do not break)

1. **A user-uploaded image is NEVER altered without explicit user action.** Period.
   - No watermarking. No upscaling. No restyle. No re-encode.
   - The verbatim bytes are persisted.
   - Geometric resize (crop-to-platform-aspect) is **opt-in** via a checkbox and uses deterministic PIL crop (no AI).
   - The original is always preserved alongside any resized variant.
   - If the user clicks "Regenerate cover" on a user-uploaded image, show a confirm dialog: *"This is the image you uploaded. Regenerating will replace it with an AI-rendered cover. Continue?"*

2. **Provenance is visible.** A user-uploaded cover shows a green "Your upload" badge. An AI-generated cover shows a `via {model}` chip. Users should never have to guess where an image came from.

3. **Per-platform tone is real.** The same brief produces different captions for Twitter (≤ 280, punchy), LinkedIn (multi-paragraph, professional), Reddit (no hashtags, conversational), etc. Pull from style guides if the org has one.

4. **Caption generation grounds in reality.** The vision LLM may only claim what's literally visible in the image. No invented stats, names, or quotes.

5. **No silent partial successes.** If the LLM call fails, return a clean error — don't leave half-built drafts in the queue.

6. **Failures in side-effects don't block the main flow.** If "save to Asset Library" fails, the draft must still land in the queue. Log + swallow.

---

## 4. Tech stack

**Backend**
- Python 3.11+, FastAPI, Motor (async MongoDB)
- Pillow (PIL) — only for **deterministic geometric resize**, never AI
- `emergentintegrations` (or any OpenAI/Gemini SDK) for the vision LLM call
- GridFS or S3 for blob storage

**Frontend**
- React 18+
- Tailwind (or any utility CSS)
- `sonner` for toasts, `lucide-react` for icons
- `axios` for the API client

**LLMs**
- **GPT-4o** for vision captioning (handles base64 image input). Gemini 3 Pro / Claude Sonnet 4.5 also work — model is swappable.
- Any text LLM for the "brief → drafts" path (GPT-4o-mini is fine).
- **Optional**: Nano Banana / DALL-E / Imagen for AI-generated covers in path #1.

**Third-party publishing** (optional but recommended)
- Composio toolkit (covers most platforms with one API key) OR direct OAuth integrations per platform.

---

## 5. Database schema

### `social_posts` collection

```python
{
  "_id": ObjectId,
  "org_id": str,                       # multi-tenant scope
  "team_id": str,                      # optional sub-scope
  "conversation_id": str,              # optional — links back to chat
  "source_message_id": str,            # optional — for chat-to-draft

  "agent_id": str,                     # which agent drafted it
  "agent_name": str,

  "status": "draft" | "queued" | "published" | "failed",
  "platforms": [str],                  # ["twitter", "linkedin", ...]
  "text": str,                         # the caption
  "hashtags": [str],                   # without the leading '#'
  "link_url": str,                     # optional CTA link

  # Cover image — ALWAYS a dict, not a bare filename.
  "image": {
    "filename": str,                   # the storage filename
    "url": str,                        # "/api/files/{filename}" or signed S3
    "with_logo": bool,                 # if True, logo already composited
    "raw_filename": str,               # un-composited base, for re-logo
    "model_used": str,                 # "user_upload" | "nano-banana" | etc.
  } | None,

  # Carousel / multi-image
  "media": [
    {"filename": str, "url": str, "kind": "image"|"video"},
    ...
  ],

  "image_status": "ready" | "generating" | "failed" | "none",
  "image_prompt": str,                 # user brief or original prompt (≤600)
  "image_style_override": str,         # optional visual style instruction
  "image_style_override_source": "explicit" | "auto_prompt" | "",

  # NEW for user-uploaded image flow
  "image_source": "user_upload" | "ai_generated" | "",
  "image_user_uploaded": bool,
  "auto_fit_applied": bool,            # geometric resize was applied

  "logo_position": "bottom-right" | "bottom-left" | "top-right" | "top-left",
  "scheduled_for": datetime | None,
  "published_at": datetime | None,
  "publish_results": {                 # per-platform publish outcomes
    "twitter": {"ok": bool, "url": str, "error": str},
    ...
  },

  "created_at": datetime,
  "created_by": str,                   # user_id
}
```

### `assets` collection (Asset Library)

```python
{
  "_id": ObjectId,
  "org_id": str,
  "filename": str,                     # same filename used by social post
  "original_name": str,                # what the user uploaded
  "ext": str,
  "mime": str,
  "size": int,
  "kind": "image" | "video" | "document" | "audio",
  "description": str,
  "category": "lifestyle" | "product" | "logo" | "team" | "other",
  "usable_in_social": bool,
  "source": "manual_upload" | "social_upload" | "ai_generated",
  "uploaded_by": str,                  # user_id
  "uploaded_by_name": str,
  "uploaded_at": datetime,
}
```

### `style_guides` collection (optional but recommended)

Per-platform tone rules the LLM should follow. Example:
```python
{
  "org_id": str,
  "twitter": {"voice": "snarky, lowercase", "do": [...], "dont": [...]},
  "linkedin": {"voice": "professional but warm", ...},
  ...
}
```

---

## 6. Backend API contract

### `POST /api/orgs/{org_id}/social/draft`
Create AI-drafted posts from a text brief.

**Body** (JSON):
```json
{
  "prompt": "Announce our Q3 webinar",
  "count": 3,
  "platforms": ["twitter", "linkedin"],
  "style_override": ""                  // optional, beats brand default
}
```

**Returns**: `{ "posts": [serialized_post, ...] }`

Behaviour:
- Calls a text LLM with the brief + style guide + soul (org context).
- For each platform × count, creates a draft. Schedules cover-image generation in the background.
- Detects style cues in the brief ("infographic", "watercolour", "3D render") and auto-fills `image_style_override` when not explicitly set — surface this as `image_style_override_source: "auto_prompt"` so the FE can show an "auto-detected" hint.

### `POST /api/orgs/{org_id}/social/from-image` *(the headline feature)*
Caption a user-uploaded image. Strictly non-destructive.

**Body** (multipart/form-data):
```
image_files:                 File (repeat for multiple)
platforms:                   "twitter,linkedin,instagram"
mode:                        "single" | "carousel"
brief:                       optional caption-direction text
auto_fit_per_platform:       "0" | "1"
conversation_id:             optional
```

**Returns**: `{ "posts": [...] }`

**Behaviour**:
1. Validate every file (PNG/JPEG/WEBP only, ≤ 12 MB each).
2. Read all bytes into memory.
3. For each source image:
   - Persist bytes verbatim to disk + GridFS.
   - Mirror into the org's Asset Library (`db.assets`).
   - Run a single GPT-4o vision call: image + brief + platform conventions + style guide → JSON map `{platform: {text, hashtags[]}}`.
4. For each platform:
   - If `auto_fit_per_platform=1`, do a deterministic PIL crop to the platform's aspect target (square for IG, 9:16 for TikTok, 16:9 for Twitter/YouTube, 1200×627 for LinkedIn, 1000×1500 for Pinterest). Save as additional file; ORIGINAL stays.
   - Insert draft document with `image_source: "user_upload"`, `image_user_uploaded: true`, image dict pointing to the platform-fit (or original) filename.

**Mode rules**:
- `single` — M images × N platforms = **M × N drafts**, one image per draft.
- `carousel` — N platforms = **N drafts**, each with all M images attached (first as cover, rest in `media[]`).
- If `carousel` requested but only 1 image uploaded, silently downgrade to `single`.

### `GET /api/orgs/{org_id}/social/posts?status=draft|queued|published`
List drafts.

### `PATCH /api/orgs/{org_id}/social/posts/{id}`
Edit caption / hashtags / platforms / schedule.

### `POST /api/orgs/{org_id}/social/posts/{id}/generate-image`
Regenerate the cover (AI). On a `image_source=user_upload` post, frontend MUST confirm before calling.

### `POST /api/orgs/{org_id}/social/posts/{id}/publish`
Publish now or schedule. Returns `publish_results` per platform.

### `DELETE /api/orgs/{org_id}/social/posts/{id}`
Delete a draft.

---

## 7. Vision LLM prompt (per-platform captioning)

```text
You are a senior social copywriter. The user has uploaded an image and
(optionally) given a brief. Look at the image AND the brief, then write
one caption PER target platform tuned to that platform's conventions.

COMPANY SOUL:
- Mission: {mission}
- Tone: {tone}
- Audience: {audience}
- Values: {values}

BRAND STYLE GUIDE (apply when set):
{style_guide_block}

PLATFORM CONVENTIONS:
  • Twitter/X: ≤ 280 chars, punchy hook first sentence, 0-2 hashtags inline.
  • Bluesky: ≤ 300 chars, conversational, 0-2 hashtags.
  • Threads: 1-2 short paragraphs, friendly first-person, 1-3 tags.
  • LinkedIn: 2-4 short paragraphs, professional but human, lead with the
    hook, 2-5 contextual hashtags at the bottom.
  • Facebook: 1-3 sentences, warm + community-oriented, minimal hashtags.
  • Instagram: punchy caption (1-3 lines) + 5-12 relevant hashtags at end.
  • TikTok: 1-2 lively sentences + 3-6 hashtags.
  • YouTube (community/short): 1-2 sentences, intrigue hook, 2-4 hashtags.
  • Reddit: longer-form, conversational, no marketing-speak, NO hashtags.
  • Mastodon: 1-2 short paragraphs, plain prose, 0-2 tags.
  • Pinterest: keyword-rich, descriptive, 2-4 hashtags.

Rules:
  • Ground every claim in what's literally visible in the image.
    Do NOT invent product names, statistics, dates, or quotes.
  • If the brief is empty, write a caption that simply describes
    what's in the image in a way that fits the brand voice.
  • If the brief contradicts the image, prioritise the brief but
    stay accurate about visible content.
  • Never reference the fact that the user uploaded an image or
    asked you to caption it. The caption IS the post.

OUTPUT FORMAT — a single JSON object, no markdown, no commentary.
Keys are EXACTLY the platform IDs requested. Each value is an object
with two keys: "text" (the caption string) and "hashtags" (an array of
plain string hashtags WITHOUT the leading '#'). Hashtags must be empty
for reddit and may be empty for any platform where they don't fit.
```

**User message**:
```text
Brief from the poster (may be empty): {brief or "(no brief — describe what you see)"}
Now produce the JSON object of platform → caption.
```

**Attach the image as base64** to the user message (`ImageContent(image_base64=...)`).

---

## 8. Per-platform geometric fit targets (opt-in resize)

```python
PLATFORM_FIT_TARGETS = {
  "instagram": (1080, 1080),     # square
  "tiktok":    (1080, 1920),     # 9:16 vertical
  "youtube":   (1280, 720),      # 16:9
  "twitter":   (1600, 900),      # 16:9
  "bluesky":   (1600, 900),
  "mastodon":  (1600, 900),
  "linkedin":  (1200, 627),
  "facebook":  (1200, 630),
  "reddit":    (1200, 630),
  "threads":   (1080, 1080),
  "pinterest": (1000, 1500),     # 2:3
}
```

Implementation: PIL `ImageOps.fit(img, target, Image.LANCZOS, centering=(0.5, 0.5))` — scale-to-cover + center-crop. **No filters, no enhancement, no re-quantize beyond what's needed to save.**

---

## 9. Frontend components

### `SocialPage.js` (main surface)
Three tabs: **Drafts / Queued / Published**. Top has a compose form.

### Compose form
- **Mode toggle** (pill segmented control):
  - "Brief → AI image" (default)
  - "Your image → caption"
- **Brief textarea** (always visible; placeholder changes per mode)
- **Platform multi-select chips**
- When mode = "image": render `<ComposeImagePicker />`

### `ComposeImagePicker` component
- Drag-drop zone + file input (accept: `image/png,image/jpeg,image/webp`, multiple, ≤ 10 files, ≤ 12 MB each)
- Per-image thumbnail grid with remove buttons
- **Carousel checkbox** — shown only when 2+ images uploaded
- **Auto-fit checkbox** — opt-in, always visible. Microcopy: *"Deterministic crop only (no AI). Your original image is always preserved alongside."*

### Draft card (`PostCard`)
- Cover image (resolves `post.image.url`)
- "Your upload" badge (emerald) when `image_source === "user_upload"`, otherwise `via {model_used}` chip
- Caption (editable inline)
- Platform chips, scheduled-for time, status pill
- Actions: Edit caption · Edit image · **Regenerate** (confirm guard on user-uploaded) · Schedule · Publish now · Delete

### `PostCoverImage` sub-component
Critical: expects `post.image` to be an OBJECT, not a string.

```jsx
{post.image?.url && (
  <img src={post.image.url} alt="" data-testid={`post-image-${post.id}`} />
)}
```

---

## 10. Implementation order (recommended)

1. **DB models + serializers** — `social_posts`, `assets`. Get the dict-shaped `image` field right from day one.
2. **GET /social/posts** + list UI — verify reads work.
3. **POST /social/draft** (brief → AI text drafts) — text-only, skip image gen at first. Confirm the queue UI works.
4. **Add AI cover generation** to drafts (Nano Banana / DALL-E). Background task pattern.
5. **POST /social/from-image** with **single mode only**, no auto-fit, no carousel. The cover image must render on the draft card after this step. ← *Verify with a real upload before moving on.*
6. **Asset Library mirror** in the from-image endpoint.
7. **Carousel mode**.
8. **Auto-fit checkbox + per-platform geometric resize**.
9. **"Your upload" badge + regenerate confirm guard**.
10. **Schedule + publish** (Composio or direct OAuth per platform).
11. **Style guide / Soul integration** in the vision prompt.

Test points #5 and #9 are where features have historically broken — verify them with curl + a real image before considering the feature done.

---

## 11. Gotchas / lessons learned

1. **The `image` field must be a dict, not a string.**
   Storing `image: "abc123.jpg"` and rendering `<img src={post.image.url}>` will fail silently — the cover disappears from the card with no console error. Always serialize to `{filename, url, with_logo, raw_filename, model_used}`.

2. **`media[]` entries are also dicts** with `{filename, url, kind}`. Same trap as #1.

3. **The vision call output is JSON.** Always wrap in `re.search(r"\{.*\}", text, DOTALL)` before `json.loads` — models occasionally wrap their output in markdown fences.

4. **Use `model_used: "user_upload"`** as the provenance signal. Anything else (e.g. omitting it) will make the frontend think it's AI-generated and offer Regenerate without a guard.

5. **The Asset Library mirror is best-effort.** Wrap in try/except — never let a Mongo or disk hiccup block the user's draft from landing in the queue.

6. **Don't transcode the user's image.** Even a JPEG re-save changes the bytes. Persist what they uploaded byte-for-byte.

7. **Charge per draft, not per call.** A vision call generates N captions but creates M × N drafts. Charge the right number against quota.

8. **GPT-4o handles base64 images directly** via `ImageContent(image_base64=...)`. Don't bother with file-upload step.

9. **Add testid attributes** for every interactive element from day one — saves hours during testing.

10. **Strip leading `#` from hashtags** on the way IN — store plain strings; render with `#` at display time.

---

## 12. Optional enhancements (after MVP)

- **"Pick from library" tab** in the compose dialog — caption an image the user already uploaded without re-uploading.
- **Bulk-schedule** — multi-select N drafts → spread across M-W-F.
- **Recurring drafting mission** — weekly cron that generates 3 fresh drafts every Monday from the org's content calendar.
- **A/B caption variants** — the vision call returns 2 captions per platform; the user picks.
- **Inline image-edit nudge** — vision LLM points out one tweak the user could make (e.g. "logo is in the IG-square crop zone") WITHOUT altering the image itself.
- **Style guide editor UI** so users can curate per-platform voice rules instead of relying on defaults.

---

## 13. Quick test recipes

### Backend (curl)
```bash
# Create a fake image
python -c "from PIL import Image; Image.new('RGB',(1024,1024),(45,32,25)).save('/tmp/x.jpg')"

# Hit the from-image endpoint
curl -X POST "$API_URL/api/orgs/$ORG/social/from-image" \
  -H "Authorization: Bearer $TOKEN" \
  -F "platforms=twitter,linkedin" \
  -F "mode=single" \
  -F "brief=Lead with the handcrafted angle" \
  -F "image_files=@/tmp/x.jpg" | jq

# Expect:
#   posts[i].image.url     starts with /api/files/
#   posts[i].image.model_used === "user_upload"
#   posts[i].image_source === "user_upload"
#   Each platform's caption is different and platform-appropriate.

# Verify Asset Library mirror:
curl -X GET "$API_URL/api/orgs/$ORG/assets" -H "Authorization: Bearer $TOKEN" | jq '.[] | {filename, original_name, source}'
# Expect a row with source: "social_upload" and the same filename.
```

### Pytest
```python
def test_serializer_image_dict_passes_through_for_user_upload():
    p = {
        "_id": "abc", "org_id": "o",
        "image": {
            "filename": "abc123.jpg",
            "url": "/api/files/abc123.jpg",
            "with_logo": False,
            "raw_filename": "abc123.jpg",
            "model_used": "user_upload",
        },
        "image_source": "user_upload",
        "image_user_uploaded": True,
    }
    out = serialize_social_post(p)
    assert isinstance(out["image"], dict)
    assert out["image"]["url"] == "/api/files/abc123.jpg"
    assert out["image"]["model_used"] == "user_upload"

def test_platform_fit_targets_are_sane():
    for plat, (w, h) in PLATFORM_FIT_TARGETS.items():
        assert w > 0 and h > 0
    assert PLATFORM_FIT_TARGETS["tiktok"][1] > PLATFORM_FIT_TARGETS["tiktok"][0]
    assert PLATFORM_FIT_TARGETS["instagram"][0] == PLATFORM_FIT_TARGETS["instagram"][1]
```

---

## 14. Definition of done

- [ ] A user can paste a text brief → get drafts across N platforms with platform-tuned copy.
- [ ] A user can upload 1+ images → get drafts across N platforms with per-platform captions tuned to each.
- [ ] The uploaded image renders on the draft card.
- [ ] The uploaded image lives in the Asset Library.
- [ ] No platform's caption is identical to another's — each is tuned to its conventions.
- [ ] Carousel mode produces one post per platform with all images attached.
- [ ] Auto-fit produces deterministically-sized variants (1080×1080 for IG, 1600×900 for Twitter/X, etc.) while preserving the original.
- [ ] "Your upload" badge visible on user-uploaded covers; "via {model}" on AI-generated.
- [ ] Regenerate on a user-uploaded cover shows a confirm prompt.
- [ ] Failure to mirror to the Asset Library doesn't block the draft from landing in the queue.
- [ ] Vision LLM never invents content not visible in the image (verify with a sanity-check test image).
- [ ] All interactive elements have `data-testid` attributes.

---

*Spec drawn from a production implementation. Adjust models / framework as needed; the rules in §3 and the schema in §5 are the parts that matter most.*
