from unittest.mock import patch

import pytest

from claude_fleet.desktop import _ensure_service_port_available
from claude_fleet.desktop import _existing_service_ready
from claude_fleet.desktop import _recover_owned_unhealthy_listener


def test_start_services_reuses_recovered_listener(tmp_path):
    from contextlib import nullcontext
    from claude_fleet import desktop

    runtime = {"db_path": str(tmp_path / "fleet.db")}
    status = {"coordinator": True, "dataplane": False,
              "coordinator_url": "http://127.0.0.1:17893/health",
              "dataplane_url": "http://127.0.0.1:13457/teamclaude/ready"}
    with patch.object(desktop.os, "name", "nt"), \
         patch.object(desktop, "load_runtime", return_value=runtime), \
         patch.object(desktop, "_startup_lock", return_value=nullcontext()), \
         patch.object(desktop, "service_status", return_value=status), \
         patch.object(desktop, "_read_json", return_value={}), \
         patch.object(desktop, "service_commands", return_value={}), \
         patch.object(desktop, "local_port_ready", return_value=True), \
         patch.object(desktop, "_url_ready", side_effect=[False, True]), \
         patch.object(desktop.time, "sleep"), \
         patch.object(desktop, "_spawn_hidden") as spawn:
        desktop.start_services()
        spawn.assert_not_called()


def test_busy_listener_recovers_without_spawn():
    with patch("claude_fleet.desktop.local_port_ready", return_value=True), \
         patch("claude_fleet.desktop._url_ready", side_effect=[False, True]) as health, \
         patch("claude_fleet.desktop.time.sleep") as sleep, \
         patch("claude_fleet.desktop._spawn_hidden") as spawn:
        assert _existing_service_ready("http://127.0.0.1:13457/teamclaude/ready")
        assert health.call_count == 2
        assert health.call_args.kwargs == {"timeout": 3.0}
        sleep.assert_called_once_with(1)
        spawn.assert_not_called()


def test_persistently_unhealthy_listener_still_refused():
    with patch("claude_fleet.desktop.local_port_ready", return_value=True), \
         patch("claude_fleet.desktop._url_ready", return_value=False) as health, \
         patch("claude_fleet.desktop.time.sleep") as sleep:
        with pytest.raises(RuntimeError, match="no duplicate process started"):
            _existing_service_ready("http://127.0.0.1:13457/teamclaude/ready")
        assert health.call_count == 3
        assert [c.args for c in sleep.call_args_list] == [(1,), (2,)]


def test_missing_listener_does_not_wait():
    with patch("claude_fleet.desktop._service_listener_present", return_value=False), \
         patch("claude_fleet.desktop._url_ready") as health:
        assert not _existing_service_ready("http://127.0.0.1:17893/health")
        health.assert_not_called()


def test_listener_disappears_during_retry():
    with patch("claude_fleet.desktop._service_listener_present", side_effect=[True, False]), \
         patch("claude_fleet.desktop._url_ready", return_value=False), \
         patch("claude_fleet.desktop.time.sleep"):
        assert not _existing_service_ready("http://127.0.0.1:17893/health")


def test_unhealthy_listener_is_not_replaced():
    with patch("claude_fleet.desktop.local_port_ready", return_value=True) as probe:
        with pytest.raises(RuntimeError, match="no duplicate process started"):
            _ensure_service_port_available("http://127.0.0.1:13457/teamclaude/ready")
        probe.assert_called_once_with("127.0.0.1", 13457)


def test_absent_listener_can_start():
    with patch("claude_fleet.desktop._service_listener_present", return_value=False):
        _ensure_service_port_available("http://127.0.0.1:17893/health")


def test_bound_listener_refuses_duplicate_when_connect_probe_fails():
    from claude_fleet import desktop

    with patch.object(desktop, "_service_listener_present", return_value=True), \
         patch.object(desktop, "_url_ready", return_value=False), \
         patch.object(desktop.time, "sleep"):
        with pytest.raises(RuntimeError, match="no duplicate process started"):
            _existing_service_ready("http://127.0.0.1:17893/health")


