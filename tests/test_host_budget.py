"""Two hosts, one subscription, and neither can see the other's decisions.

Each host learns an account's usage from the rate-limit headers on its own
responses, plus whatever `fleet-quota-sync` has gossiped across since. So both
can be looking at 0.55 while the truth is already higher, both route, and
together push past the line the reserve floor exists to protect.

That floor is the owner's share for his desktop client and CoWork. Breaching it
is the single thing it was for.

A per-host ceiling on TOTAL observed usage bounds the damage: with the Mac at
0.6 and the server at 1.0, the Mac cannot drive the total past 60% of the usable
band however stale its view is, and the server bounds the whole thing at the
line. Two hosts racing each other becomes one host's in-flight burst.

Said precisely, because it is easy to oversell: this BOUNDS the overshoot, it
does not eliminate it. Eliminating it needs per-host consumption accounting, and
the only signal available — `weekly_used` — is an aggregate that already mixes
in the other host and the desktop client, so it cannot be split after the fact.
"""

from __future__ import annotations

import pytest

from claude_fleet import control as module
from claude_fleet.control import FleetControl


@pytest.fixture(autouse=True)
def _clear_budget_cache():
    module._HOST_BUDGET_CACHE.clear()
    yield
    module._HOST_BUDGET_CACHE.clear()


def _budget(monkeypatch, value):
    module._HOST_BUDGET_CACHE.clear()
    monkeypatch.setattr(module, "_host_budget", lambda: value)


def _row(control, account_id="acct", *, used=0.0, floor=0.0):
    control.upsert_account(account_id, weekly_floor=floor)
    control.report_quota(account_id, weekly_used=used)
    with control.db.transaction() as conn:
        return conn.execute("SELECT * FROM accounts WHERE id=?",
                            (account_id,)).fetchone()


def test_the_default_changes_nothing(control, monkeypatch):
    """A fleet that never configures this must behave exactly as it does today.
    Measured on this machine: host_budget resolves to 1.0."""
    _budget(monkeypatch, 1.0)
    row = _row(control, used=0.30, floor=0.40)
    assert FleetControl._remaining(row, "weekly_used", "weekly_floor") == \
        pytest.approx(0.30)


def test_a_partitioned_host_stops_at_its_share_of_the_usable_band(control,
                                                                   monkeypatch):
    """floor 0.40 leaves a usable band of 0.60. A host at 0.6 may drive total
    usage to 0.36 — not to 0.60, and not to 0.6 of what is left."""
    _budget(monkeypatch, 0.6)
    row = _row(control, used=0.30, floor=0.40)
    assert FleetControl._remaining(row, "weekly_used", "weekly_floor") == \
        pytest.approx(0.06)


def test_it_never_grants_more_than_the_reserve_allows(control, monkeypatch):
    """The budget is a second ceiling, never a licence to exceed the first. A
    host configured to 1.0 must still stop at the floor."""
    _budget(monkeypatch, 1.0)
    row = _row(control, used=0.65, floor=0.40)
    assert FleetControl._remaining(row, "weekly_used", "weekly_floor") < 0


def test_a_partitioned_host_is_always_stricter_than_the_reserve_alone(control,
                                                                       monkeypatch):
    """The property the code relies on instead of a redundant `min`.

    It was written as `min(floor_remaining, budget_remaining)`, and mutation
    testing showed no test could reach that `min` — because with budget < 1 the
    budget figure is provably the smaller of the two:

        budget·(1-floor) - used  −  (1 - floor - used)  =  (budget-1)·(1-floor) ≤ 0

    Unreachable defensive code is worse than none: it reads as protection and
    protects nothing. So the `min` is gone and the premise is asserted here
    across the range instead.
    """
    for budget in (0.1, 0.5, 0.9, 0.99):
        for floor in (0.0, 0.25, 0.4):
            for used in (0.0, 0.1, 0.3):
                _budget(monkeypatch, budget)
                row = _row(control, "a-%s-%s-%s" % (budget, floor, used),
                           used=used, floor=floor)
                partitioned = FleetControl._remaining(
                    row, "weekly_used", "weekly_floor")
                unpartitioned = 1 - used - floor
                assert partitioned <= unpartitioned + 1e-9, (
                    "budget=%s floor=%s used=%s" % (budget, floor, used))


def test_a_stale_host_cannot_walk_past_its_ceiling(control, monkeypatch):
    """The whole point. Even believing usage is far lower than it is, the host
    with the smaller ceiling runs out of room first."""
    _budget(monkeypatch, 0.6)
    row = _row(control, used=0.36, floor=0.40)
    assert FleetControl._remaining(row, "weekly_used", "weekly_floor") <= 0


def test_the_unpartitioned_host_still_reaches_the_line(control, monkeypatch):
    """Cumulative, not disjoint. If every host took its own slice — 0.6 and 0.4
    — the total would never reach the line and the rest of the window would be
    thrown away. One host has to carry 1.0."""
    _budget(monkeypatch, 1.0)
    row = _row(control, used=0.50, floor=0.40)
    assert FleetControl._remaining(row, "weekly_used", "weekly_floor") == \
        pytest.approx(0.10)


def test_an_unmeasured_account_stays_unmeasured(control, monkeypatch):
    """None means "no reading", and the optimizer drops such an account rather
    than guessing. A budget must not turn that into a number."""
    _budget(monkeypatch, 0.6)
    control.upsert_account("acct")
    with control.db.transaction() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id='acct'").fetchone()
    assert FleetControl._remaining(row, "weekly_used", "weekly_floor") is None


def test_the_five_hour_window_is_partitioned_too(control, monkeypatch):
    """A burst inside one five-hour window is exactly where two hosts collide;
    partitioning only the weekly window would leave the sharp edge in place."""
    _budget(monkeypatch, 0.5)
    control.upsert_account("acct", five_hour_floor=0.0)
    control.report_quota("acct", five_hour_used=0.40)
    with control.db.transaction() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id='acct'").fetchone()
    assert FleetControl._remaining(row, "five_hour_used", "five_hour_floor") == \
        pytest.approx(0.10)


# ---------------------------------------------------------------------------
# reading the setting
# ---------------------------------------------------------------------------

def test_a_nonsense_value_falls_back_to_no_partition(monkeypatch, tmp_path):
    """A typo must not become a fleet-wide throttle, and must not become an
    unbounded ceiling either. Either way it keeps serving."""
    import json

    for bad in (0.0, -1.0, 1.5, "abc"):
        runtime = tmp_path / "runtime.json"
        runtime.write_text(json.dumps({"host_budget": bad}), encoding="utf-8")
        monkeypatch.setenv("FLEET_RUNTIME_CONFIG", str(runtime))
        module._HOST_BUDGET_CACHE.clear()
        assert module._host_budget() == 1.0, bad


def test_a_missing_config_is_not_an_error(monkeypatch):
    """This is read from inside the per-request eligibility path. It cannot
    raise, and it cannot be the reason a fleet stops routing."""
    monkeypatch.setenv("FLEET_RUNTIME_CONFIG", "/nonexistent/runtime.json")
    module._HOST_BUDGET_CACHE.clear()
    assert module._host_budget() == 1.0


def test_a_configured_value_is_honoured(monkeypatch, tmp_path):
    import json

    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"host_budget": 0.6}), encoding="utf-8")
    monkeypatch.setenv("FLEET_RUNTIME_CONFIG", str(runtime))
    module._HOST_BUDGET_CACHE.clear()
    assert module._host_budget() == pytest.approx(0.6)
