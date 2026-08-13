"""Load must mean work that is happening, not work that once happened.

Found on the live Mac: 4,344 executions were `status='active'` with a live
writer lease and not one of them had a surviving writer process. Counted as
load, the scheduler saw hello@ carrying 2,272 families and hey@ 1,676 — while
in the preceding half hour the real figures were 8 and 90. The load term was not
noisy, it was inverted, and at 20 points per family in `_score` a gap that size
buries every other term in the ranking.

The reason it accumulates is structural rather than a leak: the writer lease
runs 24 hours, and `renew_writer` returns without writing while the lease is
more than a quarter of that from expiry. So a live session can leave both
`executions.updated_at` and its lease untouched for eighteen hours, which rules
both out as liveness signals. `route_leases.updated_at` moves on every routing
decision, so that is the clock the counting now uses.
"""

from __future__ import annotations

import time

from claude_fleet.control import ACTIVE_LOAD_WINDOW_SECONDS

from conftest import quota

HOUR = 3600


def _age_lease(db, session_id, seconds):
    """Push a session's last routing decision into the past.

    Only the route lease moves. The execution stays `active` with a live writer
    lease, which is exactly the state the abandoned sessions were in — that is
    the point of the test.
    """
    with db.transaction() as conn:
        conn.execute(
            "UPDATE route_leases SET updated_at=? WHERE session_id=?",
            (time.time() - seconds, session_id),
        )


def _place(control, session, account):
    control.register_execution(session, workload_class="interactive")
    generation = control.acquire_writer(session, f"holder-{session}")
    control.set_route_policy(session, "pin", account)
    control.decide_route(session, f"holder-{session}", generation)
    control.set_route_policy(session, "pin", None)


def test_abandoned_sessions_do_not_count_as_load(control, db, now):
    """The exact inversion, reproduced.

    `stale` carries far more sessions than `busy`, but none of them has routed
    in hours. A new session must be placed as though `stale` were idle, because
    it is.
    """
    control.upsert_account("stale")
    control.upsert_account("busy")
    for account in ("stale", "busy"):
        quota(control, account, weekly=0.2, five_hour=0.0,
              weekly_reset=now + 3 * 24 * HOUR, five_reset=now + 3 * HOUR)

    for index in range(5):
        session = f"ghost-{index}"
        _place(control, session, "stale")
        _age_lease(db, session, ACTIVE_LOAD_WINDOW_SECONDS + HOUR)

    _place(control, "live", "busy")

    control.register_execution("new", workload_class="interactive")
    generation = control.acquire_writer("new", "holder-new")
    route = control.decide_route("new", "holder-new", generation)

    assert route["account_id"] == "stale", (
        "five abandoned sessions made an idle account look like the busiest "
        f"one (reason={route.get('reason')})"
    )


def test_a_session_that_just_routed_still_counts(control, db, now):
    """The window must not simply disable load accounting.

    Same shape as above with the ages removed: `loaded` really is carrying the
    work now, so the new session belongs on `idle`. Without this the fix would
    be indistinguishable from deleting the load term.
    """
    control.upsert_account("loaded")
    control.upsert_account("idle")
    for account in ("loaded", "idle"):
        quota(control, account, weekly=0.2, five_hour=0.0,
              weekly_reset=now + 3 * 24 * HOUR, five_reset=now + 3 * HOUR)

    for index in range(5):
        _place(control, f"recent-{index}", "loaded")

    control.register_execution("new", workload_class="interactive")
    generation = control.acquire_writer("new", "holder-new")
    route = control.decide_route("new", "holder-new", generation)

    assert route["account_id"] == "idle", (
        "load stopped being a tie-breaker at all "
        f"(reason={route.get('reason')})"
    )


def test_abandoned_sessions_do_not_consume_a_concurrency_limit(control, db, now):
    """The same count also enforces `max_concurrency`, where it is a hard gate.

    Worse than a skewed ranking: phantom rows can push an account to its limit
    and stop it being routed to at all, while the machine sits idle.
    """
    control.upsert_account("small", max_concurrency=2)
    quota(control, "small", weekly=0.1, five_hour=0.0,
          weekly_reset=now + 3 * 24 * HOUR, five_reset=now + 3 * HOUR)

    for index in range(5):
        session = f"ghost-{index}"
        _place(control, session, "small")
        _age_lease(db, session, ACTIVE_LOAD_WINDOW_SECONDS + HOUR)

    control.register_execution("new", workload_class="interactive")
    generation = control.acquire_writer("new", "holder-new")
    route = control.decide_route("new", "holder-new", generation)

    assert route["account_id"] == "small", (
        "five abandoned sessions exhausted a concurrency limit of 2 "
        f"(reason={route.get('reason')})"
    )
