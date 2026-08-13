"""Requirement (c): manual control, and the order the modes resolve in.

Phase 2 changes the candidate pre-filter and Phase 3 adds parent-id cascade;
both run downstream of this precedence, so it needs to be pinned first.
"""

from __future__ import annotations

import pytest

from claude_fleet.control import NoCapacity
from conftest import quota


def _fleet(control, *account_ids, weekly=0.1):
    for account_id in account_ids:
        control.upsert_account(account_id)
        quota(control, account_id, weekly=weekly, five_hour=0.0)
    control.register_execution("s1", workload_class="interactive")
    return control.acquire_writer("s1", "holder-1")


def test_next_outranks_pin(control):
    """`next` is a one-shot override checked before `hard_pin`."""
    generation = _fleet(control, "a", "b", "c")
    control.set_route_policy("s1", "pin", "a")
    control.set_route_policy("s1", "next", "b")
    assert control.decide_route("s1", "holder-1", generation)["account_id"] == "b"


def test_next_is_one_shot_and_then_pin_resumes(control):
    generation = _fleet(control, "a", "b", "c")
    control.set_route_policy("s1", "pin", "a")
    control.set_route_policy("s1", "next", "b")
    assert control.decide_route("s1", "holder-1", generation)["account_id"] == "b"
    # `next` cleared itself; the pin is authoritative again.
    assert control.decide_route("s1", "holder-1", generation)["account_id"] == "a"


def test_pin_outranks_chain(control):
    generation = _fleet(control, "a", "b", "c")
    control.set_route_policy("s1", "chain", ["c", "b"])
    control.set_route_policy("s1", "pin", "a")
    assert control.decide_route("s1", "holder-1", generation)["account_id"] == "a"


def test_chain_picks_first_eligible_member(control):
    generation = _fleet(control, "a", "b", "c")
    control.set_account_state("b", "disabled")
    control.set_route_policy("s1", "chain", ["b", "c"])
    assert control.decide_route("s1", "holder-1", generation)["account_id"] == "c"


def test_pin_to_disabled_account_parks_rather_than_silently_rerouting(control):
    """A pin must fail loudly. Silently honouring it elsewhere would make the
    manual control a suggestion rather than a constraint."""
    generation = _fleet(control, "a", "b")
    control.set_account_state("a", "disabled")
    control.set_route_policy("s1", "pin", "a")
    with pytest.raises(NoCapacity):
        control.decide_route("s1", "holder-1", generation)


def test_clearing_a_mode_restores_automatic_selection(control):
    generation = _fleet(control, "a", "b")
    control.set_route_policy("s1", "pin", "a")
    assert control.decide_route("s1", "holder-1", generation)["account_id"] == "a"
    control.set_route_policy("s1", "pin", None)
    # Any eligible account is acceptable; the point is that it no longer raises
    # and is no longer forced.
    assert control.decide_route("s1", "holder-1", generation)["account_id"] in {"a", "b"}


def test_draining_stops_new_sessions_but_not_existing_ones(control):
    """`drain` is the ordered half of requirement (c): stop new work without
    interrupting a long-running window."""
    generation = _fleet(control, "a", "b")
    control.set_route_policy("s1", "pin", "a")
    assert control.decide_route("s1", "holder-1", generation)["account_id"] == "a"
    control.set_route_policy("s1", "pin", None)

    control.set_account_state("a", "draining")
    # The existing lease-holder stays on the draining account.
    assert control.decide_route("s1", "holder-1", generation)["account_id"] == "a"

    # A different session cannot start there.
    control.register_execution("s2", workload_class="interactive")
    gen2 = control.acquire_writer("s2", "holder-2")
    assert control.decide_route("s2", "holder-2", gen2)["account_id"] == "b"


def test_disabled_evicts_even_an_existing_session(control):
    """`disable` is today's de-facto evacuate — Phase 3 adds an ordered one."""
    generation = _fleet(control, "a", "b")
    control.set_route_policy("s1", "pin", "a")
    control.decide_route("s1", "holder-1", generation)
    control.set_route_policy("s1", "pin", None)

    control.set_account_state("a", "disabled")
    assert control.decide_route("s1", "holder-1", generation)["account_id"] == "b"


def test_workload_allow_list_is_a_hard_constraint(control):
    """This is how a daemon gets confined to a pool of accounts."""
    generation = _fleet(control, "a", "b")
    control.set_workload_policy("daemon:indexer", allowed_accounts=["b"])

    control.register_execution(
        "s2", workload_class="daemon", workload_id="daemon:indexer"
    )
    gen2 = control.acquire_writer("s2", "holder-2")
    assert control.decide_route("s2", "holder-2", gen2)["account_id"] == "b"


def test_exclude_is_honoured(control):
    """The data plane's per-request exclusion — the mechanism a network error
    currently abuses (Phase 1.1)."""
    generation = _fleet(control, "a", "b")
    route = control.decide_route("s1", "holder-1", generation, exclude=["a"])
    assert route["account_id"] == "b"


def test_all_accounts_excluded_parks(control):
    generation = _fleet(control, "a", "b")
    with pytest.raises(NoCapacity):
        control.decide_route("s1", "holder-1", generation, exclude=["a", "b"])
