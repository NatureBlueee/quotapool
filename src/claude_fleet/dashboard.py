"""Loopback-only dashboard for the Windows Claude Desktop adapter.

The dashboard deliberately exposes only redacted runtime state. OAuth tokens
stay in ``teamclaude.json`` and are never sent to the browser. Mutating actions
delegate to TeamClaude's own CLI so its atomic config writes and live reload
semantics remain the source of truth.
"""

from __future__ import annotations

import json
import ntpath
import os
import re
import argparse
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from .accounts import set_mode
from .db import FleetDB
from .desktop import (
    hidden_subprocess_kwargs,
    launch_claude_desktop,
    service_status,
    start_services,
)
from .launcher import dataplane_endpoint, load_runtime
from . import lark_integration
from .automations import Automations
from .resource_guard import read_guard_state, record_guard_sample
from .worktree_guard import read_worktree_state
from .wsl_guard import (
    read_maintenance_state,
    read_manual_maintenance_request,
    request_manual_maintenance,
)


DEFAULT_DASHBOARD_HOST = "127.0.0.1"
DEFAULT_DASHBOARD_PORT = 17892
_ACCOUNT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._@()\-]{0,99}$")
_ACTION_HEADER = "QuotapoolDashboard"
_CLAUDE_DESKTOP_APP_ID_FALLBACK = "Claude_pzs8sxrjxfjjc!Claude"
_DESKTOP_CACHE_LOCK = threading.Lock()
_DESKTOP_INFO_CACHE: tuple[float, Dict[str, Any]] = (0.0, {})
_DESKTOP_STATUS_CACHE: tuple[float, Dict[str, Any]] = (0.0, {})
_WINDOWS_HOST_CACHE: tuple[float, Dict[str, Any]] = (0.0, {})
_WINDOWS_HOST_REFRESHING = False
_RESOURCE_SAMPLE_LOCK = threading.Lock()
_RESOURCE_CPU_SAMPLE: tuple[float, Dict[int, float]] = (0.0, {})


_RESOURCE_CATEGORY_LABELS = {
    "quotapool": "QuotaPool 服务",
    "claude-code": "Claude Code 会话",
    "claude-desktop": "Claude Desktop",
    "build": "构建与编译",
    "runtime": "Node / Python 运行时",
    "wsl": "WSL",
    "apps": "开发与桌面应用",
    "other": "系统与其他进程",
}


