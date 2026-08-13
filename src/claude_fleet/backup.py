"""Snapshots of the state database, because nothing else could put one back.

`fleet-rollback` says so in its own docstring: it deploys code, and "a schema
written by a newer version is not something git can undo". That was an honest
limitation right up until the next change needs to rewrite the accounts table —
and then it is the difference between a bad afternoon and a lost fleet.

The evidence that this was always needed is sitting in the state directory:

    fleet.sqlite3.before-orphan-cleanup           61 MB, 07-30
    fleet.sqlite3.pre-cap-migration-20260729-1430 25 MB, 07-29

Two snapshots, taken by hand, named by hand, at the two moments someone was
about to do something irreversible. What was missing was not the idea — it was
a snapshot that happens without anyone remembering to take one, and a way back
that does not depend on guessing which hand-named file was the good one.

## Why VACUUM INTO rather than the backup API

Measured on the live 94 MB database, with the fleet serving:

    backup API    0.2s   94.6 MB
    VACUUM INTO   0.1s   59.2 MB

Faster and 37% smaller, because the database carries a lot of free pages from
swept events. Both are safe against a live writer; VACUUM INTO also produces a
defragmented file, so a restore lands on a compact database rather than
inheriting the fragmentation of whatever moment it was taken.
"""

from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .db import connect_readonly

# Ten is roughly one working day of deploys at 59 MB each — about 600 MB on a
# disk with 140 GB free. Cheap enough that keeping fewer would only trade real
# safety for storage nobody is short of.
KEEP = 10

# Sortable, greppable, and unambiguous about which is newest. The reason is in
# the name because "which of these was the good one" is the question you ask at
# the worst possible time.
STAMP = "%Y%m%dT%H%M%S"


def backup_dir(db_path: str) -> Path:
    return Path(db_path).parent / "backups"


def _stamp(now: Optional[float] = None) -> str:
    return time.strftime(STAMP, time.localtime(now if now is not None else time.time()))


def list_backups(db_path: str) -> List[Dict[str, Any]]:
    """Newest first."""
    directory = backup_dir(db_path)
    if not directory.is_dir():
        return []
    items = []
    for path in directory.glob("fleet-*.sqlite3"):
        try:
            stat = path.stat()
        except OSError:
            continue
        # fleet-<stamp>-<reason>.sqlite3
        parts = path.stem.split("-", 2)
        items.append({
            "path": str(path),
            "at": stat.st_mtime,
            "bytes": stat.st_size,
            "reason": parts[2] if len(parts) > 2 else "",
        })
    # Sorted by name as well as time, because two snapshots taken in the same
    # second share an mtime and "newest" would otherwise be whichever the
    # filesystem happened to list first. `restore` with no argument takes the
    # first entry, so an ambiguous order there is an ambiguous recovery.
    return sorted(items, key=lambda item: (item["at"], item["path"]), reverse=True)


def _prune(db_path: str, keep: int = KEEP) -> List[str]:
    removed = []
    for item in list_backups(db_path)[keep:]:
        try:
            os.unlink(item["path"])
            removed.append(item["path"])
        except OSError:
            pass
    return removed


