from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


# Bump whenever SCHEMA changes. ``initialize`` compares this against the
# database's ``PRAGMA user_version`` and only takes a write lock when they
# differ, so hook subprocesses stop competing with the coordinator for DDL.
SCHEMA_VERSION = 3

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'owned'
        CHECK(kind IN ('owned', 'shared')),
    state TEXT NOT NULL DEFAULT 'healthy'
        CHECK(state IN ('healthy', 'draining', 'exhausted', 'disabled')),
    priority INTEGER NOT NULL DEFAULT 0,
    five_hour_used REAL,
    five_hour_reset_at REAL,
    weekly_used REAL,
    weekly_reset_at REAL,
    five_hour_floor REAL NOT NULL DEFAULT 0,
    weekly_floor REAL NOT NULL DEFAULT 0,
    -- Quota below this fraction is reserved for work someone is waiting on.
    -- Deferrable classes (daemon/workflow/subagent-bulk) are refused once an
    -- account drops under it, so a batch job cannot drain the last of a window
    -- and leave an interactive session with nothing.
    interactive_reserve REAL NOT NULL DEFAULT 0,
    max_concurrency INTEGER NOT NULL DEFAULT 2147483647,
    allowed_workloads TEXT,
    quota_updated_at REAL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS executions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE,
    parent_id TEXT,
    workload_class TEXT NOT NULL,
    workload_id TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active', 'parked', 'completed', 'abandoned')),
    hard_pin TEXT,
    preference TEXT,
    next_account TEXT,
    route_chain TEXT,
    writer_generation INTEGER NOT NULL DEFAULT 0,
    writer_holder TEXT,
    writer_expires_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(parent_id) REFERENCES executions(id)
);

