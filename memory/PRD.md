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
- ✅ **Wider responsive chat layout** + scroll-to-top / scroll-to-bottom floating arrows
- ✅ **Rich link previews** for cited web sources — `/api/unfurl?url=…` extracts OG/Twitter meta + favicon, cached 24h in MongoDB. Frontend `LinkPreviewCard` renders Slack/Discord-style cards with title / description / thumbnail / favicon / site name.

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
