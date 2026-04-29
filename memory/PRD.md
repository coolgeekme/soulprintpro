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

## Implemented (Apr 29, 2026)
- ✅ Full schema rewrite — orgs, teams, members, invites, conversations, messages
- ✅ JWT cookie auth + Bearer fallback (localStorage)
- ✅ **169 specialised agents** across **14 divisions**, seeded from
  [msitarzewski/agency-agents](https://github.com/msitarzewski/agency-agents)
- ✅ SoulPrint onboarding injected into every agent system prompt
- ✅ Direct chats (always respond) and group chats with `@mention` support for
  **id, short-slug, or slugified display name**, plus `auto_respond` toggle
- ✅ **Smart auto-routing** — `POST /api/teams/{id}/start` routes a free-text prompt
  to the best of 169 agents and posts the first message in one shot
- ✅ **Switch agent** — `POST /api/conversations/{id}/switch-agent` swaps the
  agent on a direct conversation with a bridging handoff
- ✅ **Smart-expand groups** — auto-add a new specialist when the topic shifts
- ✅ **Image generation** — `POST /api/conversations/{id}/generate-image` via
  Gemini Nano Banana (Emergent LLM key). Files served from
  `/api/files/{filename}` with auth + conversation-membership checks
- ✅ **Privacy** — conversations are scoped to `member_ids`. UI shows
  "Private to you" or "Shared with N people" badges in the meta strip and
  conversation cards
- ✅ Invite system: code + shareable link (`/invite/:code`) + mailto helper
- ✅ Access control at org + team + conversation + file level
- ✅ Dark "liquid glass" UI; "Just start typing" composer + image-mode toggle
- ✅ Searchable + division-filterable crew browser & dialogs
- ✅ Test suite: **60/60 pytest passing** across `test_soulprint.py`,
  `test_agents_catalogue.py`, `test_smart_routing.py`,
  `test_privacy_and_images.py`

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
