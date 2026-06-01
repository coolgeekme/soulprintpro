# 🔧 Foundry — What We've Built So Far

Plain-English feature inventory of **Foundry by SoulPrint Engine** for non-technical teammates. Each section is a category of capabilities and what they do for users.

---
## ✍️ Feb 20, 2026 — Social Queue draft: resilience hardening (random failures fix)
**Production bug**: users got intermittent "Failed to generate" toasts when generating posts in the Social Queue. Reported as random / non-reproducible — which matches a **transient LLM provider hiccup** profile (OpenAI 5xx, rate-limit, or a slow run that drifts past the production-ingress proxy timeout).

Root cause: `_draft_via_agent` in `social_posts.py` was a **single-shot** call to `gpt-4.1-mini` with NO timeout guard, NO retry, and NO fallback model. Every transient failure became a user-visible 502.

**Three-layer fix**:

1. **Per-attempt timeout** — every LLM call now wrapped in `asyncio.wait_for(chat.send_message(...), timeout=...)`. Times out at 50s (primary) / 35s (fallback) — well inside the production-ingress 60-100s window so we fail-fast with a precise error before the request dies at the proxy.

2. **`_MODEL_ATTEMPTS` retry list** — three attempts in order:
   - `gpt-4.1-mini` 50s (primary)
   - `gpt-4.1-mini` 50s (immediate retry — most transient OpenAI hiccups clear in <2s)
   - `gpt-4.1-nano` 35s (cheaper + faster fallback — handles cases where the mini model is degraded provider-side)

3. **Status code 503 (not 502)** on exhausted retries — the FE has a one-shot `attemptDraft(attempt=2)` retry-on-503 loop in `SocialPage.js` that adds yet another silent retry from the client side after a 1.5s back-off. So a single transient hiccup now gets up to **6 chances** (3 backend × 2 frontend) to succeed before the user even sees a toast.

4. **Friendlier error copy** — `TimeoutError` surfaces as `"Timeout — try again in a few seconds; the AI provider was slow."` distinct from a real LLM error (`"AI provider hiccup: <exc>"`). Empty-body case explicitly says "try again with a more specific prompt".

**Tests** — 5 new pytest cases in `tests/test_social_draft_resilience.py` (timeout literal exists, multi-model attempts, 503 status, TimeoutError handling, route uses 503 not 502). 15 image+draft resilience tests pass total.

**Production action**: redeploy. After deploy, the "Failed to generate" frequency should drop near-zero. The few cases where ALL 6 attempts fail will show a precise, actionable error toast.

---

