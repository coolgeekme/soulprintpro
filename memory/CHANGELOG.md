# 🔧 Foundry — What We've Built So Far

Plain-English feature inventory of **Foundry by SoulPrint Engine** for non-technical teammates. Each section is a category of capabilities and what they do for users.

---
## 🔌 May 14, 2026 — Composio Integration (Phase 1: Connections)
- **New feature**: each org member can now connect their own SaaS accounts — Gmail, Google Calendar, Slack, Notion, HubSpot — via Composio's managed OAuth. No OAuth-app registration on our side; Composio handles all third-party credentials.
- **Per-user, per-org scope**: connections are keyed by `{org_id}:{user_id}`, so the same Foundry user in two different orgs has two completely separate connection sets (mirrors how Memory isolates per workspace).
- New backend module `/app/backend/composio_integration.py` wraps the SDK behind a thin interface so we can swap providers (Pipedream, Nango, MCP) later without touching routes or agent code.
- Routes: `GET /api/orgs/{org_id}/composio/toolkits`, `POST /api/orgs/{org_id}/composio/connect`, `POST /api/orgs/{org_id}/composio/connections/{id}/refresh`, `DELETE /api/orgs/{org_id}/composio/connections/{id}` — all gated by `require_org_member`.
- Frontend `ConnectionsPanel.js` mounted inside `OrgPage` "Org setup & insights" section — 5 toolkit cards with one-click Connect, post-OAuth polling auto-detects when a connection flips ACTIVE, Disconnect with confirm.
- Auth configs bootstrapped idempotently at app startup; stored in `composio_auth_configs` collection so reboots don't churn.
- Telemetry plumbed: every future `execute_tool` writes to `llm_calls` with `kind_label="composio:<toolkit>:<slug>"` so SpendDashboard rolls it up alongside LLM cost. **`execute_tool` is built but intentionally NOT HTTP-exposed yet** — Phase 2 will inject these tools into the agent loop.
- 12/12 backend pytest pass at `/app/backend/tests/test_composio.py` covering auth gating, per-org isolation, idempotent bootstrap, cross-user disconnect protection, bad-toolkit 400.

---

## 💬 May 14, 2026 — Live Polling for Team Chats (Slack-feel)
- **User-reported bug**: in multi-member team chats, replies from other members or agents only appeared after a manual page refresh.
- **Root cause**: `ConversationPage.js` only polled `/messages` while a media-generation placeholder was in flight. Regular text replies were never refetched — the UI was effectively static until reload.
- **Fix**: new `GET /api/conversations/{id}/messages/since?cursor=<ISO>` cursor endpoint returns only messages newer than the cursor (matches both `timestamp` and `ready_at` so in-place media patches still surface). Frontend runs a universal 3s polling loop that reads `max(timestamp, ready_at)` from current state as the cursor, dedupes results by `id`, re-sorts by timestamp, and pauses while the tab is hidden (battery + LLM cost saver).
- Auth-gated via the same `load_conv_or_403` guard as the full fetch — non-members get 403, unauthenticated callers get 401.
- 12-case pytest regression at `/app/backend/tests/test_messages_since.py` covers cursor edge cases, auth gating, feedback parity, multi-user live update, and the `ready_at` $or branch.

---

