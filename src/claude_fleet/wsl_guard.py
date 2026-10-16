"""Invisible Windows helpers that keep the local WSL SSH bridge available."""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shlex
import shutil
import socket
import stat as stat_module
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from .desktop import hidden_subprocess_kwargs
from .disk_policy import GIB, append_disk_sample, assess_disk


LOW_DISK_EXIT = 4
DEFAULT_MIN_FREE_GB = 16.0
DEFAULT_MAINTENANCE_TRIGGER_GB = 128.0
DEFAULT_RECLAIM_TRIGGER_GB = 128.0
DEFAULT_TEMP_MAX_AGE_DAYS = 7.0
DEFAULT_BUILD_TEMP_MAX_AGE_HOURS = 6.0
DEFAULT_CLAUDE_TARGET_MAX_AGE_HOURS = 3.0
DEFAULT_FIXED_CACHE_COOLDOWN_MINUTES = 15.0
DEFAULT_FIXED_CLEANUP_MAX_GB = 48.0
DEFAULT_FIXED_CLEANUP_MAX_SECONDS = 90.0
DEFAULT_WINDOWS_TEMP_CLEANUP_MAX_SECONDS = 90.0
# Incremental Rust artifacts are fully reproducible and grow extremely fast in
# multi-worktree development.  A half-day grace period protects active work
# while allowing warning-level maintenance to recover useful space before the
# drive becomes critical.
DEFAULT_WINDOWS_INCREMENTAL_MIN_AGE_HOURS = 12.0
MIN_PROGRESS_BYTES = 64 * 1024**2
MAINTENANCE_STATE_NAME = "disk-maintenance.json"
MANUAL_MAINTENANCE_REQUEST_NAME = "disk-maintenance.request.json"
SAFE_BUILD_TEMP_PREFIXES = (
    "evox-",
    "evomap-",
    "puppeteer_dev_chrome_profile-",
)
SAFE_CLAUDE_TARGET_PATTERN = re.compile(r"^target-[A-Za-z0-9._-]+$")
SAFE_FIXED_DRIVE_CACHE_RULES = {
    # Ordered by recovery value.  The maintenance pass has a bounded runtime,
    # so large old quarantine files must not sit behind a package store with
    # tens of thousands of tiny entries.
    "cdrivecleanupquarantine": (7.0, "cleanup-quarantine"),
    ".pnpm-store": (30.0, "pnpm-package-cache"),
    "go-build-cache": (7.0, "go-build-cache"),
    "tmpcargo": (7.0, "cargo-temp-cache"),
    "gotmp": (7.0, "go-temp-cache"),
    "npmtmp": (7.0, "npm-temp-cache"),
    "tmp-claude": (7.0, "claude-temp-cache"),
    # Historical Codex/Rust overflow targets are reproducible build output.
    # These exact root names were used by the old Windows overflow scripts;
    # source trees and the generic, potentially active ``cargo-target`` remain
    # outside the allowlist.
    "codex-targets": (7.0, "fixed-drive-build-cache"),
    "codex-build-overflow": (7.0, "fixed-drive-build-cache"),
    "codex-target-evox-runtime-ledger-queue": (7.0, "fixed-drive-build-cache"),
    "cargo-target-evox-runtime-ledger-queue-20260716": (
        7.0,
        "fixed-drive-build-cache",
    ),
    # Dedicated root-level scratch directories are common on secondary build
    # volumes.  They are not project trees: only entries older than the normal
    # retention window are removed, while recent files and locked build output
    # remain protected by ``clean_stale_windows_temp``.
    "tmp": (7.0, "fixed-drive-temp"),
    "temp": (7.0, "fixed-drive-temp"),
}


def _system_executable(name: str) -> str:
    windir = Path(os.environ.get("WINDIR", "C:/Windows"))
    return str(windir / "System32" / name)


def run_hidden(command: Sequence[str], timeout: Optional[float] = None) -> int:
    """Run a console program without ever allocating a visible console window."""
    try:
        completed = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1
    return completed.returncode


def run_capture(
    command: Sequence[str], timeout: Optional[float] = None
) -> subprocess.CompletedProcess[str]:
    """Run a console program invisibly while retaining bounded text output."""
    try:
        return subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(list(command), 1, "", str(exc))


