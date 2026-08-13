"""Explain a routing decision.

Every route decision already writes a `route.changed` event carrying the full
optimizer result, and every park writes an `execution.parked` event carrying the
per-account exclusion set. Roughly ten thousand of each were sitting in the
database with nothing that could read them, while the operator-facing view was
`fleet status` — a raw dump of seven tables in which quota is a bare float and
resets are epoch seconds.

That gap is why a fleet that was correctly waiting and a fleet that was broken
looked identical for three investigation passes. This turns the record that was
already being kept into an answer.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from .db import FleetDB, json_load


def _age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 0:
        return "in the past"
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _account_view(
    row: Dict[str, Any],
    now: float,
    routed_ever: int = 0,
    routed_recently: int = 0,
) -> Dict[str, Any]:
    metadata = json_load(row.get("metadata"), {}) or {}

    def remaining(used_key: str, floor_key: str) -> Optional[float]:
        used = row.get(used_key)
        if used is None:
            return None
        return round(1 - float(used) - float(row.get(floor_key) or 0), 4)

    view: Dict[str, Any] = {
        "account": metadata.get("runtime_name") or row["id"],
        "id": row["id"],
        "kind": row["kind"],
        "state": row["state"],
        # The number that actually decides eligibility: what is left after the
        # reserve floor, not raw utilization.
        "weekly_remaining_after_floor": remaining("weekly_used", "weekly_floor"),
        "weekly_resets_in": _age(
            float(row["weekly_reset_at"]) - now if row["weekly_reset_at"] else None
        ),
        "five_hour_remaining_after_floor": remaining(
            "five_hour_used", "five_hour_floor"
        ),
        "five_hour_resets_in": _age(
            float(row["five_hour_reset_at"]) - now
            if row["five_hour_reset_at"] else None
        ),
        "quota_observed": _age(
            now - float(row["quota_updated_at"]) if row["quota_updated_at"] else None
        ) + " ago" if row["quota_updated_at"] else "never",
        "runtime_status": metadata.get("runtime_status"),
        "allowed_workloads": json_load(row.get("allowed_workloads"), None),
        # Quota this fleet spent vs quota spent elsewhere. Without this, a
        # shared account sitting past its reserve floor is ambiguous between
        # "Fleet breached the reserve" and "the person I share it with used it"
        # — opposite conclusions from the same number. Measured on the live
        # database: the shared account was 40% past its floor with zero route
        # leases ever, i.e. the floor held and the consumption was not ours.
        "routed_by_fleet_ever": routed_ever,
        "routed_by_fleet_24h": routed_recently,
    }
    if row["kind"] == "shared":
        weekly = view["weekly_remaining_after_floor"]
        if weekly is not None and weekly <= 0:
            view["reserve_note"] = (
                "past its reserve floor and therefore ineligible"
                + (
                    " — but Fleet has never routed here, so the consumption is "
                    "the other holder's, not a breach"
                    if routed_ever == 0
                    else f" — Fleet routed here {routed_ever} time(s); check "
                         "whether the floor was set after that usage"
                )
            )
    return view


def explain(db: FleetDB, session_id: str, *, limit: int = 5) -> Dict[str, Any]:
    now = time.time()
    conn = db.connect()
    try:
        execution = conn.execute(
            "SELECT * FROM executions WHERE session_id=?", (session_id,)
        ).fetchone()
        if execution is None:
            alias = conn.execute(
                "SELECT canonical_session_id FROM session_aliases "
                "WHERE alias_session_id=?",
                (session_id,),
            ).fetchone()
            if alias is not None:
                execution = conn.execute(
                    "SELECT * FROM executions WHERE session_id=?",
                    (alias["canonical_session_id"],),
                ).fetchone()
            if execution is None:
                return {
                    "session_id": session_id,
                    "found": False,
                    "hint": "no execution for this session; list them with "
                            "`fleet status`",
                }

        execution_id = execution["id"]
        lease = conn.execute(
            "SELECT * FROM route_leases WHERE session_id=?",
            (execution["session_id"],),
        ).fetchone()

        routed_ever = {
            row["account_id"]: row["n"] for row in conn.execute(
                "SELECT account_id, count(*) AS n FROM route_leases "
                "GROUP BY account_id"
            ).fetchall()
        }
        routed_recently = {
            row["account_id"]: row["n"] for row in conn.execute(
                "SELECT account_id, count(*) AS n FROM route_leases "
                "WHERE created_at > ? GROUP BY account_id",
                (now - 86400,),
            ).fetchall()
        }
        accounts = [
            _account_view(
                dict(row), now,
                routed_ever.get(row["id"], 0),
                routed_recently.get(row["id"], 0),
            )
            for row in conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        ]
        names = {item["id"]: item["account"] for item in accounts}

        # The two decision events are keyed differently: `route.changed` is
        # written with the session id as its subject and `execution.parked` with
        # the execution id. Querying either alone silently returns half the
        # history — and the half you get depends on which way the decision went,
        # which is the worst possible failure for a diagnostic.
        decisions: List[Dict[str, Any]] = []
        for row in conn.execute(
            "SELECT at,kind,payload FROM events "
            "WHERE kind IN ('route.changed','execution.parked') "
            "AND subject IN (?,?) "
            "ORDER BY seq DESC LIMIT ?",
            (execution_id, execution["session_id"], limit),
        ).fetchall():
            payload = json_load(row["payload"], {}) or {}
            entry: Dict[str, Any] = {
                "when": _age(now - float(row["at"])) + " ago",
                "event": row["kind"],
            }
            if row["kind"] == "route.changed":
                entry["chose"] = names.get(payload.get("to"), payload.get("to"))
                entry["moved_from"] = names.get(payload.get("from"))
                entry["reason"] = payload.get("reason")
                optimizer = payload.get("optimizer") or {}
                if optimizer:
                    # The solver's own verdict on whether the fleet could meet
                    # the forecast. `capacity_limited_optimal` means it did the
                    # best it could with less quota than the forecast wanted.
                    entry["solver_status"] = optimizer.get("status")
                    entry["forecast_burn_per_hour"] = optimizer.get(
                        "weekly_burn_per_hour"
                    )
                    entry["forecast_was_a_default_guess"] = optimizer.get(
                        "used_default_forecast"
                    )
            else:
                excluded = payload.get("excluded") or []
                entry["reason"] = payload.get("reason")
                entry["excluded"] = [names.get(item, item) for item in excluded]
            decisions.append(entry)

        result: Dict[str, Any] = {
            "session_id": execution["session_id"],
            "found": True,
            "execution_id": execution_id,
            "status": execution["status"],
            "workload_class": execution["workload_class"],
            "workload_id": execution["workload_id"],
            "writer_holder": execution["writer_holder"],
            "writer_generation": execution["writer_generation"],
            "manual_controls": {
                "pin": names.get(execution["hard_pin"], execution["hard_pin"]),
                "prefer": names.get(
                    execution["preference"], execution["preference"]
                ),
                "next": names.get(
                    execution["next_account"], execution["next_account"]
                ),
                "chain": [
                    names.get(item, item)
                    for item in (json_load(execution["route_chain"], []) or [])
                ],
            },
            "current_account": (
                names.get(lease["account_id"], lease["account_id"])
                if lease is not None else None
            ),
            "recent_decisions": decisions,
            "accounts": accounts,
        }

        policy = None
        if execution["workload_id"]:
            policy_row = conn.execute(
                "SELECT * FROM workload_policies WHERE workload_id=?",
                (execution["workload_id"],),
            ).fetchone()
            if policy_row is not None:
                policy = {
                    "workload_id": policy_row["workload_id"],
                    "pin": names.get(policy_row["hard_pin"], policy_row["hard_pin"]),
                    "prefer": names.get(
                        policy_row["preference"], policy_row["preference"]
                    ),
                    "allowed_accounts": [
                        names.get(item, item)
                        for item in (
                            json_load(policy_row["allowed_accounts"], None) or []
                        )
                    ] or None,
                }
        result["workload_policy"] = policy
        return result
    finally:
        conn.close()
