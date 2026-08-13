"""Why the fleet is shaped the way it is has to survive the sweep.

Four accounts were disabled and stayed that way for days while everything ran on
one. The question "who turned them off, and when" had no answer anywhere: the
`account.disabled` rows had aged out at seven days, and the only account events
left in the ledger were `account.runtime_synced`, which says nothing about
intent.

The sweep is about volume. Routing decisions and writer heartbeats run to
hundreds of thousands of rows and describe a moment that has passed. State
changes are a different kind of row — there are a few dozen of them, they are
the record of a decision somebody or something made, and keeping them forever
costs nothing next to not being able to answer that question.
"""

from __future__ import annotations

import time

import pytest

from claude_fleet.maintenance import KEEP_KINDS, sweep

OLD = 40 * 24 * 3600


def _event(db, kind, at):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO events(at,kind,subject,payload) VALUES(?,?,?,?)",
            (at, kind, "acct", "{}"))


def _kinds(db):
    with db.transaction() as conn:
        return sorted(r[0] for r in conn.execute("SELECT kind FROM events"))


def test_a_state_change_outlives_the_sweep(db, now):
    """The row that answers "who turned this off"."""
    _event(db, "account.disabled", now - OLD)
    _event(db, "route.changed", now - OLD)

    sweep(db, now=now)

    assert _kinds(db) == ["account.disabled"]


@pytest.mark.parametrize("kind", KEEP_KINDS)
def test_every_state_change_kind_is_kept(db, now, kind):
    _event(db, kind, now - OLD)
    sweep(db, now=now)
    assert _kinds(db) == [kind]


def test_the_high_volume_kinds_are_still_reclaimed(db, now):
    """This is what the sweep is for. 108k events in a week on one host, and a
    routing decision reads a six-hour window inside its write transaction — the
    table's size is a direct cost on every request."""
    for kind in ("route.changed", "writer.acquired", "execution.registered",
                 "quota.reported", "account.runtime_synced"):
        _event(db, kind, now - OLD)

    sweep(db, now=now)

    assert _kinds(db) == []


def test_recent_rows_of_every_kind_survive(db, now):
    """Retention is about age, and the exemption must not become a licence to
    ignore it in the other direction."""
    _event(db, "route.changed", now - 60)
    _event(db, "account.disabled", now - 60)
    sweep(db, now=now)
    assert len(_kinds(db)) == 2


def test_the_dry_run_count_matches_what_would_go(db, now):
    """A dry run that over-counts is a dry run nobody trusts."""
    _event(db, "account.disabled", now - OLD)
    for _ in range(3):
        _event(db, "route.changed", now - OLD)

    preview = sweep(db, now=now, dry_run=True)
    assert preview["counts"]["events_older_than_retention"] == 3

    actual = sweep(db, now=now)
    assert actual["events_removed"] == 3