def run_capture_input(
    command: Sequence[str], input_text: str, timeout: Optional[float] = None
) -> subprocess.CompletedProcess[str]:
    """Run a hidden command with script input, used by the SSH maintenance path."""
    try:
        # Bytes avoid Windows text-mode pipes translating LF back to CRLF.
        completed = subprocess.run(
            list(command),
            input=input_text.replace("\r\n", "\n").encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            **hidden_subprocess_kwargs(),
        )
        return subprocess.CompletedProcess(
            list(command),
            completed.returncode,
            completed.stdout.decode("utf-8", errors="replace"),
            completed.stderr.decode("utf-8", errors="replace"),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(list(command), 1, "", str(exc))


def _ssh_maintenance_capture(
    port: int,
    ssh_user: str,
    identity: str,
    script: str,
    *,
    args: Sequence[str] = (),
    as_root: bool = False,
    timeout: Optional[float] = None,
) -> subprocess.CompletedProcess[str]:
    """Run maintenance through the existing SSH bridge when wsl.exe is wedged."""
    ssh = _system_executable("OpenSSH/ssh.exe")
    remote = ("sudo -n " if as_root else "") + "/bin/bash -s --"
    if args:
        remote += " " + " ".join(shlex.quote(value) for value in args)
    return run_capture_input(
        [
            ssh,
            "-i", identity,
            "-p", str(port),
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ServerAliveInterval=3",
            "-o", "ServerAliveCountMax=2",
            f"{ssh_user}@127.0.0.1",
            remote,
        ],
        script.replace("\r\n", "\n"),
        timeout=timeout,
    )


def established_ssh_clients(port: int) -> Optional[int]:
    """Count Windows clients for the forwarded port; None means unknown."""
    try:
        completed = subprocess.run(
            [_system_executable("netstat.exe"), "-ano", "-p", "tcp"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    count = 0
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[0].upper() == "TCP":
            if fields[2].endswith(f":{port}") and fields[3].upper() == "ESTABLISHED":
                count += 1
    return count


def tcp_ready(port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def ssh_banner_ready(port: int, timeout: float = 1.0) -> bool:
    """Require an SSH banner, not merely a TCP accept from a wedged listener."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as client:
            client.settimeout(timeout)
            return client.recv(64).startswith(b"SSH-")
    except OSError:
        return False


def ssh_authenticated_ready(
    port: int,
    ssh_user: str,
    identity: str,
    timeout: float = 8.0,
) -> bool:
    """Require a complete public-key login, not only a potentially wedged banner."""
    if not ssh_user or not identity or not Path(identity).is_file():
        return False
    return run_hidden(
        [
            _system_executable("OpenSSH/ssh.exe"),
            "-i", identity,
            "-p", str(port),
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=3",
            "-o", "ConnectionAttempts=1",
            "-o", "ServerAliveInterval=2",
            "-o", "ServerAliveCountMax=1",
            f"{ssh_user}@127.0.0.1",
            "true",
        ],
        timeout=timeout,
    ) == 0


def wait_ssh_banner(port: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if ssh_banner_ready(port):
            return True
        time.sleep(0.4)
    return ssh_banner_ready(port)


def _number_setting(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        return max(0.0, float(raw)) if raw else default
    except ValueError:
        return default


def host_disk_usage() -> Any:
    anchor = os.environ.get("LOCALAPPDATA") or os.environ.get("SystemDrive", "C:/")
    return shutil.disk_usage(anchor)


def fixed_disk_roots() -> list[Path]:
    """Return local fixed-drive roots without starting or mounting anything."""
    if os.name != "nt":
        return []
    try:
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()
        drive_type = ctypes.windll.kernel32.GetDriveTypeW
    except (AttributeError, OSError):
        return []
    roots: list[Path] = []
    for index in range(26):
        if not mask & (1 << index):
            continue
        root = Path(f"{chr(65 + index)}:/")
        if drive_type(str(root)) == 3:  # DRIVE_FIXED
            roots.append(root)
    return roots


def _disk_pressure(usage: Any) -> str:
    """Use the same adaptive thresholds as the admission guard."""
    total = max(0, int(getattr(usage, "total", 0) or 0))
    free = max(0, int(getattr(usage, "free", 0) or 0))
    used_percent = ((total - free) / total * 100) if total else 0.0
    return str(
        assess_disk(total, free, used_percent=used_percent).get("level") or "unknown"
    )


def fixed_disk_snapshots() -> list[Dict[str, Any]]:
    snapshots: list[Dict[str, Any]] = []
    for root in fixed_disk_roots():
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            continue
        snapshots.append({
            "drive": str(root.drive or root).rstrip("\\/"),
            "root": root,
            "usage": usage,
            "level": _disk_pressure(usage),
        })
    return snapshots


def _enrich_fixed_disk_snapshots(
    snapshots: Sequence[Dict[str, Any]],
    previous_state: Dict[str, Any],
    *,
    now_epoch: Optional[float] = None,
) -> list[Dict[str, Any]]:
    now = time.time() if now_epoch is None else float(now_epoch)
    previous = {
        str(item.get("drive") or ""): item
        for item in previous_state.get("drives", [])
        if isinstance(item, dict)
    }
    enriched: list[Dict[str, Any]] = []
    for item in snapshots:
        row = dict(item)
        drive = str(row.get("drive") or "")
        usage = row.get("usage")
        total = int(getattr(usage, "total", 0) or 0)
        free = int(getattr(usage, "free", 0) or 0)
        old = previous.get(drive, {})
        samples = append_disk_sample(old.get("samples"), free, now_epoch=now)
        assessment = assess_disk(
            total,
            free,
            previous_level=str(old.get("levelAfter") or old.get("level") or "unknown"),
            samples=samples,
        )
        row.update(assessment)
        row["samples"] = samples
        row["nextEligibleEpoch"] = float(old.get("nextEligibleEpoch") or 0)
        row["noProgressCount"] = int(old.get("noProgressCount") or 0)
        enriched.append(row)
    return enriched


def host_disk_has_headroom() -> bool:
    """Avoid starting WSL when its host volume is already critically full."""
    minimum_gb = _number_setting("QUOTAPOOL_WSL_MIN_FREE_GB", DEFAULT_MIN_FREE_GB)
    try:
        free_bytes = host_disk_usage().free
    except OSError:
        # A failed safety probe should be observable, but must not permanently
        # disable an otherwise healthy installation.
        return True
    return free_bytes >= minimum_gb * 1024**3


def maintenance_state_path(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit)
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return root / "Quotapool" / "state" / MAINTENANCE_STATE_NAME


def read_maintenance_state(explicit: Optional[str] = None) -> Dict[str, Any]:
    try:
        value = json.loads(maintenance_state_path(explicit).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def manual_maintenance_request_path(explicit: Optional[str] = None) -> Path:
    return maintenance_state_path(explicit).with_name(MANUAL_MAINTENANCE_REQUEST_NAME)


def read_manual_maintenance_request(explicit: Optional[str] = None) -> Dict[str, Any]:
    try:
        value = json.loads(
            manual_maintenance_request_path(explicit).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    drive = str(value.get("drive") or "").upper()
    if not re.fullmatch(r"[A-Z]:", drive):
        return {}
    value["drive"] = drive
    return value


def request_manual_maintenance(
    drive: str,
    explicit: Optional[str] = None,
) -> Dict[str, Any]:
    """Queue one safe, targeted pass for the scheduled maintenance worker."""
    normalized = str(drive or "").strip().upper()
    if not re.fullmatch(r"[A-Z]:", normalized):
        raise ValueError("磁盘必须使用 C:、D: 这样的盘符")
    available = {
        str(item.get("drive") or "").upper()
        for item in fixed_disk_snapshots()
        if item.get("root") is not None and item.get("usage") is not None
    }
    if normalized not in available:
        raise ValueError(f"{normalized} 不是当前可用的本地固定磁盘")
    value = {
        "requestId": f"manual-{time.time_ns()}",
        "drive": normalized,
        "requestedAt": datetime.now(timezone.utc).isoformat(),
        "status": "queued",
        "force": True,
    }
    target = manual_maintenance_request_path(explicit)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return value


def _write_maintenance_state(value: Dict[str, Any], explicit: Optional[str]) -> None:
    target = maintenance_state_path(explicit)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def clean_stale_windows_temp(
    root: Path,
    max_age_days: float,
    *,
    max_bytes: Optional[int] = None,
    deadline: Optional[float] = None,
) -> Dict[str, Any]:
    """Delete only old files in the current user's temp tree, never junctions."""
    if not root.is_dir() or _is_reparse_point(root):
        return {"name": "windows-temp", "status": "skipped", "files": 0, "bytes": 0}
    cutoff = time.time() - max_age_days * 86400
    started = time.monotonic()
    removed_files = 0
    removed_bytes = 0
    scanned_files = 0
    skipped_recent = 0
    skipped_locked = 0
    directories: list[Path] = []
    stop_reason = ""
    for current, names, files in os.walk(root, topdown=True, followlinks=False):
        if deadline is not None and time.monotonic() >= deadline:
            stop_reason = "time-budget-reached"
            break
        current_path = Path(current)
        names[:] = [name for name in names if not _is_reparse_point(current_path / name)]
        # Continue through the oldest branches first.  Content-addressed stores
        # often keep a small, recently touched index next to an old multi-GB
        # payload tree; filesystem enumeration order otherwise starves the old
        # payload on every bounded maintenance pass.
        def directory_age(name: str) -> float:
            try:
                return (current_path / name).lstat().st_mtime
            except OSError:
                return float("inf")

        names.sort(key=directory_age)
        directories.extend(current_path / name for name in names)
        for name in files:
            if max_bytes is not None and removed_bytes >= max_bytes:
                stop_reason = "byte-budget-reached"
                break
            if deadline is not None and time.monotonic() >= deadline:
                stop_reason = "time-budget-reached"
                break
            candidate = current_path / name
            scanned_files += 1
            try:
                stat = candidate.lstat()
                if _is_reparse_point(candidate) or stat.st_mtime > cutoff:
                    skipped_recent += 1
                    continue
                try:
                    candidate.unlink()
                except PermissionError:
                    # Git objects and package-manager payloads are frequently
                    # marked read-only on Windows.  Inside an explicitly
                    # approved cache root they are still reproducible cache,
                    # not evidence that a process currently owns the file.
                    candidate.chmod(stat_module.S_IWRITE)
                    candidate.unlink()
                removed_files += 1
                removed_bytes += stat.st_size
            except OSError:
                # Open/locked temp files are expected and are deliberately left alone.
                skipped_locked += 1
                continue
        if stop_reason:
            break
    for directory in reversed(directories):
        try:
            directory.rmdir()
        except OSError:
            pass
    return {
        "name": "windows-temp",
        "status": stop_reason or "cleaned",
        "files": removed_files,
        "bytes": removed_bytes,
        "scannedFiles": scanned_files,
        "skippedRecent": skipped_recent,
        "skippedLocked": skipped_locked,
        "durationSeconds": round(time.monotonic() - started, 2),
    }


def clean_safe_windows_caches(local_appdata: Path, windows_temp: Path) -> list[Dict[str, Any]]:
    """Reclaim reproducible caches without touching sessions, worktrees, or models."""
    actions: list[Dict[str, Any]] = []
    cache_rules = (
        (local_appdata / "npm-cache" / "_cacache", 1.0, "npm-download-cache"),
        (local_appdata / "NVIDIA" / "DXCache", 30.0, "nvidia-shader-cache"),
        (local_appdata / "CrashDumps", 1.0, "windows-crash-dumps"),
    )
    for root, age_days, name in cache_rules:
        result = clean_stale_windows_temp(root, age_days)
        result["name"] = name
        actions.append(result)

    removed_files = 0
    removed_bytes = 0
    removed_directories = 0
    cutoff = time.time() - DEFAULT_BUILD_TEMP_MAX_AGE_HOURS * 3600
    if windows_temp.is_dir() and not _is_reparse_point(windows_temp):
        try:
            children = tuple(windows_temp.iterdir())
        except OSError:
            children = ()
        for child in children:
            try:
                stat = child.lstat()
            except OSError:
                continue
            if (
                not child.is_dir()
                or _is_reparse_point(child)
                or stat.st_mtime > cutoff
                or not child.name.casefold().startswith(SAFE_BUILD_TEMP_PREFIXES)
            ):
                continue
            result = clean_stale_windows_temp(child, 0.0)
            removed_files += int(result.get("files", 0))
            removed_bytes += int(result.get("bytes", 0))
            try:
                child.rmdir()
                removed_directories += 1
            except OSError:
                pass
    actions.append({
        "name": "windows-build-temp",
        "status": "cleaned",
        "files": removed_files,
        "directories": removed_directories,
        "bytes": removed_bytes,
    })
    return actions


def _windows_process_command_lines() -> tuple[bool, list[str]]:
    """Return current Windows process command lines, failing closed on errors."""
    if os.name != "nt":
        return True, []
    powershell = shutil.which("powershell.exe")
    if not powershell:
        return False, []
    script = (
        "@(Get-CimInstance Win32_Process -ErrorAction Stop|"
        "ForEach-Object{[string]$_.CommandLine})|ConvertTo-Json -Compress"
    )
    result = run_capture(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-Command",
            script,
        ],
        timeout=30,
    )
    if result.returncode != 0:
        return False, []
    try:
        value = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return False, []
    values = value if isinstance(value, list) else [value]
    return True, [str(item) for item in values if item]


def _path_referenced_by_process(path: Path, commands: Sequence[str]) -> bool:
    windows = str(path).replace("/", "\\").casefold()
    variants = {windows, windows.replace("\\", "/")}
    match = re.match(r"^([a-z]):\\(.*)$", windows)
    if match:
        variants.add(f"/{match.group(1)}/{match.group(2).replace('\\', '/')}")
    for raw in commands:
        command = str(raw).casefold().replace("\\\\", "\\")
        if any(value in command or value in command.replace("\\", "/") for value in variants):
            return True
    return False


def _tree_has_recent_write(root: Path, cutoff: float, deadline: float) -> Optional[bool]:
    """Return True on recent activity and None when a safe scan cannot finish."""
    for current, names, files in os.walk(root, topdown=True, followlinks=False):
        if time.monotonic() >= deadline:
            return None
        current_path = Path(current)
        try:
            if current_path.lstat().st_mtime > cutoff:
                return True
        except OSError:
            return None
        retained: list[str] = []
        for name in names:
            child = current_path / name
            if _is_reparse_point(child):
                return None
            try:
                if child.lstat().st_mtime > cutoff:
                    return True
            except OSError:
                return None
            retained.append(name)
        names[:] = retained
        for name in files:
            try:
                if (current_path / name).lstat().st_mtime > cutoff:
                    return True
            except OSError:
                return None
    return False


def clean_inactive_claude_build_targets(
    windows_temp: Path,
    *,
    min_age_hours: float = DEFAULT_CLAUDE_TARGET_MAX_AGE_HOURS,
    process_commands: Optional[Sequence[str]] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Reclaim idle reproducible build targets from one exact Claude temp root."""
    root = windows_temp / "claude" / "evox-wt"
    action: Dict[str, Any] = {
        "name": "claude-build-targets",
        "drive": str(windows_temp.drive).upper() or "C:",
        "status": "cleaned",
        "scope": "windows-temp-claude-target-only",
        "bytes": 0,
        "files": 0,
        "directories": 0,
        "cleaned": [],
        "skippedActive": [],
        "skippedRecent": [],
    }
    if not root.is_dir() or _is_reparse_point(root):
        action["status"] = "skipped"
        return action
    try:
        approved_root = root.resolve(strict=True)
        children = tuple(root.iterdir())
    except OSError:
        action["status"] = "probe-failed"
        return action

    if process_commands is None:
        process_ok, commands = _windows_process_command_lines()
        if not process_ok:
            action["status"] = "process-probe-failed"
            return action
    else:
        commands = [str(value) for value in process_commands]

    cutoff = (time.time() if now is None else now) - max(1.0, min_age_hours) * 3600
    scan_seconds = _number_setting(
        "QUOTAPOOL_WINDOWS_TEMP_CLEANUP_MAX_SECONDS",
        DEFAULT_WINDOWS_TEMP_CLEANUP_MAX_SECONDS,
    )
    candidates: list[Path] = []
    for child in children:
        if SAFE_CLAUDE_TARGET_PATTERN.fullmatch(child.name):
            candidates.append(child)
            continue
        # Claude's current Windows worktree layout places the reproducible
        # Cargo cache at ``evox-wt/<worktree>/target``.  Accept only that exact
        # depth and require the parent to be a real Git worktree; the worktree
        # and all source files remain outside the deletion target.
        nested_target = child / "target"
        try:
            worktree = child.resolve(strict=True)
        except OSError:
            continue
        if (
            worktree.parent == approved_root
            and worktree.is_dir()
            and not _is_reparse_point(worktree)
            and (worktree / ".git").exists()
            and nested_target.is_dir()
        ):
            candidates.append(nested_target)

    for child in sorted(candidates, key=lambda value: str(value).casefold()):
        try:
            candidate = child.resolve(strict=True)
        except OSError:
            continue
        direct_target = (
            candidate.parent == approved_root
            and SAFE_CLAUDE_TARGET_PATTERN.fullmatch(candidate.name)
        )
        worktree = candidate.parent
        nested_target = (
            candidate.name.casefold() == "target"
            and worktree.parent == approved_root
            and worktree.is_dir()
            and not _is_reparse_point(worktree)
            and (worktree / ".git").exists()
        )
        if not (direct_target or nested_target) or not candidate.is_dir() or _is_reparse_point(candidate):
            continue
        if _path_referenced_by_process(candidate, commands):
            action["skippedActive"].append(str(candidate))
            continue
        recent = _tree_has_recent_write(
            candidate,
            cutoff,
            time.monotonic() + scan_seconds,
        )
        if recent is not False:
            action["skippedRecent"].append(str(candidate))
            continue
        # Re-probe immediately before deletion to close the long tree-scan gap.
        if process_commands is None:
            process_ok, latest_commands = _windows_process_command_lines()
            if not process_ok or _path_referenced_by_process(candidate, latest_commands):
                action["skippedActive"].append(str(candidate))
                continue
        quarantine = candidate.parent / (
            f".quotapool-cleaning-{candidate.name}-{os.getpid()}-{time.time_ns()}"
        )
        try:
            candidate.rename(quarantine)
        except OSError:
            # An open handle or a newly started builder keeps the original
            # directory untouched.  Renaming also ensures a later builder can
            # create a fresh target while the isolated old cache is removed.
            action["skippedActive"].append(str(candidate))
            continue
        result = clean_stale_windows_temp(
            quarantine,
            0.0,
            deadline=time.monotonic() + scan_seconds,
        )
        action["bytes"] += int(result.get("bytes") or 0)
        action["files"] += int(result.get("files") or 0)
        try:
            quarantine.rmdir()
            action["directories"] += 1
            action["cleaned"].append(str(candidate))
        except OSError:
            # Preserve any locked remainder under its original approved name.
            if not candidate.exists():
                try:
                    quarantine.rename(candidate)
                except OSError:
                    pass
            action["skippedActive"].append(str(candidate))
    if not action["cleaned"] and action["skippedActive"]:
        action["status"] = "active-protected"
    elif not action["cleaned"] and action["skippedRecent"]:
        action["status"] = "recent-protected"
    return action


def _approved_fixed_drive_cache_root(drive_root: Path, candidate: Path) -> bool:
    """Require an exact, non-linked allowlisted child of a fixed-drive root."""
    try:
        root = drive_root.resolve(strict=True)
        target = candidate.resolve(strict=True)
    except OSError:
        return False
    return (
        target.is_dir()
        and not _is_reparse_point(target)
        and target.parent == root
        and target.name.casefold() in SAFE_FIXED_DRIVE_CACHE_RULES
    )


def clean_safe_fixed_drive_caches(
    snapshots: Optional[Sequence[Dict[str, Any]]] = None,
    *,
    ignore_backoff: bool = False,
) -> list[Dict[str, Any]]:
    """Clean age-bounded caches with per-run byte/time budgets and recovery stops.

    Project trees, repositories, build ``target`` directories, models, sessions,
    transcripts and Docker data are intentionally outside the allowlist. Locked
    or recently modified files are skipped by ``clean_stale_windows_temp``.
    """
    rows = list(snapshots) if snapshots is not None else fixed_disk_snapshots()
    actions: list[Dict[str, Any]] = []
    now_epoch = time.time()
    deadline = time.monotonic() + _number_setting(
        "QUOTAPOOL_FIXED_CLEANUP_MAX_SECONDS",
        DEFAULT_FIXED_CLEANUP_MAX_SECONDS,
    )
    max_run_bytes = int(_number_setting(
        "QUOTAPOOL_FIXED_CLEANUP_MAX_GB",
        DEFAULT_FIXED_CLEANUP_MAX_GB,
    ) * GIB)
    for disk in rows:
        level = str(disk.get("level") or "healthy")
        mode = str(disk.get("mode") or {
            "critical": "critical",
            "warning": "warning",
        }.get(level, "observe"))
        if mode == "observe":
            continue
        root_value = disk.get("root")
        if not root_value:
            continue
        drive_root = Path(root_value)
        drive = str(disk.get("drive") or drive_root.drive).upper()
        next_eligible = float(disk.get("nextEligibleEpoch") or 0)
        if next_eligible > now_epoch and not ignore_backoff:
            actions.append({
                "name": "fixed-drive-cache-maintenance",
                "drive": drive,
                "status": "backoff",
                "scope": "approved-cache-only",
                "bytes": 0,
                "nextActionEpoch": next_eligible,
            })
            continue
        try:
            initial_usage = shutil.disk_usage(drive_root)
        except OSError:
            continue
        fallback_assessment = assess_disk(
            int(initial_usage.total),
            int(initial_usage.free),
            previous_level=level,
        )
        target_free = int(
            disk.get("recoveryBytes") or fallback_assessment["recoveryBytes"]
        )
        needed = max(0, target_free - int(initial_usage.free))
        if mode == "predictive":
            needed = max(needed, 8 * GIB)
        drive_budget = min(max_run_bytes, max(2 * GIB, needed))
        age_factor = {
            "emergency": 0.25,
            "critical": 1.0,
            "warning": 2.0,
            "predictive": 4.0,
        }.get(mode, 2.0)
        drive_removed = 0
        final_status = "completed"
        for leaf, (age_days, name) in SAFE_FIXED_DRIVE_CACHE_RULES.items():
            try:
                current_usage = shutil.disk_usage(drive_root)
                if int(current_usage.free) >= target_free:
                    final_status = "recovered-stop"
                    break
            except OSError:
                final_status = "probe-failed"
                break
            if time.monotonic() >= deadline:
                final_status = "time-budget-reached"
                break
            remaining = drive_budget - drive_removed
            if remaining <= 0:
                final_status = "byte-budget-reached"
                break
            candidate = drive_root / leaf
            if not _approved_fixed_drive_cache_root(drive_root, candidate):
                continue
            result = clean_stale_windows_temp(
                candidate,
                age_days * age_factor,
                max_bytes=remaining,
                deadline=deadline,
            )
            result.update({
                "name": name,
                "drive": drive,
                "root": str(candidate),
                "retentionDays": age_days * age_factor,
                "scope": "approved-cache-only",
                "mode": mode,
            })
            actions.append(result)
            drive_removed += int(result.get("bytes") or 0)
            if result.get("status") in {"time-budget-reached", "byte-budget-reached"}:
                final_status = str(result["status"])
                break
        actions.append({
            "name": "fixed-drive-cache-maintenance",
            "drive": drive,
            "status": final_status,
            "scope": "approved-cache-only",
            "bytes": 0,
            "reclaimedBytes": drive_removed,
            "budgetBytes": drive_budget,
            "targetFreeBytes": target_free,
            "mode": mode,
            "reason": str(disk.get("reason") or "磁盘压力触发"),
        })
    return actions


def _windows_builds_active() -> bool:
    """Fail closed while any native Rust build tool is running."""
    if os.name != "nt":
        return False
    process_ok, commands = _windows_process_command_lines()
    if not process_ok:
        return True
    executable = re.compile(
        r"(?:^|[\\/\s\"])(?:cargo|rustc|clippy-driver|sccache|link)(?:\.exe)?(?:[\s\"]|$)",
        re.IGNORECASE,
    )
    return any(executable.search(command) for command in commands)


def _approved_windows_worktree_target(candidate: Path) -> bool:
    """Accept only an ordinary Cargo target root under a Git worktree."""
    try:
        target = candidate.resolve(strict=True)
    except OSError:
        return False
    worktree = target.parent
    return (
        (
            target.name.casefold() == "target"
            or SAFE_CLAUDE_TARGET_PATTERN.fullmatch(target.name) is not None
        )
        and target.is_dir()
        and not _is_reparse_point(target)
        and worktree.parent != worktree
        and (worktree / ".git").exists()
    )


def clean_inactive_named_windows_targets(
    patterns: Sequence[str],
    pressured_drives: Sequence[Dict[str, Any]],
    *,
    min_age_hours: float = 1.0,
    process_commands: Optional[Sequence[str]] = None,
    now: Optional[float] = None,
) -> list[Dict[str, Any]]:
    """Reclaim idle ``target-*`` roots without blocking on unrelated builds."""
    if os.name != "nt" or not patterns:
        return []
    pressure = {
        str(item.get("drive") or "").upper(): str(item.get("mode") or "warning")
        for item in pressured_drives
    }
    if not pressure:
        return []
    if process_commands is None:
        process_ok, commands = _windows_process_command_lines()
        if not process_ok:
            return [{
                "name": "windows-rust-named-target",
                "status": "process-probe-failed",
                "scope": "configured-named-target-only",
                "bytes": 0,
            }]
    else:
        commands = [str(value) for value in process_commands]

    now_epoch = time.time() if now is None else now
    cutoff = now_epoch - max(1.0, min_age_hours) * 3600
    scan_seconds = _number_setting(
        "QUOTAPOOL_WINDOWS_TEMP_CLEANUP_MAX_SECONDS",
        DEFAULT_WINDOWS_TEMP_CLEANUP_MAX_SECONDS,
    )
    actions: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for pattern in patterns:
        for raw in glob.glob(str(pattern)):
            candidate = Path(raw)
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            key = os.path.normcase(str(resolved))
            if (
                key in seen
                or SAFE_CLAUDE_TARGET_PATTERN.fullmatch(resolved.name) is None
                or not _approved_windows_worktree_target(resolved)
            ):
                continue
            seen.add(key)
            drive = str(resolved.drive).upper()
            if pressure.get(drive) not in {"critical", "emergency"}:
                continue
            if _path_referenced_by_process(resolved, commands):
                actions.append({
                    "name": "windows-rust-named-target",
                    "drive": drive,
                    "root": str(resolved),
                    "status": "active-protected",
                    "scope": "configured-named-target-only",
                    "bytes": 0,
                })
                continue
            recent = _tree_has_recent_write(
                resolved,
                cutoff,
                time.monotonic() + scan_seconds,
            )
            if recent is not False:
                actions.append({
                    "name": "windows-rust-named-target",
                    "drive": drive,
                    "root": str(resolved),
                    "status": "recent-protected",
                    "scope": "configured-named-target-only",
                    "bytes": 0,
                    "retentionHours": max(1.0, min_age_hours),
                })
                continue
            if process_commands is None:
                process_ok, latest_commands = _windows_process_command_lines()
                if not process_ok or _path_referenced_by_process(resolved, latest_commands):
                    actions.append({
                        "name": "windows-rust-named-target",
                        "drive": drive,
                        "root": str(resolved),
                        "status": "active-protected",
                        "scope": "configured-named-target-only",
                        "bytes": 0,
                    })
                    continue
            quarantine = resolved.parent / (
                f".quotapool-cleaning-{resolved.name}-{os.getpid()}-{time.time_ns()}"
            )
            try:
                resolved.rename(quarantine)
            except OSError:
                actions.append({
                    "name": "windows-rust-named-target",
                    "drive": drive,
                    "root": str(resolved),
                    "status": "active-protected",
                    "scope": "configured-named-target-only",
                    "bytes": 0,
                })
                continue
            result = clean_stale_windows_temp(
                quarantine,
                0.0,
                deadline=time.monotonic() + scan_seconds,
            )
            try:
                quarantine.rmdir()
            except OSError:
                if not resolved.exists():
                    try:
                        quarantine.rename(resolved)
                    except OSError:
                        pass
            result.update({
                "name": "windows-rust-named-target",
                "drive": drive,
                "root": str(resolved),
                "scope": "configured-named-target-only",
                "retentionHours": max(1.0, min_age_hours),
            })
            actions.append(result)
    return actions


def clean_inactive_windows_incremental_caches(
    patterns: Sequence[str],
    pressured_drives: Sequence[Dict[str, Any]],
) -> list[Dict[str, Any]]:
    """Reclaim stale Rust incremental caches from explicitly registered worktrees.

    This deliberately skips every target while any Rust/link build process is
    present.  Warning/predictive maintenance removes only
    ``target/debug/incremental``.  Under critical/emergency pressure an
    explicitly registered, ordinary target may be removed in full after all
    builds have stopped; sources and Git state remain intact.
    """
    if os.name != "nt" or not patterns:
        return []
    pressure = {
        str(item.get("drive") or "").upper(): str(item.get("mode") or "warning")
        for item in pressured_drives
    }
    if not pressure:
        return []
    if _windows_builds_active():
        return [{
            "name": "windows-rust-incremental",
            "drive": drive,
            "status": "build-active",
            "scope": "configured-worktree-incremental-only",
            "bytes": 0,
        } for drive in sorted(pressure)]

    now = time.time()
    deadline = time.monotonic() + _number_setting(
        "QUOTAPOOL_FIXED_CLEANUP_MAX_SECONDS",
        DEFAULT_FIXED_CLEANUP_MAX_SECONDS,
    )
    remaining = int(_number_setting(
        "QUOTAPOOL_FIXED_CLEANUP_MAX_GB",
        DEFAULT_FIXED_CLEANUP_MAX_GB,
    ) * GIB)
    actions: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for pattern in patterns:
        for raw in glob.glob(str(pattern)):
            candidate = Path(raw)
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            key = os.path.normcase(str(resolved))
            if key in seen or not _approved_windows_worktree_target(resolved):
                continue
            seen.add(key)
            drive = str(resolved.drive).upper()
            mode = pressure.get(drive)
            if not mode:
                continue
            full_target = mode in {"emergency", "critical"}
            cleanup_root = resolved if full_target else resolved / "debug" / "incremental"
            if not cleanup_root.is_dir() or _is_reparse_point(cleanup_root):
                continue
            age_hours = 0.0 if full_target else {
                "emergency": 6.0,
                "critical": 24.0,
                "warning": DEFAULT_WINDOWS_INCREMENTAL_MIN_AGE_HOURS,
                "predictive": 168.0,
            }.get(mode, DEFAULT_WINDOWS_INCREMENTAL_MIN_AGE_HOURS)
            try:
                # For warning-level incremental cleanup, unrelated outputs in
                # target/debug must not make a genuinely idle incremental tree
                # look active.  Full-target cleanup remains conservative.
                last_write = cleanup_root.stat().st_mtime
                if full_target:
                    last_write = max(
                        last_write,
                        (resolved / "debug").stat().st_mtime,
                    )
            except OSError:
                continue
            if last_write > now - age_hours * 3600:
                actions.append({
                    "name": "windows-rust-incremental",
                    "drive": drive,
                    "root": str(cleanup_root),
                    "status": "skipped-recent",
                    "scope": (
                        "configured-worktree-target-only"
                        if full_target
                        else "configured-worktree-incremental-only"
                    ),
                    "bytes": 0,
                    "retentionHours": age_hours,
                })
                continue
            if time.monotonic() >= deadline or remaining <= 0:
                break
            result = clean_stale_windows_temp(
                cleanup_root,
                0,
                max_bytes=remaining,
                deadline=deadline,
            )
            result.update({
                "name": (
                    "windows-rust-target"
                    if full_target
                    else "windows-rust-incremental"
                ),
                "drive": drive,
                "root": str(cleanup_root),
                "scope": (
                    "configured-worktree-target-only"
                    if full_target
                    else "configured-worktree-incremental-only"
                ),
                "retentionHours": age_hours,
            })
            actions.append(result)
            remaining -= int(result.get("bytes") or 0)
    return actions


def _valid_reclaim_pattern(value: str) -> bool:
    """Only explicitly configured, reproducible Rust target directories qualify."""
    normalized = value.strip().replace("\\", "/")
    if not normalized.startswith("/") or "/../" in f"/{normalized.strip('/')}/":
        return False
    leaf = normalized.rstrip("/").rsplit("/", 1)[-1]
    return (
        leaf == "target"
        or leaf.startswith(".evox-target")
        or leaf.startswith(".cargo-target")
        or leaf == ".cargo-shared-target"
    )


def _wsl_base_maintenance(
    distribution: str,
    port: int = 0,
    ssh_user: str = "",
    identity: str = "",
) -> Dict[str, Any]:
    command = (
        "journalctl --vacuum-time=14d --vacuum-size=256M >/dev/null 2>&1 || true; "
        "if command -v apt-get >/dev/null 2>&1; then apt-get clean >/dev/null 2>&1 || true; fi; "
        "sync; /usr/sbin/fstrim -av"
    )
    transport = "wsl"
    limited_error = ""
    if port and ssh_user and identity and Path(identity).is_file() and tcp_ready(port):
        result = _ssh_maintenance_capture(
            port, ssh_user, identity, command + "\n", as_root=True, timeout=60
        )
        transport = "ssh"
        if result.returncode != 0:
            limited_error = result.stderr[-500:].strip()
            # Once deletion has restored basic headroom, retry the privileged
            # route. A wedged WSL service can recover without ending SSH work.
            if host_disk_has_headroom():
                direct = run_capture(
                    [
                        _system_executable("wsl.exe"), "-d", distribution,
                        "-u", "root", "--exec", "/bin/sh", "-lc", command,
                    ],
                    timeout=600,
                )
                if direct.returncode == 0:
                    result = direct
                    transport = "wsl"
                    limited_error = ""
            if result.returncode != 0:
                # Cache deletion is still valuable when sudo/root TRIM is not
                # available. Sync safely and defer privileged trimming.
                result = _ssh_maintenance_capture(
                    port, ssh_user, identity, "sync\n", timeout=60
                )
                transport = "ssh-limited"
    else:
        result = run_capture(
            [
                _system_executable("wsl.exe"), "-d", distribution, "-u", "root",
                "--exec", "/bin/sh", "-lc", command,
            ],
            timeout=600,
        )
    return {
        "name": "wsl-safe-maintenance",
        "status": (
            "limited" if result.returncode == 0 and transport == "ssh-limited"
            else "completed" if result.returncode == 0 else "failed"
        ),
        "result": result.returncode,
        "transport": transport,
        "error": limited_error or (result.stderr[-500:].strip() if result.returncode else None),
    }


def _wsl_reclaim_targets(
    distribution: str,
    patterns: Sequence[str],
    port: int = 0,
    ssh_user: str = "",
    identity: str = "",
) -> Dict[str, Any]:
    approved = [value for value in patterns if _valid_reclaim_pattern(value)]
    if not approved:
        return {"name": "wsl-build-cache", "status": "not-configured", "bytes": 0}
    script = r'''
set -u
target_is_active() {
  path=$1
  parent=${path%/*}
  leaf=${path##*/}

  # Protect a cache whenever any process is executing from it or still has an
  # output/object open there. Checking file descriptors is important because a
  # compiler normally runs with the workspace, not target/, as its cwd.
  for link in /proc/[0-9]*/cwd /proc/[0-9]*/exe /proc/[0-9]*/fd/*; do
    used=$(readlink -f "$link" 2>/dev/null || true)
    case "$used" in "$path"|"$path"/*) return 0 ;; esac
  done

  # Cargo may not yet have opened an output file. Protect its declared target
  # directory and the default target/ belonging to its workspace as well.
  for pid in $(
    { pgrep -x cargo; pgrep -x rustc; pgrep -x sccache; } 2>/dev/null |
      sort -u
  ); do
    cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)
    declared=$(
      tr '\0' '\n' <"/proc/$pid/environ" 2>/dev/null |
        sed -n 's/^CARGO_TARGET_DIR=//p' | head -n 1
    )
    if [ -n "$declared" ]; then
      case "$declared" in /*) ;; *) declared="$cwd/$declared" ;; esac
      declared=$(readlink -m "$declared" 2>/dev/null || true)
      [ "$declared" = "$path" ] && return 0
    fi
    command=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)
    case "$command" in *"$path"*) return 0 ;; esac
    if [ "$leaf" = "target" ]; then
      case "$cwd" in
        "$parent"/.claude/worktrees/*|"$parent"/.codex/worktrees/*) ;;
        "$parent"|"$parent"/*) return 0 ;;
      esac
    fi
  done
  return 1
}

total=0
for pattern in "$@"; do
  while IFS= read -r path; do
    [ -d "$path" ] || continue
    [ ! -L "$path" ] || continue
    path=$(readlink -f "$path" 2>/dev/null || true)
    [ -n "$path" ] || continue
    find "$path" -mindepth 1 -print -quit 2>/dev/null | grep -q . || continue
    leaf=${path%/}; leaf=${leaf##*/}
    case "$leaf" in target|.evox-target*|.cargo-target*|.cargo-shared-target) ;; *) continue ;; esac
    if target_is_active "$path"; then
      printf 'SKIP_ACTIVE\t%s\n' "$path"
      continue
    fi
    size=$(du -sb "$path" 2>/dev/null | awk '{print $1}')
    size=${size:-0}
    find "$path" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    total=$((total + size))
    printf 'CLEANED\t%s\t%s\n' "$size" "$path"
  done < <(compgen -G "$pattern" || true)
done
sync
printf 'TOTAL\t%s\n' "$total"
'''
    transport = "wsl"
    if port and ssh_user and identity and Path(identity).is_file() and tcp_ready(port):
        result = _ssh_maintenance_capture(
            port, ssh_user, identity, script, args=approved, timeout=1800
        )
        transport = "ssh"
        if result.returncode not in (0, 20) and host_disk_has_headroom():
            direct = run_capture(
                [
                    _system_executable("wsl.exe"), "-d", distribution, "-u", "root",
                    "--exec", "/bin/bash", "-c", script,
                    "quotapool-maintenance", *approved,
                ],
                timeout=1800,
            )
            if direct.returncode in (0, 20):
                result = direct
                transport = "wsl"
    else:
        result = run_capture(
            [
                _system_executable("wsl.exe"), "-d", distribution, "-u", "root",
                "--exec", "/bin/bash", "-c", script, "quotapool-maintenance", *approved,
            ],
            timeout=1800,
        )
    reclaimed = 0
    cleaned: list[str] = []
    skipped: list[str] = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if fields[0] == "CLEANED" and len(fields) == 3:
            try:
                reclaimed += int(fields[1])
            except ValueError:
                pass
            cleaned.append(fields[2])
        elif fields[0] == "SKIP_ACTIVE" and len(fields) == 2:
            skipped.append(fields[1])
    status = "completed" if result.returncode == 0 else "build-active" if result.returncode == 20 else "failed"
    return {
        "name": "wsl-build-cache",
        "status": status,
        "result": result.returncode,
        "bytes": reclaimed,
        "cleaned": cleaned,
        "skippedActive": skipped,
        "transport": transport,
        "error": result.stderr[-500:].strip() if result.returncode not in (0, 20) else None,
    }


def maintain(
    distribution: str,
    port: int,
    state_path: Optional[str] = None,
    reclaim_paths: Sequence[str] = (),
    ssh_user: str = "",
    identity: str = "",
    windows_reclaim_paths: Sequence[str] = (),
    target_drives: Sequence[str] = (),
    force: bool = False,
) -> int:
    """Keep the Windows host volume usable without interrupting live work."""
    trigger_gb = _number_setting(
        "QUOTAPOOL_DISK_MAINTENANCE_GB", DEFAULT_MAINTENANCE_TRIGGER_GB
    )
    reclaim_gb = _number_setting(
        "QUOTAPOOL_DISK_RECLAIM_GB", DEFAULT_RECLAIM_TRIGGER_GB
    )
    minimum_gb = _number_setting("QUOTAPOOL_WSL_MIN_FREE_GB", DEFAULT_MIN_FREE_GB)
    previous_state = read_maintenance_state(state_path)
    pending_request = read_manual_maintenance_request(state_path)
    requested_drives = {
        str(value or "").strip().upper() for value in target_drives
        if re.fullmatch(r"[A-Za-z]:", str(value or "").strip())
    }
    if not requested_drives and pending_request:
        requested_drives = {str(pending_request["drive"]).upper()}
        force = bool(pending_request.get("force", True))
    try:
        before = host_disk_usage()
    except OSError as exc:
        _write_maintenance_state({
            "at": datetime.now(timezone.utc).isoformat(),
            "status": "probe-failed",
            "error": str(exc),
        }, state_path)
        return 1

    now_epoch = time.time()
    drive_snapshots = _enrich_fixed_disk_snapshots(
        fixed_disk_snapshots() if os.name == "nt" else [],
        previous_state,
        now_epoch=now_epoch,
    )
    pressured_drives = []
    for raw_item in drive_snapshots:
        drive = str(raw_item.get("drive") or "").upper()
        if requested_drives and drive not in requested_drives:
            continue
        item = dict(raw_item)
        if force:
            item["nextEligibleEpoch"] = 0.0
            if str(item.get("mode") or "observe") == "observe":
                item["mode"] = "warning"
                item["reason"] = "用户从本地面板发起主动安全维护"
        if str(item.get("mode") or "observe") != "observe":
            pressured_drives.append(item)
    c_selected = not requested_drives or "C:" in requested_drives
    c_snapshot = next(
        (item for item in drive_snapshots if str(item.get("drive") or "").upper() == "C:"),
        None,
    )
    c_next_eligible = float((c_snapshot or {}).get("nextEligibleEpoch") or 0)
    previous_c = next(
        (
            item for item in previous_state.get("drives", [])
            if isinstance(item, dict) and str(item.get("drive") or "").upper() == "C:"
        ),
        {},
    )
    previous_c_free = int(
        previous_c.get("freeBytesAfter") or previous_c.get("freeBytesBefore") or before.free
    )
    # Honor no-progress backoff for the expensive C: scans.  A meaningful new
    # loss of space or an emergency state re-arms maintenance immediately.
    c_pressure_worsened = (
        str((c_snapshot or {}).get("mode") or "observe") == "emergency"
        or previous_c_free - int(before.free) >= 2 * GIB
    )
    c_in_backoff = (
        not force
        and c_next_eligible > now_epoch
        and not c_pressure_worsened
    )
    c_maintenance_needed = c_selected and (
        before.free < trigger_gb * 1024**3 or force
    ) and not c_in_backoff
    state: Dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "status": "healthy",
        "freeBytesBefore": before.free,
        "freeBytesAfter": before.free,
        "thresholds": {
            "maintenanceBytes": int(trigger_gb * 1024**3),
            "reclaimBytes": int(reclaim_gb * 1024**3),
            "minimumBytes": int(minimum_gb * 1024**3),
        },
        "actions": [],
        "manual": ({
            "requestId": pending_request.get("requestId"),
            "drive": next(iter(requested_drives)),
            "requestedAt": pending_request.get("requestedAt"),
            "status": "running",
        } if requested_drives else previous_state.get("manual")),
        "drives": [
            {
                "drive": str(item.get("drive") or ""),
                "level": str(item.get("level") or "unknown"),
                "effectiveLevel": str(item.get("effectiveLevel") or "unknown"),
                "mode": str(item.get("mode") or "observe"),
                "reason": str(item.get("reason") or ""),
                "freeBytesBefore": int(getattr(item.get("usage"), "free", 0) or 0),
                "criticalBytes": int(item.get("criticalBytes") or 0),
                "warningBytes": int(item.get("warningBytes") or 0),
                "recoveryBytes": int(item.get("recoveryBytes") or 0),
                "consumptionBytesPerHour": int(item.get("consumptionBytesPerHour") or 0),
                "estimatedMinutesToCritical": item.get("estimatedMinutesToCritical"),
                "samples": item.get("samples") or [],
                "nextEligibleEpoch": float(item.get("nextEligibleEpoch") or 0),
                "noProgressCount": int(item.get("noProgressCount") or 0),
            }
            for item in drive_snapshots
        ],
    }
    if not c_maintenance_needed and not pressured_drives:
        _write_maintenance_state(state, state_path)
        return 0

    state["status"] = "maintaining"
    # Publish the transition before a large cache tree is scanned so the
    # dashboard never looks idle while maintenance is actively reclaiming.
    _write_maintenance_state(state, state_path)
    local_appdata = os.environ.get("LOCALAPPDATA")
    temp_value = (
        str(Path(local_appdata) / "Temp")
        if local_appdata
        else (os.environ.get("TEMP") or os.environ.get("TMP"))
    )
    if os.name == "nt" and c_maintenance_needed:
        windows_temp = Path(os.environ.get("WINDIR", "C:/Windows")) / "Temp"
        state["actions"].append(clean_inactive_claude_build_targets(windows_temp))
    if c_maintenance_needed and temp_value:
        state["actions"].append(clean_stale_windows_temp(
            Path(temp_value),
            _number_setting("QUOTAPOOL_TEMP_MAX_AGE_DAYS", DEFAULT_TEMP_MAX_AGE_DAYS),
            deadline=(
                time.monotonic()
                + _number_setting(
                    "QUOTAPOOL_WINDOWS_TEMP_CLEANUP_MAX_SECONDS",
                    DEFAULT_WINDOWS_TEMP_CLEANUP_MAX_SECONDS,
                )
            ),
        ))
    if os.name == "nt" and c_maintenance_needed:
        windows_temp = Path(os.environ.get("WINDIR", "C:/Windows")) / "Temp"
        if not temp_value or windows_temp.resolve() != Path(temp_value).resolve():
            system_temp = clean_stale_windows_temp(
                windows_temp,
                _number_setting(
                    "QUOTAPOOL_SYSTEM_TEMP_MAX_AGE_DAYS",
                    DEFAULT_TEMP_MAX_AGE_DAYS,
                ),
                deadline=(
                    time.monotonic()
                    + _number_setting(
                        "QUOTAPOOL_WINDOWS_TEMP_CLEANUP_MAX_SECONDS",
                        DEFAULT_WINDOWS_TEMP_CLEANUP_MAX_SECONDS,
                    )
                ),
            )
            system_temp["name"] = "windows-system-temp"
            state["actions"].append(system_temp)
        if local_appdata:
            state["actions"].extend(
                clean_safe_windows_caches(Path(local_appdata), windows_temp)
            )
    if os.name == "nt" and pressured_drives:
        if force:
            fixed_actions = clean_safe_fixed_drive_caches(
                pressured_drives,
                ignore_backoff=True,
            )
        else:
            fixed_actions = clean_safe_fixed_drive_caches(pressured_drives)
        state["actions"].extend(fixed_actions)
        incremental_drives = [
            item for item in pressured_drives
            if force
            or float(item.get("nextEligibleEpoch") or 0) <= now_epoch
            or str(item.get("mode") or "observe") == "emergency"
        ]
        state["actions"].extend(clean_inactive_named_windows_targets(
            windows_reclaim_paths,
            incremental_drives,
        ))
        state["actions"].extend(clean_inactive_windows_incremental_caches(
            windows_reclaim_paths,
            incremental_drives,
        ))

    # Do not boot a stopped WSL instance when Windows is already critically full.
    if c_maintenance_needed and (tcp_ready(port) or before.free >= minimum_gb * 1024**3):
        state["actions"].append(
            _wsl_base_maintenance(distribution, port, ssh_user, identity)
        )
    elif c_maintenance_needed:
        state["actions"].append({"name": "wsl-safe-maintenance", "status": "blocked-low-disk"})

    current = host_disk_usage()
    active_clients = established_ssh_clients(port)
    state["activeSshClients"] = active_clients
    if c_maintenance_needed and current.free < reclaim_gb * 1024**3:
        # Reclaim remains safe with connected SSH clients: each configured
        # target is checked against build environments, workspaces, executable
        # paths and open file descriptors. Active targets are kept while stale
        # targets from other worktrees can still be recovered.
        reclaimed = _wsl_reclaim_targets(
            distribution, reclaim_paths, port, ssh_user, identity
        )
        state["actions"].append(reclaimed)
        # Discard freed VHDX blocks immediately. This is safe with connected
        # SSH clients and is essential when Windows is too full for wsl.exe.
        if reclaimed.get("bytes", 0) or active_clients == 0:
            state["actions"].append(
                _wsl_base_maintenance(distribution, port, ssh_user, identity)
            )

    after = host_disk_usage()
    after_drives = _enrich_fixed_disk_snapshots(
        fixed_disk_snapshots() if os.name == "nt" else [],
        {"drives": state["drives"]},
        now_epoch=time.time(),
    )
    after_by_drive = {str(item.get("drive") or ""): item for item in after_drives}
    action_bytes: Dict[str, int] = {}
    action_statuses: Dict[str, set[str]] = {}
    for action in state["actions"]:
        if not isinstance(action, dict):
            continue
        drive = str(action.get("drive") or "").upper()
        if not drive:
            continue
        action_bytes[drive] = action_bytes.get(drive, 0) + max(0, int(action.get("bytes") or 0))
        action_statuses.setdefault(drive, set()).add(str(action.get("status") or ""))
    old_by_drive = {
        str(item.get("drive") or "").upper(): item
        for item in previous_state.get("drives", [])
        if isinstance(item, dict)
    }
    next_epochs: list[float] = []
    for item in state["drives"]:
        latest = after_by_drive.get(str(item.get("drive") or ""))
        if latest:
            item["freeBytesAfter"] = int(getattr(latest.get("usage"), "free", 0) or 0)
            item["levelAfter"] = str(latest.get("level") or "unknown")
            item["effectiveLevelAfter"] = str(latest.get("effectiveLevel") or "unknown")
            item["modeAfter"] = str(latest.get("mode") or "observe")
            item["reason"] = str(latest.get("reason") or item.get("reason") or "")
            item["samples"] = latest.get("samples") or item.get("samples") or []
            item["consumptionBytesPerHour"] = int(latest.get("consumptionBytesPerHour") or 0)
            item["estimatedMinutesToCritical"] = latest.get("estimatedMinutesToCritical")
            item["netFreeChangeBytes"] = (
                item["freeBytesAfter"] - int(item.get("freeBytesBefore") or 0)
            )
            drive = str(item.get("drive") or "").upper()
            reclaimed = action_bytes.get(drive, 0)
            item["reclaimedBytes"] = reclaimed
            previous_drive = old_by_drive.get(drive, {})
            current_statuses = action_statuses.get(drive, set())
            if item["effectiveLevelAfter"] == "healthy":
                next_epoch = time.time() + DEFAULT_FIXED_CACHE_COOLDOWN_MINUTES * 60
                no_progress = 0
                action_reason = "已恢复健康，进入冷却观察"
            elif "backoff" in current_statuses:
                next_epoch = max(
                    float(previous_drive.get("nextEligibleEpoch") or 0),
                    time.time() + 60,
                )
                no_progress = int(previous_drive.get("noProgressCount") or 0)
                action_reason = "上次无有效回收，正在退避，避免反复扫描"
            elif current_statuses & {"time-budget-reached", "byte-budget-reached"}:
                # A bounded pass that still had work left is not "no progress".
                # Retry on the normal two-minute cadence instead of applying an
                # exponential backoff that can leave a pressured disk stranded.
                next_epoch = time.time() + 120
                no_progress = 0
                action_reason = "本轮达到安全预算，2 分钟后从剩余缓存继续"
            elif reclaimed >= MIN_PROGRESS_BYTES:
                next_epoch = time.time() + 120
                no_progress = 0
                action_reason = "已回收缓存，若压力仍在将继续下一轮"
            else:
                no_progress = int(previous_drive.get("noProgressCount") or 0) + 1
                backoff_minutes = min(60, 5 * (2 ** min(no_progress - 1, 4)))
                next_epoch = time.time() + backoff_minutes * 60
                action_reason = f"未发现足够旧缓存，退避 {backoff_minutes} 分钟"
            item["nextEligibleEpoch"] = next_epoch
            item["nextActionAt"] = datetime.fromtimestamp(
                next_epoch, timezone.utc
            ).isoformat()
            item["noProgressCount"] = no_progress
            item["actionReason"] = action_reason
            next_epochs.append(next_epoch)
    state["freeBytesAfter"] = after.free
    state["netFreeChangeBytes"] = after.free - before.free
    state["reclaimedBytes"] = sum(
        max(0, int(action.get("bytes") or 0))
        for action in state["actions"]
        if isinstance(action, dict)
    )
    if next_epochs:
        state["nextActionAt"] = datetime.fromtimestamp(min(next_epochs), timezone.utc).isoformat()
    critical_drives = [
        item for item in state["drives"]
        if item.get("effectiveLevelAfter", item.get("effectiveLevel")) == "critical"
    ]
    warning_drives = [
        item for item in state["drives"]
        if item.get("effectiveLevelAfter", item.get("effectiveLevel")) == "warning"
    ]
    if after.free < minimum_gb * 1024**3 or critical_drives:
        state["status"] = "critical"
        result = LOW_DISK_EXIT
    elif after.free < trigger_gb * 1024**3 or warning_drives:
        state["status"] = "warning"
        result = 0
    else:
        state["status"] = "recovered"
        result = 0
    _write_maintenance_state(state, state_path)
    if requested_drives:
        state["manual"]["status"] = "completed"
        state["manual"]["completedAt"] = datetime.now(timezone.utc).isoformat()
        _write_maintenance_state(state, state_path)
        request_path = manual_maintenance_request_path(state_path)
        latest_request = read_manual_maintenance_request(state_path)
        if (
            pending_request
            and latest_request.get("requestId") == pending_request.get("requestId")
        ):
            try:
                request_path.unlink()
            except FileNotFoundError:
                pass
    return result


def keepalive(distribution: str) -> int:
    """Keep WSL resident while remaining completely windowless on Windows."""
    if not host_disk_has_headroom():
        return LOW_DISK_EXIT
    return run_hidden(
        [
            _system_executable("wsl.exe"),
            "-d",
            distribution,
            "--exec",
            "/usr/bin/sleep",
            "infinity",
        ]
    )


def trim(distribution: str, port: int) -> int:
    """Return discarded WSL blocks to Windows without flashing a console."""
    # TRIM is useful under disk pressure when WSL is already reachable, but do
    # not boot a stopped distro from a critically full host volume.
    if not host_disk_has_headroom() and not tcp_ready(port):
        return LOW_DISK_EXIT
    return run_hidden(
        [
            _system_executable("wsl.exe"),
            "-d",
            distribution,
            "-u",
            "root",
            "--exec",
            "/bin/sh",
            "-lc",
            "sync; /usr/sbin/fstrim -av",
        ],
        timeout=600,
    )


def watchdog(
    distribution: str,
    port: int,
    keepalive_task: str,
    ssh_user: str = "",
    identity: str = "",
) -> int:
    """Start the resident task and repair SSH without stopping the distribution."""
    def connection_ready() -> bool:
        if ssh_user and identity:
            return ssh_authenticated_ready(port, ssh_user, identity)
        return ssh_banner_ready(port)

    def wait_connection_ready(seconds: float) -> bool:
        if not (ssh_user and identity):
            return wait_ssh_banner(port, seconds)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if connection_ready():
                return True
            time.sleep(0.4)
        return connection_ready()

    # A healthy SSH listener proves WSL is already resident. Avoid re-triggering
    # the scheduled task on every one-minute probe.
    if connection_ready():
        return 0

    # Starting WSL while its host volume is critically full can wedge both the
    # distro and HCS. Leave it stopped until space has been recovered.
    if not host_disk_has_headroom():
        return LOW_DISK_EXIT

    # Starting an already-running task is harmless. schtasks.exe is used instead
    # of PowerShell so the once-per-minute probe cannot briefly flash a console.
    run_hidden(
        [
            _system_executable("schtasks.exe"),
            "/Run",
            "/TN",
            keepalive_task,
        ],
        timeout=10,
    )

    # A listener that accepts TCP but cannot produce a banner may still own
    # live Claude SSH sessions. Report it as unhealthy, but never restart the
    # service automatically and risk interrupting those sessions.
    if tcp_ready(port):
        active = established_ssh_clients(port)
        if active is None or active > 0:
            return 2

    # A stopped distribution can need a few seconds after keepalive starts.
    if wait_connection_ready(6):
        return 0
    if tcp_ready(port):
        active = established_ssh_clients(port)
        if active is None or active > 0:
            return 2

    result = run_hidden(
        [
            _system_executable("wsl.exe"),
            "-d",
            distribution,
            "-u",
            "root",
            "--exec",
            "/usr/bin/systemctl",
            "restart",
            "ssh",
        ],
        timeout=15,
    )
    if result != 0:
        return result
    return 0 if wait_connection_ready(6) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    resident = subparsers.add_parser("keepalive")
    resident.add_argument("--distribution", default="Ubuntu-22.04")

    reclaim = subparsers.add_parser("trim")
    reclaim.add_argument("--distribution", default="Ubuntu-22.04")
    reclaim.add_argument("--port", type=int, default=2222)

    maintenance = subparsers.add_parser("maintain")
    maintenance.add_argument("--distribution", default="Ubuntu-22.04")
    maintenance.add_argument("--port", type=int, default=2222)
    maintenance.add_argument("--state")
    maintenance.add_argument("--reclaim-path", action="append", default=[])
    maintenance.add_argument("--windows-reclaim-path", action="append", default=[])
    maintenance.add_argument("--ssh-user", default="")
    maintenance.add_argument("--identity", default="")
    maintenance.add_argument("--drive", action="append", default=[])
    maintenance.add_argument("--force", action="store_true")

    monitor = subparsers.add_parser("watchdog")
    monitor.add_argument("--distribution", default="Ubuntu-22.04")
    monitor.add_argument("--port", type=int, default=2222)
    monitor.add_argument(
        "--keepalive-task", default="Claude WSL SSH Keepalive"
    )
    monitor.add_argument("--ssh-user", default="")
    monitor.add_argument("--identity", default="")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "keepalive":
        return keepalive(args.distribution)
    if args.command == "trim":
        return trim(args.distribution, args.port)
    if args.command == "maintain":
        return maintain(
            args.distribution,
            args.port,
            args.state,
            args.reclaim_path,
            args.ssh_user,
            args.identity,
            args.windows_reclaim_path,
            args.drive,
            args.force,
        )
    return watchdog(
        args.distribution,
        args.port,
        args.keepalive_task,
        args.ssh_user,
        args.identity,
    )


if __name__ == "__main__":
    raise SystemExit(main())
