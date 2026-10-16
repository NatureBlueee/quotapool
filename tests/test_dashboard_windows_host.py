from __future__ import annotations

import json
import threading
from pathlib import Path

from claude_fleet import dashboard


def test_wsl_verbose_output_parses_utf16_and_localized_states():
    payload = (
        "  NAME                   STATE           VERSION\r\n"
        "* Ubuntu-22.04           Running         2\r\n"
        "  docker-desktop         Stopped         2\r\n"
    ).encode("utf-16")

    rows = dashboard._parse_wsl_distributions(
        dashboard._decode_windows_output(payload)
    )

    assert rows == [
        {"name": "Ubuntu-22.04", "state": "running", "version": 2, "default": True},
        {"name": "docker-desktop", "state": "stopped", "version": 2, "default": False},
    ]


def test_wsl_verbose_output_accepts_chinese_state_labels():
    rows = dashboard._parse_wsl_distributions(
        "* Ubuntu-22.04    运行中    2\n  docker-desktop    已停止    2\n"
    )

    assert [row["state"] for row in rows] == ["running", "stopped"]


def test_snapshot_exposes_windows_host_without_credentials(tmp_path, monkeypatch):
    team = tmp_path / "teamclaude.json"
    team.write_text(json.dumps({"accounts": []}), encoding="utf-8")
    runtime = {"teamclaude_config": str(team), "db_path": str(tmp_path / "fleet.sqlite3")}
    windows = {
        "isWindows": True,
        "healthy": True,
        "connection": {"host": "kachi@127.0.0.1", "port": 2222},
    }

    monkeypatch.setattr(dashboard, "dataplane_endpoint", lambda value: "http://127.0.0.1:1")
    monkeypatch.setattr(dashboard, "_local_json", lambda *args, **kwargs: {})
    monkeypatch.setattr(dashboard, "service_status", lambda value: {})
    monkeypatch.setattr(dashboard, "_fleet_activity", lambda value: {"records": [], "total": 0, "active": 0})
    monkeypatch.setattr(dashboard, "_desktop_snapshot", lambda: {"installed": True})
    monkeypatch.setattr(dashboard, "_windows_host_snapshot", lambda value: windows)

    result = dashboard._snapshot(runtime)

    assert result["dashboard"]["windowsHost"] == windows
    assert "token" not in json.dumps(result).casefold()


def test_claude_ssh_connection_ignores_secrets_and_invalid_port(tmp_path, monkeypatch):
    claude = tmp_path / "Claude"
    claude.mkdir()
    (claude / "ssh_configs.json").write_text(json.dumps({
        "configs": [{
            "name": "Local WSL",
            "sshHost": "dev@127.0.0.1",
            "sshPort": "not-a-port",
            "sshIdentityFile": str(tmp_path / "identity"),
            "accessToken": "must-never-leak",
        }]
    }), encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path))

    connection = dashboard._claude_ssh_connection()

    assert connection["host"] == "dev@127.0.0.1"
    assert connection["port"] == 2222
    assert "accessToken" not in connection
    assert "must-never-leak" not in json.dumps(connection)


def test_dashboard_contains_storage_health_panel_without_wsl_connection_ui():
    html = Path(dashboard.__file__).with_name("dashboard.html").read_text(encoding="utf-8")

    for marker in (
        'id="windows-section"',
        "存储健康",
        'id="disk-health-state"',
        'id="disk-maintenance-state"',
        'id="win-space-bar"',
        'id="disk-maintenance-drive"',
        'id="disk-maintenance-run"',
        "/api/disk-maintenance/run",
        "主动维护",
        "自动维护会处理什么？",
    ):
        assert marker in html
    for removed in ('id="copy-ssh"', 'id="ssh-state"', "Windows 如何连接 Claude Desktop → WSL"):
        assert removed not in html


def test_home_health_badge_explains_and_navigates_to_the_attention_area():
    html = Path(dashboard.__file__).with_name("dashboard.html").read_text(encoding="utf-8")

    assert 'id="system-health" type="button"' in html
    assert "healthAction" in html
    assert "diskWorst?.drive" in html
    assert "dataset.targetView" in html
    assert "activateView(view)" in html
    assert "有一项需要关注" not in html


