from __future__ import annotations

import json
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from claude_fleet import wsl_guard


@pytest.fixture(autouse=True)
def _isolate_fixed_drive_discovery(monkeypatch):
    """Unit tests must never inspect or clean the host's real fixed disks."""
    monkeypatch.setattr(wsl_guard, "fixed_disk_snapshots", lambda: [])


def test_run_hidden_suppresses_all_child_output(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(wsl_guard.subprocess, "run", fake_run)
    monkeypatch.setattr(
        wsl_guard, "hidden_subprocess_kwargs", lambda: {"creationflags": 123}
    )

    assert wsl_guard.run_hidden(["probe.exe"]) == 0
    assert captured["creationflags"] == 123
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.DEVNULL


def test_healthy_watchdog_does_not_retrigger_keepalive(monkeypatch):
    commands = []
    monkeypatch.setattr(
        wsl_guard, "run_hidden", lambda command, timeout=None: commands.append(command) or 0
    )
    monkeypatch.setattr(wsl_guard, "ssh_banner_ready", lambda port: True)

    result = wsl_guard.watchdog("Ubuntu-Test", 2222, "Keepalive Test")

    assert result == 0
    assert commands == []


def test_keepalive_does_not_start_wsl_under_disk_pressure(monkeypatch):
    commands = []
    monkeypatch.setattr(wsl_guard, "host_disk_has_headroom", lambda: False)
    monkeypatch.setattr(
        wsl_guard, "run_hidden", lambda command, timeout=None: commands.append(command) or 0
    )

    assert wsl_guard.keepalive("Ubuntu-Test") == wsl_guard.LOW_DISK_EXIT
    assert commands == []


def test_watchdog_does_not_start_wsl_under_disk_pressure(monkeypatch):
    commands = []
    monkeypatch.setattr(wsl_guard, "ssh_banner_ready", lambda port: False)
    monkeypatch.setattr(wsl_guard, "host_disk_has_headroom", lambda: False)
    monkeypatch.setattr(
        wsl_guard, "run_hidden", lambda command, timeout=None: commands.append(command) or 0
    )

    result = wsl_guard.watchdog("Ubuntu-Test", 2222, "Keepalive Test")

    assert result == wsl_guard.LOW_DISK_EXIT
    assert commands == []


def test_trim_does_not_boot_stopped_wsl_under_disk_pressure(monkeypatch):
    commands = []
    monkeypatch.setattr(wsl_guard, "host_disk_has_headroom", lambda: False)
    monkeypatch.setattr(wsl_guard, "tcp_ready", lambda port: False)
    monkeypatch.setattr(
        wsl_guard, "run_hidden", lambda command, timeout=None: commands.append(command) or 0
    )

    assert wsl_guard.trim("Ubuntu-Test", 2222) == wsl_guard.LOW_DISK_EXIT
    assert commands == []


def test_trim_runs_for_a_reachable_distro_under_disk_pressure(monkeypatch):
    commands = []
    monkeypatch.setattr(wsl_guard, "host_disk_has_headroom", lambda: False)
    monkeypatch.setattr(wsl_guard, "tcp_ready", lambda port: True)
    monkeypatch.setattr(
        wsl_guard, "run_hidden", lambda command, timeout=None: commands.append((command, timeout)) or 0
    )

    assert wsl_guard.trim("Ubuntu-Test", 2222) == 0
    assert commands[0][1] == 600
    assert commands[0][0][-2:] == ["-lc", "sync; /usr/sbin/fstrim -av"]


def test_watchdog_does_not_restart_an_open_listener_without_a_banner(monkeypatch):
    commands = []
    monkeypatch.setattr(
        wsl_guard, "run_hidden", lambda command, timeout=None: commands.append(command) or 0
    )
    monkeypatch.setattr(wsl_guard, "ssh_banner_ready", lambda port: False)
    monkeypatch.setattr(wsl_guard, "host_disk_has_headroom", lambda: True)
    monkeypatch.setattr(wsl_guard, "tcp_ready", lambda port: True)
    monkeypatch.setattr(wsl_guard, "established_ssh_clients", lambda port: 1)

    result = wsl_guard.watchdog("Ubuntu-Test", 2222, "Keepalive Test")

    assert result == 2
    assert len(commands) == 1


def test_watchdog_repairs_a_wedged_listener_after_clients_disconnect(monkeypatch):
    commands = []
    readiness = iter([False, True])
    monkeypatch.setattr(
        wsl_guard, "run_hidden", lambda command, timeout=None: commands.append(command) or 0
    )
    monkeypatch.setattr(wsl_guard, "ssh_banner_ready", lambda port: False)
    monkeypatch.setattr(wsl_guard, "host_disk_has_headroom", lambda: True)
    monkeypatch.setattr(wsl_guard, "tcp_ready", lambda port: True)
    monkeypatch.setattr(wsl_guard, "established_ssh_clients", lambda port: 0)
    monkeypatch.setattr(
        wsl_guard, "wait_ssh_banner", lambda port, seconds: next(readiness)
    )

    result = wsl_guard.watchdog("Ubuntu-Test", 2222, "Keepalive Test")

    assert result == 0
    assert commands[1][-3:] == ["/usr/bin/systemctl", "restart", "ssh"]


def test_watchdog_repairs_ssh_after_keepalive_start(monkeypatch):
    commands = []
    readiness = iter([False, True])
    monkeypatch.setattr(
        wsl_guard, "run_hidden", lambda command, timeout=None: commands.append(command) or 0
    )
    monkeypatch.setattr(wsl_guard, "ssh_banner_ready", lambda port: False)
    monkeypatch.setattr(wsl_guard, "host_disk_has_headroom", lambda: True)
    monkeypatch.setattr(
        wsl_guard, "wait_ssh_banner", lambda port, seconds: next(readiness)
    )
    monkeypatch.setattr(wsl_guard, "tcp_ready", lambda port: False)

    result = wsl_guard.watchdog("Ubuntu-Test", 2222, "Keepalive Test")

    assert result == 0
    assert len(commands) == 2
    assert commands[1][-3:] == ["/usr/bin/systemctl", "restart", "ssh"]


def test_reclaim_paths_are_restricted_to_rust_target_directories():
    assert wsl_guard._valid_reclaim_pattern("/home/kachi/projects/evox/target")
    assert wsl_guard._valid_reclaim_pattern("/home/kachi/projects/.evox-target-shared")
    assert wsl_guard._valid_reclaim_pattern("/home/kachi/.cargo-target-shared")
    assert wsl_guard._valid_reclaim_pattern("/home/kachi/.cargo-shared-target")
    assert not wsl_guard._valid_reclaim_pattern("/home/kachi/projects/evox")
    assert not wsl_guard._valid_reclaim_pattern("/home/kachi/projects/evox/../models/target")
    assert not wsl_guard._valid_reclaim_pattern("C:\\Users\\target")


def test_reclaim_protects_only_the_target_used_by_a_live_build(monkeypatch):
    captured = {}

    def fake_capture(command, timeout=None):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, "SKIP_ACTIVE\t/tmp/app/target\n", "")

    monkeypatch.setattr(wsl_guard, "run_capture", fake_capture)

    result = wsl_guard._wsl_reclaim_targets(
        "Ubuntu-Test", ["/tmp/app/target", "/tmp/old/target"]
    )

    script = captured["command"][captured["command"].index("-c") + 1]
    assert "SKIP_BUILD" not in script
    assert "/proc/[0-9]*/fd/*" in script
    assert "CARGO_TARGET_DIR" in script
    assert result["status"] == "completed"
    assert result["skippedActive"] == ["/tmp/app/target"]


