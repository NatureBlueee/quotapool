from __future__ import annotations

import pytest

from claude_fleet import tray
from claude_fleet.tray import status_from_snapshot


def _snapshot(*, accounts, coordinator=True, dataplane=True, waiting=0, host=None):
    return {
        "services": {"coordinator": coordinator, "dataplane": dataplane},
        "dataPlane": {
            "accounts": accounts,
            "currentAccount": "max-2",
            "sessions": {"active": 3},
            "waiting": {"count": waiting},
        },
        "dashboard": {"windowsHost": host or {"isWindows": False}},
    }


def test_tray_badge_is_the_number_of_available_accounts():
    status = status_from_snapshot(_snapshot(accounts=[
        {"name": "max-1", "status": "active", "quota": {"unifiedStatus": "allowed"}},
        {"name": "max-2", "status": "active", "quota": {"unifiedStatus": "allowed"}},
        {"name": "old", "disabled": True},
    ]))

    assert status.level == "ok"
    assert status.badge == "2"
    assert status.available == 2
    assert status.total == 3
    assert status.active == 3
    assert "max-2" in status.tooltip


def test_tray_reports_service_failure_before_account_health():
    status = status_from_snapshot(_snapshot(
        accounts=[{"status": "active"}], dataplane=False,
    ))

    assert status.level == "error"
    assert status.badge == "!"


def test_tray_shows_red_zero_when_no_account_is_configured():
    status = status_from_snapshot(_snapshot(accounts=[]))

    assert status.level == "error"
    assert status.badge == "0"
    assert status.detail == "尚未添加账号"


def test_tray_warns_for_waiting_sessions_or_windows_disk():
    waiting = status_from_snapshot(_snapshot(
        accounts=[{"status": "active"}], waiting=2,
    ))
    disk = status_from_snapshot(_snapshot(
        accounts=[{"status": "active"}],
        host={"isWindows": True, "healthy": False, "refreshing": False},
    ))

    assert waiting.level == "warning"
    assert "2 个会话" in waiting.detail
    assert disk.level == "warning"
    assert "C 盘" in disk.title


def test_tray_handles_unreachable_dashboard():
    status = status_from_snapshot(None, error="connection refused")

    assert status.level == "error"
    assert status.badge == "!"
    assert "connection refused" in status.tooltip


def test_tray_does_not_spawn_a_dashboard_when_the_port_is_listening(monkeypatch):
    monitor = object.__new__(tray.NativeTray)
    monitor.snapshot_url = "http://127.0.0.1:17892/api/snapshot"
    monitor.runtime_path = None
    monitor.host = "127.0.0.1"
    monitor.port = 17892
    monkeypatch.setattr(tray, "_fetch_snapshot", lambda value: (_ for _ in ()).throw(TimeoutError()))
    monkeypatch.setattr(tray, "local_port_ready", lambda *args: True)
    monkeypatch.setattr(
        tray, "_start_dashboard_background",
        lambda *args: (_ for _ in ()).throw(AssertionError("duplicate spawned")),
    )

    with pytest.raises(TimeoutError):
        monitor._fetch_with_dashboard_repair()
