"""State retention for the Fleet database.

Nothing in Fleet reclaimed state. Measured on 2026-07-30 after 1.5 days of a
single laptop: 158k events, 7,785 executions of which 6,457 were still `active`
and only 12 `completed`, 6,802 route leases, 45MB of SQLite. The growth is not
merely disk: `decide_route` reads a six-hour window of `account.runtime_synced`
events *inside* its write transaction, so the events table's size is directly
proportional to how long every route decision holds the single writer lock. On
the Harness authority server — months of uptime, no one watching the logs — that
is the failure mode that arrives first.

Deletion order matters. `PRAGMA foreign_keys=ON` is set on every connection and
`executions` is referenced by `route_leases`, `session_aliases`,
`execution_session_bindings` and by itself through `parent_id`. Children are
removed before parents, and an execution that still has a live child is kept
until the child ages out.

Batching matters too: this runs against the same database the coordinator is
serving from, so each batch is a short transaction rather than one long
exclusive hold.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .db import FleetDB


# Six hours is what decide_route's forecast actually reads; a week keeps
# `fleet observe` and the reconciler useful without unbounded growth.
EVENT_RETENTION_SECONDS = 7 * 24 * 3600
HOOK_EVENT_RETENTION_SECONDS = 7 * 24 * 3600
# Terminal executions are history. Keep enough to explain recent lineage.
TERMINAL_EXECUTION_RETENTION_SECONDS = 3 * 24 * 3600
# An `active` execution whose writer lease expired long ago is not a running
# session — it is a phantom left by a request that never reported completion.
# The grace period is deliberately far longer than WRITER_TTL_SECONDS so a
# genuinely idle interactive session is never abandoned out from under its user.
STALE_ACTIVE_GRACE_SECONDS = 24 * 3600
PROCESS_RETENTION_SECONDS = 7 * 24 * 3600
BATCH = 500


def _delete_batched(conn, table: str, where: str, params: tuple) -> int:
    removed = 0
    while True:
        cursor = conn.execute(
            f"DELETE FROM {table} WHERE rowid IN ("
            f"SELECT rowid FROM {table} WHERE {where} LIMIT {BATCH:d})",
            params,
        )
        if not cursor.rowcount:
            return removed
        removed += cursor.rowcount
        if cursor.rowcount < BATCH:
            return removed


# Events the sweep must never reclaim.
#
# The sweep is about volume: routing decisions and writer heartbeats run to
# hundreds of thousands of rows and describe a moment that has passed. State
# changes are different — there are a handful of them, and they are the only
# record of WHY the fleet is shaped the way it is.
#
# Measured: four accounts were disabled and stayed that way for days, and the
# question "who turned them off, and when" had no answer anywhere, because the
# `account.disabled` rows had aged out at seven days while the fleet ran on one
# account. Keeping a few dozen rows forever costs nothing next to that.
KEEP_KINDS = (
    "account.disabled",
    "account.healthy",
    "account.draining",
    "account.exhausted",
    "account.identity_collision",
    "account.window_rolled_over",
    "account.discovered",
)
KEEP_KINDS_SQL = ",".join("?" * len(KEEP_KINDS))


def sweep(
    db: FleetDB,
    *,
    now: Optional[float] = None,
    event_retention: float = EVENT_RETENTION_SECONDS,
    hook_retention: float = HOOK_EVENT_RETENTION_SECONDS,
    terminal_retention: float = TERMINAL_EXECUTION_RETENTION_SECONDS,
    stale_active_grace: float = STALE_ACTIVE_GRACE_SECONDS,
    process_retention: float = PROCESS_RETENTION_SECONDS,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Reclaim aged state. Safe to run while the coordinator is serving."""

    moment = time.time() if now is None else now
    report: Dict[str, Any] = {"at": moment, "dry_run": bool(dry_run)}

    if dry_run:
        conn = db.connect()
        try:
            report["counts"] = {
                "events_older_than_retention": conn.execute(
                    "SELECT count(*) FROM events WHERE at<? AND kind NOT IN (%s)"
                    % KEEP_KINDS_SQL,
                    (moment - event_retention,) + KEEP_KINDS,
                ).fetchone()[0],
                "hook_events_older_than_retention": conn.execute(
                    "SELECT count(*) FROM hook_events WHERE at<?",
                    (moment - hook_retention,),
                ).fetchone()[0],
                "terminal_executions_older_than_retention": conn.execute(
                    "SELECT count(*) FROM executions "
                    "WHERE status IN ('completed','abandoned','parked') "
                    "AND updated_at<?",
                    (moment - terminal_retention,),
                ).fetchone()[0],
                "stale_active_executions": conn.execute(
                    "SELECT count(*) FROM executions WHERE status='active' "
                    "AND updated_at<? "
                    "AND (writer_expires_at IS NULL OR writer_expires_at<?)",
                    (moment - stale_active_grace, moment),
                ).fetchone()[0],
                "processes_older_than_retention": conn.execute(
                    "SELECT count(*) FROM processes WHERE last_seen_at<?",
                    (moment - process_retention,),
                ).fetchone()[0],
            }
            return report
        finally:
            conn.close()

    # Phase 1 — mark phantom sessions. A separate short transaction so the
    # deletion phase sees them as terminal without holding one long lock.
    with db.transaction() as conn:
        marked = conn.execute(
            "UPDATE executions SET status='abandoned',updated_at=? "
            "WHERE status='active' AND updated_at<? "
            "AND (writer_expires_at IS NULL OR writer_expires_at<?)",
            (moment, moment - stale_active_grace, moment),
        ).rowcount
        if marked:
            FleetDB.event(
                conn,
                "maintenance.executions_abandoned",
                None,
                {"count": marked, "grace_seconds": stale_active_grace},
            )
    report["executions_abandoned"] = marked

    # Phase 2 — aged rows with no inbound foreign keys.
    with db.transaction() as conn:
        report["events_removed"] = _delete_batched(
            conn, "events", "at<? AND kind NOT IN (%s)" % KEEP_KINDS_SQL,
            (moment - event_retention,) + KEEP_KINDS
        )
    with db.transaction() as conn:
        report["hook_events_removed"] = _delete_batched(
            conn, "hook_events", "at<?", (moment - hook_retention,)
        )

    # Phase 3 — terminal executions, children first. `parent_id` is
    # self-referential, so a parent whose child is still within retention stays
    # until the next sweep; iterating would let one run cascade unpredictably
    # deep while holding the writer lock.
    cutoff = moment - terminal_retention
    with db.transaction() as conn:
        doomed: List[str] = [
            str(row["id"]) for row in conn.execute(
                "SELECT id FROM executions "
                "WHERE status IN ('completed','abandoned','parked') "
                "AND updated_at<? "
                "AND id NOT IN ("
                "  SELECT parent_id FROM executions WHERE parent_id IS NOT NULL"
                ") LIMIT 5000",
                (cutoff,),
            ).fetchall()
        ]
        removed = 0
        leases = aliases = bindings = 0
        for index in range(0, len(doomed), 100):
            chunk = doomed[index:index + 100]
            marks = ",".join("?" * len(chunk))
            leases += conn.execute(
                f"DELETE FROM route_leases WHERE execution_id IN ({marks})",
                chunk,
            ).rowcount
            aliases += conn.execute(
                f"DELETE FROM session_aliases WHERE execution_id IN ({marks})",
                chunk,
            ).rowcount
            bindings += conn.execute(
                "DELETE FROM execution_session_bindings "
                f"WHERE execution_id IN ({marks})",
                chunk,
            ).rowcount
            removed += conn.execute(
                f"DELETE FROM executions WHERE id IN ({marks})", chunk
            ).rowcount
        report["executions_removed"] = removed
        report["route_leases_removed"] = leases
        report["session_aliases_removed"] = aliases
        report["execution_bindings_removed"] = bindings
        # Whatever is still here is aged-out but retained because a descendant
        # references it. The next sweep collects it once the child ages out.
        report["executions_deferred"] = conn.execute(
            "SELECT count(*) FROM executions "
            "WHERE status IN ('completed','abandoned','parked') AND updated_at<?",
            (cutoff,),
        ).fetchone()[0]

    # Phase 4 — process registry and orphaned lineage nodes.
    with db.transaction() as conn:
        report["execution_nodes_removed"] = _delete_batched(
            conn,
            "execution_nodes",
            "updated_at<? AND id NOT IN "
            "(SELECT parent_id FROM execution_nodes WHERE parent_id IS NOT NULL)",
            (moment - process_retention,),
        )
    with db.transaction() as conn:
        report["processes_removed"] = _delete_batched(
            conn,
            "processes",
            "last_seen_at<? AND id NOT IN "
            "(SELECT process_id FROM execution_nodes WHERE process_id IS NOT NULL)",
            (moment - process_retention,),
        )

    # Phase 5 — return WAL pages to the filesystem. TRUNCATE needs no writers
    # in flight; taking the advisory lock first means we are the only Fleet
    # writer, and failure here is cosmetic rather than a data problem.
    with db._writer_lock():  # noqa: SLF001 - same-module maintenance path
        conn = db.connect()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            report["wal_checkpointed"] = True
        except Exception as exc:  # pragma: no cover - diagnostic only
            report["wal_checkpointed"] = False
            report["wal_checkpoint_error"] = str(exc)
        finally:
            conn.close()

    return report


def vacuum(db: FleetDB) -> Dict[str, Any]:
    """Rebuild the database file. Never call this from a request path.

    VACUUM cannot run inside a transaction and takes an exclusive lock for the
    duration, so it belongs in an explicit operator command or a maintenance
    window — not in `sweep`, which is safe to schedule.
    """

    with db._writer_lock():  # noqa: SLF001 - operator-invoked maintenance
        conn = db.connect()
        try:
            before = conn.execute("PRAGMA page_count").fetchone()[0]
            conn.execute("VACUUM")
            after = conn.execute("PRAGMA page_count").fetchone()[0]
            page = conn.execute("PRAGMA page_size").fetchone()[0]
            return {
                "pages_before": before,
                "pages_after": after,
                "bytes_reclaimed": (before - after) * page,
            }
        finally:
            conn.close()
