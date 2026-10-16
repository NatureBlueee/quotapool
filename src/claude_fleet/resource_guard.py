"""Conservative host-pressure guard for new deferrable Fleet workloads.

The guard never terminates a process.  It records a small, credential-free
host-pressure snapshot and delays only workload classes that Fleet already
defines as deferrable.  Interactive work, subagents and forks remain admitted
so an in-progress user task cannot be stranded by resource management.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Union

from .control import DEFERRABLE_WORKLOADS, canonical_workload_class
from .disk_policy import append_disk_sample, assess_disk


DEFAULT_RESOURCE_GUARD = {
    "enabled": True,
    "freshness_seconds": 75,
    "poll_seconds": 5,
    "max_wait_seconds": 900,
}


def guard_config(runtime: Mapping[str, Any]) -> Dict[str, Any]:
    configured = runtime.get("resource_guard")
    configured = configured if isinstance(configured, dict) else {}
    value = dict(DEFAULT_RESOURCE_GUARD)
    value.update({key: configured[key] for key in value if key in configured})
    value["enabled"] = bool(value["enabled"])
    for key in ("freshness_seconds", "poll_seconds", "max_wait_seconds"):
        try:
            value[key] = max(1, int(value[key]))
        except (TypeError, ValueError):
            value[key] = DEFAULT_RESOURCE_GUARD[key]
    return value


def guard_state_path(runtime: Mapping[str, Any]) -> Path:
    configured = str(runtime.get("resource_guard_state") or "").strip()
    if configured:
        return Path(configured).expanduser()
    db_path = str(runtime.get("db_path") or "").strip()
    if db_path:
        return Path(db_path).expanduser().parent / "resource-guard.json"
    state_dir = str(runtime.get("state_dir") or "").strip()
    if state_dir:
        return Path(state_dir).expanduser() / "resource-guard.json"
    return Path.cwd() / "resource-guard.json"


def read_guard_state(runtime: Mapping[str, Any]) -> Dict[str, Any]:
    path = guard_state_path(runtime)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_guard_state(runtime: Mapping[str, Any], value: Mapping[str, Any]) -> None:
    path = guard_state_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text(
        json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _disk_level(disk: Mapping[str, Any]) -> str:
    if not disk.get("available"):
        return "unknown"
    try:
        total = int(disk.get("totalBytes") or 0)
        free = int(disk.get("freeBytes") or 0)
        used = float(disk.get("usedPercent") or 0)
    except (TypeError, ValueError):
        return "unknown"
    return str(assess_disk(total, free, used_percent=used).get("level") or "unknown")


def record_guard_sample(
    runtime: Mapping[str, Any],
    resources: Mapping[str, Any],
    disk: Optional[Union[Mapping[str, Any], Sequence[Mapping[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Persist only the pressure summary needed by launch admission."""
    config = guard_config(runtime)
    disks = list(disk) if isinstance(disk, (list, tuple)) else [disk or {}]
    previous = read_guard_state(runtime)
    cpu_level_raw = str((resources.get("cpu") or {}).get("level") or "unknown")
    memory_level = str((resources.get("memory") or {}).get("level") or "unknown")
    previous_streaks = previous.get("pressureStreaks")
    previous_streaks = previous_streaks if isinstance(previous_streaks, dict) else {}
    cpu_critical_streak = (
        int(previous_streaks.get("cpuCritical") or 0) + 1
        if cpu_level_raw == "critical" else 0
    )
    # CPU is bursty: require two consecutive critical samples before deferring
    # background work. Memory and disk pressure remain immediate safety signals.
    cpu_level = (
        "warning" if cpu_level_raw == "critical" and cpu_critical_streak < 2
        else cpu_level_raw
    )
    now_epoch = time.time()
    previous_drives = {
        str(item.get("drive") or ""): item
        for item in previous.get("drives", [])
        if isinstance(item, Mapping)
    }
    disk_levels: list[Dict[str, Any]] = []
    for item in disks:
        if not isinstance(item, Mapping):
            continue
        drive = str(item.get("drive") or item.get("path") or "磁盘").rstrip("\\/")
        if not item.get("available"):
            disk_levels.append({"drive": drive, "level": "unknown", "effectiveLevel": "unknown"})
            continue
        try:
            total = int(item.get("totalBytes") or 0)
            free = int(item.get("freeBytes") or 0)
            used = float(item.get("usedPercent") or 0)
        except (TypeError, ValueError):
            disk_levels.append({"drive": drive, "level": "unknown", "effectiveLevel": "unknown"})
            continue
        old = previous_drives.get(drive, {})
        samples = append_disk_sample(old.get("samples"), free, now_epoch=now_epoch)
        assessment = assess_disk(
            total,
            free,
            used_percent=used,
            # Disk maintenance already owns hysteresis.  Its pressure level is
            # fresher than an older guard-only warning after a one-off move.
            previous_level=str(
                item.get("pressureLevel") or old.get("level") or "unknown"
            ),
            samples=samples,
        )
        disk_levels.append({
            "drive": drive,
            "level": assessment["level"],
            "effectiveLevel": assessment["effectiveLevel"],
            "mode": assessment["mode"],
            "reason": assessment["reason"],
            "freeBytes": free,
            "warningBytes": assessment["warningBytes"],
            "criticalBytes": assessment["criticalBytes"],
            "recoveryBytes": assessment["recoveryBytes"],
            "consumptionBytesPerHour": assessment["consumptionBytesPerHour"],
            "estimatedMinutesToCritical": assessment["estimatedMinutesToCritical"],
            "samples": samples,
        })
    rank = {"unknown": -1, "healthy": 0, "warning": 1, "critical": 2}
    disk_level = max(
        (item.get("effectiveLevel", item["level"]) for item in disk_levels),
        key=lambda value: rank.get(value, -1),
        default="unknown",
    )
    critical_disks = [
        item["drive"] for item in disk_levels
        if item.get("effectiveLevel", item["level"]) == "critical"
    ]
    warning_disks = [
        item["drive"] for item in disk_levels
        if item.get("effectiveLevel", item["level"]) == "warning"
    ]
    critical = [
        name for name, level in (
            ("CPU", cpu_level),
            ("内存", memory_level),
            (f"磁盘({', '.join(critical_disks)})" if critical_disks else "磁盘", disk_level),
        ) if level == "critical"
    ]
    warning = [
        name for name, level in (
            ("CPU", cpu_level),
            ("内存", memory_level),
            (f"磁盘({', '.join(warning_disks)})" if warning_disks else "磁盘", disk_level),
        ) if level == "warning"
    ]
    unknown_resources = cpu_level not in ("healthy", "warning", "critical") or memory_level not in ("healthy", "warning", "critical")
    defer = bool(config["enabled"] and (critical or unknown_resources))
    now = datetime.now(timezone.utc).isoformat()
    status = "protecting" if defer else "watching" if warning else "healthy"
    reason = (
        f"{'、'.join(critical)}达到临界值，新的可延期后台任务正在排队。"
        if defer and critical else
        "资源采样不完整，暂缓新的可延期后台任务，等待有效采样；当前任务不受影响。"
        if defer else
        f"{'、'.join(warning)}接近阈值，继续观察，不影响当前任务。"
        if warning else
        "资源余量正常，后台任务可以正常启动。"
    )
    previous_defer = bool(previous.get("deferNewBackground"))
    value = {
        "version": 2,
        "enabled": config["enabled"],
        "status": status,
        "sampledAt": now,
        "sampledEpoch": time.time(),
        "deferNewBackground": defer,
        "reason": reason,
        "levels": {
            "cpu": cpu_level,
            "memory": memory_level,
            "disk": disk_level,
        },
        "rawLevels": {
            "cpu": cpu_level_raw,
            "memory": memory_level,
            "disk": disk_level,
        },
        "pressureStreaks": {"cpuCritical": cpu_critical_streak},
        "drives": disk_levels,
        "activeTasksProtected": True,
        "managedWorkloads": sorted(DEFERRABLE_WORKLOADS),
        "lastTransitionAt": (
            now if defer != previous_defer else previous.get("lastTransitionAt") or now
        ),
        "lastAction": (
            "已暂停新增可延期后台任务" if defer else
            "已恢复后台任务准入" if previous_defer else
            "持续监测"
        ),
    }
    _write_guard_state(runtime, value)
    return value


