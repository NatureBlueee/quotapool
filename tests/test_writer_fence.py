"""Single-writer fencing.

Phase 2 of the repair plan touches these semantics, and a regression here is
the worst kind: `fleet-coordinator.js:198-215` deliberately turns
`stale_writer` into a non-retryable user-visible 409, so a generation that goes
backwards becomes a hard error in a live window.
"""

from __future__ import annotations

import time

import pytest

from claude_fleet.control import LeaseConflict, NotFound, StaleWriter


def test_generation_starts_at_one_and_is_stable_for_same_holder(control):
    control.register_execution("s1")
    first = control.acquire_writer("s1", "holder-a")
    assert first >= 1
    # Re-acquiring with a live lease must not burn a generation: the data plane
    # asks for the writer on every model request.
    assert control.acquire_writer("s1", "holder-a") == first


def test_second_holder_is_refused_while_lease_is_live(control):
    control.register_execution("s1")
    control.acquire_writer("s1", "holder-a")
    with pytest.raises(LeaseConflict):
        control.acquire_writer("s1", "holder-b")


def test_takeover_increments_generation_and_never_decrements(control):
    control.register_execution("s1")
    first = control.acquire_writer("s1", "holder-a")
    second = control.acquire_writer("s1", "holder-b", takeover=True)
    assert second > first

    third = control.acquire_writer("s1", "holder-c", takeover=True)
    assert third > second


def test_stale_generation_is_rejected_on_heartbeat(control):
    control.register_execution("s1")
    first = control.acquire_writer("s1", "holder-a")
    control.acquire_writer("s1", "holder-b", takeover=True)

    # The displaced window keeps running and tries to renew its old lease.
    with pytest.raises(StaleWriter):
        control.heartbeat_writer("s1", "holder-a", first)


def test_stale_generation_cannot_route(control):
    control.upsert_account("acct")
    control.register_execution("s1")
    first = control.acquire_writer("s1", "holder-a")
    control.acquire_writer("s1", "holder-b", takeover=True)

    with pytest.raises(StaleWriter):
        control.decide_route("s1", "holder-a", first)


def test_expired_lease_allows_a_new_holder_without_takeover(control):
    control.register_execution("s1")
    control.acquire_writer("s1", "holder-a", ttl_seconds=1)
    with control.db.transaction() as conn:
        conn.execute(
            "UPDATE executions SET writer_expires_at=? WHERE session_id=?",
            (time.time() - 1, "s1"),
        )
    # Not a conflict: the previous holder's lease is gone.
    assert control.acquire_writer("s1", "holder-b") >= 1


def test_unknown_session_raises_not_found(control):
    with pytest.raises(NotFound):
        control.acquire_writer("nope", "holder-a")