def test_drive_cards_distinguish_capacity_and_predictive_alerts():
    html = Path(dashboard.__file__).with_name("dashboard.html").read_text(encoding="utf-8")

    assert "driveAlertKind" in html
    assert "趋势预警" in html
    assert "容量告警" in html
    assert "容量临界" in html
    assert "drive-alert" in html


def test_dashboard_contains_persistent_dark_mode_controls():
    html = Path(dashboard.__file__).with_name("dashboard.html").read_text(encoding="utf-8")

    assert 'id="theme-toggle"' in html
    assert 'data-theme="dark"' in html
    assert "quotapool-theme" in html
    assert "prefers-color-scheme: dark" in html


def test_resource_snapshot_groups_sessions_builds_and_services(tmp_path, monkeypatch):
    pid_file = tmp_path / "desktop-services.json"
    pid_file.write_text(json.dumps({
        "coordinator": {"pid": 101},
        "dataplane": {"pid": 102},
    }), encoding="utf-8")
    monkeypatch.setattr(dashboard, "_resource_probe_windows", lambda: {
        "available": True,
        "cpuPercent": 84,
        "logicalCpus": 8,
        "memoryTotalBytes": 16 * 1024**3,
        "memoryFreeBytes": 3 * 1024**3,
        "fixedDisks": [
            {"drive": "C:", "label": "OS", "fileSystem": "NTFS",
             "totalBytes": 1000 * 1024**3, "freeBytes": 200 * 1024**3},
            {"drive": "E:", "label": "Project", "fileSystem": "NTFS",
             "totalBytes": 300 * 1024**3, "freeBytes": 8 * 1024**3},
        ],
        "processes": [
            {"pid": 101, "name": "python", "workingSetBytes": 100, "cpuSeconds": 1},
            {"pid": 102, "name": "node", "workingSetBytes": 200, "cpuSeconds": 2},
            {"pid": 201, "name": "claude.quotapool-real", "workingSetBytes": 300, "cpuSeconds": 3},
            {"pid": 301, "name": "rustc", "workingSetBytes": 400, "cpuSeconds": 4},
        ],
    })
    monkeypatch.setattr(dashboard, "_RESOURCE_CPU_SAMPLE", (0.0, {}))

    result = dashboard._resource_snapshot({"desktop_pid_file": str(pid_file)})

    groups = {item["key"]: item for item in result["groups"]}
    assert result["available"] is True
    assert result["overall"] == "warning"
    assert groups["quotapool"]["processCount"] == 2
    assert groups["claude-code"]["processCount"] == 1
    assert groups["build"]["processCount"] == 1
    assert result["policy"]["activeTasksProtected"] is True
    assert result["policy"]["automaticProcessTermination"] is False
    assert [item["drive"] for item in result["disks"]] == ["C:", "E:"]
    assert result["disks"][0]["maintenance"] == "automatic-safe-cache"
    assert result["disks"][1]["maintenance"] == "automatic-safe-cache"
    assert all(item["protectedProjectData"] for item in result["disks"])


def test_dashboard_contains_resource_management_view():
    html = Path(dashboard.__file__).with_name("dashboard.html").read_text(encoding="utf-8")

    for marker in (
        'data-nav="resources"',
        'id="resources-section"',
        'id="resource-cpu"',
        'id="resource-memory"',
        'id="resource-disk"',
        'id="resource-services"',
        'id="resource-top"',
        'id="fixed-drives"',
        'id="fixed-drive-count"',
        "function renderResources",
        "function renderFixedDrives",
        "所有固定盘自动维护白名单旧缓存",
        "按趋势预测并自动维护",
        "function fmtEta(v){if(v==null",
        "自动守护",
        "活跃任务受保护",
        'id="worktree-policy"',
        "function renderWorktreePolicy",
        "分支不会删除",
    ):
        assert marker in html


