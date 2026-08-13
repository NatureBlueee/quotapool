from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from .db import FleetDB
from .launcher import DEFAULT_PROXY_PORT, load_runtime, proxy_port
from .observe import observe


def _dataplane_accounts(port: Optional[int] = None):
    """Which accounts the data plane can actually serve, by its own account.

    Returns None when unreachable — absence of an answer must not be read as
    "no account is usable", which would flag every row.

    No proxy: loopback, and inheriting HTTPS_PROXY would route the probe through
    the thing being probed.

    The port comes from runtime.json rather than a literal. It used to default
    to 13456 and the only caller passed nothing, so a second instance brought up
    on another port for verification would have been reconciled against
    production's data plane — and the report would have looked like it was about
    the instance under test.
    """
    import json as _json
    import urllib.request

    if port is None:
        try:
            port = proxy_port(load_runtime())
        except Exception:                                     # noqa: BLE001
            port = DEFAULT_PROXY_PORT

    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(
            f"http://127.0.0.1:{port}/teamclaude/status", timeout=4
        ) as response:
            live = _json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    # `/teamclaude/status` now serialises `id` — the same `accountUuid || name`
    # the coordinator keys on. When it is present, match on it ALONE.
    #
    # The old behaviour accepted a name as well, because the endpoint began as
    # a display endpoint and carried no identity at all; reading it as if it
    # did flagged all five healthy accounts on this check's first run. But the
    # lenient version has its own blind spot, and it is worse: an account row
    # whose *id* is a name matches on the name and looks perfectly servable.
    # That is precisely the shape that 502'd this fleet — and it was
    # reproduced accidentally, by a diagnostic script that posted a decide
    # payload with `accountUuid || name` against a status endpoint that
    # returned no uuid, minting five enabled account rows keyed by email. This
    # check saw nothing wrong with any of them.
    #
    # The fallback stays for a data plane that predates the `id` field: during
    # a rolling deploy, strict matching would flag every account, which is the
    # failure this check already had once and the reason it must never be the
    # louder of two mistakes.
    entries = live.get("accounts") or []
    serves_id = any("id" in entry for entry in entries)
    usable = set()
    for entry in entries:
        if entry.get("disabled") or entry.get("status") == "error":
            continue
        if serves_id:
            if entry.get("id"):
                usable.add(str(entry["id"]))
            continue
        for value in (entry.get("accountUuid"), entry.get("name")):
            if value:
                usable.add(str(value))
    return usable


