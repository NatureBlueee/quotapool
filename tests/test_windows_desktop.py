from __future__ import annotations

import json
import os
from contextlib import nullcontext
from pathlib import Path

from claude_fleet import dashboard, desktop
from claude_fleet.desktop import (
    current_desktop_claude,
    desktop_launch_env,
    discover_desktop_claude,
    hidden_subprocess_kwargs,
    inspect_desktop,
    normalize_desktop_model_settings,
    service_commands,
)


def _version(root: Path, name: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "claude.exe").write_bytes(b"not-an-executable-for-discovery")
    return directory


def test_desktop_code_versions_are_discovered_newest_first(tmp_path):
    root = tmp_path / "Claude" / "claude-code"
    _version(root, "2.1.9")
    _version(root, "2.1.229")
    _version(root, "2.1.230-beta")

    versions = discover_desktop_claude([root])

    assert [item["version"] for item in versions] == [
        "2.1.230-beta", "2.1.229", "2.1.9"
    ]
    assert current_desktop_claude([root])["version"] == "2.1.230-beta"


def test_desktop_code_discovery_handles_build_hash_directories(tmp_path):
    root = tmp_path / "Claude" / "claude-code"
    _version(root, "2.1.286")
    nested = _version(root / "2.1.288", "36aa8c97bf86")
    # Do not recurse into unrelated deeper data directories.
    _version(root / "2.1.999" / "cache", "unrelated")

    installs = discover_desktop_claude([root])

    assert [item["version"] for item in installs] == ["2.1.288", "2.1.286"]
    assert current_desktop_claude([root])["binary"] == str(nested / "claude.exe")


def test_desktop_inspect_is_read_only(tmp_path):
    runtime = tmp_path / "runtime.json"
    settings = tmp_path / "settings.json"
    runtime.write_text(json.dumps({"desktop_wrapper": "C:/wrapper.exe"}))
    settings.write_text(json.dumps({"processWrapper": "C:/wrapper.exe"}))
    root = tmp_path / "Claude" / "claude-code"
    _version(root, "2.1.229")

    report = inspect_desktop(
        runtime_path=str(runtime), settings_path=str(settings), roots=[root]
    )

    assert report["desktop_claude"]["version"] == "2.1.229"
    assert report["process_wrapper_is_quotapool"] is True
    assert runtime.read_text() == json.dumps({"desktop_wrapper": "C:/wrapper.exe"})
    assert settings.read_text() == json.dumps({"processWrapper": "C:/wrapper.exe"})


def test_windows_service_commands_use_managed_runtime_paths(tmp_path):
    runtime = {
        "node_binary": r"C:\Program Files\nodejs\node.exe",
        "prefix": str(tmp_path),
        "db_path": str(tmp_path / "state" / "fleet.sqlite3"),
        "coordinator_port": 27891,
        "proxy_port": 23456,
    }

    commands = service_commands(runtime, python_executable=r"C:\Python\python.exe")

    assert commands["coordinator"][:4] == [
        r"C:\Python\python.exe", "-m", "claude_fleet.cli", "--db"
    ]
    assert commands["coordinator"][-1] == "27891"
    assert commands["dataplane"][-2:] == ["server", "--headless"]
    assert commands["dataplane"][-3].endswith("vendor\\teamclaude\\src\\index.js")


def test_windows_console_probes_are_created_without_a_visible_window():
    options = hidden_subprocess_kwargs()

    assert options["creationflags"] != 0
    assert options["startupinfo"].dwFlags != 0


def test_desktop_launch_env_routes_first_local_session_through_pool():
    runtime = {
        "proxy_port": 13457,
        "desktop_wrapper": r"C:\QuotaPool\wrapper.exe",
        "shim_dir": r"C:\QuotaPool\shims",
        "hold_seconds": 21600,
    }

    env = desktop_launch_env(
        runtime,
        {
            "PATH": r"C:\Windows\System32",
            "CLAUDE_CODE_OAUTH_TOKEN": "real-desktop-token",
            "HTTPS_PROXY": "http://127.0.0.1:7897",
        },
    )

    assert env["HTTPS_PROXY"].endswith("@127.0.0.1:13457")
    assert env["HTTP_PROXY"] == env["HTTPS_PROXY"]
    assert env["CLAUDE_CODE_PROCESS_WRAPPER"] == r"C:\QuotaPool\wrapper.exe"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["FLEET_WORKLOAD_CLASS"] == "desktop"


def test_desktop_launch_uses_normal_network_path(tmp_path, monkeypatch):
    binary = tmp_path / "Claude.exe"
    binary.write_bytes(b"desktop")
    captured = {}

    monkeypatch.setattr(desktop.os, "name", "nt")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:13457")
    monkeypatch.setattr(desktop, "ensure_services", lambda *_: None)
    monkeypatch.setattr(
        desktop,
        "load_runtime",
        lambda *_: {"proxy_port": 13457, "desktop_wrapper": ""},
    )
    monkeypatch.setattr(
        desktop.subprocess,
        "Popen",
        lambda args, **kwargs: captured.update(args=args, kwargs=kwargs) or type("P", (), {"pid": 42})(),
    )

    result = desktop.launch_claude_desktop(executable=str(binary))

    assert captured["args"] == [str(binary)]
    assert "HTTPS_PROXY" not in captured["kwargs"]["env"]
    assert result["proxy_port"] == 13457


