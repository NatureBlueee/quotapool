from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SLOT_SECONDS = 30 * 60
HORIZON_SECONDS = 7 * 24 * 60 * 60
QUANTA_PER_WEEKLY_WINDOW = 100
DEFAULT_FIVE_TO_WEEKLY_CAPACITY = 0.20
DEFAULT_WEEKLY_BURN_PER_FAMILY_HOUR = 0.005
MIGRATION_COST = 200
# Deadline nearness is lexicographic on 30-minute slots: one slot of urgency
# costs DEADLINE_LEXICOGRAPHIC_SCALE, so an hour of difference is 2,000.
DEADLINE_LEXICOGRAPHIC_SCALE = 1000

# Load must break ties between accounts of comparable urgency WITHOUT overriding
# urgency itself. At the previous 50,000 one active session outweighed a
# 25-hour deadline difference (50,000 vs 50 slots x 1000), which — together with
# the min-load pre-filter that used to run before this solve — is why an account
# holding capacity that expired within the hour lost to an idle one whose window
# had just reset. That is requirement (a) failing.
#
# 500 is deliberately below one slot of deadline (1,000): load can decide
# between accounts whose windows expire at the same time, and cannot outvote
# even 30 minutes of genuine urgency.
ACTIVE_FAMILY_LOAD_COST = 500
# A manual weight is a bounded tie-breaker, never a replacement for eligibility,
# operator priority or a 30-minute quota-expiry difference (1,000 cost units).
MAX_ROUTING_WEIGHT_BIAS = 400
INF = 10**9


@dataclass(frozen=True)
class OptimizationAccount:
    account_id: str
    five_remaining: float
    five_reset_at: Optional[float]
    weekly_remaining: float
    weekly_reset_at: Optional[float]
    priority: int = 0
    five_floor: float = 0
    active_families: int = 0
    routing_weight: int = 1


def expiry_pressure(
    account: OptimizationAccount, *, now: float,
    five_to_weekly_capacity: float, weekly_burn_per_hour: float,
) -> Dict[str, Any]:
    """Estimate reachable quota at risk within one rolling 5h window.

    Compare against the ENTIRE fleet's forecast demand: if even all that work
    cannot consume this capacity in time, diverting it for load balance wastes
    quota. Unknown/past deadlines confer no urgency. This is a forecast, not
    permission to exceed observed quotas or reserve floors.
    """
    ratio = max(0.03, min(1.0, five_to_weekly_capacity))
    current_capacity = min(
        _bounded(account.weekly_remaining),
        _bounded(account.five_remaining) * ratio,
    )
    windows = []
    five_end = account.five_reset_at
    weekly_end = account.weekly_reset_at
    if five_end and now < five_end <= now + 5 * 3600:
        windows.append((five_end, "five_hour", current_capacity))
    if weekly_end and now < weekly_end <= now + 5 * 3600:
        capacity = current_capacity
        # Only count a renewal when its observed deadline is still in future
        # and strictly before the weekly reset. Do not invent missing clocks.
        if five_end and now < five_end < weekly_end:
            capacity += (1.0 - _bounded(account.five_floor)) * ratio
        windows.append((weekly_end, "weekly", min(
            _bounded(account.weekly_remaining), capacity,
        )))
    risks = []
    for deadline, bucket, capacity in windows:
        demand = max(0.0, weekly_burn_per_hour) * (deadline - now) / 3600
        at_risk = max(0.0, capacity - demand)
        # The upstream reports quota at 1% resolution; smaller differences
        # cannot justify overriding a normal balancing decision.
        if at_risk >= 1.0 / QUANTA_PER_WEEKLY_WINDOW:
            risks.append({
                "bucket": bucket, "reset_at": deadline,
                "seconds_to_reset": round(deadline - now, 3),
                "spendable_weekly_fraction": round(capacity, 6),
                "forecast_demand_fraction": round(demand, 6),
                "at_risk_weekly_fraction": round(at_risk, 6),
            })
    return min(risks, key=lambda item: item["reset_at"]) if risks else {}


