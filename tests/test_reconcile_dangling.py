"""Manual controls that point at a vanished account must not stay invisible.

Two failures this catches, both of which happened.

Note how they arise: `set_workload_policy` DOES validate that the account
exists, so a policy is never written dangling. It becomes dangling when the
account id changes underneath it — which is exactly what the UUID migration did.
The tests below reproduce that sequence rather than writing an invalid policy
directly, because writing one directly is already impossible.

A `preference` is a SOFT hint: `decide_route` filters candidates to it and falls
through silently when the filter is empty. That is right for an account that is
merely spent, and wrong for one whose id no longer exists. After the UUID
migration the server ran eight daemon policies pointing at `max-c` for two days,
every one inert, with no signal anywhere.

And an execution keyed on a display name — what `--resume 空港` used to create —
held a 24-hour writer lease under a key nothing could ever match, so every later
resume of that name hit LeaseConflict.
"""

from __future__ import annotations

import time

from claude_fleet.reconcile import reconcile_report
from conftest import quota


def _types(report):
    return [a["type"] for a in report["anomalies"]]


def test_a_clean_fleet_reports_neither(control, db):
    control.upsert_account("acct")
    control.set_workload_policy("daemon:x", preference="acct")
    control.register_execution(
        "0a8623a1-9350-4983-abc2-de428397e069", workload_class="interactive"
    )
    report = reconcile_report(db)
    assert "DANGLING_WORKLOAD_POLICY" not in _types(report)
    assert "NON_SESSION_EXECUTION" not in _types(report)


def _rename_account(db, old, new):
    """Reproduce the UUID migration: the same subscription re-registers under a
    new id, leaving policies pointing at the old one."""
    with db.transaction() as conn:
        conn.execute("UPDATE accounts SET id=? WHERE id=?", (new, old))


def test_a_preference_to_a_vanished_account_is_reported(control, db):
    """The server's exact state: eight policies left on 'max-c' after that
    account was re-registered under its UUID."""
    control.upsert_account("max-c")
    control.set_workload_policy("hsm:w1", preference="max-c")
    _rename_account(db, "max-c", "00000000-0000-4000-8000-000000000005")

    found = [
        a for a in reconcile_report(db)["anomalies"]
        if a["type"] == "DANGLING_WORKLOAD_POLICY"
    ]
    assert len(found) == 1
    assert found[0]["field"] == "preference"
    assert found[0]["target"] == "max-c"
    # The effect matters more than the fact: a silent no-op reads very
    # differently from a parked workload.
    assert "silently ignored" in found[0]["effect"]


def test_a_dangling_hard_pin_says_it_parks_requests(control, db):
    control.upsert_account("gone")
    control.set_workload_policy("daemon:y", hard_pin="gone")
    _rename_account(db, "gone", "real")

    found = next(
        a for a in reconcile_report(db)["anomalies"]
        if a["type"] == "DANGLING_WORKLOAD_POLICY"
    )
    assert found["field"] == "hard_pin"
    assert "parks" in found["effect"]


def test_a_dangling_allow_list_entry_is_reported(control, db):
    """An allow-list is a HARD constraint, so a stale entry shrinks the
    permitted set rather than being ignored."""
    control.upsert_account("real")
    control.upsert_account("gone")
    control.set_workload_policy("daemon:z", allowed_accounts=["real", "gone"])
    _rename_account(db, "gone", "renamed")

    found = [
        a for a in reconcile_report(db)["anomalies"]
        if a["type"] == "DANGLING_WORKLOAD_POLICY" and a["target"] == "gone"
    ]
    assert len(found) == 1
    assert found[0]["field"] == "allowed_accounts"


def test_a_dangling_chain_entry_is_reported(control, db):
    control.upsert_account("real")
    control.upsert_account("gone")
    control.set_workload_policy("daemon:c", route_chain=["gone", "real"])
    _rename_account(db, "gone", "renamed")

    found = [
        a for a in reconcile_report(db)["anomalies"]
        if a["type"] == "DANGLING_WORKLOAD_POLICY"
    ]
    assert len(found) == 1
    assert found[0]["field"] == "route_chain"


def test_an_execution_keyed_on_a_display_name_is_reported(control, db):
    """What `--resume 空港` used to create."""
    control.upsert_account("acct")
    control.register_execution("空港", workload_class="interactive")
    control.acquire_writer("空港", "holder-A")

    found = [
        a for a in reconcile_report(db)["anomalies"]
        if a["type"] == "NON_SESSION_EXECUTION"
    ]
    assert len(found) == 1
    assert found[0]["session_id"] == "空港"
    # A live lease is the part that breaks the next resume.
    assert found[0]["holds_writer"] is True


