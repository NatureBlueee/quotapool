"""A wait caused by the control plane must not read as a wait for quota.

Both arrive at the user the same way: the session sits there. The health check
called every held request "the fleet waiting, not failing", which is the right
reading for an empty quota window — waiting self-recovers, nobody has to
intervene — and precisely the wrong one when the control plane is too slow to
answer. Nothing separated them, so the second hid inside the first.

That is not hypothetical. A routing decision measured 19.0s against a data
plane that gives up at 15s; the data plane logged "coordinator unreachable —
holding the request" every few minutes into 23 MB of undated identical lines,
the health check reported a benign note, and the only signal that reached
anyone was the owner saying sessions felt slow.

The coordinator now writes a row only when a decision runs long, so the row is
itself the anomaly — no rate to normalise, no threshold to tune. Zero rows is
the healthy state, and it costs nothing to produce.
"""

from __future__ import annotations

import json
import time

from claude_fleet import coordinator as coordinator_module


HOUR = 3600


def _coordinator(db):
    return coordinator_module.Coordinator(db)


def _slow_events(db):
    with db.transaction() as conn:
        return [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT payload FROM events WHERE kind='route.slow' "
                "ORDER BY at DESC"
            ).fetchall()
        ]


def test_a_fast_decision_writes_nothing(db):
    """The common path must stay free, or the events table grows by a row per
    decision — and route decisions read that table inside the write
    transaction, so the diagnostic would slow down the thing it measures."""
    coordinator = _coordinator(db)
    coordinator._record_if_slow("s1", 0.05, {"reason": "sticky"})
    assert _slow_events(db) == []


def test_a_slow_decision_leaves_a_trace(db):
    coordinator = _coordinator(db)
    coordinator._record_if_slow(
        "s1",
        coordinator_module.SLOW_DECISION_SECONDS + 0.5,
        {"reason": "rolling_optimal",
         "optimizer": {"status": "capacity_limited_optimal"}},
    )
    events = _slow_events(db)
    assert len(events) == 1
    assert events[0]["seconds"] > coordinator_module.SLOW_DECISION_SECONDS
    assert events[0]["solved"] is True, (
        "whether the optimizer ran is the first branch in diagnosing this — a "
        "slow decision that did NOT solve points somewhere else entirely"
    )


def test_the_threshold_leaves_room_before_the_data_plane_gives_up(db):
    """A trace that only appears once requests are already being held is a
    post-mortem, not an alarm."""
    assert (
        coordinator_module.SLOW_DECISION_SECONDS
        < coordinator_module.DATAPLANE_TIMEOUT_SECONDS / 2
    ), "the warning must fire well before the deadline it protects"


def test_a_failure_to_record_never_breaks_routing(db, monkeypatch):
    """Diagnostics are the least important thing in this call path.

    If writing the trace can raise, then the fleet stops routing at exactly the
    moment it is under stress — the diagnostic would cause the outage it exists
    to describe.
    """
    coordinator = _coordinator(db)

    def exploding(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(coordinator.db, "transaction", exploding)
    coordinator._record_if_slow(
        "s1", coordinator_module.SLOW_DECISION_SECONDS + 1, {"reason": "x"})
