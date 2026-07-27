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
        "tagline": "For solo founders shipping alone",
        "monthly_usd": 80.0,
        "annual_usd": 66.0,
        "orgs_included": 1,
        "seats_included": 1,
        "included_llm_budget_usd": 20.0,
        "recurring": True,
        "lookup_keys": {
            "monthly": "foundry_spark_monthly_v1",
            "annual": "foundry_spark_annual_v1",
        },
        "features": [
            "1 user · 1 organization · 3 teams",
            "Full 150+ agent catalogue",
            "750 agent messages / month",
            "5 autonomous mission runs / month",
            "15 Signal Audits / month",
            "60 browser automation min / mo (Crucible + Missions)",
            "10 GB documents · 250 files",
            "Community email support",
            "+ Add-ons: extra orgs ($20/mo) · seats ($30/mo) · usage packs",
        ],
        "studio_perk": "ArcheForge Studios services available at retail",
    },
    "temper": {
        "id": "temper",
        "name": "Temper",
        "tagline": "For teams scaling without scaling headcount",
        "monthly_usd": 300.0,
        "annual_usd": 250.0,
        "orgs_included": 3,
        "seats_included": 5,
        "extra_seat_usd": 40.0,
        "max_seats": 20,
        "included_llm_budget_usd": 100.0,
        "recurring": True,
        "lookup_keys": {
            "monthly": "foundry_temper_monthly_v1",
            "annual": "foundry_temper_annual_v1",
        },
        "highlight": True,
        "features": [
            "5 seats · 3 orgs · unlimited teams",
            "Custom agent creation",
            "5,000 messages / month (pooled)",
            "75 mission runs / month",
            "200 Signal Audits / month",
            "600 browser automation min / mo + 3 saved logins",
            "25 GB documents · unlimited files",
            "Proof of Integrity (Bitcoin-anchored)",
            "White-label Signal Audit PDFs",
            "Priority support",
        ],
        "studio_perk": "15% off all ArcheForge Studios services + priority queue",
    },
    "refine": {
        "id": "refine",
        "name": "Refine",
        "tagline": "For agencies + enterprises running ON our platform",
        "monthly_usd": None,           # custom — sales-led
        "orgs_included": 10_000,
        "seats_included": 10_000,
        "included_llm_budget_usd": 500.0,
        "recurring": False,
        "lookup_keys": None,
        "features": [
            "Unlimited seats · unlimited orgs · multi-workspace",
            "Custom agent personalities co-designed with our team",
            "Fair-use everything (100k msgs · 1k missions · 2k audits / mo)",
            "Unlimited browser automation min + unlimited saved logins",
            "Bring-your-own LLM key option",
            "White-label everything: domain, logo, client portal",
            "SSO/SAML · SCIM · DPA · custom data retention (scoped into your build)",
            "Dedicated infra option — your own cluster (scoped into your build)",
            "SOC 2 Type II on the roadmap for the dedicated reference build",
            "Dedicated CSM + QBR",
        ],
        "studio_perk": "$2,000/mo ArcheForge credits included + 30% off everything after",
    },
}

# Overage multiplier — applied to the raw LLM cost we logged for the
# customer's month. Conservative (2×) so customers don't feel gouged.
OVERAGE_MARKUP = float(os.environ.get("FOUNDRY_OVERAGE_MARKUP", "2.0"))

# ─── Add-on catalogue ─────────────────────────────────────────────────────
#
# Mirrors the "Need more headroom? Add it on." section on /pricing. Three
# kinds:
#   • cadence="recurring" → Stripe Subscription line; mutates user-level
#     `extra_orgs` / `extra_seats` counters which the seat-policy + org-
#     cap resolvers ADD to the plan's baseline.
#   • cadence="one_time" → either (a) a one-time Stripe Checkout when the
#     user has no active sub, or (b) an Invoice Item bolted onto the next
#     recurring invoice when they do. Either way, on successful payment
#     we top up a credit balance the consumer (messages / browser min /
#     audits) decrements before falling back to plan limits.
#
# Each add-on owns its own Stripe lookup_key so the dashboard groups
# clean line items. Versioned `_v1` so we can retire if economics shift.

ADDONS: Dict[str, Dict[str, Any]] = {
    "extra_org": {
        "id": "extra_org",
        "name": "Extra organization",
        "blurb": "Spin up another org with its own teams, agents, and brand soul.",
        "price_usd": 20.0,
        "unit": "/org / mo",
        "cadence": "recurring",
        "lookup_key": "foundry_addon_extra_org_v1",
        # When applied → mutates `users.extra_orgs` += quantity.
        "apply_kind": "extra_orgs",
        "credit_units": None,
    },
    "extra_seat": {
        "id": "extra_seat",
        "name": "Extra seat (Spark)",
        "blurb": "Invite another teammate to your Spark org. Shared usage pool.",
        "price_usd": 30.0,
        "unit": "/seat / mo",
        "cadence": "recurring",
        "lookup_key": "foundry_addon_extra_seat_v1",
        # Only Spark sells per-seat add-ons; Temper includes 5 already +
        # has its own extra-seat at $40/mo via the plan upgrade path.
        # We refuse purchase on Temper at checkout time.
        "plan_restrictions": ["spark"],
        "apply_kind": "extra_seats",
        "credit_units": None,
    },
    "message_pack": {
        "id": "message_pack",
        "name": "Message pack",
        "blurb": "Top up 500 extra agent messages whenever you spike. No expiry.",
        "price_usd": 15.0,
        "unit": "/ 500 msgs",
        "cadence": "one_time",
        "lookup_key": "foundry_addon_message_pack_v1",
        "apply_kind": "credit_messages",
        "credit_units": 500,
    },
    "browser_pack": {
        "id": "browser_pack",
        "name": "Browser automation pack",
        "blurb": "Add 200 extra minutes for Crucible + Mission browser work.",
        "price_usd": 20.0,
        "unit": "/ 200 min",
        "cadence": "one_time",
        "lookup_key": "foundry_addon_browser_pack_v1",
        "apply_kind": "credit_browser_minutes",
        "credit_units": 200,
    },
    "mission_pack": {
        "id": "mission_pack",
        "name": "Mission pack",
        "blurb": "10 extra Foundry missions for deep research, lead-hunting, or scraping. No expiry.",
        "price_usd": 10.0,
        "unit": "/ 10 missions",
        "cadence": "one_time",
        "lookup_key": "foundry_addon_mission_pack_v1",
        "apply_kind": "credit_missions",
        "credit_units": 10,
    },
}


