"""Nothing closed the loop between "it broke" and "it is back".

Every other safety mechanism here is one half of the job:

    release-gate.sh        checks before a deploy — only when somebody runs it
    fleet-reload-service   verifies a service swap — only when swapping one
    markgood (hourly)      RECORDS a good version  — never acts on a bad one
    fleet-rollback now     goes back               — needs a person to type it

So a version that passed every check and broke ten minutes later — which is what
every outage in this system has actually looked like — sat broken until somebody
noticed by hand. That is the thing the owner asked never to happen again.

The hard part is not detecting. It is not making things worse: a watchdog that
acts on a blip, or thrashes services in a loop, or "rolls back" to the version
already running, is a second outage wearing the costume of a fix.
"""

from __future__ import annotations

import contextlib
import http.server
import importlib.util
import json
import socket
import threading
import time
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parent.parent / "scripts" / "fleet-watchdog"


def _module(tmp_path):
    spec = importlib.util.spec_from_loader("watchdog", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__dict__["__file__"] = str(SOURCE)
    text = SOURCE.read_text(encoding="utf-8")
    marker = 'if __name__ == "__main__":'
    if marker in text:
        text = text[: text.index(marker)]
    exec(compile(text, str(SOURCE), "exec"), module.__dict__)
    module.STATE_DIR = tmp_path
    module.STATE = tmp_path / "watchdog.json"
    module.ALARM = tmp_path / "watchdog-alarm.json"
    return module


def _down(module, why="协调器没有应答", blind=False):
    module._assess = lambda: {"ok": False, "why": why, "blind": blind,
                              "hosts": {"本机": why}}


def _up(module, blind=False):
    module._assess = lambda: {"ok": True, "why": "协调器 HTTP 200",
                              "blind": blind, "hosts": {"本机": "HTTP 200"}}


@pytest.fixture(autouse=True)
def _contain_the_subprocess_stubs():
    """Several tests below replace `subprocess.run` to prove the watchdog did or
    did not shell out. `module.subprocess` is the real module object, so every
    one of those assignments was escaping into whatever ran next.

    It was invisible while the probe used `urllib` in-process. The moment the
    probe became a subprocess — so that one implementation could ask both hosts
    the same question — three probe tests started reading `够不着: peer restart
    failed`, a string from a rollback stub two hundred lines away. Green tests
    measuring a leak, which is worse than red ones.
    """
    import subprocess as real
    saved = real.run
    try:
        yield
    finally:
        real.run = saved


def _never_rolls_back(module):
    calls = []
    module.subprocess.run = lambda *a, **k: calls.append(a) or pytest.fail(
        "acted when it should not have")
    return calls


# ---------------------------------------------------------------------------
# not making things worse
# ---------------------------------------------------------------------------

def test_one_failure_is_not_an_outage(tmp_path):
    """A restart in progress, a laptop waking up, one refused connection. Acting
    on any of those would make the watchdog the most common cause of deploys."""
    module = _module(tmp_path)
    _down(module)
    _never_rolls_back(module)

    assert module.check(act=True) == 1
    assert json.loads(module.STATE.read_text())["consecutive_failures"] == 1


def test_it_acts_on_the_third(tmp_path, monkeypatch):
    module = _module(tmp_path)
    _down(module)
    monkeypatch.setattr(module, "_rollback_target", lambda: "goodsha")

    calls = []

    class _Result:
        returncode = 0
        stdout = "rolled back"
        stderr = ""

    module.subprocess.run = lambda *a, **k: calls.append(a[0]) or _Result()

    assert module.check(act=True) == 1
    assert module.check(act=True) == 1
    assert not calls, "must not act before the threshold"

    assert module.check(act=True) == 0
    assert calls and "goodsha" in calls[0], calls


def test_recovery_clears_the_count(tmp_path):
    """Two failures then a success is a network, not a version."""
    module = _module(tmp_path)
    _down(module)
    module.check(act=False)
    module.check(act=False)
    _up(module)
    module.check(act=False)

    assert json.loads(module.STATE.read_text())["consecutive_failures"] == 0


def test_it_will_not_roll_back_to_what_is_already_running(tmp_path, monkeypatch):
    """Restarting services and changing nothing is worse than doing nothing: it
    looks like action while the real fault stays."""
    module = _module(tmp_path)
    _down(module)
    monkeypatch.setattr(module, "_rollback_target", lambda: None)
    _never_rolls_back(module)

    for _ in range(module.FAILURES_BEFORE_ACTING):
        code = module.check(act=True)
    assert code == 2

    alarm = json.loads(module.ALARM.read_text())
    assert alarm["latest"]["kind"] == "down_no_target", (
        "with nowhere to go it must still shout"
    )


def test_it_does_not_thrash(tmp_path, monkeypatch):
    """If a rollback did not fix it, the fault is not the version. Rolling back
    again every few minutes turns one outage into a service-restart loop."""
    module = _module(tmp_path)
    _down(module)
    module.STATE.write_text(json.dumps({
        "consecutive_failures": module.FAILURES_BEFORE_ACTING - 1,
        "last_action_at": time.time() - 60,     # acted a minute ago
        "history": [],
    }), encoding="utf-8")
    monkeypatch.setattr(module, "_rollback_target", lambda: "goodsha")
    _never_rolls_back(module)

    assert module.check(act=True) == 2


def test_still_down_after_a_rollback_is_the_loudest_case(tmp_path, monkeypatch):
    """Silence during the cooldown would be the worst possible behaviour: the
    fleet is down AND the one thing that was supposed to fix it did not."""
    module = _module(tmp_path)
    _down(module)
    module.STATE.write_text(json.dumps({
        "consecutive_failures": module.FAILURES_BEFORE_ACTING - 1,
        "last_action_at": time.time() - 60,
        "history": [],
    }), encoding="utf-8")
    monkeypatch.setattr(module, "_rollback_target", lambda: "goodsha")
    module.subprocess.run = lambda *a, **k: pytest.fail("acted during cooldown")

    module.check(act=True)
    alarm = json.loads(module.ALARM.read_text())
    assert alarm["latest"]["kind"] == "still_down_after_rollback"


def test_check_mode_never_acts(tmp_path, monkeypatch):
    module = _module(tmp_path)
    _down(module)
    monkeypatch.setattr(module, "_rollback_target", lambda: "goodsha")
    _never_rolls_back(module)

    for _ in range(module.FAILURES_BEFORE_ACTING + 2):
        module.check(act=False)


# ---------------------------------------------------------------------------
# choosing where to go back to
# ---------------------------------------------------------------------------

def _marks(module, tmp_path, entries):
    (tmp_path / "known-good.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def _git_says(module, head, existing):
    """Stand in for git: HEAD, and which shas it still has."""
    class _Result:
        def __init__(self, stdout="", code=0):
            self.stdout = stdout
            self.returncode = code

    def run(cmd, **kwargs):
        if "rev-parse" in cmd:
            return _Result(head + "\n")
        if "cat-file" in cmd:
            sha = cmd[-1].split("^")[0]
            return _Result(code=0 if sha in existing else 1)
        return _Result()

    module.subprocess.run = run


def test_it_skips_the_version_already_running(tmp_path):
    """Mutation testing found this untested: every other case here mocks
    `_rollback_target` away, so the one guard inside it was never exercised.

    Rolling "back" to HEAD restarts both hosts and changes nothing, while the
    real fault stays and the alarm says a rollback happened.
    """
    module = _module(tmp_path)
    _marks(module, tmp_path, [
        {"sha": "older", "at": 1.0},
        {"sha": "current", "at": 2.0},
    ])
    _git_says(module, head="current", existing={"older", "current"})

    assert module._rollback_target() == "older"


def test_the_newest_usable_point_wins(tmp_path):
    module = _module(tmp_path)
    _marks(module, tmp_path, [
        {"sha": "oldest", "at": 1.0},
        {"sha": "newer", "at": 2.0},
    ])
    _git_says(module, head="running", existing={"oldest", "newer"})

    assert module._rollback_target() == "newer"


def test_a_mark_whose_commit_is_gone_is_skipped(tmp_path):
    """A rebased or garbage-collected commit is a dead end discovered at the
    worst moment — while the fleet is down and unattended."""
    module = _module(tmp_path)
    _marks(module, tmp_path, [
        {"sha": "still-here", "at": 1.0},
        {"sha": "vanished", "at": 2.0},
    ])
    _git_says(module, head="running", existing={"still-here"})

    assert module._rollback_target() == "still-here"


def test_no_marks_means_no_target(tmp_path):
    module = _module(tmp_path)
    _git_says(module, head="running", existing=set())
    assert module._rollback_target() is None


# ---------------------------------------------------------------------------
# making sure somebody finds out
# ---------------------------------------------------------------------------

def test_every_action_leaves_an_alarm(tmp_path, monkeypatch):
    """A rollback nobody knows about is its own outage — the owner would be
    running a version he did not choose, for reasons nothing recorded."""
    module = _module(tmp_path)
    _down(module, why="鉴权被拒 (HTTP 401)")
    monkeypatch.setattr(module, "_rollback_target", lambda: "goodsha")

    class _Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    module.subprocess.run = lambda *a, **k: _Result()
    for _ in range(module.FAILURES_BEFORE_ACTING):
        module.check(act=True)

    latest = json.loads(module.ALARM.read_text())["latest"]
    assert latest["kind"] == "rolled_back"
    assert latest["target"] == "goodsha"
    assert "401" in latest["why"], "the alarm has to say WHY, not just that"


def test_a_failed_rollback_says_so(tmp_path, monkeypatch):
    """The one case that genuinely needs a human, so it must not be reported the
    same way as a success."""
    module = _module(tmp_path)
    _down(module)
    monkeypatch.setattr(module, "_rollback_target", lambda: "goodsha")

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "peer restart failed"

    module.subprocess.run = lambda *a, **k: _Result()
    for _ in range(module.FAILURES_BEFORE_ACTING):
        code = module.check(act=True)

    assert code == 3
    assert json.loads(module.ALARM.read_text())["latest"]["kind"] == "rollback_failed"


# ---------------------------------------------------------------------------
# the probe asks the right question
# ---------------------------------------------------------------------------

def test_it_does_not_depend_on_the_thing_it_watches():
    """A watchdog that needs a working control plane to notice a broken control
    plane is decoration. Same rule as `claude-direct`."""
    source = SOURCE.read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#"))
    assert "import claude_fleet" not in code
    assert "from claude_fleet" not in code


def test_it_probes_an_authenticated_endpoint_not_health():
    """`/health` takes no credentials, which is exactly how a fleet with broken
    authentication looked alive to every check in this system. Measured side by
    side: the watchdog reported `鉴权被拒 (HTTP 401)` while `/health` returned
    200 on the same coordinator."""
    source = SOURCE.read_text(encoding="utf-8")
    assert "/v1/pin/check" in source
    assert "Authorization" in source


@contextlib.contextmanager
def _coordinator(status: int):
    """A real socket answering with a real status code.

    The probe is a snippet that runs under a separate interpreter — here and,
    over ssh, on the other host. Mocking `urllib` inside this process would test
    a copy of it that nothing ever executes."""
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):                                    # noqa: N802
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):                            # quiet
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _runtime_files(tmp_path, coordinator_port, proxy_port):
    (tmp_path / "tc.json").write_text(json.dumps({
        "fleetCoordinator": {"token": "t"},
        "accounts": [{"type": "oauth", "accountUuid": "u"}],
    }), encoding="utf-8")
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({
        "teamclaude_config": str(tmp_path / "tc.json"),
        "coordinator_port": coordinator_port, "proxy_port": proxy_port,
    }), encoding="utf-8")
    return runtime


