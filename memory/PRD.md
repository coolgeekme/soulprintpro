# SoulPrint Engine — Product Requirements

## Original Problem Statement
Pivot from a single-user Emergent Wingman clone to a **B2B multi-tenant platform**
inspired by [SoulPrintEngine.ai](https://soulprintengine.ai/) + the `agency-agents`
pattern. Users must be able to:

- Create **organizations** (a user may belong to many)
- Inside an org, create **teams** — each team has its own private **SoulPrint**
  (mission, tone, audience, values, north star) that is automatically injected
  into every agent's system prompt
- Access a crew of **specialized agency-style AI agents** (Strategist, Researcher,
  Copywriter, Engineer, Ops Manager)
- Have **individual (direct) or group conversations** with those agents. In group
  chats agents respond only on `@mention` by default, with an optional
  auto-respond toggle

## User Preferences (confirmed)
- **1B** Org-level workspaces — users belong to many orgs
- **2C** Group chat: agent responds on `@mention` (default) or every message (toggle)
- **3B** Replace old agents with agency roles (Nova/Atlas/Ember/Vector/Sable)
- **4A** Team SoulPrint onboarding injected into agent prompts
- **5C** Both invite types — shareable link AND email (via `mailto:`)

## Architecture
- **Backend** FastAPI + Motor/MongoDB, JWT cookies
  - Collections: `users`, `organizations`, `org_members`, `teams`, `invites`,
    `conversations`, `messages`
- **Frontend** React 19 + Tailwind + shadcn/ui, dark "liquid glass" theme with
  Instrument Serif display + aurora halo
- **LLM** `emergentintegrations` → `openai/gpt-4.1-mini` via the Emergent LLM key.
  Prompts combine the agent persona + the team's SoulPrint.

## Routes (frontend)
- `/` Landing
- `/auth` Login / Register
- `/app` Org picker + create + invite accept
- `/app/org/:orgId` Teams, members, invite generation
- `/app/team/:teamId` SoulPrint editor, conversation list, new-conversation dialog
- `/app/team/:teamId/c/:convId` Chat (direct or group with @mention picker)
- `/invite/:code` Auto-accept an invite code from a shareable link

## Key API Endpoints
- `POST /api/auth/register|login|logout` · `GET /api/auth/me`
- `GET|POST /api/orgs` · `GET /api/orgs/{id}` · `GET /api/orgs/{id}/members`
- `GET|POST /api/orgs/{id}/invites` · `POST /api/invites/accept`
- `GET|POST /api/orgs/{id}/teams` · `GET /api/teams/{id}` · `PUT /api/teams/{id}/soulprint`
- `GET /api/teams/{id}/agents`
- `GET|POST /api/teams/{id}/conversations` · `GET|DELETE /api/conversations/{id}`
- `GET|POST /api/conversations/{id}/messages`

## Implemented (Apr 29-30, 2026)
- ✅ Full schema rewrite — orgs, teams, members, invites, conversations, messages, **assets**, **schedules**
- ✅ JWT cookie auth + Bearer fallback (localStorage). CORS uses `allow_origin_regex=".*"` for any deploy domain.
- ✅ **169 specialised agents** across **14 divisions**
- ✅ SoulPrint onboarding injected into every agent system prompt
- ✅ **Org asset library** — upload images, PDFs, DOCX, text, audio, video (≤10 MB each)
- ✅ Direct + group chats with `@mention`
- ✅ Smart auto-routing across 169 agents
- ✅ **Auto image generation** (Nano Banana) — agents render images when asked
- ✅ **Auto video generation** — Kie.ai Veo 3.1 (async submit + poll), inline `<video>` rendering
- ✅ Switch agent + smart-expand groups
- ✅ Real-time web search via Brave (intent-classified, source citations)
- ✅ Privacy badges on each conversation
- ✅ Invite system: code + shareable link + mailto helper
- ✅ **Light + Dark theme** with persisted toggle
- ✅ **Company "Soul" capture on org creation** — URL scrape + LLM synthesis (mission / audience / tone / values / north star / summary), injected into every agent prompt for the org
- ✅ **Auto-created "General" team + conversation** on org creation — users can chat instantly
- ✅ **Edit + Delete orgs and teams** (PATCH/DELETE, owner/admin only, full cascade, General team protected)
- ✅ **Schedules / cron-style task automation** — APScheduler in-process; conversation-, team-, and org-scoped recurring + one-shot prompts; rehydrated on startup; `run-now`, pause/resume, delete
- ✅ **Timezone + GPS-derived location** injected via `X-User-Timezone` / `X-User-Location` headers — agents quote "today" / "your local time" correctly and tailor regional advice
- ✅ **soulprintengine.ai brand alignment** — orange (`#F64000`) accent + `#0a0a0a` black background; new orange swirl logo replaces Sparkles site-wide
- ✅ **PWA support** — `manifest.json`, `sw.js` install service worker, Apple/Safari tags, theme-color, offline shell
- ✅ **Public chat-image URLs** — `/api/files/{filename}` is now public-with-unguessable-URL so `<img>` tags render inline in chat
- ✅ **Public org-asset URLs** — same pattern applied to `/api/files/assets/{filename}` so uploaded asset thumbnails also render inline
- ✅ **Pure "just type" composer** — manual image/video toggles removed. Backend now uses a deterministic keyword fast-path (`Instagram post + create/make/...` etc.) before the LLM classifier, so common asks always trigger the right media generation. Verified against 11 fixture prompts.
- ✅ **Pinned scroll-to-top / scroll-to-latest arrows** — moved out of the scrolling container into the composer wrapper so they always pin above the input bar (no longer scroll with messages); threshold lowered from 200px to 60px so they appear on shorter conversations.
- ✅ **Manual add/remove agents in group chats** — `POST /api/conversations/{id}/agents` and `DELETE /api/conversations/{id}/agents/{agent_id}` (group-only, member-gated, last-agent guarded). Frontend "Manage agents" dialog: removable chips for current agents + searchable + division-filtered picker for everyone else. System messages render as centered chips, not bubbles.
- ✅ **In-chat file uploads for agent review** — `POST /api/conversations/{id}/upload` (multipart, ≤25 MB). Images go to GPT-4.1-mini vision; PDFs/DOCX/TXT/MD/CSV/JSON have their text extracted and injected into the prompt; audio/video and other binaries get acknowledged. Smart-router picks the best specialist for the file in group chats. Files served via `/api/files/conv/{conv_id}/{filename}` (public-with-unguessable-URL). UI: paperclip button in composer, inline rendering of images/video/audio, filetype card for PDFs/docs.
- ✅ **OpenAI direct fallback for budget exhaustion** — `llm_fallback.py` monkey-patches `LlmChat.send_message` so that whenever the Emergent universal-key returns "Budget has been exceeded" (or insufficient quota), the call is transparently retried against the user's own `OPENAI_API_KEY` in `/app/backend/.env`. All 8 existing call sites benefit automatically. Verified live: simulated budget failure → fallback hit OpenAI directly → got the expected reply.
- ✅ **Image editing via Nano Banana (image-to-image)** — when a user uploads an image and the prompt contains an edit verb ("add", "remove", "change", "swap", "make it…", etc.), the upload endpoint feeds the source image to `gemini-3.1-flash-image-preview` and returns a freshly EDITED image (intent: `image_edit_reply`). Pure description prompts still use the vision-describe path. Verified end-to-end: starter logo with "A" → "Add the letter B to the lower right corner" → returned a 700 KB edited JPEG with the letter B. Also fixed a pre-existing `ImageContent(mime_type=...)` bug that was disabling the vision-describe path.
- ✅ **Wider responsive chat layout** + scroll-to-top / scroll-to-bottom floating arrows
- ✅ **Rich link previews** for cited web sources — `/api/unfurl?url=…` extracts OG/Twitter meta + favicon, cached 24h in MongoDB. Frontend `LinkPreviewCard` renders Slack/Discord-style cards with title / description / thumbnail / favicon / site name.
- ✅ **Re-synthesise SoulPrint (Edit Org → checkbox)** — fixed silent failure where regen produced an empty soul whenever URL fetch / LLM failed. `_generate_company_soul` now raises typed `SoulGenError` (no_input / fetch_failed / llm_failed / bad_json / empty_output); `PATCH /api/orgs/{id}` returns 422 / 502 with a human message and **preserves the previous Soul** instead of wiping it. The new Soul is also cascaded into the auto-created **General team's soulprint snapshot** so its agents pick up the new mission/tone/audience/values/north-star immediately. Verified end-to-end via curl: valid URL ✓, bogus URL ✓ (422 + soul kept), description-only ✓, both blank ✓ (422), team cascade ✓ (Apr 30, 2026).
- ✅ **Robust URL scraping for `.app` and other modern domains** — `_fetch_url_text` rewritten: real Chrome User-Agent (sites no longer 403 us as a bot), tries both apex and `www.` variant, follows `<meta http-equiv="refresh">` AND inline `window.location.href = "/x"` JS redirects (typical of `.app` lander pages like `arc.app → /lander`), and falls back to stitching `<title> + <meta description> + og:*` tags when the body is a JS-only SPA shell. Verified: `linear.app`, `framer.app`, `raycast.app`, `cron.app`, `sora.app`, `beeper.com` all return rich text now; Cloudflare-protected sites still 422 with a helpful message asking for a description (Apr 30, 2026).
- ✅ **Perplexity Sonar fallback for blocked sites and dead Brave queries** (`perplexity_client.py`): two helpers — `perplexity_company_summary(name, url)` for Soul-generation when our scraper can't read a site (Cloudflare 403, JS-only SPA), and `perplexity_web_search(query)` returning the same `WebResult` shape as Brave so it drops into `maybe_fetch_web_context` as a seamless fallback when Brave returns 0 results / rate-limits. Soul flow now: scrape → if <200 chars and we have a URL, retry via Perplexity → feed into existing LLM Soul-extractor. Verified live: `arc.app` and `mirror.xyz` (both Cloudflare-blocked) now produce accurate Souls instead of 422; `linear.app` skips the fallback entirely (logged). Key in `backend/.env` as `PERPLEXITY_API_KEY` (Apr 30, 2026).
- ✅ **Fixed first-message "Network Error" race** — root cause: the preview ingress strips `Access-Control-Allow-Credentials: true` and rewrites `Access-Control-Allow-Origin` to `*` on **OPTIONS preflights** (verified via curl: backend emits the right headers, the ingress overwrites them). Combined with axios' `withCredentials: true` this caused the browser to reject the preflight intermittently. Fix: drop `withCredentials: true` from the axios client in `frontend/src/lib/api.js`. Auth is unaffected — the request interceptor always sends `Authorization: Bearer ...` from localStorage. Verified end-to-end via Playwright: register → create org → first message → "Paris" reply in 8s, zero error toasts (Apr 30, 2026).
- ✅ **Fixed wrong-site identification for niche `.app` PWAs** — `playtrackr.app` (a basketball stats tracker) was being described as "a YouTube playlist learning app" because (1) our scraper found 0 chars in the SPA shell and (2) Perplexity confidently substituted `playtrackr.com` (an unrelated YouTube tutorial tracker with the same name). Two-pronged fix: (a) `_fetch_url_text` now also fetches the PWA `manifest.json` referenced by `<link rel="manifest">` and stitches its `name + short_name + description` — this catches the canonical site identity for almost every `.app` PWA. (b) The Perplexity prompt was tightened to ground STRICTLY on the exact URL ("the URL is ground truth, names may overlap with unrelated sites"), and the response is rejected unless it (i) isn't the literal "UNKNOWN" sentinel and (ii) actually mentions the URL host or the company name. Verified: `playtrackr.app` now correctly identifies as a basketball stats tracker; Perplexity returns UNKNOWN for `arc.app` instead of guessing wrong (Apr 30, 2026).

## Roadmap
- **P1** Telegram integration (outbound + inbound)
- **P1** Audio TTS replies (OpenAI TTS via Emergent key)
- **P1** Pending-invite management + revoke + Resend email delivery
- **P1** Real-time updates for group chats (WebSocket or polling)
- **P2** Custom user-authored team agents on top of the 169 built-ins
- **P2** Agent-to-agent `@ping`
- **P2** Real Google / GitHub OAuth
- **P2** `serialize_team` should emit `is_general` so the frontend stops relying on a name regex
- **P2** Extract the schedules block (~370 LOC) and the orgs/teams routes from `server.py` into dedicated routers (the file is now ~2400 lines)
- **P2** Live `next_run_at` in `_serialize_schedule` (read from APScheduler instead of stored doc)
