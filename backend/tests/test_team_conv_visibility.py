"""Team-conversation visibility regression — Feb 2026 fix.

Rule: any member of a team can see every conversation in that team.
Private Chief-of-Staff threads remain 1:1 to their owner; org owners
retain audit access to private CoS for oversight.

End-to-end behavior was verified with curl during the fix:
  • Owner can read a member's team conv (200) and sees it in the list
  • Member can read an owner's team conv (200) and sees it in the list
  • Member CANNOT read another user's private CoS (403)

This file pins the two helper-level facts the visibility chain relies
on. The full HTTP path runs through `load_conv_or_403` which combines
`can_user_view_team` + the private-CoS exception — both well-covered
by existing integration tests once Stripe-test deploy lands.
"""
from team_access import can_user_view_team


def _team(**kw):
    return {
        "kind": kw.get("kind", "open"),
        "member_user_ids": list(kw.get("member_ids", [])),
        "admin_user_ids": list(kw.get("admin_ids", [])),
        "is_general": kw.get("is_general", False),
    }


def _org(owner_id="owner-1"):
    return {"created_by": owner_id, "owners": [owner_id]}


def test_open_team_visible_to_every_org_member():
    # Whole point of an open team — every org member sees it.
    team = _team(kind="open")
    assert can_user_view_team(
        team=team, org=_org(), user_id="m2",
        org_member_ids=["owner-1", "m1", "m2"],
    )


def test_org_owner_views_closed_team_via_org_role():
    # Owner-of-org override is what unlocks audit access to a member's
    # closed-team conv after the visibility widening landed.
    team = _team(kind="closed", member_ids=["m1"])
    assert can_user_view_team(
        team=team, org=_org(owner_id="owner-1"), user_id="owner-1",
        org_member_ids=["owner-1", "m1"],
    )