def test_reclaim_falls_back_to_wsl_when_ssh_transport_fails(tmp_path, monkeypatch):
    identity = tmp_path / "id"
    identity.write_text("test", encoding="utf-8")
    direct_calls = []
    monkeypatch.setattr(wsl_guard, "tcp_ready", lambda port: True)
    monkeypatch.setattr(wsl_guard, "host_disk_has_headroom", lambda: True)
    monkeypatch.setattr(
        wsl_guard,
        "_ssh_maintenance_capture",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 255, "", "ssh failed"),
    )

    def fake_capture(command, timeout=None):
        direct_calls.append(command)
        return subprocess.CompletedProcess(command, 0, "TOTAL\t0\n", "")

    monkeypatch.setattr(wsl_guard, "run_capture", fake_capture)

    result = wsl_guard._wsl_reclaim_targets(
        "Ubuntu-Test", ["/tmp/app/target"], 2222, "kachi", str(identity)
    )

    assert direct_calls
    assert result["status"] == "completed"
    assert result["transport"] == "wsl"


def test_disk_guard_reclaims_before_the_host_becomes_critical():
    assert wsl_guard.DEFAULT_MAINTENANCE_TRIGGER_GB == 128.0
    assert wsl_guard.DEFAULT_RECLAIM_TRIGGER_GB == 128.0


