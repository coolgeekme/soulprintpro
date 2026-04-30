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
- ✅ Full schema rewrite — orgs, teams, members, invites, conversations, messages, **assets**
- ✅ JWT cookie auth + Bearer fallback (localStorage). CORS uses `allow_origin_regex=".*"` for any deploy domain.
- ✅ **169 specialised agents** across **14 divisions** (sourced internally; no external attribution surfaced in the UI)
- ✅ SoulPrint onboarding injected into every agent system prompt
- ✅ **Org asset library** — upload images, PDFs, DOCX, text, audio, video (≤10 MB each).
  Text files (PDF/DOCX/TXT) are extracted on upload and a compact list of asset
  names + descriptions + excerpts is injected into every agent's system prompt
  in that org so agents can cite the brand guide / handbook / etc.
- ✅ Direct chats + group chats with `@mention` (id, short slug, or display name)
- ✅ **Smart auto-routing** — `POST /api/teams/{id}/start` routes a free-text prompt to the best of 169 agents
- ✅ **Auto image generation** — when the user clearly asks for an image
  (Instagram post, logo, banner, etc.), the chosen agent generates it
  automatically without any toggle. Aspect-ratio hints for all major social
  formats (1:1, 9:16, 1.91:1, 16:9) baked into the system message.
- ✅ Manual 🎨 image-mode toggle still available for explicit prompts
- ✅ **Switch agent** with bridging handoff
- ✅ **Smart-expand groups** — auto-add a new specialist when the topic shifts
- ✅ **Real-time web search** via Brave (intent-classified per message; sources rendered as clickable links under the reply)
- ✅ **Privacy** — conversations scoped to `member_ids`; UI shows "Private to you" / "Shared with N" badges
- ✅ Invite system: code + shareable link + mailto helper
- ✅ Dark "liquid glass" UI **+ light theme** with persisted toggle in TopBar
- ✅ 16px chat font (matches modern AI platforms)
- ✅ Removed "Made with Emergent" badge per user request

## Roadmap
- **P0 (next)** Tool-calling framework — agents invoke tools via OpenAI function
  calling. Initial tools: `web_search`, `generate_pdf`, `generate_docx`,
  `generate_video` (Sora 2). Per-conversation permission toggles.
- **P1** Telegram integration — outbound first (push generated assets to
  Telegram chats), then two-way relay. Needs bot token from user.
- **P1** Audio (TTS) for agent replies (OpenAI TTS via Emergent key)
- **P1** Pending-invite management + revoke + Resend email delivery
- **P1** Real-time updates for group chats (WebSocket or polling)
- **P2** Custom user-authored team agents on top of the 169 built-ins
- **P2** Agent-to-agent `@ping` so a specialist can pull in a peer
- **P2** Google/GitHub OAuth
- **P2** Scheduled / background autonomous runs (e.g. weekly research digest)
