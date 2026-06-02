"""End-to-end tests for user-level billing.

Subscriptions are scoped to USERS (not orgs). One subscription
unlocks N workspaces per the plan's `orgs_included`. Test coverage:
  • Public catalogue exposes `orgs_included` and Refine is non-recurring.
  • Per-user summary (auth-gated) returns rolled-up spend, plan,
    orgs_used/orgs_limit.
  • Subscribe endpoint validates plan + consent text + rejects Refine.
  • Contact-sales lead capture persists a row and emails the team.
  • Phase C overage aggregation: idempotent, paid-invoice preservation,
    rolls up per-user across all owned orgs.
  • Org-creation gate: 402 returned when user is at their plan's limit.
"""
from __future__ import annotations

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
        pytest.skip("Superadmin login failed.")
    return {"token": tok}


# ─── Catalogue ────────────────────────────────────────────────────────


def test_plan_catalogue_exposes_org_limits():
    r = requests.get(f"{BASE_URL}/api/billing/plans", timeout=15)
    assert r.status_code == 200
    body = r.json()
    by_id = {p["id"]: p for p in body["plans"]}
    assert set(by_id.keys()) == {"spark", "temper", "refine"}
    for plan_id in ("spark", "temper", "refine"):
        p = by_id[plan_id]
        for key in ("name", "tagline", "orgs_included", "seats_included",
                    "included_llm_budget_usd", "features", "recurring"):
            assert key in p, f"plan {plan_id} missing {key}"
    # Hard requirement from product: Spark=1, Temper=3, Refine=unlimited.
    assert by_id["spark"]["orgs_included"] == 1
    assert by_id["temper"]["orgs_included"] == 3
    assert by_id["refine"]["orgs_included"] >= 1000
    assert by_id["refine"]["recurring"] is False
    assert by_id["refine"]["monthly_usd"] is None
    # Internal lookup_key MUST NOT leak.
    for p in body["plans"]:
        assert "lookup_key" not in p


# ─── Summary ─────────────────────────────────────────────────────────


def test_summary_requires_auth():
    r = requests.get(f"{BASE_URL}/api/billing/summary", timeout=15)
    assert r.status_code in (401, 403)


