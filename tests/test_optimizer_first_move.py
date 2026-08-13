"""An empty first slot is a plan that says "start later", not the absence of one.

`solve_rolling_horizon` returned None whenever no flow landed in slot zero, and
the scheduler read that as "the optimizer has no opinion" and fell through to a
plain score sort. That sort has no stickiness at all, and it is dominated by a
per-session load penalty — so whichever account currently holds fewer sessions
wins, the session moves there, that account becomes the loaded one, and the next
decision moves it back.

Every switch throws away a prompt cache. That is what a long task stalling looks
like from the outside.

Measured on live data, identical inputs but for `current_account`:

    current=None                  sent=53/wanted=970  -> primary@example.com
    current=fourth@example.com    sent=53/wanted=970  -> None

The same 53 quanta were routed both times: the solve did not fail, only the
reading of it did. It appears precisely when it hurts most — `current_account`
puts a migration cost on every alternative, so a session whose own account
cannot serve this slot pushes the cheapest flow into later slots.

Live consequence over six hours: 130 switches across 63 sessions, every one
labelled `scheduler_fallback`, ping-ponging me@ -> hi@ 22 times and back 15.
"""

from __future__ import annotations

import time

from claude_fleet.optimizer import OptimizationAccount, solve_rolling_horizon

HOUR = 3600


def _account(name, five_remaining, weekly_remaining, *, five_reset, weekly_reset,
             now):
    return OptimizationAccount(
        account_id=name,
        five_remaining=five_remaining,
        five_reset_at=now + five_reset,
        weekly_remaining=weekly_remaining,
        weekly_reset_at=now + weekly_reset,
        priority=0,
        five_floor=0.0,
        active_families=0,
    )


def _solve(accounts, now, current):
    return solve_rolling_horizon(
        accounts,
        now=now,
        current_account=current,
        preferred_account=None,
        weekly_burn_per_hour=0.08,
        five_to_weekly_capacity=0.18,
        used_default_forecast=False,
    )


def _fleet(now):
    """The shape that produced the failure: the session's own account nearly out
    of five-hour capacity, alternatives holding real room."""
    return [
        _account("tight", 0.03, 0.37, five_reset=2 * HOUR,
                 weekly_reset=120 * HOUR, now=now),
        _account("roomy", 0.96, 0.02, five_reset=4 * HOUR,
                 weekly_reset=50 * HOUR, now=now),
        _account("other", 0.14, 0.14, five_reset=2 * HOUR,
                 weekly_reset=45 * HOUR, now=now),
    ]


def test_a_deferred_plan_still_names_an_account():
    """The defect, directly."""
    now = time.time()
    solution = _solve(_fleet(now), now, "tight")
    assert solution is not None, (
        "the solve routed flow and the caller was told there was no plan; every "
        "such decision fell through to a sort with no stickiness"
    )
    assert solution.account_id in {"tight", "roomy", "other"}


def test_the_same_fleet_without_a_current_account_is_unchanged():
    """The no-current-account path was always fine and must stay fine."""
    now = time.time()
    assert _solve(_fleet(now), now, None) is not None


def test_an_account_that_can_serve_now_is_kept():
    """Stickiness is the point of the migration cost. It only ever worked when
    the solve returned something at all."""
    now = time.time()
    accounts = [
        _account("mine", 0.9, 0.5, five_reset=4 * HOUR,
                 weekly_reset=100 * HOUR, now=now),
        _account("theirs", 0.9, 0.5, five_reset=4 * HOUR,
                 weekly_reset=100 * HOUR, now=now),
    ]
    solution = _solve(accounts, now, "mine")
    assert solution is not None
    assert solution.account_id == "mine", (
        "two equal accounts and it moved the session anyway; that is a cache "
        "rebuild bought for nothing"
    )


def test_a_fleet_with_nothing_left_still_reports_no_plan():
    """The guard is narrowed, not removed. Genuinely no capacity must still be
    None — reporting an account here would route into a wall."""
    now = time.time()
    accounts = [
        _account("spent", 0.0, 0.0, five_reset=HOUR, weekly_reset=HOUR, now=now),
        _account("also-spent", 0.0, 0.0, five_reset=HOUR, weekly_reset=HOUR,
                 now=now),
    ]
    assert _solve(accounts, now, "spent") is None


def test_an_unknown_current_account_does_not_erase_the_plan():
    """A stale route naming an account that no longer exists put a migration
    cost on everything and produced None, which is the worst possible moment to
    have no opinion."""
    now = time.time()
    solution = _solve(_fleet(now), now, "an-account-that-was-removed")
    assert solution is not None
