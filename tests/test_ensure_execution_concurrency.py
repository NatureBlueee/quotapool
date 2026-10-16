"""`ensure_execution` must be idempotent under concurrency.

Observed live in coordinator.error.log:

    sqlite3.IntegrityError: UNIQUE constraint failed: executions.id
      coordinator.decide -> control.ensure_execution -> register_execution

The retry loop already handles the common races, but one path escapes it: when
the colliding id was invented by `register_execution` itself (no
`execution_hint`, so `base_execution_id` is None) and the session is not the one
that won the race, `get_execution_by_session` raises NotFound and the loop does
`if not base_execution_id: raise` — turning a recoverable collision into a 500.

That matters beyond tidiness: a 500 from the coordinator is not a routing
decision, so the data plane cannot distinguish it from "the control plane is
broken" and the request fails rather than being served by an account that was
available all along.

The rule these tests pin: a repeat ensure for the same session returns the
existing execution; a genuine collision between different sessions allocates a
distinct id; and only a conflict that is neither of those is allowed to raise.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid

import pytest

from claude_fleet.control import FleetControl, FleetError


def test_repeat_ensure_returns_the_same_execution(control):
    first = control.ensure_execution("s1", workload_class="interactive")
    second = control.ensure_execution("s1", workload_class="interactive")
    assert first["id"] == second["id"]


def test_desktop_workload_is_scheduled_as_interactive(control):
    execution = control.ensure_execution("desktop-session", workload_class="desktop")

    assert execution["workload_class"] == "interactive"


def test_repeat_ensure_with_a_hint_is_idempotent(control):
    first = control.ensure_execution(
        "s1", execution_hint="exec-abc123", workload_class="interactive"
    )
    second = control.ensure_execution(
        "s1", execution_hint="exec-abc123", workload_class="interactive"
    )
    assert first["id"] == second["id"] == "exec-abc123"


def test_two_sessions_sharing_a_hint_get_distinct_executions(control):
    """Agent View can issue two early requests with one launch hint but
    different provisional session ids."""
    a = control.ensure_execution("session-a", execution_hint="exec-shared")
    b = control.ensure_execution("session-b", execution_hint="exec-shared")
    assert a["id"] != b["id"]
    assert a["id"] == "exec-shared"
    assert b["id"].startswith("exec-shared:")


def test_a_collision_on_a_coordinator_invented_id_does_not_escape(control):
    """The path that produced the live 500s.

    With no hint, register_execution invents `exec-<12 hex>`. If that id is
    already taken by ANOTHER session, the old code hit
    `if not base_execution_id: raise` and surfaced IntegrityError.
    """
    taken = control.ensure_execution("other-session")
    collide_with = taken["id"]

    real_uuid4 = uuid.uuid4
    calls = {"n": 0}

    def once_colliding():
        # First invented id collides; any later attempt is random again.
        calls["n"] += 1
        if calls["n"] == 1:
            class _Fixed:
                hex = collide_with[len("exec-"):] + "0" * 20
            return _Fixed()
        return real_uuid4()

    uuid.uuid4 = once_colliding
    try:
        result = control.ensure_execution("new-session")
    finally:
        uuid.uuid4 = real_uuid4

    assert result["session_id"] == "new-session"
    assert result["id"] != collide_with, "must not steal the other session's id"


def test_concurrent_ensure_for_one_session_yields_one_execution(control):
    """Eight threads, one session id. Exactly one execution row, no exception.

    This is the shape of an Agent View / Team fan-out arriving at once.
    """
    results = []
    errors = []

    def worker():
        try:
            results.append(control.ensure_execution("shared-session"))
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent ensure raised: {errors!r}"
    assert len({r["id"] for r in results}) == 1, "one session must map to one execution"
    with control.db.connect() as conn:
        count = conn.execute(
            "SELECT count(*) FROM executions WHERE session_id=?", ("shared-session",)
        ).fetchone()[0]
    assert count == 1


def test_concurrent_ensure_sharing_one_hint_yields_distinct_executions(control, db):
    """Eight threads, one hint, eight different sessions.

    Each must end up with its own execution and none may fail. This is the race
    the retry loop exists for, exercised concurrently rather than by simulation.
    """
    results = []
    errors = []

    def worker(index):
        try:
            # A fresh FleetControl per thread: separate connections and separate
            # in-process locks, which is what the coordinator actually has under
            # ThreadingHTTPServer.
            local = FleetControl(db)
            results.append(
                local.ensure_execution(f"session-{index}", execution_hint="exec-fanout")
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent ensure raised: {errors!r}"
    assert len(results) == 8
    assert len({r["id"] for r in results}) == 8, "each session needs its own execution"


def test_an_unrelated_integrity_error_still_raises(control):
    """Do not swallow every IntegrityError.

    A foreign-key failure or any other constraint is a real bug and must
    surface, otherwise this retry loop becomes a place where corruption hides.
    """
    with pytest.raises((sqlite3.IntegrityError, FleetError)):
        control.register_execution(
            "s-bad", workload_class="interactive", parent_id="no-such-execution"
        )
