"""The whole chain, with nothing mocked out except the deploy itself.

`test_watchdog.py` checks each piece against a stub. That leaves the question
the owner actually asked — *"必须有自动的脚本保障你不会把自己改坏了后无法回滚
或者得知"* — unanswered, because a chain of individually-correct links can still
be broken at the joints: a probe that never gets called, a target lookup that
returns None because git is invoked from the wrong directory, an alarm written
somewhere the status line does not read.

So this drives the real thing:

    a real git repository          so `_rollback_target` runs real git
    a real known-good.jsonl        so it has something to choose
    a real runtime.json            pointing at ports where nothing listens
    the real `_probe` subprocess   which really fails to connect
    the real threshold and cooldown

Only `scripts/fleet-rollback` is a stub, and only because the real one restarts
production services on two machines. That half is rehearsed against production
separately — `fleet-rollback to <HEAD> --dry-run`, then for real.

What this would have caught, had it existed: the probe became a subprocess and
three unit tests started reading a string left behind by a rollback stub two
hundred lines away. They stayed green.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
import time
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parent.parent / "scripts" / "fleet-watchdog"


def _spare_port() -> int:
    """A port that nothing is listening on — bind it, read it, let it go."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """A watchdog wired to a repository and a fleet that are entirely this
    test's, and that are real rather than described."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)

    (repo / "README").write_text("v1\n", encoding="utf-8")
    git = ["git", "-C", str(repo)]
    subprocess.run(git + ["init", "--quiet"], check=True)
    subprocess.run(git + ["config", "user.email", "t@example.com"], check=True)
    subprocess.run(git + ["config", "user.name", "t"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "--quiet", "-m", "known good"], check=True)
    good = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()

    (repo / "README").write_text("v2 — the one that broke\n", encoding="utf-8")
    subprocess.run(git + ["commit", "--quiet", "-am", "the bad one"], check=True)
    head = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()

    # The stub stands in for a deploy across two machines. It records what it
    # was told to do, which is the thing under test.
    log = tmp_path / "rollback-calls.txt"
    rollback = repo / "scripts" / "fleet-rollback"
    rollback.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> " + str(log) + "\nexit ${STUB_EXIT:-0}\n",
        encoding="utf-8")
    rollback.chmod(0o755)

    state = tmp_path / "state"
    state.mkdir()
    # HEAD is in here too, and newest — which is what production looks like,
    # because `markgood` runs hourly and marks whatever is deployed after it has
    # soaked. The version that is running is by definition not somewhere to roll
    # back to; choosing it would restart both hosts, change nothing, and look
    # exactly like a fix.
    # Recent timestamps, because the watchdog also alarms on a stale rollback
    # point and epoch-zero marks would make every case here look like that too.
    now = time.time()
    (state / "known-good.jsonl").write_text(
        json.dumps({"sha": good, "at": now - 7200}) + "\n"
        + json.dumps({"sha": head, "at": now - 3600}) + "\n", encoding="utf-8")

    # A fleet that is not answering: the ports are real and nothing is on them.
    (tmp_path / "tc.json").write_text(json.dumps({
        "fleetCoordinator": {"token": "t"},
        "accounts": [{"type": "oauth", "accountUuid": "u"}],
    }), encoding="utf-8")
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({
        "teamclaude_config": str(tmp_path / "tc.json"),
        "coordinator_port": _spare_port(), "proxy_port": _spare_port(),
    }), encoding="utf-8")

    monkeypatch.setenv("FLEET_RUNTIME_CONFIG", str(runtime))
    # No peer: reaching for the real server from a test would be both slow and
    # a lie about what is being measured.
    monkeypatch.setenv("FLEET_PEER", "")

    spec = importlib.util.spec_from_loader("watchdog_rehearsal", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__dict__["__file__"] = str(SOURCE)
    text = SOURCE.read_text(encoding="utf-8")
    marker = 'if __name__ == "__main__":'
    if marker in text:
        text = text[: text.index(marker)]
    exec(compile(text, str(SOURCE), "exec"), module.__dict__)

    module.REPO = repo
    module.STATE_DIR = state
    module.STATE = state / "watchdog.json"
    module.ALARM = state / "watchdog-alarm.json"

    return type("Fleet", (), {
        "module": module, "repo": repo, "good": good, "log": log,
        "state": state})


def _calls(fleet):
    return (fleet.log.read_text(encoding="utf-8").splitlines()
            if fleet.log.exists() else [])


def test_the_whole_chain_runs(fleet):
    """Broken fleet → three probes → the right commit → an alarm that says so.

    Every link real: the probe really opens a socket and really fails, the
    target really comes out of git, the rollback is really invoked as a
    subprocess from the repository path.
    """
    module = fleet.module

    assert module.check(act=True) == 1
    assert module.check(act=True) == 1
    assert _calls(fleet) == [], "两次失败不该动手 —— 那是网络抖动的形状"

    assert module.check(act=True) == 0
    assert _calls(fleet) == ["to %s" % fleet.good], _calls(fleet)

    alarm = json.loads((fleet.state / "watchdog-alarm.json").read_text())
    assert alarm["latest"]["kind"] == "rolled_back"
    assert alarm["latest"]["target"] == fleet.good
    assert "没有应答" in alarm["latest"]["why"] or "监听" in alarm["latest"]["why"]


def test_a_rollback_that_fails_is_louder_not_quieter(fleet, monkeypatch):
    """The worst case in the whole design. If the recovery itself is broken,
    saying nothing leaves the owner believing a mechanism exists that does not.
    """
    monkeypatch.setenv("STUB_EXIT", "1")
    module = fleet.module

    for _ in range(module.FAILURES_BEFORE_ACTING - 1):
        module.check(act=True)
    assert module.check(act=True) == 3

    alarm = json.loads((fleet.state / "watchdog-alarm.json").read_text())
    assert alarm["latest"]["kind"] == "rollback_failed"


def test_it_does_not_roll_back_a_second_time(fleet):
    """Still broken after a rollback means the fault was never the version, and
    restarting services in a loop turns one outage into a worse one."""
    module = fleet.module
    for _ in range(module.FAILURES_BEFORE_ACTING):
        module.check(act=True)
    assert len(_calls(fleet)) == 1

    for _ in range(module.FAILURES_BEFORE_ACTING * 2):
        module.check(act=True)
    assert len(_calls(fleet)) == 1, "冷却期内不该再滚"

    alarm = json.loads((fleet.state / "watchdog-alarm.json").read_text())
    assert alarm["latest"]["kind"] == "still_down_after_rollback", \
        "沉默地不动作, 和没有看门狗是一回事"


def test_a_healthy_fleet_is_left_alone(fleet, monkeypatch):
    """The property that decides whether this can run unattended at all. A
    watchdog with a false positive is a scheduled outage."""
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):                                    # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        config = Path(fleet.repo).parent / "runtime.json"
        config.write_text(json.dumps({
            "teamclaude_config": str(Path(fleet.repo).parent / "tc.json"),
            # The same port for both: the probe wants an authenticated answer
            # from the coordinator and a listening socket for the data plane,
            # and one server can honestly be both here.
            "coordinator_port": port, "proxy_port": port,
        }), encoding="utf-8")
        monkeypatch.setenv("FLEET_RUNTIME_CONFIG", str(config))

        module = fleet.module
        for _ in range(module.FAILURES_BEFORE_ACTING + 2):
            assert module.check(act=True) == 0
        assert _calls(fleet) == []
        assert not (fleet.state / "watchdog-alarm.json").exists(), \
            "什么都没出问题的时候, 连告警文件都不该存在"
    finally:
        server.shutdown()
        server.server_close()