def test_safe_windows_cache_cleanup_is_narrow_and_age_bounded(tmp_path):
    local = tmp_path / "Local"
    windows_temp = tmp_path / "Windows" / "Temp"
    old_build = windows_temp / "evox-old-build"
    recent_build = windows_temp / "evox-active-build"
    unrelated = windows_temp / "important-project"
    for root in (old_build, recent_build, unrelated, local / "npm-cache" / "_cacache"):
        root.mkdir(parents=True)
        (root / "payload.bin").write_bytes(b"cache")
    old = time.time() - 8 * 3600
    os.utime(old_build, (old, old))

    actions = wsl_guard.clean_safe_windows_caches(local, windows_temp)

    assert not old_build.exists()
    assert recent_build.exists()
    assert unrelated.exists()
    assert any(action["name"] == "windows-build-temp" for action in actions)


def test_fixed_drive_cleanup_is_allowlisted_age_bounded_and_project_safe(tmp_path, monkeypatch):
    drive = tmp_path / "E"
    old_cache = drive / "go-build-cache" / "old.bin"
    recent_cache = drive / "go-build-cache" / "active.bin"
    project_file = drive / "EvoMap_projects" / "evox" / "target" / "artifact.o"
    quarantine = drive / "CDriveCleanupQuarantine" / "old-backup.bin"
    old_temp = drive / "tmp" / "old-demo" / "node_modules" / "package.bin"
    old_overflow = drive / "codex-build-overflow" / "debug" / "artifact.pdb"
    recent_temp = drive / "tmp" / "current-task.log"
    for item in (
        old_cache,
        recent_cache,
        project_file,
        quarantine,
        old_temp,
        old_overflow,
        recent_temp,
    ):
        item.parent.mkdir(parents=True, exist_ok=True)
        item.write_bytes(b"reproducible")
    old = time.time() - 40 * 86400
    for item in (old_cache, quarantine, project_file, old_temp, old_overflow):
        os.utime(item, (old, old))
    old_temp.chmod(0o444)
    monkeypatch.setattr(
        wsl_guard.shutil,
        "disk_usage",
        lambda root: SimpleNamespace(total=100, used=99, free=1),
    )

    actions = wsl_guard.clean_safe_fixed_drive_caches([{
        "drive": "E:",
        "root": drive,
        "usage": SimpleNamespace(total=100, used=99, free=1),
        "level": "critical",
    }])

    assert not old_cache.exists()
    assert recent_cache.exists()
    assert not quarantine.exists()
    assert not old_temp.exists()
    assert not old_overflow.exists()
    assert recent_temp.exists()
    assert project_file.exists()
    assert {action["scope"] for action in actions} == {"approved-cache-only"}


def test_fixed_drive_cleanup_skips_healthy_disks_and_nested_lookalikes(tmp_path):
    drive = tmp_path / "E"
    nested = drive / "project" / "go-build-cache" / "old.bin"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"source-owned")
    old = time.time() - 40 * 86400
    os.utime(nested, (old, old))

    actions = wsl_guard.clean_safe_fixed_drive_caches([{
        "drive": "E:",
        "root": drive,
        "usage": SimpleNamespace(total=100, used=20, free=80),
        "level": "healthy",
    }])

    assert actions == []
    assert nested.exists()


def test_windows_incremental_cleanup_is_explicit_stale_and_worktree_scoped(
    tmp_path, monkeypatch
):
    worktree = tmp_path / "E" / "evox-worktrees" / "old-task"
    (worktree / ".git").parent.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: somewhere", encoding="utf-8")
    artifact = worktree / "target" / "debug" / "incremental" / "old.o"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"rebuildable")
    old = time.time() - 8 * 86400
    os.utime(artifact, (old, old))
    os.utime(artifact.parent, (old, old))
    # A recent sibling output may touch target/debug without making the
    # incremental subtree active.
    os.utime(artifact.parent.parent, None)
    monkeypatch.setattr(wsl_guard, "_windows_builds_active", lambda: False)

    actions = wsl_guard.clean_inactive_windows_incremental_caches(
        [str(worktree / "target")],
        [{"drive": str((worktree / "target").drive).upper(), "mode": "warning"}],
    )

    assert any(action["name"] == "windows-rust-incremental" for action in actions)
    assert not artifact.exists()
    assert (worktree / ".git").exists()


