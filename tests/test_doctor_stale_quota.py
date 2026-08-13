"""`account_quota_fresh` must fail for a broken sync and not for a decision.

The check exists to catch a frozen quota snapshot: `weekly_used` pinned at its
last value, so every account fails its floor and the fleet parks every request
forever while `fleet status` still looks plausible.

A shared account whose weekly window has rolled over produces the same reading
age for a completely different reason. `expire_stale_windows` skips shared
accounts on purpose, and the data plane stops reporting one once its own copy
clears — measured on the server, where the shared subscription read
`weekly_used=1.0` with a reset 103 hours in the past and the data plane
returning null for every quota field. Nothing automatic will ever refresh it.
Failing on it pins the check red until a human makes a decision about someone
else's subscription, and this module's own comment is that always-red trains
you to ignore red.

reconcile's SHARED_WINDOW_ROLLED_OVER_UNREAD owns that condition and states the
remedy. So it is still listed here — never silently dropped — but it does not
fail the gate.
"""

from __future__ import annotations

import json
import time

import pytest

from claude_fleet.doctor import doctor

HOUR = 3600


@pytest.fixture
def doctor_report(db, tmp_path):
    """Run the real `doctor` against the test database.

    Only the account checks are asserted on; everything else in the report
    depends on files and ports this process does not have, and those checks
    fail or skip harmlessly.
    """
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({
        "version": 1,
        "prefix": str(tmp_path),
        "db_path": db.path,
        "teamclaude_config": str(tmp_path / "teamclaude.json"),
    }), encoding="utf-8")

    def run():
        return doctor(runtime_path=str(runtime), db_path=db.path)

    return run


def _check(report, name):
    for entry in report["checks"]:
        if entry["name"] == name:
            return entry
    raise AssertionError(f"{name} not in report")


def _accounts(control, rows):
    for row in rows:
        control.upsert_account(row["id"], kind=row.get("kind", "owned"))
        with control.db.transaction() as conn:
            conn.execute(
                "UPDATE accounts SET quota_updated_at=?,weekly_used=?,"
                "weekly_reset_at=?,kind=? WHERE id=?",
                (row.get("quota_updated_at"), row.get("weekly_used"),
                 row.get("weekly_reset_at"), row.get("kind", "owned"),
                 row["id"]),
            )


def test_a_routable_account_that_stops_reporting_fails(control, doctor_report):
    """The condition the check was built for, stated precisely.

    Readings arrive on the response of a real request. An account with room is
    receiving requests, so silence from it means the sync is broken — the
    frozen snapshot that pins weekly_used and parks the fleet while `fleet
    status` still looks plausible.
    """
    now = time.time()
    _accounts(control, [
        {"id": "owned-stale", "weekly_used": 0.3,
         "quota_updated_at": now - 6 * HOUR, "weekly_reset_at": now + 24 * HOUR},
    ])
    check = _check(doctor_report(), "account_quota_fresh")
    assert check["ok"] is False
    assert any(d["account"] == "owned-stale" for d in check["detail"])


def test_a_spent_account_going_quiet_is_not_a_fault(control, doctor_report):
    """It cannot do anything else.

    A spent account is not routable, so it receives no requests, so it produces
    no readings. Failing on that means failing on every exhausted account for
    the whole of its window — measured on hello@ at 9.1 hours stale with 31
    hours still to run, which turned `fleet doctor` red for a fleet that was
    working exactly as designed.

    The blind spot is accepted knowingly: a sync frozen AT 1.0 looks identical
    from this database. What covers it is the other host — fleet-quota-sync
    carries a fresher reading across — and the park-ratio and avoidable-wait
    checks, which measure the consequence rather than the cause.
    """
    now = time.time()
    _accounts(control, [
        {"id": "spent", "weekly_used": 1.0,
         "quota_updated_at": now - 9 * HOUR, "weekly_reset_at": now + 31 * HOUR},
    ])
    check = _check(doctor_report(), "account_quota_fresh")
    assert check["ok"] is True
    listed = [d for d in check["detail"] if d["account"] == "spent"]
    assert listed and "spent" in listed[0].get("reason", ""), (
        "muting it entirely would hide a genuinely lost account; it has to "
        "stay visible with the reason it is quiet"
    )