def admission_decision(
    runtime: Mapping[str, Any],
    workload_class: Optional[str],
    *,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    config = guard_config(runtime)
    workload = canonical_workload_class(workload_class or "interactive")
    if not config["enabled"]:
        return {"allowed": True, "managed": False, "reason": "资源守护未启用"}
    if workload not in DEFERRABLE_WORKLOADS:
        return {
            "allowed": True,
            "managed": False,
            "reason": "交互与在途任务受保护",
        }
    state = read_guard_state(runtime)
    sampled = state.get("sampledEpoch")
    try:
        age = (time.time() if now is None else now) - float(sampled)
    except (TypeError, ValueError):
        age = float("inf")
    if age > config["freshness_seconds"] or age < -5:
        return {
            "allowed": False,
            "managed": True,
            "stale": True,
            "retryAfterSeconds": config["poll_seconds"],
            "reason": "资源样本缺失或已过期，暂缓新后台任务，当前交互不受影响",
        }
    # Older running samplers may still write healthy for unknown measurements.
    # Validate at admission too, rather than trusting their boolean alone.
    levels = state.get("levels") or {}
    if any(levels.get(key) not in ("healthy", "warning", "critical") for key in ("cpu", "memory")):
        return {"allowed": False, "managed": True,
                "retryAfterSeconds": config["poll_seconds"],
                "reason": "CPU 或内存采样未知，暂缓新的后台任务"}
    if state.get("deferNewBackground"):
        return {
            "allowed": False,
            "managed": True,
            "retryAfterSeconds": config["poll_seconds"],
            "reason": str(state.get("reason") or "系统资源压力较高"),
        }
    return {"allowed": True, "managed": True, "reason": "资源余量允许启动"}


def wait_for_admission(
    runtime: Mapping[str, Any],
    workload_class: Optional[str],
) -> Dict[str, Any]:
    """Wait for a deferrable launch; never waits for interactive work."""
    config = guard_config(runtime)
    started = time.monotonic()
    announced = False
    while True:
        decision = admission_decision(runtime, workload_class)
        if decision.get("allowed"):
            decision["waitedSeconds"] = round(time.monotonic() - started, 1)
            return decision
        if not announced:
            print(
                "claude-fleet: 系统资源压力较高，新的可延期后台任务正在排队；"
                "当前交互任务不受影响。",
                file=os.sys.stderr,
                flush=True,
            )
            announced = True
        if time.monotonic() - started >= config["max_wait_seconds"]:
            raise RuntimeError(
                "resource guard timed out waiting to start a deferrable workload; "
                "current interactive tasks remain protected"
            )
        time.sleep(config["poll_seconds"])
