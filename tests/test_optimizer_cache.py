"""The optimizer cache must not be invalidated by the act of using it.

A routing decision writes a route lease. That lease raises the active-family
count on the account it chose. The count was part of the cache key, so the next
decision looked up a key that the previous decision had just changed — the
cache was destroyed by its own consumers, and could never hit during a burst,
which is the only situation it was written for. The comment above it says so
in as many words: "prevents a reconnect storm from serializing dozens of
identical min-cost-flow solves inside SQLite's write transaction."

Measured on a copy of the production database, 24 simultaneous decisions:

    cache key with load     hit rate  0%   p95 13.76s   24 solves
    cache key without       hit rate 96%   p95  0.62s    1 solve

The data plane's coordinator timeout is 15 seconds, so the first figure is not
a performance note. Live, it showed up as ~10-19s route decisions and a data
plane log filling with "coordinator unreachable — holding the request", which
is a wait the user sees and which no quota shortage caused.

Routing outcome was identical in both arms (24/24 to the same account), so the
load tie-break was not spreading anything it now stops spreading.

The tests below are sequential on purpose. Threads would make this flaky, and
the defect does not need concurrency to show: one decision changing the count
is enough to miss the next lookup.
"""

from __future__ import annotations

import time

from claude_fleet import control as control_module

from conftest import quota

HOUR = 3600


def _fleet(control, now):
    """Two roomy accounts, so the solver has a real choice to make."""
    for name, weekly in (("alpha", 0.2), ("beta", 0.3)):
        control.upsert_account(name)
        quota(control, name, weekly=weekly, five_hour=0.1,
              weekly_reset=now + 3 * 24 * HOUR, five_reset=now + 4 * HOUR)


def _decide(control, session):
    control.register_execution(session, workload_class="interactive")
    generation = control.acquire_writer(session, f"holder-{session}")
    return control.decide_route(session, f"holder-{session}", generation)


def test_a_second_decision_reuses_the_first_solve(control, now, monkeypatch):
    """The defect, stated as the smallest case that shows it."""
    calls = {"n": 0}
    real = control_module.solve_rolling_horizon

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(control_module, "solve_rolling_horizon", counting)
    _fleet(control, now)

    _decide(control, "s1")
    assert calls["n"] == 1, "the first decision has nothing to reuse"

    _decide(control, "s2")
    assert calls["n"] == 1, (
        "the second decision solved again. The only thing that changed between "
        "them is the route lease the first one wrote — so the cache is being "
        "invalidated by its own consumers and cannot hit during a burst"
    )


def test_a_changed_quota_reading_still_forces_a_new_solve(control, now,
                                                          monkeypatch):
    """The guard against over-correcting.

    Load was dropped from the key because it is a tie-break the cache can
    afford to see five seconds late. Quota is not: a plan computed against an
    account that has since been spent would route into a wall. Widening the
    exemption to "cache everything for five seconds" would trade a latency bug
    for a correctness one.
    """
    calls = {"n": 0}
    real = control_module.solve_rolling_horizon

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(control_module, "solve_rolling_horizon", counting)
    _fleet(control, now)

    _decide(control, "s1")
    assert calls["n"] == 1

    quota(control, "alpha", weekly=0.95)
    _decide(control, "s2")
    assert calls["n"] == 2, (
        "an account's remaining quota changed and the cached plan was reused "
        "anyway; that plan describes a fleet that no longer exists"
    )


def test_the_cache_still_expires(control, now, monkeypatch):
    """A cache that never expires is a frozen plan.

    The five-second bound is what makes every other staleness in the key
    acceptable, including the load staleness this change introduces.
    """
    calls = {"n": 0}
    real = control_module.solve_rolling_horizon

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(control_module, "solve_rolling_horizon", counting)
    _fleet(control, now)

    _decide(control, "s1")
    assert calls["n"] == 1

    real_time = time.time
    monkeypatch.setattr(
        control_module.time, "time",
        lambda: real_time() + control_module.OPTIMIZER_CACHE_SECONDS + 1,
    )
    _decide(control, "s2")
    assert calls["n"] == 2
