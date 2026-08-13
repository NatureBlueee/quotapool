from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .control import FleetControl, FleetError, NotFound
from .db import FleetDB


ACCOUNT_ALIASES = {
    "1": "max-a",
    "2": "max-b",
    "3": "max-c",
    "4": "max-d",
    "account1": "max-a",
    "account2": "max-b",
    "account3": "max-c",
    "account4": "max-d",
    "max-a": "max-a",
    "max-b": "max-b",
    "max-c": "max-c",
    "max-d": "max-d",
}
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
CLAUDE_COMMANDS = {"claude", "claude-real"}
DEFAULT_ACCOUNT_FILE = Path(
    "/var/lib/harness/claude-account/default-account"
)


class TerminalError(FleetError):
    code = "terminal_error"


@dataclass(frozen=True)
class TmuxSession:
    name: str
    windows: int
    attached: bool
    pane_commands: tuple[str, ...]
    window_names: tuple[str, ...] = ()


def _run_tmux(
    arguments: Sequence[str],
    *,
    timeout: float = 10,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["tmux", *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TerminalError(f"tmux command failed: {exc}") from exc


def normalize_account(value: str) -> str:
    alias = ACCOUNT_ALIASES.get(value.strip().lower())
    if not alias:
        raise TerminalError(
            "account must be 1-4, account1-account4, or max-a-max-d"
        )
    return alias


def legacy_account_name(alias: str) -> str:
    normalized = normalize_account(alias)
    return f"account{'abcd'.index(normalized[-1]) + 1}"


def read_default_account(
    path: Path = DEFAULT_ACCOUNT_FILE,
) -> Optional[str]:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return normalize_account(value)
    except TerminalError:
        return None


def set_default_account(
    account: str,
    *,
    path: Path = DEFAULT_ACCOUNT_FILE,
) -> Dict[str, Any]:
    alias = normalize_account(account)
    previous = read_default_account(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        f"{legacy_account_name(alias)}\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    return {
        "action": "default_account_updated",
        "previous": previous,
        "account": alias,
        "running_processes_restarted": False,
    }


def workload_id(session_name: str) -> str:
    validate_session_name(session_name)
    return f"hsm:{session_name}"


def validate_session_name(value: str) -> str:
    if not SESSION_NAME_RE.fullmatch(value):
        raise TerminalError(
            "terminal name must contain only letters, numbers, dot, dash, or underscore"
        )
    return value


def list_tmux_sessions() -> List[TmuxSession]:
    result = _run_tmux(
        [
            "list-sessions",
            "-F",
            "#{session_name}\t#{session_windows}\t#{session_attached}",
        ]
    )
    if result.returncode != 0:
        if "no server running" in result.stderr.lower():
            return []
        raise TerminalError(result.stderr.strip() or "cannot list tmux sessions")
    sessions: List[TmuxSession] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        name, windows, attached = parts
        panes = _run_tmux(
            ["list-panes", "-s", "-t", name, "-F", "#{pane_current_command}"]
        )
        if panes.returncode != 0:
            raise TerminalError(
                panes.stderr.strip() or f"cannot inspect tmux session {name}"
            )
        windows_result = _run_tmux(
            ["list-windows", "-t", name, "-F", "#{window_name}"]
        )
        if windows_result.returncode != 0:
            raise TerminalError(
                windows_result.stderr.strip()
                or f"cannot inspect windows in workspace {name}"
            )
        sessions.append(
            TmuxSession(
                name=name,
                windows=int(windows),
                attached=attached == "1",
                pane_commands=tuple(
                    item.strip()
                    for item in panes.stdout.splitlines()
                    if item.strip()
                ),
                window_names=tuple(
                    item.strip()
                    for item in windows_result.stdout.splitlines()
                    if item.strip()
                ),
            )
        )
    return sorted(sessions, key=lambda item: item.name)


def current_tmux_session() -> Optional[str]:
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    result = _run_tmux(
        ["display-message", "-p", "-t", pane, "#{session_name}"]
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def next_window_name(sessions: Iterable[TmuxSession]) -> str:
    occupied = {item.name for item in sessions}
    for number in range(1, 100):
        candidate = f"w{number}"
        if candidate not in occupied:
            return candidate
    raise TerminalError("all automatic terminal names w1-w99 are occupied")


def next_tab_name(names: Iterable[str]) -> str:
    occupied = set(names)
    for number in range(1, 100):
        candidate = f"tab{number}"
        if candidate not in occupied:
            return candidate
    raise TerminalError("all automatic tab names tab1-tab99 are occupied")


def create_terminal(
    name: Optional[str] = None,
    *,
    workdir: str = "/home/nature/个人项目/Harness",
    startup: str = "/home/nature/.local/bin/hsm-session",
) -> Dict[str, Any]:
    sessions = list_tmux_sessions()
    target = validate_session_name(name) if name else next_window_name(sessions)
    if any(item.name == target for item in sessions):
        raise TerminalError(f"terminal {target!r} already exists")
    command = shlex.join([startup, "auto"])
    result = _run_tmux(
        ["new-session", "-d", "-s", target, "-c", workdir, command]
    )
    if result.returncode != 0:
        raise TerminalError(result.stderr.strip() or f"cannot create {target}")
    return {
        "action": "terminal_created",
        "terminal": target,
        "workload_id": workload_id(target),
    }


def create_tab(
    workspace: str,
    name: Optional[str] = None,
    *,
    workdir: str = "/home/nature/个人项目/Harness",
    startup: str = "/home/nature/.local/bin/hsm-session",
) -> Dict[str, Any]:
    target_workspace = validate_session_name(workspace)
    sessions = {
        item.name: item
        for item in list_tmux_sessions()
    }
    session = sessions.get(target_workspace)
    if session is None:
        raise TerminalError(f"workspace {target_workspace!r} does not exist")
    target_tab = (
        validate_session_name(name)
        if name
        else next_tab_name(session.window_names)
    )
    if target_tab in session.window_names:
        raise TerminalError(
            f"tab {target_tab!r} already exists in {target_workspace!r}"
        )
    command = shlex.join([startup, "auto"])
    result = _run_tmux([
        "new-window",
        "-d",
        "-t",
        target_workspace,
        "-n",
        target_tab,
        "-c",
        workdir,
        command,
    ])
    if result.returncode != 0:
        raise TerminalError(
            result.stderr.strip()
            or f"cannot create tab {target_tab!r} in {target_workspace!r}"
        )
    return {
        "action": "tab_created",
        "workspace": target_workspace,
        "tab": target_tab,
        "workload_id": workload_id(target_workspace),
    }


def attach_terminal(name: str) -> None:
    target = validate_session_name(name)
    known = {item.name for item in list_tmux_sessions()}
    if target not in known:
        raise TerminalError(f"terminal {target!r} does not exist")
    if current_tmux_session():
        command = ["switch-client", "-t", target]
    else:
        command = ["attach-session", "-t", target]
    try:
        os.execvp("tmux", ["tmux", *command])
    except OSError as exc:
        raise TerminalError(f"cannot attach terminal {target}: {exc}") from exc


def close_terminal(name: str, *, force: bool = False) -> Dict[str, Any]:
    target = validate_session_name(name)
    matches = [item for item in list_tmux_sessions() if item.name == target]
    if not matches:
        raise TerminalError(f"terminal {target!r} does not exist")
    session = matches[0]
    active_claude = [
        command
        for command in session.pane_commands
        if command in CLAUDE_COMMANDS or re.fullmatch(r"\d+\.\d+\.\d+", command)
    ]
    if active_claude and not force:
        raise TerminalError(
            f"terminal {target!r} still runs Claude; use --force only when "
            "you intend to stop that task completely"
        )
    result = _run_tmux(["kill-session", "-t", target])
    if result.returncode != 0:
        raise TerminalError(result.stderr.strip() or f"cannot close {target}")
    return {
        "action": "terminal_closed",
        "terminal": target,
        "stopped_active_claude": bool(active_claude),
    }


def close_tab(
    workspace: str,
    tab: str,
    *,
    force: bool = False,
) -> Dict[str, Any]:
    target_workspace = validate_session_name(workspace)
    target_tab = validate_session_name(tab)
    sessions = {
        item.name: item
        for item in list_tmux_sessions()
    }
    session = sessions.get(target_workspace)
    if session is None:
        raise TerminalError(f"workspace {target_workspace!r} does not exist")
    if target_tab not in session.window_names:
        raise TerminalError(
            f"tab {target_tab!r} does not exist in {target_workspace!r}"
        )
    panes = _run_tmux([
        "list-panes",
        "-t",
        f"{target_workspace}:{target_tab}",
        "-F",
        "#{pane_current_command}",
    ])
    if panes.returncode != 0:
        raise TerminalError(
            panes.stderr.strip()
            or f"cannot inspect tab {target_workspace}:{target_tab}"
        )
    active_claude = [
        command.strip()
        for command in panes.stdout.splitlines()
        if (
            command.strip() in CLAUDE_COMMANDS
            or re.fullmatch(r"\d+\.\d+\.\d+", command.strip())
        )
    ]
    if active_claude and not force:
        raise TerminalError(
            f"tab {target_workspace}:{target_tab} still runs Claude; "
            "use --force only when you intend to stop that task completely"
        )
    result = _run_tmux([
        "kill-window",
        "-t",
        f"{target_workspace}:{target_tab}",
    ])
    if result.returncode != 0:
        raise TerminalError(
            result.stderr.strip()
            or f"cannot close tab {target_workspace}:{target_tab}"
        )
    return {
        "action": "tab_closed",
        "workspace": target_workspace,
        "tab": target_tab,
        "stopped_active_claude": bool(active_claude),
    }


def _execution_sessions_for_workloads(
    db: FleetDB,
    workload_ids: set[str],
) -> List[str]:
    return [
        str(item["session_id"])
        for item in db.executions()
        if item.get("workload_id") in workload_ids
        and item.get("status") in {"active", "parked"}
    ]


def set_terminal_route(
    db: FleetDB,
    control: FleetControl,
    *,
    account: str,
    terminal_names: Sequence[str],
    hard_pin: bool = False,
) -> Dict[str, Any]:
    alias = normalize_account(account)
    try:
        control.get_account(alias)
    except NotFound as exc:
        raise TerminalError(
            f"{alias} is not imported into Claude Fleet"
        ) from exc
    names = [validate_session_name(item) for item in terminal_names]
    ids = {workload_id(item) for item in names}
    for identity in sorted(ids):
        control.set_workload_policy(
            identity,
            hard_pin=alias if hard_pin else None,
            preference=None if hard_pin else alias,
        )
    sessions = _execution_sessions_for_workloads(db, ids)
    for session_id in sessions:
        control.set_route_policy(
            session_id,
            "pin" if hard_pin else "next",
            alias,
        )
    return {
        "action": "terminal_route_updated",
        "account": alias,
        "mode": "pin" if hard_pin else "prefer-and-next",
        "terminals": names,
        "active_sessions_updated": sessions,
        "processes_restarted": False,
    }


def clear_terminal_route(
    db: FleetDB,
    control: FleetControl,
    *,
    terminal_names: Sequence[str],
) -> Dict[str, Any]:
    names = [validate_session_name(item) for item in terminal_names]
    ids = {workload_id(item) for item in names}
    for identity in sorted(ids):
        control.set_workload_policy(identity)
    sessions = _execution_sessions_for_workloads(db, ids)
    for session_id in sessions:
        for mode in ("pin", "prefer", "next"):
            control.set_route_policy(session_id, mode, None)
        control.set_route_policy(session_id, "chain", None)
    return {
        "action": "terminal_route_auto",
        "terminals": names,
        "active_sessions_updated": sessions,
        "processes_restarted": False,
    }


def terminal_status(db: FleetDB) -> Dict[str, Any]:
    policies = {
        str(item["workload_id"]): item
        for item in db.workload_policies()
    }
    executions: Dict[str, List[Dict[str, Any]]] = {}
    for item in db.executions():
        identity = item.get("workload_id")
        if identity:
            executions.setdefault(str(identity), []).append(item)
    items = []
    for session in list_tmux_sessions():
        identity = workload_id(session.name)
        policy = policies.get(identity, {})
        items.append(
            {
                **asdict(session),
                "workload_id": identity,
                "preference": policy.get("preference"),
                "hard_pin": policy.get("hard_pin"),
                "fleet_sessions": [
                    {
                        "session_id": item["session_id"],
                        "status": item["status"],
                    }
                    for item in executions.get(identity, [])
                ],
            }
        )
    return {
        "default_account": read_default_account(),
        "terminals": items,
    }