def test_summary_returns_user_level_payload(admin_creds):
    r = requests.get(
        f"{BASE_URL}/api/billing/summary",
        headers=_h(admin_creds["token"]), timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan"]["id"] in ("spark", "temper", "refine")
    assert body["plan_status"] in ("active", "cancelling", "unpaid", "none", "staff")
    assert "orgs_used" in body and "orgs_limit" in body
    assert isinstance(body["recent_transactions"], list)


def test_summary_treats_reggie_as_staff(admin_creds):
    r = requests.get(
        f"{BASE_URL}/api/billing/summary",
        headers=_h(admin_creds["token"]), timeout=15,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["is_staff"] is True
    assert body["plan_status"] == "staff"
    assert body["plan"]["id"] == "refine"
    # Staff get the Refine org_limit so they can spin up unlimited workspaces.
    assert body["orgs_limit"] >= 1000


# ─── Subscribe ────────────────────────────────────────────────────────


def test_subscribe_rejects_refine(admin_creds):
    r = requests.post(
        f"{BASE_URL}/api/billing/subscribe",
        headers=_h(admin_creds["token"]),
        json={
            "plan_id": "refine",
            "origin_url": "https://example.com",
            "consent_text": "I authorize the recurring monthly charge.",
        },
        timeout=15,
    )
    assert r.status_code == 400, r.text
    assert "refine" in r.text.lower() or "sales" in r.text.lower()


def test_subscribe_rejects_unknown_plan(admin_creds):
    r = requests.post(
        f"{BASE_URL}/api/billing/subscribe",
        headers=_h(admin_creds["token"]),
        json={
            "plan_id": "ultra",
            "origin_url": "https://example.com",
            "consent_text": "I authorize the recurring monthly charge.",
        },
        timeout=15,
    )
    assert r.status_code == 400


def test_subscribe_requires_consent_text(admin_creds):
    r = requests.post(
        f"{BASE_URL}/api/billing/subscribe",
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
    """In preview the Emergent shadow STRIPE_API_KEY returns 503 cleanly.
    With a real key, the endpoint should return a Stripe Checkout URL."""
    r = requests.post(
        f"{BASE_URL}/api/billing/subscribe",
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


# ─── Refine contact sales ────────────────────────────────────────────


def test_contact_sales_persists_lead(admin_creds):
    r = requests.post(
        f"{BASE_URL}/api/billing/contact-sales",
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
        f"{BASE_URL}/api/billing/contact-sales",
        headers=_h(admin_creds["token"]),
        json={"name": "Bad Email Lead", "email": "not-an-email"},
        timeout=15,
    )
    assert r.status_code in (400, 422), r.text


# ─── Webhook smoke ───────────────────────────────────────────────────


def test_webhook_accepts_irrelevant_event():
    r = requests.post(
        f"{BASE_URL}/api/webhook/stripe",
        json={"type": "ping", "data": {"object": {}}},
        timeout=15,
    )
    assert r.status_code in (200, 503), r.text


# ─── Status polling ──────────────────────────────────────────────────


def test_status_unknown_session_404(admin_creds):
    r = requests.get(
        f"{BASE_URL}/api/billing/status/sess_does_not_exist",
        headers=_h(admin_creds["token"]), timeout=15,
    )
    assert r.status_code == 404


# ─── Phase C — overage aggregation (live Mongo) ─────────────────────


@pytest.mark.asyncio
async def test_overage_aggregation_rolls_up_per_user():
    """Insert llm_calls in last month across two orgs owned by the same
    fake user. The user's pooled spend should exceed Spark's $20 budget
    and trigger ONE invoice row (not two)."""
    import sys
    sys.path.insert(0, "/app/backend")
    from motor.motor_asyncio import AsyncIOMotorClient
    from bson import ObjectId
    from billing import (
        aggregate_overages_for_previous_month, OVERAGE_MARKUP, PLANS,
    )

    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME")
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]

    user_oid = ObjectId()
    fake_user_id = str(user_oid)
    org_a_oid = ObjectId()
    org_b_oid = ObjectId()
    org_a, org_b = str(org_a_oid), str(org_b_oid)

    await db.users.insert_one({
        "_id": user_oid, "name": "Overage Tester", "email": "overage@test.local",
        "plan": "spark",
    })
    for oid in (org_a_oid, org_b_oid):
        await db.organizations.insert_one({"_id": oid, "name": "Org", "owner_id": fake_user_id})
        await db.org_members.insert_one({
            "org_id": str(oid), "user_id": fake_user_id, "role": "owner",
            "joined_at": datetime.now(timezone.utc),
        })

    now = datetime.now(timezone.utc)
    if now.month == 1:
        prev_mid = datetime(now.year - 1, 12, 15, tzinfo=timezone.utc)
    else:
        prev_mid = datetime(now.year, now.month - 1, 15, tzinfo=timezone.utc)
    period_key = prev_mid.strftime("%Y-%m")

    # Split spend across both orgs — together past Spark's $20 budget.
    await db.llm_calls.insert_one({"org_id": org_a, "created_at": prev_mid, "cost_usd": 20.0, "model": "test"})
    await db.llm_calls.insert_one({"org_id": org_b, "created_at": prev_mid, "cost_usd": 30.0, "model": "test"})

    try:
        await aggregate_overages_for_previous_month(db)
        await aggregate_overages_for_previous_month(db)  # idempotent

        rows = await db.billing_overage_invoices.find(
            {"user_id": fake_user_id, "period_key": period_key}
        ).to_list(10)
        assert len(rows) == 1, f"Expected one rolled-up invoice, got {len(rows)}"
        included = PLANS["spark"]["included_llm_budget_usd"]
        expected = round((50.0 - included) * OVERAGE_MARKUP, 2)
        assert rows[0]["amount_due_usd"] == expected
        assert rows[0]["raw_cost_usd"] == 50.0

        # Paid invoices must not be overwritten.
        await db.billing_overage_invoices.update_one(
            {"_id": rows[0]["_id"]},
            {"$set": {"status": "paid", "amount_due_usd": 999.0}},
        )
        await aggregate_overages_for_previous_month(db)
        post = await db.billing_overage_invoices.find_one({"_id": rows[0]["_id"]})
        assert post["status"] == "paid"
        assert post["amount_due_usd"] == 999.0
    finally:
        await db.llm_calls.delete_many({"org_id": {"$in": [org_a, org_b]}})
        await db.billing_overage_invoices.delete_many({"user_id": fake_user_id})
        await db.org_members.delete_many({"user_id": fake_user_id})
        await db.organizations.delete_many({"_id": {"$in": [org_a_oid, org_b_oid]}})
        await db.users.delete_one({"_id": user_oid})
        client.close()


@pytest.mark.asyncio
async def test_org_creation_limit_gate():
    """A fake Spark user with 1 org already cannot create a second."""
    import sys
    sys.path.insert(0, "/app/backend")
    from motor.motor_asyncio import AsyncIOMotorClient
    from bson import ObjectId
    from fastapi import HTTPException
    from billing import enforce_org_creation_limit

    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME")
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]

    user_oid = ObjectId()
    fake_user_id = str(user_oid)
    org_oid = ObjectId()
    await db.users.insert_one({
        "_id": user_oid, "name": "Spark User", "email": "spark@test.local",
        "plan": "spark",
    })
    await db.organizations.insert_one({"_id": org_oid, "name": "Org", "owner_id": fake_user_id})
    await db.org_members.insert_one({
        "org_id": str(org_oid), "user_id": fake_user_id, "role": "owner",
        "joined_at": datetime.now(timezone.utc),
    })

    user = {"id": fake_user_id, "email": "spark@test.local", "name": "Spark User"}
    try:
        with pytest.raises(HTTPException) as exc:
            await enforce_org_creation_limit(db, user)
        assert exc.value.status_code == 402
        assert "Spark" in exc.value.detail
        assert "organization" in exc.value.detail.lower()
    finally:
        await db.org_members.delete_many({"user_id": fake_user_id})
        await db.organizations.delete_one({"_id": org_oid})
        await db.users.delete_one({"_id": user_oid})
        client.close()
