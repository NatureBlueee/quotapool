"""The coordinator must never choose an account the data plane cannot use.

The accounts table can hold ids the executor has never heard of — legacy names
kept for their foreign keys, rows from another host's era, anything imported by
hand. Choosing one produces `Fleet coordinator selected unknown account` and a
502 on every request.

This is not hypothetical, and it is not old news either: it happened on the
server while this suite was being written. The legacy `max-a/b/c` rows sat
frozen at weekly_used=1.0 and were harmless only because "spent" made them
unselectable. `expire_stale_windows` reset their long-passed windows to zero,
they instantly became the most attractive accounts in the fleet, and every
request 502'd until they were disabled by hand.

ACCOUNTS.md had named that exact scenario months earlier as the danger of
keeping those rows: "要是当初停在一个低值，它们就会变成永远显示空闲的幽灵候选，
每次都被优先选中、每次都路由失败." Writing the danger down did not stop it. A
test does.
"""

from __future__ import annotations

import pytest

from claude_fleet.control import NoCapacity

from conftest import quota

HOUR = 3600


def _session(control, name="s1"):
    control.register_execution(name, workload_class="interactive")
    return control.acquire_writer(name, f"holder-{name}")


def test_an_account_the_data_plane_does_not_report_is_never_chosen(control, now):
    """The ghost row is the most attractive account in the fleet and must lose."""
    control.upsert_account("ghost")
    control.upsert_account("real")
    # The ghost looks perfect: untouched window, furthest deadline.
    quota(control, "ghost", weekly=0.0, five_hour=0.0,
          weekly_reset=now + 7 * 24 * HOUR, five_reset=now + 5 * HOUR)
    quota(control, "real", weekly=0.6, five_hour=0.3,
          weekly_reset=now + 2 * 24 * HOUR, five_reset=now + 2 * HOUR)

    generation = _session(control)
    route = control.decide_route("s1", "holder-s1", generation,
                                 executable={"real"})
    assert route["account_id"] == "real", (
        "an account the executor does not know was selected; every request "
        "against it returns 502"
    )


def test_a_hard_pin_to_an_unknown_account_parks(control, now):
    """hard_pin names an account directly and bypasses the candidate ranking.

    The check therefore belongs in the eligibility closure, not in the candidate
    loop — pin, route_chain and `next` all name accounts the same way.

    It PARKS rather than quietly rerouting. That is the existing rule for any
    unusable pin and it is the right one: a pin is an instruction, and silently
    doing something else with it hides an operator mistake instead of reporting
    it. My first version of this test asserted a reroute and was simply wrong
    about the semantics.
    """
    control.upsert_account("ghost")
    control.upsert_account("real")
    quota(control, "ghost", weekly=0.0, five_hour=0.0,
          weekly_reset=now + 7 * 24 * HOUR, five_reset=now + 5 * HOUR)
    quota(control, "real", weekly=0.1, five_hour=0.0,
          weekly_reset=now + 2 * 24 * HOUR, five_reset=now + 2 * HOUR)
    control.set_workload_policy("w", hard_pin="ghost")

    control.register_execution("s1", workload_class="interactive", workload_id="w")
    generation = control.acquire_writer("s1", "holder-s1")
    with pytest.raises(NoCapacity) as caught:
        control.decide_route("s1", "holder-s1", generation, executable={"real"})
    assert "not_executable" in str(caught.value)


def test_an_established_route_to_a_vanished_account_is_abandoned(control, now):
    """The sticky fast path must honour it too.

    A session already parked on an account that later disappears from the data
    plane would otherwise keep being handed the same dead id on every request,
    which is precisely the 502 loop.
    """
    control.upsert_account("ghost")
    control.upsert_account("real")
    for account in ("ghost", "real"):
        quota(control, account, weekly=0.2, five_hour=0.0,
              weekly_reset=now + 3 * 24 * HOUR, five_reset=now + 3 * HOUR)

    generation = _session(control)
    control.set_route_policy("s1", "pin", "ghost")
    first = control.decide_route("s1", "holder-s1", generation)
    assert first["account_id"] == "ghost"
    control.set_route_policy("s1", "pin", None)

    second = control.decide_route("s1", "holder-s1", generation,
                                  executable={"real"})
    assert second["account_id"] == "real", (
        "an established route kept pointing at an account the data plane no "
        "longer reports"
    )


def test_saying_nothing_filters_nothing(control, now):
    """Probes and tests call `decide_route` without a list.

    An absent set means "the caller did not say", not "no account is usable" —
    getting that backwards would park everything.
    """
    control.upsert_account("only")
    quota(control, "only", weekly=0.2, five_hour=0.0,
          weekly_reset=now + 3 * 24 * HOUR, five_reset=now + 3 * HOUR)

    generation = _session(control)
    assert control.decide_route("s1", "holder-s1", generation)["account_id"] == "only"
    assert control.decide_route("s1", "holder-s1", generation,
                                executable=set())["account_id"] == "only"