def test_windows_bound_socket_is_seen_when_connect_probe_fails():
    from claude_fleet import desktop

    if desktop.os.name != "nt":
        pytest.skip("Windows listener ownership probe")
    with patch.object(desktop, "local_port_ready", return_value=False), \
         patch.object(desktop, "_listener_pid", return_value=1234):
        assert desktop._service_listener_present("http://127.0.0.1:17893/health")


def test_owned_unhealthy_listener_needs_two_observations(tmp_path):
    from claude_fleet import desktop

    runtime = {"db_path": str(tmp_path / "fleet.sqlite3")}
    record = {
        "pid": 1234, "created_at": 100.0, "executable": "node.exe",
        "service": "dataplane", "port": 13457,
    }
    command = ["node.exe", "index.js", "server", "--headless"]
    with patch.object(desktop, "_owned_listener_record", return_value=record), \
         patch.object(desktop.os, "kill") as kill, \
         patch.object(desktop, "local_port_ready", return_value=False):
        with pytest.raises(RuntimeError, match="confirmation 1/2"):
            _recover_owned_unhealthy_listener(
                runtime, "dataplane", "http://127.0.0.1:13457/ready",
                command, now=1000,
            )
        result = _recover_owned_unhealthy_listener(
            runtime, "dataplane", "http://127.0.0.1:13457/ready",
            command, now=1031,
        )
    assert result["restarted"] is True
    kill.assert_called_once_with(1234, desktop.signal.SIGTERM)


def test_foreign_unhealthy_listener_is_never_terminated(tmp_path):
    from claude_fleet import desktop

    runtime = {"db_path": str(tmp_path / "fleet.sqlite3")}
    with patch.object(desktop, "_owned_listener_record", return_value=None), \
         patch.object(desktop.os, "kill") as kill:
        with pytest.raises(RuntimeError, match="ownership could not be proven"):
            _recover_owned_unhealthy_listener(
                runtime, "dataplane", "http://127.0.0.1:13457/ready",
                ["node.exe", "index.js", "server"], now=1000,
            )
    kill.assert_not_called()


def test_python_launcher_family_can_own_exact_service_command():
    from claude_fleet import desktop

    command = [
        r"C:\QuotaPool\venv\Scripts\python.exe",
        "-m", "claude_fleet.cli", "--db", r"C:\QuotaPool\fleet.db",
        "coordinator", "--host", "127.0.0.1", "--port", "17893",
    ]
    command_line = (
        r'"C:\QuotaPool\venv\Scripts\pythonw.exe" -m claude_fleet.cli '
        r'--db C:\QuotaPool\fleet.db coordinator --host 127.0.0.1 --port 17893'
    )
    with patch.object(desktop, "_listener_pid", return_value=1234), \
         patch.object(desktop, "_process_image", return_value=r"C:\Python\pythonw.exe"), \
         patch.object(desktop, "_process_command_line", return_value=command_line), \
         patch.object(desktop, "_process_created_at", return_value=100.0):
        record = desktop._owned_listener_record(
            "coordinator", "http://127.0.0.1:17893/health", command
        )

    assert record is not None
    assert record["pid"] == 1234


def test_stop_services_preserves_unrelated_dashboard_record(tmp_path):
    from contextlib import nullcontext
    from claude_fleet import desktop

    runtime = {"db_path": str(tmp_path / "fleet.sqlite3")}
    pid_file = tmp_path / "desktop-services.json"
    records = {
        "dataplane": {"pid": 11},
        "coordinator": {"pid": 12},
        "dashboard": {"pid": 13, "executable": "pythonw.exe"},
    }
    with patch.object(desktop.os, "name", "nt"), \
         patch.object(desktop, "load_runtime", return_value=runtime), \
         patch.object(desktop, "_startup_lock", return_value=nullcontext()), \
         patch.object(desktop, "_pid_file", return_value=pid_file), \
         patch.object(desktop, "_read_json", return_value=records), \
         patch.object(desktop, "_record_matches_process", return_value=True), \
         patch.object(desktop.os, "kill") as kill, \
         patch.object(desktop, "_private_json") as write:
        result = desktop.stop_services()

    assert result == {"stopped": ["dataplane", "coordinator"], "skipped": []}
    assert kill.call_count == 2
    write.assert_called_once_with(pid_file, records)
    assert records == {"dashboard": {"pid": 13, "executable": "pythonw.exe"}}
