from __future__ import annotations

import argparse
import json
import os
import re
import socket
import stat
import subprocess
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any, Optional, Sequence

from . import __version__
from .coordinator import serve_coordinator
from .control import (
    UNLIMITED_CONCURRENCY,
    WRITER_TTL_SECONDS,
    FleetControl,
    FleetError,
)
from .accounts import set_mode as set_account_mode
from .backup import KEEP as BACKUP_KEEP
from .backup import create as create_backup
from .backup import list_backups
from .backup import restore as restore_backup
from .db import FleetDB
from .doctor import doctor
from .hooks import read_stdin_payload, record_hook
from .launcher import (
    DEFAULT_COORDINATOR_PORT,
    coordinator_port,
    exec_wrapped,
    load_runtime,
)
from .observe import observe
from .proxy import serve
from .reconcile import reconcile_report
from .settings import (
    install_settings,
    proposed_settings,
    remove_settings,
)
from .shell import (
    install_shell,
    proposed_shell,
    remove_shell,
)


def default_db() -> str:
    if os.environ.get("FLEET_DB"):
        return os.environ["FLEET_DB"]
    runtime_path = Path(os.environ.get(
        "FLEET_RUNTIME_CONFIG",
        str(Path.home() / ".config" / "claude-fleet" / "runtime.json"),
    )).expanduser()
    try:
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        if runtime.get("db_path"):
            return str(runtime["db_path"])
    except (OSError, json.JSONDecodeError):
        pass
    return str(Path.cwd() / "work" / "claude-fleet.sqlite3")


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def timestamp(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    # ISO 8601 subset accepted by Python 3.9 without third-party packages.
    from datetime import datetime

    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).timestamp()


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=default_db(), help="SQLite state path")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="quotapool")
    root.add_argument("--version", action="version", version=__version__)
    add_common(root)
    commands = root.add_subparsers(dest="command", required=True)

    account = commands.add_parser("account")
    account_cmd = account.add_subparsers(dest="account_command", required=True)
    account_cmd.add_parser("list")
    # Every option defaults to None so an omitted flag PRESERVES the stored
    # value. With argparse defaults these were written unconditionally, so
    # `fleet account add <existing-id>` silently reset kind to owned and both
    # reserve floors to zero — removing the shared account's only protection.
    add = account_cmd.add_parser(
        "add",
        help="create an account, or update only the fields you pass",
    )
    add.add_argument("id")
    add.add_argument("--kind", choices=["owned", "shared"], default=None)
    add.add_argument("--priority", type=int, default=None)
    add.add_argument("--five-hour-floor", type=float, default=None)
    add.add_argument("--weekly-floor", type=float, default=None)
    add.add_argument(
        "--interactive-reserve", type=float, default=None,
        help="quota fraction held back from deferrable work (daemon, workflow, "
             "subagent-bulk) so a batch job cannot drain the tail of a window "
             "that an interactive session needs",
    )
    add.add_argument("--max-concurrency", type=int, default=None)
    add.add_argument("--allow", action="append", dest="allowed")
    add.add_argument(
        "--clear-allow",
        action="store_true",
        help="remove the workload allow-list (omitting --allow preserves it)",
    )
    quota = account_cmd.add_parser("quota")
    quota.add_argument("id")
    quota.add_argument("--five-hour-used", type=float)
    quota.add_argument("--five-hour-reset")
    quota.add_argument("--weekly-used", type=float)
    quota.add_argument("--weekly-reset")
    # The switch that decides anything. `enable`/`disable` below write only the
    # control plane, which is why an account could be switched off but not on.
    for verb, help_text in (
        ("pause", "暂停: 不接请求, 但继续刷新令牌与探测额度"),
        ("resume", "恢复接收请求"),
        ("retire", "退休: 不接请求, 也不再为它花任何东西 (凭据真的没了)"),
    ):
        item = account_cmd.add_parser(verb, help=help_text)
        item.add_argument("id")
    for name in ("drain", "enable", "disable", "exhaust"):
        item = account_cmd.add_parser(name)
        item.add_argument("id")

    execution = commands.add_parser("execution")
    execution_cmd = execution.add_subparsers(dest="execution_command", required=True)
    register = execution_cmd.add_parser("register")
    register.add_argument("session_id")
    register.add_argument("--id", dest="execution_id")
    register.add_argument("--class", dest="workload_class", default="interactive")
    register.add_argument("--parent")
    release = execution_cmd.add_parser("release")
    release.add_argument("session_id")
    release.add_argument("--status", default="completed")
    tree = execution_cmd.add_parser("tree")
    tree.add_argument("execution_id")

    writer = commands.add_parser("writer")
    writer_cmd = writer.add_subparsers(dest="writer_command", required=True)
    acquire = writer_cmd.add_parser("acquire")
    acquire.add_argument("session_id")
    acquire.add_argument("holder")
    acquire.add_argument("--ttl", type=int, default=WRITER_TTL_SECONDS)
    acquire.add_argument("--takeover", action="store_true")

    route = commands.add_parser("route")
    route_cmd = route.add_subparsers(dest="route_command", required=True)
    decide = route_cmd.add_parser("decide")
    decide.add_argument("session_id")
    decide.add_argument("holder")
    decide.add_argument("generation", type=int)
    decide.add_argument("--exclude", action="append", default=[])
    for name in ("pin", "prefer", "next"):
        item = route_cmd.add_parser(name)
        item.add_argument("session_id")
        item.add_argument("account", nargs="?")
    chain = route_cmd.add_parser("chain")
    chain.add_argument("session_id")
    chain.add_argument("accounts", nargs="*")

    policy = commands.add_parser("policy")
    policy_cmd = policy.add_subparsers(dest="policy_command", required=True)
    policy_set = policy_cmd.add_parser("set")
    policy_set.add_argument("workload_id")
    policy_set.add_argument("--pin")
    policy_set.add_argument("--prefer")
    policy_set.add_argument("--chain", action="append", default=[])
    policy_set.add_argument("--allow-account", action="append", default=[])
    policy_get = policy_cmd.add_parser("get")
    policy_get.add_argument("workload_id")

    status = commands.add_parser("status")
    status.add_argument("--events", type=int, default=20)

    commands.add_parser("observe")
    capacity = commands.add_parser(
        "capacity",
        help="is now a good time to start batch work? (for daemon schedulers)",
    )
    capacity.add_argument(
        "--for", dest="capacity_workload", default="daemon",
        help="workload class the caller intends to run (default: daemon)",
    )
    why = commands.add_parser(
        "why",
        help="explain how a session's account was chosen, and why a request "
             "would be parked",
    )
    why.add_argument("session_id")
    why.add_argument("--decisions", type=int, default=5)
    backup_cmd = commands.add_parser(
        "backup", help="快照状态数据库（对正在服务的舰队安全）")
    backup_cmd.add_argument("--reason", default="manual")
    backup_cmd.add_argument("--keep", type=int, default=BACKUP_KEEP)
    commands.add_parser("backups", help="列出快照，最新在前")
    restore_cmd = commands.add_parser(
        "restore", help="把某个快照放回去（当前状态会先被快照）")
    restore_cmd.add_argument("source", nargs="?", help="默认用最新的一份")
    restore_cmd.add_argument("--force", action="store_true",
                             help="协调器仍在运行时也执行（它开着这个库）")

    doctor_cmd = commands.add_parser("doctor")
    doctor_cmd.add_argument(
        "--db",
        dest="doctor_db",
        default=None,
        help="state database to health-check (defaults to runtime.json db_path)",
    )

    maintenance = commands.add_parser("maintenance")
    maintenance_cmd = maintenance.add_subparsers(dest="maintenance_command")
    sweep_cmd = maintenance_cmd.add_parser("sweep")
    sweep_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="report what retention would reclaim without deleting",
    )
    sweep_cmd.add_argument("--event-days", type=float, default=7.0)
    sweep_cmd.add_argument("--terminal-days", type=float, default=3.0)
    sweep_cmd.add_argument(
        "--stale-active-hours",
        type=float,
        default=24.0,
        help="age after which an active execution with an expired writer lease "
             "is treated as a phantom and abandoned",
    )
    maintenance_cmd.add_parser("vacuum")

    log_sink = commands.add_parser(
        "log-sink",
        help="read stdin, prefix each line with a timestamp, write to a "
             "size-capped rotating file",
    )
    log_sink.add_argument("--path", required=True)
    log_sink.add_argument("--max-bytes", type=int, default=8 * 1024 * 1024)
    log_sink.add_argument("--keep", type=int, default=3)
    log_sink.add_argument(
        "--stdout-path",
        default=None,
        help="keep stdout in its own file instead of merging into --path. "
             "doctor reads the stdout log to tell whether the fleet is serving, "
             "so merging the streams would blind that check.",
    )
    log_sink.add_argument(
        "target_args",
        # REMAINDER, not "*": argparse still tries to interpret option-looking
        # tokens after a bare "--" when the positional is "*". The units as
        # generated wrap a flagless path so either works, but this stops the day
        # someone adds a flag to the wrapped command from becoming a puzzle.
        nargs=argparse.REMAINDER,
        metavar="-- COMMAND [ARGS...]",
        help="run COMMAND and exit with its status, logging its output. "
             "Without it, stdin is logged instead — which must NOT be used in a "
             "unit's ExecStart pipeline, since a shell reports the pipeline's "
             "last exit status and Restart=on-failure would never fire.",
    )

    login = commands.add_parser("login")
    login.add_argument("name")
    setup_token = commands.add_parser("import-setup-token")
    setup_token.add_argument("name")
    setup_token.add_argument("--from-file", required=True)
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("--stale-after", type=int, default=300)

    hook = commands.add_parser("hook")
    hook.add_argument("event_name")

    coordinator = commands.add_parser("coordinator")
    coordinator.add_argument(
        "--host", default=os.environ.get("FLEET_COORDINATOR_HOST", "127.0.0.1")
    )

    settings = commands.add_parser("settings")
    settings_cmd = settings.add_subparsers(dest="settings_command", required=True)
    for name in ("propose", "install", "remove"):
        item = settings_cmd.add_parser(name)
        item.add_argument(
            "--settings",
            default=str(Path.home() / ".claude" / "settings.json"),
        )
        item.add_argument("--wrapper", required=True)
        item.add_argument("--fleet-binary", required=True)
        item.add_argument("--state-db", required=True)
    shell = commands.add_parser("shell")
    shell_cmd = shell.add_subparsers(dest="shell_command", required=True)
    for name in ("propose", "install", "remove"):
        item = shell_cmd.add_parser(name)
        item.add_argument(
            "--file",
            default=str(Path.home() / ".zshrc"),
        )
        item.add_argument(
            "--shim-dir",
            default=str(Path.home() / ".local" / "share" / "claude-fleet" / "shims"),
        )
    terminal = commands.add_parser("terminal")
    terminal_cmd = terminal.add_subparsers(
        dest="terminal_command",
        required=True,
    )
    terminal_cmd.add_parser("list")
    terminal_cmd.add_parser("status")
    terminal_new = terminal_cmd.add_parser("new")
    terminal_new.add_argument("name", nargs="?")
    terminal_new.add_argument("--attach", action="store_true")
    terminal_attach = terminal_cmd.add_parser("attach")
    terminal_attach.add_argument("name")
    terminal_close = terminal_cmd.add_parser("close")
    terminal_close.add_argument("name")
    terminal_close.add_argument("--force", action="store_true")
    terminal_tab = terminal_cmd.add_parser("tab")
    terminal_tab.add_argument("workspace")
    terminal_tab.add_argument("name", nargs="?")
    terminal_close_tab = terminal_cmd.add_parser("close-tab")
    terminal_close_tab.add_argument("workspace")
    terminal_close_tab.add_argument("name")
    terminal_close_tab.add_argument("--force", action="store_true")
    for name in ("account", "pin"):
        item = terminal_cmd.add_parser(name)
        item.add_argument("account")
        item.add_argument("name", nargs="?")
        item.add_argument("--all", action="store_true")
    terminal_auto = terminal_cmd.add_parser("auto")
    terminal_auto.add_argument("name", nargs="?")
    terminal_auto.add_argument("--all", action="store_true")
    terminal_default = terminal_cmd.add_parser("default")
    terminal_default.add_argument("account")
    coordinator.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("FLEET_COORDINATOR_PORT",
                                   str(DEFAULT_COORDINATOR_PORT))),
    )

    proxy = commands.add_parser("proxy")
    proxy.add_argument(
        "--upstream-template",
        default=os.environ.get("FLEET_UPSTREAM_TEMPLATE"),
        help="Example: http://127.0.0.1:3456/tc-acct/{account}",
    )
    proxy.add_argument("--host", default=os.environ.get("FLEET_HOST", "127.0.0.1"))
    proxy.add_argument("--port", type=int, default=int(os.environ.get("FLEET_PORT", "17890")))

    run = commands.add_parser("run")
    run.add_argument("--class", dest="workload_class")
    run.add_argument("--workload-id")
    run.add_argument("--execution-id")
    run.add_argument("--parent")
    run.add_argument("--launch-id", help=argparse.SUPPRESS)
    run.add_argument("claude_args", nargs=argparse.REMAINDER)

    wrapper = commands.add_parser("wrapper")
    wrapper.add_argument("target_args", nargs=argparse.REMAINDER)
    return root


