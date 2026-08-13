"""A quota window whose reset time has passed no longer describes anything.

`report_quota` writes every field as COALESCE(?, old), so a NULL means "not
reported" and cannot clear a value. The data plane, meanwhile, throws a bucket
away as soon as its window rolls over. Nothing carried that across, so the
control plane kept the last utilisation for ever.

Measured on the live Mac: `primary@example.com` at weekly_used=1.0 with a
reset stamp sixteen hours in the past, cleared to null on the data plane,
treated as an exhausted account by the scheduler while sessions waited for
capacity that had already come back.
"""

from __future__ import annotations

import time

from conftest import quota

HOUR = 3600


def _row(db, account_id):
    with db.transaction() as conn:
        return dict(conn.execute(
            "SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone())


def test_a_rolled_over_weekly_window_becomes_spendable(control, db, now):
    """The exact live state, reproduced."""
    control.upsert_account("rolled")
    quota(control, "rolled", weekly=1.0, five_hour=0.0,
          weekly_reset=now - 16 * HOUR, five_reset=now + HOUR)

    control.register_execution("s1", workload_class="interactive")
    generation = control.acquire_writer("s1", "holder-1")
    route = control.decide_route("s1", "holder-1", generation)

    assert route["account_id"] == "rolled", (
        "an account whose weekly window rolled over sixteen hours ago was still "
        f"being treated as exhausted (reason={route.get('reason')})"
    )
    after = _row(db, "rolled")
    assert after["weekly_used"] == 0.0
    assert after["weekly_reset_at"] is None, (
        "the stale reset stamp must go too, or the next pass keeps re-expiring "
        "against a timestamp that no longer means anything"
    )


def test_zero_not_unknown(control, db, now):
    """Clearing to NULL would be worse than the bug it fixes.

    `decide_route` drops accounts with an unreadable bucket from the optimizer,
    so an account cleared to unknown goes from wrongly-spent to invisible. A new
    window starts at zero; that is what a reset means.
    """
    control.upsert_account("rolled")
    control.upsert_account("known")
    quota(control, "rolled", weekly=1.0, five_hour=1.0,
          weekly_reset=now - HOUR, five_reset=now - HOUR)
    quota(control, "known", weekly=0.5, five_hour=0.5,
          weekly_reset=now + 3 * 24 * HOUR, five_reset=now + 2 * HOUR)

    control.register_execution("s1", workload_class="interactive")
    generation = control.acquire_writer("s1", "holder-1")
    control.decide_route("s1", "holder-1", generation)

    after = _row(db, "rolled")
    assert after["weekly_used"] == 0.0 and after["five_hour_used"] == 0.0, (
        "both buckets must read as a fresh window, not as unknown"
    )


def test_a_live_window_is_never_touched(control, db, now):
    """Only windows that have actually passed."""
    control.upsert_account("live")
    quota(control, "live", weekly=0.7, five_hour=0.3,
          weekly_reset=now + 2 * HOUR, five_reset=now + HOUR)

    control.register_execution("s1", workload_class="interactive")
    generation = control.acquire_writer("s1", "holder-1")
    control.decide_route("s1", "holder-1", generation)

    after = _row(db, "live")
    assert after["weekly_used"] == 0.7 and after["five_hour_used"] == 0.3


def test_a_shared_account_is_left_stale_on_purpose(control, db, now):
    """Requirement (b) outranks tidiness.

    Assuming zero on a shared subscription means assuming the other party has
    not touched the new window either, which nothing here can know. Stale and
    cautious is the correct side to fail on; reconcile surfaces it instead.
    """
    control.upsert_account("shared", kind="shared", weekly_floor=0.4)
    control.upsert_account("owned")
    quota(control, "shared", weekly=1.0, five_hour=0.0,
          weekly_reset=now - 16 * HOUR, five_reset=now + HOUR)
    quota(control, "owned", weekly=0.2, five_hour=0.0,
          weekly_reset=now + 3 * 24 * HOUR, five_reset=now + 2 * HOUR)

    control.register_execution("s1", workload_class="interactive")
    generation = control.acquire_writer("s1", "holder-1")
    control.decide_route("s1", "holder-1", generation)

    after = _row(db, "shared")
    assert after["weekly_used"] == 1.0, (
        "a shared account must not be assumed fresh just because its window "
        "rolled over — the other party's usage is unobserved"
    )
