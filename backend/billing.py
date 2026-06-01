"""Foundry billing — Stripe Checkout + plan upgrades + metered overage.

This module implements **Phase B + Phase C** of the Foundry billing
roadmap:

  • **Phase B** — Stripe Checkout flow so an org owner can upgrade
    their plan (Spark → Temper → Refine), buy extra-seat add-ons,
    or settle their current overage bill in a single click. The
    Emergent Stripe integration is used so the same demo key works
    for every Emergent tenant without per-tenant configuration.

  • **Phase C** — A monthly aggregation job (run via APScheduler from
    `server.py`) that closes the previous month, computes each org's
    overage vs. its plan's included LLM budget, and persists an
    invoice row in `billing_overage_invoices`. Owners see the row
    in the Billing dashboard and can pay it via the same Checkout
    flow.

Design choices worth calling out:

  • We treat plan upgrades as **one-time monthly charges** rather
    than true Stripe subscriptions. The Emergent Stripe library
    only exposes one-time Checkout — true subscriptions would need
    direct Stripe SDK access, which is intentionally outside the
    Emergent demo-key sandbox. This is fine for the MVP: the
    `plan_paid_until` timestamp gates the org's effective plan and
    the next renewal is a single button click.

  • Overage pricing is a transparent **2× markup on raw LLM cost**.
    Aggressively low compared to typical SaaS markups (10–20×) — the
    point is that the user *sees* exactly what their agents cost and
    decides whether to pay it. We do not bill silently.

  • Every Stripe interaction is mirrored into a `payment_transactions`
    row before the redirect. The completion handler (`/api/webhook/
    stripe` *and* polling) is idempotent — applying the plan twice
    for the same `session_id` is a no-op.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ─── Pricing catalogue ────────────────────────────────────────────────────
#
# Single source of truth for plans + add-ons. Anyone editing pricing
# (marketing pages, billing dashboard, seat-policy warnings) should
# read from here so the numbers can never drift apart.
#
# `included_llm_budget_usd` is the dollar amount of LLM compute the org
# can burn each month before overage kicks in. It is intentionally
# generous on Refine — we want unlimited-seat customers to *use* the
# product, not nickel-and-dime them.

PLANS: Dict[str, Dict[str, Any]] = {
    "spark": {
        "id": "spark",
        "name": "Spark",
        "tagline": "Solo operator. One seat, one workspace.",
        "monthly_usd": 80.0,
        "seats_included": 1,
        "extra_seat_usd": 30.0,
        "included_llm_budget_usd": 20.0,
        "features": [
            "1 seat included ($30/mo per extra seat)",
            "$20/mo of agent compute included",
            "Unlimited org notes + asset library",
            "Pay-as-you-go for overages",
        ],
    },
    "temper": {
        "id": "temper",
        "name": "Temper",
        "tagline": "Growing team. Five seats, shared agents.",
        "monthly_usd": 300.0,
        "seats_included": 5,
        "extra_seat_usd": 40.0,
        "included_llm_budget_usd": 100.0,
        "features": [
            "5 seats included ($40/mo per extra seat)",
            "$100/mo of agent compute included",
            "Crucible (mission agents) unlocked",
            "Recurring scheduled missions",
        ],
    },
    "refine": {
        "id": "refine",
        "name": "Refine",
        "tagline": "Agency-scale. Unlimited seats, white-glove.",
        "monthly_usd": 1000.0,
        "seats_included": 10_000,
        "extra_seat_usd": 0.0,
        "included_llm_budget_usd": 500.0,
        "features": [
            "Unlimited seats",
            "$500/mo of agent compute included",
            "White-label audit PDFs",
            "Priority support + onboarding",
        ],
    },
}

# Overage multiplier — applied to the raw LLM cost we logged for the
# customer's month. Conservative (2×) so customers don't feel gouged.
# Tune via env if needed without redeploying.
OVERAGE_MARKUP = float(os.environ.get("FOUNDRY_OVERAGE_MARKUP", "2.0"))


# ─── Models ───────────────────────────────────────────────────────────────


class CheckoutKind:
    """String constants for `payment_transactions.kind`. Stringly-typed
    so the value also serves as a human-readable column in Mongo."""
    PLAN = "plan"
    SEATS = "seats"
    OVERAGE = "overage"


class CheckoutBody(BaseModel):
    """Frontend → backend payload. We *never* take amount from the
    client. Amount is resolved server-side from the pricing catalogue.

    `origin_url` is required so the success/cancel URLs route back to
    the caller's environment (preview vs. production)."""
    origin_url: str = Field(..., description="window.location.origin from the browser")


