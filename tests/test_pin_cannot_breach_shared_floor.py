"""A pin must not spend a shared account past its reserve.

Found by audit and confirmed live: `/tc-acct/<uuid>` requests reach the upstream
with the coordinator recording NOTHING — zero route.changed events — because
`server.js` skips `hooks.selectAccount` entirely when `ctx.pinnedIndex` is set.
`_eligible` and the floor therefore never ran, and anything on loopback could
spend the shared account to zero.

Verified before the fix: a pinned request to 9f9010ad returned
`rate_limit_error` from the account itself while the coordinator logged no
routing decision at all.

A pin bypassing SCHEDULING is the feature. A pin bypassing a policy that
protects someone else's quota is not.
"""

from __future__ import annotations

import pytest

from conftest import quota


def test_owned_accounts_stay_pinnable_even_when_spent(control, db):
    """Exhausting your own account is your business, and refusing would break
    the manual override requirement (c) is about."""
    from claude_fleet.coordinator import Coordinator

    control.upsert_account("owned")
    quota(control, "owned", weekly=1.0, five_hour=1.0)
    assert Coordinator(db).pin_check({"account_id": "owned"})["allowed"] is True


def test_a_shared_account_above_its_floor_can_be_pinned(control, db):
    from claude_fleet.coordinator import Coordinator

    control.upsert_account("shared", kind="shared", weekly_floor=0.4, five_hour_floor=0.5)
    quota(control, "shared", weekly=0.2, five_hour=0.1)
    assert Coordinator(db).pin_check({"account_id": "shared"})["allowed"] is True


def test_a_shared_account_at_its_weekly_floor_is_refused(control, db):
    from claude_fleet.coordinator import Coordinator

    control.upsert_account("shared", kind="shared", weekly_floor=0.4)
    quota(control, "shared", weekly=0.7, five_hour=0.0)
    verdict = Coordinator(db).pin_check({"account_id": "shared"})
    assert verdict["allowed"] is False
    assert "reserve floor" in verdict["reason"]


def test_a_shared_account_at_its_five_hour_floor_is_refused(control, db):
    from claude_fleet.coordinator import Coordinator

    control.upsert_account("shared", kind="shared", five_hour_floor=0.5)
    quota(control, "shared", weekly=0.0, five_hour=0.6)
    assert Coordinator(db).pin_check({"account_id": "shared"})["allowed"] is False


def test_a_shared_account_with_unknown_quota_fails_closed(control, db):
    """Matching `_eligible`: never spend a shared account blind."""
    from claude_fleet.coordinator import Coordinator

    control.upsert_account("shared", kind="shared", weekly_floor=0.4)
    verdict = Coordinator(db).pin_check({"account_id": "shared"})
    assert verdict["allowed"] is False
    assert "unknown" in verdict["reason"]


def test_the_live_shared_account_shape_is_refused(control, db):
    """The exact state of shared@example.com when the hole was found:
    weekly 1.0 against a 0.4 floor."""
    from claude_fleet.coordinator import Coordinator

    control.upsert_account(
        "00000000-0000-4000-8000-000000000001",
        kind="shared", weekly_floor=0.4, five_hour_floor=0.5,
    )
    quota(control, "00000000-0000-4000-8000-000000000001", weekly=1.0, five_hour=0.0)
    verdict = Coordinator(db).pin_check(
        {"account_id": "00000000-0000-4000-8000-000000000001"})
    assert verdict["allowed"] is False


def test_an_unregistered_account_does_not_break_pinning(control, db):
    """A fresh install has no account rows yet; refusing here would make pinning
    impossible rather than safe."""
    from claude_fleet.coordinator import Coordinator

    verdict = Coordinator(db).pin_check({"account_id": "never-seen"})
    assert verdict["allowed"] is True


def test_a_missing_account_id_is_an_error(db):
    from claude_fleet.control import FleetError
    from claude_fleet.coordinator import Coordinator

    with pytest.raises(FleetError):
        Coordinator(db).pin_check({})
