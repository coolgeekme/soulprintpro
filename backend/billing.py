"""Foundry billing — true Stripe Subscriptions for Spark + Temper,
plus a Refine "Contact sales" flow, the FTC-compliant disclosure
plumbing, and the Phase C monthly overage aggregation.

Architecture
============

  • **Spark + Temper** are real Stripe recurring Subscriptions
    (`mode=subscription`). The first invoice charges immediately; the
    card stays on file and Stripe re-charges monthly. Customers
    self-serve cancel / update card via Stripe Customer Portal.

  • **Refine** is sales-led. The "Contact sales" button posts a lead
    to `db.sales_inquiries` and (best-effort) fires an email to
    `SALES_CONTACT_EMAIL` via Resend.

  • **Overage** is settled out-of-band via a one-time Checkout at
    month-close (see `aggregate_overages_for_previous_month`). We do
    not bury overage into the recurring subscription line so the
    customer can dispute / pay exactly what they used.

Why direct Stripe SDK (not `emergentintegrations`):
  The Emergent helper only exposes one-time `payment` Checkouts
  (`mode=payment`). Recurring subscriptions need `mode=subscription`
  + a recurring Price, which only the direct SDK supports.

Idempotency:
  • Stripe Products + Prices are upserted by `lookup_key` on boot.
  • Org → Stripe Customer mapping is cached on `org.stripe_customer_id`.
  • Webhook handlers re-derive everything from the Stripe object,
    so re-delivery is safe.

Production-key swap:
  STRIPE_API_KEY in `backend/.env` is the only knob. Swap from
  `sk_test_...` to `sk_live_...` and restart the backend. Also
  point the Stripe Dashboard's webhook endpoint at
  `<public-origin>/api/webhook/stripe` and set the signing secret
  via `STRIPE_WEBHOOK_SECRET` (optional — signature verification
  is enforced when present).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
import stripe
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

logger = logging.getLogger(__name__)


# ─── Pricing catalogue ────────────────────────────────────────────────────
#
# Single source of truth for plans + add-ons. Anyone editing pricing
# (marketing pages, billing dashboard, seat-policy warnings) should
# read from here so the numbers can never drift apart.
#
# `recurring`: when True we mint a Stripe Subscription. Refine is
# False — sales-led, custom pricing, no self-serve Checkout.

PLANS: Dict[str, Dict[str, Any]] = {
    "spark": {
        "id": "spark",
        "name": "Spark",
        "tagline": "Solo operator. One organization, focused.",
        "monthly_usd": 80.0,
        "orgs_included": 1,
        "seats_included": 1,
        "included_llm_budget_usd": 20.0,
        "recurring": True,
        "lookup_key": "foundry_spark_monthly_v1",
        "features": [
            "1 organization included",
            "$20/mo of agent compute included",
            "Unlimited notes + asset library",
            "Pay-as-you-go for any overage",
        ],
    },
    "temper": {
        "id": "temper",
        "name": "Temper",
        "tagline": "Growing team. Up to 3 organizations.",
        "monthly_usd": 300.0,
        "orgs_included": 3,
        "seats_included": 5,
        "included_llm_budget_usd": 100.0,
        "recurring": True,
        "lookup_key": "foundry_temper_monthly_v1",
        "features": [
            "Up to 3 organizations",
            "5 seats included (shared across organizations)",
            "$100/mo of agent compute included",
            "Crucible (mission agents) unlocked",
            "Recurring scheduled missions",
        ],
    },
    "refine": {
        "id": "refine",
        "name": "Refine",
        "tagline": "Agency-scale. Unlimited organizations, white-glove.",
        "monthly_usd": None,           # custom — sales-led
        "orgs_included": 10_000,
        "seats_included": 10_000,
        "included_llm_budget_usd": 500.0,
        "recurring": False,
        "lookup_key": None,
        "features": [
            "Unlimited organizations",
            "Unlimited seats",
            "Generous agent-compute included (negotiated)",
            "White-label audit PDFs",
            "Priority support + onboarding",
        ],
    },
}

# Overage multiplier — applied to the raw LLM cost we logged for the
# customer's month. Conservative (2×) so customers don't feel gouged.
OVERAGE_MARKUP = float(os.environ.get("FOUNDRY_OVERAGE_MARKUP", "2.0"))

# Where Refine "contact sales" leads are emailed. Comma-separated for
# multiple recipients ("team@archeforge.com,reggie@coolgeek.me").
SALES_CONTACT_EMAIL = os.environ.get(
    "SALES_CONTACT_EMAIL", "team@archeforge.com",
)


# ─── Pydantic models ──────────────────────────────────────────────────────


class SubscribeBody(BaseModel):
    """Frontend → backend payload for starting a subscription.

    `consent_text` is the exact disclosure copy the user saw next to
    the "Subscribe" button — we persist it on the transaction row
    so we can prove FTC-compliant affirmative consent later."""
    plan_id: str = Field(..., description="One of: spark, temper")
    origin_url: str = Field(..., description="window.location.origin from the browser")
    consent_text: str = Field(
        ...,
        min_length=10,
        description="Verbatim copy of the disclosure the user agreed to",
    )


class SalesContactBody(BaseModel):
    """Refine 'contact sales' lead form payload."""
    name: str = Field(..., min_length=1, max_length=200)
    email: EmailStr
    company: str = Field("", max_length=200)
    message: str = Field("", max_length=2000)


# ─── Helpers ──────────────────────────────────────────────────────────────


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if not isinstance(dt, datetime):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _stripe_configured() -> bool:
    return bool(os.environ.get("STRIPE_API_KEY", "").strip())


def _configure_stripe() -> None:
    """Set the API key on the stripe module. Cheap to call repeatedly."""
    key = os.environ.get("STRIPE_API_KEY", "").strip()
    if key:
        stripe.api_key = key


def _month_window(now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """Return (start_of_current_month, now) in UTC."""
    now = now or datetime.now(timezone.utc)
    start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    return start, now


def _previous_month_window(now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """Return (start_of_previous_month, start_of_current_month) in UTC."""
    now = now or datetime.now(timezone.utc)
    cur_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    if now.month == 1:
        prev_start = datetime(now.year - 1, 12, 1, tzinfo=timezone.utc)
    else:
        prev_start = datetime(now.year, now.month - 1, 1, tzinfo=timezone.utc)
    return prev_start, cur_start


async def _org_llm_spend(db, org_id: str, start: datetime, end: datetime) -> float:
    """Sum `llm_calls.cost_usd` for the org over the window."""
    cur = db.llm_calls.aggregate([
        {"$match": {"org_id": org_id, "created_at": {"$gte": start, "$lt": end}}},
        {"$group": {"_id": None, "cost_usd": {"$sum": "$cost_usd"}}},
    ])
    rows = await cur.to_list(1)
    return float(rows[0]["cost_usd"]) if rows else 0.0


async def _org_doc(db, org_id: str) -> Optional[Dict[str, Any]]:
    try:
        return await db.organizations.find_one({"_id": ObjectId(org_id)})
    except Exception:
        return None


async def _user_doc(db, user_id: str) -> Optional[Dict[str, Any]]:
    try:
        return await db.users.find_one({"_id": ObjectId(user_id)})
    except Exception:
        return None


async def _user_plan(db, user_id: str) -> str:
    """Current effective plan for the user. Defaults to spark when the
    user has never subscribed."""
    user = await _user_doc(db, user_id)
    return (user or {}).get("plan") or "spark"


async def _user_owned_org_count(db, user_id: str) -> int:
    """How many orgs this user currently owns. Used by the plan-limit
    gate on org creation."""
    return await db.org_members.count_documents(
        {"user_id": user_id, "role": "owner"},
    )


async def _user_llm_spend(db, user_id: str, start: datetime, end: datetime) -> float:
    """Sum `llm_calls.cost_usd` across every org this user owns over the
    window. Subscriptions are user-level so spend rolls up across all
    their workspaces."""
    org_ids = []
    async for m in db.org_members.find(
        {"user_id": user_id, "role": "owner"}, {"org_id": 1, "_id": 0},
    ):
        if m.get("org_id"):
            org_ids.append(m["org_id"])
    if not org_ids:
        return 0.0
    cur = db.llm_calls.aggregate([
        {"$match": {"org_id": {"$in": org_ids}, "created_at": {"$gte": start, "$lt": end}}},
        {"$group": {"_id": None, "cost_usd": {"$sum": "$cost_usd"}}},
    ])
    rows = await cur.to_list(1)
    return float(rows[0]["cost_usd"]) if rows else 0.0


async def _outstanding_overage_invoice(db, user_id: str) -> Optional[Dict[str, Any]]:
    """Find the user's most recent unpaid overage invoice."""
    return await db.billing_overage_invoices.find_one(
        {"user_id": user_id, "status": {"$in": ["open", "pending"]}},
        sort=[("period_end", -1)],
        projection={"_id": 0},
    )