def _json_file(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _local_json(
    url: str,
    *,
    method: str = "GET",
    timeout: float = 4.0,
) -> Dict[str, Any]:
    request = urllib.request.Request(url, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
            return value if isinstance(value, dict) else {"value": value}
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"error": str(exc)}


def _safe_accounts(team: Mapping[str, Any]) -> list[Dict[str, Any]]:
    result: list[Dict[str, Any]] = []
    for raw in team.get("accounts", []) if isinstance(team.get("accounts"), list) else []:
        if not isinstance(raw, dict):
            continue
        # Never include accessToken, refreshToken, apiKey, or arbitrary config
        # fields. The browser only needs operator-safe identity metadata.
        result.append({
            "name": str(raw.get("name") or ""),
            "type": str(raw.get("type") or "oauth"),
            "disabled": bool(raw.get("disabled")),
            "identityStatus": str(raw.get("identityStatus") or "unknown"),
            "expiresAt": raw.get("expiresAt"),
            "priority": raw.get("priority", 0),
        })
    return result


def _iso_time(value: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _short_id(value: Any, length: int = 12) -> str:
    text = str(value or "")
    return text if len(text) <= length else f"{text[:length]}…"


def _decode_windows_output(payload: bytes) -> str:
    """Decode native Windows command output, including WSL's UTF-16 stream."""
    if not payload:
        return ""
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in payload[:32]:
        try:
            return payload.decode("utf-16").lstrip("\ufeff")
        except UnicodeDecodeError:
            pass
    return payload.decode("utf-8", errors="replace")


def _parse_wsl_distributions(output: str) -> list[Dict[str, Any]]:
    """Parse ``wsl.exe --list --verbose`` without depending on UI language."""
    rows: list[Dict[str, Any]] = []
    for raw_line in output.splitlines():
        line = raw_line.replace("\x00", "").strip()
        if not line:
            continue
        default = line.startswith("*")
        if default:
            line = line[1:].strip()
        match = re.match(r"^(.*?)\s{2,}(Running|Stopped|运行中|已停止)\s+(\d+)\s*$", line, re.I)
        if not match:
            continue
        state_raw = match.group(2).casefold()
        state = "running" if state_raw in {"running", "运行中"} else "stopped"
        rows.append({
            "name": match.group(1).strip(),
            "state": state,
            "version": int(match.group(3)),
            "default": default,
        })
    return rows


def _disk_snapshot(path: Path) -> Dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return {"available": False, "error": str(exc)}
    percent = round((usage.used / usage.total) * 100, 1) if usage.total else 0.0
    return {
        "available": True,
        "path": str(path),
        "totalBytes": usage.total,
        "usedBytes": usage.used,
        "freeBytes": usage.free,
        "usedPercent": percent,
    }


def _tcp_reachable(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _wsl_disk_snapshot(wsl: str, distro: str) -> Dict[str, Any]:
    try:
        result = subprocess.run(
            [wsl, "-d", distro, "--exec", "/bin/df", "-B1", "--output=size,used,avail,pcent", "/"],
            capture_output=True,
            timeout=4,
            check=False,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "error": str(exc)}
    lines = [line.strip() for line in _decode_windows_output(result.stdout).splitlines() if line.strip()]
    if result.returncode != 0 or len(lines) < 2:
        return {"available": False, "error": "无法读取 WSL 磁盘空间"}
    values = re.split(r"\s+", lines[-1])
    if len(values) != 4:
        return {"available": False, "error": "WSL 磁盘数据格式异常"}
    try:
        total, used, free = (int(values[index]) for index in range(3))
        percent = float(values[3].rstrip("%"))
    except ValueError:
        return {"available": False, "error": "WSL 磁盘数据格式异常"}
    return {
        "available": True,
        "path": "/",
        "totalBytes": total,
        "usedBytes": used,
        "freeBytes": free,
        "usedPercent": percent,
    }


def _task_states() -> Dict[str, Any]:
    powershell = shutil.which("powershell.exe")
    if not powershell:
        return {}
    script = (
        "$names=@('QuotaPool Disk Maintenance','QuotaPool Worktree Maintenance','QuotaPool Tray Monitor');"
        "$rows=foreach($name in $names){$task=Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue;"
        "if($task){$info=Get-ScheduledTaskInfo -TaskName $name;"
        "[pscustomobject]@{Name=$name;State=[string]$task.State;LastTaskResult=$info.LastTaskResult;"
        "LastRunTime=[string]$info.LastRunTime;NextRunTime=[string]$info.NextRunTime}}};"
        "$rows|ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            **hidden_subprocess_kwargs(),
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {}
        raw = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}
    rows = raw if isinstance(raw, list) else [raw]
    return {
        str(row.get("Name")): {
            "installed": True,
            "state": str(row.get("State") or "Unknown").casefold(),
            "lastTaskResult": row.get("LastTaskResult"),
            "lastRunTime": row.get("LastRunTime"),
            "nextRunTime": row.get("NextRunTime"),
        }
        for row in rows
        if isinstance(row, dict) and row.get("Name")
    }


def _desktop_service_records(runtime: Mapping[str, Any]) -> Dict[str, Any]:
    configured = str(runtime.get("desktop_pid_file") or "").strip()
    if configured:
        path = Path(configured).expanduser()
    elif str(runtime.get("db_path") or "").strip():
        path = Path(str(runtime["db_path"])).parent / "desktop-services.json"
    elif str(runtime.get("state_dir") or "").strip():
        path = Path(str(runtime["state_dir"])) / "desktop-services.json"
    else:
        path = Path.cwd() / "desktop-services.json"
    return _json_file(path)


def _resource_probe_windows() -> Dict[str, Any]:
    """Collect a redacted host/process sample using only Windows built-ins."""
    powershell = shutil.which("powershell.exe")
    if not powershell:
        return {"available": False, "error": "Windows resource probe unavailable"}
    script = (
        "$os=Get-CimInstance Win32_OperatingSystem;"
        "$cpuRows=Get-CimInstance Win32_Processor;"
        "$disks=@(Get-CimInstance Win32_LogicalDisk -Filter \"DriveType=3\"|ForEach-Object{"
        "[pscustomobject]@{drive=[string]$_.DeviceID;label=[string]$_.VolumeName;"
        "fileSystem=[string]$_.FileSystem;totalBytes=[int64]$_.Size;freeBytes=[int64]$_.FreeSpace}});"
        "$cpu=[math]::Round((($cpuRows|Measure-Object -Property LoadPercentage -Average).Average),1);"
        "$rows=@(Get-Process -ErrorAction SilentlyContinue|ForEach-Object{"
        "$c=0;if($null-ne$_.CPU){$c=[double]$_.CPU};"
        "[pscustomobject]@{pid=[int]$_.Id;name=[string]$_.ProcessName;"
        "workingSetBytes=[int64]$_.WorkingSet64;cpuSeconds=$c}});"
        "[pscustomobject]@{available=$true;cpuPercent=$cpu;"
        "logicalCpus=[int]$env:NUMBER_OF_PROCESSORS;"
        "memoryTotalBytes=[int64]$os.TotalVisibleMemorySize*1024;"
        "memoryFreeBytes=[int64]$os.FreePhysicalMemory*1024;fixedDisks=$disks;processes=$rows}"
        "|ConvertTo-Json -Compress -Depth 4"
    )
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            **hidden_subprocess_kwargs(),
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {"available": False, "error": "无法读取 Windows 资源状态"}
        value = json.loads(result.stdout)
        return value if isinstance(value, dict) else {
            "available": False,
            "error": "Windows 资源数据格式异常",
        }
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return {"available": False, "error": str(exc)}


def _resource_category(name: str, pid: int, service_pids: set[int]) -> str:
    if pid in service_pids:
        return "quotapool"
    lowered = name.casefold()
    if lowered in {"claude.quotapool-real", "claude-code", "claude_code"}:
        return "claude-code"
    if lowered == "claude":
        return "claude-desktop"
    if lowered in {
        "cargo", "rustc", "link", "cl", "msbuild", "dotnet", "cmake",
        "ninja", "make", "webpack", "vite", "esbuild", "tsc",
    }:
        return "build"
    if lowered in {"node", "python", "pythonw", "npm", "pnpm", "yarn", "bun", "deno"}:
        return "runtime"
    if lowered in {"vmmemwsl", "wsl", "wslhost", "wslservice", "wslrelay"}:
        return "wsl"
    if lowered in {
        "chatgpt", "code", "cursor", "chrome", "msedge", "firefox", "feishu",
        "slack", "teams", "windowsterminal", "explorer",
    }:
        return "apps"
    return "other"


def _pressure_level(percent: float, *, warning: float, critical: float) -> str:
    if percent >= critical:
        return "critical"
    if percent >= warning:
        return "warning"
    return "healthy"


def _resource_snapshot(runtime: Mapping[str, Any]) -> Dict[str, Any]:
    raw = _resource_probe_windows()
    if not raw.get("available"):
        return raw
    try:
        total = max(0, int(raw.get("memoryTotalBytes") or 0))
        free = max(0, int(raw.get("memoryFreeBytes") or 0))
        cpu_percent = max(0.0, min(100.0, float(raw.get("cpuPercent") or 0)))
        logical_cpus = max(1, int(raw.get("logicalCpus") or os.cpu_count() or 1))
    except (TypeError, ValueError):
        return {"available": False, "error": "Windows 资源数据格式异常"}

    records = _desktop_service_records(runtime)
    service_roles: Dict[int, str] = {os.getpid(): "QuotaPool 可视化面板"}
    for key, label in (("coordinator", "QuotaPool 协调器"), ("dataplane", "QuotaPool 数据面")):
        record = records.get(key) if isinstance(records.get(key), dict) else {}
        try:
            service_roles[int(record.get("pid"))] = label
        except (TypeError, ValueError):
            pass
    service_pids = set(service_roles)

    now = time.monotonic()
    current_cpu: Dict[int, float] = {}
    rows: list[Dict[str, Any]] = []
    for item in raw.get("processes", []) if isinstance(raw.get("processes"), list) else []:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get("pid"))
            memory = max(0, int(item.get("workingSetBytes") or 0))
            cpu_seconds = max(0.0, float(item.get("cpuSeconds") or 0))
        except (TypeError, ValueError):
            continue
        name = str(item.get("name") or "unknown")[:80]
        current_cpu[pid] = cpu_seconds
        rows.append({
            "pid": pid,
            "name": name,
            "memoryBytes": memory,
            "cpuSeconds": cpu_seconds,
            "category": _resource_category(name, pid, service_pids),
        })

    global _RESOURCE_CPU_SAMPLE
    with _RESOURCE_SAMPLE_LOCK:
        previous_at, previous_cpu = _RESOURCE_CPU_SAMPLE
        _RESOURCE_CPU_SAMPLE = (now, current_cpu)
    elapsed = now - previous_at if previous_at else 0.0
    for row in rows:
        previous = previous_cpu.get(int(row["pid"]))
        if elapsed > 0 and previous is not None and row["cpuSeconds"] >= previous:
            row["cpuPercent"] = round(
                min(100.0, ((row["cpuSeconds"] - previous) / elapsed / logical_cpus) * 100),
                1,
            )
        else:
            row["cpuPercent"] = None
        row.pop("cpuSeconds", None)

    groups: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = str(row["category"])
        group = groups.setdefault(key, {
            "key": key,
            "label": _RESOURCE_CATEGORY_LABELS[key],
            "processCount": 0,
            "memoryBytes": 0,
            "cpuPercent": 0.0,
        })
        group["processCount"] += 1
        group["memoryBytes"] += int(row["memoryBytes"])
        if row["cpuPercent"] is not None:
            group["cpuPercent"] += float(row["cpuPercent"])
    grouped = sorted(groups.values(), key=lambda item: item["memoryBytes"], reverse=True)
    for group in grouped:
        group["cpuPercent"] = round(min(100.0, group["cpuPercent"]), 1)

    process_by_pid = {int(row["pid"]): row for row in rows}
    services = []
    for pid, label in service_roles.items():
        row = process_by_pid.get(pid)
        services.append({
            "name": label,
            "pid": pid,
            "running": bool(row),
            "memoryBytes": row.get("memoryBytes", 0) if row else 0,
            "cpuPercent": row.get("cpuPercent") if row else None,
        })

    memory_used = max(0, total - free)
    memory_percent = round((memory_used / total) * 100, 1) if total else 0.0
    memory_level = _pressure_level(memory_percent, warning=75, critical=88)
    cpu_level = _pressure_level(cpu_percent, warning=80, critical=92)
    rank = {"healthy": 0, "warning": 1, "critical": 2}
    overall = max((memory_level, cpu_level), key=lambda value: rank[value])
    counts = {key: int(groups.get(key, {}).get("processCount", 0)) for key in _RESOURCE_CATEGORY_LABELS}
    build_memory = int(groups.get("build", {}).get("memoryBytes", 0))
    recommendations: list[Dict[str, str]] = []
    if cpu_level != "healthy" and counts["build"]:
        recommendations.append({
            "tone": cpu_level,
            "title": "降低并行构建数",
            "detail": f"检测到 {counts['build']} 个编译进程；先让当前构建收尾，再启动下一批。",
        })
    if memory_level != "healthy":
        recommendations.append({
            "tone": memory_level,
            "title": "暂停新增后台会话",
            "detail": "保留正在回复的任务；完成后关闭闲置会话，避免强制终止造成上下文丢失。",
        })
    if counts["claude-code"] >= 16:
        recommendations.append({
            "tone": "warning",
            "title": "Claude Code 会话较多",
            "detail": f"当前发现 {counts['claude-code']} 个会话进程。QuotaPool 不会自动结束活跃任务。",
        })
    if build_memory >= 4 * 1024**3:
        recommendations.append({
            "tone": "warning",
            "title": "编译占用正在形成峰值",
            "detail": "构建进程合计占用较高；建议把大型 Rust / Node 构建错峰执行。",
        })
    if not recommendations:
        recommendations.append({
            "tone": "healthy",
            "title": "资源余量正常",
            "detail": "无需主动释放内存；继续保护当前任务，磁盘仍由安全维护策略处理。",
        })

    top = sorted(rows, key=lambda item: (item["memoryBytes"], item["cpuPercent"] or 0), reverse=True)[:18]
    disks: list[Dict[str, Any]] = []
    for item in raw.get("fixedDisks", []) if isinstance(raw.get("fixedDisks"), list) else []:
        if not isinstance(item, dict):
            continue
        drive = str(item.get("drive") or "").upper()
        if not re.fullmatch(r"[A-Z]:", drive):
            continue
        try:
            disk_total = max(0, int(item.get("totalBytes") or 0))
            disk_free = max(0, int(item.get("freeBytes") or 0))
        except (TypeError, ValueError):
            continue
        disk_used = max(0, disk_total - disk_free)
        disks.append({
            "available": True,
            "drive": drive,
            "path": f"{drive}\\",
            "label": str(item.get("label") or "")[:80],
            "fileSystem": str(item.get("fileSystem") or "")[:20],
            "totalBytes": disk_total,
            "usedBytes": disk_used,
            "freeBytes": disk_free,
            "usedPercent": round((disk_used / disk_total) * 100, 1) if disk_total else 0.0,
            "maintenance": "automatic-safe-cache",
            "protectedProjectData": True,
        })
    disks.sort(key=lambda item: item["drive"])
    return {
        "available": True,
        "sampledAt": datetime.now(timezone.utc).isoformat(),
        "overall": overall,
        "cpu": {"percent": cpu_percent, "level": cpu_level, "logicalCpus": logical_cpus},
        "memory": {
            "totalBytes": total,
            "usedBytes": memory_used,
            "freeBytes": free,
            "usedPercent": memory_percent,
            "level": memory_level,
        },
        "processCount": len(rows),
        "disks": disks,
        "groups": grouped,
        "services": services,
        "topProcesses": top,
        "recommendations": recommendations,
        "policy": {
            "activeTasksProtected": True,
            "automaticProcessTermination": False,
            "automaticDiskMaintenance": True,
            "strategy": "先观测，再限制新增并发；只自动清理安全缓存，不强杀活跃任务。",
        },
    }


def _claude_ssh_connection() -> Dict[str, Any]:
    appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    configs = _json_file(appdata / "Claude" / "ssh_configs.json").get("configs")
    rows = configs if isinstance(configs, list) else []
    selected = next(
        (row for row in rows if isinstance(row, dict) and "127.0.0.1" in str(row.get("sshHost") or "")),
        next((row for row in rows if isinstance(row, dict)), {}),
    )
    host = str(selected.get("sshHost") or "kachi@127.0.0.1")
    try:
        port = int(selected.get("sshPort") or 2222)
    except (TypeError, ValueError):
        port = 2222
    identity = str(selected.get("sshIdentityFile") or "")
    identity_path = Path(identity) if identity else None
    user_profile = str(Path.home()).replace("/", "\\").rstrip("\\")
    display_identity = identity.replace("/", "\\")
    if display_identity.casefold().startswith(user_profile.casefold()):
        display_identity = "%USERPROFILE%" + display_identity[len(user_profile):]
    ssh_command = f'ssh -i "{display_identity}" -p {port} {host}' if display_identity else f"ssh -p {port} {host}"
    return {
        "name": str(selected.get("name") or "QuotaPool WSL"),
        "host": host,
        "hostname": host.rsplit("@", 1)[-1],
        "port": port,
        "identityFile": display_identity or None,
        "identityPresent": bool(identity_path and identity_path.is_file()),
        "configured": bool(selected),
        "sshCommand": ssh_command,
    }


def _collect_windows_host_snapshot(runtime: Mapping[str, Any]) -> Dict[str, Any]:
    task_states = _task_states()
    disk_task = task_states.get("QuotaPool Disk Maintenance", {"installed": False, "state": "missing"})
    worktree_task = task_states.get("QuotaPool Worktree Maintenance", {"installed": False, "state": "missing"})
    tray = task_states.get("QuotaPool Tray Monitor", {"installed": False, "state": "missing"})
    disk_maintenance = read_maintenance_state()
    disk_manual_request = read_manual_maintenance_request()
    worktree_maintenance = read_worktree_state()
    prefix = Path(str(runtime.get("prefix") or Path.home()))
    windows_disk = _disk_snapshot(prefix if prefix.exists() else Path.home())
    resources = _resource_snapshot(runtime)
    maintenance_drives = {
        str(item.get("drive") or "").upper(): item
        for item in disk_maintenance.get("drives", [])
        if isinstance(item, dict)
    }
    maintenance_actions: Dict[str, Dict[str, Any]] = {}
    for action in disk_maintenance.get("actions", []):
        if not isinstance(action, dict):
            continue
        drive = str(action.get("drive") or "").upper()
        if not drive:
            continue
        summary = maintenance_actions.setdefault(
            drive, {
                "actions": 0,
                "bytes": 0,
                "buildActive": False,
                "activeProtected": 0,
                "recentProtected": 0,
            }
        )
        summary["actions"] += 1
        summary["bytes"] += max(0, int(action.get("bytes") or 0))
        if action.get("status") == "build-active":
            summary["buildActive"] = True
        if action.get("name") == "claude-build-targets":
            active_paths = action.get("skippedActive")
            recent_paths = action.get("skippedRecent")
            if isinstance(active_paths, list):
                summary["activeProtected"] += len(active_paths)
            if isinstance(recent_paths, list):
                summary["recentProtected"] += len(recent_paths)
    for disk in resources.get("disks", []):
        if not isinstance(disk, dict):
            continue
        drive = str(disk.get("drive") or "").upper()
        row = maintenance_drives.get(drive, {})
        summary = maintenance_actions.get(drive, {})
        disk["maintenanceState"] = str(row.get("levelAfter") or row.get("level") or "unknown")
        disk["lastReclaimedBytes"] = max(
            int(row.get("reclaimedBytes") or 0), int(summary.get("bytes") or 0)
        )
        disk["lastMaintenanceActions"] = int(summary.get("actions") or 0)
        disk["nextActionAt"] = row.get("nextActionAt")
        protection_notes = []
        if summary.get("buildActive"):
            protection_notes.append("检测到活跃 Rust 构建，当前任务已保护")
        if summary.get("activeProtected"):
            protection_notes.append(
                f"{int(summary['activeProtected'])} 个 Claude 构建缓存仍被进程使用"
            )
        if summary.get("recentProtected"):
            protection_notes.append(
                f"{int(summary['recentProtected'])} 个近期 Claude 构建缓存冷却后自动回收"
            )
        disk["actionReason"] = (
            "；".join(protection_notes)
            if protection_notes
            else str(row.get("actionReason") or "")
        )
        disk["noProgressCount"] = int(row.get("noProgressCount") or 0)
        disk["pressureLevel"] = str(
            row.get("effectiveLevelAfter") or row.get("effectiveLevel") or "unknown"
        )
        disk["maintenanceMode"] = str(row.get("modeAfter") or row.get("mode") or "observe")
        disk["maintenanceReason"] = str(row.get("reason") or "")
        disk["consumptionBytesPerHour"] = int(row.get("consumptionBytesPerHour") or 0)
        disk["estimatedMinutesToCritical"] = row.get("estimatedMinutesToCritical")
        disk["criticalBytes"] = int(row.get("criticalBytes") or 0)
        disk["warningBytes"] = int(row.get("warningBytes") or 0)
        disk["recoveryBytes"] = int(row.get("recoveryBytes") or 0)
    try:
        resource_guard = record_guard_sample(
            runtime,
            resources,
            resources.get("disks") or [windows_disk],
        )
    except OSError:
        resource_guard = read_guard_state(runtime)
    guard_drives = {
        str(item.get("drive") or "").upper(): item
        for item in resource_guard.get("drives", [])
        if isinstance(item, dict)
    }
    for disk in resources.get("disks", []):
        if not isinstance(disk, dict):
            continue
        drive = str(disk.get("drive") or "").upper()
        guard_drive = guard_drives.get(drive, {})
        if guard_drive:
            level_rank = {"unknown": -1, "healthy": 0, "warning": 1, "critical": 2}
            guard_level = str(
                guard_drive.get("effectiveLevel") or guard_drive.get("level") or "unknown"
            )
            if level_rank.get(guard_level, -1) >= level_rank.get(
                str(disk.get("pressureLevel") or "unknown"), -1
            ):
                disk["pressureLevel"] = guard_level
            guard_mode = str(guard_drive.get("mode") or "observe")
            if guard_mode != "observe":
                disk["maintenanceMode"] = guard_mode
                disk["maintenanceReason"] = str(guard_drive.get("reason") or "")
            disk["consumptionBytesPerHour"] = max(
                int(disk.get("consumptionBytesPerHour") or 0),
                int(guard_drive.get("consumptionBytesPerHour") or 0),
            )
            estimates = [
                int(value) for value in (
                    disk.get("estimatedMinutesToCritical"),
                    guard_drive.get("estimatedMinutesToCritical"),
                ) if value is not None
            ]
            disk["estimatedMinutesToCritical"] = min(estimates) if estimates else None
            disk["criticalBytes"] = int(guard_drive.get("criticalBytes") or 0)
            disk["warningBytes"] = int(guard_drive.get("warningBytes") or 0)
            disk["recoveryBytes"] = int(guard_drive.get("recoveryBytes") or 0)
    guard_disk_level = str((resource_guard.get("levels") or {}).get("disk") or "unknown")
    if guard_disk_level == "critical":
        resources["overall"] = "critical"
        resources["recommendations"] = [
            item for item in resources.get("recommendations", [])
            if isinstance(item, dict) and item.get("tone") != "healthy"
        ]
        critical_drives = [
            str(item.get("drive") or "磁盘")
            for item in resource_guard.get("drives", [])
            if isinstance(item, dict) and item.get("level") == "critical"
        ]
        resources.setdefault("recommendations", []).insert(0, {
            "tone": "critical",
            "title": f"{'、'.join(critical_drives) or '本地'} 盘空间临界",
            "detail": "已暂停新增可延期后台任务，并自动清理各盘白名单旧缓存；项目、模型、会话与活跃构建均受保护。",
        })
    elif guard_disk_level == "warning" and resources.get("overall") == "healthy":
        resources["overall"] = "warning"
    maintenance_status = str(disk_maintenance.get("status") or "")
    healthy = bool(
        windows_disk.get("available")
        and maintenance_status in {"healthy", "recovered"}
        and guard_disk_level == "healthy"
    )
    snapshot = {
        "isWindows": True,
        "healthy": healthy,
        "error": None,
        "automation": {
            "diskTask": disk_task,
            "diskMaintenance": disk_maintenance,
            "diskManualRequest": disk_manual_request,
            "worktreeTask": worktree_task,
            "worktreeMaintenance": worktree_maintenance,
            "tray": tray,
            "resourceGuard": resource_guard,
        },
        "windowsDisk": windows_disk,
        "resources": resources,
    }
    return snapshot


def _refresh_windows_host_snapshot(runtime: Mapping[str, Any]) -> None:
    global _WINDOWS_HOST_CACHE, _WINDOWS_HOST_REFRESHING
    try:
        snapshot = _collect_windows_host_snapshot(runtime)
    except Exception as exc:  # keep the loopback dashboard responsive on host-tool failures
        snapshot = {"isWindows": True, "healthy": False, "error": str(exc)}
    with _DESKTOP_CACHE_LOCK:
        _WINDOWS_HOST_CACHE = (time.monotonic(), dict(snapshot))
        _WINDOWS_HOST_REFRESHING = False


def _windows_host_snapshot(runtime: Mapping[str, Any], *, fresh: bool = False) -> Dict[str, Any]:
    """Return cached Windows storage health without probing WSL or SSH."""
    if os.name != "nt":
        return {"isWindows": False}
    global _WINDOWS_HOST_CACHE, _WINDOWS_HOST_REFRESHING
    if fresh:
        snapshot = _collect_windows_host_snapshot(runtime)
        with _DESKTOP_CACHE_LOCK:
            _WINDOWS_HOST_CACHE = (time.monotonic(), dict(snapshot))
        return snapshot

    start_refresh = False
    with _DESKTOP_CACHE_LOCK:
        cached_at, cached = _WINDOWS_HOST_CACHE
        if cached and time.monotonic() - cached_at < 15:
            return dict(cached)
        if not _WINDOWS_HOST_REFRESHING:
            _WINDOWS_HOST_REFRESHING = True
            start_refresh = True
        response = dict(cached) if cached else {
            "isWindows": True,
            "healthy": False,
            "error": None,
            "automation": {
                "diskTask": {"installed": False, "state": "checking"},
                "diskMaintenance": {},
                "worktreeTask": {"installed": False, "state": "checking"},
                "worktreeMaintenance": {},
                "tray": {"installed": False, "state": "checking"},
            },
            "windowsDisk": _disk_snapshot(Path.home()),
            "resources": {"available": False, "checking": True},
        }
        response["refreshing"] = True
    if start_refresh:
        threading.Thread(
            target=_refresh_windows_host_snapshot,
            args=(dict(runtime),),
            name="quotapool-dashboard-windows-host",
            daemon=True,
        ).start()
    return response


def _fleet_activity(
    runtime: Mapping[str, Any],
    *,
    limit: int = 40,
) -> Dict[str, Any]:
    """Return a bounded, token-free view of recent Fleet executions."""
    db_path = str(runtime.get("db_path") or "").strip()
    if not db_path:
        return {"records": [], "total": 0, "active": 0, "error": "未配置状态数据库"}
    try:
        db = FleetDB(db_path)
        executions = db.executions()
        leases = db.leases()
        accounts = db.accounts()
    except (OSError, RuntimeError) as exc:
        return {"records": [], "total": 0, "active": 0, "error": str(exc)}

    account_names: Dict[str, str] = {}
    for account in accounts:
        metadata = account.get("metadata") if isinstance(account.get("metadata"), dict) else {}
        account_id = str(account.get("id") or "")
        account_names[account_id] = str(
            metadata.get("runtime_name") or metadata.get("name") or _short_id(account_id, 8)
        )
    lease_by_execution = {
        str(lease.get("execution_id") or ""): lease
        for lease in leases
        if lease.get("execution_id")
    }
    recent = sorted(
        executions,
        key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0),
        reverse=True,
    )[:max(1, min(int(limit), 100))]
    records: list[Dict[str, Any]] = []
    for execution in recent:
        execution_id = str(execution.get("id") or "")
        lease = lease_by_execution.get(execution_id, {})
        account_id = str(lease.get("account_id") or "")
        records.append({
            "executionId": _short_id(execution_id, 22),
            "sessionId": _short_id(execution.get("session_id"), 18),
            "status": str(execution.get("status") or "unknown"),
            "workloadClass": str(execution.get("workload_class") or "interactive"),
            "account": account_names.get(account_id, _short_id(account_id, 8) or "—"),
            "createdAt": _iso_time(execution.get("created_at")),
            "updatedAt": _iso_time(execution.get("updated_at")),
            "leased": bool(lease),
        })
    return {
        "records": records,
        "total": len(executions),
        "active": sum(1 for item in executions if item.get("status") == "active"),
    }


def _snapshot(
    runtime: Mapping[str, Any],
    *,
    login: Optional[Mapping[str, Any]] = None,
    lark: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    team_path = Path(str(runtime.get("teamclaude_config") or ""))
    team = _json_file(team_path)
    endpoint = dataplane_endpoint(dict(runtime))
    data = _local_json(f"{endpoint}/teamclaude/status")
    services = service_status(dict(runtime))
    coordinator_health = _local_json(str(services.get("coordinator_url") or "")) \
        if services.get("coordinator_url") else {"error": "not configured"}
    services["coordinatorHealth"] = coordinator_health
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "services": services,
        "accounts": _safe_accounts(team),
        "dataPlane": data,
        "fleetActivity": _fleet_activity(runtime),
        "dashboard": {
            "host": DEFAULT_DASHBOARD_HOST,
            "port": DEFAULT_DASHBOARD_PORT,
            "loopbackOnly": True,
            "login": dict(login or {"status": "idle"}),
            "lark": dict(lark or {
                "installed": False,
                "configured": False,
                "connected": False,
                "auth": {"status": "idle"},
                "settings": {"version": 1, "identity": "user", "recipients": []},
            }),
            "claudeDesktop": _desktop_snapshot(),
            "windowsHost": _windows_host_snapshot(runtime),
        },
    }


def _teamclaude_command(runtime: Mapping[str, Any], args: Sequence[str]) -> list[str]:
    node = str(runtime.get("node_binary") or "")
    prefix = str(runtime.get("prefix") or "")
    if not node or not prefix:
        raise RuntimeError("runtime.json is missing node_binary or prefix")
    entry = Path(prefix) / "vendor" / "teamclaude" / "src" / "index.js"
    return [node, str(entry), *args]


def _toggle_account(
    runtime: Mapping[str, Any],
    name: str,
    *,
    runtime_path: Optional[str] = None,
) -> None:
    if not _ACCOUNT_NAME.fullmatch(name) or "\n" in name or "\r" in name:
        raise ValueError("invalid account name")
    team = _json_file(Path(str(runtime.get("teamclaude_config") or "")))
    accounts = team.get("accounts") if isinstance(team.get("accounts"), list) else []
    target = next((a for a in accounts if isinstance(a, dict) and a.get("name") == name), None)
    if target is None:
        raise ValueError("account not found")
    mode = "active" if target.get("disabled") else "paused"
    db_path = str(runtime.get("db_path") or "").strip()
    if not db_path:
        raise RuntimeError("runtime.json is missing db_path")
    # `set_mode` is the project's single supported account switch: it updates
    # teamclaude.json, Fleet's accounts.state and the live data-plane reload in
    # one operation.  Calling the TeamClaude CLI alone left the dashboard green
    # while the coordinator silently kept the same account disabled.
    set_mode(FleetDB(db_path), name, mode, runtime_path=runtime_path)


def _next_account_name(team: Mapping[str, Any]) -> str:
    names = {
        str(item.get("name"))
        for item in team.get("accounts", [])
        if isinstance(item, dict)
    }
    index = 1
    while f"max-{index}" in names:
        index += 1
    return f"max-{index}"


def _claude_desktop_info() -> Dict[str, Any]:
    """Discover the Store Claude Desktop executable and AUMID safely."""
    powershell = shutil.which("powershell.exe")
    if not powershell:
        return {"installed": False, "reason": "Windows bridge unavailable"}
    global _DESKTOP_INFO_CACHE
    with _DESKTOP_CACHE_LOCK:
        cached_at, cached = _DESKTOP_INFO_CACHE
        if cached and time.monotonic() - cached_at < 300:
            return dict(cached)
    candidates: list[tuple[str, str, str]] = []
    # WindowsApps is ACL-protected and ordinary directory globbing can return
    # nothing even though the package is installed. AppX's own read-only
    # package query is the reliable discovery path and needs no elevation.
    command = (
        "Get-AppxPackage -Name Claude | ForEach-Object { "
        "$exe=Join-Path $_.InstallLocation 'app\\claude.exe'; "
        "[pscustomobject]@{InstallLocation=$_.InstallLocation;"
        "PackageFullName=$_.PackageFullName;ExecutablePath=$exe;"
        "Exists=(Test-Path -LiteralPath $exe)} } | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8, check=False,
            **hidden_subprocess_kwargs(),
        )
        if result.returncode == 0 and result.stdout.strip():
            raw = json.loads(result.stdout)
            rows = raw if isinstance(raw, list) else [raw]
            for row in rows:
                if isinstance(row, dict) and row.get("InstallLocation") and row.get("Exists"):
                    candidates.append((
                        str(row["InstallLocation"]),
                        str(row.get("PackageFullName") or ""),
                        str(row.get("ExecutablePath") or ""),
                    ))
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        pass
    for install, package_full_name, executable in candidates:
        exe = executable or ntpath.join(install, "app", "claude.exe")
        match = re.search(r"__([^_]+)$", package_full_name, re.I)
        publisher_id = match.group(1) if match else "pzs8sxrjxfjjc"
        found = {
            "installed": True,
            "path": exe,
            "package": package_full_name or ntpath.basename(install),
            "appId": f"Claude_{publisher_id}!Claude",
        }
        with _DESKTOP_CACHE_LOCK:
            _DESKTOP_INFO_CACHE = (time.monotonic(), dict(found))
        return found
    missing = {"installed": False, "appId": _CLAUDE_DESKTOP_APP_ID_FALLBACK}
    with _DESKTOP_CACHE_LOCK:
        _DESKTOP_INFO_CACHE = (time.monotonic(), dict(missing))
    return missing


def _claude_desktop_processes(info: Mapping[str, Any]) -> list[Dict[str, Any]]:
    """Return only Store Desktop processes, never Claude Code binaries."""
    powershell = shutil.which("powershell.exe")
    target = str(info.get("path") or "").replace("/", "\\").rstrip("\\").casefold()
    if not powershell or not target:
        return []
    command = (
        "Get-CimInstance Win32_Process -Filter \"Name='claude.exe'\" "
        "| Select-Object ProcessId,ParentProcessId,ExecutablePath "
        "| ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8, check=False,
            **hidden_subprocess_kwargs(),
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        raw = json.loads(result.stdout)
        rows = raw if isinstance(raw, list) else [raw]
        found = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            path = str(row.get("ExecutablePath") or "").replace("/", "\\").rstrip("\\").casefold()
            if path == target:
                found.append({
                    "pid": int(row.get("ProcessId")),
                    "parentPid": int(row.get("ParentProcessId")) if row.get("ParentProcessId") else None,
                })
        return found
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return []


def _desktop_snapshot(*, fresh: bool = False) -> Dict[str, Any]:
    global _DESKTOP_STATUS_CACHE
    with _DESKTOP_CACHE_LOCK:
        cached_at, cached = _DESKTOP_STATUS_CACHE
        if not fresh and cached and time.monotonic() - cached_at < 3:
            return dict(cached)
    info = _claude_desktop_info()
    processes = _claude_desktop_processes(info) if info.get("installed") else []
    snapshot = {
        **info,
        "processCount": len(processes),
        "running": bool(processes),
        "codeSessionsProtected": True,
    }
    with _DESKTOP_CACHE_LOCK:
        _DESKTOP_STATUS_CACHE = (time.monotonic(), dict(snapshot))
    return snapshot


def _latest_claude_appx_error() -> Optional[str]:
    powershell = shutil.which("powershell.exe")
    if not powershell:
        return None
    command = (
        "$e=Get-WinEvent -LogName 'Microsoft-Windows-AppModel-Runtime/Admin' -MaxEvents 30 "
        "-ErrorAction SilentlyContinue | Where-Object {$_.Id -in 208,215 -and "
        "$_.Message -match 'Claude_'} | Select-Object -First 1; if($e){$e.Message}"
    )
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8, check=False,
            **hidden_subprocess_kwargs(),
        )
        message = result.stdout.strip()
        return message[:500] if message else None
    except (OSError, subprocess.SubprocessError):
        return None


def _start_claude_desktop(
    *,
    force_restart: bool = False,
    runtime_path: Optional[str] = None,
) -> Dict[str, Any]:
    powershell = shutil.which("powershell.exe")
    if not powershell:
        raise RuntimeError("无法连接 Windows Claude Desktop")
    info = _claude_desktop_info()
    if not info.get("installed"):
        raise RuntimeError("未找到 Microsoft Store 版 Claude Desktop")
    before = _claude_desktop_processes(info)
    stopped: list[int] = []
    if force_restart and before:
        # Kill only processes whose full executable path is the Store Desktop
        # binary. Claude Code's managed binary lives under %APPDATA% and is not
        # in this set, so active Code work is deliberately left untouched.
        ids = ",".join(str(int(item["pid"])) for item in before)
        command = f"Stop-Process -Id {ids} -Force -ErrorAction Stop"
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, check=False,
            **hidden_subprocess_kwargs(),
        )
        if result.returncode != 0:
            raise RuntimeError("无法强制关闭 Claude Desktop 进程")
        stopped = [int(item["pid"]) for item in before]
        time.sleep(0.8)
    launch = launch_claude_desktop(
        runtime_path,
        # _claude_desktop_info returns ``path``.  Using a non-existent
        # ``executable`` key silently fell back to secondary discovery, which
        # can fail under the WindowsApps ACL even though the AppX query already
        # found the installed Claude binary.
        executable=str(info.get("path") or "") or None,
    )
    deadline = time.monotonic() + 5.0
    after = _desktop_snapshot(fresh=True)
    while not after.get("running") and time.monotonic() < deadline:
        time.sleep(0.4)
        after = _desktop_snapshot(fresh=True)
    if not after.get("running"):
        error = _latest_claude_appx_error() or "Windows 未创建 Claude Desktop 进程"
        return {
            "ok": False,
            "error": error,
            "action": "restart" if force_restart else "open",
            "stoppedDesktopPids": stopped,
            "desktop": after,
        }
    return {
        "ok": True,
        "action": "restart" if force_restart else "open",
        "stoppedDesktopPids": stopped,
        "beforeProcessCount": len(before),
        "desktop": after,
        "launch": launch,
        "message": "Claude Desktop 已通过 QuotaPool 启动。",
    }


