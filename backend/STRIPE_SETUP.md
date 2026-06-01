# Stripe Subscriptions — Production Setup Guide

Foundry uses **real Stripe Subscriptions** for Spark and Temper. This doc explains exactly what to swap when moving from preview → production.

---

## Preview (current state)

`backend/.env` contains:

```
STRIPE_API_KEY=sk_test_emergent
```

This is **Emergent's shadow key** — it only works with the `emergentintegrations` Stripe library (which we **don't** use for subscriptions). The real Stripe SDK rejects it with a 401, and the backend logs:

> Stripe API key is invalid for the real SDK. … Endpoints will return 503 until then.

That's expected. The Billing endpoints all return clean 503s when the key is invalid, so the UI keeps working — no crashes, no broken flows. You just can't actually charge a card in preview.

---

## Production swap — 5-minute checklist

### 1. Set the real Stripe secret key

1. Log in to your Stripe Dashboard → **Developers → API keys**.
2. Copy the **Secret key** (`sk_live_...` for prod, or `sk_test_...` from your *own* test mode for staging).
3. In production, set the env var:

   ```
   STRIPE_API_KEY=sk_live_...
   ```

   (Emergent → Project Settings → Environment Variables, or `backend/.env` if self-hosted.)

4. Restart the backend. On boot you'll see:

   ```
   INFO  Stripe catalogue ready: {'spark': 'price_...', 'temper': 'price_...'}
   ```

   That confirms the Products + Prices were idempotently created in your Stripe account.

### 2. Configure the webhook

Subscriptions are driven by webhooks — Stripe → us tells us when a subscription is created, paid, cancelled, etc. Without this, the UI won't update after billing-period boundaries.

1. Stripe Dashboard → **Developers → Webhooks → Add endpoint**.
2. Endpoint URL: `https://<your-domain>/api/webhook/stripe`
   - Production: `https://foundryagents.ai/api/webhook/stripe`
3. **Events to send** — at minimum:
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.paid`
   - `invoice.payment_failed`
4. Reveal the **Signing secret** (`whsec_...`) and set it in env:

   ```
   STRIPE_WEBHOOK_SECRET=whsec_...
   ```

   When this is set, the backend enforces signature verification on every webhook delivery (recommended for production). When it's missing, the backend accepts unsigned payloads — fine for dev, never use in prod.

### 3. Enable Customer Portal

The "Manage subscription" button opens Stripe's hosted Customer Portal. You need to enable it once per Stripe account:

1. Stripe Dashboard → **Settings → Billing → Customer portal**.
2. Toggle **Activate** on.
3. Recommended settings:
   - **Cancellation behavior:** Cancel at end of billing period (FTC-friendly — they get what they paid for).
   - **Cancellation reasons:** Enable (you'll learn why churn happens).
   - **Update payment method:** Enable.
   - **Invoice history:** Enable.
   - **Products** — leave empty; we don't allow self-serve plan switches yet (use the BillingPage instead).

### 4. (Optional) Enable Tax + Shipping address collection

We don't currently collect sales tax. If you sell into states that require it (most US states for SaaS), turn on **Stripe Tax** in the Dashboard and we'll pick up `automatic_tax: enabled` on the next code change.

---

## Pricing changes

Plan prices live in **`backend/billing.py`** under `PLANS`:

```python
"spark":  {"monthly_usd": 80.0, ...,  "lookup_key": "foundry_spark_monthly_v1"},
"temper": {"monthly_usd": 300.0, ..., "lookup_key": "foundry_temper_monthly_v1"},
```

To change a price for **new customers** without affecting grandfathered ones:

1. Bump the `lookup_key` (e.g. `foundry_spark_monthly_v2`).
2. Edit `monthly_usd`.
3. Restart the backend.

On boot, `ensure_stripe_catalogue()` will create a new Stripe Price with the new lookup key. Existing subscriptions stay attached to the old Price. The new subscription flow uses the new Price.

---

## Refine "contact sales" emails

`backend/.env`:

```
SALES_CONTACT_EMAIL=team@archeforge.com
RESEND_API_KEY=re_...
RESEND_FROM=hello@pro.soulprintengine.ai
```

The Refine contact form POSTs to `/api/orgs/{id}/billing/contact-sales`, which:
1. Always persists the lead to `db.sales_inquiries`.
2. Best-effort emails `SALES_CONTACT_EMAIL` via Resend. If Resend isn't configured the row is still saved — you'll just see leads in the DB only.

Inspect leads directly:

```
db.sales_inquiries.find({status: "new"}).sort({submitted_at: -1})
```

---

## FTC / ROSCA compliance built-in

The subscription flow already includes:

1. **Clear and conspicuous disclosure** — displayed in the `ConsentModal` next to the consent checkbox before any redirect to Stripe.
2. **Affirmative consent** — required checkbox (not pre-checked).
3. **Consent record-keeping** — the verbatim disclosure text + IP + timestamp + user_id are persisted on `payment_transactions.consent_text` / `consent_ip` / `consent_at`.
4. **Simple cancellation** — "Manage subscription" button opens the Stripe Customer Portal at any time.
5. **FTC `Shield` notice** — persistent block on the BillingPage explaining the recurring nature in plain English.

Before launching in jurisdictions that require additional disclosures (California AB-390, Illinois, NY S1209-A, etc.), have a subscription-law attorney review the final copy. This implementation is engineering-compliant but not legal advice.

---

_Last updated: Jun 1, 2026 (see `/app/memory/CHANGELOG.md` for the full billing changelog)._
