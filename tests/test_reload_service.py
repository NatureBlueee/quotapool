"""`launchctl bootout` returns before the job is gone.

Issuing `bootstrap` immediately fails with `Bootstrap failed: 5: Input/output
error`, and the service is now stopped with nothing running. That is what took
the data plane down: bootout, immediate bootstrap, EIO, port 13456 unbound,
live sessions refused with ConnectionRefused.

Reproduced afterwards on an isolated label whose program lingers six seconds on
SIGTERM:

    bootout; bootstrap                    -> Bootstrap failed: 5
    bootout; poll until gone; bootstrap   -> ok

The coordinator had absorbed the same command several times without complaint,
which is exactly why the pattern looked safe: Python exits on SIGTERM at once,
so the gap was never long enough to matter. The data plane is Node holding live
connections, and its teardown outlasted the zero-second gap.

The rollback is the other half. A tool that can fail after stopping the service
has to be able to put it back, and a rollback that has never run is not a
rollback — the end-to-end version of these was exercised against a throwaway
label serving on port 18899: a plist that cannot start failed the probe, the
backup was restored, and the port answered 200 again.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "fleet-reload-service"
    spec = importlib.util.spec_from_loader("reloadsvc", loader=None)
    module = importlib.util.module_from_spec(spec)
    # A script that resolves its own location needs `__file__`, and every
    # real import provides it. Omitting it here made the loader unfaithful
    # in exactly the way that hides a working script behind a NameError.
    module.__dict__["__file__"] = str(path)
    source = path.read_text(encoding="utf-8").replace(
        'if __name__ == "__main__":\n    sys.exit(main())', "")
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


def test_bootstrap_waits_for_the_old_job_to_disappear(monkeypatch):
    """The ordering the outage came from, asserted directly."""
    module = _module()
    events = []
    still_loaded = {"n": 3}          # gone only on the fourth check

    monkeypatch.setattr(module, "_bootout",
                        lambda label: events.append("bootout"))
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    monkeypatch.setattr(module, "shutil", type("S", (), {
        "copyfile": staticmethod(lambda a, b: None)})())

    def loaded(label):
        events.append("poll")
        if still_loaded["n"] > 0:
            still_loaded["n"] -= 1
            return True
        return False

    def bootstrap(plist):
        events.append("bootstrap")
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr(module, "_loaded", loaded)
    monkeypatch.setattr(module, "_bootstrap", bootstrap)

    assert module._install("lbl", Path("/tmp/live.plist"),
                           Path("/tmp/new.plist"), None, "test") is None

    assert events[0] == "bootout"
    assert events[-1] == "bootstrap"
    assert events.count("poll") >= 3, (
        "bootstrap was issued without waiting for the old job to go; that is "
        "the exact sequence that returned EIO and left the port unbound"
    )


def test_a_teardown_that_never_finishes_is_reported_not_bootstrapped(monkeypatch):
    """Better to say so than to bootstrap into a half-gone job."""
    module = _module()
    monkeypatch.setattr(module, "_bootout", lambda label: None)
    monkeypatch.setattr(module, "_loaded", lambda label: True)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    monkeypatch.setattr(module.time, "time",
                        _advancing(start=0.0, step=5.0))
    monkeypatch.setattr(module, "shutil", type("S", (), {
        "copyfile": staticmethod(lambda a, b: None)})())
    monkeypatch.setattr(module, "_bootstrap", lambda plist: (_ for _ in ()).throw(
        AssertionError("must not bootstrap over a job that is still there")))

    failure = module._install("lbl", Path("/tmp/live.plist"),
                              Path("/tmp/new.plist"), None, "test")
    assert failure is not None and "tearing down" in failure


def test_a_started_service_that_does_not_answer_counts_as_failure(monkeypatch):
    """bootstrap succeeding is not the service working.

    The plist that broke the shadow test bootstrapped fine and then exited
    immediately; only the probe caught it.
    """
    module = _module()
    monkeypatch.setattr(module, "_bootout", lambda label: None)
    monkeypatch.setattr(module, "_loaded", lambda label: False)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    monkeypatch.setattr(module, "shutil", type("S", (), {
        "copyfile": staticmethod(lambda a, b: None)})())
    monkeypatch.setattr(module, "_bootstrap", lambda plist: type(
        "R", (), {"returncode": 0, "stderr": "", "stdout": ""})())
    monkeypatch.setattr(module, "_probe", lambda url: "connection refused")

    failure = module._install("lbl", Path("/tmp/live.plist"),
                              Path("/tmp/new.plist"), "http://127.0.0.1:1/",
                              "test")
    assert failure is not None and "did not answer" in failure


def test_the_shadow_never_carries_the_real_label_or_program(tmp_path, monkeypatch):
    """It must not race the real service for a port, or write its logs."""
    import plistlib

    module = _module()
    source = tmp_path / "new.plist"
    source.write_bytes(plistlib.dumps({
        "Label": "com.claude-fleet.dataplane",
        "ProgramArguments": ["/usr/bin/node", "server.js"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardErrorPath": "/tmp/should-not-be-written.log",
        "ProcessType": "Interactive",
    }))

    captured = {}

    def bootstrap(plist):
        captured["data"] = plistlib.loads(Path(plist).read_bytes())
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr(module, "_bootstrap", bootstrap)
    monkeypatch.setattr(module, "_bootout", lambda label: None)
    monkeypatch.setattr(module, "_loaded", lambda label: False)
    monkeypatch.setattr(module, "_lint", lambda plist: None)

    module._shadow_check(source, tmp_path)

    data = captured["data"]
    assert data["Label"] != "com.claude-fleet.dataplane"
    assert data["ProgramArguments"] == ["/usr/bin/true"]
    assert "StandardErrorPath" not in data
    assert "KeepAlive" not in data
    assert data["ProcessType"] == "Interactive", (
        "the shadow exists to prove launchd accepts the keys under test; "
        "stripping ProcessType would test nothing"
    )


def _advancing(start: float, step: float):
    state = {"t": start}

    def now():
        state["t"] += step
        return state["t"]

    return now


def test_restart_only_reports_a_service_that_starts_and_does_not_answer(
    monkeypatch, capsys
):
    """No configuration changed, so there is nothing to roll back TO.

    The probe is the entire guarantee in this mode. Exiting 0 because
    `systemctl restart` returned 0 would call a service healthy when it started
    and died — and picking up new code is exactly when that happens.
    """
    module = _module()
    monkeypatch.setattr(module, "SYSTEMD", True)
    monkeypatch.setattr(module, "_systemd_restart", lambda unit: None)
    monkeypatch.setattr(module, "_probe", lambda url: "connection refused")

    code = module.main(["claude-fleet-dataplane", "--restart-only",
                        "--probe", "http://127.0.0.1:13456/x"])
    assert code == 1
    assert "无响应" in capsys.readouterr().err


def test_restart_only_succeeds_when_the_probe_answers(monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "SYSTEMD", True)
    monkeypatch.setattr(module, "_systemd_restart", lambda unit: None)
    monkeypatch.setattr(module, "_probe", lambda url: None)

    assert module.main(["claude-fleet-dataplane", "--restart-only",
                        "--probe", "http://127.0.0.1:13456/x"]) == 0


def test_a_unit_that_does_not_come_back_active_is_a_failure(monkeypatch):
    """`systemctl restart` returning 0 is not the unit running."""
    module = _module()
    calls = []

    def run(args, check=False):
        calls.append(args)
        if args[:2] == ["sudo", "systemctl"]:
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        return type("R", (), {"returncode": 0, "stdout": "failed\n",
                              "stderr": ""})()

    monkeypatch.setattr(module, "_run", run)
    failure = module._systemd_restart("claude-fleet-dataplane")
    assert failure is not None and "failed" in failure


def test_a_plist_swap_is_refused_on_systemd(monkeypatch, capsys):
    """Unit files are root-owned and belong to configuration management.

    Silently doing something else — restarting without applying the file the
    caller passed — would be worse than refusing.
    """
    module = _module()
    monkeypatch.setattr(module, "SYSTEMD", True)
    code = module.main(["claude-fleet-dataplane", "/tmp/whatever.plist"])
    assert code == 2
    assert "restart-only" in capsys.readouterr().err


def test_passing_the_live_plist_is_a_restart_not_a_crash(tmp_path, monkeypatch):
    """The obvious way to ask for "restart with what it already has".

    It used to raise SameFileError out of shutil.copyfile — an unhandled
    traceback where a plain answer belongs. It happened to crash before
    stopping anything, so nothing broke, but that was ordering luck.
    """
    module = _module()
    live = tmp_path / "live.plist"
    live.write_text("x", encoding="utf-8")

    events = []
    monkeypatch.setattr(module, "_bootout", lambda label: events.append("bootout"))
    monkeypatch.setattr(module, "_loaded", lambda label: False)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    monkeypatch.setattr(module, "_bootstrap", lambda plist: (
        events.append("bootstrap"),
        type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})())[1])

    assert module._install("lbl", live, live, None, "test") is None
    assert events == ["bootout", "bootstrap"]

