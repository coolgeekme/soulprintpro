"""Staff override — who gets unlimited access without subscribing.

A small list of explicit emails + a domain wildcard. Used by:
  • `billing.py` — staff orgs are reported as Refine-equivalent in
    the billing summary, no "Subscribe" nag is shown.
  • `server.py` register hook — we skip the new-signup notification
    when a staff member is the one registering.
  • Anywhere else that needs to ask "is this user one of us?"

Tweak via `STAFF_EMAILS` (comma-separated exact addresses) and
`STAFF_DOMAINS` (comma-separated email domains) env vars without
touching code. The hardcoded fallbacks below are the source of
truth in production.
"""
from __future__ import annotations

import os
from typing import Iterable


def _split_env(name: str, fallback: Iterable[str]) -> set[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {x.strip().lower() for x in fallback if x and x.strip()}
    return {x.strip().lower() for x in raw.split(",") if x and x.strip()}


# Explicit overrides — exact email match, case-insensitive.
STAFF_EMAILS: set[str] = _split_env(
    "STAFF_EMAILS",
    fallback={"reggie@coolgeek.me"},
)

# Domain overrides — anyone whose email's domain is in this set
# gets staff access. We strip a leading "@" if the operator
# included one in the env value.
STAFF_DOMAINS: set[str] = {
    d.lstrip("@")
    for d in _split_env("STAFF_DOMAINS", fallback={"archeforge.com"})
}


def is_staff_email(email: str | None) -> bool:
    """True when the supplied email belongs to a staff member."""
    if not email:
        return False
    addr = email.strip().lower()
    if not addr or "@" not in addr:
        return False
    if addr in STAFF_EMAILS:
        return True
    domain = addr.rsplit("@", 1)[-1]
    return domain in STAFF_DOMAINS


async def org_has_staff_owner(db, org_id: str) -> bool:
    """True when any owner/admin of the org is staff. Cheap one-shot
    query — joins `org_members` with `users` on owner/admin rows.
    """
    if not org_id:
        return False
    cursor = db.org_members.find(
        {"org_id": org_id, "role": {"$in": ["owner", "admin"]}},
        {"user_id": 1, "_id": 0},
    )
    user_ids: list[str] = []
    async for m in cursor:
        if m.get("user_id"):
            user_ids.append(m["user_id"])
    if not user_ids:
        return False
    from bson import ObjectId
    oids = []
    for uid in user_ids:
        try:
            oids.append(ObjectId(uid))
        except Exception:
            pass
    if not oids:
        return False
    async for u in db.users.find(
        {"_id": {"$in": oids}}, {"email": 1, "_id": 0},
    ):
        if is_staff_email(u.get("email")):
            return True
    return False
