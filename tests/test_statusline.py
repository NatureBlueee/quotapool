"""Waiting must look like waiting, not like the session died.

The owner's rule: running out of quota and waiting for a window to refresh is
FINE — it recovers by itself and needs nobody. Returning an error is the
violation. But a session that goes quiet with no explanation is
indistinguishable from one that has died, and that is what sent him hunting for
faults that were not there, twice in one day.

So: one line, only when there is something to say, and never at the cost of the
prompt rendering.
"""

from __future__ import annotations

import importlib.util
import pathlib as module_path
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "fleet-statusline"
    spec = importlib.util.spec_from_loader("statusline", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__dict__["__file__"] = str(path)
    source = path.read_text(encoding="utf-8")
    marker = 'if __name__ == "__main__":'
    if marker in source:
        source = source[: source.index(marker)]
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


@pytest.fixture(autouse=True)
def _never_read_the_real_machine(monkeypatch, tmp_path):
    """No test in this file may see this machine's own state.

    `_fleet_line` consults the watchdog's alarm file under `Path.home()`, so
    `test_a_machine_without_fleet_says_nothing` was quietly asserting something
    about the developer's laptop. It passed for as long as that laptop happened
    to have no renderable alarm — and failed the moment a real one was raised,
    with a message about markgood that had nothing to do with what it tests.

    A test whose result depends on the machine it runs on is not a test.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(module_path.Path, "home", staticmethod(lambda: home))


def _capacity(module, monkeypatch, report):
    """Stand in for report_capacity without touching a real database."""
    import types

    fake_capacity = types.ModuleType("claude_fleet.capacity")
    fake_capacity.report_capacity = lambda db, workload: report
    fake_db = types.ModuleType("claude_fleet.db")
    fake_db.FleetDB = lambda path: object()

    import sys
    monkeypatch.setitem(sys.modules, "claude_fleet.capacity", fake_capacity)
    monkeypatch.setitem(sys.modules, "claude_fleet.db", fake_db)

    import json
    monkeypatch.setattr(module.Path, "read_text",
                        lambda self, **kw: json.dumps({"db_path": "/tmp/x"}),
                        raising=False)


def test_silence_when_capacity_exists(monkeypatch):
    module = _module()
    _capacity(module, monkeypatch, {
        "usable_accounts": [
            {"account": "a@", "weekly_remaining_after_floor": 0.4,
             "resets_in_hours": 100.0},
            {"account": "b@", "weekly_remaining_after_floor": 0.6,
             "resets_in_hours": 100.0},
        ],
        "blocked_accounts": [],
    })
    assert module._fleet_line() == ""


def test_the_last_account_standing_is_worth_saying(monkeypatch):
    """Capacity loss was invisible until it hit zero.

    Four of six accounts were gone for four days while this line said nothing —
    one account still had room, and by the old rule one account with room was
    silence. The owner found out by noticing the fleet felt slow. Losing
    redundancy is the warning; losing the last account is the outage.
    """
    module = _module()
    _capacity(module, monkeypatch, {
        "usable_accounts": [
            {"account": "survivor@", "weekly_remaining_after_floor": 0.5,
             "resets_in_hours": 40.0},
        ],
        "blocked_accounts": [
            {"account": "gone1@", "weekly_remaining_after_floor": 0.0,
             "resets_in_hours": 10.0},
            {"account": "gone2@", "weekly_remaining_after_floor": 0.0,
             "resets_in_hours": 20.0},
        ],
    })
    line = module._fleet_line()
    assert "1/3" in line, "the fraction is the whole message"


def test_a_single_account_fleet_is_not_nagged(monkeypatch):
    """One account is a complete fleet for plenty of people, and a warning that
    can never be resolved is a warning nobody reads."""
    module = _module()
    _capacity(module, monkeypatch, {
        "usable_accounts": [
            {"account": "only@", "weekly_remaining_after_floor": 0.9,
             "resets_in_hours": 100.0},
        ],
        "blocked_accounts": [],
    })
    assert module._fleet_line() == ""


def test_a_fleet_with_nothing_left_says_when_it_returns(monkeypatch):
    """The fact and the time. Not whether to worry about it."""
    module = _module()
    _capacity(module, monkeypatch, {
        "usable_accounts": [],
        "blocked_accounts": [
            {"account": "hey@", "weekly_remaining_after_floor": 0.0,
             "resets_in_hours": 42.0},
            {"account": "hello@", "weekly_remaining_after_floor": 0.0,
             "resets_in_hours": 2.25},
        ],
    })
    line = module._fleet_line()
    assert "额度已用尽" in line
    assert "2h15m" in line, (
        "it has to name the soonest refresh; without it the owner cannot tell "
        "a two-minute wait from a two-day one, and that is the whole question"
    )


def test_a_sliver_of_quota_is_not_capacity(monkeypatch):
    """An account with 0.4% left serves one request and then the wait resumes.
    Calling that "capacity" is how the panel stayed green through a park storm.
    """
    module = _module()
    _capacity(module, monkeypatch, {
        "usable_accounts": [
            {"account": "hi@", "weekly_remaining_after_floor": 0.004,
             "resets_in_hours": 18.0},
        ],
        "blocked_accounts": [],
    })
    assert "额度已用尽" in module._fleet_line()


def test_no_reset_time_still_reports_the_state(monkeypatch):
    """Unknown recovery is worse news, not a reason to go quiet."""
    module = _module()
    _capacity(module, monkeypatch, {
        "usable_accounts": [],
        "blocked_accounts": [{"account": "x", "resets_in_hours": None}],
    })
    assert "额度已用尽" in module._fleet_line()


def test_a_machine_without_fleet_says_nothing(monkeypatch):
    """This wrapper has to work where Fleet has never run."""
    module = _module()
    import sys, types
    broken = types.ModuleType("claude_fleet.capacity")

    def explode(*a, **k):
        raise RuntimeError("no database")

    broken.report_capacity = explode
    monkeypatch.setitem(sys.modules, "claude_fleet.capacity", broken)
    assert module._fleet_line() == ""


def test_durations_are_lowercase_and_never_round_to_zero():
    """Both asked for explicitly: `3h18m`, `5d21h`, and never `0m`."""
    module = _module()
    assert module._hms(3600 * 3 + 60 * 18) == "3h18m"
    assert module._hms(3600 * 141) == "5d21h"
    assert module._hms(20) == "1m", "a wait that rounds to zero reads as none"
    assert module._hms(0) == "1m"


# ---------------------------------------------------------------------------
# the watchdog's alarms, which have to actually arrive
# ---------------------------------------------------------------------------

def _alarm_file(module, monkeypatch, tmp_path, payload):
    import json
    path = tmp_path / "watchdog-alarm.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    class _Home:
        def __truediv__(self, _):
            return self

        def read_text(self, **kw):
            return path.read_text(**kw)

    monkeypatch.setattr(module.Path, "home", staticmethod(lambda: _Home()))


def test_a_stale_rollback_point_reaches_the_prompt(monkeypatch, tmp_path):
    """The watchdog raised this alarm into a file whose reader had a whitelist
    of four kinds and returned "" for anything else. So the alarm existed, was
    correct, was written every five minutes — and was invisible.

    That is the same failure it was built to fix, one layer up: a mechanism that
    runs on schedule and reports only where nobody looks.
    """
    module = _module()
    _alarm_file(module, monkeypatch, tmp_path,
                {"conditions": {"known_good": {
                    "kind": "known_good_stale", "hours": 127, "at": 0}}})
    line = module._watchdog_line()
    assert "127" in line and "markgood" in line


def test_having_nowhere_to_roll_back_to_reaches_it_too(monkeypatch, tmp_path):
    module = _module()
    _alarm_file(module, monkeypatch, tmp_path,
                {"conditions": {"known_good": {
                    "kind": "known_good_missing", "at": 0}}})
    assert "无处可退" in module._watchdog_line()


def test_an_unwatched_peer_reaches_it(monkeypatch, tmp_path):
    module = _module()
    _alarm_file(module, monkeypatch, tmp_path,
                {"conditions": {"peer": {"kind": "peer_unreachable", "at": 0}}})
    assert "够不着对端" in module._watchdog_line()


def test_a_condition_that_has_been_fixed_stops_showing(monkeypatch, tmp_path):
    """The reason conditions are not events. An indicator that stays red after
    the problem is gone gets ignored, and then it is worth less than nothing —
    it is a channel the real alarm also arrives on."""
    module = _module()
    _alarm_file(module, monkeypatch, tmp_path, {"conditions": {}, "events": []})
    assert module._watchdog_line() == ""


def test_a_rollback_that_happened_still_shows(monkeypatch, tmp_path):
    """Events are history and stay. The owner is running a version he did not
    choose; that does not stop being true because time passed."""
    import time
    module = _module()
    _alarm_file(module, monkeypatch, tmp_path,
                {"latest": {"kind": "rolled_back", "at": time.time() - 7200,
                            "why": "协调器没有应答"}})
    line = module._watchdog_line()
    assert "自动回滚" in line and "2h" in line


def test_a_live_condition_outranks_an_old_rollback(monkeypatch, tmp_path):
    """One line, so the choice has to be made. "There is nowhere to roll back
    to" is about this minute; a rollback that already finished is not."""
    import time
    module = _module()
    _alarm_file(module, monkeypatch, tmp_path, {
        "latest": {"kind": "rolled_back", "at": time.time() - 7200, "why": "x"},
        "conditions": {"known_good": {"kind": "known_good_missing", "at": 0}},
    })
    assert "无处可退" in module._watchdog_line()


def test_no_alarm_file_at_all_is_silence_not_a_crash(monkeypatch, tmp_path):
    """It runs on every prompt. An exception here costs the owner his prompt."""
    module = _module()

    class _Missing:
        def __truediv__(self, _):
            return self

        def read_text(self, **kw):
            raise FileNotFoundError

    monkeypatch.setattr(module.Path, "home", staticmethod(lambda: _Missing()))
    assert module._watchdog_line() == ""