def test_windows_incremental_cleanup_fails_closed_while_build_is_active(
    tmp_path, monkeypatch
):
    worktree = tmp_path / "E" / "evox-worktrees" / "active-task"
    (worktree / ".git").parent.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: somewhere", encoding="utf-8")
    artifact = worktree / "target" / "debug" / "incremental" / "active.o"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"busy")
    monkeypatch.setattr(wsl_guard, "_windows_builds_active", lambda: True)

    actions = wsl_guard.clean_inactive_windows_incremental_caches(
        [str(worktree / "target")],
        [{"drive": str((worktree / "target").drive).upper(), "mode": "critical"}],
    )

    assert actions[0]["status"] == "build-active"
    assert actions[0]["drive"] == str((worktree / "target").drive).upper()
    assert artifact.exists()


def test_windows_build_probe_uses_command_lines_and_fails_closed(monkeypatch):
    monkeypatch.setattr(wsl_guard.os, "name", "nt")
    monkeypatch.setattr(
        wsl_guard,
        "_windows_process_command_lines",
        lambda: (True, [r'C:\\Rust\\bin\\cargo.exe build']),
    )
    assert wsl_guard._windows_builds_active() is True

    monkeypatch.setattr(wsl_guard, "_windows_process_command_lines", lambda: (False, []))
    assert wsl_guard._windows_builds_active() is True


def test_windows_critical_cleanup_removes_registered_target_after_builds_stop(
    tmp_path, monkeypatch
):
    worktree = tmp_path / "E" / "evox"
    (worktree / ".git").mkdir(parents=True)
    source = worktree / "src" / "lib.rs"
    source.parent.mkdir()
    source.write_text("pub fn kept() {}", encoding="utf-8")
    artifact = worktree / "target" / "debug" / "deps" / "libold.rlib"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"rebuildable")
    monkeypatch.setattr(wsl_guard, "_windows_builds_active", lambda: False)

    actions = wsl_guard.clean_inactive_windows_incremental_caches(
        [str(worktree / "target")],
        [{"drive": str((worktree / "target").drive).upper(), "mode": "critical"}],
    )

    assert any(action["name"] == "windows-rust-target" for action in actions)
    assert not artifact.exists()
    assert source.read_text(encoding="utf-8") == "pub fn kept() {}"
    assert (worktree / ".git").exists()


def test_windows_critical_cleanup_accepts_named_cargo_target(tmp_path, monkeypatch):
    worktree = tmp_path / "E" / "evox"
    (worktree / ".git").mkdir(parents=True)
    source = worktree / "src" / "lib.rs"
    source.parent.mkdir()
    source.write_text("pub fn kept() {}", encoding="utf-8")
    artifact = worktree / "target-hookgate" / "debug" / "deps" / "old.rlib"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"rebuildable")
    monkeypatch.setattr(wsl_guard, "_windows_builds_active", lambda: False)

    actions = wsl_guard.clean_inactive_windows_incremental_caches(
        [str(worktree / "target-*")],
        [{"drive": str((worktree / "target-hookgate").drive).upper(), "mode": "critical"}],
    )

    assert any(action["name"] == "windows-rust-target" for action in actions)
    assert not artifact.exists()
    assert source.read_text(encoding="utf-8") == "pub fn kept() {}"
    assert (worktree / ".git").exists()


def test_named_target_cleanup_ignores_unrelated_build_and_protects_recent_target(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(wsl_guard.os, "name", "nt")
    worktree = tmp_path / "E" / "evox"
    (worktree / ".git").mkdir(parents=True)
    source = worktree / "src" / "lib.rs"
    source.parent.mkdir()
    source.write_text("pub fn kept() {}", encoding="utf-8")
    stale = worktree / "target-old" / "debug" / "old.rlib"
    recent = worktree / "target-active" / "debug" / "new.rlib"
    for artifact in (stale, recent):
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"rebuildable")
    now = time.time()
    old = now - 3 * 3600
    for path in (stale, stale.parent, stale.parent.parent):
        os.utime(path, (old, old))

    actions = wsl_guard.clean_inactive_named_windows_targets(
        [str(worktree / "target-*")],
        [{"drive": str(stale.parent.parent.drive).upper(), "mode": "critical"}],
        process_commands=[f"rustc --out-dir {recent.parent}"],
        now=now,
    )

    assert not stale.exists()
    assert recent.exists()
    assert source.read_text(encoding="utf-8") == "pub fn kept() {}"
    assert any(action["status"] == "active-protected" for action in actions)


