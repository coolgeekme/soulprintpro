"""Seed an org with a pending ownership transfer where recipient is on 'spark'
so the frontend 402 upgrade-CTA can be exercised.

Outputs JSON with recipient credentials + org_id + transfer_id.
"""
import os
import sys
import json
import time
import uuid
import requests
from pymongo import MongoClient
from bson import ObjectId

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE}/api"
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")
SU_EMAIL = "reggie@coolgeek.me"
SU_PASS = "superadminpass123"


def _h(tok):
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def login(email, password):
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def mint_code(admin_token):
    code = f"TESTITER33UI{uuid.uuid4().hex[:6].upper()}"
    r = requests.post(f"{API}/admin/access-codes", headers=_h(admin_token),
                      json={"code": code, "usage_type": "multi", "max_uses": 10,
                            "note": "iter-33 UI 402 retest"}, timeout=30)
    r.raise_for_status()
    return r.json()["code"]


def register(code, prefix):
    email = f"TEST_{prefix}_{uuid.uuid4().hex[:8]}@example.com"
    pw = "testpass123"
    for i in range(10):
        r = requests.post(f"{API}/auth/register",
                          json={"name": f"Test {prefix}", "email": email,
                                "password": pw, "access_code": code}, timeout=30)
        if r.status_code == 429:
            time.sleep(4 + 2 * i); continue
        r.raise_for_status()
        j = r.json()
        return {"token": j["access_token"], "id": j["id"], "email": email, "password": pw}
    raise RuntimeError("register: too many 429s")


def main():
    admin_token = login(SU_EMAIL, SU_PASS)
    code = mint_code(admin_token)
    owner = register(code, "uiownerA")
    recipient = register(code, "uirecipB")

    # Owner creates org
    r = requests.post(f"{API}/orgs", headers=_h(owner["token"]),
                      json={"name": f"TEST_UIIter33_Org_{uuid.uuid4().hex[:5]}"}, timeout=30)
    r.raise_for_status()
    org = r.json()
    org_id = org["id"]

    # Invite recipient
    r = requests.post(f"{API}/orgs/{org_id}/invites", headers=_h(owner["token"]),
                      json={"role": "member"}, timeout=30)
    r.raise_for_status()
    invite_code = r.json()["code"]
    r = requests.post(f"{API}/invites/accept", headers=_h(recipient["token"]),
                      json={"code": invite_code}, timeout=30)
    r.raise_for_status()

    # Create ownership transfer to recipient
    r = requests.post(f"{API}/orgs/{org_id}/ownership-transfers",
                      headers=_h(owner["token"]),
                      json={"recipient_user_id": recipient["id"],
                            "outgoing_role_after": "member",
                            "also_leave_after": False},
                      timeout=30)
    r.raise_for_status()
    transfer = r.json()
    transfer_id = transfer["id"]

    # Force-stamp recipient plan to 'spark'
    cli = MongoClient(MONGO_URL)
    db = cli[DB_NAME]
    db.users.update_one({"_id": ObjectId(recipient["id"])}, {"$set": {"plan": "spark"}})
    cli.close()

    out = {
        "owner_id": owner["id"],
        "owner_email": owner["email"],
        "recipient_email": recipient["email"],
        "recipient_password": recipient["password"],
        "recipient_id": recipient["id"],
        "recipient_token": recipient["token"],
        "org_id": org_id,
        "org_name": org["name"],
        "transfer_id": transfer_id,
        "required_plan_tier": transfer.get("required_plan_tier"),
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