class _DashboardHandler(BaseHTTPRequestHandler):
    server_version = "QuotapoolDashboard/1.0"

    @property
    def dashboard(self) -> "DashboardServer":
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: Any) -> None:
        # Keep tokens and account metadata out of the service console log.
        self.dashboard.log(format % args)

    def _send_json(self, status: int, value: Mapping[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _action_allowed(self) -> bool:
        return self.headers.get("X-Quotapool-Action") == _ACTION_HEADER

    def _read_json_body(self, *, limit: int = 131072) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length < 0 or length > limit:
            raise ValueError("请求内容过大")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求内容不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("请求内容必须是对象")
        return value

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path == '/api/automations':
            self._send_json(200, self.dashboard.automations.list())
            return
        if path in ('/workspace.css', '/workspace.js'):
            payload = self.dashboard.asset.with_name(path[1:]).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/css; charset=utf-8' if path.endswith('.css') else 'text/javascript; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path in ("/", "/index.html"):
            html = self.dashboard.asset.read_text(encoding='utf-8')
            html = html.replace('</head>', '<link rel="stylesheet" href="/workspace.css"></head>')
            html = html.replace('</body>', '<script src="/workspace.js"></script></body>')
            payload = html.encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/sidebar-mascot.png":
            asset = self.dashboard.asset.with_name("sidebar-mascot.png")
            if not asset.is_file():
                self._send_json(404, {"error": "not found"})
                return
            payload = asset.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/api/snapshot":
            self._send_json(200, _snapshot(
                self.dashboard.runtime,
                login=self.dashboard.login_state(),
                lark=self.dashboard.lark_state(),
            ))
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._action_allowed():
            self._send_json(403, {"error": "dashboard action header required"})
            return
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path.startswith('/api/automations/'):
                body = self._read_json_body()
                action = path.rsplit('/', 1)[-1]
                if action == 'save':
                    result = self.dashboard.automations.save(body)
                elif action == 'delete':
                    self.dashboard.automations.delete(str(body.get('id') or ''))
                    result = {'ok': True}
                elif action == 'preview':
                    result = self.dashboard.automations.run(str(body.get('id') or ''), preview=True)
                else:
                    self._send_json(404, {'error': 'not found'})
                    return
                self._send_json(200, result)
                return
            if path == "/api/probe":
                endpoint = dataplane_endpoint(dict(self.dashboard.runtime))
                result = _local_json(f"{endpoint}/teamclaude/probe", method="POST", timeout=20)
                self._send_json(200 if "error" not in result else 502, result)
                return
            if path == "/api/accounts/login":
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(min(length, 4096)) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError:
                    body = {}
                requested = body.get("name") if isinstance(body, dict) else None
                team = _json_file(Path(str(self.dashboard.runtime["teamclaude_config"])))
                name = str(requested or _next_account_name(team)).strip()
                if not _ACCOUNT_NAME.fullmatch(name):
                    raise ValueError("账号名称只能包含字母、数字、空格、点、下划线、@、括号或短横线")
                result = self.dashboard.start_login(name)
                self._send_json(200, result)
                return
            if path == "/api/accounts/login/cancel":
                self._send_json(200, self.dashboard.cancel_login())
                return
            if path == "/api/lark/auth/start":
                self._send_json(200, self.dashboard.start_lark_auth())
                return
            if path == "/api/lark/auth/complete":
                self._send_json(202, self.dashboard.complete_lark_auth())
                return
            if path == "/api/lark/status/refresh":
                lark_integration.invalidate_status_cache()
                self._send_json(200, {"ok": True, "lark": self.dashboard.lark_state()})
                return
            if path == "/api/lark/recipients/search":
                body = self._read_json_body()
                result = lark_integration.search_recipients(
                    str(body.get("query") or ""),
                    identity=str(body.get("identity") or "user"),
                )
                self._send_json(200, {"ok": True, **result})
                return
            if path == "/api/lark/settings":
                body = self._read_json_body()
                settings = lark_integration.save_settings(self.dashboard.runtime, body)
                self._send_json(200, {"ok": True, "settings": settings})
                return
            if path == "/api/lark/share":
                if self.headers.get("X-Quotapool-Confirm") != "share-lark":
                    self._send_json(409, {"error": "请先确认飞书发送对象、身份与内容"})
                    return
                body = self._read_json_body(limit=262144)
                result = lark_integration.share(self.dashboard.runtime, body)
                self._send_json(200 if result.get("ok") else 207, result)
                return
            if path == "/api/claude-desktop/open":
                self._send_json(200, _start_claude_desktop(
                    force_restart=False,
                    runtime_path=self.dashboard.runtime_path,
                ))
                return
            if path == "/api/claude-desktop/restart":
                confirm = self.headers.get("X-Quotapool-Confirm")
                if confirm != "restart-claude-desktop":
                    self._send_json(409, {"error": "需要确认强制重启 Claude Desktop"})
                    return
                self._send_json(200, _start_claude_desktop(
                    force_restart=True,
                    runtime_path=self.dashboard.runtime_path,
                ))
                return
            if path == "/api/disk-maintenance/run":
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(min(length, 4096)) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError:
                    body = {}
                drive = str(body.get("drive") if isinstance(body, dict) else "").upper()
                if not re.fullmatch(r"[A-Z]:", drive):
                    raise ValueError("请选择一个有效的本地固定磁盘")
                confirm = self.headers.get("X-Quotapool-Confirm")
                if confirm != f"maintain-disk:{drive}":
                    self._send_json(409, {"error": "需要确认主动磁盘维护"})
                    return
                queued = request_manual_maintenance(drive)
                command = shutil.which("schtasks.exe") or "schtasks.exe"
                result = subprocess.run(
                    [command, "/Run", "/TN", "QuotaPool Disk Maintenance"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=15,
                    check=False,
                    **hidden_subprocess_kwargs(),
                )
                immediate = result.returncode == 0
                self._send_json(202, {
                    "ok": True,
                    "status": "queued",
                    "drive": drive,
                    "requestId": queued.get("requestId"),
                    "immediate": immediate,
                    "message": (
                        f"{drive} 主动安全维护已排队，正在绕过退避重新扫描"
                        if immediate else
                        f"{drive} 主动安全维护已排队，将在下个 2 分钟周期执行"
                    ),
                })
                return
            prefix = "/api/accounts/"
            if path.startswith(prefix) and path.endswith("/toggle"):
                encoded = path[len(prefix):-len("/toggle")].strip("/")
                name = urllib.parse.unquote(encoded)
                _toggle_account(
                    self.dashboard.runtime,
                    name,
                    runtime_path=self.dashboard.runtime_path,
                )
                self._send_json(200, {"ok": True, "snapshot": _snapshot(
                    self.dashboard.runtime,
                    login=self.dashboard.login_state(),
                    lark=self.dashboard.lark_state(),
                )})
                return
            if path == "/api/services/start":
                self._send_json(200, start_services(self.dashboard.runtime_path))
                return
        except lark_integration.LarkCLIError as exc:
            self._send_json(400, {"error": str(exc), "details": exc.details})
            return
        except (OSError, RuntimeError, ValueError, urllib.error.URLError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(404, {"error": "not found"})


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows SO_REUSEADDR allows two unrelated processes to bind the same
    # address. That is unsafe for a local control panel and made a slow health
    # check look like permission to create another server.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address: tuple[str, int], runtime: Dict[str, Any], runtime_path: Optional[str]):
        super().__init__(address, _DashboardHandler)
        self.runtime = runtime
        self.runtime_path = runtime_path
        self.asset = Path(__file__).with_name("dashboard.html")
        self._login_lock = threading.Lock()
        self._login: Dict[str, Any] = {"status": "idle"}
        self._lark_auth_lock = threading.Lock()
        self._lark_auth: Dict[str, Any] = {"status": "idle"}
        self.automations = Automations(runtime, self.automation_sample)

    def automation_sample(self):
        disks = []
        if os.name == 'nt':
            import ctypes
        for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
            drive = f'{letter}:\\'
            if os.name == 'nt' and ctypes.windll.kernel32.GetDriveTypeW(drive) == 3:
                try:
                    usage = shutil.disk_usage(drive)
                    disks.append({'drive': drive, 'freeBytes': usage.free})
                except OSError:
                    pass
        health = service_status(dict(self.runtime))
        return {'services': {k: bool(health.get(k)) for k in ('coordinator', 'dataplane')}, 'disks': disks}

    def login_state(self) -> Dict[str, Any]:
        with self._login_lock:
            state = dict(self._login)
            process = state.pop("process", None)
            if process is not None:
                code = process.poll()
                if code is None:
                    state["status"] = "waiting"
                elif code == 0:
                    state["status"] = "completed"
                    state.setdefault("message", "账号授权完成，正在热加载")
                else:
                    state["status"] = "failed"
                    state.setdefault("message", "OAuth 授权未完成")
                self._login["status"] = state["status"]
                self._login["exitCode"] = code
            state.pop("pid", None)
            return state

    def start_login(self, name: str) -> Dict[str, Any]:
        with self._login_lock:
            current = self._login.get("process")
            if current is not None and current.poll() is None:
                raise RuntimeError("已有一个账号授权流程在等待，请先完成或等待它超时")
            env = os.environ.copy()
            env["TEAMCLAUDE_CONFIG"] = str(self.runtime["teamclaude_config"])
            env["TEAMCLAUDE_DISABLE_AUTOUPDATE"] = "1"
            env["TEAMCLAUDE_LOGIN_TIMEOUT_MS"] = "600000"
            env["PYTHONUTF8"] = "1"
            env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1,::1"
            for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
                env.pop(key, None)
            flags = 0
            if os.name == "nt":
                flags = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NO_WINDOW
                )
            process = subprocess.Popen(
                _teamclaude_command(self.runtime, ["login", "--oauth", "--name", name]),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                close_fds=True,
                creationflags=flags,
            )
            self._login = {
                "status": "starting",
                "name": name,
                "startedAt": datetime.now(timezone.utc).isoformat(),
                "process": process,
            }
            threading.Thread(
                target=self._read_login_output,
                args=(process,),
                name="quotapool-dashboard-oauth",
                daemon=True,
            ).start()
            return {"ok": True, "name": name, "status": "starting"}

    def cancel_login(self) -> Dict[str, Any]:
        with self._login_lock:
            process = self._login.get("process")
            name = str(self._login.get("name") or "")
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            self._login = {
                "status": "cancelled",
                "name": name,
                "message": "账号授权已取消",
            }
            return {"ok": True, "name": name, "status": "cancelled"}

    def lark_state(self) -> Dict[str, Any]:
        status = lark_integration.public_status(self.runtime)
        with self._lark_auth_lock:
            auth = {
                key: value
                for key, value in self._lark_auth.items()
                if key != "deviceCode"
            }
        return {**status, "auth": auth}

    def start_lark_auth(self) -> Dict[str, Any]:
        with self._lark_auth_lock:
            if self._lark_auth.get("status") == "completing":
                raise RuntimeError("飞书授权正在完成，请稍候")
        result = lark_integration.start_auth()
        state = {
            "status": "waiting",
            "verificationUrl": result["verificationUrl"],
            "qrDataUrl": result.get("qrDataUrl") or "",
            "expiresIn": result.get("expiresIn") or 600,
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "deviceCode": result["deviceCode"],
        }
        with self._lark_auth_lock:
            self._lark_auth = state
        return {"ok": True, **{key: value for key, value in state.items() if key != "deviceCode"}}

    def complete_lark_auth(self) -> Dict[str, Any]:
        with self._lark_auth_lock:
            device_code = str(self._lark_auth.get("deviceCode") or "")
            if not device_code:
                raise RuntimeError("没有等待完成的飞书授权，请重新开始")
            if self._lark_auth.get("status") == "completing":
                return {"ok": True, "status": "completing"}
            self._lark_auth["status"] = "completing"
        threading.Thread(
            target=self._complete_lark_auth_worker,
            args=(device_code,),
            name="quotapool-lark-auth",
            daemon=True,
        ).start()
        return {"ok": True, "status": "completing"}

    def _complete_lark_auth_worker(self, device_code: str) -> None:
        try:
            lark_integration.complete_auth(device_code)
            update = {
                "status": "completed",
                "message": "飞书 CLI 已连接，可以搜索分享对象",
                "completedAt": datetime.now(timezone.utc).isoformat(),
            }
        except (lark_integration.LarkCLIError, OSError, ValueError) as exc:
            update = {
                "status": "failed",
                "message": str(exc),
                "completedAt": datetime.now(timezone.utc).isoformat(),
            }
        with self._lark_auth_lock:
            if self._lark_auth.get("deviceCode") == device_code:
                self._lark_auth = update

    def _read_login_output(self, process: subprocess.Popen[str]) -> None:
        url_pattern = re.compile(r"https://claude\.ai/oauth/authorize\?[^\s]+")
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    match = url_pattern.search(line)
                    if match:
                        with self._login_lock:
                            if self._login.get("process") is process:
                                self._login["authUrl"] = match.group(0)
                                self._login["status"] = "waiting"
        finally:
            if process.stdout is not None:
                process.stdout.close()

    def log(self, message: str) -> None:
        print(f"[quotapool-dashboard] {message}", file=sys.stderr, flush=True)


def serve_dashboard(
    runtime_path: Optional[str] = None,
    *,
    host: str = DEFAULT_DASHBOARD_HOST,
    port: int = DEFAULT_DASHBOARD_PORT,
    open_browser: bool = False,
) -> int:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError("dashboard is loopback-only")
    runtime = load_runtime(runtime_path)
    server = DashboardServer((host, int(port)), runtime, runtime_path)
    threading.Thread(target=server.automations.loop, name='quotapool-automations', daemon=True).start()
    url = f"http://{host}:{server.server_port}/"
    print(f"Quotapool dashboard: {url}", flush=True)
    if open_browser:
        threading.Timer(0.2, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.automations.stop.set()
        server.server_close()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m claude_fleet.dashboard")
    parser.add_argument("--runtime")
    parser.add_argument("--host", default=DEFAULT_DASHBOARD_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_DASHBOARD_PORT)
    args = parser.parse_args(argv)
    return serve_dashboard(args.runtime, host=args.host, port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
