"""There was no way to put the state database back.

`fleet-rollback` deploys code and says so plainly in its own docstring: "a
schema written by a newer version is not something git can undo." That was an
honest limitation until the next change on the list rewrites the accounts table.

The evidence that this was always needed is in the state directory — two
snapshots, taken by hand, named by hand, at the two moments someone was about to
do something irreversible:

    fleet.sqlite3.before-orphan-cleanup             61 MB, 07-30
    fleet.sqlite3.pre-cap-migration-20260729-1430   25 MB, 07-29

What was missing was never the idea. It was a snapshot nobody has to remember to
take, and a way back that does not depend on guessing which hand-named file was
the good one.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from claude_fleet.backup import create, list_backups, restore
from claude_fleet.db import FleetDB, connect_readonly


@pytest.fixture()
def database(tmp_path):
    """A real Fleet database, in WAL mode, with a row worth losing."""
    path = tmp_path / "fleet.sqlite3"
    db = FleetDB(str(path))
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO accounts(id,kind,state,priority,five_hour_floor,"
            "weekly_floor,max_concurrency,created_at,updated_at) "
            "VALUES('acct','owned','healthy',0,0,0,1,0,0)")
    return str(path)


def _accounts(path):
    conn = connect_readonly(path)
    try:
        return conn.execute("SELECT count(*) FROM accounts").fetchone()[0]
    finally:
        conn.close()


def test_a_database_whose_services_are_stopped_can_still_be_backed_up(database):
    """The bug this file caught before it shipped.

    `create` opened with `mode=ro`, and a read-only connection cannot create the
    -shm file a WAL database needs. It worked against the live database only
    because the coordinator was holding that file open. On any database whose
    services are not running it failed with "unable to open database file" —
    which is exactly the state right after a restore, when a second snapshot
    before touching anything is the whole point.
    """
    for suffix in ("-wal", "-shm"):
        stale = Path(database + suffix)
        if stale.exists():
            stale.unlink()

    result = create(database, reason="services_down")
    assert Path(result["path"]).exists()


def test_the_snapshot_is_a_complete_database(database):
    """A backup you cannot restore is worse than no backup: it stops you looking
    for one that works."""
    backup = create(database, reason="integrity")["path"]

    conn = sqlite3.connect(backup)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT count(*) FROM accounts").fetchone()[0] == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == \
            sqlite3.connect(database).execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def test_a_round_trip_puts_the_rows_back(database):
    """Backup, destroy, restore. The whole contract in one test."""
    backup = create(database, reason="roundtrip")["path"]
    assert _accounts(database) == 1

    conn = sqlite3.connect(database)
    conn.execute("DELETE FROM accounts")
    conn.commit()
    conn.close()
    assert _accounts(database) == 0

    restore(database, backup)
    assert _accounts(database) == 1


def test_restoring_is_itself_reversible(database):
    """Restoring the wrong backup at 3am must not be a one-way door."""
    good = create(database, reason="good")["path"]
    conn = sqlite3.connect(database)
    conn.execute("INSERT INTO accounts(id,kind,state,priority,five_hour_floor,"
                 "weekly_floor,max_concurrency,created_at,updated_at) "
                 "VALUES('later','owned','healthy',0,0,0,1,0,0)")
    conn.commit()
    conn.close()
    assert _accounts(database) == 2

    result = restore(database, good)
    assert _accounts(database) == 1

    # The two-row state we just discarded is still recoverable.
    restore(database, result["previous_state_saved_to"])
    assert _accounts(database) == 2


def test_restore_clears_the_previous_databases_wal(database):
    """The -wal describes the file that was just replaced. Left in place SQLite
    applies those frames on top of the restored database, silently putting back
    exactly the writes being undone."""
    backup = create(database, reason="wal")["path"]
    Path(database + "-wal").write_bytes(b"stale frames")
    Path(database + "-shm").write_bytes(b"stale index")

    result = restore(database, backup)

    assert not Path(database + "-wal").exists()
    assert not Path(database + "-shm").exists()
    assert len(result["removed_stale"]) == 2


def test_restore_refuses_while_the_coordinator_holds_the_database(database,
                                                                   monkeypatch):
    """Overwriting the file under a live writer leaves that process holding a
    handle to a path that no longer means what it did; its next commit either
    resurrects the old state or corrupts the new."""
    import claude_fleet.backup as module

    backup = create(database, reason="busy")["path"]
    monkeypatch.setattr(module, "_coordinator_listening", lambda port: True)

    with pytest.raises(RuntimeError) as caught:
        restore(database, backup, coordinator_port=17891)
    assert "协调器" in str(caught.value)

    # ...and --force is the deliberate way past it.
    restore(database, backup, coordinator_port=17891, force=True)


def test_retention_keeps_the_newest(database):
    """Ten snapshots of a 59 MB database is 600 MB. Unbounded is a disk-full
    outage waiting for a busy week."""
    made = [create(database, reason="r%d" % i, keep=3)["path"] for i in range(5)]

    kept = {item["path"] for item in list_backups(database)}
    assert len(kept) == 3
    assert set(made[-3:]) == kept, "retention must drop the oldest, not any three"


def test_a_failed_snapshot_leaves_nothing_that_looks_like_one(database,
                                                               monkeypatch):
    """A truncated file in the backups directory reads as a backup to
    `list_backups` and to whoever is scanning that listing under pressure."""
    import claude_fleet.backup as module

    class _Failing:
        def execute(self, sql, params=()):
            # Write enough to look like a file, then die — the shape a full disk
            # or a killed process actually leaves behind.
            Path(params[0]).write_bytes(b"SQLite format 3\x00truncated")
            raise sqlite3.OperationalError("database or disk is full")

        def close(self):
            pass

    monkeypatch.setattr(module, "connect_readonly", lambda *a, **k: _Failing())
    with pytest.raises(sqlite3.OperationalError):
        create(database, reason="doomed")

    monkeypatch.undo()
    assert list_backups(database) == [], "a half-written file must not be listed"
    assert not list(Path(database).parent.glob("backups/*.partial"))


def test_restore_with_no_backups_says_so(database):
    with pytest.raises(FileNotFoundError):
        restore(database)


def test_snapshots_are_not_world_readable(database):
    """They contain the same account policy and history as the database."""
    backup = create(database, reason="perms")["path"]
    assert oct(os.stat(backup).st_mode & 0o777) == "0o600"
