"""The health check must give the verdict `fleet doctor` cannot.

Measured on the Harness server 2026-07-30: `fleet doctor` reported ok:true and
ready_for_canary:true while the fleet had, over fifteen hours, routed 17
requests and parked 678. Every doctor check was correct — they inspect
configuration. None asks whether requests are being served.

These tests pin the judgement, not the plumbing: given a set of measurements,
does it reach the right verdict and name the right action?
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

# `fleet-health` is an extensionless executable, so importlib cannot infer a
# loader for it; name one explicitly. Keeping it extensionless is deliberate —
# it is a command an operator types, not a module anyone imports.
_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fleet-health"
_SPEC = importlib.util.spec_from_loader(
    "fleet_health",
    importlib.machinery.SourceFileLoader("fleet_health", str(_PATH)),
)
fleet_health = importlib.util.module_from_spec(_SPEC)
sys.modules["fleet_health"] = fleet_health
_SPEC.loader.exec_module(fleet_health)

SERVING = fleet_health.SERVING
DEGRADED = fleet_health.DEGRADED
STALLED = fleet_health.STALLED


def judge(**overrides):
    report = {
        "problems": [],
        "notes": [],
        "serving": {"routes_in_window": 100, "parks_in_window": 0},
        "accounts": [],
        "usable_accounts": 2,
        "total_accounts": 2,
        "state_size": {"events": 1000, "executions_active": 5},
    }
    report.update(overrides)
    fleet_health._judge(report)
    return report


def test_a_healthy_fleet_is_serving():
    assert judge()["verdict"] == SERVING


def test_the_harness_server_state_is_stalled():
    """The exact numbers measured on the server, which doctor called healthy."""
    report = judge(
        serving={"routes_in_window": 17, "parks_in_window": 678},
        usable_accounts=1,
        total_accounts=3,
    )
    assert report["verdict"] == STALLED
    assert "678 parks against 17 routes" in report["problems"][0]["problem"]


def test_zero_routes_with_parks_is_stalled():
    report = judge(serving={"routes_in_window": 0, "parks_in_window": 40})
    assert report["verdict"] == STALLED
    assert "not served a single request" in report["problems"][0]["means"]


def test_clients_bypassing_fleet_is_stalled_and_says_quota_will_not_help():
    """`holder is required` means requests never reach account selection at all.

    Naming this precisely matters: the symptom looks like a capacity problem,
    and the fix has nothing to do with capacity.
    """
    report = judge(rejects_per_minute=12.0)
    assert report["verdict"] == STALLED
    problem = report["problems"][0]
    assert "WITHOUT going through" in problem["means"]
    assert "More quota will not help" in problem["action"]


def test_no_usable_account_is_stalled_with_the_soonest_reset():
    report = judge(
        usable_accounts=0,
        total_accounts=3,
        accounts=[
            {"account": "a", "kind": "owned", "resets_in": "19.6h",
             "weekly_remaining_after_floor": 0.0},
            {"account": "b", "kind": "owned", "resets_in": "4.1d",
             "weekly_remaining_after_floor": 0.0},
        ],
    )
    assert report["verdict"] == STALLED
    assert any("19.6h" in p["action"] for p in report["problems"])


def test_elevated_parks_are_degraded_not_stalled():
    """Some parking is normal. Only domination is a fault."""
    report = judge(serving={"routes_in_window": 100, "parks_in_window": 60})
    assert report["verdict"] == DEGRADED


def test_an_account_needing_relogin_is_degraded_with_the_command():
    report = judge(accounts=[
        {"account": "primary", "kind": "owned", "runtime_status": "error",
         "resets_in": "never", "weekly_remaining_after_floor": None},
    ])
    assert report["verdict"] == DEGRADED
    assert "fleet login primary" in report["problems"][0]["action"]


def test_a_crowded_decision_window_is_degraded_and_explains_the_cost():
    """The 6h window is the cost; the table's total size is not.

    This test used to assert on the total, and the check matched it. Both were
    wrong in the same direction: measured on the live Mac, 216,323 events total
    with 1,982 inside the 6h window, and that query returns in 0.0 ms. The alarm
    fired while its own stated harm — "lengthens every routing decision" — was
    not happening, on a machine serving 4,743 routes a day.
    """
    report = judge(state_size={"events": 250_000, "events_6h": 80_000,
                               "executions_active": 10})
    assert report["verdict"] == DEGRADED
    problem = next(p for p in report["problems"] if "events" in p["problem"])
    # The operator needs to know this is a latency problem, not just disk.
    assert "INSIDE the write" in problem["means"]
    assert "maintenance sweep" in problem["action"]


def test_a_big_table_with_a_small_window_is_only_a_note():
    """A busy fleet is not a sick one.

    Retention keeps six days; a machine doing thousands of routes a day fills
    that legitimately. Calling it DEGRADED trains people to ignore the check.
    """
    report = judge(state_size={"events": 2_000_000, "events_6h": 1_500,
                               "executions_active": 10})
    assert report["verdict"] != DEGRADED or not any(
        "events" in p["problem"] for p in report["problems"]
    )
    assert any("retention" in note for note in report["notes"])


def test_a_shared_account_past_its_floor_is_only_a_note():
    """It may be the other holder's usage, not a breach — so it must not be
    reported as a fault without evidence."""
    report = judge(accounts=[
        {"account": "shared", "kind": "shared", "resets_in": "19h",
         "weekly_remaining_after_floor": -0.4},
    ])
    assert report["verdict"] == SERVING
    assert any("past its reserve floor" in n for n in report["notes"])


def test_the_worst_problem_leads_the_headline():
    report = judge(
        serving={"routes_in_window": 100, "parks_in_window": 60},
        rejects_per_minute=12.0,
    )
    assert report["verdict"] == STALLED
    assert "rejected" in fleet_health.render(report, quiet=True)
