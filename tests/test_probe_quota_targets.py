"""Which accounts are worth spending a probe on.

Both hosts happen to have nothing to probe right now, so the live run proves
only that the script does not crash. These pin the decision itself, which is
the part that has to be right the next time an account goes unmeasured.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fleet-probe-quota"


def _load():
    spec = importlib.util.spec_from_loader(
        "fleet_probe_quota",
        importlib.machinery.SourceFileLoader("fleet_probe_quota", str(SCRIPT)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe_quota = _load()


def account(**overrides):
    base = {
        "id": "acct",
        "kind": "owned",
        "state": "healthy",
        "weekly_used": 0.4,
        "five_hour_used": 0.2,
        "weekly_floor": 0,
        "updated_at": 0,
    }
    base.update(overrides)
    return base


def targets(accounts, disabled=frozenset()):
    return [item["id"] for item in probe_quota._unmeasured(accounts, set(disabled))]


def test_fully_measured_account_is_not_probed():
    assert targets([account()]) == []


def test_unknown_weekly_is_probed():
    assert targets([account(weekly_used=None)]) == ["acct"]


def test_unknown_five_hour_with_headroom_is_probed():
    """The case that is easy to miss.

    A known weekly reading with plenty left still leaves the account invisible
    to the optimizer if the five-hour bucket has never been read, because
    control.py:1685 drops the account when EITHER bucket is None.
    """
    assert targets([account(weekly_used=0.1, five_hour_used=None)]) == ["acct"]


def test_unknown_five_hour_on_a_spent_weekly_is_not_probed():
    """Not capacity the scheduler is missing — capacity that is gone.

    Learning the five-hour bucket changes nothing until the weekly window
    resets, so the probe would buy no decision.
    """
    assert targets([account(weekly_used=1.0, five_hour_used=None)]) == []


def test_the_floor_counts_as_spent_for_a_shared_account():
    """0.65 used against a 0.4 reserve leaves nothing spendable."""
    spent = account(kind="shared", weekly_used=0.65, weekly_floor=0.4,
                    five_hour_used=None)
    assert targets([spent]) == []
    usable = account(kind="shared", weekly_used=0.2, weekly_floor=0.4,
                     five_hour_used=None)
    assert targets([usable]) == ["acct"]


def test_a_disabled_account_is_never_probed():
    """A dead refresh token would spend the probe on a guaranteed 401."""
    assert targets([account(state="disabled", weekly_used=None)]) == []


def test_the_data_plane_veto_is_honoured_over_a_healthy_database_row():
    """The retired account on this Mac is exactly this shape.

    `primary-current` is disabled at the data plane and still `healthy` in the
    control database, so filtering on the database row alone would probe it.
    """
    stale = account(id="primary-current", state="healthy", weekly_used=None)
    assert targets([stale]) == ["primary-current"]
    assert targets([stale], disabled={"primary-current"}) == []


def test_probe_removes_its_pin_even_when_the_run_fails(monkeypatch):
    """A probe that leaks a hard_pin would park the workload forever.

    That is the DANGLING_WORKLOAD_POLICY failure class, so the cleanup is in a
    `finally` and this holds it there.
    """
    calls = []

    class Result:
        def __init__(self, returncode):
            self.returncode = returncode
            self.stdout = ""
            self.stderr = "boom"

    def fake_run(fleet, args, timeout=60):
        calls.append(args)
        return Result(0 if args[2] == "policy" else 1)

    monkeypatch.setattr(probe_quota, "_run", fake_run)
    result = probe_quota.probe("fleet", "/tmp/db", "acct")

    assert result["status"] == "failed"
    assert calls[-1] == ["--db", "/tmp/db", "policy", "set",
                         probe_quota.PROBE_WORKLOAD]


def test_a_refused_pin_does_not_run_anything(monkeypatch):
    """The shared-account guard is a refusal by design, not a failure.

    Refusing to spend a shared subscription blind is the correct side to fail
    on, so the probe must stop rather than route the request anyway.
    """
    calls = []

    class Result:
        returncode = 2
        stdout = ""
        stderr = "shared and its quota is unknown"

    def fake_run(fleet, args, timeout=60):
        calls.append(args)
        return Result()

    monkeypatch.setattr(probe_quota, "_run", fake_run)
    result = probe_quota.probe("fleet", "/tmp/db", "shared-acct")

    assert result["status"] == "refused"
    assert len(calls) == 1, "must not launch a run after a refused pin"