def test_anonymous_transport_ids_are_not_flagged(control, db):
    """Those are a known, separate shape with their own handling — flagging
    them here would bury the real finding in thousands of rows."""
    control.upsert_account("acct")
    control.register_execution("anonymous:host:abc123", workload_class="interactive")
    assert "NON_SESSION_EXECUTION" not in _types(reconcile_report(db))


def test_an_account_with_no_quota_reading_is_reported(control, db):
    """It cannot be routed to, so it cannot measure itself.

    This is how 67% of a weekly window stayed invisible on the server while
    every request was parking for no_capacity.
    """
    control.upsert_account("blind")

    found = next(
        a for a in reconcile_report(db)["anomalies"]
        if a["type"] == "UNMEASURED_ACCOUNT"
    )
    assert found["account_id"] == "blind"
    assert set(found["missing"]) == {"weekly", "five_hour"}
    assert "cannot measure itself" in found["effect"]


def test_an_unread_five_hour_bucket_alone_is_enough(control, db):
    """The easy case to miss.

    A healthy weekly reading with most of the window left, and the account is
    still invisible, because control.py:1685 drops it when EITHER bucket is
    None.
    """
    control.upsert_account("halfblind")
    control.report_quota("halfblind", weekly_used=0.1)

    found = next(
        a for a in reconcile_report(db)["anomalies"]
        if a["type"] == "UNMEASURED_ACCOUNT"
    )
    assert found["missing"] == ["five_hour"]


def test_a_spent_account_is_not_reported_as_unmeasured(control, db):
    """Gone capacity is not missing capacity.

    Probing a measured, exhausted weekly window buys no decision until it
    resets, so reporting it would be noise.
    """
    control.upsert_account("spent")
    control.report_quota("spent", weekly_used=1.0)

    types = _types(reconcile_report(db))
    assert "UNMEASURED_ACCOUNT" not in types


def test_an_enabled_account_the_data_plane_cannot_serve_is_reported(control, db, monkeypatch):
    """The shape that has now caused two outages, both fixed by hand.

    `executable` stops the coordinator CHOOSING such a row per request. This
    reports that the row EXISTS, because a hazard defused only by another
    component staying correct is still a hazard — and both times, what defused
    it was luck: the legacy rows happened to read as spent, and the local one
    happened to have no quota reading at all.
    """
    import claude_fleet.reconcile as reconcile

    control.upsert_account("ghost")
    control.upsert_account("real")
    monkeypatch.setattr(reconcile, "_dataplane_accounts", lambda port=13456: {"real"})

    found = [
        a for a in reconcile_report(db)["anomalies"]
        if a["type"] == "UNSERVABLE_ACCOUNT_ENABLED"
    ]
    assert [a["account_id"] for a in found] == ["ghost"]


def test_an_unreachable_data_plane_flags_nothing(control, db, monkeypatch):
    """No answer must not be read as "no account is usable".

    Getting this backwards would flag every account the moment the data plane
    restarts, which is both wrong and the fastest way to get a check ignored.
    """
    import claude_fleet.reconcile as reconcile

    control.upsert_account("a")
    control.upsert_account("b")
    monkeypatch.setattr(reconcile, "_dataplane_accounts", lambda port=13456: None)

    assert "UNSERVABLE_ACCOUNT_ENABLED" not in _types(reconcile_report(db))


def test_a_manual_control_aimed_at_a_disabled_account_is_reported(control, db):
    """The half `DANGLING_WORKLOAD_POLICY` does not cover.

    That check asks whether the target EXISTS. This one bit precisely because
    the target existed: disabling the legacy rows to stop a 502 storm left 16
    sessions on the server queued onto `max-c`, parking on every request for
    ever, with nothing reporting it. A loud failure had been traded for a
    silent one.
    """
    control.upsert_account("retired")
    control.upsert_account("live")
    control.register_execution("s1", workload_class="interactive")
    control.set_route_policy("s1", "next", "retired")
    with db.transaction() as conn:
        conn.execute("UPDATE accounts SET state='disabled' WHERE id='retired'")

    found = [
        a for a in reconcile_report(db)["anomalies"]
        if a["type"] == "UNSATISFIABLE_MANUAL_CONTROL"
    ]
    assert len(found) == 1
    assert found[0]["field"] == "next_account"
    assert found[0]["target"] == "retired"


def test_a_merely_spent_target_is_not_reported(control, db, now):
    """Spent recovers by itself at the reset, so parking until then is intended.

    Flagging it would make the check fire constantly during normal operation,
    which is the fastest way to get a check ignored.
    """
    control.upsert_account("spent")
    quota(control, "spent", weekly=1.0, five_hour=0.0,
          weekly_reset=now + 3600, five_reset=now + 3600)
    control.register_execution("s1", workload_class="interactive")
    control.set_route_policy("s1", "next", "spent")

    assert "UNSATISFIABLE_MANUAL_CONTROL" not in _types(reconcile_report(db))
