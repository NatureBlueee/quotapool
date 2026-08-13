"""A reserve on a shared window is worth less as that window runs out.

The floor on `shared@example.com` exists so the other party can use their
share. What it protects is their OPPORTUNITY to use it, and that opportunity
shrinks with the window — quota nobody spends before the reset is lost to
everyone, not saved for anyone.

Measured when this was asked for: 0.51 used, a 0.4 floor, 46 hours left. Fleet
could touch 0.09 of the subscription while roughly a third of the week's quota
was on course to expire unused.

The taper is a function of the clock, deliberately, rather than an edit to the
stored floor. Lowering the number by hand would carry into the NEXT window,
where the other party's opportunity is whole again — a temporary judgement
becoming permanent policy on somebody else's subscription, silently.

It never reaches zero. A quarter of nominal survives to the reset so a party
who starts late still finds room: the taper releases the part that was going to
expire, not the whole reserve.
"""

from __future__ import annotations

import time

from claude_fleet.control import (
    SHARED_FLOOR_TAPER_FLOOR,
    SHARED_FLOOR_TAPER_HOURS,
    _tapered_shared_floor,
)

HOUR = 3600


def _at(hours_left: float, floor: float = 0.4) -> float:
    now = 1_700_000_000.0
    return _tapered_shared_floor(floor, now + hours_left * HOUR, now)


def test_a_window_with_room_left_keeps_the_whole_reserve():
    """Most of the time this must do nothing at all."""
    assert _at(SHARED_FLOOR_TAPER_HOURS + 1) == 0.4
    assert _at(120) == 0.4


def test_the_reserve_shrinks_as_the_window_closes():
    early = _at(SHARED_FLOOR_TAPER_HOURS)
    mid = _at(SHARED_FLOOR_TAPER_HOURS / 2)
    late = _at(1)
    assert early > mid > late, (
        "the whole point is that an hour before reset the reserve is protecting "
        "an opportunity that no longer exists"
    )


def test_a_meaningful_reserve_survives_to_the_reset():
    """Never zero. Someone may start late, and then the reserve is real again.

    Asserted as an ABSOLUTE floor, not against the constant. The first version
    of this test read `_at(0) == 0.4 * SHARED_FLOOR_TAPER_FLOOR`, which is the
    constant checking itself: setting it to zero left the whole suite green
    while the taper released the other party's entire reserve. A test that
    cannot fail in the dangerous direction is not protecting anything.
    """
    assert _at(0) >= 0.05, (
        "the taper released essentially all of a 0.4 reserve at the reset; "
        "someone starting late would find the window already spent"
    )
    assert _at(0) < 0.4, "and it must actually taper"
    assert _at(-5) >= 0.05, (
        "a window whose reset has passed but whose reading has not refreshed "
        "must not release the reserve entirely"
    )


def test_an_account_with_no_reset_time_is_left_alone():
    """No clock, no taper. Guessing here would spend someone else's quota on an
    assumption."""
    assert _tapered_shared_floor(0.4, None) == 0.4
    assert _tapered_shared_floor(0.4, 0) == 0.4


def test_an_account_with_no_floor_is_unaffected():
    assert _at(1, floor=0.0) == 0.0


def test_the_measured_case(control, now):
    """The state that prompted this, end to end through the eligibility path.

    0.51 used against a 0.4 floor with 46 hours left: 0.09 usable before, and
    the taper roughly doubles it without touching the stored floor.
    """
    control.upsert_account("shared", kind="shared", weekly_floor=0.4)
    with control.db.transaction() as conn:
        conn.execute(
            "UPDATE accounts SET weekly_used=0.51, weekly_reset_at=?, "
            "five_hour_used=0.0, five_hour_reset_at=? WHERE id='shared'",
            (now + 46.2 * HOUR, now + 2 * HOUR),
        )
        row = conn.execute("SELECT * FROM accounts WHERE id='shared'").fetchone()

    stored = row["weekly_floor"]
    remaining = control._remaining(row, "weekly_used", "weekly_floor", now)

    assert stored == 0.4, "the stored floor must not be edited by the taper"
    assert 0.15 < remaining < 0.25, remaining


def test_an_owned_account_is_never_tapered(control, now):
    """Only a shared window has another party to protect. Applying this to an
    owned account would be releasing a reserve that means something else."""
    control.upsert_account("mine", kind="owned", weekly_floor=0.4)
    with control.db.transaction() as conn:
        conn.execute(
            "UPDATE accounts SET weekly_used=0.51, weekly_reset_at=? "
            "WHERE id='mine'", (now + 1 * HOUR,))
        row = conn.execute("SELECT * FROM accounts WHERE id='mine'").fetchone()

    assert control._remaining(row, "weekly_used", "weekly_floor", now) == (
        1 - 0.51 - 0.4)


def test_the_five_hour_floor_is_never_tapered(control, now):
    """A five-hour window resets on its own within hours; there is no waste to
    reclaim, and releasing that reserve would just spend the other party's
    burst capacity."""
    control.upsert_account("shared", kind="shared")
    with control.db.transaction() as conn:
        conn.execute(
            "UPDATE accounts SET five_hour_used=0.2, five_hour_floor=0.5, "
            "five_hour_reset_at=?, weekly_reset_at=? WHERE id='shared'",
            (now + 0.1 * HOUR, now + 1 * HOUR))
        row = conn.execute("SELECT * FROM accounts WHERE id='shared'").fetchone()

    assert control._remaining(
        row, "five_hour_used", "five_hour_floor", now) == 1 - 0.2 - 0.5
