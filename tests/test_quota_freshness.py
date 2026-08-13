"""`quota_updated_at` must mean "when we last LEARNED a number".

Not "when we last heard from the data plane". The two coincide almost always,
which is exactly why the difference went unnoticed for four days.

Observed on the server, the same shared subscription on two hosts:

    本机  已用=0.38  重置=65.2h后    读数=2.1 分钟前
    硅谷  已用=1.0   重置=102.8h前   读数=0.2 小时前

The server's reading was twelve minutes old and its value was four days stale.
The data plane had rolled its own window over and was reporting null for that
account on every sync; the values are COALESCE'd so they survived, but the
timestamp was written unconditionally, so it kept moving.

Three consumers read that timestamp as data age and all three were lied to:
the desktop panel's "quota seen Xm ago", reconcile's
`SHARED_WINDOW_ROLLED_OVER_UNREAD.reading_age_hours` — which reported 0.2 hours
for a four-day-old reading, i.e. the check built to catch this exact condition
was blinded by it — and anyone reading `fleet-health`.

A null report is not news. It must not look like news.
"""

from __future__ import annotations

import time


def _quota_row(control, account_id):
    with control.db.transaction() as conn:
        return conn.execute(
            "SELECT weekly_used, five_hour_used, quota_updated_at "
            "FROM accounts WHERE id=?", (account_id,)
        ).fetchone()


def test_a_report_carrying_no_reading_does_not_refresh_the_timestamp(control):
    """The bug, stated directly."""
    control.sync_account_runtime("acct", weekly_used=0.5, five_hour_used=0.1)
    learned = _quota_row(control, "acct")["quota_updated_at"]

    time.sleep(0.01)
    control.sync_account_runtime("acct", weekly_used=None, five_hour_used=None)

    after = _quota_row(control, "acct")
    assert after["weekly_used"] == 0.5, "COALESCE keeps the old value — unchanged"
    assert after["quota_updated_at"] == learned, (
        "the timestamp moved while the value did not, which is the whole "
        "defect: it makes a stale number look freshly measured"
    )


def test_a_report_carrying_a_reading_does_refresh_it(control):
    """The other half. A guard that never lets the timestamp move would be
    worse than the bug — every reading would look infinitely old and the
    staleness checks would fire constantly."""
    control.sync_account_runtime("acct", weekly_used=0.5, five_hour_used=0.1)
    first = _quota_row(control, "acct")["quota_updated_at"]

    time.sleep(0.01)
    control.sync_account_runtime("acct", weekly_used=0.6, five_hour_used=0.2)

    after = _quota_row(control, "acct")
    assert after["weekly_used"] == 0.6
    assert after["quota_updated_at"] > first


def test_either_reading_alone_counts_as_news(control):
    """The two windows are reported independently and one can arrive without
    the other — a five-hour bucket that has rolled over reports null while the
    weekly figure is still live. Requiring both would throw away half the
    updates."""
    control.sync_account_runtime("acct", weekly_used=0.5, five_hour_used=0.1)
    first = _quota_row(control, "acct")["quota_updated_at"]

    time.sleep(0.01)
    control.sync_account_runtime("acct", weekly_used=0.7, five_hour_used=None)
    weekly_only = _quota_row(control, "acct")["quota_updated_at"]
    assert weekly_only > first

    time.sleep(0.01)
    control.sync_account_runtime("acct", weekly_used=None, five_hour_used=0.3)
    assert _quota_row(control, "acct")["quota_updated_at"] > weekly_only


def test_a_first_sight_of_an_account_is_not_a_reading(control):
    """An account that has never reported a number must not claim a fresh one.

    This is the shape the server was actually in: the row existed, carried a
    value from days ago, and every sync since had been empty. A brand-new row
    with no reading is the same situation at time zero — if it starts out
    looking fresh, the staleness check can never fire for it.
    """
    control.sync_account_runtime("never-read", weekly_used=None, five_hour_used=None)
    row = _quota_row(control, "never-read")
    assert row["weekly_used"] is None
    assert row["quota_updated_at"] is None, (
        "no reading has ever arrived, so there is no reading age to report"
    )
