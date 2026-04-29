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
  agent on a direct conversation with a bridging handoff message
- ✅ **Smart-expand groups** — `smart_expand: true` lets the system auto-add a
  new specialist when the topic shifts beyond the current agents' lanes
- ✅ Invite system: code + shareable link (`/invite/:code`) + mailto helper
- ✅ Access control at org + team + conversation membership level
- ✅ Dark "liquid glass" UI; "Just start typing" composer is the primary CTA on
  the team page; the manual agent picker stays available behind a small link
- ✅ Searchable + division-filterable crew browser & dialogs
- ✅ Test suite: **47/47 pytest passing** across `test_soulprint.py`,
  `test_agents_catalogue.py`, and `test_smart_routing.py`
- ✅ Auth bug fixes: tz-aware invite expiry; `Secure` cookie; Bearer fallback

## Roadmap
- **P1** Invite revocation + pending invite list in UI
- **P1** Email invite via Resend integration (currently falls back to `mailto:`)
- **P1** Real-time updates for group chats (WebSocket or polling) so other members
  see new messages without reload
- **P2** Custom team agents (user-defined name + system prompt) on top of the 5 built-ins
- **P2** Agent-to-agent collaboration in group rooms (a mentioned agent can @ping another)
- **P2** Per-conversation memory/notes distinct from team SoulPrint
- **P2** File attachments + image uploads
- **P2** Google/GitHub OAuth in addition to email/password