@dataclass(frozen=True)
class OptimizationResult:
    account_id: str
    status: str
    objective_cost: int
    flow_quanta: int
    demand_quanta: int
    first_slot_flow: Mapping[str, int]
    five_to_weekly_capacity: float
    weekly_burn_per_hour: float
    horizon_end: float
    used_default_forecast: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
            "algorithm": "time_expanded_min_cost_flow",
            "status": self.status,
            "account_id": self.account_id,
            "objective_cost": self.objective_cost,
            "flow_quanta": self.flow_quanta,
            "demand_quanta": self.demand_quanta,
            "first_slot_flow": dict(self.first_slot_flow),
            "five_to_weekly_capacity": self.five_to_weekly_capacity,
            "weekly_burn_per_hour": self.weekly_burn_per_hour,
            "horizon_end": self.horizon_end,
            "used_default_forecast": self.used_default_forecast,
            "optimality_scope": (
                "exact integral optimum for the current finite-horizon forecast; "
                "reused only for identical planning snapshots within five seconds"
            ),
        }


@dataclass
class _Edge:
    to: int
    rev: int
    cap: int
    cost: int
    original_cap: int
    tag: Optional[Tuple[str, int]] = None


class _MinCostFlow:
    """Small exact integral min-cost max-flow solver.

    Capacities and costs are integers, so successive shortest augmenting paths
    return an integral optimum for this network without an external solver.
    """

    def __init__(self) -> None:
        self.graph: List[List[_Edge]] = []

    def node(self) -> int:
        self.graph.append([])
        return len(self.graph) - 1

    def edge(
        self,
        source: int,
        target: int,
        capacity: int,
        cost: int,
        *,
        tag: Optional[Tuple[str, int]] = None,
    ) -> None:
        if capacity <= 0:
            return
        forward = _Edge(
            target,
            len(self.graph[target]),
            capacity,
            cost,
            capacity,
            tag,
        )
        reverse = _Edge(source, len(self.graph[source]), 0, -cost, 0, None)
        self.graph[source].append(forward)
        self.graph[target].append(reverse)

    def solve(self, source: int, sink: int, wanted: int) -> Tuple[int, int]:
        flow = 0
        cost = 0
        n = len(self.graph)
        while flow < wanted:
            distance = [INF] * n
            previous_node = [-1] * n
            previous_edge = [-1] * n
            in_queue = [False] * n
            queue = [source]
            distance[source] = 0
            in_queue[source] = True
            head = 0
            while head < len(queue):
                node = queue[head]
                head += 1
                in_queue[node] = False
                for edge_index, edge in enumerate(self.graph[node]):
                    if edge.cap <= 0:
                        continue
                    candidate = distance[node] + edge.cost
                    if candidate >= distance[edge.to]:
                        continue
                    distance[edge.to] = candidate
                    previous_node[edge.to] = node
                    previous_edge[edge.to] = edge_index
                    if not in_queue[edge.to]:
                        queue.append(edge.to)
                        in_queue[edge.to] = True
            if distance[sink] == INF:
                break
            amount = wanted - flow
            node = sink
            while node != source:
                parent = previous_node[node]
                if parent < 0:
                    amount = 0
                    break
                edge = self.graph[parent][previous_edge[node]]
                amount = min(amount, edge.cap)
                node = parent
            if amount <= 0:
                break
            node = sink
            while node != source:
                parent = previous_node[node]
                edge = self.graph[parent][previous_edge[node]]
                edge.cap -= amount
                self.graph[node][edge.rev].cap += amount
                node = parent
            flow += amount
            cost += amount * distance[sink]
        return flow, cost

    def tagged_flow(self, slot_index: int) -> Dict[str, int]:
        result: Dict[str, int] = {}
        for edges in self.graph:
            for edge in edges:
                if edge.tag is None or edge.tag[1] != slot_index:
                    continue
                used = edge.original_cap - edge.cap
                if used > 0:
                    result[edge.tag[0]] = result.get(edge.tag[0], 0) + used
        return result