def _addon_or_404(addon_id: str) -> Dict[str, Any]:
    """Resolve an add-on id → catalogue entry or raise HTTPException(404).
    Centralised so the same error shape comes back from every endpoint."""
    a = ADDONS.get(addon_id)
    if not a:
        raise HTTPException(404, f"Unknown add-on: {addon_id}")
    return a


def _public_addons() -> List[Dict[str, Any]]:
    """Catalogue scrubbed for /billing/plans (no internal-only keys)."""
    out = []
    for a in ADDONS.values():
        out.append({k: v for k, v in a.items()
                    if k not in {"lookup_key", "apply_kind"}})
    return out

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
    so we can prove FTC-compliant affirmative consent later.

    `cadence` picks between Stripe's monthly and annual prices. Both
    are recurring; annual = `annual_usd × 12` billed once a year.
    """
    plan_id: str = Field(..., description="One of: spark, temper")
    origin_url: str = Field(..., description="window.location.origin from the browser")
    consent_text: str = Field(
        ...,
        min_length=10,
        description="Verbatim copy of the disclosure the user agreed to",
    )
    cadence: str = Field(
        "monthly",
        pattern="^(monthly|annual)$",
        description="Billing cadence — monthly or annual",
    )


class SalesContactBody(BaseModel):
    """Refine 'contact sales' lead form payload."""
    name: str = Field(..., min_length=1, max_length=200)
    email: EmailStr
    company: str = Field("", max_length=200)
    message: str = Field("", max_length=2000)


class AddonCheckoutBody(BaseModel):
    """Frontend → backend payload for purchasing an add-on.

    ``quantity`` only applies to recurring add-ons (extra_org / extra_seat).
    One-time packs always provision exactly one pack's worth of credit
    units per purchase — quantity is ignored.

    ``consent_text`` is the verbatim disclosure the user saw next to the
    "Buy" button, persisted on payment_transactions for FTC ROSCA
    compliance (same pattern as plan subscribe).
    """
    addon_id: str = Field(..., description="One of ADDONS keys")
    origin_url: str = Field(..., description="window.location.origin")
    consent_text: str = Field(..., min_length=10)
    quantity: int = Field(1, ge=1, le=50)


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


async def ensure_stripe_catalogue() -> Dict[str, Dict[str, str]]:
    """Idempotently create Stripe Products + Prices for every recurring
    plan in `PLANS`. Each plan has TWO recurring prices — a monthly
    and an annual one — keyed by independent `lookup_keys`.

    Returns `{plan_id: {"monthly": price_id, "annual": price_id}}`.

    Returns an empty dict if Stripe is not configured OR if the
    configured key is not a real Stripe key (Emergent's shadow
    `sk_test_emergent` only works with the Emergent-payments library).

    Cheap to call on every boot — the underlying `Price.list` filter
    by `lookup_key` is a single round-trip per price.
    """
    if not _stripe_configured():
        logger.info("Stripe not configured — skipping catalogue provisioning.")
        return {}
    _configure_stripe()

    result: Dict[str, Dict[str, str]] = {}
    for plan_id, plan in PLANS.items():
        if not plan.get("recurring") or not plan.get("lookup_keys"):
            continue
        plan_prices: Dict[str, str] = {}
        # We provision both cadences. Annual is `unit_amount × 12`
        # because Stripe wants the full per-period amount, not the
        # monthly equivalent.
        cadences = [
            ("monthly", float(plan["monthly_usd"]), "month"),
            ("annual", float(plan["annual_usd"]) * 12, "year"),
        ]
        # Reuse one Product per plan across both prices for clean dashboards.
        product_id = None
        for cadence, total_usd, interval in cadences:
            lookup_key = plan["lookup_keys"][cadence]
            amount_cents = int(round(total_usd * 100))
            try:
                search = await _stripe_call(
                    stripe.Price.list, lookup_keys=[lookup_key], limit=1, active=True,
                )
            except stripe.AuthenticationError:
                logger.warning(
                    "Stripe API key invalid for the real SDK. Endpoints will "
                    "return 503 until a real `sk_test_...` or `sk_live_...` "
                    "is configured."
                )
                return {}
            except Exception as exc:
                logger.exception("Stripe Price.list failed for %s: %s", lookup_key, exc)
                continue
            existing = (search.data or [None])[0]
            if (
                existing
                and existing.unit_amount == amount_cents
                and existing.recurring
                and existing.recurring.interval == interval
            ):
                plan_prices[cadence] = existing.id
                product_id = existing.product
                continue
            # Retire stale price (different amount or interval) so we
            # can mint a fresh one under the same lookup_key.
            if existing:
                try:
                    await _stripe_call(
                        stripe.Price.modify, existing.id,
                        lookup_key=f"{lookup_key}__retired_{int(datetime.now(timezone.utc).timestamp())}",
                    )
                except Exception:
                    logger.exception("Could not retire stale Price %s", existing.id)
            try:
                if not product_id:
                    product = await _stripe_call(
                        stripe.Product.create,
                        name=f"Foundry {plan['name']}",
                        description=plan["tagline"],
                        metadata={"plan_id": plan_id},
                    )
                    product_id = product.id
                price = await _stripe_call(
                    stripe.Price.create,
                    unit_amount=amount_cents,
                    currency="usd",
                    recurring={"interval": interval},
                    product=product_id,
                    lookup_key=lookup_key,
                    metadata={"plan_id": plan_id, "cadence": cadence},
                )
                plan_prices[cadence] = price.id
                logger.info("Stripe catalogue: %s/%s -> %s", plan_id, cadence, price.id)
            except Exception as exc:
                logger.exception("Stripe Price.create failed for %s/%s: %s", plan_id, cadence, exc)
        if plan_prices:
            result[plan_id] = plan_prices
    return result


async def ensure_stripe_addon_catalogue() -> Dict[str, str]:
    """Idempotently provision a Stripe Product + Price for every add-on
    in ``ADDONS``. Returns ``{addon_id: price_id}``.

    Recurring add-ons get a recurring Price (interval=month). One-time
    add-ons get a one-shot Price (no recurring block) which we attach
    either to a one-time Checkout or to an Invoice Item depending on
    the customer's current state.

    Same shape as ``ensure_stripe_catalogue`` so the boot path can call
    both. Cheap — one Price.list per add-on, no-op if pricing hasn't
    drifted."""
    if not _stripe_configured():
        logger.info("Stripe not configured — skipping add-on catalogue.")
        return {}
    _configure_stripe()
    result: Dict[str, str] = {}
    for addon_id, a in ADDONS.items():
        lookup_key = a["lookup_key"]
        amount_cents = int(round(float(a["price_usd"]) * 100))
        try:
            search = await _stripe_call(
                stripe.Price.list, lookup_keys=[lookup_key], limit=1, active=True,
            )
        except stripe.AuthenticationError:
            logger.warning("Stripe API key invalid — skipping add-on catalogue.")
            return {}
        except Exception as exc:
            logger.exception("Stripe Price.list failed for %s: %s", lookup_key, exc)
            continue
        existing = (search.data or [None])[0]
        is_recurring = (a["cadence"] == "recurring")
        match_recurring = bool(existing and existing.recurring) == is_recurring
        if (
            existing and existing.unit_amount == amount_cents
            and match_recurring
            and (not is_recurring or (existing.recurring and existing.recurring.interval == "month"))
        ):
            result[addon_id] = existing.id
            continue
        # Retire stale price so we can mint fresh under the same lookup_key.
        if existing:
            try:
                await _stripe_call(
                    stripe.Price.modify, existing.id,
                    lookup_key=f"{lookup_key}__retired_{int(datetime.now(timezone.utc).timestamp())}",
                )
            except Exception:
                logger.exception("Could not retire stale add-on Price %s", existing.id)
        # Create fresh Product + Price.
        try:
            product = await _stripe_call(
                stripe.Product.create,
                name=f"Foundry · {a['name']}",
                description=a["blurb"],
                metadata={"addon_id": addon_id, "kind": "addon"},
            )
            price_kwargs: Dict[str, Any] = dict(
                unit_amount=amount_cents,
                currency="usd",
                product=product.id,
                lookup_key=lookup_key,
                metadata={"addon_id": addon_id,
                          "apply_kind": a["apply_kind"],
                          "credit_units": str(a.get("credit_units") or "")},
            )
            if is_recurring:
                price_kwargs["recurring"] = {"interval": "month"}
            price = await _stripe_call(stripe.Price.create, **price_kwargs)
            result[addon_id] = price.id
            logger.info("Stripe add-on catalogue: %s -> %s", addon_id, price.id)
        except Exception as exc:
            logger.exception("Stripe Price.create failed for add-on %s: %s", addon_id, exc)
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
    members and superadmins get Refine's limits regardless of
    subscription state. Returns
    `{plan, plan_id, orgs_limit, is_staff, extra_orgs}`.

    ``orgs_limit`` already incorporates any active `extra_org` add-on
    subscriptions (purchased via /billing/addons/checkout). Callers
    SHOULD NOT add `extra_orgs` themselves — the resolver does it.
    """
    from staff import is_privileged_user
    user_doc = await _user_doc(db, user_id) or {}
    # Prefer the freshly-fetched email so we don't miss a superadmin
    # whose caller only supplied the id.
    user_doc.setdefault("email", user_email)
    if is_privileged_user(user_doc):
        return {
            "plan": PLANS["refine"], "plan_id": "refine",
            "orgs_limit": PLANS["refine"]["orgs_included"], "is_staff": True,
            "extra_orgs": 0,
        }
    plan_id = user_doc.get("plan") or "spark"
    plan = PLANS.get(plan_id, PLANS["spark"])
    # Pull add-on counter off the user row. Refine is unlimited already
    # — don't bother stacking extras, the number would be misleading.
    extra_orgs = 0
    if plan_id != "refine":
        extra_orgs = max(0, int(user_doc.get("extra_orgs") or 0))
    return {
        "plan": plan, "plan_id": plan_id,
        "orgs_limit": plan["orgs_included"] + extra_orgs,
        "is_staff": False,
        "extra_orgs": extra_orgs,
    }


