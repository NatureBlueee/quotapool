from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import uuid
import zlib
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .db import FleetDB, json_dump, json_load
from .optimizer import (
    OptimizationAccount,
    estimate_forecast,
    solve_rolling_horizon,
)


class FleetError(RuntimeError):
    code = "fleet_error"


class NotFound(FleetError):
    code = "not_found"


class LeaseConflict(FleetError):
    code = "lease_conflict"


class StaleWriter(FleetError):
    code = "stale_writer"


class NoCapacity(FleetError):
    code = "no_capacity"


WORKLOADS = {
    "interactive",
    "daemon",
    "workflow",
    "team-lead",
    "team-member",
    "subagent",
    "subagent-bulk",
    "agent-view",
    "fork",
}

# Work that can wait for a later request boundary without a person noticing.
#
# This split is structural, not learned. An earlier plan was to fit a
# time-of-day demand curve, but the data refused it: over the only two days
# available, 18 of 24 hours had a cross-day coefficient of variation above 0.7
# — 04:00 was 3 requests one day and 502 the next. A curve fitted to that
# encodes what happened to be running, not a rhythm, and the owner expects the
# mix to flatten as automated work grows. Classifying by what the work IS
# survives that change; classifying by when it happened does not.
#
# `subagent` and `fork` are deliberately NOT deferrable: they are children of a
# session someone is waiting on, so starving them stalls interactive work while
# looking like background throttling.
DEFERRABLE_WORKLOADS = frozenset({"daemon", "workflow", "subagent-bulk"})

# A Claude process can spend a long time inside Bash, MCP, browser work, an
# Agent Team member, or another tool without making a model request.  A short
# lease would expire during that legitimate work and allow a second window to
# resume the same transcript. Normal SessionEnd releases immediately; crashes
# require an explicit fenced takeover rather than risking silent double-write.
WRITER_TTL_SECONDS = 24 * 60 * 60

# How recently a session must have routed to count as load on an account.
#
# The writer lease runs 24 hours and `renew_writer` short-circuits without
# writing while the lease is more than a quarter of that from expiry, so
# neither `executions.updated_at` nor the lease itself tracks whether a session
# is still doing anything. `route_leases.updated_at` does: it moves on every
# routing decision, which is per request.
#
# Measured on this Mac before the fix: 4,344 executions were `active` with a
# live writer lease and not ONE had a living writer process. Counted that way
# the scheduler saw hello@ carrying 2,272 families and hey@ 1,676; in the last
# half hour the real numbers were 8 and 90. The load term was not merely noisy,
# it pointed the wrong way — and at 20 points per family in `_score` a gap that
# size buries every other term.
ACTIVE_LOAD_WINDOW_SECONDS = 30 * 60

# How much of a weekly window an ESTABLISHED session must still see in front of
# it to keep using the account it is on.
#
# Derived, not chosen: 0.02 is the largest single-step increase ever observed in
# a weekly reading (1,047 samples; median, p90, p95 and p99 are all 0.01 because
# the upstream reports at 1% granularity, and the maximum is 0.02). A margin at
# least that wide means one more request cannot carry the account past 100%.
#
# It is 1 − switchThreshold, which the data plane has carried in config all
# along. That value works; what was missing was any written reason for it, so
# the next person to touch it could not tell 0.02 from a rounded guess.
#
# Applied ONLY to an established session, and the asymmetry is the point:
#
#   established — hitting the wall costs a failed request AND the cache rebuild
#                 that the failover does anyway. Moving first costs only the
#                 rebuild. So move first; it is strictly cheaper.
#   new         — has no cache to lose, and the optimizer already prefers a
#                 roomier account on its own. Applying the margin here would
#                 only park a session that could have used the last 2%, which
#                 wastes quota to prevent nothing.
WEEKLY_SWITCH_AHEAD_MARGIN = 0.02
# The last three days of a seven-day window. Chosen as the span over which an
# unused reserve stops being protection and starts being waste; not measured,
# because what it trades off is one person's convenience against another's, and
# no measurement settles that.
# This host's ceiling on an account's TOTAL observed usage, as a fraction of the
# usable band (1 - floor). 1.0 is the whole band and is the default, so a fleet
# that never configures this behaves exactly as it does today.
#
# Set it so the hosts' values are ORDERED and the largest is 1.0 — e.g. Mac 0.6,
# server 1.0. Giving each host its own disjoint slice instead (0.6 and 0.4) would
# mean the total never reaches the line and the rest of the window is wasted.
#
# Read from runtime.json rather than baked in, because it is a property of this
# machine's place in the fleet, not of the code.
DEFAULT_HOST_BUDGET = 1.0
_HOST_BUDGET_CACHE: Dict[str, float] = {}


def _host_budget() -> float:
    """Cached: this sits inside the per-request eligibility path."""
    if "value" not in _HOST_BUDGET_CACHE:
        value = DEFAULT_HOST_BUDGET
        try:
            from .launcher import load_runtime

            raw = load_runtime().get("host_budget")
            if raw is not None:
                value = float(raw)
        except Exception:                                     # noqa: BLE001
            pass
        # A nonsensical value must not silently become a fleet-wide throttle or
        # an unbounded ceiling; fall back to "no partition" and keep serving.
        if not 0.0 < value <= 1.0:
            value = DEFAULT_HOST_BUDGET
        _HOST_BUDGET_CACHE["value"] = value
    return _HOST_BUDGET_CACHE["value"]


SHARED_FLOOR_TAPER_HOURS = 72.0
# What survives to the reset. A quarter of nominal, so a party who starts late
# still finds room — the taper releases the part that was going to expire, not
# the whole reserve.
SHARED_FLOOR_TAPER_FLOOR = 0.25

# A session will not be moved off the same account by switch-ahead twice inside
# this window.
#
# Structurally this should never fire: quota within a window only increases, so
# an account left because it was nearly spent stays nearly spent, and the only
# way back is its window resetting — which is a legitimate return, not a flap.
# The guard exists because that argument depends on readings being monotonic,
# and a reading that dips across the margin even once would produce a loop.
#
# Keyed on (session, account) rather than "once per session", so a session that
# legitimately has to leave two different accounts still can.
#
# In memory: a coordinator restart clears it, which is correct — a restart is
# not a flap, and persisting this would mean a schema change to defend against
# something that has never been observed.
SWITCH_AHEAD_COOLDOWN_SECONDS = 3600


def expire_stale_windows(conn: sqlite3.Connection, now: float) -> int:
    """A quota window whose reset time has passed no longer describes anything.

    `report_quota` writes every field as COALESCE(?, old), so a NULL means "not
    reported" and can never clear a value. That is right for a reading that
    simply did not arrive, and wrong for one the data plane has already thrown
    away: when a window rolls over the data plane clears its own bucket, the
    control plane never hears about it, and the stale utilisation stands for
    ever.

    Measured on this Mac: `primary@example.com` sat at weekly_used=1.0 with a
    reset timestamp SIXTEEN HOURS in the past. The data plane had already
    cleared it to null. The scheduler went on treating a fresh weekly window as
    an exhausted account, while sessions waited.

    A new window starts at zero — that is what a reset is, not a guess — so the
    used figure is set to 0 rather than to unknown. Unknown would be worse than
    the bug: `decide_route` drops accounts with an unreadable bucket from the
    optimizer entirely, so the account would go from wrongly-spent to
    invisible. The first real response refreshes both fields anyway.

    SHARED ACCOUNTS ARE LEFT ALONE. Assuming zero there means assuming the other
    party has not used the new window either, and this cannot know that. Stale
    and cautious is the correct side to fail on for a subscription that is not
    solely ours; reconcile reports it instead.
    """
    changed = conn.execute(
        """
        UPDATE accounts SET
            weekly_used=CASE WHEN weekly_reset_at IS NOT NULL
                              AND weekly_reset_at<=? THEN 0.0
                         ELSE weekly_used END,
            weekly_reset_at=CASE WHEN weekly_reset_at IS NOT NULL
                                  AND weekly_reset_at<=? THEN NULL
                            ELSE weekly_reset_at END,
            five_hour_used=CASE WHEN five_hour_reset_at IS NOT NULL
                                 AND five_hour_reset_at<=? THEN 0.0
                           ELSE five_hour_used END,
            five_hour_reset_at=CASE WHEN five_hour_reset_at IS NOT NULL
                                     AND five_hour_reset_at<=? THEN NULL
                                ELSE five_hour_reset_at END,
            updated_at=?
        WHERE kind<>'shared'
          AND ((weekly_reset_at IS NOT NULL AND weekly_reset_at<=?)
               OR (five_hour_reset_at IS NOT NULL AND five_hour_reset_at<=?))
        """,
        (now, now, now, now, now, now, now),
    ).rowcount
    if changed:
        FleetDB.event(conn, "account.window_rolled_over", None, {"accounts": changed})
    return changed
RESET_RECONCILE_INTERVAL_SECONDS = 30
OPTIMIZER_CACHE_SECONDS = 5
ROUTE_REOPTIMIZE_SECONDS = 60
WRITER_RENEW_MARGIN_SECONDS = 60 * 60
# SQLite stores a required integer for policy compatibility with explicit
# shared-account caps. Owned accounts use an effectively unbounded sentinel;
# Fleet does not impose a practical session/concurrency ceiling.
UNLIMITED_CONCURRENCY = 2_147_483_647


def _bounded_ratio(value: Optional[float], name: str) -> Optional[float]:
    if value is None:
        return None
    value = float(value)
    if not 0 <= value <= 1:
        raise FleetError(f"{name} must be between 0 and 1")
    return value


def effective_weekly_floor(account, now: Optional[float] = None) -> float:
    """The weekly floor actually in force for this account, for EVERY reader.

    Three separate places computed this and two of them got it wrong in the
    same way — the panel showed the shared account exhausted at -0.02 while the
    scheduler, reading +0.11, routed 58 sessions to it, and `fleet capacity`
    listed an account as usable with -0.22 remaining. Each had its own copy of
    `1 - used - floor` against the STORED floor, which for a shared account is
    not the floor being applied.

    So there is one function now, and the arithmetic that disagreed lives in
    exactly one place.
    """
    floor = float(account["weekly_floor"] or 0)
    if floor and account["kind"] == "shared":
        return _tapered_shared_floor(floor, account["weekly_reset_at"], now)
    return floor


