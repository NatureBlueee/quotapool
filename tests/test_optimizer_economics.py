"""The two economic goals, and which mechanism actually delivers each.

The owner states them as: spend a weekly window before it resets (unused quota
evaporates — it does not carry over), while keeping agents served without
interruption. Those pull against each other, and it matters a great deal which
part of the model enforces which.

The claim under test: **continuity is structural, not a penalty term.**
`solve_rolling_horizon` places forecast demand into every 30-minute slot out to
the horizon and each account contributes a finite capacity arc. Piling all
demand onto one account leaves later slots unservable, so min-cost *max*-flow
spreads it — not because spreading is rewarded, but because it is the only way
to satisfy the demand at all.

That distinction is what makes it safe to lower ACTIVE_FAMILY_LOAD_COST from
50,000 to 500. If continuity depended on that penalty, lowering it would trade
away goal two to buy goal one. It does not.
"""

from __future__ import annotations

import time

from claude_fleet.optimizer import (
    ACTIVE_FAMILY_LOAD_COST,
    DEADLINE_LEXICOGRAPHIC_SCALE,
    OptimizationAccount,
    solve_rolling_horizon,
)


HOUR = 3600
DAY = 24 * HOUR


def account(account_id, *, weekly, weekly_reset, five=1.0, five_reset=None,
            families=0, priority=0, now=None):
    now = now or time.time()
    return OptimizationAccount(
        account_id=account_id,
        five_remaining=five,
        five_reset_at=five_reset if five_reset is not None else now + 5 * HOUR,
        weekly_remaining=weekly,
        weekly_reset_at=weekly_reset,
        priority=priority,
        five_floor=0.0,
        active_families=families,
    )


def solve(accounts, *, now=None, current=None, burn=0.04):
    now = now or time.time()
    return solve_rolling_horizon(
        accounts,
        now=now,
        current_account=current,
        preferred_account=None,
        weekly_burn_per_hour=burn,
        five_to_weekly_capacity=0.2,
    )


# --- goal one: spend it before it evaporates --------------------------------

def test_expiring_capacity_wins_even_when_that_account_is_busy():
    now = time.time()
    result = solve([
        # 70% left, gone in an hour, already carrying two sessions
        account("expiring", weekly=0.7, weekly_reset=now + HOUR,
                five_reset=now + HOUR, families=2, now=now),
        # 90% left, resets in a week, idle
        account("relaxed", weekly=0.9, weekly_reset=now + 7 * DAY,
                five_reset=now + 5 * HOUR, families=0, now=now),
    ], now=now)
    assert result is not None
    assert result.account_id == "expiring", (
        "quota that evaporates in an hour must outrank an idle account whose "
        "window resets in a week"
    )


def test_load_still_decides_between_equally_urgent_accounts():
    """Lowering the penalty must not make it inert — that would concentrate a
    fan-out on one account for no reason."""
    now = time.time()
    result = solve([
        account("busy", weekly=0.8, weekly_reset=now + 3 * DAY,
                five_reset=now + 5 * HOUR, families=3, now=now),
        account("idle", weekly=0.8, weekly_reset=now + 3 * DAY,
                five_reset=now + 5 * HOUR, families=0, now=now),
    ], now=now)
    assert result is not None
    assert result.account_id == "idle"


def test_the_weight_cannot_outvote_half_an_hour_of_urgency():
    """The calibration, asserted as arithmetic rather than as a comment.

    One slot of deadline difference costs DEADLINE_LEXICOGRAPHIC_SCALE. If a
    single active session could outweigh that, the scheduler would again prefer
    idle accounts over expiring ones — the exact regression this guards.
    """
    assert ACTIVE_FAMILY_LOAD_COST < DEADLINE_LEXICOGRAPHIC_SCALE


# --- goal two: keep agents served -------------------------------------------

def test_demand_spreads_across_accounts_when_one_cannot_cover_it():
    """Continuity is delivered by the capacity constraint, not by a penalty.

    Each account holds far less than the horizon needs, so a solution that used
    only one would starve later slots. The flow must therefore draw on several.
    """
    now = time.time()
    accounts = [
        account("a", weekly=0.25, weekly_reset=now + 7 * DAY,
                five_reset=now + 5 * HOUR, now=now),
        account("b", weekly=0.25, weekly_reset=now + 7 * DAY,
                five_reset=now + 5 * HOUR, now=now),
        account("c", weekly=0.25, weekly_reset=now + 7 * DAY,
                five_reset=now + 5 * HOUR, now=now),
    ]
    # A burn rate that needs more than any single account can supply.
    result = solve(accounts, now=now, burn=0.05)
    assert result is not None
    assert result.flow_quanta > 0
    # The decisive assertion: the plan is not satisfiable from one account.
    single_account_capacity = 25  # 0.25 * QUANTA_PER_WEEKLY_WINDOW
    assert result.flow_quanta > single_account_capacity, (
        "the horizon needs more than one account can supply, so the model must "
        "be planning to draw on several — continuity comes from this, not from "
        "the load penalty"
    )


