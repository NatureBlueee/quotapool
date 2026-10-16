from __future__ import annotations

import faulthandler
import hmac
import json
import os
import signal
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from .launcher import DEFAULT_COORDINATOR_PORT
from .control import (
    WRITER_TTL_SECONDS,
    FleetControl,
    FleetError,
    NoCapacity,
    NotFound,
)
from .db import FleetDB, WriteBudgetExceeded
from . import maintenance as maintenance_module


MAX_BODY_BYTES = 1024 * 1024
DEFAULT_MAINTENANCE_INTERVAL_SECONDS = 6 * 60 * 60
DEFAULT_MAINTENANCE_INITIAL_DELAY_SECONDS = 60
MAINTENANCE_RETRY_BASE_SECONDS = 5 * 60
MAINTENANCE_RETRY_MAX_SECONDS = 30 * 60

# Mirrors the data plane's FLEET_AUTH_MODE. `audit` (the default) changes
# nothing for anyone and only counts what enforcing would have refused; the flip
# to `enforce` is meant to be made from that count after a long enough window,
# and reverted with an environment variable rather than a code rollback.
AUTH_AUDIT = {"unauthenticated_serves": 0, "last_at": None}


class CoordinatorHTTPServer(ThreadingHTTPServer):
    """Own the coordinator port exclusively, especially on Windows.

    ``ThreadingHTTPServer`` enables address reuse in current Python releases.
    On Windows that can let several watchdog-spawned coordinators listen on the
    same port after one slow health probe.  An exclusive bind turns that race
    into a clean startup failure instead of a persistent process leak.
    """

    allow_reuse_address = False

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_EXCLUSIVEADDRUSE,
                1,
            )
        super().server_bind()

    def server_close(self) -> None:
        stop = getattr(self, "maintenance_stop", None)
        if stop is not None:
            stop.set()
        super().server_close()