def merge_custom_headers(existing: str, additions: Sequence[str]) -> str:
    values = [value for value in existing.splitlines() if value.strip()]
    names = {value.split(":", 1)[0].strip().lower() for value in values if ":" in value}
    for addition in additions:
        name = addition.split(":", 1)[0].strip().lower()
        if name not in names:
            values.append(addition)
    return "\n".join(values)


def run_claude(args: argparse.Namespace) -> None:
    claude_args = list(args.claude_args)
    if claude_args and claude_args[0] == "--":
        claude_args = claude_args[1:]
    workload_class, parent_execution_id = infer_launch_context(
        claude_args,
        args.db,
        requested_class=args.workload_class,
        requested_parent=args.parent,
    )
    inherited_execution = os.environ.get("FLEET_EXECUTION_ID")
    if parent_execution_id is None and inherited_execution:
        parent_execution_id = inherited_execution
        if args.workload_class is None and workload_class == "interactive":
            workload_class = "subagent"
    launch_id = (
        args.launch_id
        or f"{socket.gethostname()}:{uuid.uuid4().hex}"
    )
    session_id = explicit_session_id(claude_args)
    resolved_execution_id = args.execution_id
    if session_id:
        control = FleetControl(FleetDB(args.db))
        execution = control.ensure_execution(
            session_id,
            execution_hint=args.execution_id,
            workload_class=workload_class,
            workload_id=args.workload_id or os.environ.get("FLEET_WORKLOAD_ID"),
            parent_hint=parent_execution_id,
        )
        resolved_execution_id = execution["id"]
        # Reserve ownership before Claude starts. Without this preflight, a
        # second `--resume` process can append a user record to the transcript
        # before its first HTTP request reaches coordinator fencing.
        control.acquire_writer(session_id, launch_id)
    exec_wrapped(
        ["claude"] + claude_args,
        workload_class=workload_class,
        workload_id=args.workload_id,
        execution_id=resolved_execution_id,
        parent_execution_id=parent_execution_id,
        session_id=session_id,
        launch_id=launch_id,
        new_launch=True,
    )


_SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def explicit_session_id(claude_args: Sequence[str]) -> Optional[str]:
    """Return an explicit existing session that must be fenced pre-launch.

    A fork resumes parent context but creates a new Claude session, so acquiring
    the parent writer would incorrectly block a legitimate branch.

    Only a real session UUID is returned. `--resume` also accepts a session's
    display name — `claude --resume 空港` — and treating that name as a session
    id caused a reproducible failure:

      1. Fleet registers an execution whose session_id is the NAME and takes a
         24-hour writer lease on it.
      2. Claude resumes the actual session, which has a UUID. The two never
         meet, so the lease guards nothing.
      3. Every later `--resume <same name>` hits LeaseConflict against that
         orphan, and the session appears broken until the lease expires.

    Reproduced end to end, and one such row (session_id='空港', writer still
    live) was found in the live database.

    Skipping the preflight for a named resume is the correct trade. The
    preflight is an optimisation — it fences a duplicate `--resume` before
    Claude can append to the transcript — but the coordinator still fences on
    the first model request, using the session id Claude itself reports. So a
    named resume is fenced a moment later rather than not at all, and nothing
    is fenced under a key that can never match.
    """
    values = list(claude_args)
    if "--fork-session" in values:
        return None
    for flag in ("--session-id", "--resume", "-r"):
        try:
            index = values.index(flag)
        except ValueError:
            continue
        if index + 1 >= len(values):
            continue
        value = values[index + 1].strip()
        if not value or value.startswith("-"):
            continue
        if not _SESSION_ID_RE.match(value):
            # A display name, not a session id. Let Claude resolve it and let
            # the coordinator fence on the id it reports.
            return None
        return value
    return None