def test_dashboard_displays_fable_7d_weekly_quota_and_reset_countdown():
    html = Path(dashboard.__file__).with_name("dashboard.html").read_text(encoding="utf-8")

    assert "Fable 7D" in html
    assert "FABLE 5" not in html
    assert "unified7dFable" in html
    assert "unified7dFableReset" in html
    assert "function fmtResetCountdown" in html
    assert "remaining>=2*day" in html
    assert "还剩 ${Math.floor(remaining/hour)}小时" in html
    assert "还剩 ${Math.max(1,Math.ceil(remaining/minute))}分钟" in html
    assert "resetText=available?`重置 ${exact} · ${countdown}`" in html
    assert "function resetCountdownTone" in html
    assert "remaining<=24*hour" in html
    assert "remaining<48*hour" in html
    assert 'class="reset-countdown ${tone}"' in html


def test_many_accounts_use_a_dense_searchable_card_grid():
    html = Path(dashboard.__file__).with_name("dashboard.html").read_text(encoding="utf-8")

    for marker in (
        'id="account-search"',
        'id="account-list-count"',
        "repeat(auto-fill,minmax(300px,1fr))",
        'class="quota-grid"',
        "quotaTile('Fable 7D'",
        "renderAccounts(acc,cfg)",
    ):
        assert marker in html


def test_sidebar_navigation_switches_between_overview_and_detail_views():
    html = Path(dashboard.__file__).with_name("dashboard.html").read_text(encoding="utf-8")

    for marker in (
        'data-nav="overview"',
        'data-nav="accounts"',
        'data-nav="windows"',
        'data-nav="activity"',
        'data-nav="routes"',
        'data-view="overview"',
        'data-view="accounts" hidden',
        'data-view="windows" hidden',
        'data-view="activity" hidden',
        'data-view="routes" hidden',
        "function activateView",
        "function viewFromHash",
        "history.pushState",
    ):
        assert marker in html


def test_overview_lists_every_account_with_all_three_quota_windows():
    html = Path(dashboard.__file__).with_name("dashboard.html").read_text(encoding="utf-8")

    for marker in (
        'id="overview-accounts"',
        'id="overview-account-count"',
        'data-go-view="accounts"',
        "function renderOverviewAccounts",
        "5H 剩余",
        "7D 剩余",
        "Fable 7D",
    ):
        assert marker in html


def test_add_account_uses_an_inline_three_step_authorization_flow():
    html = Path(dashboard.__file__).with_name("dashboard.html").read_text(encoding="utf-8")

    for marker in (
        'id="add-account-modal"',
        'id="add-account-form"',
        'id="add-account-name"',
        'id="account-login-link"',
        'id="auth-progress-name"',
        'id="auth-progress-authorize"',
        'id="auth-progress-sync"',
        'id="auth-pane-syncing"',
        'id="cancel-account-login"',
        'id="check-account-login"',
        "继续授权",
        "accountIsSynced",
        "无需刷新页面，账号卡片会自动出现",
    ):
        assert marker in html
    assert "prompt(" not in html
    assert "open('about:blank'" not in html


def test_dashboard_can_cancel_an_active_account_authorization():
    class LoginProcess:
        def __init__(self):
            self.running = True
            self.terminated = False

        def poll(self):
            return None if self.running else 1

        def terminate(self):
            self.terminated = True
            self.running = False

        def wait(self, timeout):
            assert timeout == 3
            return 1

    server = object.__new__(dashboard.DashboardServer)
    server._login_lock = threading.Lock()
    process = LoginProcess()
    server._login = {"status": "waiting", "name": "max-3", "process": process}

    result = server.cancel_login()

    assert process.terminated is True
    assert result == {"ok": True, "name": "max-3", "status": "cancelled"}
    assert server.login_state() == {
        "status": "cancelled",
        "name": "max-3",
        "message": "账号授权已取消",
    }


def test_windows_dashboard_does_not_reuse_an_occupied_port():
    assert dashboard.DashboardServer.allow_reuse_address is False