# ─── Stripe Product / Price provisioning ─────────────────────────────────


async def ensure_stripe_catalogue() -> Dict[str, str]:
    """Idempotently create Stripe Products + Prices for every recurring
    plan in `PLANS`. Returns `{plan_id: stripe_price_id}`.

    Returns an empty dict if Stripe is not configured OR if the
    configured key is not a real Stripe key (Emergent's shadow
    `sk_test_emergent` only works with the Emergent-payments library
    and the real SDK rejects it). We log + continue so boot never
    fails on the customer's behalf — endpoints requiring Stripe
    will surface a clean 503 instead.

    We key on `lookup_key` (which Stripe lets us treat as a unique
    identifier) so re-running this on every boot does NOT spam
    duplicate Prices into the dashboard.
    """
    if not _stripe_configured():
        logger.info("Stripe not configured — skipping catalogue provisioning.")
        return {}
    _configure_stripe()

    result: Dict[str, str] = {}
    for plan_id, plan in PLANS.items():
        if not plan.get("recurring") or not plan.get("lookup_key"):
            continue
        lookup_key = plan["lookup_key"]
        amount_cents = int(round(float(plan["monthly_usd"]) * 100))

        # 1. Look up existing Price by lookup_key. If present, reuse it.
        try:
            search = await _stripe_call(
                stripe.Price.list, lookup_keys=[lookup_key], limit=1, active=True,
            )
        except stripe.AuthenticationError:
            logger.warning(
                "Stripe API key is invalid for the real SDK. "
                "If you're on the Emergent shadow key `sk_test_emergent`, "
                "swap to a real `sk_test_...` or `sk_live_...` to enable "
                "subscriptions. Endpoints will return 503 until then."
            )
            return {}
        except Exception as exc:
            logger.exception("Stripe Price.list failed for %s: %s", lookup_key, exc)
            continue
        existing = (search.data or [None])[0]
        if existing and existing.unit_amount == amount_cents and existing.recurring \
                and existing.recurring.interval == "month":
            result[plan_id] = existing.id
            continue

        # 2. Otherwise create a new Product + Price. If the price exists
        #    but the amount changed, we leave the old one (Stripe will
        #    let it stay attached to grandfathered subscriptions) and
        #    mint a new active one with the same lookup_key. To do
        #    that cleanly we first nudge the old Price's lookup_key
        #    aside.
        if existing:
            try:
                await _stripe_call(
                    stripe.Price.modify, existing.id,
                    lookup_key=f"{lookup_key}__retired_{int(datetime.now(timezone.utc).timestamp())}",
                )
            except Exception:
                logger.exception("Could not retire stale Price %s", existing.id)

        try:
            product = await _stripe_call(
                stripe.Product.create,
                name=f"Foundry {plan['name']}",
                description=plan["tagline"],
                metadata={"plan_id": plan_id},
            )
            price = await _stripe_call(
                stripe.Price.create,
                unit_amount=amount_cents,
                currency="usd",
                recurring={"interval": "month"},
                product=product.id,
                lookup_key=lookup_key,
                metadata={"plan_id": plan_id},
            )
        except Exception as exc:
            logger.exception("Stripe catalogue create failed for %s: %s", plan_id, exc)
            continue
        result[plan_id] = price.id
        logger.info("Stripe catalogue: provisioned %s -> price %s", plan_id, price.id)
    return result