def infer_launch_context(
    claude_args: Sequence[str],
    db_path: str,
    *,
    requested_class: Optional[str] = None,
    requested_parent: Optional[str] = None,
) -> tuple:
    """Infer only contexts that Claude exposes unambiguously at launch.

    In particular, `--fork-session --resume <uuid>` is observable before the
    child session is created.  We attach the new Fleet execution to the
    registered parent execution instead of leaving two flat transcript
    versions.  Unknown or picker-based resumes stay unmodified.
    """
    workload_class = requested_class or "interactive"
    parent = requested_parent
    values = list(claude_args)
    if requested_class is None and (
        (values and values[0] == "agents")
        or "--bg" in values
        or "--background" in values
    ):
        workload_class = "agent-view"
    if "--fork-session" not in values:
        return workload_class, parent
    if requested_class is None:
        workload_class = "fork"
    if parent:
        return workload_class, parent
    resume_session = None
    for flag in ("--resume", "-r"):
        try:
            index = values.index(flag)
        except ValueError:
            continue
        if index + 1 < len(values) and not values[index + 1].startswith("-"):
            resume_session = values[index + 1]
            break
    if not resume_session:
        return workload_class, None
    try:
        db = FleetDB(db_path)
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id FROM executions WHERE session_id=?",
                (resume_session,),
            ).fetchone()
        if row is not None:
            parent = str(row["id"])
    except Exception:
        # Launch remains usable if the read-only lineage lookup cannot run; the
        # coordinator will still enforce writer/route safety for the new
        # session. Reconciler can flag missing parentage for explicit repair.
        parent = None
    return workload_class, parent


