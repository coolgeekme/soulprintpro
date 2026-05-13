# 🔧 Foundry — What We've Built So Far

Plain-English feature inventory of **Foundry by SoulPrint Engine** for non-technical teammates. Each section is a category of capabilities and what they do for users.

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