def _spare_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_a_401_is_treated_as_down(tmp_path, monkeypatch):
    """The regression this whole session was about. A coordinator that answers
    but refuses everything is not healthy — and `/health` cannot tell."""
    module = _module(tmp_path)
    with _coordinator(401) as port:
        monkeypatch.setenv("FLEET_RUNTIME_CONFIG",
                           str(_runtime_files(tmp_path, port, port)))
        result = module._probe(None)
    assert result["ok"] is False and "401" in result["why"]


def test_a_coordinator_that_answers_is_not_enough(tmp_path, monkeypatch):
    """The data plane is what actually carries requests. It has gone down on its
    own while the coordinator stayed perfectly healthy."""
    module = _module(tmp_path)
    dead = _spare_port()
    with _coordinator(200) as port:
        monkeypatch.setenv("FLEET_RUNTIME_CONFIG",
                           str(_runtime_files(tmp_path, port, dead)))
        result = module._probe(None)
    assert result["ok"] is False and str(dead) in result["why"]


def test_a_serving_host_reads_as_up(tmp_path, monkeypatch):
    """Without this the suite could not tell a probe that works from one that
    reports everything as broken."""
    module = _module(tmp_path)
    with _coordinator(200) as port:
        monkeypatch.setenv("FLEET_RUNTIME_CONFIG",
                           str(_runtime_files(tmp_path, port, port)))
        result = module._probe(None)
    assert result["ok"] is True, result


