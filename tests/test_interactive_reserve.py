"""Keep the tail of a window for work someone is waiting on.

The owner's intuition was "automated work should run at night". The data
refused a time-of-day rule: across the only two days available, 18 of 24 hours
had a cross-day coefficient of variation above 0.7 (04:00 was 3 requests one
day and 502 the next), and he expects the mix to flatten as automated work
grows — so a fitted curve would encode this week's accidents and then go stale.

The durable version of that intuition is not "when", it is "what": batch work
should yield to work a person is waiting on, whenever quota gets tight. That
holds at 3am and at noon, and it survives the demand curve changing.
"""

from __future__ import annotations

import pytest

from claude_fleet.control import DEFERRABLE_WORKLOADS, NoCapacity
from conftest import quota


def _account(control, account_id):
    with control.db.connect() as conn:
        return conn.execute(
            "SELECT * FROM accounts WHERE id=?", (account_id,)
        ).fetchone()


def test_default_is_off_so_nothing_changes_until_configured(control):
    control.upsert_account("a")
    quota(control, "a", weekly=0.99, five_hour=0.0)
    row = _account(control, "a")
    ok, _ = control._eligible(row, "daemon", existing=False, active_count=0)
    assert ok, "with no reserve set, a daemon may use the last of a window"


def test_batch_work_is_refused_below_the_reserve(control):
    control.upsert_account("a", interactive_reserve=0.2)
    quota(control, "a", weekly=0.9, five_hour=0.0)  # 0.10 left, under 0.20
    row = _account(control, "a")
    ok, why = control._eligible(row, "daemon", existing=False, active_count=0)
    assert not ok and why == "interactive_reserve"


def test_interactive_work_still_gets_the_reserve(control):
    """The whole point: the tail exists FOR interactive work."""
    control.upsert_account("a", interactive_reserve=0.2)
    quota(control, "a", weekly=0.9, five_hour=0.0)
    row = _account(control, "a")
    ok, _ = control._eligible(row, "interactive", existing=False, active_count=0)
    assert ok


def test_above_the_reserve_batch_and_interactive_compete_normally(control):
    control.upsert_account("a", interactive_reserve=0.2)
    quota(control, "a", weekly=0.5, five_hour=0.0)  # 0.50 left
    row = _account(control, "a")
    for workload in ("daemon", "interactive", "workflow"):
        ok, why = control._eligible(row, workload, existing=False, active_count=0)
        assert ok, f"{workload} refused with plenty left: {why}"


def test_subagents_and_forks_are_not_treated_as_batch(control):
    """They are children of a session someone is waiting on. Starving them
    stalls interactive work while looking like background throttling."""
    control.upsert_account("a", interactive_reserve=0.2)
    quota(control, "a", weekly=0.9, five_hour=0.0)
    row = _account(control, "a")
    for workload in ("subagent", "fork", "team-member", "team-lead", "agent-view"):
        ok, why = control._eligible(row, workload, existing=False, active_count=0)
        assert ok, f"{workload} must not be deferred: {why}"


def test_the_deferrable_set_is_what_it_claims(control):
    assert DEFERRABLE_WORKLOADS == {"daemon", "workflow", "subagent-bulk"}


def test_the_five_hour_window_can_trigger_it_too(control):
    """A 5h window can be nearly spent while the weekly one is fine. Batch work
    should yield on whichever is tighter."""
    control.upsert_account("a", interactive_reserve=0.2)
    quota(control, "a", weekly=0.1, five_hour=0.9)  # weekly fine, 5h nearly gone
    row = _account(control, "a")
    ok, why = control._eligible(row, "daemon", existing=False, active_count=0)
    assert not ok and why == "interactive_reserve"


def test_a_daemon_routes_around_a_reserved_account(control):
    """End to end through decide_route: batch work is not blocked, it is moved."""
    control.upsert_account("tight", interactive_reserve=0.3)
    control.upsert_account("roomy")
    quota(control, "tight", weekly=0.9, five_hour=0.0)
    quota(control, "roomy", weekly=0.4, five_hour=0.0)

    control.register_execution("d1", workload_class="daemon")
    generation = control.acquire_writer("d1", "holder-d")
    route = control.decide_route("d1", "holder-d", generation)
    assert route["account_id"] == "roomy"


def test_interactive_can_still_use_the_reserved_account(control):
    control.upsert_account("tight", interactive_reserve=0.3)
    quota(control, "tight", weekly=0.9, five_hour=0.0)

    control.register_execution("s1", workload_class="interactive")
    generation = control.acquire_writer("s1", "holder-s")
    route = control.decide_route("s1", "holder-s", generation)
    assert route["account_id"] == "tight"


def test_batch_work_parks_when_only_reserved_capacity_remains(control):
    """Parking a daemon so a person can keep working is the correct outcome,
    and requirement (d) says park rather than reach for paid capacity."""
    control.upsert_account("only", interactive_reserve=0.3)
    quota(control, "only", weekly=0.9, five_hour=0.0)

    control.register_execution("d1", workload_class="daemon")
    generation = control.acquire_writer("d1", "holder-d")
    with pytest.raises(NoCapacity):
        control.decide_route("d1", "holder-d", generation)


def test_reserve_composes_with_the_shared_floor(control):
    """Two independent protections on one account: the shared floor keeps quota
    for the other holder, the reserve keeps some of what is left for
    interactive use."""
    control.upsert_account(
        "shared", kind="shared", weekly_floor=0.4, interactive_reserve=0.2
    )
    quota(control, "shared", weekly=0.5, five_hour=0.0)  # 0.10 after the floor
    row = _account(control, "shared")

    ok, why = control._eligible(row, "daemon", existing=False, active_count=0)
    assert not ok and why == "interactive_reserve"
    ok, _ = control._eligible(row, "interactive", existing=False, active_count=0)
    assert ok

    # Past the floor, nothing gets in — the floor outranks the reserve.
    quota(control, "shared", weekly=0.7, five_hour=0.0)
    row = _account(control, "shared")
    ok, why = control._eligible(row, "interactive", existing=False, active_count=0)
    assert not ok and why == "weekly_floor"


def test_the_reserve_is_preserved_by_a_partial_update(control):
    control.upsert_account("a", interactive_reserve=0.25)
    control.upsert_account("a", priority=3)
    assert control.get_account("a")["interactive_reserve"] == pytest.approx(0.25)