def test_cache_cleanup_obeys_byte_budget(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    first = cache / "first.bin"
    second = cache / "second.bin"
    first.write_bytes(b"a" * 16)
    second.write_bytes(b"b" * 16)
    old = time.time() - 40 * 86400
    os.utime(first, (old, old))
    os.utime(second, (old, old))

    result = wsl_guard.clean_stale_windows_temp(cache, 7, max_bytes=16)

    assert result["status"] == "byte-budget-reached"
    assert result["bytes"] == 16
    assert len(list(cache.iterdir())) == 1


def test_claude_build_target_cleanup_removes_only_stale_allowlisted_targets(tmp_path):
    windows_temp = tmp_path / "Windows" / "Temp"
    stale = windows_temp / "claude" / "evox-wt" / "target-old" / "debug" / "old.o"
    recent = windows_temp / "claude" / "evox-wt" / "target-recent" / "debug" / "new.o"
    source = windows_temp / "claude" / "evox-wt" / "s1-terminal-invariants" / "src" / "lib.rs"
    stale.parent.mkdir(parents=True)
    recent.parent.mkdir(parents=True)
    source.parent.mkdir(parents=True)
    stale.write_bytes(b"rebuildable")
    recent.write_bytes(b"busy")
    source.write_text("source", encoding="utf-8")
    now = time.time()
    old = now - 5 * 3600
    for path in (stale, stale.parent, stale.parent.parent):
        os.utime(path, (old, old))

    result = wsl_guard.clean_inactive_claude_build_targets(
        windows_temp,
        min_age_hours=3,
        process_commands=[],
        now=now,
    )

    assert result["bytes"] == len(b"rebuildable")
    assert not stale.parent.parent.exists()
    assert recent.exists()
    assert source.read_text(encoding="utf-8") == "source"


def test_claude_build_target_cleanup_protects_process_referenced_target(tmp_path):
    windows_temp = tmp_path / "Windows" / "Temp"
    artifact = windows_temp / "claude" / "evox-wt" / "target-active" / "debug" / "busy.o"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"busy")
    now = time.time()
    old = now - 5 * 3600
    for path in (artifact, artifact.parent, artifact.parent.parent):
        os.utime(path, (old, old))

    result = wsl_guard.clean_inactive_claude_build_targets(
        windows_temp,
        min_age_hours=3,
        process_commands=[f"rustc --out-dir {artifact.parent}"],
        now=now,
    )

    assert result["status"] == "active-protected"
    assert artifact.exists()


def test_claude_build_target_cleanup_removes_only_nested_worktree_target(tmp_path):
    windows_temp = tmp_path / "Windows" / "Temp"
    worktree = windows_temp / "claude" / "evox-wt" / "s1-terminal-invariants"
    artifact = worktree / "target" / "debug" / "old.o"
    source = worktree / "src" / "dirty.rs"
    artifact.parent.mkdir(parents=True)
    source.parent.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: somewhere", encoding="utf-8")
    artifact.write_bytes(b"rebuildable")
    source.write_text("keep me", encoding="utf-8")
    now = time.time()
    old = now - 5 * 3600
    for path in (artifact, artifact.parent, artifact.parent.parent):
        os.utime(path, (old, old))

    result = wsl_guard.clean_inactive_claude_build_targets(
        windows_temp,
        min_age_hours=3,
        process_commands=[],
        now=now,
    )

    assert result["bytes"] == len(b"rebuildable")
    assert not (worktree / "target").exists()
    assert source.read_text(encoding="utf-8") == "keep me"
    assert (worktree / ".git").is_file()


def test_claude_build_target_cleanup_rejects_nested_non_worktree_target(tmp_path):
    windows_temp = tmp_path / "Windows" / "Temp"
    artifact = (
        windows_temp / "claude" / "evox-wt" / "ordinary-folder"
        / "target" / "debug" / "old.o"
    )
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"keep")
    now = time.time()
    old = now - 5 * 3600
    for path in (artifact, artifact.parent, artifact.parent.parent):
        os.utime(path, (old, old))

    result = wsl_guard.clean_inactive_claude_build_targets(
        windows_temp,
        min_age_hours=3,
        process_commands=[],
        now=now,
    )

    assert result["bytes"] == 0
    assert artifact.exists()