def _positive_env_seconds(name: str, fallback: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return fallback
    return value if value > 0 else fallback


def _run_maintenance_once(
    db: FleetDB, status: Dict[str, Any], *, now: Optional[float] = None
) -> None:
    """Run bounded state reclamation and publish a small health summary."""
    started = time.time() if now is None else now
    status.update({"running": True, "lastStartedAt": started, "lastError": None})
    try:
        result = maintenance_module.sweep(db, now=now)
        status.update({
            "running": False,
            "lastCompletedAt": time.time() if now is None else now,
            "lastResult": {
                key: value for key, value in result.items()
                if key.endswith("_removed") or key.endswith("_abandoned")
            },
        })
    except Exception as exc:  # maintenance must never take routing down
        status.update({
            "running": False,
            "lastFailedAt": time.time() if now is None else now,
            "lastError": f"{type(exc).__name__}: {exc}",
        })
        print(
            "[ClaudeFleet] automatic maintenance failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def _maintenance_loop(
    db: FleetDB,
    status: Dict[str, Any],
    stop: threading.Event,
    initial_delay: int,
    interval: int,
) -> None:
    if stop.wait(initial_delay):
        return
    failures = 0
    while not stop.is_set():
        status["nextRunAt"] = None
        _run_maintenance_once(db, status)
        if status.get("lastError"):
            failures += 1
            delay = min(
                interval,
                MAINTENANCE_RETRY_MAX_SECONDS,
                MAINTENANCE_RETRY_BASE_SECONDS * 2 ** min(failures - 1, 3),
            )
        else:
            failures = 0
            delay = interval
        status["consecutiveFailures"] = failures
        status["nextRunAt"] = time.time() + delay
        if stop.wait(delay):
            return


def _auth_mode() -> str:
    return "enforce" if os.environ.get("FLEET_AUTH_MODE") == "enforce" else "audit"


def _ratio(value: Any) -> Optional[float]:
    """Normalise a reported utilization to 0..1.

    A value slightly above 1 is not corrupt data — upstream genuinely reports
    utilizations like 1.01 for an account that overshot its window. Discarding
    those as invalid, which this used to do, is the most dangerous possible
    reading: the account looks like it has NO measurement rather than no
    capacity, so a fully-spent account is treated as unknown-and-usable.

    Measured on the Harness server: max-c reported `unified7d: 1.01` and
    `unified7dFable: 0.11`. The 1.01 was dropped, the Fable bucket survived, and
    the control plane recorded weekly_used=0.11 for an account with nothing
    left. `fleet-health` then reported "1 of 3 accounts usable" while all three
    were spent — reporting available capacity that does not exist is worse than
    reporting none.

    Clamped rather than rejected. A negative value or a wildly out-of-range one
    is still treated as no measurement.
    """
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN
        return None
    if result < 0:
        return None
    # Anything past 2.0 is not a utilization ratio; treat it as unreadable
    # rather than silently clamping a percentage (e.g. 101) to "spent".
    if result > 2:
        return None
    return min(result, 1.0)


def _timestamp(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result > 10_000_000_000:
        result /= 1000
    return result


# The data plane gives a routing decision this long before it gives up and
# holds the request. Kept here so the alarm is stated in terms of the deadline
# it is protecting rather than a number chosen for looking round.
DATAPLANE_TIMEOUT_SECONDS = 15.0

# A third of the deadline. Early enough that the trace exists before requests
# start being held, rare enough in a healthy fleet that a row means something:
# measured after the cache fix, decisions run 0.19-0.82s on the Mac under
# ~830 live sessions and 0.01-0.02s on the server.
SLOW_DECISION_SECONDS = DATAPLANE_TIMEOUT_SECONDS / 3


def _effective_weekly(quota: Dict[str, Any], model: Optional[str]) -> tuple:
    candidates = [
        (_ratio(quota.get("unified7d")), _timestamp(quota.get("unified7dReset")))
    ]
    lowered = (model or "").lower()
    if "sonnet" in lowered:
        candidates.append((
            _ratio(quota.get("unified7dSonnet")),
            _timestamp(quota.get("unified7dSonnetReset")),
        ))
    if "fable" in lowered:
        candidates.append((
            _ratio(quota.get("unified7dFable")),
            _timestamp(quota.get("unified7dFableReset")),
        ))
    known = [(used, reset) for used, reset in candidates if used is not None]
    return max(known, key=lambda item: item[0]) if known else (None, None)


class Coordinator:
    def __init__(self, db: FleetDB):
        self.db = db
        self.control = FleetControl(db)
        self._runtime_sync_lock = threading.Lock()
        self._runtime_signatures: Dict[str, tuple] = {}
        # Bound abandoned HTTP retries as well as the client-visible wait.
        # Hundreds of timed-out handlers must not keep mutating the same DB.
        self.route_slots = threading.BoundedSemaphore(4)

    def _identity_collisions(self, accounts: List[Any]) -> set:
        """Two organisations arriving under one key. Refuse both, loudly.

        `fleet-coordinator.js` keys an account as `accountUuid || name` and the
        control plane keeps `org_uuid` only in metadata. One Claude account that
        belongs to two organisations therefore writes both subscriptions into a
        single row: credentials, quota, reserve floor, pins and audit history
        merged. That is data corruption, and it is silent.

        The obvious repair is a composite primary key, and the read-only dry run
        (`fleet-identity-check`) says why that is not today's change:

        - No collision exists on this fleet. Every accountUuid maps to exactly
          one orgUuid on both hosts, so the corruption is latent.
        - The migration is a SQLite table rebuild behind a foreign key —
          16,866 lease rows on the Mac — with no downgrade path.
        - And it would currently BREAK cross-host sync: the server's credential
          file carries no orgUuid for three of five accounts, so the same
          subscription would key as `<uuid>/<org>` on the Mac and `<uuid>/-` on
          the server. `fleet-quota-sync` matches on id, so it would stop
          propagating readings — reintroducing the exact deadlock it was written
          to fix.

        So: remove the danger without the migration. A collision is refused
        rather than absorbed. Both accounts drop out of routing and say so —
        losing the use of an account is recoverable in a way that merging two
        subscriptions' state is not, and it makes the day someone adds a second
        organisation a visible event rather than a quiet corruption.
        """
        by_uuid: Dict[str, set] = {}
        rows: Dict[str, List[str]] = {}
        for raw in accounts:
            if not isinstance(raw, dict):
                continue
            uuid = str(raw.get("account_uuid") or "").strip()
            account_id = str(raw.get("id") or raw.get("name") or "").strip()
            if not uuid or not account_id:
                continue
            by_uuid.setdefault(uuid, set()).add(str(raw.get("org_uuid") or ""))
            rows.setdefault(uuid, []).append(account_id)

        colliding = set()
        for uuid, orgs in by_uuid.items():
            if len(orgs) <= 1:
                continue
            for account_id in rows[uuid]:
                colliding.add(account_id)
            try:
                with self.db.transaction() as conn:
                    self.db.event(conn, "account.identity_collision", uuid, {
                        "org_uuids": sorted(orgs),
                        "account_ids": sorted(set(rows[uuid])),
                        "action": "excluded_from_routing",
                    })
            except Exception:                                 # noqa: BLE001
                pass                     # diagnostics must never break routing
            print(
                "claude-fleet: account %s appears under %d organisations "
                "(%s). Both are excluded from routing rather than merged into "
                "one row — run `fleet-identity-check` and migrate before using "
                "them." % (uuid, len(orgs), ", ".join(sorted(o or "-" for o in orgs))),
                file=sys.stderr,
            )
        return colliding

    def decide(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        session_id = str(payload.get("session_id") or "").strip()
        holder = str(payload.get("holder") or "").strip()
        if not session_id:
            raise FleetError("session_id is required")
        if not holder:
            raise FleetError("holder is required; launch Claude through Fleet")
        # A delayed Agent View provisional request must join the real
        # SessionStart identity instead of recreating a second execution.
        session_id = self.control.resolve_session_id(session_id)
        workload = str(payload.get("workload_class") or "interactive")
        model = payload.get("model")
        accounts = payload.get("accounts")
        if not isinstance(accounts, list) or not accounts:
            raise NoCapacity("TeamClaude reported no subscription accounts")

        runtime_excluded = set(str(item) for item in payload.get("exclude_account_ids") or [])
        runtime_excluded |= self._identity_collisions(accounts)
        runtime_names: Dict[str, str] = {}
        for raw in accounts:
            if not isinstance(raw, dict):
                continue
            account_id = str(raw.get("id") or raw.get("name") or "").strip()
            if not account_id:
                continue
            name = str(raw.get("name") or account_id)
            runtime_names[account_id] = name
            quota = raw.get("quota") if isinstance(raw.get("quota"), dict) else {}
            weekly_used, weekly_reset = _effective_weekly(quota, model)
            unavailable = (
                bool(raw.get("disabled"))
                or raw.get("status") == "error"
                or raw.get("eligible") is False
            )
            if unavailable:
                runtime_excluded.add(account_id)
            five_hour_used = _ratio(quota.get("unified5h"))
            five_hour_reset = _timestamp(quota.get("unified5hReset"))
            metadata = {
                "runtime_status": raw.get("status"),
                "runtime_disabled": bool(raw.get("disabled")),
                "runtime_index": raw.get("index"),
                "account_uuid": raw.get("account_uuid"),
                "org_uuid": raw.get("org_uuid"),
            }
            signature = (
                name,
                bool(raw.get("disabled")),
                five_hour_used,
                five_hour_reset,
                weekly_used,
                weekly_reset,
                tuple(sorted(metadata.items())),
            )
            # TeamClaude reports a cached quota snapshot on every request. Only
            # persist a changed snapshot; otherwise six account transactions per
            # request create a thundering herd under Agent View/Team concurrency.
            with self.db.budget_lock(self._runtime_sync_lock):
                if self._runtime_signatures.get(account_id) != signature:
                    self.control.sync_account_runtime(
                        account_id,
                        name=name,
                        disabled=bool(raw.get("disabled")),
                        five_hour_used=five_hour_used,
                        five_hour_reset_at=five_hour_reset,
                        weekly_used=weekly_used,
                        weekly_reset_at=weekly_reset,
                        metadata=metadata,
                    )
                    self._runtime_signatures[account_id] = signature

        execution = self.control.ensure_execution(
            session_id,
            execution_hint=payload.get("execution_id"),
            workload_class=workload,
            workload_id=payload.get("workload_id"),
            parent_hint=payload.get("parent_execution_id"),
        )
        # Model requests are not a safe place to infer top-level transcript
        # ownership. Claude may legitimately send the same session through a
        # Team member, subagent, fork worker or Agent View transport whose
        # process/CONNECT identity differs from the root window. The launcher
        # already fences an explicit --resume/--session-id before Claude can
        # append to the transcript. Once a live execution owns the session,
        # every descendant request must reuse that writer rather than being
        # mistaken for a second terminal window.
        now = time.time()
        if (
            execution.get("writer_holder")
            and execution.get("writer_expires_at")
            and float(execution["writer_expires_at"]) > now
        ):
            route_holder = str(execution["writer_holder"])
            generation = int(execution["writer_generation"])
        else:
            route_holder = holder
            generation = self.control.acquire_writer(session_id, route_holder)
        decide_started = time.time()
        route = self.control.decide_route(
            session_id,
            route_holder,
            generation,
            exclude=runtime_excluded,
            # The data plane's own account list, which it sends with every
            # request. Previously read only for display names; it is the
            # authoritative set of what this host can actually execute.
            executable=set(runtime_names),
        )
        self._record_if_slow(session_id, time.time() - decide_started, route)
        route["account_name"] = runtime_names.get(route["account_id"], route["account_id"])
        route["execution_id"] = execution["id"]
        route["writer_holder"] = route_holder
        return route

    def _record_if_slow(
        self, session_id: str, elapsed: float, route: Dict[str, Any]
    ) -> None:
        """Leave a trace when a routing decision takes too long.

        A slow decision is invisible by every other measure. The data plane
        holds the request and logs "coordinator unreachable", the health check
        reads a held request as "the fleet waiting, not failing" — which is the
        right reading when the wait is a quota shortage, and exactly the wrong
        one when it is the control plane being slow. Nothing distinguished the
        two, so the second hid inside the first.

        It happened: the optimizer cache was keyed on a count that every
        decision incremented, so it never hit during a burst and each decision
        solved its own min-cost flow inside the write transaction. Measured
        19.0s against a data-plane timeout of 15s. No quota was short; the same
        decision returned an account with room.

        Written only when slow, so the common path costs nothing and the events
        table does not grow by one row per decision. That makes the row itself
        the signal: any of these is worth reading.
        """
        if elapsed < SLOW_DECISION_SECONDS:
            return
        try:
            with self.db.transaction() as conn:
                self.db.event(conn, "route.slow", session_id, {
                    "seconds": round(elapsed, 2),
                    "reason": route.get("reason"),
                    # A cache miss is the expected cause. Its absence points
                    # somewhere else entirely — lock contention, a stalled
                    # query — and that distinction is the whole reason to
                    # record rather than just count.
                    "optimizer_status": (route.get("optimizer") or {}).get("status"),
                    "solved": bool(route.get("optimizer")),
                    # queued  — waiting for SQLite's write lock, i.e. behind
                    #           other decisions
                    # solve   — the min-cost flow, when it ran
                    # inside  — everything after the lock was acquired
                    # Without this split the number is just "slow", and the
                    # first 91 of these already showed the obvious answer is
                    # wrong: 27 were slow without solving at all.
                    "timing": route.get("timing"),
                    "dataplane_timeout_seconds": DATAPLANE_TIMEOUT_SECONDS,
                })
        except Exception:
            # Diagnostics must never be the reason a route fails to return.
            pass

    def pin_check(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """May a pinned request use this account?

        A pin bypasses scheduling on purpose — that is the feature. It must not
        bypass the shared-account reserve, which exists to protect another
        person's quota rather than to express a routing preference.

        Measured before this existed: a `/tc-acct/<uuid>` request reached the
        upstream while the coordinator recorded nothing at all, so `_eligible`
        and the floor never ran. Anything on loopback could have spent the
        shared account to zero.

        Owned accounts stay pinnable regardless of quota: exhausting your own
        account is your business, and refusing would break the manual override
        that requirement (c) is about.
        """
        account_id = str(payload.get("account_id") or "").strip()
        if not account_id:
            raise FleetError("account_id is required")
        try:
            account = self.control.get_account(account_id)
        except NotFound:
            # An account the control plane has never seen cannot be checked
            # against a floor. Owned-by-default is the existing behaviour for
            # unknown accounts elsewhere, and refusing here would break pinning
            # on a fresh install.
            return {"allowed": True, "reason": "account not registered"}

        if account.get("kind") != "shared":
            return {"allowed": True}

        weekly_used = account.get("weekly_used")
        five_used = account.get("five_hour_used")
        # Fail closed on unknown quota, matching `_eligible`'s rule for shared
        # accounts: never spend one blind.
        if weekly_used is None or five_used is None:
            return {
                "allowed": False,
                "reason": (
                    f"{account_id} is shared and its quota is unknown; "
                    "refusing to pin it."
                ),
            }
        weekly_left = 1 - float(weekly_used) - float(account.get("weekly_floor") or 0)
        five_left = 1 - float(five_used) - float(account.get("five_hour_floor") or 0)
        if weekly_left <= 0 or five_left <= 0:
            return {
                "allowed": False,
                "reason": (
                    f"{account_id} is a shared account at its reserve floor "
                    f"(weekly {weekly_left:.2f}, 5h {five_left:.2f} remaining "
                    "after the floor); pinning it would spend someone else's "
                    "quota."
                ),
            }
        return {"allowed": True}

    def renew(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        session_id = str(payload.get("session_id") or "").strip()
        holder = str(payload.get("holder") or "").strip()
        try:
            generation = int(payload.get("generation"))
        except (TypeError, ValueError):
            generation = 0
        if not session_id or not holder or generation < 1:
            raise FleetError("session_id, holder and generation are required")
        session_id = self.control.resolve_session_id(session_id)
        self.control.heartbeat_writer(
            session_id,
            holder,
            generation,
            ttl_seconds=WRITER_TTL_SECONDS,
        )
        return {"ok": True, "writer_generation": generation}


class CoordinatorHandler(BaseHTTPRequestHandler):
    server_version = "ClaudeFleetCoordinator/0.3"

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
        ):
            # The client timed out or was cancelled after the decision
            # completed. There is no second response to send and this must not
            # be reclassified as an internal coordinator failure.
            return

    def _authorized(self) -> bool:
        """Fail closed under enforce; count and allow under audit.

        `if not expected: return True` meant that a coordinator started without
        FLEET_COORDINATOR_TOKEN authenticated nothing at all — every routing
        decision in the fleet available to any local process. On this machine
        the service wrapper does set it (measured: an unauthenticated request
        gets 401), but nothing required that, and the packaging scripts do not
        set it, so anyone installing from source would run wide open.

        Two modes, for the same reason the data plane has them: enforcing blind
        would turn a misconfiguration into a total outage, and this system's
        worst day started with a change that looked equally safe. Audit makes
        the answer measurable first.
        """
        expected = self.server.fleet_token  # type: ignore[attr-defined]
        if expected:
            actual = self.headers.get("Authorization", "")
            return hmac.compare_digest(actual, f"Bearer {expected}")

        AUTH_AUDIT["unauthenticated_serves"] += 1
        AUTH_AUDIT["last_at"] = time.time()
        if AUTH_AUDIT["unauthenticated_serves"] <= 3:
            print(
                "claude-fleet: coordinator has no FLEET_COORDINATOR_TOKEN; "
                "serving without authentication (FLEET_AUTH_MODE=%s). Under "
                "enforce this request would be refused." % _auth_mode(),
                file=sys.stderr,
            )
        return _auth_mode() != "enforce"

    def do_GET(self) -> None:
        if self.path != "/health":
            self._json(404, {"error": "not_found"})
            return
        self._json(200, {
            "ok": True,
            "time": time.time(),
            "maintenance": dict(getattr(
                self.server, "maintenance_status", {"enabled": False}
            )),
        })

    def do_POST(self) -> None:
        if self.path not in {
            "/v1/route/decide", "/v1/writer/renew", "/v1/pin/check",
        }:
            self._json(404, {"error": "not_found"})
            return
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = 0
        if size <= 0 or size > MAX_BODY_BYTES:
            self._json(413, {"error": "invalid_body_size"})
            return
        try:
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError("body must be an object")
            if self.path == "/v1/writer/renew":
                result = self.server.coordinator.renew(payload)  # type: ignore[attr-defined]
            elif self.path == "/v1/pin/check":
                result = self.server.coordinator.pin_check(payload)  # type: ignore[attr-defined]
            else:
                coordinator = self.server.coordinator
                if not coordinator.route_slots.acquire(blocking=False):
                    self._json(503, {"error": "coordinator_busy", "message": "Routing queue is busy; retry shortly"})
                    return
                try:
                    with coordinator.db.write_budget(8.0):
                        result = coordinator.decide(payload)
                finally:
                    coordinator.route_slots.release()
            self._json(200, result)
        except WriteBudgetExceeded:
            self._json(503, {"error": "coordinator_busy", "message": "Database writer exceeded routing deadline; retry shortly"})
        except FleetError as exc:
            status = 409 if isinstance(exc, NoCapacity) else 422
            self._json(status, {"error": exc.code, "message": str(exc)})
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            self._json(400, {"error": "bad_request", "message": str(exc)})
        except Exception as exc:
            print(
                "[ClaudeFleet] coordinator internal error: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            traceback.print_exc()
            self._json(500, {"error": "internal_error"})

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve_coordinator(
    db_path: str,
    host: str = "127.0.0.1",
    port: int = DEFAULT_COORDINATOR_PORT,
    token: Optional[str] = None,
) -> ThreadingHTTPServer:
    # On macOS/Linux, SIGUSR1 is a read-only diagnostic: dump every Python
    # thread stack to the service error log without stopping the coordinator.
    # It makes lock convoys diagnosable without attaching a debugger or
    # logging request payloads.
    try:
        faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
    except (AttributeError, OSError, RuntimeError, ValueError):
        pass
    db = FleetDB(db_path)
    server = CoordinatorHTTPServer((host, port), CoordinatorHandler)
    server.coordinator = Coordinator(db)  # type: ignore[attr-defined]
    server.fleet_token = token  # type: ignore[attr-defined]
    interval = _positive_env_seconds(
        "FLEET_MAINTENANCE_INTERVAL_SECONDS",
        DEFAULT_MAINTENANCE_INTERVAL_SECONDS,
    )
    initial_delay = _positive_env_seconds(
        "FLEET_MAINTENANCE_INITIAL_DELAY_SECONDS",
        DEFAULT_MAINTENANCE_INITIAL_DELAY_SECONDS,
    )
    server.maintenance_status = {  # type: ignore[attr-defined]
        "enabled": True,
        "intervalSeconds": interval,
        "initialDelaySeconds": initial_delay,
        "running": False,
        "lastStartedAt": None,
        "lastCompletedAt": None,
        "lastError": None,
        "lastResult": {},
        "consecutiveFailures": 0,
        "nextRunAt": time.time() + initial_delay,
    }
    server.maintenance_stop = threading.Event()  # type: ignore[attr-defined]
    thread = threading.Thread(
        target=_maintenance_loop,
        args=(db, server.maintenance_status, server.maintenance_stop,
              initial_delay, interval),
        name="quotapool-maintenance",
        daemon=True,
    )
    server.maintenance_thread = thread  # type: ignore[attr-defined]
    thread.start()
    return server