CREATE TABLE IF NOT EXISTS workload_policies (
    workload_id TEXT PRIMARY KEY,
    hard_pin TEXT,
    preference TEXT,
    route_chain TEXT,
    allowed_accounts TEXT,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS route_leases (
    session_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    route_generation INTEGER NOT NULL,
    holder TEXT NOT NULL,
    writer_generation INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(execution_id) REFERENCES executions(id),
    FOREIGN KEY(account_id) REFERENCES accounts(id)
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processes (
    id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL,
    pid INTEGER,
    parent_process_id TEXT,
    workload_class TEXT NOT NULL,
    config_dir TEXT,
    cwd TEXT,
    state TEXT NOT NULL DEFAULT 'active'
        CHECK(state IN ('active', 'stale', 'stopped')),
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS execution_nodes (
    id TEXT PRIMARY KEY,
    node_type TEXT NOT NULL,
    session_id TEXT,
    parent_id TEXT,
    process_id TEXT,
    agent_id TEXT,
    team_name TEXT,
    teammate_name TEXT,
    worktree TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(parent_id) REFERENCES execution_nodes(id),
    FOREIGN KEY(process_id) REFERENCES processes(id)
);

CREATE TABLE IF NOT EXISTS hook_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    event_name TEXT NOT NULL,
    session_id TEXT,
    execution_node_id TEXT,
    process_id TEXT,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_aliases (
    alias_session_id TEXT PRIMARY KEY,
    canonical_session_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    reason TEXT NOT NULL,
    CHECK(alias_session_id <> canonical_session_id),
    FOREIGN KEY(execution_id) REFERENCES executions(id)
);

CREATE TABLE IF NOT EXISTS execution_session_bindings (
    execution_hint TEXT NOT NULL,
    session_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    bound_at REAL NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY(execution_hint,session_id),
    UNIQUE(session_id),
    FOREIGN KEY(execution_id) REFERENCES executions(id)
);

CREATE INDEX IF NOT EXISTS idx_executions_parent ON executions(parent_id);
CREATE INDEX IF NOT EXISTS idx_route_account ON route_leases(account_id);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at);
-- decide_route reads the last six hours of account.runtime_synced inside the
-- write transaction to build its burn forecast. With only idx_events_at the
-- planner picks a full table SCAN, so the write lock is held for as long as the
-- events table is large: 14ms at 158k rows, and events had no retention. A
-- composite index keyed on the filter columns keeps that read O(window).
CREATE INDEX IF NOT EXISTS idx_events_kind_at ON events(kind, at);
CREATE INDEX IF NOT EXISTS idx_executions_status_created
    ON executions(status, created_at);
CREATE INDEX IF NOT EXISTS idx_processes_seen ON processes(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON execution_nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_nodes_session ON execution_nodes(session_id);
CREATE INDEX IF NOT EXISTS idx_hooks_session ON hook_events(session_id);
CREATE INDEX IF NOT EXISTS idx_aliases_canonical
    ON session_aliases(canonical_session_id);
CREATE INDEX IF NOT EXISTS idx_bindings_execution
    ON execution_session_bindings(execution_id);
"""


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def json_load(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def connect_readonly(path: str, timeout: float = 10.0) -> sqlite3.Connection:
    """Open the state database for reading, and actually succeed.

    `mode=ro` is the honest way to say "this tool will not write", and it is
    what every reporting script used. It also fails, intermittently, with
    `unable to open database file` — measured at 8% of runs for
    fleet-quota-sync (7 of 86) and six times for fleet-health, which means the
    health check had blind spots it never mentioned.

    The cause is WAL. A read-only connection cannot create the -shm file or run
    WAL recovery when the index needs it; a read-write connection can.
    Reproduced directly: with the -shm removed and its directory writable, a
    read-only open succeeds; with the directory not writable it fails with
    exactly that message.

    So: ask for read-only, and fall back rather than fail. The guarantee holds
    on the normal path, and the rare recovery case gets a connection that can
    do what it needs. Nothing here writes either way — the fallback changes
    what the connection is ALLOWED to do, not what it does.
    """
    try:
        conn = sqlite3.connect(
            "file:%s?mode=ro" % path, uri=True, timeout=timeout)
        conn.execute("SELECT 1").fetchone()
    except sqlite3.OperationalError:
        conn = sqlite3.connect(path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


class FleetDB:
    def __init__(self, path: str):
        self.path = str(Path(path).expanduser())
        self._writer_lock_path = f"{self.path}.writer.lock"
        # SQLite permits one writer. Let threads queue on a cheap Python lock
        # before opening ``BEGIN IMMEDIATE`` instead of creating hundreds of
        # SQLite busy handlers that each sleep for ten seconds and then fail.
        # RLock keeps future transaction helpers composable within one process.
        self._write_lock = threading.RLock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    # Columns added after a table's original CREATE. `CREATE TABLE IF NOT
    # EXISTS` will not add them to an existing table, so each needs an explicit
    # ALTER — and each must also be verifiable, see initialize().
    _ADDED_COLUMNS = {
        "executions": {"workload_id": "TEXT"},
        "accounts": {"interactive_reserve": "REAL NOT NULL DEFAULT 0"},
    }

    def initialize(self) -> None:
        # Every FleetDB construction used to run ``executescript(SCHEMA)``
        # unconditionally and outside the writer lock. Each hook subprocess and
        # each operator command therefore took a SQLite write lock for its DDL
        # while the coordinator was inside ``BEGIN IMMEDIATE``, which is one of
        # the paths that produced "database is locked". Reading user_version is
        # a lock-free read; the DDL now runs once per schema version and does so
        # behind the same advisory lock every other writer queues on.
        #
        # The version number alone is NOT sufficient as the skip condition. A
        # database was found stamped user_version=3 while `accounts` had no
        # `interactive_reserve` column: the version was written by a build whose
        # ALTER had not been added yet, and every later start then skipped the
        # migration on the strength of that stamp. The database would have
        # stayed broken forever, and the failure surfaced as an IndexError deep
        # in the request path rather than as a migration error.
        #
        # So verify the columns actually exist before trusting the stamp. This
        # costs one PRAGMA per construction and makes a wrong stamp
        # self-healing.
        if self._schema_version() == SCHEMA_VERSION and self._columns_present():
            return
        with self._writer_lock():
            conn = self.connect()
            try:
                # Re-check under the lock: a concurrent process may have
                # migrated between our read and our lock acquisition.
                if (
                    self._schema_version(conn) == SCHEMA_VERSION
                    and self._columns_present(conn)
                ):
                    return
                conn.executescript(SCHEMA)
                # `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table,
                # so every column added after v1 needs an explicit ALTER here or
                # it exists only in fresh databases. The release gate's
                # migration round trip (v0 -> initialize -> current) is what
                # catches a forgotten one.
                for table, columns in self._ADDED_COLUMNS.items():
                    self._add_missing_columns(conn, table, columns)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION:d}")
            finally:
                conn.close()

    def _columns_present(
        self, conn: Optional[sqlite3.Connection] = None
    ) -> bool:
        """Do all post-v1 columns actually exist?

        Guards against a database stamped with a version whose migration never
        ran — see the note in initialize().
        """
        own = conn is None
        if own:
            if not Path(self.path).exists():
                return False
            try:
                conn = self.connect()
            except sqlite3.Error:
                return False
        try:
            for table, columns in self._ADDED_COLUMNS.items():
                try:
                    existing = {
                        row["name"] for row in conn.execute(
                            f"PRAGMA table_info({table})"
                        ).fetchall()
                    }
                except sqlite3.Error:
                    return False
                if not existing:
                    continue  # table not created yet; SCHEMA will handle it
                if not set(columns) <= existing:
                    return False
            return True
        finally:
            if own:
                conn.close()

    @staticmethod
    def _add_missing_columns(
        conn: sqlite3.Connection,
        table: str,
        columns: Dict[str, str],
    ) -> None:
        existing = {
            row["name"] for row in conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
        }
        for name, declaration in columns.items():
            if name not in existing:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                )

    def _schema_version(self, conn: Optional[sqlite3.Connection] = None) -> int:
        if conn is not None:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        if not Path(self.path).exists():
            return -1
        try:
            probe = self.connect()
        except sqlite3.Error:
            return -1
        try:
            return int(probe.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.Error:
            return -1
        finally:
            probe.close()

    @contextlib.contextmanager
    def _writer_lock(self) -> Iterator[None]:
        # Hooks, the coordinator and operator commands are separate processes. A
        # Python RLock only queues threads inside one of them; under large Agent
        # View/Team fan-out the processes otherwise form independent SQLite
        # busy-handler convoys and route decisions time out after 10-15 seconds.
        # An advisory file lock gives every Fleet process one shared writer
        # queue before BEGIN IMMEDIATE — and before schema DDL.
        with self._write_lock:
            lock_fd = os.open(
                self._writer_lock_path,
                os.O_CREAT | os.O_RDWR,
                0o600,
            )
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._writer_lock():
            conn = self.connect()
            began = False
            try:
                conn.execute("BEGIN IMMEDIATE")
                began = True
                yield conn
                conn.execute("COMMIT")
            except Exception:
                # BEGIN itself can fail after SQLite's busy timeout. Rolling
                # back a transaction that never began masks the useful
                # "database is locked" error with "cannot rollback - no
                # transaction is active".
                if began and conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    @staticmethod
    def event(
        conn: sqlite3.Connection,
        kind: str,
        subject: Optional[str],
        payload: Dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO events(at,kind,subject,payload) VALUES(?,?,?,?)",
            (time.time(), kind, subject, json_dump(payload)),
        )

    def accounts(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY priority,id").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["allowed_workloads"] = json_load(item["allowed_workloads"], None)
            item["metadata"] = json_load(item["metadata"], {})
            result.append(item)
        return result

    def executions(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM executions ORDER BY created_at").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["route_chain"] = json_load(item["route_chain"], [])
            result.append(item)
        return result

    def leases(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM route_leases ORDER BY updated_at DESC"
            ).fetchall()]

    def workload_policies(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM workload_policies ORDER BY workload_id"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["route_chain"] = json_load(item["route_chain"], [])
            item["allowed_accounts"] = json_load(item["allowed_accounts"], None)
            result.append(item)
        return result

    def recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json_load(item["payload"], {})
            result.append(item)
        return result

    def processes(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM processes ORDER BY last_seen_at DESC"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json_load(item["metadata"], {})
            result.append(item)
        return result

    def execution_nodes(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM execution_nodes ORDER BY created_at"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json_load(item["metadata"], {})
            result.append(item)
        return result

    def recent_hook_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM hook_events ORDER BY seq DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json_load(item["payload"], {})
            result.append(item)
        return result
