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


def test_severe_load_skew_cannot_starve_a_healthy_account(control, monkeypatch):
    """A rolling plan must eventually commit to the account it keeps saving.

    This reproduces the live max-4 shape: a healthy account already carries
    many active task families while a full, later-resetting account remains
    idle.  The old receding horizon always planned to use the idle account
    later, then moved that later slot forward on every new request forever.
    """
    now = time.time()
    # There is enough observed demand to spend the current window before its
    # reset, so a modest balancing deviation does not endanger expiring quota.
    monkeypatch.setattr(control, "_forecast", lambda *a, **kw: (0.2, 0.2, False))
    control.upsert_account("busy")
    control.upsert_account("idle")
    quota(control, "busy", weekly=0.2, five_hour=0.1,
          weekly_reset=now + 3 * 24 * HOUR, five_reset=now + 2 * HOUR)
    quota(control, "idle", weekly=0.0, five_hour=0.0,
          weekly_reset=now + 5 * 24 * HOUR, five_reset=now + 5 * HOUR)

    for index in range(8):
        session = f"busy-{index}"
        holder = f"holder-{index}"
        control.register_execution(session, workload_class="interactive")
        generation = control.acquire_writer(session, holder)
        control.set_route_policy(session, "pin", "busy")
        control.decide_route(session, holder, generation)

    control.register_execution("new", workload_class="interactive")
    generation = control.acquire_writer("new", "holder-new")
    route = control.decide_route("new", "holder-new", generation)

    assert route["account_id"] == "idle"
    assert route["reason"] == "anti_starvation"
    assert route["optimizer"]["planned_account_id"] == "busy"
    assert route["optimizer"]["dynamic_policy"]["action"] == "balance_new_session"


def test_severe_skew_does_not_divert_endangered_expiring_capacity(control):
    now = time.time()
    control.upsert_account("soon")
    control.upsert_account("later")
    quota(control, "soon", weekly=0.58, five_hour=0.75,
          weekly_reset=now + HOUR / 2, five_reset=now + 2 * HOUR)
    quota(control, "later", weekly=0.14, five_hour=0.0,
          weekly_reset=now + 16 * HOUR, five_reset=now + 5 * HOUR)
    for index in range(8):
        session, holder = f"urgent-{index}", f"holder-{index}"
        control.register_execution(session, workload_class="interactive")
        generation = control.acquire_writer(session, holder)
        control.set_route_policy(session, "pin", "soon")
        control.decide_route(session, holder, generation)
    control.register_execution("new", workload_class="interactive")
    generation = control.acquire_writer("new", "holder-new")
    route = control.decide_route("new", "holder-new", generation)
    assert route["account_id"] == "soon"
    policy = route["optimizer"]["dynamic_policy"]
    assert policy["action"] == "protect_expiring_capacity"
    assert policy["expiry_pressure"]["soon"]["bucket"] == "weekly"
    assert policy["expiry_pressure"]["soon"]["at_risk_weekly_fraction"] > 0


def test_forecast_cache_reuses_observed_history(control, monkeypatch):
    calls = []

    def fake_forecast(rows, *, active_families, now):
        calls.append((list(rows), active_families, now))
        return (0.18, 0.03, False)

    monkeypatch.setattr("claude_fleet.control.estimate_forecast", fake_forecast)
    with control.db.connect() as conn:
        first = control._forecast(conn, active_families=2, now=1000.0)
        second = control._forecast(conn, active_families=9, now=1059.0)

    assert first == second == (0.18, 0.03, False)
    assert len(calls) == 1


def test_default_forecast_cache_tracks_active_family_count(control, monkeypatch):
    calls = []

    def fake_forecast(rows, *, active_families, now):
        calls.append(active_families)
        return (0.18, active_families * 0.001, True)

    monkeypatch.setattr("claude_fleet.control.estimate_forecast", fake_forecast)
    with control.db.connect() as conn:
        control._forecast(conn, active_families=2, now=1000.0)
        control._forecast(conn, active_families=3, now=1001.0)

    assert calls == [2, 3]


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
