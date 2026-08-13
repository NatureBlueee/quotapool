"""A probe that could not finish is not a probe that found something.

The auth probe exists because credentials expired on 2026-07-17 and nothing
noticed for eleven days: a long-lived daemon held the old token in memory, so
every already-running process kept working while every new one died. Nothing in
the transcripts said "auth". That is the condition worth a STALLED verdict.

It is not the condition that fires most often. The probe spawns a real Claude
process and waits 120 seconds; on a machine at load average 107 across ten
cores it simply does not finish. Measured today, with a manual run minutes
earlier — at load 30 — passing both paths in 31s and 24.8s. The credentials
were never in question, and the health check said "a new process cannot
authenticate".

Saying that about a timeout is how the real one gets ignored. The probe already
recorded the difference in its `reason` field; only the rendering conflated
them.

Reported, not muted, in both cases: a probe that cannot finish is a probe that
is not watching, and an hour with nothing validated is worth knowing about. It
is just not the emergency the word "authenticate" implies.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "fleet-health"
    spec = importlib.util.spec_from_loader("fleethealth", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__dict__["__file__"] = str(path)
    source = path.read_text(encoding="utf-8")
    marker = 'if __name__ == "__main__":'
    if marker in source:
        source = source[: source.index(marker)]
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


def _verdict(module, auth):
    report = {
        "auth_probe": auth,
        "problems": [], "notes": [],
        "serving": {"routes_in_window": 100, "parks_in_window": 0},
        "accounts": [], "usable_accounts": 1, "total_accounts": 1,
        "state_size": {"events": 1, "events_6h": 1, "executions_active": 1},
        "services": {}, "waiting": {}, "slow_decisions": {},
    }
    module._judge(report)
    return report["verdict"], report["problems"], report["notes"]


def _find(problems, fragment):
    return [p for p in problems if fragment in p["problem"]]


def test_a_rejected_credential_is_still_the_emergency(module_env=None):
    module = _module()
    verdict, problems, _notes = _verdict(module, {
        "ok": False,
        "bare": {"ok": False, "reason": "auth"},
        "fleet": {"ok": False, "reason": "auth"},
    })
    assert verdict == module.STALLED
    assert _find(problems, "cannot authenticate")


def test_a_timeout_is_not_reported_as_an_auth_failure():
    """The words matter: this is the sentence that gets ignored later."""
    module = _module()
    verdict, problems, _notes = _verdict(module, {
        "ok": False,
        "bare": {"ok": False, "reason": "timeout"},
        "fleet": {"ok": False, "reason": "timeout"},
    })
    assert verdict == module.DEGRADED, "a probe that did not finish found nothing"
    assert not _find(problems, "cannot authenticate"), (
        "a machine too busy to run the probe was reported as a dead credential"
    )
    assert _find(problems, "超时")


def test_a_timeout_is_still_reported():
    """Muting it would be the other failure: an hour with nothing validated."""
    module = _module()
    _verdict_, problems, _notes = _verdict(module, {
        "ok": False,
        "bare": {"ok": False, "reason": "timeout"},
        "fleet": {"ok": False, "reason": "timeout"},
    })
    assert problems, "the probe stopped watching and said nothing"


def test_one_path_rejected_outranks_the_other_timing_out():
    """Mixed reasons must not let a real rejection hide behind a timeout."""
    module = _module()
    verdict, problems, _notes = _verdict(module, {
        "ok": False,
        "bare": {"ok": False, "reason": "auth"},
        "fleet": {"ok": False, "reason": "timeout"},
    })
    assert verdict == module.STALLED
    assert _find(problems, "cannot authenticate")


def test_a_third_reason_is_named_rather_than_guessed_at():
    """`spawn_failed` is neither a timeout nor a rejection, and pretending it
    is either sends the reader somewhere useless."""
    module = _module()
    verdict, problems, _notes = _verdict(module, {
        "ok": False,
        "bare": {"ok": False, "reason": "spawn_failed"},
        "fleet": {"ok": True},
    })
    assert verdict == module.DEGRADED
    assert _find(problems, "spawn_failed")
