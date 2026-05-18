# SOC 2 Type I — Foundry Readiness Plan

**Last updated: February 2026 · Internal planning document · Not for distribution**

This is an actionable readiness plan for a SOC 2 Type I attestation, scoped specifically to Foundry's current architecture (FastAPI + MongoDB + React, two-environment topology, small operator team). It is meant to be read by the Foundry team and (eventually) handed to a compliance partner like Vanta, Drata, or Secureframe.

---

## TL;DR

**Recommended path**: Engage Vanta (or Drata) as the compliance-automation platform; budget ~$10–15k/yr for the platform + ~$10–15k one-time for the auditor. Total cash to first Type I report: **~$20–30k**. Total wall-clock time: **~4 months** of focused effort, of which ~2 months can be parallelised with normal product work.

**Recommended trigger to start**: when a real enterprise prospect asks for a SOC 2 report as a precondition for signing. Doing it earlier is capital tied up in compliance that could be spent on product or growth. Doing it later costs deals.

---

## Why SOC 2 Type I (and not Type II)

| Dimension | Type I | Type II |
|---|---|---|
| What it audits | Design of controls at a point in time | Operation of controls over 6–12 months |
| Time to first report | ~3–4 months | ~9–14 months |
| Cost | ~$15–30k | ~$25–60k |
| Sales utility | Acceptable for most mid-market deals | Required for Fortune-500 / regulated industries |
| Renewal | Annual | Annual (continuous evidence collection) |

**Type I is the right starting point**: it unlocks ~80% of enterprise sales conversations and lets you publish a real attestation in months, not years. Once you renew Type I successfully, the upgrade path to Type II is incremental — same controls, just sustained operation.

---

## Trust Service Criteria (TSC) scope

SOC 2 audits map your controls to one or more of five Trust Service Criteria. The standard starting scope:

- **Security** — required for any SOC 2 report
- **Confidentiality** — strongly recommended given Foundry stores customer prompts, documents, Composio OAuth tokens
- **Availability** — recommended if Foundry positions itself as production-critical (we should)
- **Processing Integrity** — optional; add later if customer financial flows expand
- **Privacy** — optional; the GDPR work (separate doc) covers most of this anyway

**Recommended Type I scope**: Security + Confidentiality + Availability. Skip Processing Integrity and Privacy for v1.

---

## Compliance-automation partner comparison

Choose one before writing any policies. All three integrate with AWS / GCP / Mongo Atlas / GitHub / Google Workspace and auto-collect ~70% of the evidence.

| Vendor | Annual cost | Notable strengths | Weaknesses |
|---|---|---|---|
| **Vanta** | ~$10–12k yr 1 (small team) | Best policy templates, strongest ecosystem of partner auditors, mature integrations, very clean UX | Pricier than Drata at small scale |
| **Drata** | ~$8–12k yr 1 | Strong continuous monitoring, good for engineering-heavy teams | Policy templates less polished than Vanta |
| **Secureframe** | ~$8–14k yr 1 | Tight HIPAA support if that's a future direction | Smaller marketplace |
| **Thoropass** (formerly Laika) | ~$15–20k yr 1 | Includes auditor in the price (bundled) | More expensive baseline, less flexibility on auditor choice |

**Recommendation**: **Vanta** for the small-team path. Their templates are the most ready-to-publish; the auditor marketplace gives the best price discovery; integrations cover every Foundry vendor.

---

## Gap analysis — what Foundry has vs. what SOC 2 needs

Status legend: ✅ in place · 🟡 partial · 🔴 missing

### Security TSC

