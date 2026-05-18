# Foundry Agents — White Paper

**The B2B AI workforce platform for agencies, founders, and operators.**

*Last updated: February 2026 · Foundry Agents · [foundryagents.ai](https://foundryagents.ai)*

---

## Executive Summary

Foundry Agents is a **multi-tenant AI workforce platform** that lets a single human operator run an unlimited number of brands, clients, or product lines — each with its own dedicated team of specialised AI workers, its own brand voice, its own document library, its own social publishing accounts, its own billing, and its own audit trail.

It is built for the operator who has outgrown ChatGPT but cannot justify hiring a marketing team. It replaces — credibly and conservatively — a stack of seven to ten point tools (ChatGPT Team, Jasper, Buffer, Lindy, Notion AI, Firecrawl, a junior marketer, a research VA, and a discovery-audit consultant) with one organisation, one login, one bill, and one source of truth.

Foundry's core thesis is that **the bottleneck in solo / small-team operations is no longer access to AI; it is integration, brand consistency, and trust**. A ChatGPT subscription gives you a chatbot. Foundry gives you a workforce that knows your customers, uses your tools, ships finished deliverables, and never confuses one client's brand with another's.

This document covers the platform's architecture, the agent runtime, the SoulPrint brand-injection layer, the integration surface, the security and isolation model, the publishing pipeline, the audit and research stack, the autonomous-missions runtime, the cross-org Telegram interface, the billing and observability stack, and a series of comparison frameworks against the tools Foundry is designed to replace.

The intended reader is a founder, agency owner, ops lead, or technical evaluator who needs to understand not just *what* Foundry does but *why* the architectural choices behind it matter for their use case.

---

## Table of Contents

1. [The Problem Foundry Was Built to Solve](#1-the-problem-foundry-was-built-to-solve)
2. [System Architecture Overview](#2-system-architecture-overview)
3. [The SoulPrint Model — Brand-Aware AI](#3-the-soulprint-model--brand-aware-ai)
4. [The Agent Catalogue — 150+ Specialists Plus a Builder](#4-the-agent-catalogue--150-specialists-plus-a-builder)
5. [Conversations — DM, Group, and Team-Visible Threads](#5-conversations--dm-group-and-team-visible-threads)
6. [The Artifact System — `[[ARTIFACT]]`, `[[APP]]`, `[[IMAGE]]`](#6-the-artifact-system--artifact-app-image)
7. [Missions — Autonomous Multi-Step Work](#7-missions--autonomous-multi-step-work)
8. [Forge Crawl — Our In-House Web Intelligence Stack](#8-forge-crawl--our-in-house-web-intelligence-stack)
9. [Business Audits — Lead-Gen Quality In 90 Seconds](#9-business-audits--lead-gen-quality-in-90-seconds)
10. [Crucible — Live Remote Browser Automation](#10-crucible--live-remote-browser-automation)
11. [Social Publishing — From Draft to Posted in One Reply](#11-social-publishing--from-draft-to-posted-in-one-reply)
12. [Composio Integrations — 250+ Connected Tools](#12-composio-integrations--250-connected-tools)
13. [The Telegram Super Chief of Staff](#13-the-telegram-super-chief-of-staff)
14. [Memory, SoulPrint, and Multi-Tenant Isolation](#14-memory-soulprint-and-multi-tenant-isolation)
15. [Trust, Security, and Compliance Posture](#15-trust-security-and-compliance-posture)
16. [Billing, Observability, and Spend Caps](#16-billing-observability-and-spend-caps)
17. [Performance and Reliability](#17-performance-and-reliability)
18. [The Operator's Stack — What Foundry Replaces](#18-the-operators-stack--what-foundry-replaces)
19. [Common Use Cases and Workflows](#19-common-use-cases-and-workflows)
20. [Pricing and Plans](#20-pricing-and-plans)
21. [Roadmap and Open Questions](#21-roadmap-and-open-questions)
22. [Getting Started](#22-getting-started)

---

## 1. The Problem Foundry Was Built to Solve

The operator class has changed. Five years ago, a solo founder ran a business with a laptop, a Slack, and a Stripe account. Today the same founder is expected to ship the marketing of a small agency, the research output of a junior analyst, the social calendar of a content team, the customer comms of a CS rep, the financial dashboards of a controller, and the legal hygiene of a paralegal. The expectation is right; the human capacity to deliver it is not.

The dominant response from the AI industry has been to ship more chatbots. The dominant outcome for the operator has been **tab sprawl**: a tab for ChatGPT, a tab for Jasper, a tab for Buffer, a tab for Lindy, a tab for Notion AI, a tab for an audit tool, a tab for a CRM, a tab for a research tool. Each is excellent in isolation. None of them know each other. None of them know the operator's brand voice. None of them remember yesterday. None of them can hand a deliverable to the next tool without the operator brokering the handoff.

The problem is not access to AI. The problem is **AI that finishes the job**.

Foundry's hypothesis is that the next generation of business software is not a chatbot but **a workforce**. The unit of work is not a prompt-and-response, but a goal handed to a role-specialised agent that knows the brand, has access to the tools, owns the deliverable, and reports back when it's done. The platform is the operating system that gives this workforce a structure (orgs, teams, roles), a voice (SoulPrint), a memory (documents + per-org isolation), a toolkit (Composio), and a way to talk to its human boss (web app, Telegram, email).

Everything in this document follows from that thesis.

---

## 2. System Architecture Overview

Foundry is a conventional three-tier web application with a deliberate bias toward boring, durable technology choices.

- **Frontend**: React 19, Tailwind, shadcn/ui. Light + dark mode. PWA-installable. Mobile-first responsive layout. Instrument Serif display + sand-on-orange editorial palette.
- **Backend**: FastAPI on Python 3.11. Async-first. Motor + MongoDB for primary data. APScheduler for background jobs (social publishing, mission resumes, daily pulses). No Redis, no Celery, no Kafka. The stack is deliberately monolithic; it is not webscale by design and does not pretend to be.
- **LLM runtime**: `emergentintegrations` wraps OpenAI, Anthropic, and Google so callers don't care which model is downstream. Default model is GPT-4.1-mini for chat, Nano Banana for image generation, Whisper for voice transcription, OpenAI TTS for voice replies.
- **Tool calling**: Composio SDK v3. Provides 250+ third-party integrations with OAuth flows, scope minimisation, and per-user / per-org connection isolation.
- **Browser automation**: Playwright headless Chromium. Used by Crucible (live remote browser) and Forge Crawl's JS-rendering fallback.
- **Web intelligence**: Forge Crawl (in-house, httpx + trafilatura + Playwright + LLM-driven structured extraction).
- **Auth**: JWT-based session cookies. Optional Google OAuth via the platform's managed adapter. HttpOnly cookie support.
- **Deployment**: Two-environment topology — `preview` (development pod, mutable) and `production` (https://foundryagents.ai, immutable except via promote-and-deploy).

The architectural commitment is to **make the simplest thing work first, and refuse to add complexity until the simplest thing breaks**. There is no microservice split. There is no queue. There is no caching layer. There is one Python process and one Mongo cluster. This makes the system unusually easy to reason about, easy to debug end-to-end, and unusually fast for the operator's small-team scale. The cost is that Foundry is not engineered for FAANG-scale load — which, given the target user is a one-to-ten-seat operator, is the right trade.

---

## 3. The SoulPrint Model — Brand-Aware AI

Most "AI for agencies" products try to solve brand consistency by letting you upload a style guide PDF and hoping the model reads it. This works approximately 40% of the time and produces approximately 0% reproducibility.

Foundry's answer is the **SoulPrint** — a structured five-field record attached to every team:

- **Mission**: what the brand exists to do.
- **Tone**: how the brand speaks (warm? clinical? rebellious? authoritative?).
- **Audience**: who the brand is talking to (founders? high-net-worth? working parents? developers?).
- **Values**: the operating principles the brand will not violate.
- **North Star**: the long-term qualitative goal.

The SoulPrint is **automatically injected into every agent's system prompt**, every time, for every reply, in every conversation, in every mission, in every image-generation request, in every social caption, in every audit. It is not a feature the user has to remember to use; it is the default state of every prompt.

The model matters because it is **machine-checkable** without being brittle. The SoulPrint is not a 4,000-word brand guide that an LLM has to ignore most of; it is five short paragraphs that fit in any context window and that an LLM consistently respects. We've validated this across multiple LLM providers — GPT-4.1, Claude Sonnet, Gemini — and the brand-consistency improvement is reproducible.

The practical consequence: an operator running ten brands sees ten distinctly different writing voices, ten distinctly different aesthetic directions in image generation, and ten distinctly different audit tones — without rewriting any prompts. The SoulPrint is the layer that turns one agent catalogue into ten brand-aware workforces.

---

## 4. The Agent Catalogue — 150+ Specialists Plus a Builder

Foundry ships with a catalogue of more than 150 named agents organised by agency role. The catalogue is not a list of personalities; it is a list of **job descriptions**. Each agent carries:

- A canonical role name (e.g. *Brand Architect*, *Funnel Surgeon*, *Crisis Comms Lead*, *Investor Relations Specialist*).
- A short bio.
- A system prompt scoped to that role's responsibilities.
- A default toolkit (which Composio tools are pre-enabled).
- A default model temperature.
- A default avatar.

The founding crew — **Nova** (Strategist), **Atlas** (Researcher), **Ember** (Copywriter), **Vector** (Engineer), **Sable** (Ops Manager) — was the original v1 set. The catalogue has expanded to cover specialised marketing, growth, brand, ops, finance, legal-adjacent, customer-success, data, and creative roles.

For roles the catalogue doesn't cover, the **Custom Agent Builder** is a guided wizard that turns a one-line description ("a Pinterest pin specialist who writes in our voice and uses the Pinterest API") into a fully-prompted, SoulPrint-aware, tool-enabled agent in under sixty seconds. The Builder asks five structured questions, drafts the system prompt, picks the right Composio toolkits, and writes a default avatar prompt. The output is a first-class agent that lives in the org and is available to every team member.

The architectural commitment behind this is that **the user picks a role, not a model**. They do not need to know what an LLM is. They pick the *Performance Marketer* and the platform handles the model selection, the prompt engineering, the tool wiring, and the SoulPrint injection. This is the central UX simplification that lets non-technical operators get value from the platform in their first session.

---

## 5. Conversations — DM, Group, and Team-Visible Threads

Conversations in Foundry are first-class objects, not ephemeral chat sessions.

- **Direct conversations** — one human talks to one agent. Like a Slack DM with a specialist. The state is persisted indefinitely.
- **Group conversations** — one human plus multiple agents in one thread. By default agents respond on `@mention`; the operator can flip an `auto_respond` toggle to make every agent reply to every message (useful for brainstorming).
- **Team-visible conversations** — by default, every conversation an agent participates in is visible to every member of the team that owns the conversation. This is a deliberate choice. Most "AI for teams" products silo conversations behind the user who started them. Foundry's stance is that the AI is a *team member*, and team members' work product belongs to the team, not the individual.
- **Private Chief of Staff threads** — the one exception. Every user gets a private CoS conversation in every org they're in. Only the owner sees it. This is where personal-context questions live.

The chat experience uses sub-second live polling so messages feel instant. There is no websocket budget; the polling is opportunistic and degrades gracefully. Attachments (PDFs, DOCX, CSV, plain text) are first-class and become agent-readable artefacts.

Idle conversations are soft-archived by an APScheduler job; the user can configure the idle window per-org. Archived conversations remain searchable; they just stop appearing in the recent-conversation sidebar.

---

## 6. The Artifact System — `[[ARTIFACT]]`, `[[APP]]`, `[[IMAGE]]`

Agents do not return prose alone. They return **deliverables**.

The artifact system is a markdown-extension grammar that lets any agent reply attach a real downloadable file to the chat:

- **`[[ARTIFACT name="..." mime="..."]]…body…[[/ARTIFACT]]`** — produces a `.md`, `.txt`, `.html`, `.pdf`, `.docx`, `.csv`, or any text-based file. The file is automatically saved to the org's Documents library, indexed, and downloadable from the chat bubble.
- **`[[APP]]…manifest…[[/APP]]`** — produces a multi-file working web app bundle (HTML + CSS + JS) that gets sandboxed, deployed to a public share URL, and shown inline in the chat as an interactive embed. Use cases: ROI calculators, quote generators, intake forms, embeddable lead-magnets.
- **`[[IMAGE prompt="..." aspect="..."]]`** — produces an image rendered via Nano Banana (default) or GPT Image 1 (optional). The image is saved to Documents, attached to the chat, and (when the conversation is on Telegram) sent as a native photo bubble.

Single-file HTML artefacts are auto-promoted to interactive `app` artefacts with a shareable URL. PDFs and DOCX files are rendered server-side from the markdown body using a deterministic pipeline (no LLM-driven binary generation). Images are persisted both to the org's Documents library and to a public share URL so they can be referenced by URL from third-party APIs (Meta, TikTok, etc.).

The system has a deliberate guardrail: **agents are prohibited from promising deliverables in a "next message"**. The system prompt makes this explicit. If an agent commits to producing a caption + an image for a social post, both must arrive in the same reply, with both an `[[ARTIFACT]]` block and an `[[IMAGE]]` block. A safety-net post-processor enforces this for social-media requests: if the user asked for a post and the LLM forgot the `[[IMAGE]]` block, the platform auto-injects one with a derived prompt and the right aspect ratio.

---

## 7. Missions — Autonomous Multi-Step Work

A Mission is what happens when a goal is too large for a single chat reply.

- **Input**: a goal, written in plain English ("research the top 5 competitors of our client Acme, write a positioning brief, and draft three LinkedIn posts highlighting our differentiators").
- **Output**: a set of artefacts — typically a research summary, a brief document, a deck, a social-post pack, an image set, or any combination thereof.
- **Runtime**: a single autonomous agent (the *mission runner*) picks the right tools, breaks the goal into steps, runs them, accumulates artefacts, and stops when the goal is met or the budget is exhausted.

Missions have hard guardrails:

- **Token + dollar budget caps** per mission. Runaway loops are mechanically impossible.
- **Pause / resume / ask-the-user** — the mission can stop mid-run and ask the operator a clarifying question via the chat UI. When the operator replies, the mission continues from where it stopped.
- **Tool budgets** — each tool category (browser, search, scrape, LLM, image-gen) has its own ceiling. Browser tools get a longer ceiling because Playwright is slow.
- **Composio access** — agents inside missions can call any of the 250+ Composio toolkits the org has connected (Gmail, HubSpot, Slack, Notion, Linear, Shopify, etc.).
- **Live progress** — the operator watches the artefact list pile up in real time as the mission runs.

The Mission Goal Drafter is a small auxiliary agent whose only job is to take a fuzzy idea ("I want to do a launch for our new pricing") and turn it into a precise mission brief before kick-off. This dramatically improves mission-success rates because the goal is less ambiguous.

---

## 8. Forge Crawl — Our In-House Web Intelligence Stack

Most "agent platforms" rent web-scraping from Firecrawl, BrowserBase, or Apify. This is fine for a venture-funded product. It is **terrible** for a small-team SaaS product, because (a) the per-page costs add up fast and (b) the AGPL licence on Firecrawl makes embedding it in a closed-source product legally awkward.

Foundry's answer is **Forge Crawl** — an in-house web intelligence stack with three public functions:

- **`scrape(url, render=…)`** — fetches a single URL and returns clean markdown of the main content (chrome, ads, nav stripped). Uses `httpx` for server-rendered sites (fast, ~100 ms) and auto-falls-back to Playwright for JS-heavy SPAs. Pluggable render modes: `auto`, `force`, `never`.
- **`crawl(url, max_pages=…)`** — shallow same-host crawl. Scrapes the seed page, then prioritises and scrapes up to N additional same-host pages (about, pricing, services, contact …) using a heuristic ranking that prefers high-signal pages over deep blog archives.
- **`extract(url, schema, instructions=…)`** — scrapes a page, then asks an LLM (default: GPT-4.1-mini) to coerce the content into a caller-supplied JSON schema. Returns one structured object. Useful for "give me the product name, price, and bullet features off this landing page" tasks.

Forge Crawl is exposed three ways:

- **As mission tools** — agents can call `web_scrape`, `web_crawl`, and `web_extract` inside any mission.
- **As HTTP endpoints** — `POST /api/forge/{scrape,crawl,extract}` for use by the frontend or external Foundry clients.
- **As the engine behind Business Audits** — the audit pipeline calls `crawl()` to grab the homepage + four priority pages, then feeds the concatenated markdown to the audit synthesiser.

Forge Crawl is licensed under Foundry's own commercial licence; there is no AGPL dependency. The total third-party cost per crawl is the LLM call inside `extract()`. Pure scrape calls have zero per-request cost beyond bandwidth.

---

## 9. Business Audits — Lead-Gen Quality In 90 Seconds

The Business Audit feature is Foundry's most-used lead-gen motion. Drop a website URL; in roughly 90 seconds you get a scored brief covering:

- Brand clarity and positioning
- Conversion friction
- SEO signal
- Social proof
- Competitive positioning
- Specific, actionable recommendations

The audit is multi-page — Forge Crawl scans the homepage plus the prioritised set of secondary pages — so the synthesiser reads the whole story instead of a single landing page. The result is a downloadable PDF, branded, formatted, and ready to send to a prospect.

Two distribution modes ship in the box:

- **In-app audits** — for the org's own team. Privately viewable.
- **Public lead-gated audits** — share a public URL. Visitors enter their email before they can read the full report. The lead is captured against the org's owner.

This second mode is the agency growth engine. Most discovery-audit consultants charge $1,500 to $5,000 for a similar deliverable produced over one to two weeks. Foundry produces the same deliverable in 90 seconds and lets agencies use it as a top-of-funnel lead magnet. The conversion rate from "received an audit" to "booked a discovery call" has been the highest-ROI feature for our agency customers.

---

## 10. Crucible — Live Remote Browser Automation

Crucible is the answer for the case when an agent needs to do something that has no API. Log into a SaaS, scrape a paywalled report, fill a multi-step intake form, drive a check-out flow, click around the inside of a customer's CRM that they refuse to give you an API key for.

Crucible spins up a real headless Chromium browser (Playwright-driven) and exposes it to the agent through a tool surface:

- `browse_url` — load a page, take a screenshot, and return the rendered HTML plus a structured page-map of forms, buttons, and links with their visible text.
- `browse_click` — click any element by selector.
- `browse_fill` — fill a form field.
- `browse_submit` — submit a form.
- `browse_text` — extract text from a region.
- `browse_keyboard` — send keyboard input.

The browser canvas is live-streamed into the operator's dashboard so they can watch the mission progress. This is both fun and useful — when something goes wrong, the operator sees exactly what the agent saw.

The roadmap item that closes Crucible's biggest open gap is **Auth Handoff** — the ability to pause the mission, hand the browser to the operator to clear an OAuth challenge or solve a captcha, and resume the mission. This is in progress and is the highest-priority Crucible roadmap item.

---

## 11. Social Publishing — From Draft to Posted in One Reply

The Social Queue is one of the most-used Foundry surfaces. It is a calendar-aware composer that lets the operator (and their agents):

- **Generate a post** with any agent (text + image + hashtags + scheduling target).
- **Schedule** the post via a picker — date, time, time-zone-aware.
- **Open in composer** — one click opens the appropriate platform's native composer (X, LinkedIn, IG, FB, TikTok, Threads, Pinterest, Reddit) with the caption pre-filled. Use this when the org doesn't have an API connection.
- **Publish via Foundry** — for orgs with active Meta / X / TikTok / LinkedIn Composio connections, post directly through the platform API. No third-party Buffer or Hootsuite.
- **Auto-publish** — for scheduled posts on a connected platform, APScheduler fires the publish at the scheduled time.

Meta's API has a thicket of constraints that Foundry handles in the background:

- IG requires a **Business or Creator** account linked to a Facebook Page; personal IG accounts cannot post via API.
- The Facebook connection must hold `pages_show_list` + `pages_manage_posts` + `instagram_basic` + `instagram_content_publish` scopes.
- Image URLs sent to Meta must be **absolute https URLs on a routable host** (this is a common failure mode for self-hosted platforms that store relative paths).
- Each org needs a **pinned target Page + IG account** so multi-brand operators don't accidentally cross-publish.

Foundry's response is a per-org Meta Target Picker UI: pin one Facebook Page (and its linked IG Business account) per org. The platform stores both IDs in the org's connection record and uses them every time a post is published.

Carousels are first-class: the agent emits one `[[IMAGE]]` block per slide for IG / LinkedIn carousels, with visually consistent prompts.

Failed publishes land in a dedicated **Failed** tab with the actual Meta error message (rate-limit, scope missing, image-fetch failed), so the operator can fix and retry with one click instead of seeing a generic "publishing failed" toast.

---

## 12. Composio Integrations — 250+ Connected Tools

Composio is Foundry's integration backbone. It provides OAuth flows, scope minimisation, action execution, and a unified tool-calling surface for 250+ third-party tools.

**Productivity**: Gmail, Google Calendar, Google Drive, Outlook, Notion, Linear, Asana, Trello, ClickUp.

**CRM**: HubSpot, Salesforce, Pipedrive.

**Comms**: Slack, Discord, Telegram, Twilio SMS.

**Social**: Twitter / X, LinkedIn, Reddit, Threads, Bluesky, Mastodon, YouTube, TikTok.

**Commerce**: Shopify, Stripe, PayPal, Square.

**Dev**: GitHub, GitLab, Bitbucket.

**Marketing**: Mailchimp, SendGrid, ConvertKit, Klaviyo.

**Data**: Airtable, Google Sheets, Excel Online.

**Plus** every other Composio toolkit — 250+ at time of writing, growing weekly.

Each connection is **per-user, per-org, scope-explicit**. Connecting Gmail in Org A does not grant Org B access to that Gmail. Disconnecting in Org A does not affect Org B. The user can audit which scopes have been granted at any time via the Connections panel.

Composio supports two modes:

- **Default-managed OAuth** — Composio handles the OAuth dance using its registered client IDs. This is the default for most toolkits and requires zero setup on the operator's part.
- **Bring-Your-Own-Auth (BYOA)** — some toolkits (Shopify, certain enterprise SaaS) require the operator to register a private OAuth app first and supply the client ID / secret. Foundry surfaces this requirement explicitly in the UI with a disabled-state badge rather than failing silently with a confusing API error.

Smart defaults turn on the right tools for each agent automatically: a Performance Marketer agent gets Google Ads + Facebook Ads + Mailchimp turned on by default; a Customer Success agent gets HubSpot + Intercom + Calendly.

---

## 13. The Telegram Super Chief of Staff

The Telegram interface is the operator's mobile access to their entire Foundry workforce. It is **org-agnostic by default**, which makes it materially different from every other "AI bot for Telegram" product.

When the operator messages the bot, the message is routed by default to a **Personal Concierge** — a read-only, cross-org assistant that can:

- Pull **calendar** events across every Google Calendar connected to any org.
- Pull **Gmail** across every Gmail account connected to any org.
- Read the **full body** of any email by message ID.
- Search **documents** across every org's library.
- List the operator's **org overview** (recent activity, missions, scheduled posts).
- Answer **general chitchat / world knowledge / casual questions** without forcing a tool call.
- **Draft email replies, message drafts, talking points** — output them as text the user can copy.

The Personal Concierge cannot mutate org data. If the user asks for a write action ("send Acme an email saying yes"), the Concierge punts back with a `/org Acme send the email saying yes` instruction. The operator can then route the request to the specific org's CoS, which has the toolkits to actually send the email.

Explicit org-routing uses two grammars:

- **`/org <name> ...`** — explicit slash command. Routes the message to the named org's Chief of Staff.
- **Inline mention** — used to be the default, but caused too many "I asked ABOUT an org, not TO an org" hijacks. We removed inline auto-routing in favour of the slash-only explicit grammar.

Voice in, voice out: send a voice note, the bot transcribes with Whisper, processes the request, and replies with both a text bubble and a TTS voice note rendered as a native Telegram voice bubble (OpenAI TTS, voice=`nova`, format=`opus`).

Proactive nudges (the **CoS Pulse**) schedule outbound messages from any org's CoS to the operator's Telegram on a cadence (daily, weekly, custom). Use cases: morning brief, end-of-day status, weekly retros.

---

## 14. Memory, SoulPrint, and Multi-Tenant Isolation

Multi-tenant isolation is the single most-audited claim Foundry makes. The platform's commitment is that **memory written in Org A is mechanically unreachable from Org B**.

The implementation has three layers:

- **Application-level guards** — every collection query that touches user data includes the `org_id` in the filter. We never do `db.documents.find({"id": doc_id})`; we always do `db.documents.find({"id": doc_id, "org_id": ctx["org_id"]})`. There is no path through the code that retrieves an org-scoped object without an org_id filter.
- **Row-level isolation in Mongo** — every collection that holds org data carries an `org_id` field. There is no "global" data; even system documents are stamped with the org they belong to.
- **Agent-runtime injection** — when an agent runs, it receives a context object carrying the active `org_id`, `user_id`, and `team_id`. The context cannot be tampered with from inside the agent's prompt.

User-scoped memory is a separate concept: personal preferences (writing style, default formats, schedule habits) follow the **user** across orgs without leaking org data. This is implemented with a separate `user_memory` collection keyed by `user_id`, never by `org_id`.

SoulPrint isolation: each team's SoulPrint is stored on the team document, scoped to the org. There is no cross-team SoulPrint leakage. Custom agents inherit the SoulPrint of the team they were created in.

We do not train upstream LLMs on operator data. The default OpenAI / Anthropic / Google API tier we use is configured to opt out of model training. The same is true for the OpenAI-hosted Composio actions. Data leaves the platform only when an operator explicitly sends it (via a Composio tool, a download, or a share URL).

---

## 15. Trust, Security, and Compliance Posture

Foundry's security posture is built for SOC 2-curious operators without the SOC 2 audit overhead.

- **Auth**: JWT sessions, HttpOnly cookie support, optional Google OAuth via the platform's managed adapter.
- **Authorisation**: per-org membership table with explicit roles (Owner, Admin, Member, Viewer). Every API endpoint that touches org data calls `require_org_member(org_id, user_id)` before doing anything.
- **OAuth scope minimisation** — agents request only the scopes they need. The user can revoke at any time via the Connections panel.
- **Audit trail** — every mission, every publish, every tool call, every conversation, every audit run is logged with an `org_id`, `user_id`, timestamp, and outcome. Spend Dashboard surfaces the same data.
- **Environment separation** — `preview` is the development pod; production is `https://foundryagents.ai`. The two have separate databases. Code can only reach production via an explicit promote-and-deploy step.
- **No retraining on operator data**.
- **PII handling** — operators control which integrations they connect. The platform does not enrich, sell, or share contact data ever.

Compliance items on the roadmap (not yet committed):

- SOC 2 Type I — likely Q4 2026 if customer demand justifies.
- GDPR-friendly data-export and right-to-erasure flows — partially implemented (account delete works; data export is one-click for org owners and is being broadened).
- HIPAA — not in scope. Foundry is not a covered entity; operators handling PHI should not use Foundry as the system-of-record for that data.

---

## 16. Billing, Observability, and Spend Caps

The Spend Dashboard is the operator's window into what the AI workforce is actually costing them. Every LLM call, every image generation, every Composio tool call, every audit run, and every mission carries:

- A token count (prompt + completion).
- A dollar cost.
- A model identifier.
- A kind label (chat, image, audit, mission, tool).
- An `org_id`, `user_id`, and (when applicable) `mission_id`.

The dashboard slices this data by user, by org, by agent, by day, by month, and by category. The operator can ask:

- "Which agent ran hot last Tuesday?"
- "How much did we spend on Acme Corp's social calendar this month?"
- "Who on the team is the heaviest LLM user?"
- "Did we exceed our spend cap for Open Lounge yet?"

Spend caps are enforced two ways:

- **Soft warning** — when the operator hits 75% of their monthly cap they get a Telegram nudge plus an in-app banner.
- **Hard refusal** — when they hit 100%, the platform refuses to spend past it. Existing missions are paused, new chat replies fail-soft with a clear "you've hit your monthly cap" message.

The plan tiers (Free, Starter, Growth, Agency, Enterprise) each carry a base spend allowance plus an overage rate. The overage rate is published on the pricing page. There are no surprise bills.

Access codes are a separate mechanism: a special code can boost a seat count, extend a trial, or unlock a higher tier. Used for partner programmes and friends-and-family rollouts.

---

## 17. Performance and Reliability

The platform's performance commitments are calibrated for small-team operators, not webscale:

- **Conversation reply latency** — typically 2–4 seconds end-to-end (LLM call + artefact processing + database write).
- **Image generation** — typically 6–12 seconds via Nano Banana; 18–30 seconds via GPT Image 1.
- **Audit run** — typically 60–120 seconds (crawl + synthesis + PDF render).
- **Mission step latency** — variable; depends on tools called.
- **Social publish** — typically 4–8 seconds (image upload + container creation + publish).

Reliability targets:

- **99.5%+ uptime** is the current operational baseline. Foundry runs on a managed-Kubernetes deployment with health checks and auto-restart.
- **Hot-reload in preview** — code changes propagate in seconds.
- **Zero-downtime production deploys** — promote builds are non-disruptive.

The platform is deliberately monolithic. There is one Python process per pod, one Mongo cluster, and one APScheduler instance. This makes incident response unusually fast: there's only one log stream to read.

---

## 18. The Operator's Stack — What Foundry Replaces

A typical Foundry customer comes from a stack that looks something like this:

| Tool | Annual Cost (typical) | Foundry Replacement | Notes |
|------|----------------------|---------------------|-------|
| ChatGPT Team (5 seats) | ~$1,800 | Multi-org Foundry workspace | Foundry adds brand voice, tool calling, audit trail |
| Jasper | ~$600–1,200 | Brand-aware artefact generation | SoulPrint replaces Jasper's brand kit |
| Buffer / Hootsuite (Pro) | ~$600 | Social Queue + Publish via Foundry | Live API publishing included |
| Lindy / Relay | ~$1,200–3,000 | Missions with Composio tool calling | Foundry runs the same workflows |
| Notion AI | ~$200 | Documents library + agent search | Same use case, integrated |
| A junior marketer (US, contract) | ~$30,000–60,000 | Honestly, most of what one does in a week | Mileage varies; the platform doesn't replace strategic judgement |
| Firecrawl Scale | ~$700–2,400 | Forge Crawl (included) | Native, no AGPL |
| Discovery audit consultant | ~$1,500–5,000 per audit | Business Audits (free for the operator, paid by lead capture) | Different business model |
| Browser-automation tooling (BrowserBase / Apify) | ~$300–1,000 | Crucible (included) | Native Playwright stack |

**A small agency running three to five client brands typically replaces $10,000 to $30,000 of annual tooling spend with a Foundry Growth or Agency plan**, plus the time savings from not running ten different tabs.

The non-replaceable items: strategic judgement, client relationship management, and the moments when a human needs to be in the room. Foundry is a workforce, not a CEO.

---

## 19. Common Use Cases and Workflows

A non-exhaustive list of how operators actually use Foundry day to day:

**Agency content production**: an operator runs 5 client brands. Each is its own org with its own SoulPrint. Monday morning, the operator opens the Social Queue for each org, asks the Copywriter to draft the week's posts, reviews and approves them, and schedules for the week. Total time: 30 minutes for 5 brands and 25 posts.

**Lead-gen via audits**: an agency embeds the lead-gated audit URL on their landing page. A prospect drops their site URL, enters their email, and gets the audit in 90 seconds. The agency owner sees the lead pop into HubSpot via Composio, reaches out within an hour, and books a discovery call with the audit as the conversation-starter.

**Competitor research**: a founder asks the Researcher to "build a competitive landscape brief for our top 5 competitors". The mission runs in the background, scrapes each competitor's site via Forge Crawl, pulls pricing pages, identifies gaps, and produces a 4-page brief plus a comparison table. Total time: 8 minutes.

**Email triage**: the operator asks the Telegram bot "what important emails do I have today?" The Personal Concierge pulls Gmail across every connected account, filters by importance heuristics, and replies with a prioritised list plus suggested action for each.

**Launch graphic + caption + scheduled post**: the operator asks the Copywriter "draft a LinkedIn launch post for our new pricing page." The agent emits a caption `[[ARTIFACT]]` and a `[[IMAGE prompt="..." aspect="1.91:1"]]` block in the same reply. The operator schedules the post for Tuesday 9 AM. APScheduler publishes it automatically.

**Customer-success retros**: the Ops Manager agent reads the last week of customer support tickets via the connected Help Scout / Intercom integration, identifies recurring issues, and writes a weekly retro document. The operator reads it Monday morning over coffee.

**Brand audit before a redesign**: the operator runs Business Audit on their own site. The output goes to the designer. The designer's redesign brief writes itself.

---

## 20. Pricing and Plans

Foundry's pricing is published live on [foundryagents.ai/pricing](https://foundryagents.ai). The plan tiers are:

- **Free** — try the platform. One org, one team, modest spend allowance. Card not required.
- **Starter** — solo founder. One org, full agent catalogue, modest monthly spend allowance, no publishing connections.
- **Growth** — small team. Multiple orgs, multiple seats, generous monthly spend, live social publishing on the major platforms.
- **Agency** — multi-brand operator. Unlimited orgs, all Composio integrations, white-label audit PDFs (in progress), priority support.
- **Enterprise** — bespoke. Volume pricing, SSO (roadmap), dedicated success manager, contract terms.

Plans renew monthly or annually. Annual carries a discount that's published on the pricing page.

The overage model is explicit: each plan carries a base monthly LLM allowance; spend above the allowance is metered at a per-1k-token rate published on the pricing page. The Spend Dashboard surfaces overage status in real time. Operators can set a hard cap; the platform refuses to spend past it.

There are no per-agent fees, no per-conversation fees, and no surprise add-ons.

---

## 21. Roadmap and Open Questions

The committed roadmap (P0, currently shipping or in progress):

- **Stripe checkout for plan upgrades + add-ons** (currently soft-gated).
- **Monthly overage invoice aggregation job** (currently shown on the dashboard; not yet automatically invoiced).
- **Forge Crawl Phase B/C** — full-site BFS crawl, sitemap.xml priming, robots.txt politeness layer.

The P1 roadmap (queued, not yet started):

- **White-label Audit Mode** — agencies can ship audits with their own branding and PDF cover.
- **Crucible Auth Handoff** — pause a mission, hand the browser to the user, resume.
- **Custom Mobile Widget** — Apple Shortcuts / Tasker API for surfacing live Foundry stats on a phone home screen.
- **`server.py` modular split** — internal tech-debt task; transparent to users.

The P2/P3 backlog includes voice TTS on outbound proactive nudges, OAuth re-authentication flows for long-lived integrations, advanced cost-attribution analytics, and a dedicated code-health sprint.

Open architectural questions the team is actively working through:

- **Long-term memory vs context window**. Today the SoulPrint and per-conversation history fit in the context window comfortably. As conversations grow long, summarisation strategies will matter.
- **Vector retrieval inside Documents library**. Today retrieval is keyword-based with sensible defaults. Embedding-based retrieval is on the table once the corpus per-org reliably exceeds a few thousand documents.
- **Multi-model routing**. Today GPT-4.1-mini is the default for most paths. The platform is model-agnostic; we expect to route some workloads to Claude Sonnet or Gemini Pro as their cost/capability curves shift.

---

## 22. Getting Started

The fastest path from "signed up" to "shipped a real deliverable":

1. **[Sign up](https://foundryagents.ai)** — no card required for the trial.
2. **Create your first org.** Name it after your business or your first client brand.
3. **Tell us your SoulPrint** — mission, tone, audience, values, north star. Five fields, five minutes.
4. **Pick an agent.** The catalogue is searchable; pick one whose role matches the work you want done.
5. **Have a real conversation.** Bring a real brief — a customer email you're stuck on, a social post you're putting off, a research task you've been dreading.
6. **Connect Telegram** in `Account → Telegram` for cross-org concierge access from anywhere.
7. **Connect one Composio toolkit** — Gmail is usually the most-valuable first connection. Google Calendar second.

Most operators ship their first real deliverable inside fifteen minutes. The platform's onboarding flow is designed to reach that fifteen-minute mark by default.

If your use case has a wrinkle the platform doesn't obviously cover, reach out via the contact form on the landing page. We read every message.

---

*Foundry Agents · Built with FastAPI, React, MongoDB, Composio, OpenAI, Anthropic, Google, and a deep refusal to ship AI tools that don't actually finish the job.*

*Live at [foundryagents.ai](https://foundryagents.ai). For partnership / agency white-label / enterprise: see the landing page.*