## 👥 May 14, 2026 — Team Conversations Are Now Truly Team-Visible
- **User-reported bug**: a member started a conversation in a team but the owner/admin couldn't see it. Team chat was acting like 1:1 DM by accident.
- **Fix**: visibility logic in `list_conversations` + `load_conv_or_403` now widens from "you must be a direct member of this conv" → "you must be a member of the team that owns this conv." Reuses the existing `can_user_view_team` gate so visibility is consistent with how teams already work (open teams = everyone in org, closed teams = explicit members, owner-override always).
- **Private Chief-of-Staff threads** (`kind=private_cos`) remain 1:1 with their owner. Org owners retain audit access to private CoS for moderation but those threads no longer appear in the team list view.
- Verified live: owner can see member-created conv (`GET conv=200, GET messages=200`, listed in team feed); reverse direction also works (member sees owner's conv); cross-user private CoS still blocked (`403`).

---
## 💰 May 14, 2026 — Phase A Billing Visibility: Workspace Spend Dashboard
- New `GET /api/orgs/{org_id}/spend?period=current_month|previous_month` endpoint aggregates `llm_calls` cost/calls/tokens, missions, audits, and members for a given org+period. Per-member rollup hydrated with name + email so owners see real people, not opaque IDs.
- New **Workspace spend** card on `/app/org/:orgId` (owners/admins only — members get a quiet 403) under the "Org setup & insights" panel. KPI strip + collapsible per-member table with % bars + per-kind chips ("$0.47 agent_reply · 121", "$0.04 mission_lead · 18") + "This month / Last month" toggle.
- Footer states the future-state plainly: *"Once Stripe billing ships (target: June 1, 2026), this total becomes your invoice. Members above your plan's seat allowance will appear as separate per-seat line items."*
- Seat warning copy + UI now offer **two clear paths** (per user request): **"+ Add a seat ($30/mo)"** secondary CTA alongside the primary **"Upgrade to Temper"**. Backend warning string updated to match.
- Sets the data foundation Phase B (Stripe checkout + metered seat billing) will plug into.

---
## 🩹 May 14, 2026 — Bug Fix: Chief of Staff silent dropout
- **User-reported bug** (ben@archeforge.com): 3 consecutive messages to the private Chief of Staff produced zero replies — the thread looked dead.
- **Root cause**: three-way pile-up. (1) Legacy private-CoS convs had `auto_respond=False` from before the flag was default-True. (2) The image-intent classifier wrongly tagged document-shaped phrasing like *"put together everything in a contract"* as an image request, clearing responders. (3) No safety net guaranteed at least one CoS reply per private-CoS conv.
- **Fix shipped**: three layered guarantees in `server.py` — (a) startup migration that heals every legacy `kind=private_cos` conv (idempotent: stamps `auto_respond=True` + ensures CoS in `agent_ids`); (b) force CoS into responders after the router/smart-expand decision so smart-expand can add specialists alongside CoS but never replace it; (c) re-force CoS AFTER the image/video intent block clears responders so a misfiring classifier can't silence the room.
- Verified end-to-end: reset Reggie's CoS to Ben's exact broken state (auto_respond=False, agent_ids=[]) → sent Ben's exact wording → CoS now replies *"to draft a consulting agreement contract for hire, I need several details..."* + conv self-heals to `auto_respond=True, agent_ids=[doc-generator, cos]`.
- 8-case pytest covers the document-request false-positives + clear-image-request still-recognised side.

---
## 💺 May 14, 2026 — Seat Policy: Soft Enforcement + Access-Code Boost
- New `seat_policy.py` helper + `GET /api/orgs/{org_id}/seat-status` endpoint surfaces each org's seat allowance vs. usage so the invite UI can render an in-product warning when admins go over their plan.
- **Soft enforcement only** — invites still go through. The amber "Seat allowance exceeded" banner with "Upgrade to Temper" / "See pricing" CTAs appears above the invite controls instead of blocking the flow (so we don't break existing customers who already over-invited under the old code).
- **Access-code boost**: owners who joined via one of our pre-launch private/trial codes get free **Temper-equivalent** allowance (5 seats) for the lifetime of their code — emerald "Access-code boost active" banner appears in place of the warning so they know they're inside the deal.
- Boost respects code expiry: when an admin code expires (or the 24-hour trial timer runs out), the org drops back to its actual plan and the warning re-appears automatically.
- Coverage: 5-case pytest verifies the Spark-no-boost warning, boost-lifts-to-Temper case, code-expired-drops-back case, and over-cap-while-boosted (still warns, mentions the boost).

---
## 🧙 May 13, 2026 — Custom Agent Builder: Guided Wizard
- Replaced the single intimidating "system prompt" textarea with a **4-step guided wizard**: Identity → Role → Voice & rules → Preview.
- **8 ready-made templates** on step 1 (Pricing Strategist · Snarky CFO · Brand Guardian · SDR Coach · Investor Memo Writer · User Researcher · Product Strategist · Ops Systematizer) — one click pre-fills the wizard so users can ship their first agent in <60 seconds.
- **Voice & rules step** asks 3 plain-English questions ("how should it talk?", "what should it ALWAYS do?", "what should it NEVER do?") and the **drafter LLM** (gpt-4.1-mini) composes a production-grade system prompt automatically. Hide the "system prompt" jargon entirely from non-technical users.
- **Live preview chat in step 4** — one-shot LLM call against the draft prompt so users can vibe-check their agent before saving. No history, fresh per message.
- **Refine** affordance on the composed prompt — say *"make it more concise"* or *"add humor"* and get a v2 without re-typing.
- **Advanced mode** toggle for power users who want the raw system-prompt textarea back.
- Backend: `POST /api/orgs/{org_id}/custom-agents/draft-system` + `POST /.../preview` (both 30-45s timeouts, gpt-4.1-mini, defensive JSON parsing).
- Live-verified: Snarky CFO template → wizard composed a 901-char prompt with citations to "public-company 10-K filings", LTV/CAC, Rule of 40. Preview chat then produced an in-voice reply roasting a "$50M ARR in 18 months" claim.

---
## ✨ May 13, 2026 — Mission Goal Drafter
- New "✨ Help me write this" affordance on the Start-a-mission dialog. Vague mission goals are the #1 cause of bad runs — this turns a rough idea into a polished goal that passes Foundry's mission best-practice checklist.
- Three-field structured input: *Goal (rough)* + *Deliverable format (optional)* + *Success looks like (optional)*. The LLM enforces: named filename + extension (defaults to `.pdf`, never `.txt`), 3-5 measurable success criteria, ≥5 source citations for research, no vague qualifiers (*"comprehensive"*, *"thorough"*), no empty-promise verbs, definition-of-done sentence at the end.
- Character budget enforced: 200-1,200 chars (sweet spot verified across recent finished missions).
- **Refine** button on the polished draft — say *"make it shorter"* or *"focus on B2B SaaS"* and get a v2 without re-typing.
- Backend: `POST /api/missions/draft-goal` (gpt-4.1-mini, 45s timeout, strict-JSON output, defensive clip to char cap).

---
## 🚦 May 13, 2026 — Mission Ask-User Loop Fix
- Production bug: Missions kept re-asking the same clarifying question (e.g. *"can you provide more sources?"*) even after the user said *"I have nothing more to add / proceed / finish."*
- Three guards added end-to-end:
  - **Hard cap of 2 `ask_user` calls per mission.** The 3rd is blocked server-side with a synthetic instruction forcing the agent to deliver from public data or finish honestly noting the gap.
  - **"User is done" detector** on `/missions/{id}/answer` — phrases like *"proceed / skip / nothing more / use what you have / stop asking / wrap it up / I already replied"* flip a `user_declined_more_info` flag that blocks every subsequent `ask_user` for the rest of that mission.
  - **New "Just finish with what you have" button** on the *agent is asking* card — one click hits `POST /api/missions/{id}/force-finish`, sets the same flag, and resumes the loop with `ask_user` blocked.
- 26-case pytest covers the regex (declined phrases match; real new info does not); regression on the existing mission suite stable.

---
## 🔒 May 13, 2026 — Memory: Strict Per-Org Isolation
- Memories are now **strictly scoped to one organization** — a fact captured inside *Client A's* workspace can never leak into *Client B's* agent replies (critical for agencies running multiple client workspaces under one Foundry account).
- Adding a memory manually now **requires picking an org** — no more accidental "global" memories.
- One-time **migration banner** surfaces any legacy untagged memories so users can assign each to an org or delete it (untagged memories are never injected into any agent reply until they're triaged).
- New **org picker** on /app/account → Memory: pick which workspace's memories you're auditing.
- Live-verified: same question to *Reggie Lab's* General team mentions the seeded "fintech agency" fact; same question to *SG Test's* General team does NOT (because the memory was tagged to Reggie Lab only).

---
## 🧠 May 13, 2026 — Memory Phase 1 (User-Scoped)
- Foundry now **remembers durable facts about each user** across every conversation — brand voice, ICP, pricing preferences, past clients, agency playbooks, agent feedback, work context, and general preferences.
- Auto-captured in the background (no chat latency) by an LLM extractor after every user/agent exchange — gpt-5.2-mini distills the turn into structured memories, then dedupes case-insensitively so the list stays clean.
- Injected into every agent's system prompt before generating a reply — so all 169 agents stay on-brand from message 1, not message 50.
- Full audit + control panel on `/app/account`: add memories manually (great for day-one onboarding), filter by category, edit, archive, or hard-delete anything.
- Live verified end-to-end: pre-seed "User runs a 5-person fintech agency in Austin called Reggie Lab" → next reply from the General team correctly answers *"What kind of business do I run?"* with "You run Reggie Lab, a fintech agency…" — the memory + org Soul fused naturally.
- Compliance posture: users own their memory (right-to-correct + right-to-delete), cross-user isolation enforced at the DB filter layer, no credentials/PII can be extracted by design.

---
## 🕒 May 12, 2026 — Timestamps Now Local to the Viewer
- All UI timestamps (chat messages, audits, missions, social queue, schedules, documents, recent conversations, activity feed, admin tables, public verify page, integrity badges) now reflect each user's browser timezone.
- New shared helper at `/app/frontend/src/lib/datetime.js` (`parseServerDate`, `formatLocalDateTime`, `formatLocalDate`, `formatLocalTime`, `formatRelativeTime`, `formatShortDateTime`).
- Root cause: MongoDB returns naive datetimes; FastAPI serialised them as ISO strings without `Z`, which the browser parsed as **local** time instead of UTC. The helper appends `Z` when no offset is present so naive timestamps are correctly anchored to UTC before being rendered in the user's locale.

---



## 🏢 Multi-Org & Team Setup
- **Organizations & Teams** — each business gets its own private workspace, with teams inside that have their own brand voice ("Soul")
- **Invite system** — share a link or email teammates straight into an org
- **Owner controls** — only the org owner can change brand logos, colors, and tagline
- **Edit & delete orgs and teams** with safety guards so the General team stays protected
- **Open vs Closed teams** — closed teams are private to invited members; open teams are visible to everyone in the org

## 🤖 The Agent Crew
- **169 specialised agents** across 14 divisions — Marketing, Engineering, Design, Ops, Finance, Legal, Sales, and more
- **Custom Agents** — teams can build their own agents with custom personas, expertise, and tone
- **Smart routing** — when you type a question, Foundry picks the best agent automatically
- **Dynamic intelligence** — picks the right AI model for each task (Claude, GPT, Gemini)
- **Add or remove agents from group chats** anytime
- **Switch agents mid-conversation** without losing context

## 💬 Conversations
- **Direct chats** (just you and one agent) and **group chats** (multiple agents collaborating)
- **@mention agents** in group chats, or turn on auto-respond so they jump in proactively
- **Continue any agent reply privately** — one click forks the conversation into a 1:1
- **Cancel any agent reply mid-generation** with a red Stop button
- **👍/👎/Copy** on every message
- **Slash command** `/mission <goal>` to spin up an autonomous mission from any chat
- **Always-visible scroll arrows** so you can jump to top or latest message

## ✍️ Content Creation
- **Auto image generation** (Nano Banana + GPT Image 1) — ask for an image, get one inline
- **Image editing** — upload a photo, say "make it brighter", get the edited image back
- **Edit prompt & regenerate** any image with a different model
- **Auto video generation** (Kie.ai Veo 3.1) — including TikTok/Reel vertical formats
- **PDF & Word document generation** — agents produce real, polished, branded files (no more `.txt`)
- **Interactive App Artifacts** (Claude-style) — agents can build working pricing calculators, ROI tools, dashboards, quizzes, or any single-page web app
  - Live preview in chat
  - Public share URL anyone can open
  - Download as ZIP for GitHub upload

## 📱 Social Media
- **Social Queue** — generate drafts for 11 platforms (Twitter, LinkedIn, Facebook, Instagram, TikTok, YouTube, Pinterest, Threads, Bluesky, Mastodon, Reddit)
- **Three states**: Draft → Scheduled → Published
- **Schedule auto-posting** — Foundry's Mission engine logs in and publishes the post at the chosen time (7 platforms supported)
- **Cover image generation** with brand-aware presets
- **Edit image prompt** per post for custom creative direction
- **Style Guide** — Foundry reads your existing profiles + sample posts and synthesises your brand voice automatically
- **Deep-links and "Copy for IG/TikTok/YouTube"** for platforms without a web composer

## 🔍 Research & Web
- **Real-time web search** with cited sources and rich link previews
- **Anti-hallucination** — when a user pastes a URL, Foundry fetches the actual page and the agent reads it for real (no made-up numbers)
- **Perplexity Sonar** kicks in for time-sensitive queries
- **45-second URL fetch budget** with static HTML fallback for tricky sites

## 🚀 Missions (Autonomous Multi-Step Work)
- **Headless browser** — agents can browse, click buttons, fill forms, and submit them on any public website
- **Page-map intelligence** — agents see every link, button, and form field with its label, so they navigate accurately
- **Run from anywhere** — slash command, rocket button, or smart-detect pill in chat
- **Live status card in chat** — see progress without leaving the conversation
- **Work log** showing every step, screenshot, and result
- **Cancel + delete missions** with full cleanup
- **Stall protection** — 90s LLM timeout, 60s tool timeout, "stalled" warning in UI
- **Research-breadth gate** — research missions must hit ≥5 different sources before finishing (no more "I cited one website" embarrassments)
- **Autonomous social posting** — Missions can log in to your social accounts and publish scheduled posts

## 📊 Business Audits
- **Public 90-second audits** at `foundryagents.ai/get-audit` — drop a website, get a scored brief with gaps & opportunities
- **In-app audits** — logged-in users can run audits from `/app/audits`, organized by their org
- **Auto-generated PDF** for every completed audit
- **Retry stuck audits** with one click
- **Watchdog** — audits that take longer than 4 minutes auto-fail with a clear error
- **Org-shared history** — every audit team members run is visible to the whole org

## 🔥 Crucible (Live Remote Browser)
- ChatGPT-Agent-Mode-style live browser streaming
- Encrypted credential vault for saved logins (Shopify, X, LinkedIn, etc.)
- Save a login once → every future mission reuses the cookies seamlessly

## 🎨 Brand Identity
- **Company "Soul"** — Foundry scrapes your site, builds a mission/audience/tone/values profile, then injects it into every agent prompt
- **Brand logos** stored durably (mirrored to GridFS so they never disappear after deploys)
- **Style guide** — voice, vocabulary, image style, and cadence — auto-applied to every social post and agent reply
- **Light + Dark theme** site-wide with persisted toggle (now on the homepage too!)
- **Re-synthesise the Soul** anytime — agents update their tone instantly

## 🔐 Trust & Security
- **Proof of Integrity** — Bitcoin-anchored timestamps on key audits (Temper+ feature)
- **Encrypted credential vault** for stored logins
- **CSP, brute-force lockout, rate limiting** on auth
- **Pre-launch access gate** for new signups
- **JWT secrets persisted to the database** — sessions survive every redeploy (no more "log in again")
- **Search respects access controls** — closed teams, private chats, and other orgs are filtered per-user

## 🎫 Pricing & Billing (Live on the Pricing Page)
- **Spark — $80/mo** for solo founders
- **Temper — $300/mo** for small teams
- **Refine — Custom (from $1,500/mo)** for enterprise
- **Comparison matrix** — 7 grouped sections covering every feature gap
- **Add-ons** for Spark growth:
  - Extra organization — $20/mo
  - Extra seat — $30/mo
  - 500 message pack — $15 (one-time)
  - 200 browser-minute pack — $20 (one-time)
  - 10 audit pack — $10 (one-time)
- **ArcheForge Studios** — managed agency services for clients who want done-for-you work

## 🔎 Search
- **`⌘K` / `Ctrl+K`** from anywhere opens a Spotlight-style search
- Searches across **conversations, messages, missions, documents, social posts, and agents** in your active org
- **Ask Foundry** button — runs an AI synthesis over the results with inline `[1] [2]` citations
- **Per-user access controls** — never leaks private chats or closed teams

## 📱 Mobile & PWA
- **Install to home screen** — iPhone/Android PWA support
- **iPhone notch / Dynamic Island** safe-area handling
- **Mobile-friendly responsive design** across every page
- **Light/dark contrast pass** so text is readable on every device

## ⚡ Reliability & Speed
- **Auto-fallback to your OpenAI key** if the Emergent universal key budget hits zero — chat never goes down
- **Forced-delivery retry** — if an agent says "I'll get back to you", the system automatically nudges it to deliver right now
- **Empty-promise detector** — appends a helpful "reply 'go' to actually do it" footer if the agent is stalling
- **Auto-recover stuck audits** on every deploy
- **Polished file rendering** — PDFs, DOCs, images, and videos all display cleanly inline in chat
- **Smart auto-scroll** that lands on the latest message without yanking you down while you're reading history

## 🌐 Public Pages
- **`foundryagents.ai`** — landing page with hero, what-it-does, FAQs
- **`/about`** — long-form story of what Foundry is
- **`/pricing`** — tiers, matrix, add-ons, FAQs
- **`/get-audit`** — public 90-second business audits
- **`/audit/:slug`** — public audit results page
- **`/deck`** — pitch deck
- **`/trust`** — security & data handling
- **`/share/app/:token`** — public live preview of any app artifact

---

## 🗓️ Build Timeline (high-level)

| When | What we shipped |
|---|---|
| **Apr 17, 2026** | Initial scaffold |
| **Apr 27–30, 2026** | Foundation — orgs, teams, 169 agents, Soul, image/video gen, light/dark, PWA, Social Queue MVP, in-chat file uploads, Style Guide, Resourceful Agents (artifact generation) |
| **May 1, 2026** | Mission engine — headless browser, form-fill, page-map intelligence, missions inside conversations, loop-quality hardening, owner UX polish (composer pinning, reactions) |
| **May 2–4, 2026** | Real PDF & DOCX synthesis, autonomous scheduled social posting via Missions, Media Library + Social Queue integration, WorkingIndicator, stop-mid-reply cancel button, light/dark contrast pass |
| **May 5–7, 2026** | iPhone PWA notch fix, theme-aware logo wiring, anti-hallucination (auto-fetch user URLs as ground truth), brand assets owner-gating, bulletproof artifact rendering, GridFS-backed logo persistence |
| **May 8–10, 2026** | Foundry rebrand (from SoulPrint Engine Pro), Pre-Launch Access Gate, Public Business Audits (`/get-audit`), Crucible (live remote browser + credential vault), pitch deck refresh, ArcheForge Studios pricing arm |
| **May 11–12, 2026** | Custom Agents, Interactive App Artifacts (sandboxed iframe + share URL + ZIP export), unified `⌘K` Search with AI synthesis + access controls, In-App Audits with watchdog & retry, JWT secret persistence (no more re-login after deploys), Mission research-breadth gate, Pricing matrix + Add-ons strip, theme toggle on homepage |

_Roughly 3.5 weeks from blank repo to the platform that lives at `foundryagents.ai` today._

---

_Last updated: May 12, 2026 — kept in `/app/memory/CHANGELOG.md`_