def test_dashboard_open_uses_the_appx_discovered_desktop_path(monkeypatch):
    captured = {}

    monkeypatch.setattr(dashboard.shutil, "which", lambda _: "powershell.exe")
    monkeypatch.setattr(
        dashboard,
        "_claude_desktop_info",
        lambda: {"installed": True, "path": r"C:\\Program Files\\WindowsApps\\Claude\\app\\Claude.exe"},
    )
    monkeypatch.setattr(dashboard, "_claude_desktop_processes", lambda _: [])
    monkeypatch.setattr(
        dashboard,
        "launch_claude_desktop",
        lambda runtime_path, *, executable: captured.update(
            runtime_path=runtime_path, executable=executable
        ) or {"started": True},
    )
    monkeypatch.setattr(
        dashboard,
        "_desktop_snapshot",
        lambda **_: {"running": True, "installed": True},
    )

    result = dashboard._start_claude_desktop(runtime_path="C:/runtime.json")

    assert result["ok"] is True
    assert captured == {
        "runtime_path": "C:/runtime.json",
        "executable": r"C:\\Program Files\\WindowsApps\\Claude\\app\\Claude.exe",
    }


def test_desktop_code_shim_install_and_restore_is_reversible(tmp_path, monkeypatch):
    managed = tmp_path / "claude.exe"
    source = tmp_path / "quotapool-desktop-code.exe"
    runtime = tmp_path / "runtime.json"
    managed.write_bytes(b"original-claude")
    source.write_bytes(b"quotapool-shim")
    runtime.write_text(json.dumps({"claude_binary": str(managed)}))
    monkeypatch.setattr(desktop.os, "name", "nt")

    result = desktop.install_desktop_code_shim(
        managed_binary=str(managed),
        shim_source=str(source),
        runtime_path=str(runtime),
    )

    assert managed.read_bytes() == b"quotapool-shim"
    assert Path(result["real_binary"]).read_bytes() == b"original-claude"
    assert json.loads(runtime.read_text())["claude_binary"] == result["real_binary"]

    restored = desktop.uninstall_desktop_code_shim(runtime_path=str(runtime))

    assert restored["restored"] is True
    assert managed.read_bytes() == b"original-claude"
    assert not Path(result["real_binary"]).exists()


def test_explicit_opus_launch_clears_stale_ultracode_model_override():
    arguments = [
        "--model", "claude-opus-5",
        "--settings", '{"ultracode":true,"other":"kept"}',
        "--resume=session-1",
    ]

    normalized = normalize_desktop_model_settings(arguments)

    assert normalized[:2] == ["--model", "claude-opus-5"]
    assert json.loads(normalized[3]) == {
        "ultracode": False,
        "other": "kept",
    }
    assert normalized[4] == "--resume=session-1"


def test_explicit_fable_launch_preserves_ultracode_setting():
    arguments = [
        "--model=claude-fable-5-1",
        '--settings={"ultracode":true}',
    ]

    assert normalize_desktop_model_settings(arguments) == arguments


def test_opus_launch_without_stale_flag_is_unchanged():
    arguments = ["--model", "claude-opus-5", "--settings", "{}"]

    assert normalize_desktop_model_settings(arguments) == arguments


def test_resumed_desktop_session_uses_the_model_currently_saved_in_ui(tmp_path):
    account = "account-1"
    org = "org-1"
    records = tmp_path / "Claude" / "claude-code-sessions" / account / org
    records.mkdir(parents=True)
    (records / "local-1.json").write_text(json.dumps({
        "cliSessionId": "session-1",
        "model": "claude-opus-5",
    }), encoding="utf-8")
    arguments = [
        "--model", "claude-fable-5-1",
        "--resume=session-1",
        "--plugin-dir", str(tmp_path / "skills-plugin" / org / account),
        "--settings", "{}",
    ]

    normalized = normalize_desktop_model_settings(
        arguments,
        environ={"APPDATA": str(tmp_path)},
    )

    assert normalized[1] == "claude-opus-5"


def test_slow_dashboard_is_not_duplicated_while_its_port_listens(tmp_path, monkeypatch):
    runtime = {"db_path": str(tmp_path / "state" / "fleet.sqlite3")}
    readiness = iter([False, True])
    monkeypatch.setattr(desktop, "load_runtime", lambda value: runtime)
    monkeypatch.setattr(desktop, "_startup_lock", lambda value: nullcontext())
    monkeypatch.setattr(desktop, "_url_ready", lambda *args, **kwargs: next(readiness))
    monkeypatch.setattr(desktop, "local_port_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        desktop, "_spawn_hidden",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("duplicate spawned")),
    )

    result = desktop._start_dashboard_background(None, "127.0.0.1", 17892, False)

    assert result["already_running"] is True
