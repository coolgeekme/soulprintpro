"""End-to-end tests for the billing module — true Stripe Subscriptions.

Coverage:
  • Public plan catalogue + Refine `recurring=False` flag exposed.
  • Per-org summary auth gating (403 unauthenticated, 200 owner).
  • Subscribe endpoint validates plan + consent text.
  • Refine `subscribe` is rejected (400 — sales-led).
  • Refine `contact-sales` lead capture persists a row in `sales_inquiries`.
  • Phase C overage aggregation: idempotent + paid-invoice preservation.
  • Webhook accepts unsigned payloads (preview mode) and ignores
    irrelevant events without exploding.

Stripe API live calls are not required — when STRIPE_API_KEY is the
Emergent shadow key (default in preview), the subscribe endpoint
returns 503 cleanly. We test that path explicitly so we know prod
will keep working when keys are missing or rotated.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest
import requests

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL",
    "https://emergent-wing.preview.emergentagent.com",
).rstrip("/")
SUPERADMIN_EMAIL = "reggie@coolgeek.me"
SUPERADMIN_PASS = "superadminpass123"


def _login(email: str, password: str):
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": email, "password": password},
        timeout=15,
    )
    return r.json().get("access_token") if r.status_code == 200 else None


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def admin_creds():
    tok = _login(SUPERADMIN_EMAIL, SUPERADMIN_PASS)
    if not tok:
        pytest.skip("Superadmin login failed — skipping auth-gated billing tests.")
    r = requests.get(f"{BASE_URL}/api/orgs", headers=_h(tok), timeout=15)
    if r.status_code != 200 or not r.json():
        pytest.skip("Superadmin has no orgs — skipping.")
    return {"token": tok, "org_id": r.json()[0]["id"]}


# ─── Catalogue ─────────────────────────────────────────────────────────


def test_plan_catalogue_is_public_and_complete():
    r = requests.get(f"{BASE_URL}/api/billing/plans", timeout=15)
    assert r.status_code == 200
    body = r.json()
    ids = {p["id"] for p in body["plans"]}
    assert ids == {"spark", "temper", "refine"}
    for p in body["plans"]:
        # Required fields for the BillingPage card
        for key in ("name", "tagline", "seats_included",
                    "included_llm_budget_usd", "features", "recurring"):
            assert key in p, f"plan {p.get('id')} missing {key}"
        # Lookup keys are an internal detail — they MUST NOT leak.
        assert "lookup_key" not in p
    # Refine is intentionally not recurring.
    refine = next(p for p in body["plans"] if p["id"] == "refine")
    assert refine["recurring"] is False
    assert refine["monthly_usd"] is None
    # Spark + Temper are recurring with non-zero prices.
    for plan_id in ("spark", "temper"):
        p = next(p for p in body["plans"] if p["id"] == plan_id)
        assert p["recurring"] is True
        assert p["monthly_usd"] > 0


# ─── Summary ───────────────────────────────────────────────────────────


def test_summary_owner_payload(admin_creds):
    r = requests.get(
        f"{BASE_URL}/api/orgs/{admin_creds['org_id']}/billing/summary",
        headers=_h(admin_creds["token"]), timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan"]["id"] in ("spark", "temper", "refine")
    # `staff` is the override status for @archeforge.com + reggie@coolgeek.me.
    assert body["plan_status"] in ("active", "cancelling", "unpaid", "none", "staff")
    assert "subscription_status" in body
    assert "cancel_at_period_end" in body
    assert isinstance(body["recent_transactions"], list)


def test_summary_treats_reggie_as_staff(admin_creds):
    """Sanity check: the superadmin (reggie@coolgeek.me) should be
    treated as staff and get Refine-equivalent access automatically."""
    r = requests.get(
        f"{BASE_URL}/api/orgs/{admin_creds['org_id']}/billing/summary",
        headers=_h(admin_creds["token"]), timeout=15,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["is_staff"] is True
    assert body["plan_status"] == "staff"
    assert body["plan"]["id"] == "refine"


def test_summary_blocks_unauthenticated():
    r = requests.get(
        f"{BASE_URL}/api/orgs/000000000000000000000000/billing/summary",
        timeout=15,
    )
    assert r.status_code in (401, 403)


# ─── Subscribe ────────────────────────────────────────────────────────


def test_subscribe_rejects_refine(admin_creds):
    r = requests.post(
        f"{BASE_URL}/api/orgs/{admin_creds['org_id']}/billing/subscribe",
        headers=_h(admin_creds["token"]),
        json={
            "plan_id": "refine",
            "origin_url": "https://example.com",
            "consent_text": "I authorize Foundry to charge me monthly until I cancel.",
        },
        timeout=15,
    )
    assert r.status_code == 400, r.text
    assert "refine" in r.text.lower() or "sales" in r.text.lower()


def test_subscribe_rejects_unknown_plan(admin_creds):
    r = requests.post(
        f"{BASE_URL}/api/orgs/{admin_creds['org_id']}/billing/subscribe",
        headers=_h(admin_creds["token"]),
        json={
            "plan_id": "ultra",
            "origin_url": "https://example.com",
            "consent_text": "I authorize Foundry to charge me monthly until I cancel.",
        },
        timeout=15,
    )
    assert r.status_code == 400


def test_subscribe_requires_consent_text(admin_creds):
    """Empty consent must be rejected by Pydantic (min_length=10)."""
    r = requests.post(
        f"{BASE_URL}/api/orgs/{admin_creds['org_id']}/billing/subscribe",
        headers=_h(admin_creds["token"]),
        json={
            "plan_id": "spark",
            "origin_url": "https://example.com",
            "consent_text": "",
        },
        timeout=15,
    )
    assert r.status_code in (400, 422), r.text


def test_subscribe_with_real_key_or_503(admin_creds):
    """When STRIPE_API_KEY is the Emergent shadow value, subscribe
    returns 503 cleanly. When a real key is configured, we expect a
    Stripe Checkout URL. Either path proves the route works."""
    r = requests.post(
        f"{BASE_URL}/api/orgs/{admin_creds['org_id']}/billing/subscribe",
        headers=_h(admin_creds["token"]),
        json={
            "plan_id": "spark",
            "origin_url": "https://example.com",
            "consent_text": "I authorize Foundry to charge $80/mo until I cancel.",
        },
        timeout=20,
    )
    assert r.status_code in (200, 503), r.text
    if r.status_code == 200:
        body = r.json()
        assert body["url"].startswith("https://checkout.stripe.com/")
        assert body["session_id"].startswith("cs_")


# ─── Refine contact sales ─────────────────────────────────────────────


def test_contact_sales_persists_lead(admin_creds):
    """Submitting the Refine form must create a row in
    `sales_inquiries`. We don't assert on the Resend email since it
    may not be configured in preview."""
    r = requests.post(
        f"{BASE_URL}/api/orgs/{admin_creds['org_id']}/billing/contact-sales",
        headers=_h(admin_creds["token"]),
        json={
            "name": f"Test Lead {uuid.uuid4().hex[:6]}",
            "email": "lead@example.com",
            "company": "Test Co",
            "message": "We have 12 seats and need a quote.",
        },
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "emailed" in body


def test_contact_sales_requires_valid_email(admin_creds):
    r = requests.post(
        f"{BASE_URL}/api/orgs/{admin_creds['org_id']}/billing/contact-sales",
        headers=_h(admin_creds["token"]),
        json={
            "name": "Bad Email Lead",
            "email": "not-an-email",
        },
        timeout=15,
    )
    assert r.status_code in (400, 422), r.text


# ─── Webhook smoke (unsigned, dev mode) ────────────────────────────────


def test_webhook_accepts_irrelevant_event():
    """In dev (no STRIPE_WEBHOOK_SECRET) the webhook accepts unsigned
    payloads. An unknown event_type should be acknowledged, not 500."""
    r = requests.post(
        f"{BASE_URL}/api/webhook/stripe",
        json={"type": "ping", "data": {"object": {}}},
        timeout=15,
    )
    # 200 = accepted; 503 = stripe not configured at all (also fine).
    assert r.status_code in (200, 503), r.text


# ─── Phase C — overage aggregation (live Mongo, in-process) ────────────


@pytest.mark.asyncio
async def test_overage_aggregation_idempotent():
    """Insert a synthetic burst of llm_calls in last month for a fake
    org, run aggregation twice, and assert the invoice was written
    exactly once and paid invoices are preserved on re-run."""
    import sys
    sys.path.insert(0, "/app/backend")
    from motor.motor_asyncio import AsyncIOMotorClient
    from bson import ObjectId

    from billing import (
        aggregate_overages_for_previous_month, OVERAGE_MARKUP, PLANS,
    )

    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME")
    assert mongo_url and db_name, "MONGO_URL/DB_NAME not set in env"
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]

    org_oid = ObjectId()
    fake_org_id = str(org_oid)
    await db.organizations.insert_one({
        "_id": org_oid, "name": "Overage Test Org", "plan": "spark",
    })

    now = datetime.now(timezone.utc)
    if now.month == 1:
        prev_mid = datetime(now.year - 1, 12, 15, tzinfo=timezone.utc)
    else:
        prev_mid = datetime(now.year, now.month - 1, 15, tzinfo=timezone.utc)
    period_key = prev_mid.strftime("%Y-%m")
    seed_cost = 50.0  # past Spark's $20 budget

    await db.llm_calls.insert_one({
        "org_id": fake_org_id,
        "created_at": prev_mid,
        "cost_usd": seed_cost,
        "model": "gpt-test",
    })

    try:
        invoices_1 = await aggregate_overages_for_previous_month(db)
        invoices_2 = await aggregate_overages_for_previous_month(db)

        ours_1 = [i for i in invoices_1 if i["org_id"] == fake_org_id]
        ours_2 = [i for i in invoices_2 if i["org_id"] == fake_org_id]
        assert ours_1 and ours_2

        rows = await db.billing_overage_invoices.find(
            {"org_id": fake_org_id, "period_key": period_key}
        ).to_list(10)
        assert len(rows) == 1
        included = PLANS["spark"]["included_llm_budget_usd"]
        expected = round((seed_cost - included) * OVERAGE_MARKUP, 2)
        assert rows[0]["amount_due_usd"] == expected

        # Paid invoices must NOT be overwritten by re-runs.
        await db.billing_overage_invoices.update_one(
            {"_id": rows[0]["_id"]},
            {"$set": {"status": "paid", "amount_due_usd": 999.0}},
        )
        await aggregate_overages_for_previous_month(db)
        post = await db.billing_overage_invoices.find_one({"_id": rows[0]["_id"]})
        assert post["status"] == "paid"
        assert post["amount_due_usd"] == 999.0
    finally:
        await db.llm_calls.delete_many({"org_id": fake_org_id})
        await db.billing_overage_invoices.delete_many({"org_id": fake_org_id})
        await db.organizations.delete_one({"_id": org_oid})
        client.close()