| Control | Status | Notes |
|---|---|---|
| RBAC / role-based access | ✅ | Owner / Admin / Member / Viewer per org; enforced at API layer |
| Per-tenant data isolation | ✅ | Application-level guards + row-scoping on `org_id`. Strong story for auditors |
| Encryption in transit | ✅ | HTTPS-only, Cloudflare termination |
| Encryption at rest | 🟡 | Mongo Atlas default = AES-256. Need to **verify the cluster tier supports it and document** |
| Auth: MFA on admin tools | 🟡 | Personal MFA likely on; need org-wide enforcement on Mongo Atlas, GitHub, OpenAI org, Composio admin, Stripe |
| Auth: SSO on admin tools | 🔴 | Recommended for SOC 2 hygiene; Vanta will flag this |
| Centralised logging | 🟡 | Have spend / publish / mission logs. Need **auth-event log** (logins, password changes, OAuth grants, MFA changes) and **admin-action log** (owner/admin mutations with before/after diffs) |
| Vulnerability management | 🔴 | Need dependency scanning (GitHub Dependabot — free) + a documented response SLA (e.g. critical CVE → patch in 7 days) |
| Penetration test | 🔴 | One annual pen test ($4–8k). Vanta marketplace lists vetted vendors |
| Endpoint management (MDM) | 🔴 | Required for contractors handling production data. Kandji / Jamf / Microsoft Intune (~$5/seat/mo) |
| Background checks | 🔴 | Required for anyone with production access. Checkr or similar (~$30/run) |
| Annual security training | 🔴 | Vanta ships a 30-minute SAT module everyone completes annually |
| Quarterly access reviews | 🔴 | Document who has access to what; review every quarter. Vanta automates the workflow |

### Confidentiality TSC

| Control | Status | Notes |
|---|---|---|
| Data classification policy | 🔴 | Document which data is Public / Internal / Confidential / Restricted |
| DPA in place with each vendor | 🟡 | Need to file: OpenAI, Anthropic, Google, Mongo Atlas, Composio, Stripe, Telegram, Emergent. All offer DPAs |
| Vendor risk register | 🔴 | A spreadsheet/Vanta module tracking each vendor + their SOC 2 + DPA + criticality |
| Data retention policy | 🟡 | Need to document: spend logs (12 mo), audit logs (3 yr), Stripe records (7 yr per tax law), customer data (until deletion request + grace) |
| Customer-data export + erasure | 🟡 | **In progress — Phase 1 of the privacy plan covers this** |

### Availability TSC

| Control | Status | Notes |
|---|---|---|
| Backup procedure | 🟡 | Mongo Atlas snapshots assumed. Need to **document RPO** (point-in-time recovery window) |
| Disaster recovery plan | 🔴 | Document the runbook: how to restore from snapshot, who has access, communication template |
| Annual restore test | 🔴 | Run a real test: restore a snapshot to a staging cluster, verify integrity, document. Required for Type II; recommended for Type I |
| Incident response plan | 🔴 | Document the runbook: how to triage, classify, escalate, notify customers (within 72h per GDPR), document the post-mortem |
| Uptime monitoring | 🟡 | Have basic backend health checks. Need a public status page (Statuspage / BetterUptime — $30–60/mo) for enterprise sales credibility |
| SLA published | 🔴 | A simple 99.5% target on the pricing page or in the TOS is enough for Type I |

---

## Written policies you'll need

Vanta and Drata both ship templates. Customising each takes ~30 min. You'll need **13 documented policies** for the standard Security + Confidentiality + Availability scope:

1. **Information Security Policy** (the umbrella)
2. **Access Control Policy**
3. **Acceptable Use Policy**
4. **Asset Management Policy** (laptops, cloud accounts)
5. **Vendor Management Policy**
6. **Change Management Policy** (how code reaches production)
7. **Risk Assessment Policy** (annual risk register update)
8. **Incident Response Plan**
9. **Business Continuity & Disaster Recovery Plan**
10. **Data Classification & Handling Policy**
11. **Data Retention & Destruction Policy**
12. **Encryption Policy**
13. **Logging & Monitoring Policy**

Each is 2–4 pages. The whole pack is roughly two days of focused writing if you start from Vanta's templates.

---

## Code-level work needed

Distinct from policies — actual engineering Foundry needs to do:

1. **`auth_audit_log` collection + writes** wherever the auth state changes:
   - login (success + failure)
   - logout
   - password change / reset
   - MFA enable/disable
   - OAuth grant / revoke (per provider)
   - account creation
   - account deletion
   - role change

2. **`admin_audit_log` collection + writes** wherever an Owner/Admin mutates org state:
   - member invite / remove / role change
   - org name / SoulPrint edit
   - billing plan change
   - integration connect / disconnect
   - access-code redemption

3. **`/api/admin/audit-trail` route + frontend page** showing the above (org owners only).