def test_cache_cleanup_prioritizes_old_directory_within_time_budget(tmp_path):
    cache = tmp_path / "cache"
    recent = cache / "a-recent" / "recent.bin"
    old = cache / "z-old" / "old.bin"
    for item in (recent, old):
        item.parent.mkdir(parents=True, exist_ok=True)
        item.write_bytes(b"x")
    now = time.time()
    os.utime(recent, (now, now))
    os.utime(recent.parent, (now, now))
    old_time = now - 40 * 86400
    os.utime(old, (old_time, old_time))
    os.utime(old.parent, (old_time, old_time))

    result = wsl_guard.clean_stale_windows_temp(cache, 7, max_bytes=1)

    assert result["bytes"] == 1
    assert recent.exists()
    assert not old.exists()


def test_maintenance_only_records_state_while_space_is_healthy(tmp_path, monkeypatch):
    state = tmp_path / "disk-maintenance.json"
    usage = SimpleNamespace(total=200 * 1024**3, used=50 * 1024**3, free=150 * 1024**3)
    monkeypatch.setattr(wsl_guard, "host_disk_usage", lambda: usage)
    monkeypatch.setattr(
        wsl_guard, "clean_stale_windows_temp",
        lambda *args: (_ for _ in ()).throw(AssertionError("cleanup must not run")),
    )

    assert wsl_guard.maintain("Ubuntu-Test", 2222, str(state), []) == 0

    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["status"] == "healthy"
    assert saved["actions"] == []
    assert saved["freeBytesAfter"] == usage.free


def test_manual_drive_maintenance_bypasses_backoff_and_is_consumed(tmp_path, monkeypatch):
    state = tmp_path / "disk-maintenance.json"
    gib = 1024**3
    host = SimpleNamespace(total=1000 * gib, used=850 * gib, free=150 * gib)
    readings = iter([
        [{
            "drive": "E:", "root": tmp_path / "E",
            "usage": SimpleNamespace(total=300 * gib, used=250 * gib, free=50 * gib),
            "level": "healthy",
        }],
        [{
            "drive": "E:", "root": tmp_path / "E",
            "usage": SimpleNamespace(total=300 * gib, used=250 * gib, free=50 * gib),
            "level": "healthy",
        }],
    ])
    monkeypatch.setattr(wsl_guard, "host_disk_usage", lambda: host)
    monkeypatch.setattr(wsl_guard, "fixed_disk_snapshots", lambda: next(readings))
    calls = []
    monkeypatch.setattr(
        wsl_guard,
        "clean_safe_fixed_drive_caches",
        lambda disks, **kwargs: calls.append((disks, kwargs)) or [{
            "name": "fixed-drive-cache-maintenance",
            "drive": "E:",
            "status": "completed",
            "scope": "approved-cache-only",
            "bytes": 0,
        }],
    )
    request = {
        "requestId": "manual-test",
        "drive": "E:",
        "requestedAt": "2026-08-26T00:00:00+00:00",
        "status": "queued",
        "force": True,
    }
    request_path = wsl_guard.manual_maintenance_request_path(str(state))
    request_path.write_text(json.dumps(request), encoding="utf-8")

    assert wsl_guard.maintain("Ubuntu-Test", 2222, str(state), []) == 0

    saved = json.loads(state.read_text(encoding="utf-8"))
    assert calls[0][1] == {"ignore_backoff": True}
    assert calls[0][0][0]["drive"] == "E:"
    assert calls[0][0][0]["mode"] == "warning"
    assert saved["manual"]["status"] == "completed"
    assert saved["manual"]["drive"] == "E:"
    assert not request_path.exists()


