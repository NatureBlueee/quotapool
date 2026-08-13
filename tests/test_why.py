"""`fleet why` must answer the question that took three passes to answer by hand:
is this fleet correctly waiting, or is it broken?
"""

from __future__ import annotations

import time

from claude_fleet.why import explain
from conftest import quota


HOUR = 3600


def test_unknown_session_says_so_rather_than_failing(control, db):
    result = explain(db, "no-such-session")
    assert result["found"] is False
    assert "hint" in result


def test_reports_the_chosen_account_by_name(control, db):
    now = time.time()
    control.upsert_account("acct-uuid-1")
    quota(control, "acct-uuid-1", weekly=0.1, five_hour=0.0,
          weekly_reset=now + 3 * HOUR)
    control.sync_account_runtime(
        "acct-uuid-1", name="real@example.com", disabled=False,
        weekly_used=0.1, weekly_reset_at=now + 3 * HOUR,
        five_hour_used=0.0, five_hour_reset_at=None, metadata={},
    )
    control.register_execution("s1", workload_class="interactive")
    generation = control.acquire_writer("s1", "holder-1")
    control.decide_route("s1", "holder-1", generation)

    result = explain(db, "s1")
    assert result["found"] is True
    # Names, not UUIDs — the raw table dump was unreadable precisely because
    # every account was a bare uuid.
    assert result["current_account"] == "real@example.com"
    assert result["recent_decisions"][0]["chose"] == "real@example.com"


def test_park_explains_which_accounts_were_excluded(control, db):
    control.upsert_account("a")
    control.register_execution("s1", workload_class="interactive")
    generation = control.acquire_writer("s1", "holder-1")
    try:
        control.decide_route("s1", "holder-1", generation, exclude=["a"])
    except Exception:
        pass

    result = explain(db, "s1")
    parked = [
        item for item in result["recent_decisions"]
        if item["event"] == "execution.parked"
    ]
    assert parked, "a park must leave an explanation"
    assert parked[0]["reason"] == "no_capacity"
    assert "a" in parked[0]["excluded"]


def test_remaining_is_reported_after_the_floor(control, db):
    """Raw utilization is not what decides eligibility — the floor-adjusted
    remainder is. Reporting the raw number is what let a shared account look
    fine while it was actually past its reserve."""
    now = time.time()
    control.upsert_account("shared", kind="shared", weekly_floor=0.4)
    quota(control, "shared", weekly=0.8, five_hour=0.0, weekly_reset=now + HOUR)
    control.register_execution("s1", workload_class="interactive")
    control.acquire_writer("s1", "holder-1")

    result = explain(db, "s1")
    shared = next(a for a in result["accounts"] if a["id"] == "shared")
    # 1 - 0.8 - 0.4 -> negative: already past the reserve, and visibly so.
    assert shared["weekly_remaining_after_floor"] < 0
    assert shared["kind"] == "shared"


def test_manual_controls_are_surfaced(control, db):
    control.upsert_account("a")
    control.upsert_account("b")
    control.register_execution("s1", workload_class="interactive")
    control.acquire_writer("s1", "holder-1")
    control.set_route_policy("s1", "pin", "a")
    control.set_route_policy("s1", "chain", ["a", "b"])

    result = explain(db, "s1")
    assert result["manual_controls"]["pin"] == "a"
    assert result["manual_controls"]["chain"] == ["a", "b"]


def test_workload_policy_is_surfaced(control, db):
    control.upsert_account("a")
    control.set_workload_policy("daemon:x", allowed_accounts=["a"])
    control.register_execution(
        "s1", workload_class="daemon", workload_id="daemon:x"
    )
    control.acquire_writer("s1", "holder-1")

    result = explain(db, "s1")
    assert result["workload_policy"]["allowed_accounts"] == ["a"]


def test_resolves_through_a_session_alias(control, db):
    """A provisional id must still find its canonical execution, otherwise the
    command is useless for exactly the sessions that needed rebinding."""
    control.upsert_account("a")
    control.register_execution("real-session", workload_class="interactive")
    control.acquire_writer("real-session", "holder-1")
    with db.transaction() as conn:
        execution = conn.execute(
            "SELECT id FROM executions WHERE session_id=?", ("real-session",)
        ).fetchone()
        conn.execute(
            "INSERT INTO session_aliases("
            "alias_session_id,canonical_session_id,execution_id,created_at,reason"
            ") VALUES(?,?,?,?,?)",
            ("anonymous:host:abc", "real-session", execution["id"],
             time.time(), "test"),
        )

    result = explain(db, "anonymous:host:abc")
    assert result["found"] is True
    assert result["session_id"] == "real-session"


def test_shared_account_past_floor_is_attributed(control, db):
    """A shared account past its reserve is ambiguous between "Fleet breached
    it" and "the other holder used it" — opposite conclusions from one number.

    Measured on the live database: the shared account sat 40% past its floor
    with zero route leases ever. The floor had held perfectly; the raw view made
    that look like a violation.
    """
    now = time.time()
    control.upsert_account("shared", kind="shared", weekly_floor=0.4)
    quota(control, "shared", weekly=1.0, five_hour=0.0, weekly_reset=now + HOUR)
    control.register_execution("s1", workload_class="interactive")
    control.acquire_writer("s1", "holder-1")

    result = explain(db, "s1")
    shared = next(a for a in result["accounts"] if a["id"] == "shared")
    assert shared["routed_by_fleet_ever"] == 0
    assert "not a breach" in shared["reserve_note"]


def test_shared_account_routed_by_fleet_is_flagged_differently(control, db):
    now = time.time()
    control.upsert_account("shared", kind="shared", weekly_floor=0.1)
    quota(control, "shared", weekly=0.2, five_hour=0.0, weekly_reset=now + HOUR)
    control.register_execution("s1", workload_class="interactive")
    generation = control.acquire_writer("s1", "holder-1")
    control.decide_route("s1", "holder-1", generation)

    # Now push it past the floor.
    quota(control, "shared", weekly=0.95, five_hour=0.0, weekly_reset=now + HOUR)
    result = explain(db, "s1")
    shared = next(a for a in result["accounts"] if a["id"] == "shared")
    assert shared["routed_by_fleet_ever"] >= 1
    assert "not a breach" not in shared["reserve_note"]
