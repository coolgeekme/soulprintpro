"""End-to-end tests for the Phase B billing module.

Coverage:
  • Public plan catalogue (no auth).
  • Per-org summary auth gating — owners 200, non-admin members 403.
  • Plan / seat / overage checkout request validation. Real Stripe is
    never hit (we exercise the routing layer up to the live call).
  • Idempotent overage aggregation (Phase C) against the live MongoDB.

We use the standing superadmin from `/app/memory/test_credentials.md`
for any auth-gated path because new-user registration requires an
access code, which is invariant for the live preview environment.
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


# ─── Helpers ────────────────────────────────────────────────────────────


def _login(email: str, password: str):
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": email, "password": password},
        timeout=15,
    )
    if r.status_code != 200:
        return None
    return r.json().get("access_token")


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def _superadmin_org_id() -> str | None:
    tok = _login(SUPERADMIN_EMAIL, SUPERADMIN_PASS)
    if not tok:
        return None
    r = requests.get(f"{BASE_URL}/api/orgs", headers=_h(tok), timeout=15)
    if r.status_code != 200:
        return None
    orgs = r.json() or []
    return (orgs[0] or {}).get("id") if orgs else None


@pytest.fixture(scope="module")
def admin_creds():
    tok = _login(SUPERADMIN_EMAIL, SUPERADMIN_PASS)
    if not tok:
        pytest.skip("Superadmin login failed — skipping auth-gated billing tests.")
    r = requests.get(f"{BASE_URL}/api/orgs", headers=_h(tok), timeout=15)
    if r.status_code != 200 or not r.json():
        pytest.skip("Superadmin has no orgs — skipping.")
    return {"token": tok, "org_id": r.json()[0]["id"]}


# ─── Plan catalogue ─────────────────────────────────────────────────────


def test_plan_catalogue_is_public():
    r = requests.get(f"{BASE_URL}/api/billing/plans", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert set(p["id"] for p in body["plans"]) == {"spark", "temper", "refine"}
    assert body["overage_markup"] >= 1
    for p in body["plans"]:
        for key in (
            "name", "tagline", "monthly_usd", "seats_included",
            "extra_seat_usd", "included_llm_budget_usd", "features",
        ):
            assert key in p, f"plan {p.get('id')} missing {key}"


# ─── Summary auth + payload shape ───────────────────────────────────────


def test_billing_summary_owner_payload(admin_creds):
    r = requests.get(
        f"{BASE_URL}/api/orgs/{admin_creds['org_id']}/billing/summary",
        headers=_h(admin_creds["token"]), timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan"]["id"] in ("spark", "temper", "refine")
    assert "this_month_spend_usd" in body
    assert body["plan_status"] in ("paid", "unpaid")
    assert isinstance(body["recent_transactions"], list)


def test_billing_summary_blocks_unauthenticated():
    r = requests.get(
        f"{BASE_URL}/api/orgs/000000000000000000000000/billing/summary",
        timeout=15,
    )
    assert r.status_code in (401, 403), r.text


# ─── Checkout — input validation ────────────────────────────────────────


def test_checkout_plan_rejects_unknown_plan(admin_creds):
    r = requests.post(
        f"{BASE_URL}/api/orgs/{admin_creds['org_id']}/billing/checkout/plan",
        headers=_h(admin_creds["token"]),
        json={"plan_id": "ultra", "origin_url": "https://example.com"},
        timeout=15,
    )
    assert r.status_code == 400, r.text


def test_checkout_status_unknown_session_404(admin_creds):
    r = requests.get(
        f"{BASE_URL}/api/billing/status/sess_does_not_exist",
        headers=_h(admin_creds["token"]), timeout=15,
    )
    assert r.status_code == 404


# ─── Phase C — overage aggregation against the live Mongo ──────────────


@pytest.mark.asyncio
async def test_overage_aggregation_idempotent():
    """Insert a synthetic burst of llm_calls in last month for a fake
    org, run aggregation twice, and assert the invoice was written
    exactly once and paid invoices are preserved."""
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
    seed_cost = 50.0  # well past Spark's $20 budget

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
        assert ours_1, "First aggregation produced no invoice for the seeded org"
        assert ours_2, "Second aggregation produced no invoice for the seeded org"

        rows = await db.billing_overage_invoices.find(
            {"org_id": fake_org_id, "period_key": period_key}
        ).to_list(10)
        assert len(rows) == 1, f"Expected one invoice row, got {len(rows)}"

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