async def _stripe_call(fn, *args, **kwargs):
    """Tiny wrapper to run blocking Stripe SDK calls in a thread so we
    don't stall the event loop. Stripe's `_async` helpers exist in v15
    but they aren't 1:1 with every method we use, so this is simpler."""
    import asyncio
    return await asyncio.to_thread(fn, *args, **kwargs)


# ─── Stripe Customer per org ─────────────────────────────────────────────


async def _get_or_create_customer(db, user: Dict[str, Any]) -> str:
    """Return the Stripe Customer ID for the user, creating it on
    first use. Subscriptions are user-level — one customer per user,
    not per org."""
    user_doc = await _user_doc(db, user["id"])
    if not user_doc:
        raise HTTPException(404, "User not found.")
    cust_id = user_doc.get("stripe_customer_id")
    if cust_id:
        return cust_id
    _configure_stripe()
    customer = await _stripe_call(
        stripe.Customer.create,
        email=user.get("email"),
        name=user.get("name") or user.get("email") or "Foundry User",
        metadata={
            "user_id": user["id"],
            "user_email": user.get("email") or "",
        },
    )
    await db.users.update_one(
        {"_id": user_doc["_id"]},
        {"$set": {"stripe_customer_id": customer.id}},
    )
    return customer.id


async def user_plan_limits(db, user_id: str, user_email: str | None = None) -> Dict[str, Any]:
    """Resolve the effective plan + org limit for a user. Staff
    members get Refine's limits regardless of subscription state.
    Returns `{plan, plan_id, orgs_limit, is_staff}`.
    """
    from staff import is_staff_email
    if is_staff_email(user_email or ""):
        return {
            "plan": PLANS["refine"], "plan_id": "refine",
            "orgs_limit": PLANS["refine"]["orgs_included"], "is_staff": True,
        }
    plan_id = await _user_plan(db, user_id)
    plan = PLANS.get(plan_id, PLANS["spark"])
    return {
        "plan": plan, "plan_id": plan_id,
        "orgs_limit": plan["orgs_included"], "is_staff": False,
    }