def test_spreading_happens_with_the_load_penalty_removed_entirely():
    """The load penalty is a tie-breaker, not the continuity mechanism.

    Setting every account's family count to zero removes the penalty's effect
    completely. If continuity depended on it, the plan would collapse onto one
    account. It does not.
    """
    now = time.time()
    accounts = [
        account("a", weekly=0.2, weekly_reset=now + 7 * DAY,
                five_reset=now + 5 * HOUR, families=0, now=now),
        account("b", weekly=0.2, weekly_reset=now + 7 * DAY,
                five_reset=now + 5 * HOUR, families=0, now=now),
    ]
    result = solve(accounts, now=now, burn=0.05)
    assert result is not None
    assert result.flow_quanta > 20, (
        "with the penalty inert, capacity limits alone must still force the "
        "plan across both accounts"
    )


def test_a_fleet_that_cannot_meet_demand_says_so():
    """Honest degradation: the solver reports when it is capacity-limited
    rather than silently planning a shortfall. This is what tells an operator
    the fleet is short, as opposed to merely busy."""
    now = time.time()
    result = solve([
        account("tiny", weekly=0.02, weekly_reset=now + 7 * DAY,
                five_reset=now + 5 * HOUR, now=now),
    ], now=now, burn=0.2)
    assert result is not None
    assert result.status == "capacity_limited_optimal"


def test_a_fleet_with_room_reports_a_satisfied_plan():
    now = time.time()
    result = solve([
        account("big", weekly=0.95, weekly_reset=now + 7 * DAY,
                five_reset=now + 5 * HOUR, now=now),
    ], now=now, burn=0.001)
    assert result is not None
    assert result.status == "optimal"


# --- the two goals together --------------------------------------------------

def test_urgency_is_preferred_without_starving_the_horizon():
    """Both goals at once: drain the expiring account first, but keep enough
    plan on the others that work continues after it resets to zero."""
    now = time.time()
    result = solve([
        account("expiring", weekly=0.3, weekly_reset=now + 2 * HOUR,
                five_reset=now + 2 * HOUR, now=now),
        account("plenty", weekly=0.9, weekly_reset=now + 6 * DAY,
                five_reset=now + 5 * HOUR, now=now),
    ], now=now, burn=0.03)
    assert result is not None
    # Serve from the expiring account now...
    assert result.account_id == "expiring"
    # ...while the plan as a whole exceeds what that account alone holds,
    # i.e. the horizon is still covered after it is gone.
    assert result.flow_quanta > 30


def test_inside_the_solver_priority_is_a_cost_not_a_veto():
    """Where the priority guarantee actually lives — worth pinning, because it
    is not where it looks like it lives.

    Inside the solver, priority is a per-unit cost on the source arc. It can be
    outweighed, and here it correctly is: `backup`'s 5h window closes in an
    hour, so quanta not spent from it now evaporate. Serving slot 0 from a
    low-priority account whose capacity is about to vanish, while keeping the
    high-priority account's week-long window for later slots, is the cheaper
    plan *and* the economically right one.

    The hard guarantee is one layer up: `decide_route` filters candidates to
    `minimum_priority` BEFORE calling the solver, so a reachable
    higher-priority account is never traded away. That is asserted against the
    real decision path in
    tests/test_quota_utilization.py::test_priority_still_outranks_urgency.

    Keeping both tests matters: if someone ever "fixes" the solver to make
    priority a veto, they would reintroduce quota waste, and this test says why
    that is not a bug.
    """
    now = time.time()
    result = solve([
        account("preferred", weekly=0.9, weekly_reset=now + 7 * DAY,
                five_reset=now + 5 * HOUR, priority=0, now=now),
        account("backup", weekly=0.3, weekly_reset=now + HOUR,
                five_reset=now + HOUR, priority=10, now=now),
    ], now=now)
    assert result is not None
    assert result.account_id == "backup"
    assert result.status == "capacity_limited_optimal"
