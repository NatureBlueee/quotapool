from __future__ import annotations

import json
import time

from claude_fleet.resource_guard import (
    admission_decision,
    guard_state_path,
    record_guard_sample,
)


def _runtime(tmp_path):
    return {
        "db_path": str(tmp_path / "fleet.sqlite3"),
        "resource_guard": {
            "enabled": True,
            "freshness_seconds": 75,
            "poll_seconds": 1,
            "max_wait_seconds": 2,
        },
    }


def test_critical_memory_defers_only_existing_deferrable_classes(tmp_path):
    runtime = _runtime(tmp_path)
    state = record_guard_sample(runtime, {
        "available": True,
        "cpu": {"level": "healthy"},
        "memory": {"level": "critical"},
    }, {
        "available": True,
        "freeBytes": 200 * 1024**3,
        "usedPercent": 50,
    })

    assert state["deferNewBackground"] is True
    assert admission_decision(runtime, "workflow")["allowed"] is False
    assert admission_decision(runtime, "daemon")["allowed"] is False
    assert admission_decision(runtime, "subagent-bulk")["allowed"] is False
    for workload in ("interactive", "desktop", "subagent", "fork", "agent-view"):
        decision = admission_decision(runtime, workload)
        assert decision["allowed"] is True
        assert decision["reason"] == "交互与在途任务受保护"


def test_guard_recovers_admission_without_terminating_tasks(tmp_path):
    runtime = _runtime(tmp_path)
    pressure = {
        "available": True,
        "cpu": {"level": "critical"},
        "memory": {"level": "healthy"},
    }
    record_guard_sample(runtime, pressure)
    protecting = record_guard_sample(runtime, pressure)
    assert protecting["deferNewBackground"] is True
    recovered = record_guard_sample(runtime, {
        "available": True,
        "cpu": {"level": "healthy"},
        "memory": {"level": "healthy"},
    })

    assert recovered["deferNewBackground"] is False
    assert recovered["lastAction"] == "已恢复后台任务准入"
    assert admission_decision(runtime, "workflow")["allowed"] is True


def test_single_cpu_spike_is_debounced_before_deferring_background(tmp_path):
    runtime = _runtime(tmp_path)

    state = record_guard_sample(runtime, {
        "cpu": {"level": "critical"},
        "memory": {"level": "healthy"},
    })

    assert state["rawLevels"]["cpu"] == "critical"
    assert state["levels"]["cpu"] == "warning"
    assert state["pressureStreaks"]["cpuCritical"] == 1
    assert state["deferNewBackground"] is False


def test_any_critical_fixed_disk_defers_background_work(tmp_path):
    runtime = _runtime(tmp_path)
    state = record_guard_sample(runtime, {
        "available": True,
        "cpu": {"level": "healthy"},
        "memory": {"level": "healthy"},
    }, [
        {"drive": "C:", "available": True, "totalBytes": 1000 * 1024**3,
         "freeBytes": 200 * 1024**3, "usedPercent": 80},
        {"drive": "E:", "available": True, "totalBytes": 300 * 1024**3,
         "freeBytes": 8 * 1024**3, "usedPercent": 97.3},
    ])

    assert state["levels"]["disk"] == "critical"
    assert [(item["drive"], item["level"]) for item in state["drives"]] == [
        ("C:", "healthy"),
        ("E:", "critical"),
    ]
    assert state["drives"][1]["mode"] == "critical"
    assert state["drives"][1]["criticalBytes"] > 0
    assert "E:" in state["reason"]
    assert admission_decision(runtime, "workflow")["allowed"] is False
    assert admission_decision(runtime, "interactive")["allowed"] is True


def test_stale_guard_sample_defers_background_only(tmp_path):
    runtime = _runtime(tmp_path)
    path = guard_state_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "sampledEpoch": time.time() - 300,
        "deferNewBackground": True,
    }), encoding="utf-8")

    decision = admission_decision(runtime, "workflow")

    assert decision["allowed"] is False
    assert decision["stale"] is True
    assert admission_decision(runtime, "interactive")["allowed"] is True


def test_unknown_sample_never_reports_healthy(tmp_path):
    runtime = _runtime(tmp_path)
    state = record_guard_sample(runtime, {"available": False})
    assert state["deferNewBackground"] is True
    assert state["status"] == "protecting"
    assert admission_decision(runtime, "workflow")["allowed"] is False
    assert admission_decision(runtime, "interactive")["allowed"] is True


def test_old_sampler_unknown_levels_cannot_bypass_admission(tmp_path):
    runtime = _runtime(tmp_path)
    guard_state_path(runtime).write_text(json.dumps({
        "sampledEpoch": time.time(), "deferNewBackground": False,
        "levels": {"cpu": "unknown", "memory": "unknown"},
    }), encoding="utf-8")
    assert admission_decision(runtime, "workflow")["allowed"] is False
    assert admission_decision(runtime, "fork")["allowed"] is True


def test_predictive_disk_warning_does_not_interrupt_current_or_defer_work(tmp_path):
    runtime = _runtime(tmp_path)
    now = time.time()
    path = guard_state_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "drives": [{
            "drive": "E:",
            "level": "healthy",
            "samples": [
                {"at": now - 120, "freeBytes": 84 * 1024**3},
                {"at": now - 60, "freeBytes": 78 * 1024**3},
            ],
        }],
    }), encoding="utf-8")

    state = record_guard_sample(runtime, {
        "cpu": {"level": "healthy"},
        "memory": {"level": "healthy"},
    }, {
        "drive": "E:", "available": True, "totalBytes": 300 * 1024**3,
        "freeBytes": 72 * 1024**3, "usedPercent": 76.0,
    })

    assert state["levels"]["disk"] == "warning"
    assert state["drives"][0]["mode"] == "predictive"
    assert state["deferNewBackground"] is False


def test_maintenance_pressure_level_clears_stale_guard_warning(tmp_path):
    runtime = _runtime(tmp_path)
    path = guard_state_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "drives": [{"drive": "D:", "level": "warning", "samples": []}],
    }), encoding="utf-8")

    state = record_guard_sample(runtime, {
        "cpu": {"level": "healthy"},
        "memory": {"level": "healthy"},
    }, {
        "drive": "D:", "available": True, "totalBytes": 300 * 1024**3,
        "freeBytes": 65 * 1024**3, "usedPercent": 78.3,
        "pressureLevel": "healthy",
    })

    assert state["drives"][0]["level"] == "healthy"
    assert state["drives"][0]["mode"] == "observe"
