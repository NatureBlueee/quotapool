"""Requirement (a): spend quota before its window resets.

The owner's complaint, in his words: one account's weekly quota refreshes while
it still sat unused, and another was drained. The scheduler has a deadline
objective that should prevent exactly that, but a load pre-filter ran BEFORE the
optimizer and removed accounts from consideration entirely — so the objective
never saw them.
"""

from __future__ import annotations

import time

from conftest import quota


HOUR = 3600


def test_expiring_capacity_beats_an_idle_account(control):
    """The core requirement-(a) case.

    `soon` holds most of a weekly window that resets in an hour and is already
    carrying one session. `fresh` just reset and is idle. The pre-filter used to
    drop `soon` purely for being busy, guaranteeing that its quota expired
    unused — which is the failure the owner reported.
    """
    now = time.time()
    control.upsert_account("soon")
    control.upsert_account("fresh")
    # soon: 30% used, resets in 1h  -> 70% of a window about to vanish
    quota(control, "soon", weekly=0.3, five_hour=0.0,
          weekly_reset=now + HOUR, five_reset=now + HOUR)
    # fresh: 10% used, resets in 7 days -> no urgency at all
    quota(control, "fresh", weekly=0.1, five_hour=0.0,
          weekly_reset=now + 7 * 24 * HOUR, five_reset=now + 5 * HOUR)

    # Give `soon` an existing session so it is the busier account.
    control.register_execution("busy", workload_class="interactive")
    busy_gen = control.acquire_writer("busy", "holder-busy")
    control.set_route_policy("busy", "pin", "soon")
    control.decide_route("busy", "holder-busy", busy_gen)

    # A new session arrives. It should go where the quota is about to expire.
    control.register_execution("new", workload_class="interactive")
    new_gen = control.acquire_writer("new", "holder-new")
    route = control.decide_route("new", "holder-new", new_gen)

    assert route["account_id"] == "soon", (
        "capacity expiring in an hour must outrank an idle account whose window "
        f"resets in a week (reason={route.get('reason')})"
    )


def test_load_still_breaks_a_tie_between_equal_accounts(control):
    """Removing the pre-filter must not make load irrelevant.

    With nothing to distinguish two accounts on urgency, the idle one is still
    the better choice — that is what keeps a fan-out spread out.
    """
    now = time.time()
    for account_id in ("a", "b"):
        control.upsert_account(account_id)
        quota(control, account_id, weekly=0.2, five_hour=0.0,
              weekly_reset=now + 3 * 24 * HOUR, five_reset=now + 4 * HOUR)

    control.register_execution("busy", workload_class="interactive")
    busy_gen = control.acquire_writer("busy", "holder-busy")
    control.set_route_policy("busy", "pin", "a")
    control.decide_route("busy", "holder-busy", busy_gen)

    control.register_execution("new", workload_class="interactive")
    new_gen = control.acquire_writer("new", "holder-new")
    route = control.decide_route("new", "holder-new", new_gen)
    assert route["account_id"] == "b"


def test_priority_still_outranks_urgency(control):
    """Operator policy is above the automatic objective, and stays there."""
    now = time.time()
    control.upsert_account("preferred_tier", priority=0)
    control.upsert_account("backup_tier", priority=10)
    # The low-priority account is the urgent one; priority must still win.
    quota(control, "preferred_tier", weekly=0.1, five_hour=0.0,
          weekly_reset=now + 7 * 24 * HOUR, five_reset=now + 5 * HOUR)
    quota(control, "backup_tier", weekly=0.3, five_hour=0.0,
          weekly_reset=now + HOUR, five_reset=now + HOUR)

    control.register_execution("s1", workload_class="interactive")
    generation = control.acquire_writer("s1", "holder-1")
    route = control.decide_route("s1", "holder-1", generation)
    assert route["account_id"] == "preferred_tier"


def test_shared_floor_holds_against_urgency_outside_the_taper(control):
    """Requirement (b) outranks requirement (a) — for most of the window.

    An expiring shared window is the most tempting thing the scheduler can see,
    and away from the reset it must still refuse to spend into the reserve.

    This test used to assert that with the reset ONE HOUR away, and the owner
    countermanded it: a reserve nobody can spend before the window turns over is
    not protection, it is waste. `_tapered_shared_floor` releases part of it in
    the last three days. What survives that decision is asserted below and in
    test_shared_floor_taper.py — the reserve is never released entirely, and it
    is untouched outside the taper.
    """
    now = time.time()
    control.upsert_account("shared", kind="shared", weekly_floor=0.4)
    control.upsert_account("owned")
    # shared is urgent AND at its floor, with the taper not yet in play.
    quota(control, "shared", weekly=0.6, five_hour=0.0,
          weekly_reset=now + 96 * HOUR, five_reset=now + HOUR)
    quota(control, "owned", weekly=0.1, five_hour=0.0,
          weekly_reset=now + 7 * 24 * HOUR, five_reset=now + 5 * HOUR)

    control.register_execution("s1", workload_class="interactive")
    generation = control.acquire_writer("s1", "holder-1")
    route = control.decide_route("s1", "holder-1", generation)
    assert route["account_id"] == "owned"


def test_the_taper_never_opens_the_whole_reserve(control):
    """The boundary the owner's instruction did NOT move.

    "Use it moderately as the reset approaches" is not "spend the other party's
    share". Even with the reset an hour away and the shared account otherwise
    the most attractive thing in the fleet, a request that would push it past
    the tapered floor must still be refused.
    """
    now = time.time()
    control.upsert_account("shared", kind="shared", weekly_floor=0.4)
    control.upsert_account("owned")
    # 0.92 used leaves 0.08, below even the fully-tapered floor of 0.10.
    quota(control, "shared", weekly=0.92, five_hour=0.0,
          weekly_reset=now + HOUR, five_reset=now + HOUR)
    quota(control, "owned", weekly=0.1, five_hour=0.0,
          weekly_reset=now + 7 * 24 * HOUR, five_reset=now + 5 * HOUR)

    control.register_execution("s2", workload_class="interactive")
    generation = control.acquire_writer("s2", "holder-2")
    route = control.decide_route("s2", "holder-2", generation)
    assert route["account_id"] == "owned", (
        "the taper released the reserve past its floor; someone starting late "
        "would find the window already spent"
    )
