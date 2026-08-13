"""A partial update must never silently drop the shared account's protection.

`upsert_account` used to take concrete defaults and write them unconditionally,
so `fleet account add <existing-id>` — a command whose name implies creation —
reset `kind` to owned and both reserve floors to zero. Requirement (b) has no
other mechanism; `max_concurrency` is deliberately dead code. So the reserve
being one careless command away from gone was the real exposure.
"""

from __future__ import annotations

import pytest

from claude_fleet.control import FleetError


def test_bare_upsert_preserves_kind_and_floors(control):
    control.upsert_account(
        "shared", kind="shared", weekly_floor=0.4, five_hour_floor=0.5
    )
    control.upsert_account("shared")  # the dangerous no-op
    account = control.get_account("shared")
    assert account["kind"] == "shared"
    assert account["weekly_floor"] == pytest.approx(0.4)
    assert account["five_hour_floor"] == pytest.approx(0.5)


def test_partial_update_touches_only_the_named_field(control):
    control.upsert_account(
        "shared", kind="shared", weekly_floor=0.4, five_hour_floor=0.5, priority=3
    )
    control.upsert_account("shared", priority=7)
    account = control.get_account("shared")
    assert account["priority"] == 7
    assert account["kind"] == "shared"
    assert account["weekly_floor"] == pytest.approx(0.4)
    assert account["five_hour_floor"] == pytest.approx(0.5)


def test_floors_can_still_be_lowered_explicitly(control):
    """Preserving on omission must not make a floor impossible to change."""
    control.upsert_account("shared", kind="shared", weekly_floor=0.4)
    control.upsert_account("shared", weekly_floor=0.1)
    assert control.get_account("shared")["weekly_floor"] == pytest.approx(0.1)


def test_floor_can_be_set_to_zero_explicitly(control):
    control.upsert_account("shared", kind="shared", weekly_floor=0.4)
    control.upsert_account("shared", weekly_floor=0)
    assert control.get_account("shared")["weekly_floor"] == pytest.approx(0.0)


def test_allow_list_is_preserved_on_omission(control):
    control.upsert_account(
        "shared", kind="shared", allowed_workloads=["interactive"]
    )
    control.upsert_account("shared", priority=1)
    assert control.get_account("shared")["allowed_workloads"] == ["interactive"]


def test_allow_list_can_be_cleared_explicitly(control):
    control.upsert_account(
        "shared", kind="shared", allowed_workloads=["interactive"]
    )
    control.upsert_account("shared", clear_allowed_workloads=True)
    assert control.get_account("shared")["allowed_workloads"] is None


def test_new_account_still_gets_sane_defaults(control):
    control.upsert_account("fresh")
    account = control.get_account("fresh")
    assert account["kind"] == "owned"
    assert account["priority"] == 0
    assert account["weekly_floor"] == pytest.approx(0.0)
    assert account["state"] == "healthy"


def test_kind_is_still_validated(control):
    with pytest.raises(FleetError):
        control.upsert_account("x", kind="borrowed")


def test_an_owned_account_can_be_promoted_to_shared(control):
    control.upsert_account("acct")
    control.upsert_account("acct", kind="shared", weekly_floor=0.4)
    account = control.get_account("acct")
    assert account["kind"] == "shared"
    assert account["weekly_floor"] == pytest.approx(0.4)