4. **`/api/account/export` + `/api/account/delete`** — covered separately by the GDPR phase.

5. **Dependency scanning + CVE response SLA** — turn on GitHub Dependabot (free). Document the policy: critical CVE patched in 7 days, high in 30, medium/low in 90.

6. **Status page**: BetterUptime or Statuspage. Wire up basic uptime checks (frontend 200, backend `/api/health`, key endpoints).

Total engineering effort: roughly **5–7 focused days**, mostly in week 2–3 of the SOC 2 sprint.

---

## Realistic 4-month timeline

```
Month 1 — Foundations
  W1  Engage Vanta (or Drata). Wire up integrations (Mongo Atlas, GitHub, Stripe, Google Workspace, Composio admin).
  W1  Inventory vendors. File DPAs with each.
  W2  Draft 13 policies from Vanta templates. Stakeholder review.
  W3  Engineering: auth_audit_log + admin_audit_log + audit-trail page.
  W4  Engineering: status page wiring; Dependabot turned on; CVE SLA published.

Month 2 — Controls & evidence
  W5  MFA enforced on all admin tools. Document.
  W6  Quarterly access review process documented + first review run.
  W7  Run an internal "restore from snapshot" test. Document RPO/RTO.
  W8  Annual security training rolled out to team.

Month 3 — Audit prep
  W9  Engage auditor (Vanta marketplace).
  W10 Pen test runs (Cobalt.io, NetSPI, or similar).
  W11 Address pen test findings.
  W12 Auditor kickoff meeting. Evidence walk-through.

Month 4 — Audit window
  W13 Auditor field work (2–3 weeks).
  W14 Auditor field work continued.
  W15 Auditor drafting report.
  W16 Final report delivered. **Type I attestation in hand.**
```

Wall-clock overlap with normal product work: ~30% (mostly in months 1 and 2).

---

## Cost projection (small team, US-based, 2026 prices)

| Line item | One-time | Recurring (annual) |
|---|---|---|
| Vanta (or equivalent) | — | $10,000 |
| Auditor (Type I) | $12,000 | — (annual renewal $8–10k) |
| Pen test | $6,000 | $6,000 |
| MDM (3 contractors) | — | $180 |
| Background checks (3 contractors × $35) | $105 | $105 |
| Status page (BetterUptime) | — | $480 |
| Security training (Vanta-bundled) | — | included |
| Engineering time (~6 days @ blended internal cost) | $4,800 | $0 |
| **Total year 1** | **~$22,900** | **~$16,800** |

For comparison: a single enterprise deal that requires SOC 2 typically pays for ~5 years of compliance overhead.

---

## When NOT to do this

- You have no enterprise prospects asking for it. Spending $22k on compliance to chase deals that aren't there is poor capital allocation.
- The platform is < 12 months old AND your customer base is purely SMB. Type I doesn't actually move SMB customers; they don't ask.
- You're about to do a major architectural change (e.g., re-platforming, breaking changes to data isolation). Wait until the architecture is stable, otherwise your audit evidence will be stale within 6 months.

## When to do this immediately

- An enterprise prospect with > $50k ACV has it as a hard precondition.
- You're entering a regulated industry (legal, medical, financial) where compliance is table stakes.
- Investor diligence is asking about it.
- You want to publish "SOC 2 Type I" on the security page as a differentiator vs. ChatGPT Team or other AI workforce competitors.

---

## Decision checkpoints

This document gets reviewed:

1. **At the start of every enterprise sales conversation**. If they ask, you have a clear answer ("we have the readiness plan and can complete Type I in 4 months from PO").
2. **Annually** as part of normal company planning, even if no current enterprise demand. Costs and vendor landscape shift; this doc is the source of truth for "what would it cost us today."
3. **After any major architectural change**. Memory isolation, multi-tenancy model, auth surface — any of those changes invalidates parts of the readiness story and needs a refresh here.

---

*This plan is intentionally Foundry-specific. It is not a generic SOC 2 guide. Other companies need different scopes, different vendors, and different timelines.*

*Source of truth for compliance status: in-product `Account → Privacy` page + Vanta dashboard (once engaged). This document is the planning record, not the live status board.*
