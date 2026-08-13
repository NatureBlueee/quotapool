"""A reading carried from another host is as old as when it was TAKEN.

Quota belongs to the subscription, not the host: both hosts spend from one
bucket, so the freshest reading anywhere is the best estimate everywhere. What
made carrying it necessary was a deadlock, not a delay — measured on the
server, `085a6c9c` read weekly_used=1.0 with a reset 105 hours in the past
while the Mac read the same subscription at 0.39 with 63 hours left. Readings
arrive on the response of a real request; the account looked spent, so nothing
was routed to it; nothing was routed to it, so no reading arrived. Seven days,
674 routing decisions, zero of them to that account.

Carrying the reading across breaks that. Stamping it with arrival time would
recreate, in the fix, the exact defect being fixed: a row claiming a freshness
it does not have, believed by every staleness check downstream.

The backwards guard matters because the sync runs in both directions. Without
it, two hosts could walk a row between two old readings for ever.
"""

from __future__ import annotations

import time


HOUR = 3600


def _row(control, account_id):
    with control.db.transaction() as conn:
        return conn.execute(
            "SELECT weekly_used, quota_updated_at FROM accounts WHERE id=?",
            (account_id,),
        ).fetchone()


def test_a_carried_reading_keeps_the_time_it_was_taken(control):
    """The whole point. `observed_at` is when, not when-it-arrived."""
    control.upsert_account("acct")
    taken = time.time() - 12 * 60
    control.report_quota("acct", weekly_used=0.42, observed_at=taken)

    row = _row(control, "acct")
    assert row["weekly_used"] == 0.42
    assert abs(row["quota_updated_at"] - taken) < 1, (
        "the row claims to have been measured now; it was measured twelve "
        "minutes ago on another host, and every staleness check reads this "
        "field as the age of the number"
    )


def test_a_local_report_still_means_now(control):
    """The default path, unchanged.

    A data plane reporting its own response headers took the reading at the
    moment it reports it, which is why this was `now` and nothing noticed.
    """
    control.upsert_account("acct")
    before = time.time()
    control.report_quota("acct", weekly_used=0.42)
    assert _row(control, "acct")["quota_updated_at"] >= before


def test_an_older_reading_is_refused(control):
    """It can only lose information, and the sync runs both ways.

    Without this, host A pushes its 10:00 reading to B, B pushes its 09:00
    reading back, and the pair walks the row backwards for ever.
    """
    control.upsert_account("acct")
    newer = time.time() - 60
    control.report_quota("acct", weekly_used=0.42, observed_at=newer)

    result = control.report_quota(
        "acct", weekly_used=0.99, observed_at=newer - 3 * HOUR)

    assert result.get("applied") is False
    row = _row(control, "acct")
    assert row["weekly_used"] == 0.42, "the older reading overwrote the newer one"
    assert abs(row["quota_updated_at"] - newer) < 1


def test_the_plan_never_creates_an_account(tmp_path):
    """An account only one host knows must not be copied into the other.

    Creating an account row the local data plane cannot execute is the shape
    that has 502'd this fleet, and it has been produced twice by accident
    already — both times by a client whose account id fell back to a name.
    """
    import importlib.util

    spec = importlib.util.spec_from_loader("qsync", loader=None)
    module = importlib.util.module_from_spec(spec)
    source = open("scripts/fleet-quota-sync", encoding="utf-8").read()
    # A script that resolves its own location needs `__file__`, and every real
    # import provides it. Omitting it made the loader unfaithful in exactly the
    # way that hides a working script behind a NameError.
    module.__dict__["__file__"] = "scripts/fleet-quota-sync"
    exec(compile(source.replace('if __name__ == "__main__":\n    sys.exit(main())',
                                ""), "fleet-quota-sync", "exec"), module.__dict__)

    now = time.time()
    source_accounts = {
        "known": {"name": "known", "kind": "owned", "state": "healthy",
                  "five_hour_used": 0.1, "five_hour_reset_at": now + HOUR,
                  "weekly_used": 0.4, "weekly_reset_at": now + 48 * HOUR,
                  "quota_updated_at": now - 60, "disabled": False},
        "only-here": {"name": "only-here", "kind": "owned", "state": "healthy",
                      "five_hour_used": 0.1, "five_hour_reset_at": now + HOUR,
                      "weekly_used": 0.4, "weekly_reset_at": now + 48 * HOUR,
                      "quota_updated_at": now - 60, "disabled": False},
    }
    target = {
        "known": {"name": "known", "kind": "owned", "state": "healthy",
                  "five_hour_used": None, "five_hour_reset_at": None,
                  "weekly_used": 1.0, "weekly_reset_at": now - 100 * HOUR,
                  "quota_updated_at": now - 5 * HOUR, "disabled": False},
    }

    moves = module._plan(source_accounts, target, now)
    assert [m["account_id"] for m in moves] == ["known"]


def test_the_plan_refuses_a_reading_that_is_itself_old(tmp_path):
    """Older than the receiver is not the only way to be useless.

    A reading six hours old may describe a window that has since rolled over.
    Leaving the receiver visibly stale is better: reconcile reports that, and a
    fresh-looking timestamp on an ancient number reports nothing.
    """
    import importlib.util

    spec = importlib.util.spec_from_loader("qsync", loader=None)
    module = importlib.util.module_from_spec(spec)
    source = open("scripts/fleet-quota-sync", encoding="utf-8").read()
    # A script that resolves its own location needs `__file__`, and every real
    # import provides it. Omitting it made the loader unfaithful in exactly the
    # way that hides a working script behind a NameError.
    module.__dict__["__file__"] = "scripts/fleet-quota-sync"
    exec(compile(source.replace('if __name__ == "__main__":\n    sys.exit(main())',
                                ""), "fleet-quota-sync", "exec"), module.__dict__)

    now = time.time()
    ancient = {
        "a": {"name": "a", "kind": "owned", "state": "healthy",
              "five_hour_used": 0.1, "five_hour_reset_at": now,
              "weekly_used": 0.4, "weekly_reset_at": now + 48 * HOUR,
              "quota_updated_at": now - 10 * HOUR, "disabled": False},
    }
    target = {
        "a": {"name": "a", "kind": "owned", "state": "healthy",
              "five_hour_used": None, "five_hour_reset_at": None,
              "weekly_used": 1.0, "weekly_reset_at": now - 100 * HOUR,
              "quota_updated_at": now - 100 * HOUR, "disabled": False},
    }
    assert module._plan(ancient, target, now) == []