def test_a_rolled_over_shared_window_is_reported_but_does_not_fail(
    control, doctor_report
):
    """The decision, not the fault."""
    now = time.time()
    _accounts(control, [
        {"id": "shared-rolled", "kind": "shared", "weekly_used": 1.0,
         "quota_updated_at": now - 6 * HOUR,
         "weekly_reset_at": now - 100 * HOUR},
    ])
    check = _check(doctor_report(), "account_quota_fresh")
    assert check["ok"] is True, (
        "nothing automatic can clear this, so failing on it pins the gate red "
        "until a human decides about another party's subscription"
    )
    listed = [d for d in check["detail"] if d["account"] == "shared-rolled"]
    assert listed, "muting it would be the other failure — it must stay visible"
    assert "SHARED_WINDOW_ROLLED_OVER_UNREAD" in listed[0].get("reason", ""), (
        "the entry has to point at the check that owns the remedy, or the "
        "reader sees a stale account with no explanation and no next step"
    )


def test_a_retired_row_is_not_asked_for_fresh_readings(control, doctor_report):
    """Switched off by hand is a decision, not a fault.

    The server holds three legacy rows retired after they 502'd every request
    against them. Only the data plane's `runtime_disabled` flag was checked,
    not Fleet's own `state='disabled'`, so those rows were still required to
    produce fresh quota readings — one of them 5.5 days stale by construction.
    Three permanent reds on rows nobody will ever route to again.
    """
    now = time.time()
    _accounts(control, [
        {"id": "retired", "weekly_used": 0.3,
         "quota_updated_at": now - 5 * 24 * HOUR,
         "weekly_reset_at": now + 24 * HOUR},
    ])
    with control.db.transaction() as conn:
        conn.execute("UPDATE accounts SET state='disabled' WHERE id='retired'")

    check = _check(doctor_report(), "account_quota_fresh")
    assert check["ok"] is True
    assert not [d for d in check["detail"] if d["account"] == "retired"]


def test_a_shared_account_inside_its_window_still_fails(control, doctor_report):
    """The exemption is the rolled-over window, not the word "shared".

    A shared account whose window is still open and whose reading has frozen is
    an ordinary broken sync, and muting it by kind would hide exactly the
    failure this check exists for on one fifth of the fleet.
    """
    now = time.time()
    _accounts(control, [
        {"id": "shared-live", "kind": "shared", "weekly_used": 0.4,
         "quota_updated_at": now - 6 * HOUR,
         "weekly_reset_at": now + 48 * HOUR},
    ])
    check = _check(doctor_report(), "account_quota_fresh")
    assert check["ok"] is False


def test_a_leak_is_flagged_and_ordinary_traffic_is_not(control, doctor_report):
    """The check counts what traffic cannot explain.

    Counting every active row meant the threshold chased how busy the fleet
    was: 2,000, then 4,500 from a measured peak of 2,160, then failing again at
    7,591 a day later — each time with the load-count query at 3ms and zero
    slow decisions in six hours. A check that must be re-tuned whenever work
    grows is measuring the wrong thing.

    A row whose writer lease has EXPIRED and is still marked active is
    different: normal operation never clears one, only the sweep does. After a
    sweep, 7,591 became 5,053 and every survivor had a live writer — the 2,544
    with dead ones were the whole of the excess.
    """
    import time as _time
    from claude_fleet.doctor import STALE_ACTIVE_WARN

    now = _time.time()
    with control.db.transaction() as conn:
        for i in range(3):
            conn.execute(
                "INSERT INTO executions(id,session_id,status,workload_class,"
                "writer_expires_at,created_at,updated_at) "
                "VALUES(?,?,'active','interactive',?,?,?)",
                ("live-%d" % i, "s-live-%d" % i, now + 3600, now, now))

    check = _check(doctor_report(), "no_leaked_executions")
    assert check["ok"] is True, (
        "busy is not leaking; this failed on healthy fleets three times"
    )
    assert check["detail"]["stale_active"] == 0

    with control.db.transaction() as conn:
        for i in range(STALE_ACTIVE_WARN + 1):
            conn.execute(
                "INSERT INTO executions(id,session_id,status,workload_class,"
                "writer_expires_at,created_at,updated_at) "
                "VALUES(?,?,'active','interactive',?,?,?)",
                ("dead-%d" % i, "s-dead-%d" % i, now - 3600, now, now))

    check = _check(doctor_report(), "no_leaked_executions")
    assert check["ok"] is False, (
        "rows the sweep can never reclaim grew without bound and nothing said so"
    )