def create(db_path: str, reason: str = "manual", keep: int = KEEP) -> Dict[str, Any]:
    """Snapshot the database. Safe to run against a live, serving fleet."""
    source = Path(db_path)
    if not source.exists():
        raise FileNotFoundError("no database at %s" % db_path)

    directory = backup_dir(db_path)
    directory.mkdir(parents=True, exist_ok=True)
    safe_reason = "".join(
        char if char.isalnum() or char in "._" else "_" for char in reason
    )[:40] or "manual"
    # Two snapshots with the same reason inside one second would land on the
    # same name, and the second would replace the first. Caught by the test for
    # "restoring is reversible": a restore takes a pre_restore snapshot, and
    # restoring that snapshot takes another — same second, same reason, so the
    # state being preserved was overwritten by the state it was meant to undo.
    # Silent, and only visible at the moment you reach for the earlier one.
    target = directory / ("fleet-%s-%s.sqlite3" % (_stamp(), safe_reason))
    serial = 1
    while target.exists():
        target = directory / (
            "fleet-%s-%s.%d.sqlite3" % (_stamp(), safe_reason, serial))
        serial += 1

    # A stale partial file would look like a backup to `list_backups` and to a
    # frightened operator, so build under a temp name and rename on success.
    partial = target.with_suffix(".partial")
    started = time.time()
    # Not `mode=ro`. A read-only connection cannot create the -shm file a WAL
    # database needs, so it fails with "unable to open database file" on any
    # database whose services are not currently running — which is precisely the
    # state right after a restore, when you most want a second snapshot before
    # touching anything. Measured: worked against the live database only because
    # the coordinator was holding the -shm open. `connect_readonly` already
    # carries the same fallback for the same reason.
    conn = connect_readonly(db_path, timeout=30)
    try:
        # VACUUM INTO refuses an existing target, which is the behaviour we want
        # for the real name; the partial is ours to clear.
        if partial.exists():
            partial.unlink()
        conn.execute("VACUUM INTO ?", (str(partial),))
    except BaseException:
        if partial.exists():
            partial.unlink()
        raise
    finally:
        conn.close()
    os.replace(str(partial), str(target))
    os.chmod(str(target), 0o600)

    return {
        "path": str(target),
        "bytes": target.stat().st_size,
        "seconds": round(time.time() - started, 2),
        "pruned": _prune(db_path, keep),
    }


def _coordinator_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def restore(db_path: str, source: Optional[str] = None, *,
            coordinator_port: Optional[int] = None,
            force: bool = False) -> Dict[str, Any]:
    """Put a snapshot back. Refuses while the coordinator is still holding it.

    Two things this must not do, both learned the expensive way elsewhere in
    this system:

    - **Replace the file under a live writer.** The coordinator has the database
      open; overwriting it leaves that process with a handle to a file that no
      longer exists at that path, and its next commit either resurrects the old
      state or corrupts the new. So this refuses unless the port is quiet.
    - **Leave the old -wal and -shm behind.** They describe the file that was
      just replaced. SQLite would apply those frames on top of the restored
      database, silently reintroducing exactly the writes you were undoing.

    The current state is snapshotted first, so a restore is itself reversible —
    restoring the wrong backup at 3am should not be a one-way door.
    """
    target = Path(db_path)
    if source is None:
        available = list_backups(db_path)
        if not available:
            raise FileNotFoundError(
                "没有可用备份。`fleet backup` 会建一个。")
        source = available[0]["path"]
    source_path = Path(source).expanduser()
    if not source_path.exists():
        raise FileNotFoundError("no backup at %s" % source_path)

    if coordinator_port and not force and _coordinator_listening(coordinator_port):
        raise RuntimeError(
            "协调器还在 127.0.0.1:%d 上跑着, 它开着这个数据库。\n"
            "  先停掉服务再恢复, 否则它下一次提交会把旧状态写回来:\n"
            "    launchctl bootout gui/$(id -u)/com.claude-fleet.coordinator\n"
            "  (Linux: sudo systemctl stop claude-fleet-coordinator)\n"
            "  确实知道自己在做什么就加 --force。" % coordinator_port)

    # Reversible: whatever is there now becomes a backup before it is replaced.
    safety = None
    if target.exists():
        safety = create(db_path, reason="pre_restore")["path"]

    shutil.copyfile(str(source_path), str(target))
    os.chmod(str(target), 0o600)
    removed = []
    for suffix in ("-wal", "-shm"):
        stale = Path(str(target) + suffix)
        if stale.exists():
            stale.unlink()
            removed.append(str(stale))

    return {
        "restored_from": str(source_path),
        "database": str(target),
        "previous_state_saved_to": safety,
        "removed_stale": removed,
    }