async def enforce_org_creation_limit(db, user: Dict[str, Any]) -> None:
    """Raise 402 if the user is at their plan's org limit. Called from
    the server's org-creation endpoint right before insert. Privileged
    users (superadmin / staff) always bypass — they can spin up as
    many orgs as they need."""
    from staff import is_privileged_user
    user_doc = await _user_doc(db, user["id"]) or {}
    # Layer the auth-payload email onto the doc so a freshly-signed-up
    # superadmin whose row hasn't cached the field is still caught.
    if not user_doc.get("email") and user.get("email"):
        user_doc["email"] = user["email"]
    if is_privileged_user(user_doc):
        return
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
    from staff import is_privileged_user
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
        if is_privileged_user(user_doc):
            continue  # staff / superadmins don't get overage invoices
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

    # Derive plan_id from price.lookup_key (Stripe v15). The lookup
    # key may belong to either the monthly or the annual cadence —
    # check `lookup_keys` mapping. Fall back to the metadata mirror
    # we set at checkout-create time.
    plan_id = None
    cadence = None
    items = (subscription.get("items") or {}).get("data") or []
    if items:
        price = items[0].get("price") or {}
        lk = price.get("lookup_key")
        if lk:
            for pid, p in PLANS.items():
                lookup_keys = p.get("lookup_keys") or {}
                for cad, key in lookup_keys.items():
                    if key == lk:
                        plan_id = pid
                        cadence = cad
                        break
                if plan_id:
                    break
    meta = subscription.get("metadata") or {}
    if not plan_id:
        plan_id = meta.get("plan_id")
    if not cadence:
        cadence = meta.get("cadence")

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
        "subscription_cadence": cadence,
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


