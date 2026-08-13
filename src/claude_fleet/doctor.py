"""Health checks.

A note on why the runtime section exists. On 2026-07-30 this doctor reported
`ok: true` and `ready_for_canary: true` on all fourteen configuration checks
while the fleet was simultaneously logging 825 coordinator timeouts, 87
`database is locked` errors and 424 refused sx.org CONNECTs per log window, and
the data plane was parked in "All accounts exhausted — holding connection".

Every one of those checks was correct: they inspect files on disk. The gap was
that nothing inspected the running system. On a laptop a false green is merely
annoying because someone is tailing the logs. On the Harness authority server —
no one watching, and `ready_for_canary` gating a rollout — a doctor that greens a
failing fleet is worse than having no doctor at all.

Checks that cannot be evaluated in the current environment (no log files under
systemd/journald, no state database yet) report `skipped` instead of guessing;
skipped checks never count toward `ok` or `ready_for_canary` in either
direction.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import stat
import time
import plistlib
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .control import _host_budget
from .db import SCHEMA_VERSION, connect_readonly
from .launcher import (
    coordinator_port,
    dataplane_url,
    default_runtime_path,
    proxy_port,
    runtime_portability_problems,
)


# A route decision reads a six-hour events window inside its write transaction,
# so the events table is a direct proxy for how long the single writer lock is
# held. 158k rows measured at 14ms per decision; past this many rows, run
# `fleet maintenance sweep`.
EVENTS_WARN_ROWS = 200_000
# Phantom `active` executions accumulate when requests arrive without a native
# session id. 6,457 of them was the state that motivated retention.
#
# 2,000 was set when that was a lot. It no longer is: the writer lease runs 24
# hours and this host opens roughly 1,900 sessions a day, so the steady state
# peaks near 2,160 just before the 04:40 sweep and sits above 2,000 for most of
# every day. The sweep works — it cleared 4,999 route leases this morning with
# no errors — and the fleet is measurably fine at that size: the load-count
# query runs in 3.0ms median, and 1,036 routing decisions in six hours produced
# zero `route.slow` rows.
#
# So the old value reports a healthy fleet as degraded every day, which is the
# failure this file warns about in three other places.
#
# 4,500 is a little over twice the observed peak. It cannot be reached by one
# day of normal traffic, so crossing it means the sweep stopped working or
# something is leaking — which is what this check was for. The direct measure
# of the harm it stands in for now exists separately: a decision that actually
# runs long writes a `route.slow` row, and fleet-health judges those on their
# own.
ACTIVE_EXECUTIONS_WARN = 4_500
# Rows the sweep has not yet reclaimed. One day of accumulation is normal —
# the sweep runs at 04:40 — and 2,544 was one day's worth at the traffic that
# produced it. Three times that cannot be one day of anything, so it means the
# sweep has stopped or something has stopped releasing its writer.
STALE_ACTIVE_WARN = 8_000
COORDINATOR_ERROR_WARN = 5
DATAPLANE_ERROR_WARN = 50
LOG_TAIL_BYTES = 256 * 1024
# A log untouched for this long describes a past incident, not the running
# system. See _live_log for why this matters more than it looks.
LOG_FRESH_SECONDS = 15 * 60


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _tail(path: Path, limit: int = LOG_TAIL_BYTES) -> Optional[str]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None


_STAMPED = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})")


def _recent_lines(text: str, window: float) -> Optional[str]:
    """Restrict a log tail to the last `window` seconds, or None if undecidable.

    A live log is not the same as recent log *lines*. `dataplane.error.log` grows
    at roughly 330KB/hour, so a 256KB tail spans some 45 minutes and a count over
    it silently mixes a resolved incident into the current reading — which is the
    exact mistake this module's docstring describes.

    `fleet log-sink` prefixes every line with an ISO-8601 local timestamp, so
    where it is in use the window is enforced honestly. Where it is not — the
    macOS LaunchAgents still write raw `StandardErrorPath` output — there is no
    way to date a line, and the correct answer is "cannot tell", not a number.
    """

    lines = text.splitlines()
    stamped = [line for line in lines if _STAMPED.match(line)]
    if not stamped or len(stamped) < max(1, len(lines) // 20):
        return None
    cutoff = time.time() - window
    kept = []
    for line in lines:
        match = _STAMPED.match(line)
        if match:
            try:
                when = time.mktime((
                    int(match.group(1)), int(match.group(2)), int(match.group(3)),
                    int(match.group(4)), int(match.group(5)), int(match.group(6)),
                    0, 0, -1,
                ))
            except (ValueError, OverflowError):
                continue
            keep = when >= cutoff
        else:
            keep = bool(kept)  # continuation line of a kept entry (traceback)
        if keep:
            kept.append(line)
    return "\n".join(kept)


def _log_age(path: Path) -> Optional[float]:
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def _live_log(path: Path, max_age: float = LOG_FRESH_SECONDS) -> bool:
    """Is this log still being written to?

    These logs carry no timestamps, and both service log files are opened in
    append mode across restarts. Counting matches in the last N bytes therefore
    measures *position in the file*, not *recency in time*: on 2026-07-30 the
    coordinator error log's last 1200 lines contained 87 `database is locked`
    errors, and every one of them predated the running process by a day — the
    file had not been written to since the previous afternoon. A check that
    ignores mtime reports a resolved outage as an active one.
    """

    age = _log_age(path)
    return age is not None and age <= max_age


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _private(path: Path) -> Optional[bool]:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        return mode & 0o077 == 0
    except OSError:
        return None


def _wrapper_drift(runtime: Dict[str, Any]) -> Tuple[bool, Any]:
    """Does what is installed still match what is in the repository?

    For most of this system's life the answer was unknowable, because the
    wrappers were not in the repository at all — six shell scripts running in
    production with no source anywhere, including `claude-fleet-shell`, which is
    what `claude` actually resolves to. `pip install` succeeding said nothing
    about whether Fleet would start.

    Now that they are tracked, the failure mode moves rather than disappearing:
    the installed copy and the tracked copy can drift, and a rollback that
    restores the repository would leave the drifted copy running. So compare
    them, and say which ones differ.
    """
    prefix = str(runtime.get("prefix") or "")
    if not prefix:
        return True, "no prefix in runtime config"
    source_dir = Path(prefix).expanduser() / "bin"
    if not source_dir.is_dir():
        return True, "%s does not exist (older install)" % source_dir

    installed_dir = Path.home() / ".local" / "bin"
    differing, missing = [], []
    for source in sorted(source_dir.iterdir()):
        if not source.is_file():
            continue
        installed = installed_dir / source.name
        if not installed.exists():
            missing.append(source.name)
            continue
        try:
            if source.read_bytes() != installed.read_bytes():
                differing.append(source.name)
        except OSError:
            differing.append(source.name)

    if not differing and not missing:
        return True, "%d wrappers match" % len(list(source_dir.iterdir()))
    return False, {
        "differs_from_repo": differing,
        "not_installed": missing,
        "means": "a rollback would restore the repo copy while the installed "
                 "copy keeps running",
    }


def doctor(
    runtime_path: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    runtime_file = (
        Path(runtime_path).expanduser() if runtime_path else default_runtime_path()
    )
    runtime = _read_json(runtime_file)
    checks: List[Dict[str, Any]] = []

    def add(
        name: str,
        ok: bool,
        detail: Any = None,
        *,
        skipped: bool = False,
    ) -> None:
        entry: Dict[str, Any] = {"name": name, "ok": bool(ok), "detail": detail}
        if skipped:
            entry["skipped"] = True
        checks.append(entry)

    add("runtime_config", runtime is not None, str(runtime_file))
    if runtime is None:
        return {"ok": False, "checks": checks}

    team_path = Path(str(runtime.get("teamclaude_config", ""))).expanduser()
    team = _read_json(team_path)
    add("teamclaude_config", team is not None, str(team_path))
    add("runtime_config_private", _private(runtime_file) is True)
    add("teamclaude_config_private", _private(team_path) is True)
    add("installed_wrappers_match_repo", *_wrapper_drift(runtime))
    # Reported, never failed. A single-host fleet wants 1.0 and would be wrong
    # to be nagged about it; a two-host fleet wants the values ordered with one
    # of them at 1.0, and that is a judgement about this machine's place in the
    # fleet rather than something a health check can decide.
    add("host_budget", True, {
        "value": _host_budget(),
        "means": "此机可把某账号的总用量推到「可用带宽 × 该值」为止"
                 if _host_budget() < 1.0 else
                 "未分区：此机可用到预留线为止（单机舰队的正确设置）",
    })
    add(
        "coordinator_listening",
        _port_open(coordinator_port(runtime)),
        "127.0.0.1",
    )
    add(
        "dataplane_listening",
        _port_open(proxy_port(runtime)),
        "127.0.0.1",
    )
    add(
        "ca_present",
        Path(str(runtime.get("ca_path", ""))).is_file(),
        runtime.get("ca_path"),
    )
    add(
        "claude_binary",
        Path(str(runtime.get("claude_binary", ""))).is_file(),
        runtime.get("claude_binary"),
    )
    # runtime.json is written by the macOS bootstrap and carries that machine's
    # absolute paths. On the Harness server a stale `outbound_proxy` fails every
    # upstream request while looking like Anthropic being unreachable, so this
    # has to be a graded check rather than a note in a document.
    portability = runtime_portability_problems(runtime)
    add("runtime_paths_valid_on_this_host", not portability, portability or None)
    if team:
        accounts = team.get("accounts") if isinstance(team.get("accounts"), list) else []
        oauth = [item for item in accounts if isinstance(item, dict) and item.get("type") == "oauth"]
        enabled_oauth = [item for item in oauth if not item.get("disabled")]
        forbidden = [
            item.get("name")
            for item in accounts
            if isinstance(item, dict) and (
                item.get("type") != "oauth" or item.get("upstream")
            )
        ]
        add(
            "two_enabled_oauth_accounts",
            len(enabled_oauth) >= 2,
            {"enabled": len(enabled_oauth), "total": len(oauth)},
        )
        add("no_paid_or_third_party_fallback", not forbidden, forbidden)
        add("auto_update_disabled", team.get("autoUpdate") is False)
        add("warmup_disabled", int(team.get("warmupSeconds", 0)) == 0)
        add(
            "fleet_coordinator_enabled",
            bool((team.get("fleetCoordinator") or {}).get("enabled")),
        )

    settings_path = Path.home() / ".claude" / "settings.json"
    settings = _read_json(settings_path) or {}
    add("process_wrapper_active", bool(settings.get("processWrapper")), settings.get("processWrapper"))

    _runtime_health(runtime, db_path, add)

    result: Dict[str, Any] = {"checks": checks}
    graded = [item for item in checks if not item.get("skipped")]
    result["ok"] = all(item["ok"] for item in graded)
    result["ready_for_canary"] = all(
        item["ok"] for item in graded
        if item["name"] not in {"process_wrapper_active"}
    )
    result["skipped"] = [
        item["name"] for item in checks if item.get("skipped")
    ]
    return result


def _state_dir(runtime: Dict[str, Any], db_path: Optional[str]) -> Optional[Path]:
    target = db_path or runtime.get("db_path")
    if not target:
        return None
    return Path(str(target)).expanduser().parent


def _runtime_health(runtime: Dict[str, Any], db_path: Optional[str], add) -> None:
    """Checks that inspect the running system rather than files on disk."""

    resolved_db = Path(
        str(db_path or runtime.get("db_path") or "")
    ).expanduser() if (db_path or runtime.get("db_path")) else None

    # ── writer-lock path identity ────────────────────────────────────────────
    # db.py derives its advisory lock as f"{db_path}.writer.lock". If a systemd
    # unit and the operator CLI resolve db_path differently — an easy mistake
    # when the unit is root-managed and runs As=nature — the two get separate
    # lock files and therefore separate writer queues against one SQLite file.
    # That reproduces the worst form of the lock convoy on the machine where
    # nobody can watch it happen.
    if resolved_db is None:
        add("db_path_declared", False, "runtime.json has no db_path")
    else:
        add("db_path_declared", True, str(resolved_db))
        if db_path:
            add(
                "db_path_matches_runtime_config",
                Path(str(runtime.get("db_path", ""))).expanduser() == resolved_db,
                {
                    "runtime_config": runtime.get("db_path"),
                    "requested": str(resolved_db),
                    "writer_lock": f"{resolved_db}.writer.lock",
                },
            )

    # ── state database health ────────────────────────────────────────────────
    if resolved_db is None or not resolved_db.exists():
        for name in (
            "db_schema_current",
            "db_integrity",
            "events_table_bounded",
            "active_executions_bounded",
            "no_account_needs_relogin",
            "account_quota_fresh",
        ):
            add(name, True, "state database not present", skipped=True)
    else:
        try:
            conn = connect_readonly(resolved_db, timeout=5)
        except sqlite3.Error as exc:
            for name in (
                "db_schema_current",
                "db_integrity",
                "events_table_bounded",
                "active_executions_bounded",
                "no_account_needs_relogin",
                "account_quota_fresh",
            ):
                add(name, False, f"cannot open state database: {exc}")
            conn = None
        if conn is not None:
            try:
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                add(
                    "db_schema_current",
                    version == SCHEMA_VERSION,
                    {"found": version, "expected": SCHEMA_VERSION},
                )
                quick = conn.execute("PRAGMA quick_check").fetchone()[0]
                add("db_integrity", quick == "ok", quick)

                events = conn.execute("SELECT count(*) FROM events").fetchone()[0]
                add(
                    "events_table_bounded",
                    events < EVENTS_WARN_ROWS,
                    {
                        "rows": events,
                        "warn_at": EVENTS_WARN_ROWS,
                        "why": "decide_route scans a six-hour events window "
                               "inside its write transaction",
                        "remedy": "fleet maintenance sweep",
                    },
                )
                active = conn.execute(
                    "SELECT count(*) FROM executions WHERE status='active'"
                ).fetchone()[0]
                # A LEAK, not a traffic level.
                #
                # This counted every active row, so the threshold had to track
                # how busy the fleet was — set at 2,000, raised to 4,500 from a
                # measured peak of 2,160, and failing again at 7,591 a day later
                # because traffic had more than doubled. Each time the fleet was
                # measurably fine: the load-count query ran in 3ms and six hours
                # produced zero slow decisions. A check that has to be re-tuned
                # every time work grows is measuring the wrong thing.
                #
                # What cannot be explained by traffic is a row whose writer
                # lease has EXPIRED and which is still marked active. Normal
                # operation never clears those — only the sweep does — so they
                # accumulate for at most a day and then go. Growth beyond that
                # means the sweep stopped or something is not releasing.
                #
                # After a sweep just now: 7,591 active became 5,053, every one
                # with a live writer. The 2,544 with dead ones were the whole
                # of the excess.
                stale_active = conn.execute(
                    "SELECT count(*) FROM executions WHERE status='active' "
                    "AND writer_expires_at IS NOT NULL AND writer_expires_at<=?",
                    (time.time(),),
                ).fetchone()[0]
                add(
                    "no_leaked_executions",
                    stale_active < STALE_ACTIVE_WARN,
                    {"stale_active": stale_active, "warn_at": STALE_ACTIVE_WARN,
                     "active_total": active,
                     "remedy": "fleet maintenance sweep"},
                )
                add(
                    "active_executions_bounded",
                    # Reported, not failed. The direct measure of the harm this
                    # stood in for is `route.slow`, which fleet-health judges on
                    # its own; this number now only says how busy the fleet is.
                    True,
                    {
                        "active": active,
                        "warn_at": ACTIVE_EXECUTIONS_WARN,
                        "remedy": "fleet maintenance sweep",
                    },
                )

                # An account whose refresh token was rejected keeps being
                # retried by the data plane. It is excluded from scheduling, but
                # only an interactive `fleet login` can restore it, so it has to
                # be visible rather than buried in a log.
                needs_login = []
                stale_quota = []
                stale_by_policy = []
                now = time.time()
                for row in conn.execute(
                    "SELECT id,metadata,quota_updated_at,state,kind,"
                    "weekly_reset_at,weekly_used,weekly_floor FROM accounts"
                ).fetchall():
                    meta = {}
                    try:
                        meta = json.loads(row[1] or "{}")
                    except json.JSONDecodeError:
                        pass
                    name = meta.get("runtime_name") or row[0]
                    # Disabled first. A retired account with a dead token is a
                    # decision, not a fault, and reporting it as one pinned this
                    # check permanently red over primary-current — the exact
                    # "always red trains you to ignore red" failure this module
                    # warns about elsewhere.
                    #
                    # BOTH forms of disabled. `runtime_disabled` is the data
                    # plane's opinion; `state='disabled'` is Fleet's own, set by
                    # hand when a row is retired. Only the first was checked, so
                    # the server's three retired legacy rows — deliberately
                    # switched off after they 502'd every request — were still
                    # required to produce fresh quota readings, and one of them
                    # was 5.5 days stale by construction. Three permanent reds
                    # on rows nobody will ever route to again.
                    if meta.get("runtime_disabled") or row[3] == "disabled":
                        continue
                    if meta.get("runtime_status") == "error":
                        needs_login.append(name)
                        continue
                    # A shared account whose window has rolled over is stale
                    # for a REASON THIS CHECK CANNOT ACT ON. `expire_stale_windows`
                    # skips shared accounts deliberately — assuming zero there
                    # would be assuming the other party has not touched the new
                    # window — and the data plane stops reporting the account
                    # once its own copy clears. So the reading freezes by design,
                    # and nothing automatic will ever unfreeze it.
                    #
                    # reconcile's SHARED_WINDOW_ROLLED_OVER_UNREAD already owns
                    # this condition and states the remedy, which is a decision
                    # about someone else's subscription rather than a repair.
                    # Failing here too would pin `fleet doctor` red until that
                    # decision is made — and this module's own comment, three
                    # checks up, is that always-red trains you to ignore red.
                    #
                    # Still listed, never silently dropped: the point is to move
                    # the alarm to the check that has the context, not to mute it.
                    reset = row[5]
                    spent = (row[6] is not None
                             and float(row[6]) >= 1.0 - float(row[7] or 0))
                    # A spent account receives no traffic, and readings arrive
                    # on the response of a real request — so its reading MUST
                    # go stale. Nothing is wrong, and `expire_stale_windows`
                    # clears it the moment the window turns over. Failing here
                    # means failing on every exhausted account, every time, for
                    # the whole of its window: measured on hello@ at 9.1 hours
                    # stale with 31 hours still to run.
                    #
                    # What distinguishes that from the real defect is WHICH SIDE
                    # of the reset we are on. A spent reading whose window has
                    # already passed is the frozen snapshot this check exists
                    # for — nothing will refresh it, and the account is lost
                    # until someone acts. That is the state the server's shared
                    # subscription sat in for four days.
                    # The rule, stated once: an account that is ROUTABLE and
                    # silent is a broken sync. An account that is spent is not
                    # routable, so its silence is the expected consequence of
                    # the fleet working correctly, and it clears the moment
                    # `expire_stale_windows` turns the window over.
                    owned_by_reconcile = spent
                    if row[2] is None:
                        # Never measured. Semantically the most stale state
                        # there is, but `now - 0` would print it as an age of
                        # fifty-six years, which reads as a broken clock rather
                        # than as the real condition.
                        entry = {"account": name, "age": "never read"}
                    elif now - float(row[2]) > 1800:
                        entry = {"account": name,
                                 "age_seconds": int(now - float(row[2]))}
                    else:
                        continue
                    if owned_by_reconcile:
                        entry["reason"] = (
                            "shared window rolled over; see "
                            "SHARED_WINDOW_ROLLED_OVER_UNREAD"
                            if row[4] == "shared" and reset is not None
                            and reset < now
                            else "account is spent, so it receives no traffic "
                                 "and cannot produce a reading; clears at reset")
                        stale_by_policy.append(entry)
                    else:
                        stale_quota.append(entry)
                add("no_account_needs_relogin", not needs_login, needs_login)
                # A frozen quota snapshot pins weekly_used at its last value. If
                # that value is 1.0, every account fails its weekly floor and the
                # fleet parks every request forever while the numbers look
                # plausible in `fleet status`.
                add("account_quota_fresh", not stale_quota,
                    stale_quota + stale_by_policy)
            finally:
                conn.close()

    # Both request-path services were installed as ProcessType Background,
    # which on macOS is the throttled band — the documented class for work the
    # user is NOT waiting on. Every session waits on the coordinator.
    #
    # Measured under five busy cores, one variable changed: PRI 4 gave p90
    # 1.38s and worst 2.14s; PRI 31 gave p90 0.07s and worst 0.07s, with half
    # again the throughput. The data plane holds a request when a decision
    # takes 15s, so this band is upstream of every hold the fleet reports.
    #
    # Checked rather than merely fixed because nothing in this repository
    # creates those two plists — they were installed by hand, so a reinstall
    # silently returns them to Background and the latency comes back with no
    # signal at all. The periodic timers are correctly Background and are not
    # covered here: they are batch work nobody waits on.
    if sys.platform == "darwin":
        throttled = []
        agents = Path.home() / "Library" / "LaunchAgents"
        for label in ("com.claude-fleet.coordinator", "com.claude-fleet.dataplane"):
            plist = agents / f"{label}.plist"
            if not plist.is_file():
                continue
            try:
                found = plistlib.loads(plist.read_bytes()).get("ProcessType")
            except Exception:
                continue
            if found == "Background":
                throttled.append({"service": label, "process_type": found,
                                  "should_be": "Interactive"})
        # This fails, and the road to knowing that is worth writing down.
        #
        # I first blamed this band for an 11-20s latency on an endpoint that
        # does no I/O. Then I "falsified" it: same process, same band, under a
        # millisecond. That falsification was worthless — the machine was IDLE,
        # and an idle machine cannot reproduce starvation. A synthetic five-core
        # load then gave a modest p90 0.10s against 0.06s, so I downgraded this
        # to a preference.
        #
        # Measured properly, on the real machine at load average 30 across ten
        # cores, 25 samples of a constants-only endpoint:
        #
        #     Background  PRI  4   0.0009-15.148s   1 outright timeout
        #     Interactive PRI 31   0.0009-0.020s    0
        #
        # Event loop lag peaked at 13,583ms against 19ms. The data plane gives a
        # routing decision 15 seconds before it holds the request, so a service
        # that cannot answer a constant inside that window is not a preference
        # question. My first instinct was right; the retraction was the error,
        # and it came from testing on an idle box.
        #
        # Checked rather than merely set because nothing in this repository
        # creates those two plists — they were installed by hand, so a reinstall
        # silently returns them to Background.
        add(
            "request_path_not_throttled",
            not throttled,
            [dict(item, apply="fleet-reload-service %s <plist> --probe "
                              "%s/teamclaude/ready"
                              % (item["service"], dataplane_url(runtime)))
             for item in throttled]
            or "coordinator and data plane are not in the throttled band",
        )

    # ── service logs ─────────────────────────────────────────────────────────
    state_dir = _state_dir(runtime, db_path)
    coordinator_log = (state_dir / "coordinator.error.log") if state_dir else None
    dataplane_log = (state_dir / "dataplane.error.log") if state_dir else None

    if coordinator_log is None or not coordinator_log.is_file():
        add(
            "coordinator_internal_errors",
            True,
            "no coordinator error log (journald?)",
            skipped=True,
        )
        add("coordinator_db_not_locked", True, "no coordinator error log", skipped=True)
    elif not _live_log(coordinator_log):
        quiet = {
            "log": str(coordinator_log),
            "idle_seconds": int(_log_age(coordinator_log) or 0),
            "note": "no new coordinator errors; older entries describe a past "
                    "incident and are not graded",
        }
        add("coordinator_internal_errors", True, quiet, skipped=True)
        add("coordinator_db_not_locked", True, quiet, skipped=True)
    else:
        tail = _tail(coordinator_log) or ""
        internal = tail.count("[ClaudeFleet] coordinator internal error")
        locked = tail.count("database is locked")
        add(
            "coordinator_internal_errors",
            internal <= COORDINATOR_ERROR_WARN,
            {
                "in_last_bytes": LOG_TAIL_BYTES,
                "count": internal,
                "warn_above": COORDINATOR_ERROR_WARN,
            },
        )
        add(
            "coordinator_db_not_locked",
            locked == 0,
            {"count": locked, "log": str(coordinator_log)},
        )

    if dataplane_log is None or not dataplane_log.is_file():
        for name in (
            "dataplane_route_reachable",
            "no_third_party_egress_in_use",
            "dataplane_not_parked",
        ):
            add(name, True, "no data plane error log (journald?)", skipped=True)
    elif not _live_log(dataplane_log):
        quiet = {
            "log": str(dataplane_log),
            "idle_seconds": int(_log_age(dataplane_log) or 0),
            "note": "data plane error log is idle; entries are historical",
        }
        for name in (
            "dataplane_route_reachable",
            "no_third_party_egress_in_use",
            "dataplane_not_parked",
        ):
            add(name, True, quiet, skipped=True)
    else:
        raw_tail = _tail(dataplane_log) or ""
        tail = _recent_lines(raw_tail, LOG_FRESH_SECONDS)
        undated = tail is None
        if undated:
            tail = raw_tail
        undatable = {
            "note": "log lines carry no timestamps, so recency cannot be "
                    "established; counts would mix in a past incident",
            "remedy": "route the service through `fleet log-sink` (the systemd "
                      "units do this) and restart it",
        }
        timeouts = len(re.findall(r"Fleet coordinator unavailable", tail))
        if undated:
            add(
                "dataplane_route_reachable",
                True,
                {**undatable, "matches_in_tail": timeouts},
                skipped=True,
            )
        else:
            add(
                "dataplane_route_reachable",
                timeouts <= DATAPLANE_ERROR_WARN,
                {
                    "coordinator_unavailable": timeouts,
                    "window_seconds": LOG_FRESH_SECONDS,
                    "warn_above": DATAPLANE_ERROR_WARN,
                },
            )
        # The startup guard rejects sx.org when Fleet mode is on, but it only
        # runs at startup: a config reload can arm sx afterwards, and a
        # long-lived process keeps whatever it was given. The architecture
        # forbids IP-evasion egress, so its appearance in the live log is a
        # contract violation regardless of what the config file now says.
        sx_hits = len(re.findall(r"sx\.org proxy", tail))
        # This was briefly graded even when undated, on the theory that a
        # possible contract violation deserves a red light. That was wrong twice
        # over. These logs never rotate, so a historical burst stays inside the
        # tail forever: the check became permanently red, which trains an
        # operator to ignore red — the same failure as a false green, inverted.
        # Worse, it was reporting the opposite of the truth. Measured directly
        # after a data plane restart: zero sx.org lines in the new output, while
        # this check still counted 309 in the stale tail.
        if undated:
            add(
                "no_third_party_egress_in_use",
                True,
                {
                    **undatable,
                    "matches_in_tail": sx_hits,
                    "why": "ARCHITECTURE.md forbids sx.org egress in Fleet mode; "
                           "the startup guard does not cover a live reload",
                    "note": "historical matches cannot be dated and these logs "
                            "do not rotate; treat as unknown, not as live",
                },
                skipped=True,
            )
        else:
            add(
                "no_third_party_egress_in_use",
                sx_hits == 0,
                {
                    "sx_org_mentions": sx_hits,
                    "window_seconds": LOG_FRESH_SECONDS,
                    "why": "ARCHITECTURE.md forbids sx.org egress in Fleet mode; "
                           "the startup guard does not cover a live reload",
                },
            )
        # "All accounts exhausted — holding connection" goes to stdout, not the
        # error stream, so the parked state is invisible if only the error log is
        # consulted. That was how a fleet serving nothing still looked healthy.
        parked_source = (state_dir / "dataplane.log") if state_dir else None
        parked_raw = (
            _tail(parked_source, 32 * 1024)
            if parked_source and parked_source.is_file()
            else tail[-32 * 1024:]
        ) or ""
        # Same trap as the counts above, and it bit here too: after a restart
        # that stopped the parking entirely, the stale tail still said "parked".
        # A held request logs once per 60s for up to holdSeconds (6h by default),
        # so a past incident leaves thousands of lines that outlive it.
        parked_dated = _recent_lines(parked_raw, LOG_FRESH_SECONDS)
        if parked_dated is None:
            add(
                "dataplane_not_parked",
                True,
                {
                    "log": str(parked_source or dataplane_log),
                    "matches_in_tail": parked_raw.count("All accounts exhausted"),
                    **undatable,
                },
                skipped=True,
            )
        else:
            parked = "All accounts exhausted" in parked_dated
            add(
                "dataplane_not_parked",
                not parked,
                {
                    "log": str(parked_source or dataplane_log),
                    "window_seconds": LOG_FRESH_SECONDS,
                    "state": "holding connections with no eligible account"
                    if parked else "serving",
                },
            )