async def enforce_org_creation_limit(db, user: Dict[str, Any]) -> None:
    """Raise 402 if the user is at their plan's org limit. Called from
    the server's org-creation endpoint right before insert."""
    limits = await user_plan_limits(db, user["id"], user.get("email"))
    if limits["is_staff"]:
        return
    owned = await _user_owned_org_count(db, user["id"])
    if owned >= limits["orgs_limit"]:
        raise HTTPException(
            402,
            f"Your {limits['plan']['name']} plan includes {limits['orgs_limit']} "
            f"organization{'s' if limits['orgs_limit'] != 1 else ''}. "
            "Upgrade your plan to create more.",
        )


# ─── Phase C — monthly overage aggregation ────────────────────────────────


async def aggregate_overages_for_previous_month(db) -> List[Dict[str, Any]]:
    """Close out last month: for every USER whose pooled LLM spend
    across all orgs they own exceeded their plan's included budget,
    write an invoice row. Idempotent — running twice for the same
    month does not double-bill, and paid invoices are never overwritten.
    """
    from staff import is_staff_email
    prev_start, cur_start = _previous_month_window()
    period_key = prev_start.strftime("%Y-%m")
    invoices: List[Dict[str, Any]] = []

    # Roll spend up per user by joining llm_calls → org_members(owner).
    pipeline = [
        {"$match": {"created_at": {"$gte": prev_start, "$lt": cur_start}}},
        {"$group": {"_id": "$org_id", "cost_usd": {"$sum": "$cost_usd"}}},
        {"$lookup": {
            "from": "org_members",
            "let": {"oid": "$_id"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$org_id", "$$oid"]},
                    {"$eq": ["$role", "owner"]},
                ]}}},
                {"$project": {"_id": 0, "user_id": 1}},
                {"$limit": 1},
            ],
            "as": "owner",
        }},
        {"$unwind": "$owner"},
        {"$group": {"_id": "$owner.user_id", "cost_usd": {"$sum": "$cost_usd"}}},
    ]
    rows = await db.llm_calls.aggregate(pipeline).to_list(10_000)
    for r in rows:
        user_id = r.get("_id")
        if not user_id:
            continue
        raw_cost = float(r.get("cost_usd") or 0.0)
        user_doc = await _user_doc(db, user_id)
        if not user_doc:
            continue
        if is_staff_email(user_doc.get("email")):
            continue  # staff don't get overage invoices
        plan_id = user_doc.get("plan") or "spark"
        included = PLANS.get(plan_id, PLANS["spark"])["included_llm_budget_usd"]
        if raw_cost <= included:
            continue
        overage_raw = raw_cost - included
        billable = round(overage_raw * OVERAGE_MARKUP, 2)
        invoice = {
            "user_id": user_id,
            "period_key": period_key,
            "period_start": prev_start,
            "period_end": cur_start,
            "plan": plan_id,
            "raw_cost_usd": round(raw_cost, 4),
            "included_usd": included,
            "overage_raw_usd": round(overage_raw, 4),
            "markup": OVERAGE_MARKUP,
            "amount_due_usd": billable,
            "status": "open",
            "created_at": datetime.now(timezone.utc),
        }
        await db.billing_overage_invoices.update_one(
            {"user_id": user_id, "period_key": period_key, "status": {"$ne": "paid"}},
            {"$set": invoice, "$setOnInsert": {"first_seen_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        invoices.append({**invoice, "period_start": invoice["period_start"].isoformat(),
                         "period_end": invoice["period_end"].isoformat(),
                         "created_at": invoice["created_at"].isoformat()})
    logger.info("Overage aggregation closed %s: %d invoices written", period_key, len(invoices))
    return invoices


# ─── Sales lead helper ────────────────────────────────────────────────────


async def _email_sales_lead(db, lead: Dict[str, Any]) -> bool:
    """Fire-and-forget Resend email about a new Refine sales inquiry.

    Returns True when the email was accepted by Resend.
    """
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    sender = os.environ.get("RESEND_FROM", "").strip()
    if not api_key or not sender or not SALES_CONTACT_EMAIL:
        logger.info("Resend not configured — skipping sales email.")
        return False
    subject = f"[Foundry] New Refine inquiry from {lead.get('name')}"
    html_lines = [
        f"<p><strong>From:</strong> {lead.get('name')} &lt;{lead.get('email')}&gt;</p>",
        f"<p><strong>Company:</strong> {lead.get('company') or '—'}</p>",
        f"<p><strong>Org ID:</strong> {lead.get('org_id') or '—'}</p>",
        "<p><strong>Message:</strong></p>",
        f"<blockquote>{(lead.get('message') or '').replace(chr(10), '<br>') or '— (no message)'}</blockquote>",
        f"<p><em>Submitted at {lead.get('submitted_at')}</em></p>",
    ]
    payload = {
        "from": sender,
        "to": [t.strip() for t in SALES_CONTACT_EMAIL.split(",") if t.strip()],
        "subject": subject,
        "html": "".join(html_lines),
        "text": (
            f"New Refine inquiry\n"
            f"From: {lead.get('name')} <{lead.get('email')}>\n"
            f"Company: {lead.get('company') or '—'}\n"
            f"Org ID: {lead.get('org_id') or '—'}\n"
            f"Message: {lead.get('message') or '— (no message)'}\n"
            f"Submitted at {lead.get('submitted_at')}"
        ),
        "reply_to": [lead.get("email")] if lead.get("email") else None,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={k: v for k, v in payload.items() if v is not None},
            )
        if resp.status_code >= 300:
            logger.warning("Sales lead email failed %s: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as exc:
        logger.warning("Sales lead email exception: %s", exc)
        return False


# ─── Webhook side-effects ────────────────────────────────────────────────


async def _apply_subscription_state(db, subscription: Dict[str, Any]) -> None:
    """Mirror the Stripe Subscription state onto the USER doc.

    Subscriptions are user-level — one user, one subscription, N orgs.
    Single function called from every webhook event we care about so
    the user's `plan` / `plan_paid_until` / `subscription_status` /
    `subscription_cancel_at_period_end` always reflect Stripe's truth.
    """
    customer_id = subscription.get("customer")
    if not customer_id:
        return
    user = await db.users.find_one({"stripe_customer_id": customer_id})
    if not user:
        # Customer may also have been stamped via metadata — fall back.
        user_id = (subscription.get("metadata") or {}).get("user_id")
        if user_id:
            try:
                user = await db.users.find_one({"_id": ObjectId(user_id)})
            except Exception:
                user = None
    if not user:
        logger.warning("Subscription %s has no matching user (customer=%s)",
                       subscription.get("id"), customer_id)
        return

    # Derive plan_id from price.lookup_key (Stripe v15). Fall back to
    # the metadata mirror we set at checkout-create time.
    plan_id = None
    items = (subscription.get("items") or {}).get("data") or []
    if items:
        price = items[0].get("price") or {}
        lk = price.get("lookup_key")
        if lk:
            for pid, p in PLANS.items():
                if p.get("lookup_key") == lk:
                    plan_id = pid
                    break
    if not plan_id:
        plan_id = (subscription.get("metadata") or {}).get("plan_id")

    status = subscription.get("status")
    cancel_at_period_end = bool(subscription.get("cancel_at_period_end"))
    current_period_end = subscription.get("current_period_end")
    paid_until = (
        datetime.fromtimestamp(current_period_end, tz=timezone.utc)
        if isinstance(current_period_end, (int, float)) else None
    )

    update: Dict[str, Any] = {
        "subscription_id": subscription.get("id"),
        "subscription_status": status,
        "subscription_cancel_at_period_end": cancel_at_period_end,
        "stripe_customer_id": customer_id,
        "subscription_synced_at": datetime.now(timezone.utc),
    }
    if status in ("active", "trialing", "past_due") and plan_id:
        update["plan"] = plan_id
        if paid_until:
            update["plan_paid_until"] = paid_until
    elif status in ("canceled", "unpaid", "incomplete_expired"):
        update["subscription_status"] = "canceled"

    await db.users.update_one({"_id": user["_id"]}, {"$set": update})
    logger.info(
        "Synced subscription %s: user=%s plan=%s status=%s cancel_at_period_end=%s",
        subscription.get("id"), str(user["_id"]), plan_id, status, cancel_at_period_end,
    )


# ─── Router factory ───────────────────────────────────────────────────────


def make_billing_router(
    *,
    db,
    get_current_user,
    require_org_member=None,  # kept for backwards-compat with call sites
) -> APIRouter:
    """Build the user-level billing router. Mounted at /api.

    Subscriptions are scoped to the USER, not the org — one user has
    one Stripe customer + one subscription that unlocks N orgs as
    defined by the plan's `orgs_included` field.
    """
    del require_org_member  # not used post-refactor
    r = APIRouter(prefix="/api")

    # ── Catalogue (public) ────────────────────────────────────────────
    @r.get("/billing/plans")
    async def list_plans():
        return {
            "plans": [
                {k: v for k, v in p.items() if k != "lookup_key"}
                for p in PLANS.values()
            ],
            "overage_markup": OVERAGE_MARKUP,
        }

    # ── Per-user billing summary ─────────────────────────────────────
    @r.get("/billing/summary")
    async def billing_summary(u: dict = Depends(get_current_user)):
        """Single source of truth for the BillingPage. Plan / status /
        spend / orgs_used vs orgs_limit / outstanding invoice / recent
        transactions — all rolled up at the user level."""
        from staff import is_staff_email
        staff_access = is_staff_email(u.get("email"))
        start, now = _month_window()
        orgs_used = await _user_owned_org_count(db, u["id"])

        if staff_access:
            refine = PLANS["refine"]
            return {
                "plan": {k: v for k, v in refine.items() if k != "lookup_key"},
                "plan_status": "staff",
                "plan_paid_until": None,
                "subscription_status": None,
                "cancel_at_period_end": False,
                "has_stripe_customer": False,
                "is_staff": True,
                "orgs_used": orgs_used,
                "orgs_limit": refine["orgs_included"],
                "period_start": start.isoformat(),
                "period_end": now.isoformat(),
                "this_month_spend_usd": round(
                    await _user_llm_spend(db, u["id"], start, now), 4,
                ),
                "included_llm_budget_usd": refine["included_llm_budget_usd"],
                "projected_overage_raw_usd": 0.0,
                "projected_overage_billable_usd": 0.0,
                "overage_markup": OVERAGE_MARKUP,
                "outstanding_overage": None,
                "recent_transactions": [],
            }

        plan_id = await _user_plan(db, u["id"])
        plan = PLANS.get(plan_id, PLANS["spark"])

        spend = await _user_llm_spend(db, u["id"], start, now)
        included = plan["included_llm_budget_usd"]
        overage_raw = max(0.0, spend - included)
        projected_overage_billable = round(overage_raw * OVERAGE_MARKUP, 2)

        user_doc = await _user_doc(db, u["id"]) or {}
        paid_until = _aware(user_doc.get("plan_paid_until"))
        sub_status = user_doc.get("subscription_status")
        cancel_at_period_end = bool(user_doc.get("subscription_cancel_at_period_end"))

        if sub_status in ("active", "trialing") and not cancel_at_period_end:
            plan_status = "active"
        elif sub_status in ("active", "trialing") and cancel_at_period_end:
            plan_status = "cancelling"
        elif paid_until and paid_until > now:
            plan_status = "active"
        else:
            plan_status = "none"

        outstanding = await _outstanding_overage_invoice(db, u["id"])
        if outstanding:
            outstanding["period_start"] = outstanding["period_start"].isoformat()
            outstanding["period_end"] = outstanding["period_end"].isoformat()
            outstanding["created_at"] = outstanding["created_at"].isoformat()

        txn_rows = await db.payment_transactions.find(
            {"user_id": u["id"]},
            sort=[("created_at", -1)],
            projection={"_id": 0},
        ).to_list(10)
        for t in txn_rows:
            for k in ("created_at", "updated_at", "applied_at"):
                if isinstance(t.get(k), datetime):
                    t[k] = t[k].isoformat()

        return {
            "plan": {k: v for k, v in plan.items() if k != "lookup_key"},
            "plan_status": plan_status,
            "plan_paid_until": paid_until.isoformat() if paid_until else None,
            "subscription_status": sub_status,
            "cancel_at_period_end": cancel_at_period_end,
            "has_stripe_customer": bool(user_doc.get("stripe_customer_id")),
            "is_staff": False,
            "orgs_used": orgs_used,
            "orgs_limit": plan["orgs_included"],
            "period_start": start.isoformat(),
            "period_end": now.isoformat(),
            "this_month_spend_usd": round(spend, 4),
            "included_llm_budget_usd": included,
            "projected_overage_raw_usd": round(overage_raw, 4),
            "projected_overage_billable_usd": projected_overage_billable,
            "overage_markup": OVERAGE_MARKUP,
            "outstanding_overage": outstanding,
            "recent_transactions": txn_rows,
        }

    # ── Subscribe (Spark / Temper) ──────────────────────────────────
    @r.post("/billing/subscribe")
    async def subscribe(
        body: SubscribeBody,
        request: Request,
        u: dict = Depends(get_current_user),
    ):
        plan = PLANS.get(body.plan_id)
        if not plan or not plan.get("recurring"):
            raise HTTPException(
                400, "This plan can't be self-served. Contact sales for Refine.",
            )
        if not _stripe_configured():
            raise HTTPException(503, "Billing is not configured (STRIPE_API_KEY missing).")

        try:
            catalogue = await ensure_stripe_catalogue()
        except stripe.AuthenticationError:
            raise HTTPException(
                503, "Billing is not fully configured — please contact support.",
            )
        price_id = catalogue.get(plan["id"])
        if not price_id:
            raise HTTPException(
                503, "Billing is not fully configured for this plan yet. "
                "Please contact support.",
            )

        customer_id = await _get_or_create_customer(db, u)

        origin = body.origin_url.rstrip("/")
        success_url = f"{origin}/app/billing?session_id={{CHECKOUT_SESSION_ID}}"
        cancel_url = f"{origin}/app/billing?cancelled=1"

        _configure_stripe()
        session = await _stripe_call(
            stripe.checkout.Session.create,
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            allow_promotion_codes=True,
            billing_address_collection="auto",
            metadata={
                "kind": "subscription",
                "user_id": u["id"],
                "plan_id": plan["id"],
                "consent_text": body.consent_text[:500],
            },
            subscription_data={
                "metadata": {
                    "user_id": u["id"],
                    "plan_id": plan["id"],
                },
            },
            submit_type=None,
        )
        await db.payment_transactions.insert_one({
            "session_id": session.id,
            "user_id": u["id"],
            "kind": "subscription",
            "plan_id": plan["id"],
            "amount_usd": float(plan["monthly_usd"]),
            "currency": "usd",
            "consent_text": body.consent_text,
            "consent_ip": (request.client.host if request.client else None),
            "consent_at": datetime.now(timezone.utc),
            "payment_status": "initiated",
            "status": "pending",
            "created_at": datetime.now(timezone.utc),
        })
        return {"url": session.url, "session_id": session.id}

    # ── Customer Portal (manage / cancel) ───────────────────────────
    @r.post("/billing/portal")
    async def billing_portal(
        body: Dict[str, str],
        u: dict = Depends(get_current_user),
    ):
        if not _stripe_configured():
            raise HTTPException(503, "Billing is not configured (STRIPE_API_KEY missing).")
        user_doc = await _user_doc(db, u["id"]) or {}
        customer_id = user_doc.get("stripe_customer_id")
        if not customer_id:
            raise HTTPException(400, "No Stripe customer yet — subscribe first.")
        origin = (body.get("origin_url") or "").rstrip("/")
        return_url = f"{origin}/app/billing"
        _configure_stripe()
        session = await _stripe_call(
            stripe.billing_portal.Session.create,
            customer=customer_id, return_url=return_url,
        )
        return {"url": session.url}

    # ── Status polling for the Stripe redirect ───────────────────────
    @r.get("/billing/status/{session_id}")
    async def checkout_status(session_id: str, u: dict = Depends(get_current_user)):
        txn = await db.payment_transactions.find_one({"session_id": session_id}, {"_id": 0})
        if not txn:
            raise HTTPException(404, "Unknown checkout session.")
        if txn.get("user_id") != u["id"]:
            raise HTTPException(403, "Not your checkout.")
        if not _stripe_configured():
            raise HTTPException(503, "Billing is not configured.")
        _configure_stripe()
        try:
            session = await _stripe_call(
                stripe.checkout.Session.retrieve, session_id,
                expand=["subscription"],
            )
        except Exception as exc:
            logger.exception("Stripe session retrieve failed: %s", exc)
            raise HTTPException(502, "Stripe lookup failed.")

        applied = False
        if session.payment_status == "paid" and session.subscription:
            sub = session.subscription
            if isinstance(sub, str):
                sub = await _stripe_call(stripe.Subscription.retrieve, sub)
            await _apply_subscription_state(db, sub if isinstance(sub, dict) else sub.to_dict())
            applied = True
            await db.payment_transactions.update_one(
                {"session_id": session_id},
                {"$set": {
                    "payment_status": "paid",
                    "status": "complete",
                    "subscription_id": (sub.get("id") if isinstance(sub, dict) else sub.id),
                    "applied_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }},
            )
        else:
            await db.payment_transactions.update_one(
                {"session_id": session_id},
                {"$set": {
                    "payment_status": session.payment_status,
                    "status": session.status,
                    "updated_at": datetime.now(timezone.utc),
                }},
            )

        return {
            "status": session.status,
            "payment_status": session.payment_status,
            "applied": applied,
            "kind": txn.get("kind"),
            "plan_id": txn.get("plan_id"),
        }

    # ── Refine — contact-sales lead capture ─────────────────────────
    @r.post("/billing/contact-sales")
    async def contact_sales(
        body: SalesContactBody,
        request: Request,
        u: dict = Depends(get_current_user),
    ):
        lead = {
            "user_id": u["id"],
            "name": body.name.strip(),
            "email": str(body.email),
            "company": (body.company or "").strip(),
            "message": (body.message or "").strip(),
            "plan_interest": "refine",
            "ip": (request.client.host if request.client else None),
            "user_agent": request.headers.get("user-agent", ""),
            "submitted_at": datetime.now(timezone.utc),
            "status": "new",
        }
        await db.sales_inquiries.insert_one(dict(lead))
        lead_for_email = {**lead, "submitted_at": lead["submitted_at"].isoformat()}
        emailed = await _email_sales_lead(db, lead_for_email)
        return {"ok": True, "emailed": emailed}
    # ── Webhook ─────────────────────────────────────────────────────
    @r.post("/webhook/stripe")
    async def stripe_webhook(request: Request):
        """Stripe → us. Authoritative source for subscription state.

        Signature verification is enforced when `STRIPE_WEBHOOK_SECRET`
        is set. In preview we accept unsigned payloads so the
        Stripe-CLI `stripe trigger` is easy to use; production should
        always set the secret.
        """
        if not _stripe_configured():
            return {"received": False, "reason": "billing-not-configured"}
        _configure_stripe()
        body = await request.body()
        signature = request.headers.get("Stripe-Signature", "")
        webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
        try:
            if webhook_secret:
                event = stripe.Webhook.construct_event(body, signature, webhook_secret)
            else:
                # Unsigned — parse JSON ourselves. Acceptable in dev.
                import json
                event = json.loads(body.decode("utf-8") or "{}")
        except Exception as exc:
            logger.warning("Stripe webhook verification failed: %s", exc)
            raise HTTPException(400, "Invalid webhook payload.")

        event_type = (event.get("type") if isinstance(event, dict) else event["type"])
        data_object = (
            event["data"]["object"] if isinstance(event, dict)
            else event.data.object.to_dict()
        )

        if event_type in (
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
            "customer.subscription.trial_will_end",
        ):
            await _apply_subscription_state(db, data_object)
        elif event_type in ("invoice.paid", "invoice.payment_failed"):
            # On every invoice settlement Stripe also fires
            # subscription.updated — but we still log the invoice for
            # the customer-facing transaction list.
            sub_id = data_object.get("subscription")
            if sub_id:
                try:
                    sub = await _stripe_call(stripe.Subscription.retrieve, sub_id)
                    await _apply_subscription_state(db, sub.to_dict())
                except Exception:
                    logger.exception("Couldn't retrieve subscription %s for invoice event", sub_id)
            await db.payment_transactions.insert_one({
                "kind": "invoice",
                "event_type": event_type,
                "invoice_id": data_object.get("id"),
                "subscription_id": data_object.get("subscription"),
                "amount_usd": (data_object.get("amount_paid") or 0) / 100.0,
                "currency": data_object.get("currency") or "usd",
                "status": "paid" if event_type == "invoice.paid" else "failed",
                "payment_status": "paid" if event_type == "invoice.paid" else "failed",
                "stripe_customer_id": data_object.get("customer"),
                "created_at": datetime.now(timezone.utc),
            })
        else:
            logger.debug("Stripe webhook ignored: %s", event_type)

        return {"received": True, "event_type": event_type}

    return r
