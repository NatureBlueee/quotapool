"""Requirement (b): the shared account must never be fully consumed.

The floor is the only mechanism that enforces this — `max_concurrency` is
deliberately dead code (control.py:1215-1219). These tests pin the property that
*no* routing path, including an explicit manual pin, can spend past it.
"""

from __future__ import annotations

import time

import pytest

from claude_fleet.control import FleetError, NoCapacity
from conftest import quota


def test_floor_blocks_when_remaining_hits_zero(control):
    control.upsert_account("shared", kind="shared", weekly_floor=0.4)
    quota(control, "shared", weekly=0.5, five_hour=0.0)
    row = _account(control, "shared")
    ok, why = control._eligible(row, "interactive", existing=False, active_count=0)
    assert ok, why

    # 0.6 used + 0.4 floor == 1.0 exactly: the floor is reached, not breached.
    quota(control, "shared", weekly=0.6, five_hour=0.0)
    row = _account(control, "shared")
    ok, why = control._eligible(row, "interactive", existing=False, active_count=0)
    assert not ok and why == "weekly_floor"


def test_five_hour_floor_is_independent_of_weekly(control):
    control.upsert_account("shared", kind="shared", five_hour_floor=0.5)
    quota(control, "shared", weekly=0.0, five_hour=0.5)
    row = _account(control, "shared")
    ok, why = control._eligible(row, "interactive", existing=False, active_count=0)
    assert not ok and why == "five_hour_floor"


def test_shared_account_with_unknown_quota_fails_closed(control):
    """A shared account is never used blind — this is the fail-closed clause."""
    control.upsert_account("shared", kind="shared", weekly_floor=0.4)
    row = _account(control, "shared")
    assert row["weekly_used"] is None
    ok, why = control._eligible(row, "interactive", existing=False, active_count=0)
    assert not ok and why == "shared_quota_unknown"


def test_owned_account_with_unknown_quota_is_allowed(control):
    """The fail-closed rule is scoped to shared accounts on purpose:
    an owned account with no quota snapshot yet must stay usable, otherwise a
    fresh install cannot route at all."""
    control.upsert_account("owned", kind="owned")
    row = _account(control, "owned")
    ok, why = control._eligible(row, "interactive", existing=False, active_count=0)
    assert ok, why


def test_floor_survives_existing_session(control):
    """`existing=True` relaxes `draining`, and must NOT relax the floor."""
    control.upsert_account("shared", kind="shared", weekly_floor=0.4)
    quota(control, "shared", weekly=0.7, five_hour=0.0)
    row = _account(control, "shared")
    ok, why = control._eligible(row, "interactive", existing=True, active_count=0)
    assert not ok and why == "weekly_floor"


def test_hard_pin_cannot_penetrate_the_floor(control):
    """A manual pin must fail loudly rather than silently spending the reserve.

    This is the property requirement (b) actually depends on: the owner can pin
    anything, but not past the floor of an account he shares with someone else.
    """
    control.upsert_account("shared", kind="shared", weekly_floor=0.4)
    control.upsert_account("spare", kind="owned")
    quota(control, "shared", weekly=0.8, five_hour=0.0)
    quota(control, "spare", weekly=0.1, five_hour=0.0)

    control.register_execution("s1", workload_class="interactive")
    generation = control.acquire_writer("s1", "holder-1")
    control.set_route_policy("s1", "pin", "shared")

    with pytest.raises(NoCapacity):
        control.decide_route("s1", "holder-1", generation)


def test_next_account_cannot_penetrate_the_floor(control):
    control.upsert_account("shared", kind="shared", weekly_floor=0.4)
    control.upsert_account("spare", kind="owned")
    quota(control, "shared", weekly=0.8, five_hour=0.0)
    quota(control, "spare", weekly=0.1, five_hour=0.0)

    control.register_execution("s1", workload_class="interactive")
    generation = control.acquire_writer("s1", "holder-1")
    control.set_route_policy("s1", "next", "shared")

    with pytest.raises(NoCapacity):
        control.decide_route("s1", "holder-1", generation)


def test_floored_shared_account_is_skipped_not_fatal_when_alternatives_exist(control):
    """Automatic selection routes around a floored shared account silently.
    Only an explicit manual demand for it is an error."""
    control.upsert_account("shared", kind="shared", weekly_floor=0.4)
    control.upsert_account("spare", kind="owned")
    quota(control, "shared", weekly=0.8, five_hour=0.0)
    quota(control, "spare", weekly=0.1, five_hour=0.0)

    control.register_execution("s1", workload_class="interactive")
    generation = control.acquire_writer("s1", "holder-1")
    route = control.decide_route("s1", "holder-1", generation)
    assert route["account_id"] == "spare"


def test_allowed_workloads_denies_daemon_on_shared(control):
    """Requirement (b)'s other half: keep background work off the shared
    account entirely, not merely below its floor."""
    control.upsert_account(
        "shared", kind="shared", weekly_floor=0.4,
        allowed_workloads=["interactive"],
    )
    quota(control, "shared", weekly=0.1, five_hour=0.0)
    row = _account(control, "shared")

    ok, _ = control._eligible(row, "interactive", existing=False, active_count=0)
    assert ok
    ok, why = control._eligible(row, "daemon", existing=False, active_count=0)
    assert not ok and why == "workload_denied"


def _account(control, account_id):
    with control.db.connect() as conn:
        return conn.execute(
            "SELECT * FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
