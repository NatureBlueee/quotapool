"""Shared adaptive disk policy for observation, admission and maintenance."""

from __future__ import annotations

import math
import time
from typing import Any, Dict, Mapping, Optional, Sequence


GIB = 1024**3
SAMPLE_WINDOW_SECONDS = 30 * 60
MAX_SAMPLES = 24
PREDICTIVE_WARNING_MINUTES = 120
MIN_TREND_STEP_BYTES = 16 * 1024**2
WARNING_RECOVERY_MARGIN_BYTES = 4 * GIB


def disk_thresholds(total_bytes: int) -> Dict[str, int]:
    total = max(0, int(total_bytes or 0))
    # Development volumes can grow by tens of GiB during one Rust/Electron
    # build.  A 10% warning and 12% recovery line declared a 300 GiB drive
    # healthy at only 40 GiB free, so one normal build immediately consumed
    # the whole safety buffer.  Keep proportional headroom on data drives but
    # cap it so multi-terabyte system volumes do not demand hundreds of GiB.
    critical = max(16 * GIB, min(64 * GIB, int(total * 0.08)))
    warning = max(32 * GIB, min(96 * GIB, int(total * 0.18)))
    recovery = max(warning + 16 * GIB, min(128 * GIB, int(total * 0.25)))
    return {
        "criticalBytes": critical,
        "warningBytes": warning,
        "recoveryBytes": recovery,
    }


def append_disk_sample(
    samples: Optional[Sequence[Mapping[str, Any]]],
    free_bytes: int,
    *,
    now_epoch: Optional[float] = None,
) -> list[Dict[str, Any]]:
    now = time.time() if now_epoch is None else float(now_epoch)
    rows: list[Dict[str, Any]] = []
    for item in samples or ():
        try:
            at = float(item.get("at") or 0)
            free = max(0, int(item.get("freeBytes") or 0))
        except (AttributeError, TypeError, ValueError):
            continue
        if now - SAMPLE_WINDOW_SECONDS <= at <= now + 5:
            rows.append({"at": at, "freeBytes": free})
    if not rows or now - float(rows[-1]["at"]) >= 2:
        rows.append({"at": now, "freeBytes": max(0, int(free_bytes or 0))})
    else:
        rows[-1] = {"at": now, "freeBytes": max(0, int(free_bytes or 0))}
    return rows[-MAX_SAMPLES:]


def disk_consumption_rate(samples: Sequence[Mapping[str, Any]]) -> int:
    """Return positive bytes/hour consumption using a small linear regression."""
    points: list[tuple[float, float]] = []
    for item in samples:
        try:
            points.append((float(item.get("at") or 0), float(item.get("freeBytes") or 0)))
        except (AttributeError, TypeError, ValueError):
            continue
    if len(points) < 3 or points[-1][0] - points[0][0] < 60:
        return 0
    origin = points[0][0]
    xs = [at - origin for at, _ in points]
    ys = [free for _, free in points]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denominator = sum((value - mean_x) ** 2 for value in xs)
    if denominator <= 0:
        return 0
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator
    # Positive slope means free space is recovering, so consumption is zero.
    return max(0, int(-slope * 3600))


def disk_consumption_is_sustained(samples: Sequence[Mapping[str, Any]]) -> bool:
    """Reject one-off moves or cleanup steps as an ongoing consumption trend."""
    free_values: list[int] = []
    for item in samples:
        try:
            free_values.append(max(0, int(item.get("freeBytes") or 0)))
        except (AttributeError, TypeError, ValueError):
            continue
    if len(free_values) < 3:
        return False
    deltas = [before - after for before, after in zip(free_values, free_values[1:])][-6:]
    declines = [value for value in deltas if value >= MIN_TREND_STEP_BYTES]
    recoveries = [-value for value in deltas if value <= -MIN_TREND_STEP_BYTES]
    return bool(
        len(declines) >= 2
        and any(value >= MIN_TREND_STEP_BYTES for value in deltas[-3:])
        and sum(declines) > sum(recoveries)
    )


def assess_disk(
    total_bytes: int,
    free_bytes: int,
    *,
    used_percent: Optional[float] = None,
    previous_level: str = "unknown",
    samples: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    total = max(0, int(total_bytes or 0))
    free = max(0, int(free_bytes or 0))
    used = (
        float(used_percent)
        if used_percent is not None
        else ((total - free) / total * 100 if total else 0.0)
    )
    thresholds = disk_thresholds(total)
    if free < thresholds["criticalBytes"] or used >= 97:
        raw_level = "critical"
    elif free < thresholds["warningBytes"] or used >= 90:
        raw_level = "warning"
    else:
        raw_level = "healthy"

    level = raw_level
    # Hysteresis: a prior critical state keeps the full recovery buffer.  A
    # warning needs only a small margin above its threshold; otherwise a
    # normally stable 300 GiB data drive stays yellow from 54 all the way to
    # 75 GiB after one transient dip.
    if raw_level == "healthy" and previous_level == "critical":
        if free < thresholds["recoveryBytes"]:
            level = "warning"
    elif raw_level == "healthy" and previous_level == "warning":
        warning_recovery = min(
            thresholds["recoveryBytes"],
            thresholds["warningBytes"] + WARNING_RECOVERY_MARGIN_BYTES,
        )
        if free < warning_recovery:
            level = "warning"

    rate = disk_consumption_rate(samples)
    eta_minutes: Optional[int] = None
    if rate > 0 and free > thresholds["criticalBytes"]:
        eta_minutes = max(
            0,
            int(math.ceil((free - thresholds["criticalBytes"]) / rate * 60)),
        )
    predictive = bool(
        level == "healthy"
        and eta_minutes is not None
        and eta_minutes <= PREDICTIVE_WARNING_MINUTES
        and disk_consumption_is_sustained(samples)
    )
    effective_level = "warning" if predictive else level
    if free < 2 * GIB:
        mode = "emergency"
    elif level == "critical":
        mode = "critical"
    elif level == "warning":
        mode = "warning"
    elif predictive:
        mode = "predictive"
    else:
        mode = "observe"

    if mode == "emergency":
        reason = "可用空间低于 2 GB，立即执行安全缓存救援"
    elif mode == "critical":
        reason = "可用空间已低于临界阈值"
    elif mode == "warning":
        reason = "可用空间低于告警阈值，等待恢复到防抖线以上"
    elif mode == "predictive":
        reason = f"按当前趋势预计约 {eta_minutes} 分钟后进入临界"
    else:
        reason = "空间和增长趋势正常"
    return {
        **thresholds,
        "level": level,
        "effectiveLevel": effective_level,
        "mode": mode,
        "reason": reason,
        "consumptionBytesPerHour": rate,
        "estimatedMinutesToCritical": eta_minutes,
        "predictive": predictive,
    }
