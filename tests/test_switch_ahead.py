"""Phase 1: an established session leaves before it hits the wall.

Hitting the wall costs a failed request AND the cache rebuild the failover does
anyway. Moving first costs only the rebuild, so it is strictly cheaper — that
asymmetry is the entire justification, and it only holds for an ESTABLISHED
session. A new one has no cache to lose and the ranking already prefers a
roomier account, so applying the same margin there would park a session that
could have used the last 2% and waste quota to prevent nothing.

The margin is derived, not chosen: 0.02 is the largest single-step increase ever
observed in a weekly reading across 1,047 samples. Anything narrower cannot
promise that one more request stays under 100%; anything wider throws away quota
for a step size that has never occurred.
"""

from __future__ import annotations

import time

from claude_fleet.control import (
    SWITCH_AHEAD_COOLDOWN_SECONDS,
    WEEKLY_SWITCH_AHEAD_MARGIN,
)

from conftest import quota

HOUR = 3600


def _place(control, session, account, holder=None):
    holder = holder or f"holder-{session}"
    control.register_execution(session, workload_class="interactive")
    generation = control.acquire_writer(session, holder)
    control.set_route_policy(session, "pin", account)
    control.decide_route(session, holder, generation)
    control.set_route_policy(session, "pin", None)
    return generation


def _two_accounts(control, now, *, tight, roomy):
    control.upsert_account("tight")
    control.upsert_account("roomy")
    quota(control, "tight", weekly=tight, five_hour=0.0,
          weekly_reset=now + 2 * 24 * HOUR, five_reset=now + 4 * HOUR)
    quota(control, "roomy", weekly=roomy, five_hour=0.0,
          weekly_reset=now + 2 * 24 * HOUR, five_reset=now + 4 * HOUR)


def test_an_established_session_moves_before_the_wall(control, now):
    """1 − 0.985 < margin, so the session leaves rather than spend the tail."""
    _two_accounts(control, now, tight=0.985, roomy=0.3)
    generation = _place(control, "s1", "tight")

    route = control.decide_route("s1", "holder-s1", generation)

    assert route["account_id"] == "roomy"
    assert route["reason"] == "switch_ahead", (
        "the move must be labelled — `rolling_optimal` describes how the new "
        "account was picked and says nothing about leaving early, which is the "
        "only thing that distinguishes this feature from rebalancing, and the "
        "first acceptance criterion depends on being able to count it"
    )


def test_a_new_session_may_still_use_the_tail(control, now):
    """The asymmetry, asserted.

    Applying the margin to a new session would park it rather than let it spend
    the last 2% — quota thrown away to prevent a cost (cache rebuild) it does
    not have.
    """
    control.upsert_account("tight")
    quota(control, "tight", weekly=0.99, five_hour=0.0,
          weekly_reset=now + 2 * 24 * HOUR, five_reset=now + 4 * HOUR)

    control.register_execution("fresh", workload_class="interactive")
    generation = control.acquire_writer("fresh", "holder-fresh")
    route = control.decide_route("fresh", "holder-fresh", generation)

    assert route["account_id"] == "tight"


def test_the_margin_is_the_measured_maximum_step(control, now):
    """Just inside the margin moves; just outside it stays.

    Pins the boundary itself, so a later edit to the constant has to be
    deliberate rather than incidental.
    """
    _two_accounts(control, now, tight=1 - WEEKLY_SWITCH_AHEAD_MARGIN - 0.005,
                  roomy=0.3)
    generation = _place(control, "outside", "tight")
    assert control.decide_route("outside", "holder-outside",
                                generation)["account_id"] == "tight"

    quota(control, "tight", weekly=1 - WEEKLY_SWITCH_AHEAD_MARGIN + 0.005)
    assert control.decide_route("outside", "holder-outside",
                                generation)["account_id"] == "roomy"


def test_a_session_cannot_be_routed_back_to_what_it_just_left(control, now):
    """The loop, and why disabling the check was not enough to stop it.

    The margin only stops an existing session from STAYING. It never stopped one
    from being routed back in, and an arrival is not "existing" — so the next
    request made it existing again and moved it out again. Observed live on the
    server: session 501286e3 left ee6bca57 at 15:45 and again at 16:52, which it
    could only do by being routed back in between. The cooldown was not being
    defeated; it was setting the PERIOD of the loop.

    So the cooldown now excludes the account outright for that session, which is
    the only version of the guard that closes the cycle.
    """
    _two_accounts(control, now, tight=0.99, roomy=0.3)
    generation = _place(control, "s1", "tight")
    assert control.decide_route("s1", "holder-s1", generation)["reason"] == "switch_ahead"

    # Even asked for by name, it must refuse while the cooldown holds —
    # otherwise the round trip that produced the live loop is still available.
    control.set_workload_policy("w-back", hard_pin="tight")
    control.register_execution("s2", workload_class="interactive", workload_id="w-back")
    gen2 = control.acquire_writer("s2", "holder-s2")
    assert control.decide_route("s2", "holder-s2", gen2)["account_id"] == "tight", (
        "the cooldown is per SESSION — another session may still use the account"
    )

    for _ in range(3):
        route = control.decide_route("s1", "holder-s1", generation)
        assert route["account_id"] == "roomy", (
            "the session went back to the account it was just moved off; that "
            "is the loop"
        )


def test_the_cooldown_expires(control, now, monkeypatch):
    """A guard that never releases would silently disable the feature.

    Worse than no guard: it would look like switch-ahead simply stopped working,
    with nothing in the events to say why.
    """
    _two_accounts(control, now, tight=0.99, roomy=0.3)
    generation = _place(control, "s1", "tight")
    control.decide_route("s1", "holder-s1", generation)

    for key in list(control._switch_ahead_at):
        control._switch_ahead_at[key] = time.time() - SWITCH_AHEAD_COOLDOWN_SECONDS - 1

    control.set_route_policy("s1", "pin", "tight")
    control.decide_route("s1", "holder-s1", generation)
    control.set_route_policy("s1", "pin", None)
    assert control.decide_route(
        "s1", "holder-s1", generation)["reason"] == "switch_ahead"


def test_margin_zero_restores_the_previous_boundary(control, now, monkeypatch):
    """The rollback path is itself a behaviour, so it is itself tested.

    `margin = 0` must reproduce the old 100% boundary exactly. A rollback that
    has never been exercised is not a rollback.
    """
    monkeypatch.setattr("claude_fleet.control.WEEKLY_SWITCH_AHEAD_MARGIN", 0.0)
    _two_accounts(control, now, tight=0.99, roomy=0.3)
    generation = _place(control, "s1", "tight")

    route = control.decide_route("s1", "holder-s1", generation)
    assert route["account_id"] == "tight"
    assert route["reason"] != "switch_ahead"


def test_a_lone_nearly_spent_account_still_serves(control, now):
    """No alternative means no move. Parking here would be the worst outcome:
    a session stopped to avoid a cost that only exists when there is somewhere
    to go."""
    control.upsert_account("only")
    quota(control, "only", weekly=0.99, five_hour=0.0,
          weekly_reset=now + 2 * 24 * HOUR, five_reset=now + 4 * HOUR)
    generation = _place(control, "s1", "only")

    assert control.decide_route(
        "s1", "holder-s1", generation)["account_id"] == "only"
