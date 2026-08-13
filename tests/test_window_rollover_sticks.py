"""A rolled-over window must not be undone by the reading it replaced.

The owner noticed it as "the server picks a refreshed account up quickly, the
Mac takes two or three hours". Measured on hello@:

    08-06 10:00:00  window due; expire_stale_windows fires, {"accounts": 1}
    08-06 10:00-12:49  weekly_used sits at 1.00 the whole time
    08-06 12:49:48  finally reads 0.00

Two hours fifty minutes of a fresh weekly window standing idle while sessions
waited on other accounts.

`expire_stale_windows` did its job at exactly the right second. What undid it
was the very next sync: the data plane still held its cached 1.00, and
`sync_account_runtime` wrote every field as COALESCE(?, old), so a stale value
overwrote the zero. And because expire also sets the reset to NULL, its own
guard — reset NOT NULL and in the past — could never fire again for that
account. One overwrite disarmed the safety net permanently.

`used = 0 AND reset IS NULL` is the fingerprint expire leaves and nothing else
produces: a live window carries a reset, an unmeasured one carries NULL used.
In that state a report with no window of its own cannot be describing the
current one.
"""

from __future__ import annotations

import time

from claude_fleet.control import expire_stale_windows

HOUR = 3600


def _row(control, account_id="acct"):
    with control.db.transaction() as conn:
        return conn.execute(
            "SELECT weekly_used, weekly_reset_at, five_hour_used, "
            "five_hour_reset_at FROM accounts WHERE id=?", (account_id,)
        ).fetchone()


def _rolled_over(control, now):
    """Put an account in the state expire leaves behind."""
    control.upsert_account("acct")
    control.report_quota("acct", weekly_used=1.0, weekly_reset_at=now - 60,
                         five_hour_used=1.0, five_hour_reset_at=now - 60)
    with control.db.transaction() as conn:
        assert expire_stale_windows(conn, now) == 1
    row = _row(control)
    assert row["weekly_used"] == 0.0 and row["weekly_reset_at"] is None
    return row


def test_the_stale_reading_cannot_undo_the_rollover(control, now):
    """The defect, exactly as it happened."""
    _rolled_over(control, now)

    # What the data plane keeps saying until it learns the new window.
    control.sync_account_runtime("acct", weekly_used=1.0, weekly_reset_at=None,
                                 five_hour_used=1.0, five_hour_reset_at=None)

    row = _row(control)
    assert row["weekly_used"] == 0.0, (
        "the cached 1.00 overwrote the rollover; the account goes on looking "
        "spent while a fresh window sits unused, and expire can never help "
        "again because it also cleared the reset"
    )
    assert row["five_hour_used"] == 0.0


def test_a_genuinely_fresh_reading_is_applied(control, now):
    """The other half. A report that carries a window IS news, and blocking it
    would freeze the account at zero — worse than the bug."""
    _rolled_over(control, now)

    control.sync_account_runtime("acct", weekly_used=0.04,
                                 weekly_reset_at=now + 7 * 24 * HOUR)

    row = _row(control)
    assert row["weekly_used"] == 0.04
    assert row["weekly_reset_at"] is not None


def test_an_ordinary_account_still_takes_readings_without_a_reset(control):
    """The guard is the rollover fingerprint, not "no reset time".

    Some accounts report usage with no reset at all. Ignoring those would stop
    the control plane learning anything about them, which is a much larger
    failure than the one being fixed.
    """
    control.upsert_account("acct")
    control.sync_account_runtime("acct", weekly_used=0.5, five_hour_used=0.1)
    control.sync_account_runtime("acct", weekly_used=0.6, five_hour_used=0.2)

    row = _row(control)
    assert row["weekly_used"] == 0.6
    assert row["five_hour_used"] == 0.2


def test_a_zero_reading_with_a_window_is_not_mistaken_for_a_rollover(control, now):
    """An account that genuinely reads zero INSIDE a live window must keep
    taking readings — it has a reset time, so it is not in the rolled-over
    state at all."""
    control.upsert_account("acct")
    control.report_quota("acct", weekly_used=0.0,
                         weekly_reset_at=now + 3 * 24 * HOUR)

    control.sync_account_runtime("acct", weekly_used=0.2, weekly_reset_at=None)

    assert _row(control)["weekly_used"] == 0.2


def test_the_five_hour_and_weekly_windows_roll_over_independently(control, now):
    """A five-hour window turns over many times inside one weekly window;
    tying them together would freeze whichever had not turned."""
    control.upsert_account("acct")
    control.report_quota("acct", weekly_used=0.4,
                         weekly_reset_at=now + 3 * 24 * HOUR,
                         five_hour_used=1.0, five_hour_reset_at=now - 60)
    with control.db.transaction() as conn:
        expire_stale_windows(conn, now)

    row = _row(control)
    assert row["five_hour_used"] == 0.0 and row["five_hour_reset_at"] is None
    assert row["weekly_used"] == 0.4, "the weekly window had not expired"

    # A stale five-hour figure must not come back; the weekly one must update.
    control.sync_account_runtime("acct", weekly_used=0.45,
                                 weekly_reset_at=now + 3 * 24 * HOUR,
                                 five_hour_used=1.0, five_hour_reset_at=None)
    row = _row(control)
    assert row["five_hour_used"] == 0.0
    assert row["weekly_used"] == 0.45