def run_teamclaude_cli(arguments: Sequence[str]) -> None:
    from .launcher import load_runtime

    runtime = load_runtime()
    env = os.environ.copy()
    env["TEAMCLAUDE_CONFIG"] = str(runtime["teamclaude_config"])
    env["TEAMCLAUDE_DISABLE_AUTOUPDATE"] = "1"
    outbound_proxy = str(runtime.get("outbound_proxy") or "").strip()
    if outbound_proxy:
        env["HTTPS_PROXY"] = outbound_proxy
        env["HTTP_PROXY"] = outbound_proxy
        env["ALL_PROXY"] = outbound_proxy
    no_proxy = str(runtime.get("no_proxy") or "localhost,127.0.0.1,::1").strip()
    env["NO_PROXY"] = no_proxy
    node = str(runtime["node_binary"])
    entry = str(Path(str(runtime["prefix"])) / "vendor" / "teamclaude" / "src" / "index.js")
    node_arguments = [node]
    try:
        help_text = subprocess.run(
            [node, "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        if "--use-env-proxy" in help_text:
            node_arguments.append("--use-env-proxy")
    except (OSError, subprocess.SubprocessError):
        pass
    os.execvpe(
        node,
        node_arguments + [entry] + list(arguments),
        env,
    )


def run_login(name: str) -> None:
    run_teamclaude_cli(["login", "--oauth", "--name", name])


def run_import_setup_token(name: str, source: str) -> None:
    path = Path(source).expanduser()
    try:
        info = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"cannot inspect setup-token file: {exc}") from exc
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("setup-token path must be a regular non-symlink file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError(
            "setup-token file must not be readable by group or other users"
        )
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise RuntimeError("setup-token file must be owned by the current user")
    run_teamclaude_cli([
        "import",
        "--setup-token-file",
        str(path),
        "--name",
        name,
    ])


def _runtime_account_items(runtime_path: Optional[str] = None) -> Any:
    from .launcher import load_runtime

    runtime = load_runtime(runtime_path)
    path = Path(str(runtime["teamclaude_config"]))
    value = json.loads(path.read_text(encoding="utf-8"))
    return [
        item for item in value.get("accounts", [])
        if isinstance(item, dict) and item.get("type") == "oauth"
    ]


def runtime_accounts(runtime_path: Optional[str] = None) -> Any:
    return [
        {
            "id": item.get("accountUuid") or item.get("name"),
            "name": item.get("name"),
            "type": item.get("type"),
            "disabled": bool(item.get("disabled")),
            "identity_verified": bool(item.get("accountUuid")),
        }
        for item in _runtime_account_items(runtime_path)
    ]


def sync_runtime_account_registry(
    control: FleetControl, runtime_path: Optional[str] = None
) -> Any:
    """Seed coordinator policy state from the credential registry.

    The data plane also syncs live quota on every route decision. This startup
    seed exists so manual `pin`, `next`, and `chain` commands can name an
    account before the first model request without bypassing the coordinator.
    No credential value is copied into Fleet's SQLite database.
    """
    synced = []
    for item in _runtime_account_items(runtime_path):
        account_id = str(item.get("accountUuid") or item.get("name") or "").strip()
        if not account_id:
            continue
        synced.append(control.sync_account_runtime(
            account_id,
            name=str(item.get("name") or account_id),
            disabled=bool(item.get("disabled")),
            metadata={
                "source": item.get("source"),
                "identity_status": item.get("identityStatus"),
                "account_uuid": item.get("accountUuid"),
                "org_uuid": item.get("orgUuid"),
                "startup_seed": True,
            },
        ))
    return synced


def launch_failed(detail: str) -> int:
    """Report a failed launch with a way out, because `claude` IS this command.

    `claude` is a symlink to `claude-fleet-shell`, which is `fleet run`. So when
    this fails the owner does not fall back to a slower Claude — he has no
    Claude, and the session he would use to diagnose the fault is the one that
    just refused to start. That is the loop he named: 改了就全坏, 坏了又因为坏了
    没法修.

    The fault itself is printed above this, unabridged. This only adds the exits
    that exist, because a stack trace is not an answer to "what do I do now".
    """
    dim, bold, reset = "\033[2m", "\033[1m", "\033[0m"
    print(
        "\n%sFleet 无法启动：%s%s\n\n"
        "  %sclaude-direct%s        选一个账号直连，绕过 Fleet\n"
        "  %sfleet-rollback now%s   回到上一个已知良好版本\n"
        "  %sfleet doctor%s         看具体哪里坏了\n"
        % (bold, detail, reset,
           bold, reset, bold, reset, bold, reset),
        file=sys.stderr,
    )
    # Deliberately no automatic fallback to a direct session. Falling back would
    # have to pick an account on the owner's behalf, and it would leave him
    # running unscheduled — no reserve for his desktop client, no rotation, and
    # an error instead of a wait when the window runs out. Better to cost him
    # one command than to silently move him somewhere he did not ask to be.
    return 78


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "observe":
        emit(observe())
        return 0
    if args.command == "backup":
        emit(create_backup(args.db, reason=args.reason, keep=args.keep))
        return 0
    if args.command == "backups":
        emit(list_backups(args.db))
        return 0
    if args.command == "restore":
        try:
            runtime_port = coordinator_port(load_runtime())
        except Exception:                                     # noqa: BLE001
            runtime_port = DEFAULT_COORDINATOR_PORT
        try:
            emit(restore_backup(args.db, args.source,
                                coordinator_port=runtime_port,
                                force=args.force))
        except (RuntimeError, FileNotFoundError) as exc:
            print("claude-fleet: %s" % exc, file=sys.stderr)
            return 78
        return 0
    if args.command == "doctor":
        result = doctor(db_path=args.doctor_db)
        emit(result)
        return 0 if result["ok"] else 1
    if args.command == "log-sink":
        from .logsink import pump, run

        target = list(args.target_args or [])
        if target and target[0] == "--":
            target = target[1:]
        if target:
            return run(
                args.path, target, max_bytes=args.max_bytes, keep=args.keep,
                stdout_path=args.stdout_path,
            )
        return pump(args.path, max_bytes=args.max_bytes, keep=args.keep)
    if args.command == "capacity":
        from .capacity import report_capacity

        result = report_capacity(FleetDB(args.db), args.capacity_workload)
        emit(result)
        return 0 if result["can_run_now"] else 1
    if args.command == "why":
        from .why import explain

        emit(explain(FleetDB(args.db), args.session_id, limit=args.decisions))
        return 0
    if args.command == "maintenance":
        from . import maintenance as maintenance_module

        db = FleetDB(args.db)
        if args.maintenance_command == "vacuum":
            emit(maintenance_module.vacuum(db))
            return 0
        emit(maintenance_module.sweep(
            db,
            event_retention=args.event_days * 86400,
            hook_retention=args.event_days * 86400,
            terminal_retention=args.terminal_days * 86400,
            stale_active_grace=args.stale_active_hours * 3600,
            dry_run=args.dry_run,
        ))
        return 0
    if args.command == "login":
        try:
            run_login(args.name)
            return 0
        except RuntimeError as exc:
            print(f"claude-fleet: {exc}", file=sys.stderr)
            return 78
    if args.command == "import-setup-token":
        try:
            run_import_setup_token(args.name, args.from_file)
            return 0
        except RuntimeError as exc:
            print(f"claude-fleet: {exc}", file=sys.stderr)
            return 78
    if args.command == "run":
        try:
            run_claude(args)
            return 0
        except RuntimeError as exc:
            return launch_failed(str(exc))
        except KeyboardInterrupt:
            raise
        except Exception as exc:                              # noqa: BLE001
            # Only this one command catches broadly. Everywhere else a traceback
            # is a fine answer, because the operator still has a working Claude
            # to read it with; here the traceback IS the outage. The fault is
            # printed in full first, so nothing is hidden — only accompanied.
            traceback.print_exc()
            return launch_failed("%s: %s" % (type(exc).__name__, exc))
    if args.command == "wrapper":
        try:
            exec_wrapped(args.target_args)
            return 0
        except RuntimeError as exc:
            print(f"claude-fleet: {exc}", file=sys.stderr)
            return 78

    db = FleetDB(args.db)
    control = FleetControl(db)
    try:
        if args.command == "account":
            if args.account_command == "list":
                emit(runtime_accounts())
            elif args.account_command == "add":
                emit(control.upsert_account(
                    args.id,
                    kind=args.kind,
                    priority=args.priority,
                    five_hour_floor=args.five_hour_floor,
                    weekly_floor=args.weekly_floor,
                    interactive_reserve=args.interactive_reserve,
                    max_concurrency=args.max_concurrency,
                    allowed_workloads=args.allowed,
                    clear_allowed_workloads=args.clear_allow,
                ))
            elif args.account_command == "quota":
                emit(control.report_quota(
                    args.id,
                    five_hour_used=args.five_hour_used,
                    five_hour_reset_at=timestamp(args.five_hour_reset),
                    weekly_used=args.weekly_used,
                    weekly_reset_at=timestamp(args.weekly_reset),
                ))
            elif args.account_command in ("pause", "resume", "retire"):
                mode = {"pause": "paused", "resume": "active",
                        "retire": "retired"}[args.account_command]
                try:
                    emit(set_account_mode(db, args.id, mode))
                except RuntimeError as exc:
                    print("claude-fleet: %s" % exc, file=sys.stderr)
                    return 78
            else:
                # Control plane only. Kept because scripts use them, but they
                # cannot re-enable an account on their own — the data plane goes
                # on reporting `disabled` and the coordinator re-excludes it.
                # `pause`/`resume`/`retire` are the ones that decide.
                state = {
                    "drain": "draining",
                    "enable": "healthy",
                    "disable": "disabled",
                    "exhaust": "exhausted",
                }[args.account_command]
                emit(control.set_account_state(args.id, state))
        elif args.command == "execution":
            if args.execution_command == "register":
                emit(control.register_execution(
                    args.session_id,
                    execution_id=args.execution_id,
                    workload_class=args.workload_class,
                    parent_id=args.parent,
                ))
            elif args.execution_command == "release":
                control.release_execution(args.session_id, args.status)
                emit({"ok": True})
            else:
                emit(control.tree(args.execution_id))
        elif args.command == "writer":
            generation = control.acquire_writer(
                args.session_id,
                args.holder,
                ttl_seconds=args.ttl,
                takeover=args.takeover,
            )
            emit({"generation": generation})
        elif args.command == "route":
            sync_runtime_account_registry(control)
            if args.route_command == "decide":
                emit(control.decide_route(
                    args.session_id,
                    args.holder,
                    args.generation,
                    exclude=args.exclude,
                ))
            elif args.route_command == "chain":
                emit(control.set_route_policy(
                    args.session_id, "chain", args.accounts or None
                ))
            else:
                emit(control.set_route_policy(
                    args.session_id, args.route_command, args.account
                ))
        elif args.command == "status":
            emit({
                "accounts": db.accounts(),
                "executions": db.executions(),
                "leases": db.leases(),
                "processes": db.processes(),
                "execution_nodes": db.execution_nodes(),
                "workload_policies": db.workload_policies(),
                "events": db.recent_events(args.events),
            })
        elif args.command == "policy":
            if args.policy_command == "set":
                emit(control.set_workload_policy(
                    args.workload_id,
                    hard_pin=args.pin,
                    preference=args.prefer,
                    route_chain=args.chain or None,
                    allowed_accounts=args.allow_account or None,
                ))
            else:
                emit(control.get_workload_policy(args.workload_id))
        elif args.command == "hook":
            record_hook(
                db,
                args.event_name,
                read_stdin_payload(),
            )
        elif args.command == "reconcile":
            emit(reconcile_report(db, args.stale_after))
        elif args.command == "coordinator":
            sync_runtime_account_registry(control)
            server = serve_coordinator(
                args.db,
                args.host,
                args.port,
                token=os.environ.get("FLEET_COORDINATOR_TOKEN"),
            )
            print(
                f"claude-fleet coordinator listening on "
                f"http://{args.host}:{args.port}"
            )
            server.serve_forever()
        elif args.command == "settings":
            common = {
                "wrapper_path": args.wrapper,
                "fleet_binary": args.fleet_binary,
                "db_path": args.state_db,
            }
            if args.settings_command == "install":
                emit(install_settings(args.settings, **common))
            elif args.settings_command == "remove":
                emit(remove_settings(args.settings, **common))
            else:
                path = Path(args.settings).expanduser()
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    existing = {}
                emit(proposed_settings(existing, **common))
        elif args.command == "shell":
            path = Path(args.file).expanduser()
            if args.shell_command == "install":
                emit(install_shell(str(path), args.shim_dir))
            elif args.shell_command == "remove":
                emit(remove_shell(str(path), args.shim_dir))
            else:
                try:
                    existing = path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    existing = ""
                emit({"content": proposed_shell(existing, args.shim_dir)})
        elif args.command == "terminal":
            from .terminal import (
                attach_terminal,
                clear_terminal_route,
                close_tab,
                close_terminal,
                create_tab,
                create_terminal,
                current_tmux_session,
                list_tmux_sessions,
                set_terminal_route,
                set_default_account,
                terminal_status,
            )

            if args.terminal_command in {"list", "status"}:
                emit(terminal_status(db))
            elif args.terminal_command == "default":
                emit(set_default_account(args.account))
            elif args.terminal_command == "new":
                result = create_terminal(args.name)
                if args.attach:
                    emit(result)
                    attach_terminal(result["terminal"])
                else:
                    emit(result)
            elif args.terminal_command == "attach":
                attach_terminal(args.name)
            elif args.terminal_command == "close":
                emit(close_terminal(args.name, force=args.force))
            elif args.terminal_command == "tab":
                emit(create_tab(args.workspace, args.name))
            elif args.terminal_command == "close-tab":
                emit(close_tab(
                    args.workspace,
                    args.name,
                    force=args.force,
                ))
            else:
                sync_runtime_account_registry(control)
                if args.all:
                    names = [item.name for item in list_tmux_sessions()]
                else:
                    selected = args.name or current_tmux_session()
                    if not selected:
                        raise FleetError(
                            "run inside tmux, specify a terminal name, or use --all"
                        )
                    names = [selected]
                if args.terminal_command == "auto":
                    emit(clear_terminal_route(
                        db,
                        control,
                        terminal_names=names,
                    ))
                else:
                    emit(set_terminal_route(
                        db,
                        control,
                        account=args.account,
                        terminal_names=names,
                        hard_pin=args.terminal_command == "pin",
                    ))
        elif args.command == "proxy":
            if not args.upstream_template:
                raise FleetError("--upstream-template is required")
            server = serve(
                args.db,
                args.upstream_template,
                args.host,
                args.port,
                upstream_token=os.environ.get("FLEET_UPSTREAM_TOKEN"),
                client_token=os.environ.get("FLEET_CLIENT_TOKEN"),
            )
            print(f"claude-fleet proxy listening on http://{args.host}:{args.port}")
            server.serve_forever()
        return 0
    except FleetError as exc:
        print(json.dumps({
            "error": exc.code,
            "message": str(exc),
        }, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