def _tapered_shared_floor(floor: float, reset_at: Optional[float],
                          now: Optional[float] = None) -> float:
    """A reserve on a shared window is worth less as that window runs out.

    The floor exists so the other party can use their share. What it protects
    is their OPPORTUNITY to use it, and that opportunity shrinks with the
    window — quota nobody spends before the reset is lost to everyone, not
    saved for anyone.

    Measured when this was asked for: the shared subscription sat at 0.51 used
    with a 0.4 floor and 46 hours left in its window, so Fleet could touch 0.09
    of it while roughly a third of the week's quota was on course to expire
    unused.

    Tapered linearly across the last SHARED_FLOOR_TAPER_HOURS, down to
    SHARED_FLOOR_TAPER_FLOOR of nominal — never to zero, so a party who starts
    late still finds room. Full floor at every other time.

    Deliberately a function of the clock rather than an edit to the stored
    floor. Lowering the number by hand would carry into the NEXT window, where
    the other party's opportunity is whole again — a temporary judgement
    silently becoming permanent policy, on somebody else's subscription.
    """
    if not reset_at:
        return floor
    hours_left = (float(reset_at) - (now if now is not None else time.time())) / 3600.0
    if hours_left >= SHARED_FLOOR_TAPER_HOURS:
        return floor
    if hours_left <= 0:
        return floor * SHARED_FLOOR_TAPER_FLOOR
    scale = SHARED_FLOOR_TAPER_FLOOR + (1 - SHARED_FLOOR_TAPER_FLOOR) * (
        hours_left / SHARED_FLOOR_TAPER_HOURS)
    return floor * scale


