"""Timestamping, size-capped log sink for the Fleet services.

Two defects motivated this, both found on 2026-07-30.

Unbounded growth: both LaunchAgents write straight to a `StandardErrorPath` with
no cap. One laptop produced a 12MB `dataplane.error.log` in a day and a half. On
the Harness authority server, with months of uptime, that is a slow disk-fill
with no operator watching.

No timestamps: neither TeamClaude's `console.error` output nor Python tracebacks
carry a time, and both files are opened in append mode across service restarts.
Diagnosing from them therefore measures *position in a file* rather than *time*,
which is actively misleading — the coordinator error log's most recent 1200 lines
held 87 `database is locked` errors that all predated the running process by a
day. Every line this sink writes is prefixed with an ISO-8601 local timestamp, so
"is this happening now" stops being a guess.

Rotation is size-triggered and keeps a fixed number of generations. Deliberately
no compression and no external logrotate dependency: the same code has to run on
macOS and Linux, and a service must not fail because a helper is missing.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, List, Optional


DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_KEEP = 3


def _stamp() -> str:
    now = time.time()
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
    return f"{base}.{int((now % 1) * 1000):03d}"


class RotatingSink:
    def __init__(
        self,
        path: str,
        max_bytes: int = DEFAULT_MAX_BYTES,
        keep: int = DEFAULT_KEEP,
    ) -> None:
        self.path = Path(path).expanduser()
        self.max_bytes = max(0, int(max_bytes))
        self.keep = max(0, int(keep))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle: Optional[IO[bytes]] = None
        self._size = 0
        self._open()

    def _open(self) -> None:
        self._handle = open(self.path, "ab", buffering=0)
        try:
            self._size = self.path.stat().st_size
        except OSError:
            self._size = 0

    def _rotate(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        if self.keep == 0:
            try:
                self.path.unlink()
            except OSError:
                pass
            self._open()
            return
        # Oldest generation first, so no rename ever clobbers a file we still
        # want. `.1` is the most recent rotation.
        oldest = self.path.with_name(f"{self.path.name}.{self.keep}")
        try:
            oldest.unlink()
        except OSError:
            pass
        for index in range(self.keep - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                try:
                    source.replace(
                        self.path.with_name(f"{self.path.name}.{index + 1}")
                    )
                except OSError:
                    pass
        try:
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))
        except OSError:
            pass
        self._open()

    def write_line(self, line: str) -> None:
        payload = f"{_stamp()} {line}\n".encode("utf-8", errors="replace")
        if self.max_bytes and self._size + len(payload) > self.max_bytes:
            self._rotate()
        assert self._handle is not None
        self._handle.write(payload)
        self._size += len(payload)

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def pump(
    path: str,
    max_bytes: int = DEFAULT_MAX_BYTES,
    keep: int = DEFAULT_KEEP,
    stream: Optional[IO[str]] = None,
) -> int:
    """Copy stdin to a rotating, timestamped file until EOF.

    Returns 0: losing the pipe is normal shutdown for a stdin-only sink.

    NOTE for service authors: do not use this mode in a unit's ExecStart via a
    shell pipeline. `sh` exits with the status of the LAST command in a pipeline,
    which would be this function — so a crashed service would look like a clean
    exit and `Restart=on-failure` would never fire. Use `run()` instead, which
    owns the child and exits with the child's status.
    """

    source = stream if stream is not None else sys.stdin
    sink = RotatingSink(path, max_bytes=max_bytes, keep=keep)
    try:
        for line in source:
            sink.write_line(line.rstrip("\n"))
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        sink.close()
        try:
            os.close(sys.stdin.fileno())
        except (OSError, ValueError):
            pass
    return 0


def run(
    path: str,
    argv: List[str],
    max_bytes: int = DEFAULT_MAX_BYTES,
    keep: int = DEFAULT_KEEP,
    stdout_path: Optional[str] = None,
) -> int:
    """Run `argv`, log its merged output, and exit with ITS status.

    This exists because the obvious unit line is wrong:

        ExecStart=/bin/sh -c 'exec service 2>&1 | fleet log-sink --path …'

    A POSIX shell exits with the status of the last pipeline element, so systemd
    would see the sink's exit code (always 0) instead of the service's. A crashed
    coordinator would read as a clean exit, `Restart=on-failure` would not fire,
    and the service would stay dead — silently, on a headless box. Owning the
    child here makes the status honest.

    SIGTERM/SIGINT are forwarded so `systemctl stop` reaches the real service
    rather than only its logger.

    With `stdout_path`, the two streams are kept in separate files instead of
    merged. This is not cosmetic: TeamClaude writes "All accounts exhausted —
    holding connection" to stdout, and `doctor` reads that file to decide whether
    the fleet is serving, so collapsing the streams would blind that check.
    """

    sink = RotatingSink(path, max_bytes=max_bytes, keep=keep)
    out_sink = (
        RotatingSink(stdout_path, max_bytes=max_bytes, keep=keep)
        if stdout_path
        else None
    )
    child = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if out_sink else subprocess.STDOUT,
        bufsize=1,
        text=True,
        errors="replace",
    )

    def forward(signum, _frame):
        try:
            child.send_signal(signum)
        except (ProcessLookupError, OSError):
            pass

    for name in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(name, forward)
        except (OSError, ValueError, AttributeError):
            pass

    def drain(stream, target):
        try:
            for line in stream:
                target.write_line(line.rstrip("\n"))
        except (ValueError, OSError):
            pass

    threads = []
    if out_sink is not None:
        # stdout -> out_sink, stderr -> sink, concurrently: reading them in
        # sequence would let the unread pipe fill and block the service.
        for stream, target in ((child.stdout, out_sink), (child.stderr, sink)):
            if stream is None:
                continue
            thread = threading.Thread(
                target=drain, args=(stream, target), daemon=True
            )
            thread.start()
            threads.append(thread)

    try:
        if out_sink is None:
            if child.stdout is not None:
                drain(child.stdout, sink)
        else:
            for thread in threads:
                thread.join()
        return child.wait()
    except KeyboardInterrupt:
        forward(signal.SIGINT, None)
        return child.wait()
    finally:
        sink.close()
        if out_sink is not None:
            out_sink.close()