async def _apply_addon_purchase(
    db, *, user_id: str, addon_id: str, quantity: int,
    stripe_ref: str, source: str,
) -> None:
    """Settle a completed add-on purchase.

    Decides what to mutate on the user row based on ``addon.apply_kind``:

      • ``extra_orgs`` / ``extra_seats`` — recurring add-ons; we set the
        counter to ``quantity`` (replace, not add) because Stripe's
        ``subscription.updated`` events are the source of truth and may
        replay. The seat / org policy resolvers later ADD this to the
        plan's baseline.
      • ``credit_messages`` / ``credit_browser_minutes`` / ``credit_audits``
        — one-time packs; we INCREMENT the credit balance by
        ``credit_units × quantity``. Replay-safety comes from the
        idempotency table (``addon_purchases``) — we never apply the
        same stripe_ref twice.

    ``source`` is one of "checkout" / "invoice" / "subscription" so the
    audit row records the trigger surface.
    """
    addon = ADDONS.get(addon_id)
    if not addon:
        logger.warning("Unknown addon_id from Stripe webhook: %s", addon_id)
        return

    # Idempotency: skip if we've already applied this exact event.
    existing = await db.addon_purchases.find_one({"stripe_ref": stripe_ref})
    if existing and existing.get("applied"):
        return

    try:
        user_oid = ObjectId(user_id)
    except Exception:
        logger.warning("Bad user_id on addon application: %r", user_id)
        return
    user = await db.users.find_one({"_id": user_oid})
    if not user:
        logger.warning("Addon purchase user %s vanished before apply", user_id)
        return

    apply_kind = addon["apply_kind"]
    now = datetime.now(timezone.utc)
    update: Dict[str, Any] = {"updated_at": now}

    if apply_kind in ("extra_orgs", "extra_seats"):
        # Replace semantics for recurring add-ons — Stripe is truth.
        update[apply_kind] = max(0, int(quantity))
    elif apply_kind in (
        "credit_messages", "credit_browser_minutes", "credit_missions",
    ):
        # Increment semantics for one-time packs.
        delta = int((addon.get("credit_units") or 0) * int(quantity))
        if delta <= 0:
            logger.warning("Add-on %s has no credit_units; skipping", addon_id)
            return
        await db.users.update_one(
            {"_id": user_oid},
            {"$inc": {apply_kind: delta}, "$set": {"updated_at": now}},
        )
        update = {}  # already wrote the $inc
    else:
        logger.warning("Unknown apply_kind %s on add-on %s", apply_kind, addon_id)
        return

    if update:
        await db.users.update_one({"_id": user_oid}, {"$set": update})

    await db.addon_purchases.update_one(
        {"stripe_ref": stripe_ref},
        {"$set": {
            "stripe_ref": stripe_ref, "user_id": user_id, "addon_id": addon_id,
            "quantity": int(quantity), "apply_kind": apply_kind,
            "credit_units_added": int((addon.get("credit_units") or 0) * int(quantity))
                                    if apply_kind.startswith("credit_") else 0,
            "source": source, "applied": True, "applied_at": now,
        }},
        upsert=True,
    )
    logger.info(
        "Applied add-on %s × %d for user=%s via %s (ref=%s)",
        addon_id, quantity, user_id, source, stripe_ref,
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
                {k: v for k, v in p.items() if k != "lookup_keys"}
                for p in PLANS.values()
            ],
            "addons": _public_addons(),
            "overage_markup": OVERAGE_MARKUP,
        }

    # ── Per-user billing summary ─────────────────────────────────────
    @r.get("/billing/summary")
    async def billing_summary(u: dict = Depends(get_current_user)):
        """Single source of truth for the BillingPage. Plan / status /
        spend / orgs_used vs orgs_limit / outstanding invoice / recent
        transactions — all rolled up at the user level."""
        from staff import is_privileged_user
        user_doc = await _user_doc(db, u["id"]) or {}
        # Splice in the auth payload so a freshly-flagged superadmin
        # whose /me hasn't been re-fetched still gets the bypass.
        if not user_doc.get("email") and u.get("email"):
            user_doc["email"] = u.get("email")
        if not user_doc.get("is_superadmin") and u.get("is_superadmin"):
            user_doc["is_superadmin"] = True
        staff_access = is_privileged_user(user_doc)
        start, now = _month_window()
        orgs_used = await _user_owned_org_count(db, u["id"])

        if staff_access:
            refine = PLANS["refine"]
            return {
                "plan": {k: v for k, v in refine.items() if k != "lookup_keys"},
                "plan_status": "staff",
                "plan_paid_until": None,
                "subscription_status": None,
                "cancel_at_period_end": False,
                "has_stripe_customer": False,
                "is_staff": True,
                "is_privileged": True,
                "is_superadmin": bool(user_doc.get("is_superadmin")),
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
                "addons": {
                    "extra_orgs": 0, "extra_seats": 0,
                    "credit_messages": 0, "credit_browser_minutes": 0,
                    "credit_missions": 0,
                },
                "addons_catalogue": _public_addons(),
            }

        plan_id = await _user_plan(db, u["id"])
        plan = PLANS.get(plan_id, PLANS["spark"])

        spend = await _user_llm_spend(db, u["id"], start, now)
        included = plan["included_llm_budget_usd"]
        overage_raw = max(0.0, spend - included)
        projected_overage_billable = round(overage_raw * OVERAGE_MARKUP, 2)

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
            for k in ("created_at", "updated_at", "applied_at", "consent_at"):
                if isinstance(t.get(k), datetime):
                    t[k] = t[k].isoformat()

        # ── Active add-ons + credit balances ───────────────────────
        # `extra_orgs` / `extra_seats` are recurring; the user-doc fields
        # are kept in sync by webhook handlers (subscription.* events).
        # `credit_*` are one-time pack balances — incremented on
        # successful purchase, decremented as the user consumes them.
        addons_state = {
            "extra_orgs": int(user_doc.get("extra_orgs") or 0),
            "extra_seats": int(user_doc.get("extra_seats") or 0),
            "credit_messages": int(user_doc.get("credit_messages") or 0),
            "credit_browser_minutes": int(user_doc.get("credit_browser_minutes") or 0),
            "credit_missions": int(user_doc.get("credit_missions") or 0),
        }

        return {
            "plan": {k: v for k, v in plan.items() if k != "lookup_keys"},
            "plan_status": plan_status,
            "plan_paid_until": paid_until.isoformat() if paid_until else None,
            "subscription_status": sub_status,
            "cancel_at_period_end": cancel_at_period_end,
            "has_stripe_customer": bool(user_doc.get("stripe_customer_id")),
            "is_staff": False,
            "is_privileged": False,
            "is_superadmin": False,
            "orgs_used": orgs_used,
            "orgs_limit": plan["orgs_included"] + addons_state["extra_orgs"],
            "period_start": start.isoformat(),
            "period_end": now.isoformat(),
            "this_month_spend_usd": round(spend, 4),
            "included_llm_budget_usd": included,
            "projected_overage_raw_usd": round(overage_raw, 4),
            "projected_overage_billable_usd": projected_overage_billable,
            "overage_markup": OVERAGE_MARKUP,
            "outstanding_overage": outstanding,
            "recent_transactions": txn_rows,
            "addons": addons_state,
            "addons_catalogue": _public_addons(),
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
        plan_prices = catalogue.get(plan["id"]) or {}
        price_id = plan_prices.get(body.cadence)
        if not price_id:
            raise HTTPException(
                503, "Billing is not fully configured for this plan yet. "
                "Please contact support.",
            )

        customer_id = await _get_or_create_customer(db, u)

        origin = body.origin_url.rstrip("/")
        success_url = f"{origin}/app/billing?session_id={{CHECKOUT_SESSION_ID}}"
        cancel_url = f"{origin}/app/billing?cancelled=1"

        cadence_amount = float(plan["monthly_usd"]) if body.cadence == "monthly" \
            else float(plan["annual_usd"]) * 12

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
                "cadence": body.cadence,
                "consent_text": body.consent_text[:500],
            },
            subscription_data={
                "metadata": {
                    "user_id": u["id"],
                    "plan_id": plan["id"],
                    "cadence": body.cadence,
                },
            },
            submit_type=None,
        )
        await db.payment_transactions.insert_one({
            "session_id": session.id,
            "user_id": u["id"],
            "kind": "subscription",
            "plan_id": plan["id"],
            "cadence": body.cadence,
            "amount_usd": cadence_amount,
            "currency": "usd",
            "consent_text": body.consent_text,
            "consent_ip": (request.client.host if request.client else None),
            "consent_at": datetime.now(timezone.utc),
            "payment_status": "initiated",
            "status": "pending",
            "created_at": datetime.now(timezone.utc),
        })
        return {"url": session.url, "session_id": session.id}

    # ── Add-on purchase (recurring OR hybrid one-time/invoice-item) ──
    @r.post("/billing/addons/checkout", status_code=200)
    async def addon_checkout(
        body: AddonCheckoutBody,
        request: Request,
        u: dict = Depends(get_current_user),
    ):
        """Buy an add-on. Three execution paths depending on the add-on's
        cadence + the user's subscription state:

          1. ``cadence=recurring`` → Stripe Checkout in subscription mode.
             A separate subscription is created for the add-on so each
             line item appears clean on the invoice; quantity is honored
             so a user can buy N extra orgs / seats in one click.
          2. ``cadence=one_time`` + user has active subscription →
             Invoice Item on the next recurring invoice. NO checkout
             redirect; the credit is provisionally added and finalised
             when invoice.paid lands. Cleaner UX for paying customers.
          3. ``cadence=one_time`` + user has no active subscription →
             Stripe Checkout in payment mode. One-time card form.

        Path 2's "provisional" credit is gated on the invoice paying —
        we set ``provisional_until_invoice=<invoice_id>`` and apply
        only when invoice.paid fires. If the invoice voids, the
        credit is never added.
        """
        addon = _addon_or_404(body.addon_id)
        if not _stripe_configured():
            raise HTTPException(503, "Billing is not configured (STRIPE_API_KEY missing).")

        # Enforce plan restrictions (e.g. extra_seat is Spark-only).
        restrictions = addon.get("plan_restrictions") or []
        if restrictions:
            user_doc = await _user_doc(db, u["id"]) or {}
            current_plan = user_doc.get("plan")
            if current_plan not in restrictions:
                raise HTTPException(
                    400,
                    f"This add-on is only available on {', '.join(restrictions)}. "
                    "Switch plans first or contact support.",
                )

        try:
            catalogue = await ensure_stripe_addon_catalogue()
        except stripe.AuthenticationError:
            raise HTTPException(
                503, "Billing is not fully configured — please contact support.",
            )
        price_id = catalogue.get(addon["id"])
        if not price_id:
            raise HTTPException(
                503, "Add-on is not configured in Stripe yet — please contact support.",
            )

        customer_id = await _get_or_create_customer(db, u)
        qty = max(1, int(body.quantity))
        # One-time packs always provision exactly one pack per "purchase"
        # so the credit-unit math stays predictable. Quantity-on-pack
        # would be ambiguous (10 audits × 3? or 3 packs?).
        if addon["cadence"] == "one_time":
            qty = 1
        origin = body.origin_url.rstrip("/")
        success_url = f"{origin}/app/billing?addon={addon['id']}&session_id={{CHECKOUT_SESSION_ID}}"
        cancel_url = f"{origin}/app/billing?addon_cancelled={addon['id']}"
        unit_amount = float(addon["price_usd"])
        consent_ip = request.client.host if request.client else None
        consent_at = datetime.now(timezone.utc)
        common_meta = {
            "kind": "addon",
            "user_id": u["id"],
            "addon_id": addon["id"],
            "apply_kind": addon["apply_kind"],
            "quantity": str(qty),
            "consent_text": body.consent_text[:500],
        }

        # ── Path 1: recurring add-on → subscription Checkout ─────────
        if addon["cadence"] == "recurring":
            _configure_stripe()
            session = await _stripe_call(
                stripe.checkout.Session.create,
                mode="subscription",
                customer=customer_id,
                line_items=[{"price": price_id, "quantity": qty}],
                success_url=success_url,
                cancel_url=cancel_url,
                allow_promotion_codes=False,
                billing_address_collection="auto",
                metadata=common_meta,
                subscription_data={"metadata": common_meta},
            )
            await db.payment_transactions.insert_one({
                "session_id": session.id,
                "user_id": u["id"],
                "kind": "addon",
                "addon_id": addon["id"],
                "cadence": "recurring",
                "quantity": qty,
                "amount_usd": unit_amount * qty,
                "currency": "usd",
                "consent_text": body.consent_text,
                "consent_ip": consent_ip,
                "consent_at": consent_at,
                "payment_status": "initiated",
                "status": "pending",
                "created_at": consent_at,
            })
            return {"mode": "checkout", "url": session.url, "session_id": session.id}

        # ── One-time pack: pick path 2 or path 3 ────────────────────
        # Hybrid logic: if the user has a CURRENT subscription, add an
        # invoice item to their next recurring invoice (path 2).
        # Otherwise fall back to a one-time Checkout (path 3).
        user_doc = await _user_doc(db, u["id"]) or {}
        sub_id = user_doc.get("subscription_id")
        sub_status = user_doc.get("subscription_status")
        has_active_sub = bool(
            sub_id and sub_status in ("active", "trialing", "past_due")
        )

        if has_active_sub:
            _configure_stripe()
            try:
                inv_item = await _stripe_call(
                    stripe.InvoiceItem.create,
                    customer=customer_id,
                    price=price_id,
                    quantity=1,
                    subscription=sub_id,
                    metadata=common_meta,
                    description=f"Foundry · {addon['name']}",
                )
            except Exception as exc:
                logger.exception("InvoiceItem.create failed: %s", exc)
                raise HTTPException(
                    502, "Couldn't attach the add-on to your invoice — try again.",
                )
            await db.payment_transactions.insert_one({
                "invoice_item_id": inv_item.id,
                "user_id": u["id"],
                "kind": "addon",
                "addon_id": addon["id"],
                "cadence": "one_time_on_invoice",
                "quantity": qty,
                "amount_usd": unit_amount,
                "currency": "usd",
                "consent_text": body.consent_text,
                "consent_ip": consent_ip,
                "consent_at": consent_at,
                "payment_status": "scheduled_on_next_invoice",
                "status": "pending",
                "subscription_id": sub_id,
                "created_at": consent_at,
            })
            return {
                "mode": "invoice_item",
                "invoice_item_id": inv_item.id,
                "message": (
                    f"{addon['name']} added to your next invoice. "
                    f"You'll be charged ${unit_amount:.2f} when it's issued."
                ),
            }

        # Path 3 — no active subscription, one-time Checkout.
        _configure_stripe()
        session = await _stripe_call(
            stripe.checkout.Session.create,
            mode="payment",
            customer=customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            allow_promotion_codes=False,
            billing_address_collection="auto",
            metadata=common_meta,
            payment_intent_data={"metadata": common_meta},
        )
        await db.payment_transactions.insert_one({
            "session_id": session.id,
            "user_id": u["id"],
            "kind": "addon",
            "addon_id": addon["id"],
            "cadence": "one_time_checkout",
            "quantity": 1,
            "amount_usd": unit_amount,
            "currency": "usd",
            "consent_text": body.consent_text,
            "consent_ip": consent_ip,
            "consent_at": consent_at,
            "payment_status": "initiated",
            "status": "pending",
            "created_at": consent_at,
        })
        return {"mode": "checkout", "url": session.url, "session_id": session.id}

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

    # ── Active add-on subscriptions ──────────────────────────────────
    # Recurring add-ons (extra_org, extra_seat) are stored as SEPARATE
    # Stripe subscriptions on the user's customer — not bundled into
    # the main plan sub. This endpoint surfaces them so the UI can
    # show each line item with its own cancel button.

    @r.get("/billing/addons/subscriptions")
    async def list_addon_subscriptions(
        u: dict = Depends(get_current_user),
    ):
        """Return the user's active recurring add-on subscriptions.

        We list directly off Stripe rather than our local DB because
        Stripe is authoritative on cancellation timing (proration,
        `cancel_at_period_end`, etc.). The local `payment_transactions`
        table only sees the INITIATED row, not the live state."""
        if not _stripe_configured():
            return {"subscriptions": []}
        user_doc = await _user_doc(db, u["id"]) or {}
        customer_id = user_doc.get("stripe_customer_id")
        if not customer_id:
            return {"subscriptions": []}
        _configure_stripe()
        try:
            subs = await _stripe_call(
                stripe.Subscription.list,
                customer=customer_id,
                status="all",
                limit=100,
                expand=["data.items.data.price"],
            )
        except Exception as exc:
            logger.warning("Stripe sub list failed for %s: %s", customer_id, exc)
            return {"subscriptions": []}

        catalogue = await ensure_stripe_addon_catalogue()
        price_to_addon = {v: k for k, v in catalogue.items()}
        out: List[Dict[str, Any]] = []
        for sub in (subs.data or []):
            sub_meta = (sub.metadata or {})
            if sub_meta.get("kind") != "addon":
                continue
            # Only surface live subs — canceled ones are noise.
            if sub.status in ("canceled", "incomplete_expired", "unpaid"):
                continue
            items = (sub.items.data if sub.items else []) or []
            if not items:
                continue
            first = items[0]
            price = first.price
            addon_id = sub_meta.get("addon_id") or price_to_addon.get(price.id)
            addon = ADDONS.get(addon_id) if addon_id else None
            if not addon:
                continue
            period_end = getattr(sub, "current_period_end", None)
            out.append({
                "subscription_id": sub.id,
                "addon_id": addon_id,
                "name": addon["name"],
                "unit": addon["unit"],
                "price_usd": float(addon["price_usd"]),
                "quantity": int(first.quantity or 1),
                "status": sub.status,
                "cancel_at_period_end": bool(getattr(sub, "cancel_at_period_end", False)),
                "current_period_end": (
                    datetime.fromtimestamp(period_end, tz=timezone.utc).isoformat()
                    if period_end else None
                ),
                "created_at": (
                    datetime.fromtimestamp(sub.created, tz=timezone.utc).isoformat()
                    if getattr(sub, "created", None) else None
                ),
            })
        # Newest first — matches how the UI lists them.
        out.sort(key=lambda s: s.get("created_at") or "", reverse=True)
        return {"subscriptions": out}

    @r.post("/billing/addons/{subscription_id}/cancel")
    async def cancel_addon_subscription(
        subscription_id: str,
        u: dict = Depends(get_current_user),
    ):
        """Cancel a single recurring add-on at the end of the current
        billing period. Customer keeps access until then (no proration
        clawback), matching how Spark / Temper itself cancels.

        Auth-guarded: we refuse to cancel a sub that isn't on this
        user's customer or isn't tagged as an add-on — protects
        against URL-tampering trying to nuke a paying customer's main
        plan sub via this endpoint."""
        if not _stripe_configured():
            raise HTTPException(503, "Billing is not configured.")
        user_doc = await _user_doc(db, u["id"]) or {}
        customer_id = user_doc.get("stripe_customer_id")
        if not customer_id:
            raise HTTPException(400, "No Stripe customer for this user.")
        _configure_stripe()
        try:
            sub = await _stripe_call(stripe.Subscription.retrieve, subscription_id)
        except Exception:
            raise HTTPException(404, "Subscription not found.")
        if sub.customer != customer_id:
            raise HTTPException(403, "This subscription belongs to a different account.")
        if (sub.metadata or {}).get("kind") != "addon":
            raise HTTPException(
                400,
                "Use the customer portal to cancel your main plan — "
                "this endpoint only manages add-on subscriptions.",
            )
        if getattr(sub, "cancel_at_period_end", False) or sub.status == "canceled":
            return {"ok": True, "status": sub.status,
                    "cancel_at_period_end": True,
                    "message": "Already scheduled for cancellation."}
        try:
            updated = await _stripe_call(
                stripe.Subscription.modify, subscription_id,
                cancel_at_period_end=True,
                metadata={**(sub.metadata or {}), "canceled_by_user": "true"},
            )
        except Exception as exc:
            logger.warning("Cancel add-on %s failed: %s", subscription_id, exc)
            raise HTTPException(502, "Couldn't schedule cancellation. Try again.")
        # Record the user-initiated cancel for audit. The actual counter
        # zero-out fires when Stripe sends customer.subscription.deleted
        # at period end — the user keeps access until then, which is the
        # correct UX.
        await db.payment_transactions.insert_one({
            "kind": "addon_cancel_scheduled",
            "user_id": u["id"],
            "subscription_id": subscription_id,
            "addon_id": (sub.metadata or {}).get("addon_id"),
            "status": "scheduled",
            "created_at": datetime.now(timezone.utc),
        })
        period_end = getattr(updated, "current_period_end", None)
        return {
            "ok": True,
            "status": updated.status,
            "cancel_at_period_end": True,
            "current_period_end": (
                datetime.fromtimestamp(period_end, tz=timezone.utc).isoformat()
                if period_end else None
            ),
            "message": "Cancellation scheduled — you keep access until the period ends.",
        }

    @r.post("/billing/addons/{subscription_id}/resume")
    async def resume_addon_subscription(
        subscription_id: str,
        u: dict = Depends(get_current_user),
    ):
        """Undo a 'cancel at period end' so the add-on renews normally.
        Same auth guards as cancel."""
        if not _stripe_configured():
            raise HTTPException(503, "Billing is not configured.")
        user_doc = await _user_doc(db, u["id"]) or {}
        customer_id = user_doc.get("stripe_customer_id")
        if not customer_id:
            raise HTTPException(400, "No Stripe customer for this user.")
        _configure_stripe()
        try:
            sub = await _stripe_call(stripe.Subscription.retrieve, subscription_id)
        except Exception:
            raise HTTPException(404, "Subscription not found.")
        if sub.customer != customer_id:
            raise HTTPException(403, "This subscription belongs to a different account.")
        if (sub.metadata or {}).get("kind") != "addon":
            raise HTTPException(400, "This endpoint only manages add-on subscriptions.")
        if sub.status == "canceled":
            raise HTTPException(
                400, "This subscription has already ended — purchase it again.",
            )
        try:
            updated = await _stripe_call(
                stripe.Subscription.modify, subscription_id,
                cancel_at_period_end=False,
            )
        except Exception as exc:
            logger.warning("Resume add-on %s failed: %s", subscription_id, exc)
            raise HTTPException(502, "Couldn't resume the subscription. Try again.")
        return {
            "ok": True,
            "status": updated.status,
            "cancel_at_period_end": False,
            "message": "Renewal turned back on.",
        }

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
            # Recurring add-ons live on their OWN subscriptions and are
            # tagged with metadata.kind=addon. They don't represent a
            # plan change so route them away from _apply_subscription_state
            # (which would clobber the user's main plan_id).
            meta = data_object.get("metadata") or {}
            if meta.get("kind") == "addon":
                addon_id = meta.get("addon_id")
                user_id = meta.get("user_id")
                items = (data_object.get("items") or {}).get("data") or []
                qty = int((items[0].get("quantity") if items else 1) or 1)
                if event_type == "customer.subscription.deleted" or \
                        data_object.get("status") in ("canceled", "incomplete_expired", "unpaid"):
                    qty = 0
                if addon_id and user_id:
                    await _apply_addon_purchase(
                        db, user_id=user_id, addon_id=addon_id, quantity=qty,
                        stripe_ref=f"sub:{data_object.get('id')}:{event_type}",
                        source="subscription",
                    )
            else:
                await _apply_subscription_state(db, data_object)
        elif event_type == "checkout.session.completed":
            # One-time pack purchase via Path 3 (no active sub at the time
            # of purchase). Path 1 (recurring add-on Checkout) flips the
            # subscription event above; we don't need to handle it here.
            meta = data_object.get("metadata") or {}
            if (
                meta.get("kind") == "addon"
                and data_object.get("mode") == "payment"
                and data_object.get("payment_status") == "paid"
            ):
                addon_id = meta.get("addon_id")
                user_id = meta.get("user_id")
                qty = int(meta.get("quantity") or 1)
                if addon_id and user_id:
                    await _apply_addon_purchase(
                        db, user_id=user_id, addon_id=addon_id, quantity=qty,
                        stripe_ref=f"cs:{data_object.get('id')}",
                        source="checkout",
                    )
                    await db.payment_transactions.update_one(
                        {"session_id": data_object.get("id")},
                        {"$set": {"status": "complete", "payment_status": "paid",
                                  "applied_at": datetime.now(timezone.utc)}},
                    )
        elif event_type in ("invoice.paid", "invoice.payment_failed"):
            # On invoice.paid, apply any add-on InvoiceItems on this invoice
            # (Path 2 — pack added to existing subscriber's next invoice).
            if event_type == "invoice.paid":
                lines = (data_object.get("lines") or {}).get("data") or []
                for line in lines:
                    line_meta = line.get("metadata") or {}
                    if line_meta.get("kind") != "addon":
                        continue
                    addon_id = line_meta.get("addon_id")
                    user_id = line_meta.get("user_id")
                    qty = int(line_meta.get("quantity") or 1)
                    if not (addon_id and user_id):
                        continue
                    await _apply_addon_purchase(
                        db, user_id=user_id, addon_id=addon_id, quantity=qty,
                        stripe_ref=f"inv:{data_object.get('id')}:{line.get('id')}",
                        source="invoice",
                    )
                    await db.payment_transactions.update_one(
                        {"invoice_item_id": line.get("invoice_item")
                                              or (line.get("parent") or {}).get("invoice_item_details", {}).get("invoice_item")},
                        {"$set": {"status": "complete", "payment_status": "paid",
                                  "applied_at": datetime.now(timezone.utc),
                                  "invoice_id": data_object.get("id")}},
                    )
            # On every invoice settlement Stripe also fires
            # subscription.updated — but we still log the invoice for
            # the customer-facing transaction list.
            sub_id = data_object.get("subscription")
            if sub_id:
                try:
                    sub = await _stripe_call(stripe.Subscription.retrieve, sub_id)
                    sub_dict = sub.to_dict()
                    sub_meta = sub_dict.get("metadata") or {}
                    if sub_meta.get("kind") != "addon":
                        await _apply_subscription_state(db, sub_dict)
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