def _bounded(value: Optional[float]) -> float:
    return max(0.0, min(1.0, float(value or 0)))


FORECAST_SAMPLE_SECONDS = 5 * 60


def _compact_forecast_samples(
    values: Sequence[Tuple[float, Optional[float], Optional[float]]],
) -> List[Tuple[float, Optional[float], Optional[float]]]:
    """Keep the first and last quota reading in each five-minute bucket.

    Route traffic can produce thousands of distinct fractional readings per
    account in six hours.  Forecasting compares pairs at least fifteen minutes
    apart, so every intermediate sub-five-minute point adds quadratic work but
    no useful time resolution.  Keeping both bucket edges preserves resets and
    boundary movement while placing a hard bound on the pairwise estimator.
    """
    compacted: List[Tuple[float, Optional[float], Optional[float]]] = []
    bucket_first: Optional[
        Tuple[float, Optional[float], Optional[float]]
    ] = None
    bucket_last: Optional[
        Tuple[float, Optional[float], Optional[float]]
    ] = None
    bucket_id: Optional[int] = None

    def flush() -> None:
        if bucket_first is None:
            return
        compacted.append(bucket_first)
        if bucket_last is not None and bucket_last != bucket_first:
            compacted.append(bucket_last)

    for value in values:
        current_bucket = int(value[0] // FORECAST_SAMPLE_SECONDS)
        if current_bucket != bucket_id:
            flush()
            bucket_id = current_bucket
            bucket_first = value
        bucket_last = value
    flush()
    return compacted


def estimate_forecast(
    event_rows: Iterable[Mapping[str, Any]],
    *,
    active_families: int,
    now: float,
) -> Tuple[float, float, bool]:
    """Estimate token-capacity conversion and demand from quota observations.

    The quota endpoint reports fractions rather than absolute token limits.
    When the same work advances both meters, delta(weekly)/delta(5h) estimates
    C_5h/C_weekly. For each account, the widest observed meter movement is used
    so intermediate one-percent rounding steps cannot outweigh the longer
    interval. Account burn rates are then summed because they are concurrent
    demand, while conversion ratios are combined with a median.
    """

    series: Dict[str, List[Tuple[float, Optional[float], Optional[float]]]] = {}
    for row in event_rows:
        at = float(row["at"])
        if at < now - 6 * 60 * 60:
            continue
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        five = payload.get("five_hour_used")
        weekly = payload.get("weekly_used")
        if five is None and weekly is None:
            continue
        series.setdefault(str(row["subject"]), []).append((
            at,
            None if five is None else float(five),
            None if weekly is None else float(weekly),
        ))

    account_weekly_slopes: List[float] = []
    ratios: List[float] = []
    for values in series.values():
        unique: List[Tuple[float, Optional[float], Optional[float]]] = []
        for value in sorted(values):
            if unique and value[1:] == unique[-1][1:]:
                continue
            unique.append(value)
        unique = _compact_forecast_samples(unique)
        weekly_candidates: List[Tuple[float, float]] = []
        ratio_candidates: List[Tuple[float, float, float]] = []
        for left_index, left in enumerate(unique):
            for right in unique[left_index + 1:]:
                elapsed = right[0] - left[0]
                if elapsed < 15 * 60:
                    continue
                hours = elapsed / 3600
                if left[2] is not None and right[2] is not None:
                    weekly_delta = right[2] - left[2]
                    if 0 < weekly_delta <= 0.25:
                        weekly_candidates.append((
                            elapsed,
                            weekly_delta / hours,
                        ))
                        if left[1] is not None and right[1] is not None:
                            five_delta = right[1] - left[1]
                            if 0 < five_delta <= 0.50:
                                ratio = weekly_delta / five_delta
                                if 0.03 <= ratio <= 1:
                                    ratio_candidates.append((
                                        five_delta,
                                        elapsed,
                                        ratio,
                                    ))
        if weekly_candidates:
            account_weekly_slopes.append(
                max(weekly_candidates, key=lambda item: item[0])[1]
            )
        if ratio_candidates:
            ratios.append(
                max(ratio_candidates, key=lambda item: (item[0], item[1]))[2]
            )

    used_default = not account_weekly_slopes
    if account_weekly_slopes:
        aggregate_weekly_burn = min(0.25, sum(account_weekly_slopes))
    else:
        aggregate_weekly_burn = (
            DEFAULT_WEEKLY_BURN_PER_FAMILY_HOUR
            * max(active_families, 1)
        )
    ratio = (
        statistics.median(ratios)
        if ratios
        else DEFAULT_FIVE_TO_WEEKLY_CAPACITY
    )
    return ratio, aggregate_weekly_burn, used_default


def solve_rolling_horizon(
    accounts: Sequence[OptimizationAccount],
    *,
    now: float,
    current_account: Optional[str],
    preferred_account: Optional[str],
    weekly_burn_per_hour: float,
    five_to_weekly_capacity: float,
    migration_cost: int = MIGRATION_COST,
    used_default_forecast: bool = False,
) -> Optional[OptimizationResult]:
    """Solve the current time-expanded allocation exactly.

    Model:
      * one integer quantum is 1% of a weekly subscription window;
      * each account's current weekly capacity is a source budget;
      * current and future 5h cycles are nested capacity arcs;
      * forecast demand is placed in 30-minute time slots;
      * min-cost max-flow consumes the earliest-expiring 5h capacity first,
        then the earliest weekly capacity, with explicit operator and migration
        costs.

    The horizon stops at each account's next weekly reset. At that reset the
    real quota is observed and the model is rebuilt, avoiding invented future
    capacity.
    """

    usable = [
        account for account in accounts
        if account.five_remaining > 0 and account.weekly_remaining > 0
    ]
    if not usable:
        return None
    horizon_end = min(
        now + HORIZON_SECONDS,
        max(account.weekly_reset_at or now + HORIZON_SECONDS for account in usable),
    )
    slot_count = max(1, int(math.ceil((horizon_end - now) / SLOT_SECONDS)))
    demand: List[int] = []
    carry = 0.0
    per_slot = max(0.0, weekly_burn_per_hour) * 100 * SLOT_SECONDS / 3600
    for slot_index in range(slot_count):
        carry += per_slot
        units = int(carry)
        carry -= units
        if slot_index == 0:
            units = max(1, units)
        demand.append(units)
    wanted = sum(demand)
    if wanted <= 0:
        demand[0] = 1
        wanted = 1

    flow = _MinCostFlow()
    source = flow.node()
    sink = flow.node()
    slot_nodes = [flow.node() for _ in range(slot_count)]
    for slot_index, units in enumerate(demand):
        # Earlier work is never sacrificed to serve forecast work farther out.
        flow.edge(slot_nodes[slot_index], sink, units, slot_index)

    minimum_priority = min(account.priority for account in usable)
    maximum_weight = max(max(1, min(10, account.routing_weight)) for account in usable)
    for account in sorted(usable, key=lambda item: item.account_id):
        weekly_end = min(
            account.weekly_reset_at or now + HORIZON_SECONDS,
            horizon_end,
        )
        weekly_capacity = int(math.floor(
            _bounded(account.weekly_remaining) * QUANTA_PER_WEEKLY_WINDOW
        ))
        if weekly_capacity <= 0 or weekly_end <= now:
            continue
        weekly_node = flow.node()
        priority_cost = max(0, account.priority - minimum_priority) * 1_000_000
        weight = max(1, min(10, account.routing_weight))
        weight_cost = round(MAX_ROUTING_WEIGHT_BIAS * (1 - weight / maximum_weight))
        weighted_load_cost = round(
            max(0, account.active_families) * ACTIVE_FAMILY_LOAD_COST / weight
        )
        preference_cost = (
            0
            if not preferred_account or account.account_id == preferred_account
            else 500_000
        )
        weekly_deadline_index = int(math.ceil((weekly_end - now) / SLOT_SECONDS))
        flow.edge(
            source,
            weekly_node,
            weekly_capacity,
            priority_cost
            + preference_cost
            + weight_cost + weighted_load_cost,
        )

        cycle_start = now
        cycle_end = min(
            account.five_reset_at or now + 5 * 60 * 60,
            weekly_end,
        )
        first_cycle = True
        while cycle_start < weekly_end:
            if cycle_end <= cycle_start:
                break
            fraction = (
                _bounded(account.five_remaining)
                if first_cycle
                else 1.0 - _bounded(account.five_floor)
            )
            cycle_capacity = int(math.floor(
                fraction
                * max(0.03, min(1.0, five_to_weekly_capacity))
                * QUANTA_PER_WEEKLY_WINDOW
            ))
            cycle_node = flow.node()
            five_deadline_index = int(math.ceil((cycle_end - now) / SLOT_SECONDS))
            cycle_cost = (
                five_deadline_index * DEADLINE_LEXICOGRAPHIC_SCALE
                + weekly_deadline_index
            )
            flow.edge(weekly_node, cycle_node, cycle_capacity, cycle_cost)
            for slot_index in range(slot_count):
                slot_midpoint = now + (slot_index + 0.5) * SLOT_SECONDS
                if not cycle_start <= slot_midpoint < cycle_end:
                    continue
                switch_cost = (
                    migration_cost
                    if slot_index == 0
                    and current_account
                    and account.account_id != current_account
                    else 0
                )
                flow.edge(
                    cycle_node,
                    slot_nodes[slot_index],
                    INF,
                    switch_cost,
                    tag=(account.account_id, slot_index),
                )
            first_cycle = False
            cycle_start = cycle_end
            cycle_end = min(cycle_start + 5 * 60 * 60, weekly_end)

    sent, objective = flow.solve(source, sink, wanted)
    # The plan's FIRST move, which is not always in slot zero.
    #
    # An empty slot zero used to be read as "no plan exists". It is not — it is
    # a plan that says start later, and it appears exactly when the caller most
    # needs an answer: `current_account` puts a migration cost on every
    # alternative, so if the account a session is already on cannot serve this
    # slot, the cheapest flow defers.
    #
    # Measured on live data, identical inputs but for `current_account`:
    #
    #     current=None                  sent=53/wanted=970  -> primary@example.com
    #     current=fourth@example.com    sent=53/wanted=970  -> None
    #
    # The same 53 quanta were routed both times. The solve did not fail; only
    # the reading of it did. Returning None sent every such decision to the
    # scheduler's fallback sort, which has no stickiness at all — and that sort
    # is dominated by a per-session load penalty, so sessions bounced between
    # accounts. Each bounce discards a prompt cache, which is what the owner
    # sees as a long task stalling. Observed: 130 switches across 63 sessions in
    # six hours, every one of them labelled `scheduler_fallback`, ping-ponging
    # me@ -> hi@ 22 times and back 15.
    #
    # So: take the earliest slot the plan actually routes work to. Using that
    # account now is a small deviation from a plan that already wanted it; the
    # alternative was no plan at all.
    first = flow.tagged_flow(0)
    if not first:
        for slot_index in range(1, slot_count):
            first = flow.tagged_flow(slot_index)
            if first:
                break
    if not first:
        return None
    chosen = sorted(first.items(), key=lambda item: (-item[1], item[0]))[0][0]
    return OptimizationResult(
        account_id=chosen,
        status="optimal" if sent == wanted else "capacity_limited_optimal",
        objective_cost=objective,
        flow_quanta=sent,
        demand_quanta=wanted,
        first_slot_flow=first,
        five_to_weekly_capacity=five_to_weekly_capacity,
        weekly_burn_per_hour=weekly_burn_per_hour,
        horizon_end=horizon_end,
        used_default_forecast=used_default_forecast,
    )