class PlanCheckoutBody(CheckoutBody):
    plan_id: str = Field(..., description="One of: spark, temper, refine")


class SeatCheckoutBody(CheckoutBody):
    quantity: int = Field(1, ge=1, le=50, description="Extra seats to add for one month")


# ─── Helpers ──────────────────────────────────────────────────────────────


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if not isinstance(dt, datetime):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


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


async def _org_plan(db, org_id: str) -> str:
    """Current plan id for the org. Defaults to spark if unset."""
    try:
        org_oid = ObjectId(org_id)
    except Exception:
        return "spark"
    org = await db.organizations.find_one({"_id": org_oid}, {"plan": 1})
    return (org or {}).get("plan") or "spark"


async def _outstanding_overage_invoice(db, org_id: str) -> Optional[Dict[str, Any]]:
    """Return the most recent unpaid overage invoice for the org, if any."""
    row = await db.billing_overage_invoices.find_one(
        {"org_id": org_id, "status": {"$in": ["open", "pending"]}},
        sort=[("period_end", -1)],
        projection={"_id": 0},
    )
    return row


# ─── Phase C — monthly overage aggregation ────────────────────────────────


async def aggregate_overages_for_previous_month(db) -> List[Dict[str, Any]]:
    """Close out last month: for every org that exceeded its plan's
    included LLM budget, write an invoice row.

    Idempotent — running twice for the same month does not double-bill.
    Called from the APScheduler cron in server.py on day 1 at 02:00 UTC.

    Returns the list of invoices created/updated for logging.
    """
    prev_start, cur_start = _previous_month_window()
    period_key = prev_start.strftime("%Y-%m")
    invoices: List[Dict[str, Any]] = []

    # We aggregate by org_id so we only touch each org once per month.
    cur = db.llm_calls.aggregate([
        {"$match": {"created_at": {"$gte": prev_start, "$lt": cur_start}}},
        {"$group": {"_id": "$org_id", "cost_usd": {"$sum": "$cost_usd"}}},
    ])
    rows = await cur.to_list(10_000)
    for r in rows:
        org_id = r.get("_id")
        if not org_id:
            continue
        raw_cost = float(r.get("cost_usd") or 0.0)
        plan = await _org_plan(db, org_id)
        included = PLANS.get(plan, PLANS["spark"])["included_llm_budget_usd"]
        if raw_cost <= included:
            continue  # within plan — no invoice
        overage_raw = raw_cost - included
        billable = round(overage_raw * OVERAGE_MARKUP, 2)
        invoice = {
            "org_id": org_id,
            "period_key": period_key,
            "period_start": prev_start,
            "period_end": cur_start,
            "plan": plan,
            "raw_cost_usd": round(raw_cost, 4),
            "included_usd": included,
            "overage_raw_usd": round(overage_raw, 4),
            "markup": OVERAGE_MARKUP,
            "amount_due_usd": billable,
            "status": "open",
            "created_at": datetime.now(timezone.utc),
        }
        # Upsert by (org_id, period_key) so re-runs replace the open row
        # but do NOT touch already-paid invoices.
        await db.billing_overage_invoices.update_one(
            {"org_id": org_id, "period_key": period_key, "status": {"$ne": "paid"}},
            {"$set": invoice, "$setOnInsert": {"first_seen_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        invoices.append({**invoice, "period_start": invoice["period_start"].isoformat(),
                         "period_end": invoice["period_end"].isoformat(),
                         "created_at": invoice["created_at"].isoformat()})
    logger.info("Overage aggregation closed %s: %d invoices written", period_key, len(invoices))
    return invoices


# ─── Router factory ───────────────────────────────────────────────────────


def make_billing_router(
    *,
    db,
    get_current_user,
    require_org_member,
) -> APIRouter:
    """Build the billing router. Mounted at /api/billing and /api/orgs/{id}/billing."""
    r = APIRouter(prefix="/api")
    api_key = os.environ.get("STRIPE_API_KEY", "")

    # Lazy import — the integration library is heavy and we don't want
    # to crash boot if the lib is missing in a constrained environment.
    def _checkout_client(request: Request):
        from emergentintegrations.payments.stripe.checkout import StripeCheckout
        host_url = str(request.base_url).rstrip("/")
        webhook_url = f"{host_url}/api/webhook/stripe"
        return StripeCheckout(api_key=api_key, webhook_url=webhook_url)

    # ── Catalogue ────────────────────────────────────────────────────
    @r.get("/billing/plans")
    async def list_plans():
        """Public catalogue — used by the pricing card and the Billing
        page. No auth required so we can surface this on marketing
        surfaces later without a refactor."""
        return {
            "plans": list(PLANS.values()),
            "overage_markup": OVERAGE_MARKUP,
        }

    # ── Per-org billing summary ─────────────────────────────────────
    @r.get("/orgs/{org_id}/billing/summary")
    async def billing_summary(org_id: str, u: dict = Depends(get_current_user)):
        """Single-call dashboard payload: current plan, this-month
        spend vs included budget, projected overage, outstanding
        invoice (if any), and last 5 transactions."""
        ctx = await require_org_member(org_id, u["id"])
        is_admin = ctx["member"]["role"] in ("owner", "admin")
        if not is_admin:
            raise HTTPException(403, "Only owners/admins can view billing.")

        plan_id = await _org_plan(db, org_id)
        plan = PLANS.get(plan_id, PLANS["spark"])

        start, now = _month_window()
        spend = await _org_llm_spend(db, org_id, start, now)
        included = plan["included_llm_budget_usd"]
        overage_raw = max(0.0, spend - included)
        projected_overage_billable = round(overage_raw * OVERAGE_MARKUP, 2)

        # Plan paid status — `plan_paid_until` is set when a plan
        # Checkout succeeds. If unset (legacy/free trial), we still
        # surface the plan but mark it `unpaid` so the UI nudges
        # the owner to confirm payment.
        try:
            org_oid = ObjectId(org_id)
            org = await db.organizations.find_one(
                {"_id": org_oid},
                {"plan_paid_until": 1, "extra_seats_paid_until": 1, "extra_seats_quantity": 1},
            )
        except Exception:
            org = {}
        paid_until = _aware((org or {}).get("plan_paid_until"))
        plan_status = "paid" if paid_until and paid_until > now else "unpaid"

        outstanding = await _outstanding_overage_invoice(db, org_id)
        if outstanding:
            outstanding["period_start"] = outstanding["period_start"].isoformat()
            outstanding["period_end"] = outstanding["period_end"].isoformat()
            outstanding["created_at"] = outstanding["created_at"].isoformat()

        # Recent transactions for the audit log.
        txn_rows = await db.payment_transactions.find(
            {"org_id": org_id},
            sort=[("created_at", -1)],
            projection={"_id": 0},
        ).to_list(10)
        for t in txn_rows:
            for k in ("created_at", "updated_at"):
                if isinstance(t.get(k), datetime):
                    t[k] = t[k].isoformat()

        return {
            "plan": plan,
            "plan_status": plan_status,
            "plan_paid_until": paid_until.isoformat() if paid_until else None,
            "period_start": start.isoformat(),
            "period_end": now.isoformat(),
            "this_month_spend_usd": round(spend, 4),
            "included_llm_budget_usd": included,
            "projected_overage_raw_usd": round(overage_raw, 4),
            "projected_overage_billable_usd": projected_overage_billable,
            "overage_markup": OVERAGE_MARKUP,
            "extra_seats_quantity": (org or {}).get("extra_seats_quantity") or 0,
            "outstanding_overage": outstanding,
            "recent_transactions": txn_rows,
        }

    # ── Plan upgrade Checkout ───────────────────────────────────────
    @r.post("/orgs/{org_id}/billing/checkout/plan")
    async def checkout_plan(
        org_id: str,
        body: PlanCheckoutBody,
        request: Request,
        u: dict = Depends(get_current_user),
    ):
        ctx = await require_org_member(org_id, u["id"])
        if ctx["member"]["role"] not in ("owner", "admin"):
            raise HTTPException(403, "Only owners/admins can upgrade the plan.")
        plan = PLANS.get(body.plan_id)
        if not plan:
            raise HTTPException(400, f"Unknown plan: {body.plan_id}")
        if not api_key:
            raise HTTPException(503, "Billing is not configured (STRIPE_API_KEY missing).")

        amount = float(plan["monthly_usd"])
        origin = body.origin_url.rstrip("/")
        success_url = f"{origin}/billing?session_id={{CHECKOUT_SESSION_ID}}"
        cancel_url = f"{origin}/billing?cancelled=1"

        from emergentintegrations.payments.stripe.checkout import CheckoutSessionRequest
        client = _checkout_client(request)
        session = await client.create_checkout_session(CheckoutSessionRequest(
            amount=amount,
            currency="usd",
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "kind": CheckoutKind.PLAN,
                "org_id": org_id,
                "plan_id": plan["id"],
                "user_id": u["id"],
            },
        ))
        # Mirror to payment_transactions BEFORE redirect — required by
        # the Emergent payments playbook so we can reconcile failures.
        await db.payment_transactions.insert_one({
            "session_id": session.session_id,
            "org_id": org_id,
            "user_id": u["id"],
            "kind": CheckoutKind.PLAN,
            "plan_id": plan["id"],
            "amount_usd": amount,
            "currency": "usd",
            "payment_status": "initiated",
            "status": "pending",
            "created_at": datetime.now(timezone.utc),
        })
        return {"url": session.url, "session_id": session.session_id}

    # ── Extra-seats Checkout ────────────────────────────────────────
    @r.post("/orgs/{org_id}/billing/checkout/seats")
    async def checkout_seats(
        org_id: str,
        body: SeatCheckoutBody,
        request: Request,
        u: dict = Depends(get_current_user),
    ):
        ctx = await require_org_member(org_id, u["id"])
        if ctx["member"]["role"] not in ("owner", "admin"):
            raise HTTPException(403, "Only owners/admins can buy extra seats.")
        plan_id = await _org_plan(db, org_id)
        plan = PLANS.get(plan_id, PLANS["spark"])
        seat_price = float(plan.get("extra_seat_usd") or 0.0)
        if seat_price <= 0:
            raise HTTPException(400, f"Plan {plan_id} has no per-seat add-on.")
        if not api_key:
            raise HTTPException(503, "Billing is not configured (STRIPE_API_KEY missing).")

        amount = round(seat_price * body.quantity, 2)
        origin = body.origin_url.rstrip("/")
        success_url = f"{origin}/billing?session_id={{CHECKOUT_SESSION_ID}}"
        cancel_url = f"{origin}/billing?cancelled=1"

        from emergentintegrations.payments.stripe.checkout import CheckoutSessionRequest
        client = _checkout_client(request)
        session = await client.create_checkout_session(CheckoutSessionRequest(
            amount=amount,
            currency="usd",
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "kind": CheckoutKind.SEATS,
                "org_id": org_id,
                "plan_id": plan_id,
                "seats": str(body.quantity),
                "user_id": u["id"],
            },
        ))
        await db.payment_transactions.insert_one({
            "session_id": session.session_id,
            "org_id": org_id,
            "user_id": u["id"],
            "kind": CheckoutKind.SEATS,
            "plan_id": plan_id,
            "seats": body.quantity,
            "amount_usd": amount,
            "currency": "usd",
            "payment_status": "initiated",
            "status": "pending",
            "created_at": datetime.now(timezone.utc),
        })
        return {"url": session.url, "session_id": session.session_id}

    # ── Pay outstanding overage ─────────────────────────────────────
    @r.post("/orgs/{org_id}/billing/checkout/overage")
    async def checkout_overage(
        org_id: str,
        body: CheckoutBody,
        request: Request,
        u: dict = Depends(get_current_user),
    ):
        ctx = await require_org_member(org_id, u["id"])
        if ctx["member"]["role"] not in ("owner", "admin"):
            raise HTTPException(403, "Only owners/admins can settle overage.")
        invoice = await _outstanding_overage_invoice(db, org_id)
        if not invoice:
            raise HTTPException(404, "No outstanding overage invoice.")
        amount = float(invoice["amount_due_usd"])
        if amount <= 0:
            raise HTTPException(400, "Invoice amount is zero.")
        if not api_key:
            raise HTTPException(503, "Billing is not configured (STRIPE_API_KEY missing).")

        origin = body.origin_url.rstrip("/")
        success_url = f"{origin}/billing?session_id={{CHECKOUT_SESSION_ID}}"
        cancel_url = f"{origin}/billing?cancelled=1"

        from emergentintegrations.payments.stripe.checkout import CheckoutSessionRequest
        client = _checkout_client(request)
        session = await client.create_checkout_session(CheckoutSessionRequest(
            amount=amount,
            currency="usd",
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "kind": CheckoutKind.OVERAGE,
                "org_id": org_id,
                "period_key": invoice["period_key"],
                "user_id": u["id"],
            },
        ))
        await db.payment_transactions.insert_one({
            "session_id": session.session_id,
            "org_id": org_id,
            "user_id": u["id"],
            "kind": CheckoutKind.OVERAGE,
            "period_key": invoice["period_key"],
            "amount_usd": amount,
            "currency": "usd",
            "payment_status": "initiated",
            "status": "pending",
            "created_at": datetime.now(timezone.utc),
        })
        return {"url": session.url, "session_id": session.session_id}

    # ── Polling endpoint ────────────────────────────────────────────
    @r.get("/billing/status/{session_id}")
    async def checkout_status(session_id: str, request: Request, u: dict = Depends(get_current_user)):
        """Poll the Stripe session status and — exactly once per
        session — apply the side-effect (upgrade plan, add seats,
        mark invoice paid). The side-effect is gated by a
        `payment_status == 'paid'` flag on the transaction so
        concurrent pollers and the webhook can't double-apply.
        """
        txn = await db.payment_transactions.find_one({"session_id": session_id}, {"_id": 0})
        if not txn:
            raise HTTPException(404, "Unknown checkout session.")
        # Soft authorization — only members of the org that started
        # the checkout can poll its status.
        try:
            await require_org_member(txn["org_id"], u["id"])
        except HTTPException:
            raise HTTPException(403, "Not your org's checkout.")

        if txn.get("payment_status") == "paid":
            # Already reconciled — return cached status.
            return {
                "status": "complete",
                "payment_status": "paid",
                "applied": True,
                "kind": txn.get("kind"),
                "amount_usd": txn.get("amount_usd"),
            }

        if not api_key:
            raise HTTPException(503, "Billing is not configured.")
        from emergentintegrations.payments.stripe.checkout import StripeCheckout
        client = StripeCheckout(api_key=api_key, webhook_url=f"{str(request.base_url).rstrip('/')}/api/webhook/stripe")
        status = await client.get_checkout_status(session_id)
        applied = False
        if status.payment_status == "paid":
            applied = await _apply_paid_transaction(db, txn, status.metadata or {})
        await db.payment_transactions.update_one(
            {"session_id": session_id},
            {"$set": {
                "payment_status": status.payment_status,
                "status": status.status,
                "amount_total_cents": status.amount_total,
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        return {
            "status": status.status,
            "payment_status": status.payment_status,
            "applied": applied,
            "kind": txn.get("kind"),
            "amount_usd": txn.get("amount_usd"),
        }

    # ── Webhook ─────────────────────────────────────────────────────
    @r.post("/webhook/stripe")
    async def stripe_webhook(request: Request):
        if not api_key:
            return {"received": False, "reason": "billing-not-configured"}
        from emergentintegrations.payments.stripe.checkout import StripeCheckout
        client = StripeCheckout(api_key=api_key, webhook_url=f"{str(request.base_url).rstrip('/')}/api/webhook/stripe")
        body = await request.body()
        signature = request.headers.get("Stripe-Signature", "")
        try:
            event = await client.handle_webhook(body, signature)
        except Exception as exc:
            logger.exception("Stripe webhook handler failed: %s", exc)
            raise HTTPException(400, "Invalid webhook payload.")
        # Only react to terminal "paid" events. Everything else is
        # informational (we already saw the session_id at create time).
        if event.payment_status == "paid" and event.session_id:
            txn = await db.payment_transactions.find_one({"session_id": event.session_id}, {"_id": 0})
            if txn and txn.get("payment_status") != "paid":
                await _apply_paid_transaction(db, txn, event.metadata or {})
                await db.payment_transactions.update_one(
                    {"session_id": event.session_id},
                    {"$set": {
                        "payment_status": "paid",
                        "status": "complete",
                        "updated_at": datetime.now(timezone.utc),
                    }},
                )
        return {"received": True, "event_type": event.event_type}

    return r


# ─── Side-effect application ─────────────────────────────────────────────


async def _apply_paid_transaction(db, txn: Dict[str, Any], metadata: Dict[str, str]) -> bool:
    """Idempotently apply the consequence of a paid Checkout.

    Returns True when the side-effect ran for the first time, False
    when it was already applied (so the caller can avoid double-
    notifying the UI).
    """
    if txn.get("payment_status") == "paid":
        return False

    org_id = txn.get("org_id")
    kind = txn.get("kind")
    now = datetime.now(timezone.utc)
    # 31 days keeps us safely past the longest calendar month.
    next_renewal = now + timedelta(days=31)

    if kind == CheckoutKind.PLAN:
        plan_id = txn.get("plan_id") or metadata.get("plan_id")
        if plan_id and plan_id in PLANS:
            try:
                org_oid = ObjectId(org_id)
                await db.organizations.update_one(
                    {"_id": org_oid},
                    {"$set": {
                        "plan": plan_id,
                        "plan_paid_until": next_renewal,
                        "plan_paid_at": now,
                    }},
                )
                logger.info("Org %s upgraded to %s (paid until %s)", org_id, plan_id, next_renewal.isoformat())
            except Exception as exc:
                logger.exception("Failed to apply plan upgrade: %s", exc)
                return False

    elif kind == CheckoutKind.SEATS:
        seats = int(txn.get("seats") or 1)
        try:
            org_oid = ObjectId(org_id)
            await db.organizations.update_one(
                {"_id": org_oid},
                {
                    "$inc": {"extra_seats_quantity": seats},
                    "$set": {"extra_seats_paid_until": next_renewal, "extra_seats_paid_at": now},
                },
            )
            logger.info("Org %s purchased %d extra seats", org_id, seats)
        except Exception as exc:
            logger.exception("Failed to apply seat purchase: %s", exc)
            return False

    elif kind == CheckoutKind.OVERAGE:
        period_key = txn.get("period_key") or metadata.get("period_key")
        if period_key:
            await db.billing_overage_invoices.update_one(
                {"org_id": org_id, "period_key": period_key},
                {"$set": {"status": "paid", "paid_at": now, "paid_session_id": txn.get("session_id")}},
            )
            logger.info("Org %s settled overage for %s", org_id, period_key)

    # Mark the txn paid so we never re-apply.
    await db.payment_transactions.update_one(
        {"session_id": txn.get("session_id")},
        {"$set": {"payment_status": "paid", "applied_at": now, "status": "complete"}},
    )
    return True