## 🎨 Feb 19, 2026 — Post image generation: background-task pattern
**Production bug** (Maradyne org, on top of yesterday's remix fix): clicking Regenerate on a post copy showed "Generation failed". Same root cause as the remix network-error bug — `/social/posts/{id}/generate-image` was blocking on Nano Banana for 30-90s, exceeding production-ingress proxy timeouts (60-100s).

**Fix** (`/app/backend/social_posts.py`):

1. **`/generate-image` returns `status_code=202`** with `image_status="pending"` after persisting the user's prompt synchronously. No more inline blocking.

2. **`_run_generate_bg` coroutine** runs Nano Banana + logo composite + DB update in the background. Kicked off with `asyncio.create_task(_run_generate_bg())`.

3. **Terminal status flips** — `image_status="ready"` + image dict on success, `image_status="error"` + `image_error="Generation failed — tap Regenerate to retry."` on failure.

4. **Frontend updates** (`SocialPage.js`):
   - `generate()` interprets 202 + pending → "Generating image — 30-90s…" toast (vs the legacy "Image generated" toast which fired before the image actually existed).
   - **Polling loop cap raised 90s → 180s** (23 → 45 ticks at 4s) — Nano Banana image-to-image takes 30-90s and the prior cap left spinners stuck on the slow path.
   - Polling loop now **surfaces error toasts** when posts land in `image_status="error"` after polling completes — without this users stared at a now-still card wondering what happened.

**Tests** — 5 new pytest cases in `tests/test_generate_image_async.py` mirroring the remix-async test pattern (status_code=202, create_task usage, pending-before-task ordering, sync prompt persist, error-status on failure). All 10 image-async tests pass (5 remix + 5 generate).

**Production action**: redeploy. After deploy, clicking Generate / Regenerate on a post:
1. Modal closes / Edit-prompt collapses immediately with a "Generating image — 30-90s" toast.
2. Card shows a spinner (existing pending-state UI).
3. Polling swaps in the finished image when Nano Banana completes (typically 45-75s).
4. On failure, the spinner clears + a precise error toast appears + the card surfaces `image_error` text.

---

## 🎨 Feb 19, 2026 — Asset remix: background-task pattern (network-error fix)
**Production bug**: users got a "network error" when remixing an uploaded image asset in an org. Root cause: the `/social/posts/{id}/remix-asset` endpoint called `generate_image_via_nano_banana(...)` **synchronously** inside the request handler. Nano Banana image-to-image takes 30-90 seconds depending on prompt complexity. Production-ingress proxies (Cloudflare / k8s ingress) typically cut connections at 60-100s, so the HTTP request died mid-generation and the browser surfaced the generic axios "network error".

**Fix** (`/app/backend/social_posts.py`):

1. **Route now returns `status_code=202`** immediately after persisting `image_status="pending"` + `image_prompt` synchronously. No more blocking on Nano Banana.

2. **`_run_remix_bg` coroutine** holds the full pipeline: prompt assembly → Nano Banana call → logo compositor → final post update. Kicked off with `asyncio.create_task(_run_remix_bg())` (same pattern already used elsewhere in `social_posts.py` for `publish_post`).

3. **Terminal status flips** on completion (`image_status="ready"` + image dict) or failure (`image_status="error"` + `image_error="Remix failed — try a different prompt."`). The SocialPage's existing `image_status === "pending"` polling loop picks up both outcomes — no FE plumbing changes needed.

4. **AssetPickerModal copy update** — toast switches from "Remix done" to "Remixing your photo — this can take 30-90 seconds." when the response comes back in pending state, so users know the spinner they're about to see on the post is normal.

**Tests** — 5 new pytest cases in `tests/test_remix_async.py` using AST-style code inspection (fast + no router-init boilerplate): status_code=202 decorator, asyncio.create_task usage, pending-status-before-task ordering, synchronous prompt persistence, terminal error-status on failure. All pass.

**Production action**: redeploy. After deploy, remixing an asset will:
- Close the AssetPickerModal immediately
- Show "Remixing your photo… 30-90 seconds" toast
- The post card shows a spinner (existing pending-state UI)
- Polling loop swaps in the new image when Nano Banana finishes

---

## 🐛 Feb 19, 2026 — Composio: naive-datetime TypeError root-cause fix
**Production bug**: after the previous fix shipped, users saw the new "Couldn't load your existing Composio connections" inline banner on every single Connections-panel load. Banner was working as designed — but it was masking a **persistent backend crash**, not a transient hiccup. `list_connections` was crashing 100% of the time for orgs that had any pre-existing connection row.

**Real root cause**: MongoDB returns BSON datetimes as **naive** Python `datetime` objects by default (Motor doesn't enable `tz_aware=True`). All our writers use `datetime.now(timezone.utc)` (aware), so the DB write is correct UTC. But on read, the value comes back without tzinfo. Then:
```python
now = datetime.now(timezone.utc)            # aware
(now - r["initiated_at"]).total_seconds()   # TypeError: can't subtract offset-naive and offset-aware datetimes
```
This crashed every iteration of the auto-refresh loop in `list_connections`, which propagated up and triggered the soft-error banner. **It was 100% reproducible** for any org that had a row in `composio_connections`.

**Fix** (`composio_integration.py`):

1. **`_ensure_aware(dt)` helper** — coerces naive datetimes to UTC-aware (treats them as UTC since every writer uses `datetime.now(timezone.utc)`). Returns None on None input, passes through already-aware values.

2. **Applied at every subtraction point**:
   - `list_connections` auto-refresh loop (`now - ini` → `now - _ensure_aware(ini)`)
   - `list_connections` `pending_age_seconds` stamp
   - `list_connections` ISO-serialization loop
   - `refresh_connection_status` STUCK-detection age calculation

3. **Per-row try/except in the hydration loop** — if a single connection row throws (corrupted field, broken Composio metadata), the row falls back to its minimum surfacing without taking the whole list down. Bad row logged with `connection_id + toolkit + exc` for diagnosis.

**Tests** — 3 new pytest cases in `tests/test_composio_naive_datetime.py`:
- `test_list_connections_handles_naive_datetimes_from_mongo` — reproduces the exact production crash with naive datetimes
- `test_one_malformed_row_does_not_tank_the_whole_list` — confirms per-row try/except isolation
- `test_ensure_aware_handles_none_and_aware_inputs` — helper unit tests

All 19 Composio-resilience tests pass.

**Production action**: redeploy. After deploy → Connections panel will load correctly for every existing org. The soft-error banner from yesterday is still there as a safety net but should never fire under normal operation now.

---

## 🔌 Feb 19, 2026 — Connections panel: catalog-always + IG target-picker softening
**Production bugs reported** (foundryagents.ai, on top of yesterday's STUCK fix):
  1. "Composio integration list no longer showing up on main Org page" — the **toolkit catalog** (Gmail / Slack / FB / etc. + Add cards) blanked out, leaving only the intro copy. Root cause: `composio_list_toolkits` returned `await list_connections(...)` inline, so a single transient Composio 5xx or a malformed row crashed the WHOLE endpoint, leaving the FE with no toolkits to render.
  2. "Couldn't load your Facebook Pages — Connect Facebook for this org first" modal popping when clicking **Select target** on an IG row, even when IG was connected and granted Page permissions. Root cause: the route hard-errored before even *trying* the FB-list-pages call if no separate FB row existed.

**Fixes**:

1. **Catalog-always resilience** in `composio_list_toolkits` — `list_connections` now runs inside try/except. On failure we return featured toolkits + an empty connections list + a `connections_error: "Couldn't load your existing Composio connections (transient API hiccup)…"` flag. `+ Add` grid never blanks out again.

2. **Inline soft-error banner** in `ConnectionsPanel.js` — `connectionsError` state renders an amber `AlertTriangle` banner with a `Retry now` link button right above the toolkit grid. Far better UX than the previous "toast fires + panel goes blank" failure mode.

3. **Hard-fail toast also preserves state** — if the WHOLE endpoint 500s, the catch branch no longer wipes the previously-loaded toolkits; the panel stays usable, the banner says "Couldn't reach the integrations service".

4. **Loosened IG → FB target lookup** in `composio_available_targets`:
   - If toolkit is `instagram` AND no ACTIVE FB row exists, we now **try the FB_LIST_MANAGED_PAGES call against the IG row's token first** — Composio routes Meta tools by user, not by toolkit row, so an IG OAuth grant with `pages_show_list` permission can list Pages without a separate FB connection.
   - Only AFTER the IG-path try comes back with 0 Pages do we surface the "connect FB" nudge — and the error copy explicitly explains "either the IG OAuth didn't include Manage Pages, or you have no FB Pages linked to this IG Business account" + the actionable fix.
   - Removed the dead-code duplicate exception handler that ruff was flagging.

**Tests** — 3 new pytest cases in `tests/test_composio_toolkits_resilience.py`: endpoint-returns-catalog-when-connections-blows-up, clean-path-omits-error-flag, composio-unavailable-returns-empty-safely. All 16 Composio-related tests pass. Backend boots clean.

**Production action**: redeploy. After deploy:
- The "+ Add" grid will reappear immediately on the Org page even if FB is still in STUCK state.
- Clicking **Select target** on the IG row should now either show the Page picker directly (IG token has Page perms) OR surface a precise actionable error (IG token doesn't — add FB).

---

## 🔌 Feb 19, 2026 — Composio Connections: zombie-row recovery (round 2)
**Production bug, reported twice on foundryagents.ai**: Facebook connection sat on "Connecting…" forever even after round-1's auto-refresh + Cancel button fix. Root cause: when Composio kept returning `INITIATED` past the OAuth grant deadline (popup closed early, Meta never POSTed the callback, etc.), every refresh just persisted "still INITIATED" with no terminal state — so the row became a permanent zombie that needed manual deletion before "+ Add" worked again.

**Two-layer fix shipped**:

1. **Zombie prune on `initiate_connection`** — when the user clicks "+ Add Facebook" after a stuck attempt, the backend `$delete_many`'s any other `INITIATED` or `STUCK` row for the same `(composio_user_id, toolkit)` tuple that's >120s old before minting the fresh pending row. **No more "click Cancel first, then Add" two-step dance** — `+ Add` always starts clean.

2. **`STUCK` terminal status on `refresh_connection_status`** — `refresh_attempts` counter tracks how many times we've polled Composio for the same row. When `attempts >= 3` AND `age >= 5 min` AND Composio still says `INITIATED`, we flip the row to `STUCK` ourselves. The FE then shows a red "Connection failed — Retry" badge instead of a misleading spinner forever.

3. **Composio 5xx tolerance** — `connected_accounts.get()` exceptions are now caught + logged (warning, not exception). Returns the existing row's snapshot so the panel doesn't 500 when Composio's API has a transient hiccup.

4. **One-click Retry on STUCK rows** (`ConnectionsPanel.js`) — `handleRetryStuck` deletes the failed row + immediately fires a fresh OAuth flow for the same toolkit, preserving the user's nickname. Production-grade self-healing instead of manual cleanup.

5. **`STUCK` rows participate in pending-age stamping** so the FE can show "failed 8 min ago" copy.

**Tests** — 7 new pytest cases in `tests/test_composio_stuck_recovery.py`: prune-stale-FB, preserve-IG-active, preserve-other-user, prune-STUCK-too, mark-zombie-as-STUCK, don't-mark-young-rows-STUCK, Composio-5xx-tolerance, STUCK-age-stamp. All 13 Composio-recovery tests pass.

**Bonus — answered the user's Meta strategy question**: granting "all current + future Pages" on each org's OAuth flow IS the correct strategy. Foundry's per-org `Target Picker` scopes the actual publish to a single Page; even though every token has visibility to all your Pages, Foundry never auto-posts — the user has to pick a target per org. Locking the grant to a single Page would force a re-grant on every new Page addition.

---

## 🏗️ Feb 18, 2026 — Lead-hunt missions: kitchen-sink fix (round 2)
**Production bug (reported twice on foundryagents.ai)**: a "harvest verified business leads across Phoenix/Austin/Atlanta/Baltimore/Miami" mission shipped a CSV with **1 data row** even after the round-1 fix (csv_append_rows + 15-row gate + DATA-HARVEST recipe + drafter upgrade). Root cause: the recipe pointed the agent at Chamber of Commerce / BBB / Yelp / LinkedIn — all Cloudflare-protected — so every `web_scrape` returned empty, the agent had no rows to push to `csv_append_rows`, and the one-shot finish gate let it through after a single nudge.

**Kitchen-sink fix (4 components, all shipped)**:

1. **`public_data_sources.py`** — new module wrapping 4 real JSON APIs that don't require scraping or blocking-prone OAuth:
   - `search_sec_edgar(query, state=None)` — SEC EDGAR full-text via efts.sec.gov. Verified live: 10 rows per call for "healthcare" / "consulting". Returns company name + state + SIC-mapped industry + canonical SEC company-page URL.
   - `search_usaspending(state, naics_keyword=None)` — USAspending.gov autocomplete/recipient endpoint. Verified live: 10 rows per state × keyword combo (AZ healthcare, TX technology, GA finance, MD consulting, FL marketing). Falls back to the state's full name as `search_text` when no keyword supplied (API requires non-empty). Now uses Foundry's standards-compliant Wikipedia/SEC-friendly User-Agent.
   - `search_wikipedia_companies(query)` — Wikipedia full-text `list=search` (NOT opensearch, which only matches title prefixes and missed the canonical articles). Picks the first hit titled "List of companies/corporations …" containing a query token. Parses BOTH wikitable rows (Phoenix, Miami) AND bullet-list `<li><a title=>` flavours (Austin, Atlanta) — scoped strictly to `mw-parser-output` so navigation chrome doesn't leak in. Verified live: 20 rows for Phoenix/Austin/Atlanta/Miami; Baltimore returns 0 (article format mismatch — acceptable, 4/5 metros work).
   - `search_opencorporates(jurisdiction, q)` — registered for forward-compat; fails soft with a hint pointing the agent to the other 3 sources because OC's free tier now requires an API key.

2. **4 new mission tools** wrapping the helpers (in `missions.py`'s `build_tools`): `sec_edgar_search`, `usaspending_recipients`, `wikipedia_companies_list`, `opencorporates_search`. All return rows pre-shaped for the standard lead-hunt CSV columns so the agent can push the array straight into `csv_append_rows`.

3. **Multi-attempt dataset gate** — `dataset_gate_warned` boolean replaced with `dataset_gate_warn_count`. Gate fires up to `DATASET_GATE_MAX_NUDGES=3` times with escalating urgency text ("FINISH BLOCKED" → "SECOND WARNING" → "FINAL WARNING — after this finish goes through but the mission will be marked PARTIAL"). After the cap, finish is allowed but `partial_reason` is stamped on the mission doc. Fixes the round-1 issue where the agent ignored one nudge and re-called finish.

4. **Hardened lead-mission system prompt** (`_build_lead_mission_system_prompt` in `server.py`) — dropped the broken aggregator list (Chamber/BBB/Yelp/LinkedIn) and replaced with explicit "START WITH THE JSON-API TOOLS, NOT WEB SCRAPING" mandate plus the 4-step per-metro loop using the new tools. Tells the agent point-blank: 2 metros of JSON-API calls already clears the 15-row gate. Stops the doom-loop on Cloudflare-blocked aggregators.

**Tests** — 16 new pytest cases in `tests/test_public_data_sources.py` covering shape conformance, state filtering, address parsing, SIC mapping, both Wikipedia formats (table + bullet list), and irrelevant-hit rejection. All 57 lead-hunt-related tests pass; backend reboots clean.

**Production impact**: for the user's exact mission goal (verified live):
- SEC EDGAR healthcare: 10 rows · consulting: 10 rows
- Wikipedia: 20 rows × 4 of 5 metros = 80 rows
- USAspending: 10 rows × 5 states × multiple keywords = 50+ rows
- **Realistic harvest: 180+ rows from 3 free JSON APIs, NO scraping.**

---

## 🔌 Feb 18, 2026 — Composio Connections: stuck-INITIATED UX
**Production bug (foundryagents.ai)**: Facebook "+ Add" → OAuth popup completed → UI stuck on "Connecting…" for 30+ minutes with no Page picker shown. Root cause: post-callback poll window was only 30s (10 attempts × 3s) and `list_connections` returned cached statuses — when Composio's INITIATED → ACTIVE webhook lagged >30s (common for Meta), the row was orphaned in the DB and the UI had no way out. Three-part fix:

1. **Backend auto-refresh on `list_connections`** (`composio_integration.py`) — any INITIATED row older than `_STALE_INITIATED_SECONDS = 60` triggers an inline `refresh_connection_status` call against Composio. Capped at 5 per list call so a user with many stale rows can't fanout 20 outbound API hits. Refresh failures are swallowed (we'd rather return stale data than 502 the panel). Re-reads the rows after the refresh wave so the freshly-flipped statuses make the response.

2. **`pending_age_seconds` stamp** — every INITIATED row in the API response now carries the age in seconds. The frontend uses it to render `Connecting… (3 min)` (graceful: seconds → minutes → hours) and, after 90s pending, shows an amber sub-label nudging the user to Cancel + reconnect.

3. **Frontend UX hardening** (`ConnectionsPanel.js`):
   - Post-callback poll window doubled: 30s → **60s** (20 × 3s). Most Meta OAuth round-trips resolve well inside that.
   - On poll exhaust, a `toast.warning` fires telling the user exactly what to do: "Click 'Check' to retry, or 'Cancel' to start over." No more silent failure.
   - New explicit **"Cancel"** text button next to "Check" on every INITIATED row, with a confirmation worded for the pending-OAuth mental model: "Cancel this pending connection and start over? Your existing connected accounts won't be affected." Distinct from the disconnect-active trash icon so users don't fear losing already-active connections.
   - `handleRefresh` now surfaces the Composio response: ACTIVE → "Connection verified — you're good to go", still-INITIATED → "Still pending on Composio's side", anything else → "Click Cancel and reconnect". Stops the click-Check-and-nothing-happens dead end.

**Tests** — 7 new pytest cases in `tests/test_composio_pending_refresh.py`: pending-age stamp on/off, stale row triggers refresh, fresh row skipped, 5-row cap, refresh failure non-crashing, constant sanity. All pass.

---

## 📊 Feb 18, 2026 — Mission productivity: dataset / lead-hunt missions
**Production bug**: a "harvest a verified business-lead dataset across 5 metros (Phoenix/Austin/Atlanta/Baltimore/Miami)" mission shipped a methodology PDF with ZERO rows. The lead agent treated the dataset goal like a "report" goal, did one round of web_search, and called `finish` with an outline PDF. Four-part fix lands in this session:

1. **`csv_append_rows` mission tool** (`missions.py`) — incremental CSV builder. First call declares `columns`; subsequent calls only need `name` + `rows` (list of dicts or list of lists). State persisted in `missions.csv_workspace` as an array of `{name, columns, rows, updated_at}` slots (array — NOT dotted-key dict — because Mongo treats `.` in keys as path nesting; a filename like `phoenix_leads.csv` would otherwise be split). Each call regenerates the CSV bytes, `$pull`s any prior artifact entry for the same `original_name` from `mission.artifacts`, deletes the prior `documents` row, and saves a fresh one — so the library shows ONE growing file, not a parade of revisions. Caps: 25 rows/call, 500 rows total. Stamps `csv_row_count` on the returned artifact dict so the finish gate can read it.

2. **Dataset-row finish gate** in `_run_mission_loop` — when `_is_dataset_goal(goal)` is true and the largest `.csv` artifact has fewer than `DATASET_MIN_ROWS=15` data rows, `finish` is blocked once with a "GO HARVEST MORE" nudge naming specific FREE public aggregator sources (Chamber of Commerce, BBB, Yelp, Google Maps, SEC EDGAR, OpenCorporates, USAspending.gov, state SoS, industry associations). Same one-shot pattern as the existing research-breadth gate (sets `dataset_gate_warned` so the agent gets a second chance after harvesting more).

3. **DATA-HARVEST / LEAD-HUNT RECIPE** in `_build_lead_mission_system_prompt` (`server.py`) — when the goal matches dataset intent, the agent is told the deliverable IS A CSV (not a PDF), with the exact `web_search → web_scrape/web_extract → csv_append_rows → repeat` loop pattern, a curated list of 8 FREE public sources by category, default columns for B2B lead-hunts, and a "do NOT fabricate phone/email/revenue figures — truthful blank beats hallucinated number" guardrail.

4. **Drafter upgrade** in `_DRAFTER_SYSTEM` (`missions.py`) — when rough input mentions harvest / list-of / lead-hunt / prospect / directory / scrape-companies, the drafter rewrites the goal as a `.csv`-targeted spec with explicit columns, row-count target, source hints, and `csv_append_rows` tool reference. Goal max raised from 1200 → 1800 chars to make room for column lists.

**Tests** — 27 new pytest cases:
- `tests/test_dataset_mission_gate.py` (16 cases) — `_is_dataset_goal` true/false matrix, `_count_csv_data_rows` edge cases, threshold sanity.
- `tests/test_csv_append_rows.py` (4 async cases) — happy-path 3-call growth, first-call-requires-columns guard, per-call cap (60 → 25), `$pull` cleanup of stale artifact entries for the same filename.

Backend reboots clean; existing pre-existing failures unrelated.

---

## 🤖 Feb 18, 2026 — Telegram CoS: loop-stop + brand-logo injection
- **Bug A — endless response loop**: when the agent emitted 3-5 `[[IMAGE]]` blocks in a single reply (e.g. "Here are three variants"), Telegram delivered them sequentially as Nano Banana finished each call (5-15s apart), which users perceived as the bot "spamming drafts unprompted". New helper `_cap_image_blocks_for_telegram` in `server.py` (just above `telegram_cos_turn`) keeps only the **first** `[[IMAGE]]` block and folds the rest into a single trailing nudge: `_(Held N additional draft visuals — reply 'another variation' for the next one.)_`. Idempotent — single-block replies pass through unchanged.
- **Bug B — generated images ignored uploaded org logos**: `_generate_image_attachments` in `server.py` now reads `org.brand_assets.primary_logo.filename` (and `alt_logo.filename` when present), resolves them in `UPLOAD_DIR` with a GridFS rehydrate fallback via `recover_bytes_from_gridfs`, and passes the resolved `Path`s to `generate_image_via_nano_banana(..., input_image_paths=...)`. The system prompt is augmented with a "BRAND LOGO — REFERENCE IMAGE ATTACHED" directive instructing Nano Banana to overlay the logo at corner/header scale, preserve colors/proportions, and NOT redraw or stylize the mark itself. Works for both web-chat AND Telegram-source `[[IMAGE]]` blocks.
- **8 new pytest cases**:
  - `tests/test_telegram_loop_fix.py` (6 cases) — single block passes through, multi-block caps to 1, held-nudge pluralization, `max_blocks=2` variant, empty/None safety.
  - `tests/test_image_logo_injection.py` (2 async cases with mocked Nano Banana) — verifies the logo path lands in `input_image_paths` AND that the BRAND LOGO directive appears in the system message; second case verifies orgs with no logo get `input_image_paths=None` (not an empty list).
- All 29 related backend tests pass; backend reboots cleanly with no new errors in `/var/log/supervisor/backend.err.log`.

---

## 🛡️ Feb 17, 2026 — GDPR data export + right-to-erasure (Phase 1) + SOC 2 readiness doc
- **GDPR Article 15 / 20 — Data export**: new `/app/backend/privacy.py` module. `POST /api/account/export` kicks off a background ZIP-builder job; `GET /api/account/export` polls; `GET /api/account/export/download/<token>` streams the ZIP with single-use token semantics. ZIPs auto-expire after 24h and get swept by a daily APScheduler housekeeping job.
  - ZIP contents: `account.json` (no password hash), `memory/`, `connections.json` (OAuth tokens stripped), per-org folders with `role.json`, `conversations/direct/` (DMs + private CoS), `conversations/team-authored.json` (only messages the user wrote), `documents/_manifest.json`, `soulprints.json` (owners/admins only). README inside ZIP explains the data-rights scope and what is intentionally NOT included.
- **GDPR Article 17 — Right to erasure**: `POST /api/account/delete` schedules a 30-day-grace hard delete; `POST /api/account/delete/cancel` aborts at any point in the window; daily APScheduler sweep runs the actual hard delete when grace expires.
  - **Ownership-transfer wizard** — `GET /api/account/delete/status` returns blocking orgs (where user is sole Owner with other members) + candidate transfer targets (Admins first, then Members). `POST /api/account/delete/transfer-ownership` promotes the chosen user and demotes the requester to Admin. Deletion is blocked until every blocker is resolved.
  - Hard-delete cascade: revoke Composio connections → cascade-delete solo-owned orgs → hard-delete private convs + their messages → anonymise the user's team-thread messages (`sender_name → "[deleted user]"`, `sender_id → null`) → delete `user_memory` / `org_members` / `invites` / `privacy_export_jobs` → trim auth_audit_log to 12-month retention → delete user row.
  - Retention exceptions documented in the in-ZIP README + UI: Stripe billing (tax), auth audit logs (12 mo, legitimate-interest), aggregated analytics (no PII).
- **Frontend** `/app/frontend/src/components/PrivacyPanel.js`: theme-aware Privacy card on the Account page with the export button, deletion-pending banner with cancel CTA, and a DeleteWizard modal that walks the user through ownership transfers before allowing the final "type DELETE" confirmation.
- **SOC 2 readiness plan** at `/app/docs/SOC2_READINESS.md` — actionable 4-month roadmap with Vanta/Drata/Secureframe/Thoropass comparison, control gap analysis, 13-policy checklist, ~$22k year-1 cost projection, and explicit "when to start / when to wait" decision criteria. Standalone planning doc; no engineering needed to ship.
- **7 new pytest cases** in `tests/test_privacy.py` covering blocking-orgs logic, slug edge cases, README content, end-to-end ZIP build, single-use token semantics. **43/43 backend tests pass.**
- **Live-verified**: `POST /api/account/export` → ZIP built in ~80 ms (9.8 KB); download via signed URL succeeded; `GET /api/account/delete/status` correctly flagged Reggie Lab as a blocker with 6 transfer targets sorted Admins-first. Privacy panel renders cleanly in light mode at the bottom of the Account page.

⚠️ **Production redeploy required** for any of this to be reachable on https://foundryagents.ai. Add "GDPR data export + right to erasure" to the security page once deployed.

---

## 🎨 Feb 17, 2026 — MetaTargetModal: theme-aware, robust empty/error states
- **Production bug**: user clicked "Select target" on the Facebook connection and got an effectively-empty modal — just the orange "Pin as target" pill floating at the bottom, no readable text. Root cause: the modal was hardcoded to a dark colour scheme (`bg-zinc-900` + `text-white` + `text-amber-300` errors + `opacity-70` empty-states) which against the busy beige light-mode page looked like a low-contrast island. The header / list / empty-state text was technically rendered but visually unreadable.
- **Fix** (`ConnectionsPanel.js` `MetaTargetModal`):
  - Theme-aware backdrop & card: `bg-white text-zinc-900 dark:bg-zinc-900 dark:text-white` so the modal matches whichever theme the user is in. Same for borders, hover states, and the radio-button container.
  - **Empty state** now renders inside a high-contrast amber-bordered box with clear action steps ("disconnect & reconnect Facebook with `Manage your Pages` permissions"). Hard to miss now.
  - **Error state** restructured into the same amber-bordered box, includes a "Most common cause" tip pointing at the `pages_show_list` scope.
  - Header upgraded to `text-base font-semibold` so the title doesn't disappear into the surrounding chrome.
  - Min-height on the body (`min-h-[180px]`) so the modal never collapses to button-only when the API is slow.
  - Selected-state borders darkened from `border-orange-500/60` to solid `border-orange-500` for clearer selection feedback.
- Webpack compiles clean; lint passes.

⚠️ **Production redeploy required** to pick up the modal fix on https://foundryagents.ai.

---

## 🐛 Feb 17, 2026 — IG publish silent-fail + invisible failed-post UX
- **Production root cause**: User saw "Publishing failed on 1 platform" with no detail, and the failed post vanished from view. Two distinct bugs.
- **Bug 1 — Meta Graph API was being handed relative URLs**: `_image_url(post)` returned the raw `/api/files/<filename>` path stored in `social_posts`. Meta requires fully-qualified `https://host/...` because they fetch the image server-side. The IG `POST_IG_USER_MEDIA` container-creation step silently failed because of this. Fixed `social_publishing.py`:
  - New `_public_origin()` resolves `PUBLIC_ORIGIN_OVERRIDE` → `FRONTEND_URL` (same precedence the audit-email module uses).
  - New `_absolutize(url)` promotes relative paths to `https://host/path`; passes absolutes through unchanged.
  - `_image_url()` now always returns absolute URLs, prefers `public_url` over `url` when both present.
  - Same fix automatically applies to Facebook, TikTok, and any other publisher that calls `_image_url`.
- **Bug 2 — Failed posts vanished from the UI**: `SocialPage` had tabs for `draft` / `queued` / `published` only — `status="failed"` posts matched no tab and the user couldn't see the error chip or "Why did this fail?" button on the card. Fixed:
  - Added a **Failed** tab (`SocialPage.js`).
  - **Partial** posts (some platforms succeeded) roll into the Published tab so wins aren't hidden; the per-platform red chip shows which platform failed and why.
  - After every publish attempt, the page auto-switches to the right tab — Failed if it failed, Published if it succeeded — so the user always sees the result card.
  - Improved the failure toast to include the actual Meta error text from `publish_results.<platform>.error` (truncated to 200 chars) instead of just "Publishing failed on 1 platform". Duration bumped to 10s so the user can read it.
- **Tests**: 8 new pytest cases in `tests/test_social_publish_urls.py` covering relative→absolute promotion, `public_url` precedence, origin override, edge cases (None, trailing/leading slashes). All 36 backend tests pass.

⚠️ **Production redeploy required** to fix the live IG publishing flow.

---

## 🤖 Feb 17, 2026 — Mandatory auto-image for social posts (no permission asked)
- **Production complaint addressed**: bot was responding to "Draft an IG post for AZ Pain Doctors" with text + "Want me to generate a matching image?" — never delivering the image even when user said yes. Root cause was a contradictory `NO_FAKE_PROMISES_DIRECTIVE` that told the agent "image generation is handled AUTOMATICALLY by the platform — you do not trigger generation." That made the LLM refuse images.
- **Fix 1 — Directive rewrite**: `NO_FAKE_PROMISES_DIRECTIVE` now says "Image generation is INSIDE YOUR CONTROL via the `[[IMAGE]]` block." Forbidden phrases explicitly include "I can't create or send images", "The platform will generate the image", "Want me to generate", etc.
- **Fix 2 — Mandatory clause**: Added "MANDATORY — AUTO-IMAGE FOR SOCIAL POSTS" to `RESOURCEFULNESS_DIRECTIVE`. Any of IG/FB/LinkedIn/X/TikTok/YouTube/Pinterest/Reddit/Threads/etc. post requests MUST emit BOTH a caption `[[ARTIFACT]]` AND an `[[IMAGE]]` block in the SAME turn. Opt-outs ("text only", "no image", "caption only", "I have my own photo") are honored. Carousels emit one `[[IMAGE]]` per slide.
- **Fix 3 — Few-shot examples**: appended 4 CONCRETE correct-vs-wrong example replies to the directive (most-recent context the LLM sees). Eliminates the "Want me to generate?" failure mode.
- **Fix 4 — Safety-net injector**: `_inject_social_image_block_if_missing` runs after every chat reply (both LlmChat path and Composio-tool path). If the user's message triggers `_SOCIAL_POST_TRIGGERS`, didn't opt out via `_SOCIAL_POST_OPTOUT`, AND the reply has no `[[IMAGE]]` block — auto-inject one with a derived prompt + sensible aspect ratio (`1:1` IG, `9:16` Story/Reel/TikTok, `1.91:1` LinkedIn/FB banner, `16:9` YouTube thumbnail). Belt-and-braces guarantees the image ships regardless of LLM compliance.
- **Verified live**:
  - "Draft an IG post for AZ Pain Doctors" → emits both `[[ARTIFACT]]` and `[[IMAGE]]` naturally, no permission asked.
  - Same prompt + "text only, I have my own photo" → caption only, no `[[IMAGE]]`. Opt-out respected.
  - Safety-net regex matches: IG/FB/LinkedIn/X/TikTok/YouTube/Pinterest/Threads variants; rejects unrelated questions ("what's the weather"). 8 new pytest cases (`tests/test_social_image_autoinject.py`).
- **All 28 backend tests pass.**

⚠️ **Production redeploy required** for the AZ Pain Doctors flow to actually deliver the image. The fix is fully in preview.

---

## 🖼️ Feb 17, 2026 — [[IMAGE]] blocks: chat agents can now actually generate images
- **Bug fixed (reported on production)**: User asked the bot to draft an Instagram post for AZ Pain Doctors. The bot drafted text and said "I'll generate the image" — but no image ever appeared. Root cause: the chat-reply pipeline (`generate_agent_reply`) had no image-generation tool. The agent literally couldn't produce one; only the dedicated `/conversations/{id}/generate-image` endpoint (triggered by the web UI's 🎨 button) could. Telegram users had no way to trigger it.
- **Fix**: Introduced `[[IMAGE prompt="..." aspect="1:1" model="nano-banana"]]` self-closing block. Agents emit it in any chat reply. `process_agent_artifacts` now extracts these blocks alongside `[[ARTIFACT]]` and `[[APP]]`, calls `generate_image_via_nano_banana` per block in parallel, persists each rendered PNG to Documents + GridFS, and returns image attachments alongside text artifacts.
- **Agent prompt updated** (`artifacts.py`): explicit rule that any "Instagram post / ad creative / social graphic / launch banner / draft a post for X" request must emit BOTH an `[[ARTIFACT name="caption.md"]]` block AND an `[[IMAGE prompt="..." aspect="..."]]` block in the SAME turn. No more "I'll generate the image shortly" empty promises.
- **Telegram delivery**: `telegram_cos_turn` now runs `process_agent_artifacts` after every CoS reply, queues image attachments in a `_PENDING_TELEGRAM_PHOTOS` slot per user, and `telegram_inbound._deliver_reply` drains that slot to send each image as a native `sendPhoto` photo bubble right after the text reply. New `telegram_bot.send_photo` + `safe_send_photo` primitives.
- **Verified end-to-end**: live test against Reggie Lab — agent emitted `[[IMAGE]]` + `[[ARTIFACT]]`, Nano Banana rendered a 1.1 MB PNG, saved to docs library, attachment carries `media_url=/api/files/{filename}`. Text caption persisted as a separate `.md` doc.
- **6 unit tests** added (`tests/test_image_block.py`): single block, multi-block, empty-prompt drop, no-tag passthrough, invalid model fallback, long-prompt truncation. All 20 backend tests pass.

⚠️ This fix is in **preview only**. Deploy to production to make the AZ Pain Doctors flow (and any chat image request) actually deliver the image.

---

## 🐛 Feb 17, 2026 — Telegram CoS hotfix: meta-question hijack + legacy org provisioning
- **Bug 1 — Inline mention hijack (fixed)**: Asking the bot a *meta question* about an org by name ("what calendar is connected with DisciplinedOS?") was routing the question into that org's CoS instead of letting the Personal Concierge answer with cross-org context. Inline-match auto-routing is now removed entirely — only `/org <name> ...` explicit slash routes to a specific org. The Concierge punts back to a `/org` instruction whenever a request needs an actual write.
- **Bug 2 — "Couldn't provision your Chief of Staff conversation" (fixed)**: Legacy orgs (DisciplinedOS, and any others created before the `general_team_id` field existed) didn't have a General team row, so `get_or_create_private_cos` returned None and the Telegram CoS turn errored out. `team_access.get_or_create_private_cos` now **self-heals** — it creates the missing "Chief of Staff" team on the fly and writes back `general_team_id` onto the org row. Idempotent + safe for concurrent callers.
- DisciplinedOS verified: back-fill succeeded on first invocation; the private CoS conversation was created and is now ready for future `/org DisciplinedOS ...` routing.
- Test suite: `test_inline_mention_routes_explicitly` → `test_inline_mention_does_not_route` (regression guard for the meta-question hijack). All 7 cases pass.

---

## 🤖 Feb 17, 2026 — Super CoS: Org-Agnostic Telegram Chief of Staff
- **Routing redesign**: the Telegram bot now defaults to the **Personal Concierge** for EVERY message. Org-scoped CoS only fires when the user explicitly routes via `/org <name> ...` slash command or names an org inline ("In Acme Corp, …"). Sticky 30-min carry-over and single-org auto-routing are gone — they caused "why did asking about my agenda end up logged under Random Org?" confusion.
- **Slash parser upgraded**: `/org Acme Corp send the agenda` now works (multi-word org names without needing a `:`/`-` delimiter). Falls back to single-token name when no orgs list is supplied.
- **Personal Concierge tools expanded** (`personal_concierge.py`):
  - `get_my_inbox(query?, max_results?, window_hours?)` — Gmail across every connected account; accepts full Gmail search syntax (`is:unread`, `from:`, `subject:`, …)
  - `read_email(message_id)` — full body of a single message
  - `search_my_docs(query, max_results?)` — cross-org Documents library search (name + title + preview substring match)
- **System prompt rewritten** so the bot can answer general chitchat / world knowledge / drafts without forcing a tool call. Drafting an email reply for the user to copy is now explicitly allowed; only the actual *send* (write to Gmail) is gated behind `punt_to_org`.
- **Voice replies (Phase 2)**: when the user sends voice input, the bot now responds with BOTH the text reply (so they can scroll/re-read) AND a TTS voice note via OpenAI `tts-1` (voice=`nova`, format=`opus`). Renders as a native Telegram voice bubble. `forge_tts.py` is the new shared helper; `telegram_bot.send_voice` is the multipart upload primitive.
- **Tests**: 7 new pytest cases (`tests/test_telegram_cos.py`) covering default→Concierge routing, explicit `/org` routing, inline mention routing, no-sticky regression, and the new tool schemas + `search_my_docs` cross-org behavior. Live-mode probes confirmed Concierge answers math (`Q: what's 12*8 → 96`), lists orgs, and finds the audit PDF across org boundaries.

---

## 🕷️ Feb 17, 2026 — Forge Crawl Phase A + Audit Pipeline Upgrade
- **New module `/app/backend/forge_crawl.py`**: in-house alternative to Firecrawl. Three callable functions: `scrape(url, render=…)`, `crawl(url, max_pages=…)`, `extract(url, schema, instructions=…)`. Pure-Python, AGPL-free, zero third-party API cost.
- **Fetch strategy**: `httpx` first (cheap, ~100 ms), auto-escalate to Playwright when the cleaned text comes back under a JS-shell threshold or matches `<div id="root">` etc. Override knobs: `render="force"` (always Playwright) or `render="never"` (httpx only). `trafilatura` turns the rendered HTML into clean markdown — main content only, chrome/nav/ads stripped.
- **Structured extraction**: `web_extract` ships the cleaned markdown + caller-supplied JSON schema to **gpt-4o-mini** via the Emergent LLM key and returns one JSON object. Cheap, deterministic, falls back gracefully when fields are absent (`null` / `[]`, never invents).
- **Mission tools added** (`missions.py` `build_tools`): `web_scrape`, `web_crawl`, `web_extract`. Registered with the 150s browser-tool timeout so Playwright escalations don't get cut off. LLM tool descriptions explicitly route content-only goals away from `browse_url` so we don't pay for the heavier Chromium path when an httpx fetch would do.
- **HTTP endpoints** (`/app/backend/forge_routes.py`): `POST /api/forge/scrape`, `POST /api/forge/crawl`, `POST /api/forge/extract`. Auth-gated by `get_current_user`. Same response shape as the mission tools so a future Crucible UI panel can wire to either.
- **Audit pipeline upgraded** (`audits.py` `_safe_browse`): the homepage-only Playwright fetch is replaced by a 5-page shallow crawl (homepage + /about / /pricing / /services / /contact, ranked). Each page's markdown is concatenated with a `## Title / URL` header so the synthesizer LLM can attribute claims to specific sections. Verified against Stripe — pulled 7.4 KB homepage + 16.5 KB pricing + 4.0 KB customers + 6.6 KB sales page, all via httpx with zero Playwright escalation.
- **Browser tools change**: `browse_url` now returns the rendered HTML in addition to text/screenshot/page-map. Forge Crawl's Playwright fallback uses this so trafilatura can re-clean the post-JS DOM (text-only would lose link structure).
- **Tests**: 7 unit tests in `/app/backend/tests/test_forge_crawl.py` (network-touching but fast). Covers scrape happy path, scheme normalization, error path, crawl priority ranking, max_pages cap.
- **What this unlocks**: better audits today (multi-page brand signal), cheaper mission research (httpx vs Playwright wherever possible), and a clean foundation for Phase B (full-site BFS) + Phase C (sitemap.xml priming, robots.txt politeness).

---

## 🚀 May 15, 2026 — Composio Phase 4: Tools In Missions
- **New capability**: Missions (Crucible/Playwright long-running agent runs) can now invoke the user's connected Composio tools — Gmail send, Slack post, IG publish, HubSpot create, etc. — alongside the existing web-search / browser / artifact tools. Closes the SaaS-write-boundary gap missions previously hit (they could research but couldn't *act*).
- **Same gating model as chat**: lead agent's `tools_enabled` must be ON AND user must have ACTIVE connection(s). Tools auto-enabled per division when the user connects new toolkits.
- **Budget rails**: hard cap of **20 tool calls per mission**, soft per-platform cap of **5** (prevents a runaway loop from blowing through Composio quota). Bumps the budget on every execute regardless of success so expired-auth toolkits get back-pressure.
- **Tool naming**: short LLM-friendly form (`gmail.send_email`) that maps back to Composio slugs (`GMAIL_SEND_EMAIL`) at handler time. Description prefixed with `[toolkit]` so the catalog reads cleanly.
- **Telemetry split**: tool calls from missions tag the most recent `llm_calls` row as `kind_label="mission_tool:composio:<toolkit>:<slug>"` so SpendDashboard can break out mission tool cost from chat tool cost.
- **Integration site**: new `composio_mission_tools.py` module + `_augment_with_composio_tools` helper in `missions.py`. Wired inside `run_mission_loop` so all five mission entry points (start / resume / force-finish / continue / rehydrate) automatically pick up the tools — zero call-site changes.
- **One critical bug fixed in same pass**: testing agent flagged `db.llm_calls.update_one(..., sort=[...])` was using a kwarg only added in pymongo 4.7 (we're on 4.5). Swapped to `find_one_and_update` which accepts `sort` everywhere. Test coverage now includes a regression guard that becomes a no-op if pymongo gets bumped.
- 17/17 pytest pass at `/app/backend/tests/test_composio_phase4_missions.py` — gating chain (Tool=None / not available / no ACTIVE / lead off), happy-path Tool shape, budget arithmetic, mission start regression with no connections, Phase 1+2+3 routes untouched.

---

## 🐛 May 15, 2026 — Instagram Publish Bug + Connection Visibility
- **Bug fixed**: Instagram "Publish via Foundry" reported success but nothing appeared on the user's feed. Root cause: we were calling the deprecated `INSTAGRAM_CREATE_POST` which only creates a draft media container — never actually publishes. Composio's documentation called this out but the tool *still* returns success on the container creation, masking the bug.
- **Fix**: Instagram now uses Meta's modern two-step Graph API flow:
  1. `INSTAGRAM_POST_IG_USER_MEDIA` — creates media container, returns `container_id`
  2. `INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH` — publishes the container (waits for processing internally)
- **Facebook bug fixed too**: `FACEBOOK_CREATE_POST` requires a `page_id` which we never captured. New flow: on first activation of a Facebook connection we call `FACEBOOK_LIST_MANAGED_PAGES`, pick the first Page the user admins, and cache its id+name on the connection row. Existing connections back-fill on the next `/refresh` click.
- **Image posts** for Facebook now use `FACEBOOK_CREATE_PHOTO_POST` (correct tool for image+caption), not the text-only `FACEBOOK_CREATE_POST`.
- **Per-platform publish flows refactored**: `social_publishing.py` now uses `_publish_<platform>` async functions so each platform owns its own logic (single-call for LinkedIn/Reddit/TikTok, two-call for Instagram, page-aware for Facebook).
- **Connection visibility (the user's other ask)**: Connections panel now shows the actual Facebook Page name and Instagram @handle next to each connection, not just "Connected". E.g. cards now read **"AZ Pain Doctors (Page)"** for Facebook or **"@azpaindoctors"** for Instagram. Discovered via Composio's `FACEBOOK_LIST_MANAGED_PAGES` / `INSTAGRAM_GET_USER_INFO` and cached on the connection row.
- **Back-fill**: existing FB/IG connections that pre-date this fix can pick up their Page/handle by clicking "Check status" once.

---

## 📣 May 15, 2026 — Social Queue → Live Publishing via Foundry
- **New capability**: posts in the Social Queue can now actually be **published**, not just drafted. Two new paths plus the existing manual one:
  - 🟢 **Manual "Mark posted"** — unchanged, still the fallback when no platforms are connected.
  - 🟢 **"Publish via Foundry" button** — visible whenever the post's platforms intersect the user's active Composio connections. Background task fans out to each platform sequentially; UI flips to "Publishing…" and polls until done.
  - 🟢 **Auto-publish at scheduled_for** — APScheduler's `promote_due_drafts` now fires the publish task for posts with `auto_publish=true` (default) the moment their scheduled time arrives. Posts with `auto_publish=false` just get marked ready_at for manual review.
- **Platforms wired (v1)**: LinkedIn, Facebook, Instagram, Reddit, TikTok. Twitter and TikTok require user-supplied OAuth credentials (Composio doesn't have managed creds) — handled with clean error surfaces, not crashes.
- **Per-platform result chips**: each post card now shows green/red chips post-publish (`publish-results-{post_id}`) with the per-platform error in a tooltip — partial successes are visible at a glance.
- **Instagram caveat surfaced**: NicknameModal now shows an amber warning at the moment-of-connect for instagram/facebook/tiktok/twitter explaining the Business-account-must-link-FB-Page Meta requirement (and the BYO-creds gotchas for the other two). Generic toolkits get no warning.
- **5 new featured toolkits** (linkedin/facebook/instagram/reddit added above-the-fold). Marketing/Sales/Content/Social agent divisions auto-enable when these toolkits connect.
- New module `/app/backend/social_publishing.py` keeps Composio glue out of the queue router. Builders raise `ValueError` for unsatisfiable cases (IG no image, Reddit no subreddit) — those surface as per-platform errors without breaking the fan-out.
- 19/19 pytest pass at `/app/backend/tests/test_social_publishing.py` covering the full surface — auth gates, idempotency, per-platform error paths, scheduler hook, auto_publish opt-out, idempotent ready_at filter.

---

## ⚡ May 15, 2026 — Smart Default-ON for Connected Toolkits
- **New UX**: when a user connects a toolkit (e.g. Gmail) and it transitions to ACTIVE, agents in matching divisions get `tools_enabled=true` automatically — no manual toggling.
- **Division → toolkit reverse index**: Gmail enables Sales/Support/HR/Legal agents (12 in the default catalog), Linear enables Engineering/Product (~30), Stripe enables Finance, etc.
- **Respects explicit user overrides**: if a user has *ever* set an agent's toggle (ON or OFF), the auto-enable leaves it alone. Only truly-default agents (no row in `agent_tool_prefs`) get flipped.
- **Idempotent**: re-activating a connection (e.g. after a disconnect/reconnect) doesn't churn the toggles.
- **Audit-friendly**: `agent_tool_prefs` rows the auto-flow creates carry an `auto_enabled_by_toolkit` field so it's clear in the DB why the toggle was flipped.
- **Toast feedback**: after OAuth completes, the user sees "Account connected. Enabled tools for N matching agents." Number bubbles up via `refresh_connection_status` → `result.auto_enabled_agents`.
- **Duplicate-message bug fix bundled in**: `ConversationPage.js` now pauses the 3s live-poll while a send is in flight, and the POST resolution dedupes by *incoming* server ids before appending — kills the race where polling + post-success both inserted the same user/agent messages.

---

## 🔌 May 15, 2026 — Composio Phase 3: Multi-Account, Smart Defaults & 250+ Tool Catalog
- **Multi-account per connector**: each user can now connect Gmail #1 (Work) and Gmail #2 (Personal) — same logic applies to Slack workspaces, HubSpot portals, every toolkit. `allow_multiple=true` passed to Composio's `link()`, plus a nickname captured at connect time and editable via inline pencil. Agent system-prompt now lists every connected account with its nickname so replies can say "sending from your Work Gmail".
- **Featured list grew 5 → 20**: Gmail, Calendar, Slack, Notion, HubSpot, Linear, Jira, Asana, Drive, Dropbox, Discord, Salesforce, Mailchimp, Stripe, Calendly, Zoom, Twitter, LinkedIn, Airtable, GitLab. 19 bootstrap cleanly on startup (Twitter requires custom OAuth credentials — fails gracefully).
- **Browse-all catalog**: new modal pulls the full ~250-toolkit Composio catalog, searchable by name/slug, featured-first sort. Lazy auth-config bootstrap (`ensure_auth_config_for`) creates the auth config on first user Connect click — no startup bloat for the long tail.
- **Smart defaults via recommendations**: every agent's `division_key` maps to a curated toolkit list (Engineering → Linear+GitHub+Jira+GitLab+Slack, Sales → HubSpot+Salesforce+Gmail+Calendar+Calendly, etc., 12 divisions covered). New `GET /agent-tool-recommendations` returns agents whose role matches what the user has already connected. Banner in `AgentToolPrefsPanel` surfaces them and `POST /apply-recommendations` flips them all on in one bulk-upsert (admin-gated).
- **New routes**: `GET /composio/catalog?q=`, `PUT /composio/connections/{id}/nickname`, `GET /agent-tool-recommendations`, `POST /agent-tool-prefs/apply-recommendations`.
- 40/40 pytest pass across Phase 1 + 2 + 3 (one Phase 1 test relaxed to issubset since the featured list grew, no behavioural regression).

---

## 🔧 May 14, 2026 — Composio Phase 2: Agent Tool Calling
- **New capability**: agents can now actually invoke connected tools (Gmail, Slack, Notion, HubSpot, Google Calendar) instead of just describing what they would do. Per-agent toggle, max 3 tool calls per turn.
- **Per-agent toggle** stored in `agent_tool_prefs` collection per `(org_id, agent_id)`. Default OFF — admins must explicitly opt each agent in. Managed by new `AgentToolPrefsPanel.js` (search box, per-agent switch, read-only for non-admins). Routes: `GET /api/orgs/{org_id}/agent-tool-prefs`, `PUT /api/orgs/{org_id}/agent-tool-prefs/{agent_id}` (owner/admin only).
- **Tool subturn**: when an agent has tools_enabled AND the user has at least one active connection, `generate_agent_reply` routes through new `composio_agent.py` which calls OpenAI directly with `chat.completions.create(..., tools=[...])`. `LlmChat` (emergentintegrations) doesn't expose `tools=`, so we go native for this path. Falls back silently to LlmChat when tools don't apply.
- **`/composio/execute` endpoint**: any org member can invoke a single tool on their own connected accounts. Auto-resolves the Composio toolkit version (v3 SDK quirk for manual exec). Records telemetry to `llm_calls` with `kind_label="composio:<toolkit>:<slug>"`.
- **Tool messages**: every tool call persists a `sender_type="tool"` audit message into the conversation (`tool_label`, `tool_status`, `tool_error`, `tool_arguments` preview). `ConversationPage.js` renders these as inline pills (not bubbles) — "Frontend Developer → `Gmail · send email` [ok]". Live polling picks them up automatically via the `/messages/since` cursor.
- **Telemetry**: tool-subturn OpenAI calls write to `llm_calls` with `kind_label="agent_reply:composio"` so SpendDashboard can split tool turns out of normal chat spend.
- **UX fix**: anchors `#connections` / `#agent-tools` / `#org-soul` now auto-expand the "Org setup & insights" accordion on deep-link load (was a flag from the test agent).
- 13/13 backend pytest pass at `/app/backend/tests/test_composio_phase2.py` covering admin gating, prefs CRUD, execute envelope-on-failure, telemetry kind_label, chat regression with tools OFF (default path unchanged), chat regression with tools ON + no connections (silent fallback to LlmChat).

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
| **Jun 1, 2026** | **Billing v2 — true Stripe Subscriptions for Spark + Temper, Refine via sales-contact form, FTC-compliant disclosure flow.** Direct Stripe SDK (not Emergent shadow) creates idempotent Products + recurring Prices at boot via `lookup_key`. New `/api/orgs/{id}/billing/subscribe` opens Stripe Checkout in `mode=subscription` after the user accepts a verbatim disclosure block (consent text + IP + timestamp persisted on `payment_transactions`). New `/billing/portal` opens Stripe Customer Portal for self-serve cancel / card update. Webhook (`customer.subscription.*`, `invoice.paid/failed`) mirrors Stripe state onto `org.plan` / `plan_paid_until` / `subscription_status` / `cancel_at_period_end`. Refine plan is `recurring=False` — its "Contact sales" CTA opens a small form that persists to `db.sales_inquiries` and emails `team@archeforge.com` via Resend. BillingPage redesigned: PlanStatusPill (Active / Cancels on / Not subscribed), Manage subscription button (Portal), required affirmative-consent checkbox before Stripe redirect, FTC `Shield` disclosure block. Phase C monthly overage aggregation cron kept. Production-key swap guide at `/app/backend/STRIPE_SETUP.md`. 11/11 backend pytest pass. Also: Notes editor modal now self-hosts via React Portal so the floating "+ New note" FAB works regardless of which OrgPage sections are collapsed. |

_Roughly 3.5 weeks from blank repo to the platform that lives at `foundryagents.ai` today._

---

_Last updated: Jun 1, 2026 — kept in `/app/memory/CHANGELOG.md`_