class FleetControl:
    def __init__(self, db: FleetDB):
        self.db = db
        self._reset_reconcile_lock = threading.Lock()
        self._last_reset_reconcile = 0.0
        # Thread-local: the coordinator is a ThreadingHTTPServer and every
        # request shares this one FleetControl, so a plain attribute would be
        # overwritten by whichever decision finished last. Misattributed timing
        # is worse than none — it would send the next reader after the wrong
        # half of the code.
        self._timing = threading.local()
        self._optimizer_cache_lock = threading.Lock()
        self._optimizer_cache: Dict[tuple, Any] = {}
        self._route_reoptimize_lock = threading.Lock()
        self._route_reoptimize_at: Dict[str, float] = {}
        # (session_id, account_id) → 上次因提前换号离开它的时刻。见常量处的说明。
        self._switch_ahead_at: Dict[Tuple[str, str], float] = {}

    def _reconcile_resets_if_due(self, now: float) -> None:
        """Bound reset maintenance to one write transaction per interval.

        Route selection is a hot path and can receive dozens of concurrent
        requests from Agent View and teams. Running ``BEGIN IMMEDIATE`` for
        reset maintenance on every request creates a local lock convoy even
        when no account is exhausted. A 30-second bound is safely below the
        five-minute quota probe cadence while keeping the request path cheap.
        """
        if now - self._last_reset_reconcile < RESET_RECONCILE_INTERVAL_SECONDS:
            return
        with self._reset_reconcile_lock:
            if now - self._last_reset_reconcile < RESET_RECONCILE_INTERVAL_SECONDS:
                return
            self.reconcile_resets(now)
            self._last_reset_reconcile = now

    def upsert_account(
        self,
        account_id: str,
        *,
        kind: Optional[str] = None,
        priority: Optional[int] = None,
        five_hour_floor: Optional[float] = None,
        weekly_floor: Optional[float] = None,
        interactive_reserve: Optional[float] = None,
        max_concurrency: Optional[int] = None,
        allowed_workloads: Optional[Sequence[str]] = None,
        clear_allowed_workloads: bool = False,
    ) -> Dict[str, Any]:
        """Create or update an account's policy.

        Every field is optional and an omitted field PRESERVES its current
        value. This used to take concrete defaults (kind='owned', floors 0) and
        write them unconditionally, so a bare ``fleet account add <id>`` on an
        existing account silently reset ``kind`` to owned and both reserve
        floors to zero — quietly removing the only protection the shared account
        has. Requirement (b) depends on that not happening, and nothing in the
        command's name suggests it would.
        """
        if kind is not None and kind not in {"owned", "shared"}:
            raise FleetError("kind must be owned or shared")
        if five_hour_floor is not None:
            five_hour_floor = _bounded_ratio(five_hour_floor, "five_hour_floor") or 0
        if weekly_floor is not None:
            weekly_floor = _bounded_ratio(weekly_floor, "weekly_floor") or 0
        if interactive_reserve is not None:
            interactive_reserve = _bounded_ratio(
                interactive_reserve, "interactive_reserve"
            ) or 0
        if max_concurrency is not None and max_concurrency < 1:
            raise FleetError("max_concurrency must be >= 1")
        if allowed_workloads is not None:
            unknown = set(allowed_workloads) - WORKLOADS
            if unknown:
                raise FleetError(f"unknown workload classes: {sorted(unknown)}")
        now = time.time()
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM accounts WHERE id=?", (account_id,)
            ).fetchone()

            def resolve(value, column, fallback):
                if value is not None:
                    return value
                return existing[column] if existing is not None else fallback

            final_kind = resolve(kind, "kind", "owned")
            final_priority = int(resolve(priority, "priority", 0))
            final_five_floor = float(
                resolve(five_hour_floor, "five_hour_floor", 0)
            )
            final_weekly_floor = float(resolve(weekly_floor, "weekly_floor", 0))
            final_interactive_reserve = float(
                resolve(interactive_reserve, "interactive_reserve", 0)
            )
            final_concurrency = int(
                resolve(max_concurrency, "max_concurrency", UNLIMITED_CONCURRENCY)
            )
            if clear_allowed_workloads:
                encoded = None
            elif allowed_workloads is not None:
                encoded = json_dump(list(allowed_workloads))
            else:
                encoded = existing["allowed_workloads"] if existing is not None else None

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO accounts(
                        id,kind,state,priority,five_hour_floor,weekly_floor,
                        interactive_reserve,max_concurrency,allowed_workloads,
                        created_at,updated_at
                    ) VALUES(?,?,'healthy',?,?,?,?,?,?,?,?)
                    """,
                    (
                        account_id,
                        final_kind,
                        final_priority,
                        final_five_floor,
                        final_weekly_floor,
                        final_interactive_reserve,
                        final_concurrency,
                        encoded,
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE accounts SET
                        kind=?,priority=?,five_hour_floor=?,weekly_floor=?,
                        interactive_reserve=?,max_concurrency=?,
                        allowed_workloads=?,updated_at=?
                    WHERE id=?
                    """,
                    (
                        final_kind,
                        final_priority,
                        final_five_floor,
                        final_weekly_floor,
                        final_interactive_reserve,
                        final_concurrency,
                        encoded,
                        now,
                        account_id,
                    ),
                )
            self.db.event(conn, "account.upserted", account_id, {
                "kind": final_kind,
                "priority": final_priority,
                "five_hour_floor": final_five_floor,
                "weekly_floor": final_weekly_floor,
                "interactive_reserve": final_interactive_reserve,
                "max_concurrency": final_concurrency,
                "allowed_workloads": json_load(encoded, None),
            })
        return self.get_account(account_id)

    def get_account(self, account_id: str) -> Dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if row is None:
            raise NotFound(f"account {account_id!r} not found")
        result = dict(row)
        result["allowed_workloads"] = json_load(result["allowed_workloads"], None)
        result["metadata"] = json_load(result["metadata"], {})
        return result

    def sync_account_runtime(
        self,
        account_id: str,
        *,
        name: Optional[str] = None,
        disabled: bool = False,
        five_hour_used: Optional[float] = None,
        five_hour_reset_at: Optional[float] = None,
        weekly_used: Optional[float] = None,
        weekly_reset_at: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Import live TeamClaude state without overwriting Fleet policy.

        A missing account is created with conservative owned-account defaults.
        Existing kind, reserve floors, workload allow-list, priority and manual
        state remain authoritative in Fleet.
        """
        five_hour_used = _bounded_ratio(five_hour_used, "five_hour_used")
        weekly_used = _bounded_ratio(weekly_used, "weekly_used")
        now = time.time()
        runtime_metadata = dict(metadata or {})
        if name:
            runtime_metadata["runtime_name"] = name
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO accounts(
                        id,kind,state,priority,five_hour_floor,weekly_floor,
                        max_concurrency,allowed_workloads,five_hour_used,
                        five_hour_reset_at,weekly_used,weekly_reset_at,
                        quota_updated_at,metadata,created_at,updated_at
                    ) VALUES(?,'owned',?,0,0,0,2147483647,NULL,?,?,?,?,?,?,?,?)
                    """,
                    (
                        account_id,
                        "healthy",
                        five_hour_used,
                        five_hour_reset_at,
                        weekly_used,
                        weekly_reset_at,
                        # Same rule as the UPDATE below: a row born without a
                        # reading has no reading age. Stamping `now` here would
                        # make a never-measured account look freshly measured
                        # for ever, since the UPDATE path then never has an old
                        # timestamp to preserve.
                        now if (five_hour_used is not None
                                or weekly_used is not None) else None,
                        json_dump(runtime_metadata),
                        now,
                        now,
                    ),
                )
                event_kind = "account.discovered"
            else:
                merged = json_load(row["metadata"], {})
                merged.update(runtime_metadata)
                conn.execute(
                    """
                    UPDATE accounts SET
                        -- A used figure with no window attached, for an account
                        -- whose window we have ALREADY rolled over, describes
                        -- the previous window. It is not news, and applying it
                        -- undoes the rollover.
                        --
                        -- Measured: hello@ was due at 10:00:00 and
                        -- expire_stale_windows zeroed it at exactly 10:00:00.
                        -- The very next sync restored the data plane's cached
                        -- 1.00 through COALESCE, and because expire had also
                        -- set the reset to NULL, its own guard (reset NOT NULL
                        -- AND in the past) could never fire again. The account
                        -- stayed unusable until the data plane happened to
                        -- learn the new window at 12:49:48 — two hours fifty
                        -- minutes of a fresh weekly window sitting idle while
                        -- sessions waited.
                        -- `used=0 AND reset IS NULL` is the exact fingerprint
                        -- expire_stale_windows leaves behind, and nothing else
                        -- produces it: a live window carries a reset, and an
                        -- unmeasured one carries NULL used. In that state a
                        -- report with no window of its own cannot be describing
                        -- the current one, so it must not be applied.
                        five_hour_used=CASE
                            WHEN ? IS NULL THEN five_hour_used
                            WHEN ? IS NULL AND five_hour_reset_at IS NULL
                                 AND five_hour_used=0 THEN 0
                            ELSE ? END,
                        five_hour_reset_at=COALESCE(?,five_hour_reset_at),
                        weekly_used=CASE
                            WHEN ? IS NULL THEN weekly_used
                            WHEN ? IS NULL AND weekly_reset_at IS NULL
                                 AND weekly_used=0 THEN 0
                            ELSE ? END,
                        weekly_reset_at=COALESCE(?,weekly_reset_at),
                        -- Only advances when a value actually ARRIVED. The
                        -- values are COALESCE'd, so a report carrying nothing
                        -- leaves them untouched; bumping the timestamp anyway
                        -- made it mean "we heard from the data plane" while
                        -- every reader treats it as "this number is that old".
                        --
                        -- Measured: the server's shared account showed a
                        -- reading 12 minutes old whose value was four days
                        -- stale, because the data plane had cleared its own
                        -- bucket and was reporting null on every request. Both
                        -- the panel and my own staleness check believed it.
                        quota_updated_at=CASE
                            WHEN ? IS NULL AND ? IS NULL THEN quota_updated_at
                            ELSE ? END,
                        metadata=?,updated_at=?
                    WHERE id=?
                    """,
                    (
                        five_hour_used, five_hour_reset_at, five_hour_used,
                        five_hour_reset_at,
                        weekly_used, weekly_reset_at, weekly_used,
                        weekly_reset_at,
                        # The CASE above tests both used-values: a report that
                        # carries neither taught us nothing, whatever else it
                        # contained.
                        five_hour_used,
                        weekly_used,
                        now,
                        json_dump(merged),
                        now,
                        account_id,
                    ),
                )
                event_kind = "account.runtime_synced"
            self.db.event(conn, event_kind, account_id, {
                "name": name,
                "disabled": disabled,
                "five_hour_used": five_hour_used,
                "weekly_used": weekly_used,
            })
        return self.get_account(account_id)

    def report_quota(
        self,
        account_id: str,
        *,
        five_hour_used: Optional[float] = None,
        five_hour_reset_at: Optional[float] = None,
        weekly_used: Optional[float] = None,
        weekly_reset_at: Optional[float] = None,
        observed_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        """`observed_at` is WHEN the reading was taken, not when it arrived.

        They are the same thing for a data plane reporting its own response
        headers, which is why this defaulted to now and nothing noticed. They
        are not the same for a reading carried from the other host, and writing
        `now` there would launder a reading taken minutes ago into one that
        looks freshly measured — the exact defect that let the server show a
        twelve-minute-old timestamp on a four-day-old number.

        A reading older than the one already stored is refused rather than
        applied: it describes an earlier moment, so it can only lose
        information, and accepting it would let two hosts syncing in both
        directions walk a row backwards.
        """
        five_hour_used = _bounded_ratio(five_hour_used, "five_hour_used")
        weekly_used = _bounded_ratio(weekly_used, "weekly_used")
        now = time.time()
        observed_at = now if observed_at is None else float(observed_at)
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT quota_updated_at FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"account {account_id!r} not found")
            stored = row["quota_updated_at"]
            if stored is not None and observed_at < stored:
                return {"account_id": account_id, "applied": False,
                        "reason": "older_than_stored"}
            conn.execute(
                """
                UPDATE accounts SET
                    five_hour_used=COALESCE(?,five_hour_used),
                    five_hour_reset_at=COALESCE(?,five_hour_reset_at),
                    weekly_used=COALESCE(?,weekly_used),
                    weekly_reset_at=COALESCE(?,weekly_reset_at),
                    quota_updated_at=?,
                    state=CASE
                        WHEN state='exhausted' AND COALESCE(?,five_hour_used) < 1
                             AND COALESCE(?,weekly_used) < 1 THEN 'healthy'
                        ELSE state
                    END,
                    updated_at=?
                WHERE id=?
                """,
                (
                    five_hour_used,
                    five_hour_reset_at,
                    weekly_used,
                    weekly_reset_at,
                    observed_at,
                    five_hour_used,
                    weekly_used,
                    now,
                    account_id,
                ),
            )
            self.db.event(conn, "quota.reported", account_id, {
                "observed_at": observed_at,
                "five_hour_used": five_hour_used,
                "five_hour_reset_at": five_hour_reset_at,
                "weekly_used": weekly_used,
                "weekly_reset_at": weekly_reset_at,
            })
        # Operator quota reports should affect the next safe request boundary
        # in this process. Runtime probe updates remain naturally bounded by
        # the one-minute rolling optimization cadence to avoid a quota-probe
        # thundering herd across every active Agent View session.
        with self._route_reoptimize_lock:
            for session_id in self._route_reoptimize_at:
                self._route_reoptimize_at[session_id] = 0
        return self.get_account(account_id)

    def set_account_state(self, account_id: str, state: str) -> Dict[str, Any]:
        if state not in {"healthy", "draining", "exhausted", "disabled"}:
            raise FleetError("invalid account state")
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE accounts SET state=?,updated_at=? WHERE id=?",
                (state, time.time(), account_id),
            )
            if cur.rowcount != 1:
                raise NotFound(f"account {account_id!r} not found")
            self.db.event(conn, f"account.{state}", account_id, {})
        return self.get_account(account_id)

    def mark_exhausted(
        self,
        account_id: str,
        *,
        reset_at: Optional[float] = None,
        reason: str = "quota_rejected",
    ) -> None:
        with self.db.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE accounts SET state='exhausted',
                    five_hour_used=1,
                    five_hour_reset_at=COALESCE(?,five_hour_reset_at),
                    updated_at=?
                WHERE id=?
                """,
                (reset_at, time.time(), account_id),
            )
            if cur.rowcount != 1:
                raise NotFound(f"account {account_id!r} not found")
            self.db.event(conn, "account.exhausted", account_id, {
                "reason": reason,
                "reset_at": reset_at,
            })

    def reconcile_resets(self, now: Optional[float] = None) -> List[str]:
        now = now or time.time()
        restored: List[str] = []
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM accounts WHERE state='exhausted'"
            ).fetchall()
            for row in rows:
                five_reset = row["five_hour_reset_at"]
                week_reset = row["weekly_reset_at"]
                five_ready = five_reset is not None and five_reset <= now
                week_ready = week_reset is not None and week_reset <= now
                if not (five_ready or week_ready):
                    continue
                five_used = 0 if five_ready else row["five_hour_used"]
                week_used = 0 if week_ready else row["weekly_used"]
                if (five_used or 0) >= 1 or (week_used or 0) >= 1:
                    continue
                conn.execute(
                    """
                    UPDATE accounts SET state='healthy',
                        five_hour_used=?,weekly_used=?,updated_at=?
                    WHERE id=?
                    """,
                    (five_used, week_used, now, row["id"]),
                )
                self.db.event(conn, "account.reset", row["id"], {
                    "five_hour": five_ready,
                    "weekly": week_ready,
                })
                restored.append(row["id"])
        return restored

    def register_execution(
        self,
        session_id: str,
        *,
        execution_id: Optional[str] = None,
        workload_class: str = "interactive",
        workload_id: Optional[str] = None,
        parent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if workload_class not in WORKLOADS:
            raise FleetError(f"unknown workload class {workload_class!r}")
        execution_id = execution_id or f"exec-{uuid.uuid4().hex[:12]}"
        now = time.time()
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM executions WHERE session_id=?", (session_id,)
            ).fetchone()
            if existing is not None:
                return self._execution_dict(existing)
            if parent_id is not None and conn.execute(
                "SELECT 1 FROM executions WHERE id=?", (parent_id,)
            ).fetchone() is None:
                raise NotFound(f"parent execution {parent_id!r} not found")
            conn.execute(
                """
                INSERT INTO executions(
                    id,session_id,parent_id,workload_class,workload_id,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    execution_id,
                    session_id,
                    parent_id,
                    workload_class,
                    workload_id,
                    now,
                    now,
                ),
            )
            self.db.event(conn, "execution.registered", execution_id, {
                "session_id": session_id,
                "parent_id": parent_id,
                "workload_class": workload_class,
                "workload_id": workload_id,
            })
            row = conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
        return self._execution_dict(row)

    def ensure_execution(
        self,
        session_id: str,
        *,
        execution_hint: Optional[str] = None,
        workload_class: str = "interactive",
        workload_id: Optional[str] = None,
        parent_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            return self.get_execution_by_session(session_id)
        except NotFound:
            pass
        base_execution_id = execution_hint
        execution_id = base_execution_id
        parent_id = None
        with self.db.connect() as conn:
            if execution_id and conn.execute(
                "SELECT 1 FROM executions WHERE id=?", (execution_id,)
            ).fetchone():
                execution_id = (
                    f"{execution_id}:"
                    f"{uuid.uuid5(uuid.NAMESPACE_URL, session_id).hex[:12]}"
                )
            if parent_hint and conn.execute(
                "SELECT 1 FROM executions WHERE id=?", (parent_hint,)
            ).fetchone():
                parent_id = parent_hint
        # Agent View can issue two early requests with one launch execution
        # hint but different provisional/native session IDs. Both can pass the
        # read above before either commits. Retry the narrow unique-key race:
        # same-session requests reuse the winner; sibling sessions derive a
        # stable child ID instead of returning a retryable 500.
        for attempt in range(4):
            try:
                return self.register_execution(
                    session_id,
                    execution_id=execution_id,
                    workload_class=workload_class,
                    workload_id=workload_id,
                    parent_id=parent_id,
                )
            except sqlite3.IntegrityError as exc:
                message = str(exc)
                # Only the two identity constraints are recoverable. A foreign
                # key failure or anything else is a real bug and must surface —
                # this retry loop must not become a place where corruption
                # hides.
                if (
                    "executions.id" not in message
                    and "executions.session_id" not in message
                ):
                    raise
                # Case 1 — another writer created this very session first. Its
                # row is authoritative; reuse it. This is the common Agent
                # View / Team fan-out race.
                try:
                    return self.get_execution_by_session(session_id)
                except NotFound:
                    pass
                # Case 2 — the id collided but the session did not, so the id
                # belongs to a DIFFERENT session and we need a new one.
                #
                # With a launch hint, derive a stable child id so retries of the
                # same request converge on the same value.
                if base_execution_id:
                    suffix = uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        session_id,
                    ).hex[:12]
                    execution_id = f"{base_execution_id}:{suffix}"
                    if attempt:
                        execution_id = f"{execution_id}:{attempt + 1}"
                    continue
                # Without a hint the id was invented by register_execution, so
                # there is nothing stable to derive from — let it invent another.
                #
                # This branch is why the live coordinator returned 500s: it used
                # to `raise` here, converting a recoverable collision into an
                # internal error. A 500 is not a routing decision, so the data
                # plane cannot tell it apart from "the control plane is broken"
                # and the request fails even though an account was available.
                execution_id = None
        raise FleetError("could not allocate a unique execution identity")

    @staticmethod
    def _resolve_session_id(
        conn: sqlite3.Connection,
        session_id: str,
    ) -> str:
        current = session_id
        seen = set()
        for _ in range(32):
            if current in seen:
                raise FleetError("session alias cycle detected")
            seen.add(current)
            row = conn.execute(
                """
                SELECT canonical_session_id
                FROM session_aliases
                WHERE alias_session_id=?
                """,
                (current,),
            ).fetchone()
            if row is None:
                return current
            current = str(row["canonical_session_id"])
        raise FleetError("session alias chain is too deep")

    def resolve_session_id(self, session_id: str) -> str:
        with self.db.connect() as conn:
            return self._resolve_session_id(conn, session_id)

    def session_alias_target(self, session_id: str) -> Optional[str]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT canonical_session_id
                FROM session_aliases
                WHERE alias_session_id=?
                """,
                (session_id,),
            ).fetchone()
        return str(row["canonical_session_id"]) if row is not None else None

    def bind_session_start(
        self,
        execution_hint: str,
        actual_session_id: str,
        *,
        holder: Optional[str] = None,
        workload_class: str = "interactive",
        workload_id: Optional[str] = None,
        parent_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Atomically bind Claude's real SessionStart ID to an execution.

        Agent View and ``--bg`` can expose a launch/provisional ID before the
        worker emits its real SessionStart ID.  The first real session for an
        execution hint takes over the provisional execution, including writer,
        route and manual policy.  Later Agent View children sharing that hint
        receive separate executions instead of moving the first child again.
        The old session becomes a permanent alias so a delayed provisional
        request resolves to the real session rather than recreating a split.
        """
        execution_hint = str(execution_hint or "").strip()
        actual_session_id = str(actual_session_id or "").strip()
        if not execution_hint or not actual_session_id:
            raise FleetError("execution_hint and actual_session_id are required")
        if workload_class not in WORKLOADS:
            raise FleetError(f"unknown workload class {workload_class!r}")
        now = time.time()
        with self.db.transaction() as conn:
            actual_session_id = self._resolve_session_id(
                conn,
                actual_session_id,
            )
            existing_binding = conn.execute(
                """
                SELECT execution_id
                FROM execution_session_bindings
                WHERE session_id=?
                """,
                (actual_session_id,),
            ).fetchone()
            if existing_binding is not None:
                row = conn.execute(
                    "SELECT * FROM executions WHERE id=?",
                    (existing_binding["execution_id"],),
                ).fetchone()
                if row is None:
                    raise FleetError("session binding points to missing execution")
                return self._execution_dict(row)

            bound_count = int(conn.execute(
                """
                SELECT COUNT(*)
                FROM execution_session_bindings
                WHERE execution_hint=?
                """,
                (execution_hint,),
            ).fetchone()[0])
            hint_row = conn.execute(
                "SELECT * FROM executions WHERE id=?",
                (execution_hint,),
            ).fetchone()
            actual_row = conn.execute(
                "SELECT * FROM executions WHERE session_id=?",
                (actual_session_id,),
            ).fetchone()

            # One Agent View supervisor can dispatch many sessions. Once the
            # hint is bound, additional actual sessions are siblings, not a
            # reason to move the first execution.
            if bound_count:
                if actual_row is None:
                    base_id = f"{execution_hint}:{actual_session_id[:12]}"
                    execution_id = base_id
                    suffix = 1
                    while conn.execute(
                        "SELECT 1 FROM executions WHERE id=?",
                        (execution_id,),
                    ).fetchone():
                        suffix += 1
                        execution_id = f"{base_id}:{suffix}"
                    parent_id = None
                    if parent_hint and conn.execute(
                        "SELECT 1 FROM executions WHERE id=?",
                        (parent_hint,),
                    ).fetchone():
                        parent_id = parent_hint
                    conn.execute(
                        """
                        INSERT INTO executions(
                            id,session_id,parent_id,workload_class,workload_id,
                            created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?)
                        """,
                        (
                            execution_id,
                            actual_session_id,
                            parent_id,
                            workload_class,
                            workload_id,
                            now,
                            now,
                        ),
                    )
                    actual_row = conn.execute(
                        "SELECT * FROM executions WHERE id=?",
                        (execution_id,),
                    ).fetchone()
                execution_id = str(actual_row["id"])
                conn.execute(
                    """
                    INSERT INTO execution_session_bindings(
                        execution_hint,session_id,execution_id,bound_at,source
                    ) VALUES(?,?,?,?,?)
                    """,
                    (
                        execution_hint,
                        actual_session_id,
                        execution_id,
                        now,
                        "hook.SessionStart.sibling",
                    ),
                )
                self.db.event(
                    conn,
                    "session.bound_sibling",
                    actual_session_id,
                    {
                        "execution_hint": execution_hint,
                        "execution_id": execution_id,
                    },
                )
                return self._execution_dict(actual_row)

            if hint_row is None and actual_row is None:
                parent_id = None
                if parent_hint and conn.execute(
                    "SELECT 1 FROM executions WHERE id=?",
                    (parent_hint,),
                ).fetchone():
                    parent_id = parent_hint
                conn.execute(
                    """
                    INSERT INTO executions(
                        id,session_id,parent_id,workload_class,workload_id,
                        created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        execution_hint,
                        actual_session_id,
                        parent_id,
                        workload_class,
                        workload_id,
                        now,
                        now,
                    ),
                )
                hint_row = conn.execute(
                    "SELECT * FROM executions WHERE id=?",
                    (execution_hint,),
                ).fetchone()
            elif hint_row is None:
                hint_row = actual_row

            old_session_id = str(hint_row["session_id"])
            execution_id = str(hint_row["id"])
            if old_session_id != actual_session_id:
                if holder and hint_row["writer_holder"] not in {None, holder}:
                    raise LeaseConflict(
                        "provisional execution belongs to a different launch"
                    )
                if actual_row is not None and actual_row["id"] != execution_id:
                    if holder and actual_row["writer_holder"] not in {None, holder}:
                        raise LeaseConflict(
                            "actual session belongs to a different launch"
                        )
                    holders = {
                        value
                        for value in (
                            hint_row["writer_holder"],
                            actual_row["writer_holder"],
                        )
                        if value
                    }
                    if len(holders) > 1:
                        raise LeaseConflict(
                            "cannot merge sessions owned by different launches"
                        )
                    hint_lease = conn.execute(
                        "SELECT * FROM route_leases WHERE session_id=?",
                        (old_session_id,),
                    ).fetchone()
                    actual_lease = conn.execute(
                        "SELECT * FROM route_leases WHERE session_id=?",
                        (actual_session_id,),
                    ).fetchone()
                    selected_lease = hint_lease or actual_lease
                    conn.execute(
                        "DELETE FROM route_leases WHERE session_id IN (?,?)",
                        (old_session_id, actual_session_id),
                    )
                    conn.execute(
                        "UPDATE executions SET parent_id=? WHERE parent_id=?",
                        (execution_id, actual_row["id"]),
                    )
                    conn.execute(
                        """
                        UPDATE session_aliases
                        SET execution_id=?
                        WHERE execution_id=?
                        """,
                        (execution_id, actual_row["id"]),
                    )
                    conn.execute(
                        """
                        UPDATE execution_session_bindings
                        SET execution_id=?
                        WHERE execution_id=?
                        """,
                        (execution_id, actual_row["id"]),
                    )
                    conn.execute(
                        "DELETE FROM executions WHERE id=?",
                        (actual_row["id"],),
                    )
                    final_holder = (
                        actual_row["writer_holder"]
                        or hint_row["writer_holder"]
                    )
                    final_generation = max(
                        int(hint_row["writer_generation"]),
                        int(actual_row["writer_generation"]),
                    )
                    final_expiry = max(
                        float(hint_row["writer_expires_at"] or 0),
                        float(actual_row["writer_expires_at"] or 0),
                    ) or None
                    conn.execute(
                        """
                        UPDATE executions SET
                            session_id=?,
                            parent_id=COALESCE(parent_id,?),
                            workload_id=COALESCE(workload_id,?),
                            status=CASE
                                WHEN status='active' OR ?='active'
                                THEN 'active' ELSE ?
                            END,
                            hard_pin=COALESCE(hard_pin,?),
                            preference=COALESCE(preference,?),
                            next_account=COALESCE(next_account,?),
                            writer_generation=?,
                            writer_holder=?,
                            writer_expires_at=?,
                            updated_at=?
                        WHERE id=?
                        """,
                        (
                            actual_session_id,
                            actual_row["parent_id"],
                            actual_row["workload_id"],
                            actual_row["status"],
                            actual_row["status"],
                            actual_row["hard_pin"],
                            actual_row["preference"],
                            actual_row["next_account"],
                            final_generation,
                            final_holder,
                            final_expiry,
                            now,
                            execution_id,
                        ),
                    )
                    if selected_lease is not None:
                        conn.execute(
                            """
                            INSERT INTO route_leases(
                                session_id,execution_id,account_id,
                                route_generation,holder,writer_generation,
                                created_at,updated_at
                            ) VALUES(?,?,?,?,?,?,?,?)
                            """,
                            (
                                actual_session_id,
                                execution_id,
                                selected_lease["account_id"],
                                selected_lease["route_generation"],
                                selected_lease["holder"],
                                final_generation,
                                selected_lease["created_at"],
                                now,
                            ),
                        )
                else:
                    conn.execute(
                        """
                        UPDATE route_leases
                        SET session_id=?,execution_id=?,updated_at=?
                        WHERE session_id=?
                        """,
                        (
                            actual_session_id,
                            execution_id,
                            now,
                            old_session_id,
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE executions
                        SET session_id=?,updated_at=?
                        WHERE id=?
                        """,
                        (actual_session_id, now, execution_id),
                    )
                conn.execute(
                    """
                    UPDATE session_aliases
                    SET canonical_session_id=?
                    WHERE canonical_session_id=?
                    """,
                    (actual_session_id, old_session_id),
                )
                conn.execute(
                    """
                    INSERT INTO session_aliases(
                        alias_session_id,canonical_session_id,execution_id,
                        created_at,reason
                    ) VALUES(?,?,?,?,?)
                    ON CONFLICT(alias_session_id) DO UPDATE SET
                        canonical_session_id=excluded.canonical_session_id,
                        execution_id=excluded.execution_id,
                        reason=excluded.reason
                    """,
                    (
                        old_session_id,
                        actual_session_id,
                        execution_id,
                        now,
                        "hook.SessionStart",
                    ),
                )

            conn.execute(
                """
                INSERT INTO execution_session_bindings(
                    execution_hint,session_id,execution_id,bound_at,source
                ) VALUES(?,?,?,?,?)
                """,
                (
                    execution_hint,
                    actual_session_id,
                    execution_id,
                    now,
                    "hook.SessionStart",
                ),
            )
            self.db.event(
                conn,
                "session.rebound"
                if old_session_id != actual_session_id
                else "session.bound",
                actual_session_id,
                {
                    "execution_hint": execution_hint,
                    "execution_id": execution_id,
                    "old_session_id": (
                        old_session_id
                        if old_session_id != actual_session_id
                        else None
                    ),
                },
            )
            row = conn.execute(
                "SELECT * FROM executions WHERE id=?",
                (execution_id,),
            ).fetchone()
        return self._execution_dict(row)

    def set_workload_policy(
        self,
        workload_id: str,
        *,
        hard_pin: Optional[str] = None,
        preference: Optional[str] = None,
        route_chain: Optional[Sequence[str]] = None,
        allowed_accounts: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        if not workload_id.strip():
            raise FleetError("workload_id is required")
        referenced = {
            item for item in (
                [hard_pin, preference]
                + list(route_chain or [])
                + list(allowed_accounts or [])
            )
            if item
        }
        with self.db.transaction() as conn:
            known = {
                row["id"] for row in conn.execute("SELECT id FROM accounts").fetchall()
            }
            missing = sorted(referenced - known)
            if missing:
                raise NotFound(f"workload policy accounts not found: {missing}")
            conn.execute(
                """
                INSERT INTO workload_policies(
                    workload_id,hard_pin,preference,route_chain,allowed_accounts,
                    updated_at
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(workload_id) DO UPDATE SET
                    hard_pin=excluded.hard_pin,
                    preference=excluded.preference,
                    route_chain=excluded.route_chain,
                    allowed_accounts=excluded.allowed_accounts,
                    updated_at=excluded.updated_at
                """,
                (
                    workload_id,
                    hard_pin,
                    preference,
                    json_dump(list(route_chain or [])) if route_chain else None,
                    json_dump(list(allowed_accounts or [])) if allowed_accounts else None,
                    time.time(),
                ),
            )
            self.db.event(conn, "workload.policy_set", workload_id, {
                "hard_pin": hard_pin,
                "preference": preference,
                "route_chain": list(route_chain or []),
                "allowed_accounts": list(allowed_accounts or []),
            })
        return self.get_workload_policy(workload_id)

    def get_workload_policy(self, workload_id: str) -> Dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM workload_policies WHERE workload_id=?", (workload_id,)
            ).fetchone()
        if row is None:
            raise NotFound(f"workload policy {workload_id!r} not found")
        result = dict(row)
        result["route_chain"] = json_load(result["route_chain"], [])
        result["allowed_accounts"] = json_load(result["allowed_accounts"], None)
        return result

    @staticmethod
    def _execution_dict(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["route_chain"] = json_load(result["route_chain"], [])
        return result

    def get_execution_by_session(self, session_id: str) -> Dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM executions WHERE session_id=?", (session_id,)
            ).fetchone()
        if row is None:
            raise NotFound(f"session {session_id!r} not found")
        return self._execution_dict(row)

    def acquire_writer(
        self,
        session_id: str,
        holder: str,
        *,
        ttl_seconds: int = WRITER_TTL_SECONDS,
        takeover: bool = False,
    ) -> int:
        now = time.time()
        # The data plane asks for the writer on every model request. The route
        # decision itself validates holder + generation and refreshes the
        # 24-hour lease, so rewriting the same writer and appending an event on
        # every request adds lock contention without strengthening fencing.
        # A concurrent takeover remains safe: decide_route re-checks the
        # database generation before any account is selected.
        with self.db.connect() as conn:
            current = conn.execute(
                """
                SELECT writer_holder,writer_generation,writer_expires_at
                FROM executions WHERE session_id=?
                """,
                (session_id,),
            ).fetchone()
        if (
            current is not None
            and current["writer_holder"] == holder
            and current["writer_expires_at"]
            and float(current["writer_expires_at"]) > now
        ):
            return int(current["writer_generation"])
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM executions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"session {session_id!r} not found")
            same_holder = row["writer_holder"] == holder
            expired = not row["writer_expires_at"] or row["writer_expires_at"] <= now
            if row["writer_holder"] and not same_holder and not expired and not takeover:
                raise LeaseConflict(
                    f"session {session_id!r} already has writer {row['writer_holder']!r}"
                )
            generation = int(row["writer_generation"])
            if not same_holder or expired or takeover:
                generation += 1
            conn.execute(
                """
                UPDATE executions SET writer_generation=?,writer_holder=?,
                    writer_expires_at=?,updated_at=? WHERE session_id=?
                """,
                (generation, holder, now + ttl_seconds, now, session_id),
            )
            self.db.event(conn, "writer.acquired", session_id, {
                "holder": holder,
                "generation": generation,
                "takeover": takeover,
                "expired_previous": expired,
            })
        return generation

    def heartbeat_writer(
        self,
        session_id: str,
        holder: str,
        generation: int,
        *,
        ttl_seconds: int = WRITER_TTL_SECONDS,
    ) -> None:
        now = time.time()
        with self.db.connect() as conn:
            current = conn.execute(
                """
                SELECT writer_holder,writer_generation,writer_expires_at
                FROM executions WHERE session_id=?
                """,
                (session_id,),
            ).fetchone()
        if (
            current is None
            or current["writer_holder"] != holder
            or int(current["writer_generation"]) != int(generation)
        ):
            raise StaleWriter("writer generation is stale")
        renew_margin = min(
            WRITER_RENEW_MARGIN_SECONDS,
            max(1, int(ttl_seconds) // 4),
        )
        if (
            current["writer_expires_at"]
            and float(current["writer_expires_at"]) > now + renew_margin
        ):
            # A 24-hour writer does not need 120 SQLite updates per hour merely
            # because a response is streaming. The read still fences a stale
            # generation; only leases approaching expiry enter the write path.
            return
        with self.db.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE executions SET writer_expires_at=?,updated_at=?
                WHERE session_id=? AND writer_holder=? AND writer_generation=?
                """,
                (now + ttl_seconds, now, session_id, holder, generation),
            )
            if cur.rowcount != 1:
                raise StaleWriter("writer generation is stale")

    def release_writer(
        self,
        session_id: str,
        holder: Optional[str] = None,
        *,
        retain_route: bool = True,
    ) -> None:
        """Release process ownership without losing resume affinity.

        A normal SessionEnd must allow an immediate new process to resume the
        session. The route lease is retained by default so a resumed session
        stays on the same account and preserves prompt-cache affinity.
        """
        now = time.time()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM executions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                return
            if holder and row["writer_holder"] and row["writer_holder"] != holder:
                raise StaleWriter("cannot release a different writer holder")
            conn.execute(
                """
                UPDATE executions SET status='parked',writer_holder=NULL,
                    writer_expires_at=NULL,updated_at=? WHERE session_id=?
                """,
                (now, session_id),
            )
            if not retain_route:
                conn.execute(
                    "DELETE FROM route_leases WHERE session_id=?", (session_id,)
                )
            self.db.event(conn, "writer.released", session_id, {
                "holder": holder,
                "retain_route": retain_route,
            })

    def set_route_policy(
        self,
        session_id: str,
        mode: str,
        value: Optional[Any],
    ) -> Dict[str, Any]:
        mapping = {
            "pin": "hard_pin",
            "prefer": "preference",
            "next": "next_account",
            "chain": "route_chain",
        }
        if mode not in mapping:
            raise FleetError("mode must be pin, prefer, next, or chain")
        column = mapping[mode]
        chain_accounts: List[str] = []
        if mode == "chain":
            chain_accounts = list(value or [])
            value = json_dump(chain_accounts) if chain_accounts else None
        with self.db.transaction() as conn:
            if value and mode != "chain" and conn.execute(
                "SELECT 1 FROM accounts WHERE id=?", (value,)
            ).fetchone() is None:
                raise NotFound(f"account {value!r} not found")
            if chain_accounts:
                known = {
                    row["id"] for row in conn.execute(
                        "SELECT id FROM accounts WHERE id IN (%s)"
                        % ",".join("?" for _ in chain_accounts),
                        tuple(chain_accounts),
                    ).fetchall()
                }
                missing = [item for item in chain_accounts if item not in known]
                if missing:
                    raise NotFound(f"route chain accounts not found: {missing}")
            cur = conn.execute(
                f"UPDATE executions SET {column}=?,updated_at=? WHERE session_id=?",
                (value, time.time(), session_id),
            )
            if cur.rowcount != 1:
                raise NotFound(f"session {session_id!r} not found")
            self.db.event(conn, f"route.{mode}", session_id, {"value": value})
            row = conn.execute(
                "SELECT * FROM executions WHERE session_id=?", (session_id,)
            ).fetchone()
        return self._execution_dict(row)

    @staticmethod
    def _remaining(account: sqlite3.Row, column: str, floor_column: str,
                   now: Optional[float] = None) -> Optional[float]:
        used = account[column]
        if used is None:
            return None
        floor = (effective_weekly_floor(account, now)
                 if column == "weekly_used"
                 else float(account[floor_column]))
        remaining = 1 - float(used) - floor

        # Two hosts share these subscriptions and neither can see the other's
        # decisions — only its readings, and those arrive on responses and are
        # gossiped across at intervals. So both can look at 0.55, both route,
        # and together push past the line the floor exists to protect. The floor
        # is the owner's reserve for his desktop client; breaching it is the one
        # thing the reserve was for.
        #
        # A ceiling on TOTAL observed usage, per host, bounds that. With the Mac
        # at 0.6 and the server at 1.0, the Mac cannot drive the total past 60%
        # of the usable band no matter how stale its view is, and the server
        # bounds the whole thing at the reserve line. What used to be two hosts
        # racing each other to the line becomes one host's in-flight burst
        # against it.
        #
        # Stated precisely because it is easy to oversell: this bounds the
        # overshoot, it does not eliminate it. Eliminating it needs per-host
        # consumption accounting, and the only signal available here —
        # `weekly_used` — is an aggregate that already includes the other host
        # AND the desktop client, so it cannot be split after the fact.
        #
        # Cumulative rather than per-host shares, deliberately. Giving each host
        # its own slice of the band (0.6 and 0.4) would mean the total never
        # reaches the line at all and 40% of the window is simply wasted.
        budget = _host_budget()
        if budget < 1.0:
            # No `min` against the figure above, because with budget < 1 it is
            # provably the smaller of the two and a `min` would be unreachable:
            #
            #   budget·(1-floor) - used  −  (1 - floor - used)
            #     = (budget - 1)·(1 - floor)   ≤ 0   for budget ≤ 1, floor ≤ 1
            #
            # It was written with one, and mutation testing showed no test could
            # reach it — which is the worst kind of guard, since it reads as
            # protection while protecting nothing. The range check in
            # `_host_budget` is what keeps the premise true.
            remaining = budget * max(0.0, 1 - floor) - float(used)
        return remaining

    def _switch_ahead_allowed(self, session_id: str, account_id: Optional[str],
                              now: float) -> bool:
        if account_id is None:
            return True
        last = self._switch_ahead_at.get((session_id, account_id))
        return last is None or now - last >= SWITCH_AHEAD_COOLDOWN_SECONDS

    def _note_switch_ahead(self, session_id: str, account_id: str,
                           now: float) -> None:
        # 顺手清掉过期条目，免得这张表随会话数无界增长。
        cutoff = now - SWITCH_AHEAD_COOLDOWN_SECONDS
        if len(self._switch_ahead_at) > 512:
            for key in [k for k, v in self._switch_ahead_at.items() if v < cutoff]:
                del self._switch_ahead_at[key]
        self._switch_ahead_at[(session_id, account_id)] = now

    def _eligible(
        self,
        account: sqlite3.Row,
        workload: str,
        *,
        existing: bool,
        active_count: int,
        switch_ahead: bool = True,
    ) -> Tuple[bool, str]:
        state = account["state"]
        if state in {"disabled", "exhausted"}:
            return False, state
        if state == "draining" and not existing:
            return False, "draining"
        allowed = json_load(account["allowed_workloads"], None)
        if allowed is not None and workload not in allowed:
            return False, "workload_denied"
        # Session/request concurrency is deliberately not an eligibility
        # constraint. A single normal window may fan out through many Teams,
        # subagents and forks; the subscription's real upstream limits are
        # authoritative. Reserve floors and workload allow-lists protect shared
        # accounts without manufacturing a local queue.
        five_remaining = self._remaining(account, "five_hour_used", "five_hour_floor")
        week_remaining = self._remaining(account, "weekly_used", "weekly_floor")
        if five_remaining is not None and five_remaining <= 0:
            return False, "five_hour_floor"
        if week_remaining is not None and week_remaining <= 0:
            return False, "weekly_floor"
        if (
            existing
            and switch_ahead
            and week_remaining is not None
            and week_remaining <= WEEKLY_SWITCH_AHEAD_MARGIN
        ):
            return False, "switch_ahead"
        # Keep the tail of a window for work someone is waiting on. A batch job
        # can consume a window far faster than a person can, so without this a
        # daemon started at the wrong moment leaves interactive sessions parked
        # while the numbers still look "healthy".
        #
        # Only deferrable classes are held back, and only below the reserve —
        # above it, batch and interactive compete normally. Deliberately not a
        # time-of-day rule: the measured demand curve was not stable enough
        # across days to schedule on, and the owner expects it to flatten.
        reserve = float(account["interactive_reserve"] or 0)
        if reserve > 0 and workload in DEFERRABLE_WORKLOADS:
            headroom = min(
                value for value in (five_remaining, week_remaining)
                if value is not None
            ) if (five_remaining is not None or week_remaining is not None) else None
            if headroom is not None and headroom <= reserve:
                return False, "interactive_reserve"
        if account["kind"] == "shared" and (
            account["five_hour_used"] is None or account["weekly_used"] is None
        ):
            return False, "shared_quota_unknown"
        return True, "ok"

    @staticmethod
    def _score(
        account: sqlite3.Row,
        now: float,
        active_count: int,
        preferred: Optional[str],
    ) -> float:
        score = -float(account["priority"]) * 100
        if account["id"] == preferred:
            score += 10000
        score -= active_count * 20
        # Capacity that will expire soon is more urgent. This is a rolling
        # heuristic, not a claim of exact token prediction.
        for used_key, reset_key, floor_key, weight in (
            ("weekly_used", "weekly_reset_at", "weekly_floor", 1200),
            ("five_hour_used", "five_hour_reset_at", "five_hour_floor", 400),
        ):
            used = account[used_key]
            reset_at = account[reset_key]
            if used is None:
                score -= 500
                continue
            consumable = max(0.0, 1 - float(used) - float(account[floor_key]))
            if reset_at:
                hours = max((float(reset_at) - now) / 3600, 0.25)
                score += weight * consumable / hours
            else:
                score += weight * consumable / 168
        return score

    def _fast_sticky_route(
        self,
        session_id: str,
        holder: str,
        writer_generation: int,
        *,
        excluded: Set[str],
        now: float,
        runnable: Optional[Set[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return a safe established route without entering SQLite's writer queue.

        Route optimization is deliberately periodic, not a reason to rewrite
        the same lease for every model request. On coordinator restart,
        established sessions receive a deterministic 5-59 second stagger
        before their first full re-optimization so hundreds of Agent View
        workers cannot form one ``BEGIN IMMEDIATE`` convoy.
        """
        with self.db.connect() as conn:
            execution = conn.execute(
                "SELECT * FROM executions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            current = conn.execute(
                "SELECT * FROM route_leases WHERE session_id=?",
                (session_id,),
            ).fetchone()
            # The established account must still be one the data plane can use.
            # Without this a session parked on an account that later vanished
            # keeps being handed the same dead id on every request — the 502
            # loop, made sticky.
            if (
                runnable is not None
                and current is not None
                and current["account_id"] not in runnable
            ):
                return None
            if execution is None or current is None:
                return None
            account = conn.execute(
                "SELECT * FROM accounts WHERE id=?",
                (current["account_id"],),
            ).fetchone()
            policy = None
            if execution["workload_id"]:
                policy = conn.execute(
                    "SELECT * FROM workload_policies WHERE workload_id=?",
                    (execution["workload_id"],),
                ).fetchone()

        account_id = str(current["account_id"])
        if (
            execution["status"] != "active"
            or execution["writer_holder"] != holder
            or int(execution["writer_generation"]) != int(writer_generation)
            or not execution["writer_expires_at"]
            or float(execution["writer_expires_at"]) <= now
            or account is None
            or account_id in excluded
        ):
            return None

        hard_pin = execution["hard_pin"] or (
            policy["hard_pin"] if policy is not None else None
        )
        preferred = execution["preference"] or (
            policy["preference"] if policy is not None else None
        )
        route_chain = json_load(execution["route_chain"], [])
        if not route_chain and policy is not None:
            route_chain = json_load(policy["route_chain"], [])
        policy_allowed = set(
            json_load(policy["allowed_accounts"], [])
            if policy is not None and policy["allowed_accounts"] else []
        )
        if (
            execution["next_account"]
            or (hard_pin and hard_pin != account_id)
            or (preferred and preferred != account_id)
            or route_chain
            or (policy_allowed and account_id not in policy_allowed)
        ):
            return None
        eligible, _ = self._eligible(
            account,
            execution["workload_class"],
            existing=True,
            active_count=0,
            switch_ahead=self._switch_ahead_allowed(session_id, account_id, now),
        )
        if not eligible:
            return None

        with self._route_reoptimize_lock:
            due = self._route_reoptimize_at.get(session_id)
            if due is None:
                stagger = 5 + (
                    zlib.crc32(session_id.encode("utf-8"))
                    % max(1, ROUTE_REOPTIMIZE_SECONDS - 5)
                )
                self._route_reoptimize_at[session_id] = now + stagger
            elif due <= now:
                # Reserve the next slot before entering the slow path so
                # concurrent requests for this session do not all optimize.
                self._route_reoptimize_at[
                    session_id
                ] = now + ROUTE_REOPTIMIZE_SECONDS
                return None

        return {
            "account_id": account_id,
            "reason": "sticky",
            "route_generation": int(current["route_generation"]),
            "writer_generation": int(writer_generation),
            "previous_account_id": account_id,
            "optimizer": None,
        }

    def decide_route(
        self,
        session_id: str,
        holder: str,
        writer_generation: int,
        *,
        exclude: Optional[Iterable[str]] = None,
        executable: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        """Pick an account for this session.

        `executable` is the set of account ids the DATA PLANE can actually use.
        It ships with every routing request and was previously read only for
        display names, which left a contract hole: the accounts table can hold
        ids the executor has never heard of, and choosing one produces a 502 on
        every request with `Fleet coordinator selected unknown account`.

        That is not hypothetical. The legacy `max-a/b/c` rows — the same
        subscriptions under their pre-UUID names — sat frozen at weekly_used=1.0
        and were harmless only because "spent" made them unselectable.
        `expire_stale_windows` reset their long-passed windows to zero, they
        became the most attractive accounts in the fleet, and the server 502'd
        every request until they were disabled by hand. ACCOUNTS.md had even
        named this exact scenario as the danger of those rows.

        An empty or absent set means "the caller did not say", and nothing is
        filtered — probes and tests call `decide_route` directly.
        """
        excluded: Set[str] = set(exclude or [])
        runnable: Optional[Set[str]] = None
        if executable is not None:
            candidates_reported = {str(item) for item in executable}
            if candidates_reported:
                runnable = candidates_reported
        now = time.time()
        # Where the seconds go. `route.slow` said 27 of 91 slow decisions never
        # ran the optimizer, which rules the solve out for those and leaves the
        # question open — so measure the two halves rather than guess: how long
        # it took to GET the write transaction (SQLite serialises writers, so
        # this is queueing behind other decisions) versus how long the work
        # inside it took.
        self._timing.current = {}
        _t_before_txn = now
        self._reconcile_resets_if_due(now)
        self._timing.current["resets"] = round(time.time() - _t_before_txn, 3)
        _t_fast = time.time()
        fast_route = self._fast_sticky_route(
            session_id,
            holder,
            writer_generation,
            excluded=excluded,
            now=now,
            runnable=runnable,
        )
        if fast_route is not None:
            # The cheapest path there is, and 27 of the first 91 slow decisions
            # came out of it — slow without ever reaching the optimizer, which
            # is why the breakdown has to start before the transaction rather
            # than at it. Both steps above touch the database on their own.
            self._timing.current["fast_sticky"] = round(time.time() - _t_fast, 3)
            self._timing.current["path"] = "fast_sticky"
            fast_route["timing"] = dict(self._timing.current)
            return fast_route
        self._timing.current["fast_sticky"] = round(time.time() - _t_fast, 3)
        self._timing.current["path"] = "full"
        with self.db.transaction() as conn:
            _t_got_txn = time.time()
            self._timing.current["queued"] = round(
                _t_got_txn - _t_before_txn, 3)
            # 先把已经翻页的窗口归零，再读账号 —— 否则本轮仍会拿过期读数决策。
            expire_stale_windows(conn, now)
            execution = conn.execute(
                "SELECT * FROM executions WHERE session_id=?", (session_id,)
            ).fetchone()
            if execution is None:
                raise NotFound(f"session {session_id!r} not found")
            if (
                execution["writer_holder"] != holder
                or int(execution["writer_generation"]) != int(writer_generation)
                or not execution["writer_expires_at"]
                or execution["writer_expires_at"] <= now
            ):
                raise StaleWriter("writer lease is missing, expired, or stale")
            current = conn.execute(
                "SELECT * FROM route_leases WHERE session_id=?", (session_id,)
            ).fetchone()
            current_is_active = (
                current is not None
                and execution["status"] == "active"
                and execution["writer_expires_at"]
                and float(execution["writer_expires_at"]) > now
            )
            counts = {
                row["account_id"]: int(row["n"])
                for row in conn.execute(
                    """
                    SELECT r.account_id,COUNT(*) AS n
                    FROM route_leases r
                    JOIN executions e ON e.id=r.execution_id
                    WHERE e.status='active'
                      AND e.writer_expires_at>?
                      AND r.updated_at>?
                    GROUP BY r.account_id
                    """,
                    (now, now - ACTIVE_LOAD_WINDOW_SECONDS),
                ).fetchall()
            }
            # Hard concurrency counts every active route lease. Economic load
            # balancing counts top-level writer families instead: Agent View,
            # fork preflight and session rebinding can temporarily expose more
            # than one session alias for the same launch family. Counting those
            # aliases as independent workloads would make an account look
            # artificially busy and waste sooner-expiring subscription capacity.
            load_counts = {
                row["account_id"]: int(row["n"])
                for row in conn.execute(
                    """
                    SELECT r.account_id,
                           COUNT(DISTINCT COALESCE(
                               NULLIF(e.writer_holder,''),
                               e.id
                           )) AS n
                    FROM route_leases r
                    JOIN executions e ON e.id=r.execution_id
                    WHERE e.status='active'
                      AND e.writer_expires_at>?
                      AND r.updated_at>?
                    GROUP BY r.account_id
                    """,
                    (now, now - ACTIVE_LOAD_WINDOW_SECONDS),
                ).fetchall()
            }
            # A current family must not count as load against itself. Without
            # this adjustment a session on A sees B as one family lighter,
            # migrates to B, then flips back to A on its next request.
            effective_load_counts = dict(load_counts)
            if current_is_active and current is not None:
                current_id = current["account_id"]
                effective_load_counts[current_id] = max(
                    0,
                    effective_load_counts.get(current_id, 0) - 1,
                )
            accounts = {
                row["id"]: row for row in conn.execute("SELECT * FROM accounts").fetchall()
            }
            policy = None
            if execution["workload_id"]:
                policy = conn.execute(
                    "SELECT * FROM workload_policies WHERE workload_id=?",
                    (execution["workload_id"],),
                ).fetchone()
            policy_allowed = set(
                json_load(policy["allowed_accounts"], [])
                if policy is not None and policy["allowed_accounts"] else []
            )

            # 提前换号是"有更好的去处才走"，不是"快满了就不许用"。没有替代时
            # 必须退回去继续服务 —— 为了躲一个只在有地方可去时才存在的成本，
            # 把会话停下来，是第一要义明确禁止的那种失败。
            allow_switch_ahead = True

            def eligible(account_id: str, existing: bool = False) -> Tuple[bool, str]:
                if account_id in excluded:
                    return False, "excluded"
                # Checked here rather than at the candidate loop so that
                # hard_pin, route_chain and `next` are covered too — every one
                # of those can name an account directly, and any of them
                # selecting one the data plane cannot use is the same 502.
                if runnable is not None and account_id not in runnable:
                    return False, "not_executable"
                # An account this session was just moved off is not a candidate
                # to move back onto.
                #
                # The margin only stops an EXISTING session from staying; it
                # never stopped one from being routed back in, and a new arrival
                # is not "existing", so the next request made it existing again
                # and moved it out again. Observed on the server: session
                # 501286e3 left ee6bca57 at 15:45 and again at 16:52 — it had to
                # have been routed back in between. The cooldown was not being
                # defeated, it was only setting the period of the loop.
                if not self._switch_ahead_allowed(session_id, account_id, now):
                    return False, "switch_ahead_cooldown"
                if policy_allowed and account_id not in policy_allowed:
                    return False, "workload_account_denied"
                account = accounts.get(account_id)
                if account is None:
                    return False, "missing"
                return self._eligible(
                    account,
                    execution["workload_class"],
                    existing=existing,
                    active_count=counts.get(account_id, 0),
                    switch_ahead=allow_switch_ahead,
                )

            reason = "scheduler"
            chosen: Optional[str] = None
            optimization: Optional[Dict[str, Any]] = None
            next_account = execution["next_account"]
            if next_account:
                ok, why = eligible(
                    next_account,
                    current_is_active and current["account_id"] == next_account,
                )
                if not ok:
                    raise NoCapacity(f"manual next account {next_account!r} unavailable: {why}")
                chosen = next_account
                reason = "manual_next"
            elif execution["hard_pin"] or (policy is not None and policy["hard_pin"]):
                target = execution["hard_pin"] or policy["hard_pin"]
                ok, why = eligible(
                    target,
                    current_is_active and current["account_id"] == target,
                )
                if not ok:
                    raise NoCapacity(f"pinned account {target!r} unavailable: {why}")
                chosen = target
                reason = "hard_pin"
            elif (
                current is not None
                and accounts.get(current["account_id"]) is not None
                and accounts[current["account_id"]]["state"] == "draining"
            ):
                target = current["account_id"]
                ok, _ = eligible(target, current_is_active)
                if ok:
                    chosen = target
                    reason = "sticky"

            chain = json_load(execution["route_chain"], [])
            if not chain and policy is not None:
                chain = json_load(policy["route_chain"], [])
            if chosen is None and chain:
                for account_id in chain:
                    ok, _ = eligible(
                        account_id,
                        current_is_active and current["account_id"] == account_id,
                    )
                    if ok:
                        chosen = account_id
                        reason = "route_chain"
                        break

            moved_ahead = False
            if chosen is None:
                preferred = execution["preference"] or (
                    policy["preference"] if policy is not None else None
                )
                candidates: List[Tuple[float, str]] = []
                optimization_accounts: List[OptimizationAccount] = []
                for account_id, account in accounts.items():
                    is_existing = (
                        current_is_active
                        and current is not None
                        and current["account_id"] == account_id
                    )
                    ok, why = eligible(account_id, is_existing)
                    if not ok:
                        # Remember WHY the session's own account dropped out, so
                        # the resulting move can be labelled. Without this the
                        # event says `scheduler`, which is true of every route
                        # change and therefore tells the operator nothing about
                        # whether switch-ahead is doing anything at all — and
                        # "is it doing anything" is the first acceptance
                        # criterion for this feature.
                        if is_existing and why == "switch_ahead":
                            moved_ahead = True
                        continue
                    candidates.append((
                        self._score(
                            account,
                            now,
                            effective_load_counts.get(account_id, 0),
                            preferred,
                        ),
                        account_id,
                    ))
                    five_remaining = self._remaining(
                        account,
                        "five_hour_used",
                        "five_hour_floor",
                    )
                    weekly_remaining = self._remaining(
                        account,
                        "weekly_used",
                        "weekly_floor",
                        # One clock for the whole decision. Letting each account
                        # read time.time() again would let two candidates be
                        # scored against slightly different taper points.
                        now,
                    )
                    if five_remaining is None or weekly_remaining is None:
                        continue
                    optimization_accounts.append(OptimizationAccount(
                        account_id=account_id,
                        five_remaining=five_remaining,
                        five_reset_at=account["five_hour_reset_at"],
                        weekly_remaining=weekly_remaining,
                        weekly_reset_at=account["weekly_reset_at"],
                        priority=int(account["priority"]),
                        five_floor=float(account["five_hour_floor"]),
                        active_families=effective_load_counts.get(
                            account_id,
                            0,
                        ),
                    ))
                if optimization_accounts:
                    # Preserve operator intent before automatic queue
                    # balancing. ``prefer`` is soft only when its target is
                    # unavailable; account priority is the next explicit
                    # policy tier. Within that tier queue health becomes the
                    # first automatic objective, and the exact rolling
                    # optimizer minimizes quota expiry and migration cost.
                    preferred_candidates = [
                        item for item in optimization_accounts
                        if item.account_id == preferred
                    ]
                    if preferred_candidates:
                        optimization_accounts = preferred_candidates
                    else:
                        minimum_priority = min(
                            item.priority
                            for item in optimization_accounts
                        )
                        optimization_accounts = [
                            item for item in optimization_accounts
                            if item.priority == minimum_priority
                        ]
                    # Deliberately NO load pre-filter here.
                    #
                    # This used to drop every account that was not at the
                    # minimum active-family count, which meant an account with
                    # one session was removed from consideration entirely when
                    # another had none — even if the first held most of a weekly
                    # window that reset within the hour. Because it ran before
                    # the solve, the deadline objective could never correct it,
                    # and the result was precisely the reported failure: a
                    # window refreshing while its quota sat unused.
                    #
                    # Load is still an objective term (ACTIVE_FAMILY_LOAD_COST),
                    # so it continues to break ties between accounts of equal
                    # urgency — it just no longer overrides urgency.
                    current_account = (
                        current["account_id"]
                        if current is not None
                        else None
                    )
                    active_families = sum(load_counts.values())
                    # The load counts are deliberately NOT in the key.
                    #
                    # Every decision that consumes this cache writes a route
                    # lease, which raises the active-family count on the account
                    # it chose, which changes the key for the next decision. The
                    # cache was invalidated by its own consumers, so it could
                    # never hit during a burst — precisely the case it was
                    # written for.
                    #
                    # Measured on a copy of the production database, 24
                    # simultaneous decisions: hit rate 0%, every one solving its
                    # own min-cost flow inside the write transaction, p95 13.8s
                    # against a data-plane timeout of 15s. Replacing only the
                    # solve with a constant took p95 to 1.3s, so the solve is
                    # the whole of it. Live, the same shape appeared as ~10-19s
                    # decisions and a log full of "coordinator unreachable".
                    #
                    # Dropping load from the key means a cached plan can be up
                    # to OPTIMIZER_CACHE_SECONDS stale in load. That is the
                    # bound this cache already accepts for every other input —
                    # quota readings, burn rate, reset times — and load is the
                    # weakest of them: it enters the objective only as a
                    # tie-break between accounts of equal urgency.
                    cache_key = (
                        int(now // OPTIMIZER_CACHE_SECONDS),
                        current_account,
                        preferred,
                        tuple(sorted(
                            (
                                item.account_id,
                                item.five_remaining,
                                item.five_reset_at,
                                item.weekly_remaining,
                                item.weekly_reset_at,
                                item.priority,
                                item.five_floor,
                            )
                            for item in optimization_accounts
                        )),
                    )
                    # The exact solver is deterministic for one planning
                    # snapshot. Reusing that optimum for identical requests in
                    # the same five-second boundary prevents a reconnect storm
                    # from serializing dozens of identical min-cost-flow solves
                    # inside SQLite's write transaction.
                    with self._optimizer_cache_lock:
                        if cache_key in self._optimizer_cache:
                            solution = self._optimizer_cache[cache_key]
                        else:
                            event_rows = conn.execute(
                                """
                                SELECT at,subject,payload
                                FROM events
                                WHERE kind='account.runtime_synced' AND at>=?
                                ORDER BY seq
                                """,
                                (now - 6 * 60 * 60,),
                            ).fetchall()
                            ratio, burn, used_default = estimate_forecast(
                                event_rows,
                                active_families=active_families,
                                now=now,
                            )
                            _t_solve = time.time()
                            solution = solve_rolling_horizon(
                                optimization_accounts,
                                now=now,
                                current_account=current_account,
                                preferred_account=preferred,
                                weekly_burn_per_hour=burn,
                                five_to_weekly_capacity=ratio,
                                used_default_forecast=used_default,
                            )
                            self._timing.current["solve"] = round(
                                time.time() - _t_solve, 3)
                            if len(self._optimizer_cache) >= 128:
                                self._optimizer_cache.clear()
                            self._optimizer_cache[cache_key] = solution
                    if solution is not None:
                        chosen = solution.account_id
                        optimization = solution.as_dict()
                        reason = (
                            "sticky"
                            if current_account == chosen
                            else "rolling_optimal"
                        )
                if candidates:
                    if chosen is None:
                        candidates.sort(key=lambda item: (-item[0], item[1]))
                        chosen = candidates[0][1]
                        reason = "scheduler_fallback"
                elif moved_ahead:
                    # 唯一的候选就是那个快满的账号。退让：继续用它，
                    # 而不是为了"提前"把会话停掉。
                    allow_switch_ahead = False
                    moved_ahead = False
                    for account_id, account in accounts.items():
                        is_existing = (
                            current_is_active
                            and current is not None
                            and current["account_id"] == account_id
                        )
                        ok, _ = eligible(account_id, is_existing)
                        if ok:
                            candidates.append((
                                self._score(
                                    account,
                                    now,
                                    effective_load_counts.get(account_id, 0),
                                    preferred,
                                ),
                                account_id,
                            ))
                    if candidates:
                        candidates.sort(key=lambda item: (-item[0], item[1]))
                        chosen = candidates[0][1]
                        reason = "sticky" if chosen == (
                            current["account_id"] if current is not None else None
                        ) else "scheduler_fallback"
            if chosen is None:
                conn.execute(
                    "UPDATE executions SET status='parked',updated_at=? WHERE id=?",
                    (now, execution["id"]),
                )
                self.db.event(conn, "execution.parked", execution["id"], {
                    "reason": "no_capacity",
                    "excluded": sorted(excluded),
                })
            else:
                previous = current["account_id"] if current is not None else None
                # `switch_ahead` overrides whatever the ranking called it. The
                # ranking's answer ("rolling_optimal") describes how the new
                # account was picked; it does not record that the session left
                # its old one BEFORE hitting the wall, which is the only thing
                # that distinguishes this feature from ordinary rebalancing.
                if moved_ahead and previous is not None and previous != chosen:
                    reason = "switch_ahead"
                    self._note_switch_ahead(session_id, previous, now)
                route_generation = (
                    int(current["route_generation"]) if current is not None else 0
                )
                if previous != chosen:
                    route_generation += 1
                conn.execute(
                    """
                    INSERT INTO route_leases(
                        session_id,execution_id,account_id,route_generation,holder,
                        writer_generation,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        account_id=excluded.account_id,
                        route_generation=excluded.route_generation,
                        holder=excluded.holder,
                        writer_generation=excluded.writer_generation,
                        updated_at=excluded.updated_at
                    """,
                    (
                        session_id,
                        execution["id"],
                        chosen,
                        route_generation,
                        holder,
                        writer_generation,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    UPDATE executions SET status='active',
                        next_account=CASE WHEN ?='manual_next' THEN NULL ELSE next_account END,
                        writer_expires_at=?,updated_at=?
                    WHERE id=?
                    """,
                    (reason, now + WRITER_TTL_SECONDS, now, execution["id"]),
                )
                if previous != chosen:
                    self.db.event(conn, "route.changed", session_id, {
                        "from": previous,
                        "to": chosen,
                        "reason": reason,
                        "route_generation": route_generation,
                        "optimizer": optimization,
                    })
                with self._route_reoptimize_lock:
                    self._route_reoptimize_at[
                        session_id
                    ] = now + ROUTE_REOPTIMIZE_SECONDS
                self._timing.current["inside"] = round(
                    time.time() - _t_got_txn, 3)
                return {
                    "account_id": chosen,
                    "reason": reason,
                    "timing": dict(self._timing.current),
                    "route_generation": route_generation,
                    "writer_generation": writer_generation,
                    "previous_account_id": previous,
                    "optimizer": optimization,
                }
        raise NoCapacity("no eligible subscription account; execution parked")

    def release_execution(self, session_id: str, status: str = "completed") -> None:
        if status not in {"completed", "abandoned", "parked"}:
            raise FleetError("invalid release status")
        with self.db.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE executions SET status=?,writer_holder=NULL,
                    writer_expires_at=NULL,updated_at=? WHERE session_id=?
                """,
                (status, time.time(), session_id),
            )
            if cur.rowcount != 1:
                raise NotFound(f"session {session_id!r} not found")
            conn.execute("DELETE FROM route_leases WHERE session_id=?", (session_id,))
            self.db.event(conn, "execution.released", session_id, {"status": status})

    def tree(self, execution_id: str) -> Dict[str, Any]:
        executions = {item["id"]: item for item in self.db.executions()}
        if execution_id not in executions:
            raise NotFound(f"execution {execution_id!r} not found")
        children: Dict[str, List[str]] = {}
        for item in executions.values():
            if item["parent_id"]:
                children.setdefault(item["parent_id"], []).append(item["id"])

        def build(item_id: str) -> Dict[str, Any]:
            item = dict(executions[item_id])
            item["children"] = [build(child) for child in sorted(children.get(item_id, []))]
            return item

        return build(execution_id)