def test_an_unreadable_config_is_down_not_a_crash(tmp_path, monkeypatch):
    """It runs unattended on a timer. An exception would just make it silent,
    which is the state it exists to prevent."""
    module = _module(tmp_path)
    monkeypatch.setenv("FLEET_RUNTIME_CONFIG", str(tmp_path / "nope.json"))
    assert module._probe(None)["ok"] is False


def _known_good(module, *entries):
    (module.STATE_DIR / "known-good.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


# ---------------------------------------------------------------------------
# the other host, which nothing was watching
# ---------------------------------------------------------------------------

def _hosts(module, local, peer, peer_host="peer"):
    module._peer = lambda: peer_host
    module._probe = lambda host: dict(peer if host else local)


def test_a_broken_peer_is_an_outage(tmp_path):
    """The whole point. The server has no watchdog of its own, and this one used
    to look only at the machine it runs on — so an outage there was detected by
    nobody, for as long as it lasted."""
    module = _module(tmp_path)
    _hosts(module,
           local={"ok": True, "why": "HTTP 200", "reach": "reached"},
           peer={"ok": False, "why": "鉴权被拒 (HTTP 401)", "reach": "reached"})

    result = module._assess()
    assert result["ok"] is False
    assert "peer" in result["why"] and "401" in result["why"]


def test_an_unreachable_peer_is_not_a_broken_one(tmp_path):
    """The asymmetry that keeps this from causing outages. "The server is off"
    and "this laptop lost wifi" look identical from here and call for opposite
    actions — and the rollback a wifi drop would trigger has to rsync to that
    same unreachable host, so it could only fail halfway."""
    module = _module(tmp_path)
    _hosts(module,
           local={"ok": True, "why": "HTTP 200", "reach": "reached"},
           peer={"ok": False, "why": "够不着: timeout",
                 "reach": module.UNREACHABLE})

    result = module._assess()
    assert result["ok"] is True, "being unable to see a host is not seeing it down"
    assert result["blind"] is True


def test_blindness_is_reported_even_though_it_never_acts(tmp_path):
    """Silence is the failure mode this whole system suffers from. Not acting is
    correct; not saying anything would repeat the original sin."""
    module = _module(tmp_path)
    module._assess = lambda: {"ok": True, "why": "够不着", "blind": True,
                              "hosts": {}}
    # A recent known-good point, so the only thing that can write to the alarm
    # file here is blindness. Without it the staleness alarm fires too and this
    # would be measuring both at once.
    _known_good(module, {"sha": "fresh", "at": time.time()})

    for _ in range(module.BLIND_BEFORE_ALARM - 1):
        module.check(act=False)
    assert not module.ALARM.exists(), "one missed probe is not an hour of blindness"

    module.check(act=False)
    raised = json.loads(module.ALARM.read_text())["conditions"]["peer"]
    assert raised["kind"] == "peer_unreachable"

    # ...and raised once, or the channel that carries real alarms fills with
    # this one every five minutes until nobody reads it.
    module.check(act=False)
    assert json.loads(module.ALARM.read_text())["conditions"]["peer"]["at"] \
        == raised["at"]


def test_seeing_the_peer_again_clears_the_streak(tmp_path):
    module = _module(tmp_path)
    module._assess = lambda: {"ok": True, "why": "够不着", "blind": True,
                              "hosts": {}}
    module.check(act=False)
    module.check(act=False)
    assert json.loads(module.STATE.read_text())["peer_blind_streak"] == 2

    module._assess = lambda: {"ok": True, "why": "好了", "blind": False,
                              "hosts": {}}
    module.check(act=False)
    assert json.loads(module.STATE.read_text())["peer_blind_streak"] == 0


# ---------------------------------------------------------------------------
# somewhere to go back to
# ---------------------------------------------------------------------------

def test_a_stale_rollback_point_is_said_out_loud(tmp_path):
    """The silence this whole mechanism nearly died of.

    `markgood` runs hourly. It had refused every hour for five days — accounts
    got banned, so one of six stayed enabled, so `doctor` was never clean, so
    `mark` refused — and wrote `拒绝标记` into a log nobody opens. The newest
    known-good point sat 126 hours back, at a commit predating this watchdog,
    `claude-direct` and the database backup, so an automatic rollback would have
    deleted the watchdog as its first act. Every piece was installed and
    running. The mechanism as a whole was disarmed.
    """
    module = _module(tmp_path)
    _up(module)
    _known_good(module, {"sha": "old" * 4, "at": time.time() - 130 * 3600})

    assert module.check(act=False) == 0, "回退点旧, 不等于舰队坏了"
    condition = json.loads(module.ALARM.read_text())["conditions"]["known_good"]
    assert condition["kind"] == "known_good_stale"
    assert "markgood" in condition["why"], "得指向能查出原因的地方"


def test_a_recent_rollback_point_says_nothing(tmp_path):
    """It runs every five minutes. An alarm on a healthy fleet is noise, and
    noise is how the channel that carries the real one stops being read."""
    module = _module(tmp_path)
    _up(module)
    _known_good(module, {"sha": "fresh", "at": time.time() - 600})

    module.check(act=False)
    assert not module.ALARM.exists()


def test_having_never_marked_anything_is_its_own_alarm(tmp_path):
    """A fresh install, or a state directory that got wiped. "Nowhere to roll
    back to" and "somewhere old to roll back to" need different answers."""
    module = _module(tmp_path)
    _up(module)

    module.check(act=False)
    assert json.loads(module.ALARM.read_text())["conditions"]["known_good"][
        "kind"] == "known_good_missing"


def test_it_says_it_once_per_target_not_once_per_probe(tmp_path):
    """288 identical alarms a day is the same as none."""
    module = _module(tmp_path)
    _up(module)
    _known_good(module, {"sha": "old" * 4, "at": time.time() - 130 * 3600})

    module.check(act=False)
    raised_at = json.loads(module.ALARM.read_text())["conditions"][
        "known_good"]["at"]
    for _ in range(4):
        module.check(act=False)
    assert json.loads(module.ALARM.read_text())["conditions"]["known_good"][
        "at"] == raised_at, "同一个陈旧目标不该每五分钟重新举一次"

    # But a NEW mark that is still stale is a new fact — markgood ran, produced
    # something, and it is still not recent enough to roll back to.
    _known_good(module, {"sha": "old" * 4, "at": time.time() - 130 * 3600},
                {"sha": "newer-but-still-old", "at": time.time() - 100 * 3600})
    module.check(act=False)
    fresh = json.loads(module.ALARM.read_text())["conditions"]["known_good"]
    assert fresh["at"] != raised_at and fresh["target"] == "newer-but-still-old"


def test_a_live_problem_reappears_if_the_alarm_file_is_wiped(tmp_path):
    """Deduplication must gate the printing, not the state.

    The first version returned early once it had reported, so it never wrote the
    condition again. Clear the file — or, as happened here, upgrade the watchdog
    while the problem was already live — and the indicator went blank while the
    fleet still had nowhere recent to roll back to. A silence produced by the
    very code written to end a silence.
    """
    module = _module(tmp_path)
    _up(module)
    _known_good(module, {"sha": "old" * 4, "at": time.time() - 130 * 3600})

    module.check(act=False)
    assert json.loads(module.ALARM.read_text())["conditions"]["known_good"]
    module.ALARM.unlink()

    module.check(act=False)
    assert module.ALARM.exists(), "问题还在, 指示灯不该因为文件被清了就熄"
    assert json.loads(module.ALARM.read_text())["conditions"]["known_good"][
        "kind"] == "known_good_stale"


def test_the_check_happens_while_things_are_still_fine(tmp_path):
    """By the time the fleet is down it is far too late to discover there is
    nowhere to go back to."""
    module = _module(tmp_path)
    _up(module)                                # nothing wrong at all
    _known_good(module, {"sha": "old" * 4, "at": time.time() - 130 * 3600})

    assert module.check(act=False) == 0
    assert module.ALARM.exists(), "健康的时候就该发现没地方可退"


def test_a_single_host_fleet_never_looks_for_a_peer(tmp_path, monkeypatch):
    """Almost every install of this will be one machine. Probing a peer that
    does not exist would make the common case fail on a timeout."""
    module = _module(tmp_path)
    monkeypatch.setenv("FLEET_PEER", "")
    asked = []
    module._probe = lambda host: asked.append(host) or {
        "ok": True, "why": "HTTP 200", "reach": "reached"}

    result = module._assess()
    assert asked == [None], asked
    assert result["ok"] is True and result["blind"] is False
