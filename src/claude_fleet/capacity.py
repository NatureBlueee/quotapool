"""Should a batch job start right now?

Fleet routes requests that arrive; it cannot create or postpone them. So
"spend the quota before it expires" is only half in Fleet's hands — the other
half is whoever decides when daemons and workflows run. That is Harness, not
Fleet.

This is the interface between them. Fleet answers what only Fleet knows: which
accounts have quota, how soon it evaporates, and whether starting batch work
now would eat into what interactive sessions need. Harness decides what to
launch.

The alternative — Fleet learning a time-of-day curve and scheduling around it —
was measured and rejected: across the only two days of data, 18 of 24 hours had
a cross-day coefficient of variation above 0.7, and the owner expects the
daytime peak (his own interactive triggering) to flatten as automated work
grows. A signal derived from live quota state stays correct through that; a
fitted curve does not.

    fleet capacity --for daemon      # exit 0 = go, 1 = wait

Exit 0 means there is capacity a deferrable job may use. Exit 1 means waiting is
correct — either nothing is free, or what remains is reserved for interactive
work.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List

from .control import DEFERRABLE_WORKLOADS, FleetControl
from .control import effective_weekly_floor
from .db import FleetDB, json_load


# Quota expiring within this horizon is "use it or lose it" and is the strongest
# reason to start batch work now. One 5h window plus margin.
URGENT_HORIZON_SECONDS = 6 * 3600


def report_capacity(db: FleetDB, workload: str = "daemon") -> Dict[str, Any]:
    control = FleetControl(db)
    now = time.time()
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    finally:
        conn.close()

    usable: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    expiring_soon = 0.0

    for row in rows:
        metadata = json_load(row["metadata"], {}) or {}
        name = metadata.get("runtime_name") or row["id"]
        ok, why = control._eligible(  # noqa: SLF001 — same-package read
            row, workload, existing=False, active_count=0
        )
        weekly_left = None
        if row["weekly_used"] is not None:
            weekly_left = round(
                1 - float(row["weekly_used"]) - effective_weekly_floor(row, now), 3
            )
        reset_in = (
            float(row["weekly_reset_at"]) - now if row["weekly_reset_at"] else None
        )
        entry = {
            "account": name,
            "weekly_remaining_after_floor": weekly_left,
            "resets_in_hours": round(reset_in / 3600, 1) if reset_in else None,
        }
        if ok:
            usable.append(entry)
            # Quota that will evaporate within the horizon, unless something
            # spends it. This is the number that justifies waking a batch job.
            if (
                weekly_left and weekly_left > 0
                and reset_in is not None and 0 < reset_in <= URGENT_HORIZON_SECONDS
            ):
                expiring_soon += weekly_left
        else:
            entry["blocked_by"] = why
            blocked.append(entry)

    deferrable = workload in DEFERRABLE_WORKLOADS
    can_run = bool(usable)

    if can_run:
        if expiring_soon > 0:
            recommendation = (
                "run now — %.0f%% of a weekly window expires within %dh and will "
                "be lost otherwise"
                % (expiring_soon * 100, URGENT_HORIZON_SECONDS // 3600)
            )
            urgency = "use-it-or-lose-it"
        else:
            recommendation = "capacity available; no quota is about to expire"
            urgency = "normal"
    else:
        reserved = [item for item in blocked
                    if item.get("blocked_by") == "interactive_reserve"]
        if reserved:
            recommendation = (
                "wait — the only remaining quota is reserved for interactive "
                "work. Starting batch work now would park someone's session."
            )
            urgency = "reserved-for-interactive"
        else:
            recommendation = (
                "wait — no account can serve this workload class right now"
            )
            urgency = "no-capacity"

    return {
        "at": now,
        "workload": workload,
        "deferrable": deferrable,
        "can_run_now": can_run,
        "urgency": urgency,
        "recommendation": recommendation,
        "quota_expiring_within_horizon": round(expiring_soon, 3),
        "horizon_hours": URGENT_HORIZON_SECONDS // 3600,
        "usable_accounts": usable,
        "blocked_accounts": blocked,
    }