def test_c_drive_expensive_scan_honors_backoff(tmp_path, monkeypatch):
    state = tmp_path / "disk-maintenance.json"
    gib = 1024**3
    host = SimpleNamespace(total=1000 * gib, used=930 * gib, free=70 * gib)
    previous = {
        "drives": [{
            "drive": "C:",
            "level": "warning",
            "freeBytesAfter": 70 * gib,
            "nextEligibleEpoch": time.time() + 1800,
            "noProgressCount": 2,
        }],
    }
    state.write_text(json.dumps(previous), encoding="utf-8")
    readings = lambda: [{
        "drive": "C:",
        "root": tmp_path,
        "usage": host,
        "level": "warning",
    }]
    monkeypatch.setattr(wsl_guard, "host_disk_usage", lambda: host)
    monkeypatch.setattr(wsl_guard, "fixed_disk_snapshots", readings)
    monkeypatch.setattr(wsl_guard, "established_ssh_clients", lambda port: 0)
    monkeypatch.setattr(
        wsl_guard,
        "clean_inactive_claude_build_targets",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("C scan must back off")),
    )
    monkeypatch.setattr(
        wsl_guard,
        "clean_stale_windows_temp",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("C scan must back off")),
    )
    monkeypatch.setattr(
        wsl_guard,
        "clean_safe_fixed_drive_caches",
        lambda disks: [{
            "name": "fixed-drive-cache-maintenance",
            "drive": "C:",
            "status": "backoff",
            "bytes": 0,
        }],
    )
    monkeypatch.setattr(
        wsl_guard,
        "clean_inactive_windows_incremental_caches",
        lambda patterns, disks: (
            [] if disks == [] else (_ for _ in ()).throw(AssertionError("backoff must skip build probe"))
        ),
    )

    assert wsl_guard.maintain("Ubuntu-Test", 2222, str(state), []) == 0

    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["actions"][0]["status"] == "backoff"
    assert saved["drives"][0]["noProgressCount"] == 2


def test_manual_maintenance_request_accepts_only_fixed_drive(tmp_path, monkeypatch):
    state = tmp_path / "disk-maintenance.json"
    monkeypatch.setattr(wsl_guard, "fixed_disk_snapshots", lambda: [
        {"drive": "E:", "root": tmp_path / "E", "usage": object()},
    ])

    queued = wsl_guard.request_manual_maintenance("e:", str(state))

    assert queued["drive"] == "E:"
    assert wsl_guard.read_manual_maintenance_request(str(state))["requestId"] == queued["requestId"]
    with pytest.raises(ValueError):
        wsl_guard.request_manual_maintenance("Z:", str(state))


def test_maintenance_records_smart_recovery_and_next_action(tmp_path, monkeypatch):
    state = tmp_path / "disk-maintenance.json"
    gib = 1024**3
    host = SimpleNamespace(total=1000 * gib, used=850 * gib, free=150 * gib)
    disk_readings = iter([
        [{
            "drive": "E:", "root": tmp_path / "E",
            "usage": SimpleNamespace(total=300 * gib, used=280 * gib, free=20 * gib),
            "level": "warning",
        }],
        [{
            "drive": "E:", "root": tmp_path / "E",
            "usage": SimpleNamespace(total=300 * gib, used=220 * gib, free=80 * gib),
            "level": "healthy",
        }],
    ])
    monkeypatch.setattr(wsl_guard, "host_disk_usage", lambda: host)
    monkeypatch.setattr(wsl_guard, "fixed_disk_snapshots", lambda: next(disk_readings))
    monkeypatch.setattr(wsl_guard, "established_ssh_clients", lambda port: 0)
    monkeypatch.setattr(
        wsl_guard,
        "clean_safe_fixed_drive_caches",
        lambda disks: [{
            "name": "go-build-cache", "drive": "E:", "status": "cleaned",
            "scope": "approved-cache-only", "bytes": 25 * gib,
        }],
    )

    assert wsl_guard.maintain("Ubuntu-Test", 2222, str(state), []) == 0

    saved = json.loads(state.read_text(encoding="utf-8"))
    drive = saved["drives"][0]
    assert saved["status"] == "recovered"
    assert saved["reclaimedBytes"] == 25 * gib
    assert drive["effectiveLevelAfter"] == "healthy"
    assert drive["reclaimedBytes"] == 25 * gib
    assert drive["nextActionAt"]
    assert drive["actionReason"] == "已恢复健康，进入冷却观察"