_SESSION_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def reconcile_report(db: FleetDB, stale_after: int = 300) -> Dict[str, Any]:
    observed = observe()
    anomalies: List[Dict[str, Any]] = list(observed.get("anomalies") or [])
    now = time.time()
    executions = db.executions()
    leases = {item["session_id"]: item for item in db.leases()}
    processes = db.processes()
    nodes = db.execution_nodes()
    active_writer_families = {
        str(item["writer_holder"])
        for item in executions
        if item.get("writer_holder")
        and item.get("writer_expires_at")
        and float(item["writer_expires_at"]) > now
    }

    for process in processes:
        launch_id = (process.get("metadata") or {}).get("launch_id")
        protected_by_live_writer = launch_id in active_writer_families
        if (
            process["state"] == "active"
            and not protected_by_live_writer
            and now - float(process["last_seen_at"]) > stale_after
        ):
            anomalies.append({
                "type": "STALE_PROCESS",
                "process_id": process["id"],
                "last_seen_at": process["last_seen_at"],
            })
    for execution in executions:
        lease = leases.get(execution["session_id"])
        if execution["status"] == "active" and lease is None:
            anomalies.append({
                "type": "ACTIVE_EXECUTION_WITHOUT_ROUTE",
                "execution_id": execution["id"],
                "session_id": execution["session_id"],
            })
        if lease and lease["writer_generation"] != execution["writer_generation"]:
            anomalies.append({
                "type": "STALE_ROUTE_GENERATION",
                "execution_id": execution["id"],
                "session_id": execution["session_id"],
            })
    known_sessions = {item["session_id"] for item in executions}
    for agent in observed.get("agents") or []:
        session_id = agent.get("sessionId") or agent.get("session_id") or agent.get("id")
        if session_id and str(session_id) not in known_sessions:
            anomalies.append({
                "type": "UNTRACKED_CLAUDE_SESSION",
                "session_id": str(session_id),
                "status": agent.get("status"),
            })
    # Manual controls that point at an account nobody reports any more.
    #
    # `preference` is a SOFT hint: control.py filters the candidate set to it and
    # silently falls through when the filter is empty, which is right for an
    # account that is merely spent — and wrong for one whose id no longer exists.
    # The server ran eight daemon policies pointing at `max-c` for two days after
    # the UUID migration, every one of them inert, with no signal anywhere.
    #
    # A dangling hard_pin is worse: it raises NoCapacity and parks the request.
    # Accounts the optimizer structurally cannot choose.
    #
    # `control._remaining` returns None for an unread bucket, and control.py:1685
    # drops an account from the min-cost flow when either bucket is None. Quota
    # is only learned from the headers of a request that used the account, so an
    # unmeasured account is never routed to and therefore stays unmeasured.
    #
    # Measured on the server: hello@ sat at null while the Mac already knew it
    # was 33% used, so an entire weekly window with 67% left was invisible to a
    # scheduler that was parking every request for no_capacity. `fleet-probe-quota`
    # breaks the deadlock; this makes it visible without running anything.
    accounts = list(db.accounts())
    for row in accounts:
        if row["state"] in {"disabled", "exhausted"}:
            continue
        weekly, five = row["weekly_used"], row["five_hour_used"]
        if weekly is not None and five is not None:
            continue
        # A spent weekly window is not missing capacity, it is gone capacity.
        if weekly is not None and weekly >= 1.0 - float(row["weekly_floor"] or 0):
            continue
        anomalies.append({
            "type": "UNMEASURED_ACCOUNT",
            "account_id": row["id"],
            "missing": [
                name for name, value in (("weekly", weekly), ("five_hour", five))
                if value is None
            ],
            "effect": "excluded from the optimizer; will not be routed to, "
                      "so it cannot measure itself",
            "remedy": "scripts/fleet-probe-quota",
        })

    # A shared account whose window demonstrably rolled over while its reading
    # still says spent.
    #
    # `expire_stale_windows` skips shared accounts on purpose: assuming zero
    # there means assuming the other party has not touched the new window
    # either, which nothing here can know. That is the right side to fail on —
    # but it is not free, and until now nothing said what it cost.
    #
    # Measured on the server: the shared subscription has read `weekly_used=1.0`
    # for 4.2 days AFTER its window reset, with the data plane's own copy
    # already cleared to null, while the Mac reads the same subscription as
    # having room. Unusable for days, and silent about it.
    #
    # Reported, not fixed. Whether to spend one request to learn where the new
    # window stands is a policy question about someone else's share, and it is
    # not this function's to answer.
    for row in accounts:
        if row["kind"] != "shared" or row["state"] == "disabled":
            continue
        reset = row["weekly_reset_at"]
        used = row["weekly_used"]
        if reset is None or used is None or reset > now:
            continue
        if used < 1.0 - float(row["weekly_floor"] or 0):
            continue
        anomalies.append({
            "type": "SHARED_WINDOW_ROLLED_OVER_UNREAD",
            "account_id": row["id"],
            "rolled_over_hours_ago": round((now - reset) / 3600, 1),
            "reading_age_hours": (
                round((now - row["quota_updated_at"]) / 3600, 1)
                if row["quota_updated_at"] else None),
            "effect": "the subscription has been unusable on this host since "
                      "the window reset; the stored reading describes the "
                      "previous window",
            "remedy": "a decision, not a command: reading the new window costs "
                      "one request against the other party's share",
        })

    # An account row the scheduler may choose but the executor cannot serve.
    #
    # This has now cost two outages, both by the same mechanism and both fixed
    # by hand. The legacy `max-a/b/c` rows sat frozen at weekly_used=1.0 and
    # were unselectable only because "spent" happens to disqualify an account;
    # `expire_stale_windows` reset their long-passed windows to zero and they
    # became the most attractive accounts in the fleet, 502ing every request on
    # the server. `primary-current` was sitting in exactly that shape locally,
    # kept harmless only by having no quota reading at all.
    #
    # `executable` (control.py) stops the coordinator CHOOSING one per request.
    # This reports that such a row exists at all, because a latent hazard that
    # is only defused by another component's correctness is still a hazard.
    servable = _dataplane_accounts()
    if servable is not None:
        for row in accounts:
            if row["state"] == "disabled":
                continue
            # The DB keys on uuid while the status endpoint reports the runtime
            # name; `metadata.runtime_name` is the bridge the snapshot already
            # uses for exactly this.
            names = {row["id"]}
            meta = row.get("metadata")
            if isinstance(meta, dict) and meta.get("runtime_name"):
                names.add(str(meta["runtime_name"]))
            if names & servable:
                continue
            anomalies.append({
                "type": "UNSERVABLE_ACCOUNT_ENABLED",
                "account_id": row["id"],
                "state": row["state"],
                "effect": "the scheduler may select it; the data plane has no "
                          "credential for it and every request would fail",
                "remedy": "fleet account disable, or re-import its credential",
            })

    # A manual control aimed at an account that EXISTS but can never serve.
    #
    # `DANGLING_WORKLOAD_POLICY` below only catches a target that is missing.
    # This is the other half, and it is the half that actually bit: disabling
    # the legacy `max-a/b/c` rows to stop a 502 storm left 16 sessions on the
    # server pinned or queued onto `max-c`, every one of them parking on every
    # request, for ever, with nothing reporting it. Trading a loud failure for
    # a silent one is not a fix.
    #
    # `next_account` and `hard_pin` are HARD: decide_route raises NoCapacity
    # rather than routing elsewhere, which is correct — an instruction that
    # cannot be honoured should not be silently replaced with something else —
    # and it means an unusable target stalls that session permanently.
    #
    # Deliberately not flagged: a target that is merely spent. That recovers on
    # its own at the reset, so parking until then is the intended behaviour.
    by_id = {row["id"]: row for row in accounts}
    for execution in executions:
        if execution.get("status") in {"completed", "abandoned"}:
            continue
        for field in ("next_account", "hard_pin"):
            target = execution.get(field)
            if not target:
                continue
            row = by_id.get(target)
            if row is not None and row["state"] != "disabled":
                continue
            anomalies.append({
                "type": "UNSATISFIABLE_MANUAL_CONTROL",
                "session_id": execution["session_id"],
                "field": field,
                "target": target,
                "target_state": "missing" if row is None else row["state"],
                "effect": "decide_route raises NoCapacity for this session on "
                          "every request; it will never route again until the "
                          "control is cleared",
                "remedy": "fleet route %s <session>  (with no account)"
                          % ("next" if field == "next_account" else "pin"),
            })

    known_accounts = {item["id"] for item in accounts}
    for policy in db.workload_policies():
        for field in ("hard_pin", "preference"):
            target = policy.get(field)
            if target and target not in known_accounts:
                anomalies.append({
                    "type": "DANGLING_WORKLOAD_POLICY",
                    "workload_id": policy["workload_id"],
                    "field": field,
                    "target": target,
                    "effect": (
                        "parks every request for this workload"
                        if field == "hard_pin"
                        else "silently ignored; the workload is scheduled "
                             "automatically as if no preference were set"
                    ),
                })
        for target in policy.get("route_chain") or []:
            if target not in known_accounts:
                anomalies.append({
                    "type": "DANGLING_WORKLOAD_POLICY",
                    "workload_id": policy["workload_id"],
                    "field": "route_chain",
                    "target": target,
                    "effect": "this chain entry is skipped",
                })
        for target in policy.get("allowed_accounts") or []:
            if target not in known_accounts:
                anomalies.append({
                    "type": "DANGLING_WORKLOAD_POLICY",
                    "workload_id": policy["workload_id"],
                    "field": "allowed_accounts",
                    "target": target,
                    # An allow-list is a HARD constraint, so a stale entry
                    # shrinks the permitted set rather than being ignored.
                    "effect": "narrows the allow-list; may park the workload",
                })

    # Executions keyed on something that is neither a session UUID nor an
    # anonymous transport id — the shape `--resume <display-name>` used to
    # create, which then held a 24-hour writer lease nothing could ever match.
    for execution in executions:
        session_id = str(execution["session_id"])
        if session_id.startswith("anonymous:"):
            continue
        if _SESSION_UUID.match(session_id):
            continue
        anomalies.append({
            "type": "NON_SESSION_EXECUTION",
            "session_id": session_id,
            "status": execution["status"],
            "holds_writer": bool(
                execution.get("writer_expires_at")
                and float(execution["writer_expires_at"]) > now
            ),
        })

    return {
        "summary": {
            "observed_agents": observed.get("agent_count", 0),
            "fleet_executions": len(executions),
            "fleet_routes": len(leases),
            "fleet_processes": len(processes),
            "execution_nodes": len(nodes),
            "anomaly_count": len(anomalies),
        },
        "anomalies": anomalies,
        "note": (
            "Read-only report. Existing transcripts and Claude processes were not "
            "modified or merged."
        ),
    }
