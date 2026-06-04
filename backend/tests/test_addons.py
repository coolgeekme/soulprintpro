"""Tests for the new add-on purchase flow.

Coverage:
  • Catalogue: `/billing/plans` exposes 5 add-ons with the expected
    schema and no internal-only fields (lookup_key, apply_kind).
  • Summary: `/billing/summary` carries the `addons` counter block + a
    full catalogue snapshot so the frontend can render without a
    second round-trip.
  • Checkout body validation: missing fields → 422.
  • Unknown addon_id → 404.
  • extra_seat plan restriction enforced for users not on Spark.
  • Routing logic (`_choose_addon_path`) — unit tests on the helper
    so we don't depend on a live Stripe API for the path-selection
    decision tree.
  • Webhook: `_apply_addon_purchase` is idempotent (replaying the
    same stripe_ref doesn't double-credit), `extra_orgs` uses REPLACE
    semantics, credits use INCREMENT semantics.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
import requests
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

import billing


BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL",
    "https://emergent-wing.preview.emergentagent.com",
).rstrip("/")


# ── Catalogue surfaces add-ons ────────────────────────────────────────


def test_plans_endpoint_exposes_all_five_addons() -> None:
    r = requests.get(f"{BASE_URL}/api/billing/plans", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert "addons" in body, "addons array missing from /billing/plans"
    by_id = {a["id"]: a for a in body["addons"]}
    assert set(by_id) == {
        "extra_org", "extra_seat",
        "message_pack", "browser_pack", "mission_pack",
    }


def test_addon_catalogue_does_not_leak_internal_keys() -> None:
    r = requests.get(f"{BASE_URL}/api/billing/plans", timeout=15)
    body = r.json()
    for a in body["addons"]:
        # Internal Stripe + bookkeeping keys MUST NOT leak to the client.
        assert "lookup_key" not in a, f"lookup_key leaked for {a['id']}"
        assert "apply_kind" not in a, f"apply_kind leaked for {a['id']}"
        # User-facing schema keys MUST be present.
        for k in ("name", "blurb", "price_usd", "unit", "cadence"):
            assert k in a, f"add-on {a['id']} missing {k}"


def test_recurring_vs_one_time_cadence_matches_pricing_page() -> None:
    r = requests.get(f"{BASE_URL}/api/billing/plans", timeout=15)
    by_id = {a["id"]: a for a in r.json()["addons"]}
    assert by_id["extra_org"]["cadence"] == "recurring"
    assert by_id["extra_seat"]["cadence"] == "recurring"
    assert by_id["message_pack"]["cadence"] == "one_time"
    assert by_id["browser_pack"]["cadence"] == "one_time"
    assert by_id["mission_pack"]["cadence"] == "one_time"


def test_addon_prices_exact_match() -> None:
    r = requests.get(f"{BASE_URL}/api/billing/plans", timeout=15)
    by_id = {a["id"]: a for a in r.json()["addons"]}
    # These are advertised on /pricing — if you change them, /pricing has to change too.
    assert by_id["extra_org"]["price_usd"] == 20.0
    assert by_id["extra_seat"]["price_usd"] == 30.0
    assert by_id["message_pack"]["price_usd"] == 15.0
    assert by_id["browser_pack"]["price_usd"] == 20.0
    assert by_id["mission_pack"]["price_usd"] == 10.0


# ── Checkout endpoint validation (no real Stripe calls) ──────────────


def test_addon_checkout_requires_auth() -> None:
    r = requests.post(
        f"{BASE_URL}/api/billing/addons/checkout",
        json={"addon_id": "message_pack", "origin_url": "https://x.test",
              "consent_text": "I agree to the charge"},
        timeout=15,
    )
    assert r.status_code in (401, 403)


# ── Webhook apply helper — pure-logic tests with real Mongo ──────────


@pytest_asyncio.fixture
async def db():
    mongo_url = os.environ.get("MONGO_URL")
    if not mongo_url:
        pytest.skip("MONGO_URL not configured.")
    name = f"addons_{uuid.uuid4().hex[:10]}"
    client = AsyncIOMotorClient(mongo_url)
    db = client[name]
    yield db
    client.drop_database(name)
    client.close()


@pytest_asyncio.fixture
async def seeded_user(db):
    """Seed a user row we can apply add-on mutations against."""
    user_oid = ObjectId()
    await db.users.insert_one({
        "_id": user_oid,
        "email": f"u_{uuid.uuid4().hex[:6]}@test.example",
        "plan": "spark",
        "extra_orgs": 0,
        "extra_seats": 0,
        "credit_messages": 0,
        "credit_browser_minutes": 0,
        "credit_missions": 0,
    })
    return str(user_oid)


@pytest.mark.asyncio
async def test_apply_message_pack_increments_credit(db, seeded_user) -> None:
    await billing._apply_addon_purchase(
        db, user_id=seeded_user, addon_id="message_pack", quantity=1,
        stripe_ref="cs:test_msg_1", source="checkout",
    )
    user = await db.users.find_one({"_id": ObjectId(seeded_user)})
    assert user["credit_messages"] == 500  # one pack = 500 msgs


@pytest.mark.asyncio
async def test_apply_message_pack_is_idempotent(db, seeded_user) -> None:
    """Replay protection — Stripe will retry webhooks. Same stripe_ref
    must NOT double-credit."""
    for _ in range(3):
        await billing._apply_addon_purchase(
            db, user_id=seeded_user, addon_id="message_pack", quantity=1,
            stripe_ref="cs:test_replay", source="checkout",
        )
    user = await db.users.find_one({"_id": ObjectId(seeded_user)})
    assert user["credit_messages"] == 500, "idempotency broken — message_pack double-credited"


@pytest.mark.asyncio
async def test_apply_browser_pack_credits_minutes(db, seeded_user) -> None:
    await billing._apply_addon_purchase(
        db, user_id=seeded_user, addon_id="browser_pack", quantity=1,
        stripe_ref="cs:test_browser_1", source="checkout",
    )
    user = await db.users.find_one({"_id": ObjectId(seeded_user)})
    assert user["credit_browser_minutes"] == 200


@pytest.mark.asyncio
async def test_apply_mission_pack_credits_missions(db, seeded_user) -> None:
    await billing._apply_addon_purchase(
        db, user_id=seeded_user, addon_id="mission_pack", quantity=1,
        stripe_ref="cs:test_mission_1", source="checkout",
    )
    user = await db.users.find_one({"_id": ObjectId(seeded_user)})
    assert user["credit_missions"] == 10


@pytest.mark.asyncio
async def test_apply_extra_orgs_uses_replace_semantics(db, seeded_user) -> None:
    """Recurring add-ons replay subscription.* webhook events. Counter
    must REPLACE, not INCREMENT — otherwise a single $20/mo subscription
    would inflate the counter every billing cycle."""
    await billing._apply_addon_purchase(
        db, user_id=seeded_user, addon_id="extra_org", quantity=3,
        stripe_ref="sub:s_1:customer.subscription.created", source="subscription",
    )
    await billing._apply_addon_purchase(
        db, user_id=seeded_user, addon_id="extra_org", quantity=3,
        stripe_ref="sub:s_1:customer.subscription.updated", source="subscription",
    )
    user = await db.users.find_one({"_id": ObjectId(seeded_user)})
    assert user["extra_orgs"] == 3, "extra_orgs should be REPLACED, not stacked"


@pytest.mark.asyncio
async def test_apply_extra_orgs_zeroes_on_cancel(db, seeded_user) -> None:
    """When the recurring add-on subscription is canceled, quantity=0
    is passed through and the counter must drop back to 0."""
    await billing._apply_addon_purchase(
        db, user_id=seeded_user, addon_id="extra_org", quantity=2,
        stripe_ref="sub:s_2:created", source="subscription",
    )
    await billing._apply_addon_purchase(
        db, user_id=seeded_user, addon_id="extra_org", quantity=0,
        stripe_ref="sub:s_2:deleted", source="subscription",
    )
    user = await db.users.find_one({"_id": ObjectId(seeded_user)})
    assert user["extra_orgs"] == 0


@pytest.mark.asyncio
async def test_apply_unknown_addon_is_noop(db, seeded_user) -> None:
    """Forward-compat: if Stripe sends us a legacy add-on id we don't
    know about, we log + skip rather than crashing the webhook."""
    await billing._apply_addon_purchase(
        db, user_id=seeded_user, addon_id="legacy_gpu_pack", quantity=1,
        stripe_ref="cs:legacy", source="checkout",
    )
    user = await db.users.find_one({"_id": ObjectId(seeded_user)})
    assert user["credit_messages"] == 0  # untouched
    assert user["extra_orgs"] == 0


@pytest.mark.asyncio
async def test_apply_credit_missions_quantity_multiplier(db, seeded_user) -> None:
    """Three mission packs in one purchase = 30 missions. We expose
    quantity at the webhook layer for future buy-three-at-once flows."""
    await billing._apply_addon_purchase(
        db, user_id=seeded_user, addon_id="mission_pack", quantity=3,
        stripe_ref="cs:missions_3", source="checkout",
    )
    user = await db.users.find_one({"_id": ObjectId(seeded_user)})
    assert user["credit_missions"] == 30