def test_time_budget_continues_without_exponential_backoff(tmp_path, monkeypatch):
    state = tmp_path / "disk-maintenance.json"
    gib = 1024**3
    host = SimpleNamespace(total=1000 * gib, used=850 * gib, free=150 * gib)
    readings = iter([
        [{
            "drive": "E:", "root": tmp_path / "E",
            "usage": SimpleNamespace(total=300 * gib, used=270 * gib, free=30 * gib),
            "level": "warning",
        }],
        [{
            "drive": "E:", "root": tmp_path / "E",
            "usage": SimpleNamespace(total=300 * gib, used=270 * gib, free=30 * gib),
            "level": "warning",
        }],
    ])
    monkeypatch.setattr(wsl_guard, "host_disk_usage", lambda: host)
    monkeypatch.setattr(wsl_guard, "fixed_disk_snapshots", lambda: next(readings))
    monkeypatch.setattr(
        wsl_guard,
        "clean_safe_fixed_drive_caches",
        lambda disks: [{
            "name": "fixed-drive-cache-maintenance",
            "drive": "E:",
            "status": "time-budget-reached",
            "scope": "approved-cache-only",
            "bytes": 0,
        }],
    )

    before = time.time()
    assert wsl_guard.maintain("Ubuntu-Test", 2222, str(state), []) == 0

    saved = json.loads(state.read_text(encoding="utf-8"))
    drive = saved["drives"][0]
    assert drive["noProgressCount"] == 0
    assert 100 <= drive["nextEligibleEpoch"] - before <= 140
    assert "2 分钟后" in drive["actionReason"]


def test_maintenance_reclaims_only_inactive_build_cache_during_active_ssh_session(tmp_path, monkeypatch):
    state = tmp_path / "disk-maintenance.json"
    usage = SimpleNamespace(total=200 * 1024**3, used=165 * 1024**3, free=35 * 1024**3)
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    monkeypatch.setattr(wsl_guard, "host_disk_usage", lambda: usage)
    monkeypatch.setattr(wsl_guard, "tcp_ready", lambda port: True)
    monkeypatch.setattr(wsl_guard, "established_ssh_clients", lambda port: 1)
    monkeypatch.setattr(
        wsl_guard, "clean_stale_windows_temp",
        lambda *args, **kwargs: {"name": "windows-temp", "status": "cleaned", "bytes": 0},
    )
    monkeypatch.setattr(wsl_guard, "clean_safe_windows_caches", lambda *args: [])
    monkeypatch.setattr(
        wsl_guard, "_wsl_base_maintenance",
        lambda *args: {"name": "wsl-safe-maintenance", "status": "completed"},
    )
    monkeypatch.setattr(
        wsl_guard,
        "_wsl_reclaim_targets",
        lambda distro, paths, *args: {
            "name": "wsl-build-cache",
            "status": "completed",
            "bytes": 0,
            "cleaned": [],
            "skippedActive": list(paths),
        },
    )

    assert wsl_guard.maintain(
        "Ubuntu-Test", 2222, str(state), ["/home/kachi/projects/evox/target"]
    ) == 0

    saved = json.loads(state.read_text(encoding="utf-8"))
    build = next(action for action in saved["actions"] if action["name"] == "wsl-build-cache")
    assert build["status"] == "completed"
    assert build["skippedActive"] == ["/home/kachi/projects/evox/target"]


def test_maintenance_reclaims_only_configured_cache_when_idle(tmp_path, monkeypatch):
    state = tmp_path / "disk-maintenance.json"
    gib = 1024**3
    readings = iter([
        SimpleNamespace(total=200 * gib, used=165 * gib, free=35 * gib),
        SimpleNamespace(total=200 * gib, used=165 * gib, free=35 * gib),
        SimpleNamespace(total=200 * gib, used=50 * gib, free=150 * gib),
    ])
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    monkeypatch.setattr(wsl_guard, "host_disk_usage", lambda: next(readings))
    monkeypatch.setattr(wsl_guard, "tcp_ready", lambda port: True)
    monkeypatch.setattr(wsl_guard, "established_ssh_clients", lambda port: 0)
    monkeypatch.setattr(
        wsl_guard, "clean_stale_windows_temp",
        lambda *args, **kwargs: {"name": "windows-temp", "status": "cleaned", "bytes": 0},
    )
    monkeypatch.setattr(wsl_guard, "clean_safe_windows_caches", lambda *args: [])
    monkeypatch.setattr(
        wsl_guard, "_wsl_base_maintenance",
        lambda *args: {"name": "wsl-safe-maintenance", "status": "completed"},
    )
    monkeypatch.setattr(
        wsl_guard, "_wsl_reclaim_targets",
        lambda distro, paths, *args: {"name": "wsl-build-cache", "status": "completed", "bytes": 75 * gib, "cleaned": list(paths)},
    )

    assert wsl_guard.maintain(
        "Ubuntu-Test", 2222, str(state), ["/home/kachi/projects/evox/target"]
    ) == 0

    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["status"] == "recovered"
    assert saved["freeBytesAfter"] == 150 * gib
