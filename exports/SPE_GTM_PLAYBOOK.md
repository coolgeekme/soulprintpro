# SoulPrint Engine — Foundry Go-to-Market Playbook

> Six-week playbook for using Foundry to market SoulPrint Engine (SPE Pro).
> Every step maps to a specific Foundry surface. If a step feels abstract,
> that's a bug — flag it and we'll tighten it.

---

## Phase 1 — Foundation (Day 1-2, ~2 hours total)

### 1.1 Org setup — get the "Soul" right FIRST

Everything downstream reads from Soul. Don't skip this.

**Where**: `/app/org/new` → then Org page → **Edit Soul**.

Fill each field with LITERAL sentences (not marketing prose):

| Field | For SoulPrint Engine, write… |
|-------|------------------------------|
| **Name** | SoulPrint Engine Pro |
| **Website** | https://soulprintengine.ai |
| **Mission** | Give every business owner a full agency in one login — 150+ specialised agents that remember your brand voice, run real work, and ship deliverables — not summaries. |
| **Audience** | Founders and marketers at 1-50 person B2B SaaS or service businesses who need an agency's output but can't afford agency prices. |
| **Tone** | Direct, confident, technical-but-warm. No "delight." No "revolutionise." Yes to specifics, numbers, and named features. |
| **Values** | Every reply IS the work. Ship the file. Grounded in reality (agents cite what they see). |
| **Anti-values (what we're NOT)** | Not an AI chatbot. Not a "co-pilot." Not a wrapper around ChatGPT. |
| **Differentiators** | Real Playwright browser missions (Crucible), 150+ prompt-engineered agents, Soul memory across every agent, Composio integration, audit-as-lead-magnet. |
| **North-star metric** | Paid orgs (Spark → Temper → Refine tier ladder). |

**Then click "Regenerate Soul from website"** — the platform will crawl soulprintengine.ai, distill an even sharper Soul, and every agent (all 150+) reads it before speaking.

### 1.2 Brand assets

**Where**: Org page → **Brand Assets panel** (owner-only).

- Upload the SoulPrint logo PNG (light + dark variants if you have them).
- Set primary color: `#F97316` (the fire-orange badge accent).
- Tagline: `Powered by SoulPrint Engine` — this gets stamped on every audit PDF, every social cover, every mission brief.
- Visual style preset: `Editorial · high contrast · minimal decoration` (matches the sharp positioning).

### 1.3 Style guide — teach the platform your voice

**Where**: Org page → **Style Guide panel** → "Synthesize from connected profiles."

Connect Twitter/X + LinkedIn if you have live accounts. If not, paste 5-8 of your best-performing posts into the manual override. The Style Guide gets injected into every social-caption LLM call — this is what makes drafts SOUND like you, not like GPT.

### 1.4 Connections (Composio) — critical for real work

**Where**: Org page → **Connections panel**.

Connect at minimum:
- **Gmail** — the CoS agent can email drafts, prospects, weekly summaries.
- **Google Docs** — long-form deliverables land here.
- **LinkedIn** (via Composio) — for posting + reading company/competitor pages.
- **Twitter/X** — same.
- **Notion or Slack** — wherever your team lives.

Once connected, every mission gets these tools automatically (see the "Tools your mission can use" preview in the mission dialog).

---

## Phase 2 — Team structure (Day 2)

Build TWO teams — do not build one giant catch-all. Foundry's power comes from specialised teams with focused agents.

### Team A: **"SPE Growth"** (marketing + demand-gen)

Add these agents (Team page → **Add agents**):

| Agent | Division | Why for you |
|-------|----------|-------------|
| Marketing Social Media Strategist | Marketing | Drafts platform-tuned posts from your Soul |
| Conversion Copywriter | Marketing | Rewrites your landing hero, CTA copy, email sequences |
| SEO Strategist | Marketing | Attacks "AI agent platform" / "agency alternative" keywords |
| Content Strategist | Marketing | Weekly editorial calendar |
| Competitive Researcher | Research | Weekly reads on Lindy, Cassidy, Relevance AI, Sider, Adept |
| Chief of Staff | Executive | Runs your Monday briefing via Telegram + prioritises the week |
| Marketing SEO Specialist | Marketing | Handles the audits-as-lead-magnet flywheel |

### Team B: **"SPE Product"** (product + customer)

| Agent | Division | Why for you |
|-------|----------|-------------|
| Product Manager | Product | Weekly product-council writeups |
| Customer Insights Analyst | Product | Distills user feedback / audit lead notes |
| Documentation Writer | Engineering | Turns Loom videos into help-doc pages |
| Onboarding Copywriter | Product | Rewrites the auth → first-mission flow |
| Reviews Therapist | Customer | Drafts G2 / ProductHunt / Reddit response |

**Why two teams**: it isolates memory. Growth agents don't need to know about a Postgres migration; Product agents don't need to know about last week's tweet.

---

## Phase 3 — Prospecting flywheel (Week 1)

This is where SPE differs from every other SaaS marketing playbook: **your audit generator is your best lead magnet**. Run it on 30-50 target accounts in your ICP.

### 3.1 Build a target list (30 min)

Ask the **Competitive Researcher** in the Growth team:

> Give me 40 B2B SaaS companies (50-500 employees) in the marketing-tech / sales-tech space that (a) have an active blog updated in the last 30 days, (b) are visibly hiring content or marketing roles, and (c) don't currently show an AI-agent-platform partnership. Return as a table with company, website, LinkedIn, why they're a fit, and their weakest visible marketing signal.

You'll get a real spreadsheet back (`.xlsx`). Save it to Assets → tagged "prospect-round-1."

### 3.2 Generate audits at scale (Admin dashboard → Audits tab)

For each row in your prospect list:
1. Admin → **Audits** → **New audit** → paste website URL.
2. Platform runs a ~90-second background job: crawls site, searches reviews, competitive read, LLM synthesis, PDF render, GridFS mirror.
3. You get a shareable `foundryagents.ai/audit/{slug}` gated landing page + PDF.

**Every audit already ends with "How SoulPrint Engine Pro executes each recommendation"** — that's your soft close.

### 3.3 Warm outreach

Give each audit link to a **cold-email drafter** (Conversion Copywriter agent):

> Draft a 4-line cold email to [Name] at [Company]. They're the [Role]. I just ran a Signal Audit on their site — top 3 findings were [paste]. Their score was [X/100]. Lead with ONE specific observation from the audit (not a compliment). End with a soft CTA to the audit link. No "hope this finds you well." No emojis.

Send those manually (or automate via Gmail Composio tool + a Recurring Mission — see 5.2).

**Reasoning**: an audit is a $10k McKinsey-style deliverable that costs you 30 seconds of LLM. Prospects who read it are 5-10x more qualified than a cold email opener because they've already seen SPE's execution quality.

---

## Phase 4 — Social Queue rhythm (Week 1 onwards)

### 4.1 Weekly drafting cadence

**Every Monday morning**, open the Social Queue → compose form and run these THREE prompts (creates ~9 posts, ~30 min of your time):

**Prompt 1 — Founder POV**:
> Draft 3 LinkedIn posts + 3 Twitter posts about the following pattern I keep seeing at SaaS companies: [1 specific observation from last week's audits — e.g., "founder-led SaaS with 50 employees and no marketing team; the blog is dead; SEO ranks page 3 for their own brand"]. Lead with the observation as a punchy hook, twist it into what SPE Pro would ship in the next 24 hours, and end with a link to the audit landing page (no CTA-speak).

**Prompt 2 — Product-in-motion**:
> Draft 3 posts (Twitter, LinkedIn, Threads) about a single feature I shipped this week: [feature]. Show, don't tell — write it as "here's what happened when someone used it" with a specific outcome. Attach a screenshot from Assets.

**Prompt 3 — Audit-as-lead-magnet**:
> Draft 3 posts inviting people to request a free Signal Audit. Do NOT sell SPE Pro directly. Sell the audit: "we look at your website, socials, reviews, and competitive position; you get a 20-page PDF; costs you nothing." Link to a request form (or your DMs). One post per platform: LinkedIn (professional), Twitter (punchy), Threads (conversational).

### 4.2 The "Your image → caption" flow

If you shot a Loom, a screen recording, or grabbed a screenshot of the product this week:
1. Social Queue → compose → toggle "Your image → caption"
2. Drop the image, add a 1-line brief like *"Show off the agent switching mid-conversation"*
3. Platform writes captions tuned per platform, saves the image to Asset Library, drafts land in Queue.

### 4.3 What NOT to post

Turn OFF auto-post for the first 30 days. Review every draft manually — you're teaching the platform your voice. Edit ruthlessly the first 2 weeks; by week 3 the drafts land 90% shippable.

---

## Phase 5 — Missions (Week 1 onwards, weekly)

Missions are asynchronous multi-step jobs. Use them for anything that takes >5 min of manual work.

### 5.1 Monday mission — "This week's briefing"

**Lead agent**: Chief of Staff.
**Goal**:
> Read the org's Documents from the last 7 days (audit reports, social drafts, meeting notes). Pull the top 5 competitive news items from Brave/Perplexity about Lindy, Cassidy, Relevance AI, Sider, and Adept. Deliver a 1-page Word doc briefing: "What shipped, what's shipping this week, what competitors did, and 3 asks for me."

Runs in ~2-3 minutes. Lands as a `.docx` in the chat + Telegram if CoS is connected.

### 5.2 Recurring outbound mission (roadmap — P1 backlog)

*Not shipped yet — flagged as P1.* Once shipped:
> Every Monday at 8am, pull 10 new prospects from my "prospect-round-2" list, run audits, and email a personalised opener via Gmail. Copy me on each email as it goes out.

### 5.3 "SEO content sprint" mission

**Lead agent**: SEO Strategist.
**Goal**:
> Identify 5 long-tail keywords in the "AI agent platform" cluster with monthly volume 200-2,000 and difficulty <30. For each, draft an 800-word blog post targeting it. Deliver as 5 separate .md files + a summary PDF ranking them by ease-of-win.

Run once a month. You publish 4-5 posts a month → 20-25 pages of ranking real estate a quarter.

### 5.4 "Competitor teardown" mission

**Lead agent**: Competitive Researcher.
**Goal**:
> Do a deep teardown of Lindy's homepage, pricing page, and top 3 blog posts. Where do they position? Who do they claim to help? What do they charge? Where are they weak vs. SoulPrint Engine Pro? Deliver a PDF with a side-by-side positioning table + 5 opportunities we can exploit in our messaging.

Run monthly on rotating competitors. Feeds directly into your next batch of social drafts.

---

## Phase 6 — Metrics that matter (Week 2 onwards)

Foundry surfaces these; check weekly.

| Metric | Where | Target (Month 1) |
|--------|-------|------------------|
| **Audits generated** | Admin → Audits tab | 30+ |
| **Lead capture rate** | Admin → Leads modal | ≥20% of audit views |
| **Social drafts shipped** | Social Queue → Published tab | 30+ (1/day) |
| **Missions completed** | Missions page | 8-10 |
| **Trial codes issued** | Admin → Trial Codes | 15+ |
| **Trial → Paid conversion** | Admin → Billing | 20-30% |

If any of these fall below target for 2 weeks, ask the CoS: *"Why are we below target on [X]? Give me 3 hypotheses and one action item for each."*

---

## Phase 7 — Compounding (Month 2+)

The playbook above is Month 1. Compounding starts when:

1. **Every audit you ship becomes a case study**. The Reviews Therapist agent turns them into "here's what we found for company X" LinkedIn posts (redacted where sensitive).

2. **Your Asset Library becomes a brand moat**. Every social image, every screenshot, every diagram lives there. Every new agent reads from it. The 100th post is 10x easier than the 1st.

3. **Custom Agents** for your product. When you notice yourself asking the same question weekly ("what did competitors ship?"), use the **Custom Agent Builder** to make a "Weekly Competitor Scan" agent that runs it automatically.

4. **The audit funnel is your primary paid-sub driver**. Every audit ends with "How SoulPrint Engine Pro executes this" → trial code CTA. Track cohort conversion by audit source (LinkedIn DM vs. cold email vs. inbound).

---

## The one-sentence version

> **Setup Soul once, connect Composio, build two focused teams, run 40 audits in Week 1 as your lead magnet, ship 3 social posts a day drafted from your Soul + Style Guide, and let the audit → trial CTA do the paid conversion for you.**

The whole playbook is designed to compound: every mission's output becomes tomorrow's context; every audit becomes case-study fodder; every social post teaches the platform your voice. Month 1 you're setting stones. Month 3 you're pouring concrete.

---

*Ship the file. Then ship the next one.*
