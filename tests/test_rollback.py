"""Going back must not need the person who broke it.

The owner's words: during a repair the fleet became completely unusable, and
that must never happen. What was missing was not care — it was an undo that
does not depend on me being available and getting five manual steps right.

Every rollback in this session was by hand, and I have got each step wrong at
least once: files rsynced to a directory instead of a path, a service booted
out before its replacement was proven, a plist swapped without waiting for the
old job to go. So the tool is deliberately dull, and what it refuses matters
more than what it does.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "fleet-rollback"
    spec = importlib.util.spec_from_loader("rollback", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__dict__["__file__"] = str(path)
    source = path.read_text(encoding="utf-8")
    marker = 'if __name__ == "__main__":'
    if marker in source:
        source = source[: source.index(marker)]
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


def test_a_dirty_tree_stops_everything(monkeypatch):
    """What you would lose is exactly what you were in the middle of."""
    module = _module()
    monkeypatch.setattr(module, "_git", lambda *a: "M src/claude_fleet/control.py")

    failure = module._deploy("abc1234", dry=False)
    assert failure is not None and "未提交改动" in failure


def test_an_untracked_scratch_file_does_not_block_an_emergency_rollback(monkeypatch):
    """`git checkout <sha>` does not touch untracked files, so they cannot be
    lost — and counting them means a stray note in the tree refuses a rollback
    at exactly the hour one is needed. Measured: two planning documents blocked
    `to HEAD` outright."""
    module = _module()
    seen = []

    def fake_git(*args):
        seen.append(args)
        return ""            # clean, once untracked files are excluded

    monkeypatch.setattr(module, "_git", fake_git)
    module._deploy("abc1234", dry=True)

    status = [a for a in seen if a[0] == "status"]
    assert status, "the dirty check must still run"
    assert "--untracked-files=no" in status[0], (
        "an untracked file is not an uncommitted change and must not stop a "
        "rollback"
    )


def test_a_mark_whose_commit_is_gone_is_not_offered(monkeypatch, tmp_path):
    """A known-good point naming a commit git no longer has is a dead end found
    at the worst moment. The production list on this machine carried
    `abc123 / subject` from a test run, displayed beside real entries with
    nothing to tell them apart."""
    module = _module()
    marks = tmp_path / "known-good.jsonl"
    marks.write_text(
        json.dumps({"sha": "realsha", "subject": "real", "at": 1.0}) + "\n" +
        json.dumps({"sha": "abc123", "subject": "subject", "at": 2.0}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(module, "MARKS", marks)
    monkeypatch.setattr(module, "_commit_exists", lambda sha: sha == "realsha")

    assert [m["sha"] for m in module._marks()] == ["realsha"]


def test_the_list_shows_versions_not_marks(monkeypatch, capsys, tmp_path):
    """The hourly job re-marks whatever is live, so one healthy version filled
    six identical lines. The question the list answers is "which distinct
    versions can I go back to", asked by someone already having a bad time."""
    module = _module()
    marks = tmp_path / "known-good.jsonl"
    marks.write_text("\n".join(
        json.dumps({"sha": "aaa", "subject": "same version", "at": 100.0 + i})
        for i in range(6)) + "\n" +
        json.dumps({"sha": "bbb", "subject": "older version", "at": 50.0}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(module, "MARKS", marks)
    monkeypatch.setattr(module, "_commit_exists", lambda sha: True)
    monkeypatch.setattr(module, "_git", lambda *a: "zzz")

    module.cmd_list()
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(lines) == 2, "six marks of one version are one rollback target"
    assert "×6 次" in lines[0], "how many times it proved good is worth keeping"


def test_nothing_moves_before_the_databases_are_snapshotted(monkeypatch):
    """The snapshot is the only thing standing between a bad migration and a
    lost fleet, so a deploy that cannot take one must not proceed to the part
    that changes things."""
    module = _module()
    monkeypatch.setattr(module, "_git", lambda *a: "")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: pytest.fail(
        "reached checkout/rsync/restart without a usable backup"))

    def no_backup(host, body, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(module, "_run_python_on", no_backup)

    failure = module._deploy("abc1234", dry=False)
    assert failure is not None and "备份失败" in failure


def test_marking_refuses_when_a_host_is_not_serving(monkeypatch, capsys):
    """"Known good" is a claim about a running fleet, not about a diff.

    Tests have been green through two outages in this session. If marking
    accepted that as evidence, `rollback now` would restore a version that was
    never observed to work.
    """
    module = _module()
    monkeypatch.setattr(module, "_git", lambda *a: "abc1234")
    monkeypatch.setattr(module, "_health", lambda host: {
        "coordinator": True,
        "dataplane": host is None,          # peer's data plane is down
        "doctor_clean": True,
    })

    assert module.cmd_mark(None, dry=False) == 1
    assert "拒绝标记" in capsys.readouterr().err


def test_marking_refuses_when_doctor_cannot_be_read(monkeypatch):
    """Unknown is not clean. A host that cannot answer is not evidence of
    health, and treating it as such is how a bad point gets recorded."""
    module = _module()
    monkeypatch.setattr(module, "_git", lambda *a: "abc1234")
    monkeypatch.setattr(module, "_health", lambda host: {
        "coordinator": True, "dataplane": True, "doctor_clean": None,
    })
    assert module.cmd_mark(None, dry=False) == 1


def test_marking_records_a_version_that_has_soaked(monkeypatch, tmp_path):
    """The full contract: live long enough, clean while live, serving now."""
    import time as _time

    module = _module()
    marks = tmp_path / "known-good.jsonl"
    seen = tmp_path / "seen.json"
    seen.write_text(json.dumps({"sha": "abc1234def",
                                "at": _time.time() - 10 * 3600}))
    monkeypatch.setattr(module, "MARKS", marks)
    monkeypatch.setattr(module, "SEEN", seen)
    monkeypatch.setattr(module, "_git", lambda *a: (
        "abc1234def" if a[0] == "rev-parse" else "a subject"))
    monkeypatch.setattr(module, "_soak_clean", lambda hours: None)
    monkeypatch.setattr(module, "_hosts_agree", lambda: None)
    monkeypatch.setattr(module, "_health", lambda host: {
        "coordinator": True, "dataplane": True, "doctor_clean": True,
    })

    assert module.cmd_mark(None, dry=False) == 0
    record = json.loads(marks.read_text(encoding="utf-8").splitlines()[-1])
    assert record["sha"] == "abc1234def"


def test_rollback_now_refuses_without_a_known_good_point(monkeypatch, capsys):
    """Better to say there is nothing to go back to than to pick something."""
    module = _module()
    monkeypatch.setattr(module, "_marks", lambda: [])
    assert module.main(["now"]) == 2
    assert "没有已知良好点" in capsys.readouterr().err


def test_the_database_is_never_in_the_deployed_set():
    """Rolling code back is safe; rolling state back is not, and a schema
    written by a newer version is not something git can undo."""
    module = _module()
    joined = " ".join(module.DEPLOYED)
    assert "sqlite" not in joined and "state" not in joined
    assert all(not p.startswith("/") for p in module.DEPLOYED)


def test_a_probe_counts_any_http_answer(monkeypatch):
    """The question is whether the process is turning, not what it returns —
    the same lesson the launch gate learned when a 400 was read as death."""
    module = _module()

    class _Result:
        def __init__(self, code):
            self.stdout = code
            self.returncode = 0

    monkeypatch.setattr(module.subprocess, "run",
                        lambda *a, **k: _Result("400"))
    assert module._probe("http://127.0.0.1:1/") is True

    monkeypatch.setattr(module.subprocess, "run",
                        lambda *a, **k: _Result("000"))
    assert module._probe("http://127.0.0.1:1/") is False


def test_a_fresh_deploy_cannot_be_marked_good(monkeypatch, tmp_path, capsys):
    """Healthy at deploy time is not evidence of anything.

    Every outage in this system's worst day appeared minutes after a deploy
    that had just passed every check. A mark taken then records the moment the
    version looked best, which is exactly the moment it tells you least.
    """
    module = _module()
    monkeypatch.setattr(module, "SEEN", tmp_path / "seen.json")
    monkeypatch.setattr(module, "_git", lambda *a: (
        "abc123" if a[0] == "rev-parse" else "subject"))
    monkeypatch.setattr(module, "_health", lambda host: {
        "coordinator": True, "dataplane": True, "doctor_clean": True})

    assert module.cmd_mark(None, dry=False) == 1
    assert "浸泡不足" in capsys.readouterr().err


def test_the_soak_can_be_overridden_but_only_deliberately(monkeypatch, tmp_path):
    """`--force` exists because the operator may know something the tool does
    not. It must be an explicit act, never a fallback."""
    module = _module()
    monkeypatch.setattr(module, "SEEN", tmp_path / "seen.json")
    monkeypatch.setattr(module, "MARKS", tmp_path / "marks.jsonl")
    monkeypatch.setattr(module, "_git", lambda *a: (
        "abc123" if a[0] == "rev-parse" else "subject"))
    monkeypatch.setattr(module, "_health", lambda host: {
        "coordinator": True, "dataplane": True, "doctor_clean": True})

    assert module.cmd_mark(None, dry=False, force=True) == 0


def test_a_soaked_version_with_slow_decisions_is_refused(monkeypatch, tmp_path,
                                                          capsys):
    """Surviving is not the same as serving well. A version that ran two hours
    while sessions stalled is not what anyone wants to go back to."""
    import json as _json
    import time as _time

    module = _module()
    seen = tmp_path / "seen.json"
    seen.write_text(_json.dumps({"sha": "abc123",
                                 "at": _time.time() - 10 * 3600}))
    monkeypatch.setattr(module, "SEEN", seen)
    monkeypatch.setattr(module, "_git", lambda *a: (
        "abc123" if a[0] == "rev-parse" else "subject"))
    monkeypatch.setattr(module, "_soak_clean",
                        lambda hours: "本机 在浸泡期内有 7 次慢决策")
    monkeypatch.setattr(module, "_health", lambda host: {
        "coordinator": True, "dataplane": True, "doctor_clean": True})

    assert module.cmd_mark(None, dry=False) == 1
    assert "慢决策" in capsys.readouterr().err


def _markable(module, monkeypatch, tmp_path):
    """Everything except the check under test says yes."""
    import json as _json
    import time as _time

    seen = tmp_path / "seen.json"
    seen.write_text(_json.dumps({"sha": "abc123", "at": _time.time() - 10 * 3600}))
    monkeypatch.setattr(module, "SEEN", seen)
    marks = tmp_path / "marks.jsonl"
    monkeypatch.setattr(module, "MARKS", marks)
    monkeypatch.setattr(module, "_git", lambda *a: (
        "abc123" if a[0] == "rev-parse" else "subject"))
    monkeypatch.setattr(module, "_health", lambda host: {
        "coordinator": True, "dataplane": True, "doctor_clean": True})
    return marks


def test_broken_authentication_cannot_be_marked_known_good(monkeypatch, tmp_path,
                                                            capsys):
    """The blind spot that made the whole mechanism untrustworthy.

    The soak used to ask one question — "were there slow decisions?" — and that
    question cannot see the failure it most needs to catch. Break authentication
    and every `/v1/route/decide` returns 401; no routing decision is made, so no
    `route.slow` row is written, so the soak reads perfectly clean. `/health`
    takes no credentials either, so liveness agrees. A build that rejected every
    real request in the fleet would be recorded as the known-good point a future
    rollback restores to.

    Measured against the live coordinator: a wrong token gives 401, a correct
    one gets into the handler. So ask directly instead of inferring.
    """
    module = _module()
    marks = _markable(module, monkeypatch, tmp_path)
    monkeypatch.setattr(module, "_run_python_on",
                        lambda host, body, **k: {"status": 401})

    assert module.cmd_mark(None, dry=False) == 1
    assert "鉴权被拒" in capsys.readouterr().err
    assert not marks.exists()


def test_an_idle_host_is_not_mistaken_for_a_broken_one(monkeypatch, tmp_path):
    """Counting organic traffic was the obvious fix and it does not work.

    Measured on the peer: two hours, zero routing decisions — not because
    anything was broken but because nobody was using it. Gating on volume would
    refuse to ever certify a quiet host, which quietly stops known-good points
    from being recorded at all. That failure is invisible until the day you need
    to roll back and find nothing to roll back to.
    """
    module = _module()
    marks = _markable(module, monkeypatch, tmp_path)

    def answer(host, body, **kwargs):
        if "pin/check" in body:
            return {"status": 200}
        if "hashlib" in body:
            return {"digest": "same-on-both", "files": 70}
        return {"route.slow": 0, "route.changed": 0}   # completely idle

    monkeypatch.setattr(module, "_run_python_on", answer)

    assert module.cmd_mark(None, dry=False) == 0
    assert "abc123" in marks.read_text(encoding="utf-8")


def test_a_soaked_version_that_answers_but_answers_slowly_is_refused(
        monkeypatch, tmp_path, capsys):
    """Serviceability and quality are two different questions, and passing the
    first does not excuse failing the second."""
    module = _module()
    marks = _markable(module, monkeypatch, tmp_path)

    def answer(host, body, **kwargs):
        if "pin/check" in body:
            return {"status": 200}
        return {"route.slow": 7, "route.changed": 400}

    monkeypatch.setattr(module, "_run_python_on", answer)

    assert module.cmd_mark(None, dry=False) == 1
    assert "慢决策" in capsys.readouterr().err
    assert not marks.exists()


def test_a_coordinator_that_does_not_answer_is_not_known_good(monkeypatch,
                                                               tmp_path, capsys):
    """Nothing listening is not the same as nothing wrong."""
    module = _module()
    _markable(module, monkeypatch, tmp_path)
    monkeypatch.setattr(module, "_run_python_on",
                        lambda host, body, **k: {"status": 0,
                                                 "error": "connection refused"})

    assert module.cmd_mark(None, dry=False) == 1
    assert "没有应答" in capsys.readouterr().err


def test_a_failed_restart_stops_the_rollback(monkeypatch):
    """A restart that fails leaves the OLD process holding the port, and the
    probe counts any HTTP answer as alive. Reporting success there is worse than
    failing: you would believe you had recovered while the peer still ran the
    code you were rolling away from."""
    module = _module()
    monkeypatch.setattr(module, "_git", lambda *a: "")
    monkeypatch.setattr(module, "_probe", lambda url: True)
    # The pre-deploy snapshot runs first and is not what is under test here.
    monkeypatch.setattr(module, "_run_python_on",
                        lambda host, body, **k: {"path": "/tmp/snap.sqlite3",
                                                 "bytes": 1})

    class _Result:
        def __init__(self, code):
            self.returncode = code
            self.stdout = ""
            self.stderr = "sudo: a password is required"

    def fake_run(command, **kwargs):
        # rsync succeeds; the peer restart is the one that fails.
        return _Result(1 if command[0] == "ssh" else 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    failure = module._deploy("abc1234", dry=False)
    assert failure is not None and "重启" in failure
    assert "password" in failure, "the operator needs the actual reason"


def test_first_seen_resets_when_the_version_changes(monkeypatch, tmp_path):
    """Otherwise a new deploy would inherit the previous version's soak — the
    one guarantee this whole mechanism rests on."""
    import time as _time

    module = _module()
    monkeypatch.setattr(module, "SEEN", tmp_path / "seen.json")

    first = module._first_seen("aaa")
    assert abs(module._first_seen("aaa") - first) < 1, "same commit, same clock"

    later = module._first_seen("bbb")
    assert later >= first
    assert abs(later - _time.time()) < 2, "a different commit starts over"


# ---------------------------------------------------------------------------
# a mark is a claim about the fleet, not about this laptop's git
# ---------------------------------------------------------------------------

def _digests(module, monkeypatch, by_host):
    calls = []

    def fake(host, body, timeout=60):
        calls.append(host)
        if isinstance(by_host[host], Exception):
            raise by_host[host]
        return {"digest": by_host[host], "files": 70}

    monkeypatch.setattr(module, "_run_python_on", fake)
    return calls


def test_two_hosts_in_sync_can_be_marked(monkeypatch, capsys):
    module = _module()
    _digests(module, monkeypatch, {None: "abc123", module.PEER: "abc123"})
    assert module._hosts_agree() is None
    assert "abc123" in capsys.readouterr().out, "两边的指纹都要打出来, 好核对"


def test_a_half_finished_deploy_cannot_be_marked_good(monkeypatch):
    """Only one machine has git, so the sha is read here and the other is taken
    on faith — and `_health` asks whether the peer is *answering*, which a host
    running last week's code does perfectly well.

    So a mark could be true of one machine and a lie about the other, and the
    lie surfaces at the worst possible hour: rolling "back" to it would move the
    peer somewhere it has never been.
    """
    module = _module()
    _digests(module, monkeypatch, {None: "new-code", module.PEER: "old-code"})
    problem = module._hosts_agree()
    assert problem and "不是同一份代码" in problem
    assert "to HEAD" in problem, "得说出怎么把两边弄一致"


def test_a_host_that_cannot_be_asked_is_not_assumed_to_agree(monkeypatch):
    """Unknown is not agreement. Treating an unreachable peer as matching is how
    the check would quietly stop being one."""
    module = _module()
    _digests(module, monkeypatch,
             {None: "abc123", module.PEER: RuntimeError("ssh: timeout")})
    problem = module._hosts_agree()
    assert problem and "算不出指纹" in problem


def test_marking_refuses_when_the_two_hosts_disagree(monkeypatch, tmp_path,
                                                     capsys):
    """Drives `cmd_mark`, not `_hosts_agree`.

    Mutation testing said this was needed: deleting the call from `cmd_mark`
    left every check above still green, because they all exercise the function
    directly. A correct check nobody calls is the same as no check, and it is
    the exact shape of two other bugs found this session — the Fleet policy that
    ran only at startup, and the collision refusal that `pin_check` never used.
    """
    module = _module()
    _markable(module, monkeypatch, tmp_path)

    def answer(host, body, **kwargs):
        if "pin/check" in body:
            return {"status": 200}
        if "hashlib" in body:
            return {"digest": "here" if host is None else "there", "files": 70}
        return {"route.slow": 0, "route.changed": 5}

    monkeypatch.setattr(module, "_run_python_on", answer)

    assert module.cmd_mark(None, dry=False) == 1
    assert "不是同一份代码" in capsys.readouterr().err


def test_the_fingerprint_covers_what_actually_runs(monkeypatch):
    """`packaging/` is deliberately excluded: those templates are read only when
    somebody runs an installer, so a difference there says nothing about what is
    serving — and including it would refuse marks for a reason with no effect on
    service, which is exactly how this gate jammed for five days the last time.
    """
    module = _module()
    assert "src/claude_fleet" in module._DIGEST_PATHS
    assert "vendor/teamclaude/src" in module._DIGEST_PATHS
    assert "scripts" in module._DIGEST_PATHS
    assert "packaging" not in module._DIGEST_PATHS


# ---------------------------------------------------------------------------
# what "healthy enough to mark" is allowed to mean
# ---------------------------------------------------------------------------

def _fake_doctor(tmp_path, checks):
    """Stand a fake `claude_fleet.doctor` in front of the real one.

    The snippet runs under a separate interpreter — here, and over ssh on the
    other host — and puts `<REPO>/src` at the front of `sys.path`. Pointing REPO
    at a directory holding a fake package therefore exercises the actual string
    that ships, rather than a Python-level reimplementation of it."""
    package = tmp_path / "src" / "claude_fleet"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "doctor.py").write_text(
        "def doctor():\n    return %r\n" % ({"checks": checks},), encoding="utf-8")
    return tmp_path


def test_a_banned_account_does_not_make_a_version_unmarkable(monkeypatch,
                                                             tmp_path):
    """The five-day silence, and the best-hidden failure in this system.

    Accounts got banned, so one of six stayed enabled, so
    `two_enabled_oauth_accounts` failed, so `doctor` was never clean, so `mark`
    refused — every hour, into a log nobody opens. The newest known-good point
    stayed 126 hours old: a commit predating `fleet-watchdog`, `claude-direct`
    and the database backup, so an automatic rollback would have deleted the
    watchdog as its first act and then nothing would have been left to notice.

    Marking is a claim about a commit. How many subscriptions the owner
    currently owns is not a property of the commit.
    """
    module = _module()
    monkeypatch.setattr(module, "REPO", _fake_doctor(tmp_path, [
        {"name": "coordinator_listening", "ok": True},
        {"name": "two_enabled_oauth_accounts", "ok": False},
        {"name": "no_account_needs_relogin", "ok": False},
        {"name": "account_quota_fresh", "ok": False},
        {"name": "request_path_not_throttled", "ok": False},
    ]))
    assert module._doctor_ok(None) is True


def test_a_real_failure_still_refuses(monkeypatch, tmp_path):
    """The other half. A deny-list that swallowed everything would turn the gate
    into a rubber stamp, which is worse than the jam it was fixing."""
    module = _module()
    monkeypatch.setattr(module, "REPO", _fake_doctor(tmp_path, [
        {"name": "two_enabled_oauth_accounts", "ok": False},
        {"name": "db_integrity", "ok": False},
    ]))
    assert module._doctor_ok(None) is False


def test_a_check_nobody_has_thought_of_yet_still_counts(monkeypatch, tmp_path):
    """Why this is a deny-list and not an allow-list. A check added next month
    falls inside an allow-list's blind spot and `mark` would certify a version
    it would have failed — unsafe, and silent about it."""
    module = _module()
    monkeypatch.setattr(module, "REPO", _fake_doctor(tmp_path, [
        {"name": "some_check_invented_after_this_was_written", "ok": False},
    ]))
    assert module._doctor_ok(None) is False


def test_a_skipped_check_is_not_a_failure(monkeypatch, tmp_path):
    """`doctor` skips checks it cannot date — undated log lines are unknown, not
    bad — and treating unknown as failure would jam the gate a second way."""
    module = _module()
    monkeypatch.setattr(module, "REPO", _fake_doctor(tmp_path, [
        {"name": "dataplane_not_parked", "ok": False, "skipped": True},
    ]))
    assert module._doctor_ok(None) is True


# ---------------------------------------------------------------------------
# where the repository is left standing
# ---------------------------------------------------------------------------

def _throwaway_repo(tmp_path):
    """A real repository, because the property under test is an ordering: the
    branch has to be read BEFORE the tree moves. Against a stubbed `_git` that
    distinction disappears and the test proves nothing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo)]
    subprocess.run(git + ["init", "--quiet", "-b", "trunk"], check=True)
    subprocess.run(git + ["config", "user.email", "t@example.com"], check=True)
    subprocess.run(git + ["config", "user.name", "t"], check=True)
    (repo / "a").write_text("1", encoding="utf-8")
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "--quiet", "-m", "one"], check=True)
    sha = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True,
                         text=True, check=True).stdout.strip()
    return repo, sha


def _with_the_network_stubbed(module, monkeypatch):
    """rsync, ssh and launchctl are stubbed. **git is not** — it is the thing
    being measured, and a blanket stub silently answers `rev-parse` with an
    empty string, which reads as "still on a branch" and makes the whole notice
    unreachable. That is how the first draft of these two tests passed nothing
    while looking correct."""
    monkeypatch.setattr(module, "_run_python_on",
                        lambda host, body, timeout=60: {"path": "/x", "bytes": 1})
    monkeypatch.setattr(module, "_health", lambda host: {
        "coordinator": True, "dataplane": True, "doctor_clean": True})

    real = subprocess.run

    class _Ok:
        returncode = 0
        stdout = stderr = ""

    def only_the_network(argv, *a, **k):
        if argv and argv[0] == "git":
            return real(argv, *a, **k)
        return _Ok()

    monkeypatch.setattr(module.subprocess, "run", only_the_network)


def test_a_rollback_says_the_repo_is_now_detached(monkeypatch, tmp_path, capsys):
    """Moving the tree to a commit leaves HEAD attached to nothing, and this
    said so nowhere. Detaching is right — a rollback deliberately puts the tree
    off the branch tip. Silence is not: whoever is at the keyboard right after
    an outage is exactly the person about to commit a fix, and a commit made in
    that state belongs to no branch and is one step from unreachable."""
    module = _module()
    repo, sha = _throwaway_repo(tmp_path)
    monkeypatch.setattr(module, "REPO", repo)
    _with_the_network_stubbed(module, monkeypatch)

    assert module._deploy(sha, dry=False) is None
    output = capsys.readouterr().out
    assert "游离 HEAD" in output
    assert "trunk" in output, "得说出回哪儿去, 而不是只说你迷路了"


def test_it_does_not_invent_a_branch_that_was_never_there(monkeypatch, tmp_path,
                                                          capsys):
    """Rolling back from an already-detached tree, which is what a second
    rollback in a row looks like. Naming a branch here would be a guess."""
    module = _module()
    repo, sha = _throwaway_repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "-c", "advice.detachedHead=false",
                    "switch", "--detach", sha], check=True)
    monkeypatch.setattr(module, "REPO", repo)
    _with_the_network_stubbed(module, monkeypatch)

    assert module._deploy(sha, dry=False) is None
    output = capsys.readouterr().out
    assert "游离 HEAD" in output
    assert "说不出该回到哪个分支" in output
    assert "switch" not in output, "`switch HEAD` 不是一条能用的命令, 是句瞎话"

