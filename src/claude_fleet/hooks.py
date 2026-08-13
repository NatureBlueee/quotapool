from __future__ import annotations

import json
import os
import socket
import time
from typing import Any, Dict, Optional

from .db import FleetDB, json_dump
from .control import FleetControl, FleetError, StaleWriter


SAFE_FIELDS = {
    "session_id",
    "hook_event_name",
    "source",
    "cwd",
    "permission_mode",
    "agent_id",
    "agent_type",
    "task_id",
    "team_name",
    "teammate_name",
    "worktree_path",
    "reason",
    "stop_hook_active",
}


def _safe_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key in SAFE_FIELDS:
        value = payload.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = value
    return result


def _node_identity(
    event_name: str,
    safe: Dict[str, Any],
    session_id: Optional[str],
    agent_id: Optional[str],
) -> tuple[Optional[str], str, Optional[str], str]:
    """Map Claude lifecycle events to stable, metadata-only graph nodes.

    Team members can appear through both Subagent* and TeammateIdle hooks.  Use
    the teammate name when Claude supplies one so those two surfaces converge
    on one node.  Tasks must not reuse the session node: otherwise a
    TaskCompleted hook would incorrectly mark the whole Claude session done.
    """
    session_key = session_id or "unknown"
    team_name = str(safe.get("team_name") or "") or None
    teammate_name = str(safe.get("teammate_name") or "") or None
    task_id = str(safe.get("task_id") or "") or None
    parent_id = f"session:{session_id}" if session_id else None

    if team_name or teammate_name:
        member_key = teammate_name or agent_id or "unknown"
        node_id = (
            f"team-member:{session_key}:{team_name or 'unknown'}:{member_key}"
        )
        status = "idle" if event_name == "TeammateIdle" else (
            "completed" if event_name == "SubagentStop" else "active"
        )
        return node_id, "team-member", parent_id, status
    if agent_id:
        status = "completed" if event_name == "SubagentStop" else "active"
        return (
            f"agent:{session_key}:{agent_id}",
            "subagent",
            parent_id,
            status,
        )
    if task_id:
        status = "completed" if event_name == "TaskCompleted" else "active"
        return (
            f"task:{session_key}:{task_id}",
            "task",
            parent_id,
            status,
        )
    if session_id:
        status = "completed" if event_name == "SessionEnd" else "active"
        return f"session:{session_id}", "session", None, status
    return None, "event", None, "active"


def record_hook(
    db: FleetDB,
    event_name: str,
    payload: Dict[str, Any],
    *,
    process_id: Optional[str] = None,
) -> Dict[str, Any]:
    now = time.time()
    safe = _safe_payload(payload)
    session_id = str(payload.get("session_id") or "") or None
    agent_id = str(payload.get("agent_id") or "") or None
    process_id = (
        process_id
        or os.environ.get("FLEET_PROCESS_ID")
        or os.environ.get("FLEET_LAUNCH_ID")
    )
    writer_holder = os.environ.get("FLEET_LAUNCH_ID") or process_id
    host_id = socket.gethostname()
    workload = os.environ.get("FLEET_WORKLOAD_CLASS", "interactive")
    execution_hint = os.environ.get("FLEET_EXECUTION_ID")
    process_metadata = json_dump({
        "launch_id": os.environ.get("FLEET_LAUNCH_ID"),
        "execution_id": execution_hint,
        "workload_id": os.environ.get("FLEET_WORKLOAD_ID"),
    })
    node_id, node_type, parent_id, node_status = _node_identity(
        event_name,
        safe,
        session_id,
        agent_id,
    )
    binding: Optional[Dict[str, Any]] = None
    binding_error: Optional[str] = None
    if event_name == "SessionStart" and session_id and execution_hint:
        try:
            binding = FleetControl(db).bind_session_start(
                execution_hint,
                session_id,
                holder=writer_holder,
                workload_class=workload,
                workload_id=os.environ.get("FLEET_WORKLOAD_ID"),
                parent_hint=os.environ.get("FLEET_PARENT_EXECUTION_ID"),
            )
        except FleetError as exc:
            # SessionStart observation must remain passive. Preserve a
            # metadata-only failure event for the reconciler/operator instead
            # of blocking Claude startup or persisting exception context.
            binding_error = exc.code

    with db.transaction() as conn:
        if process_id:
            conn.execute(
                """
                INSERT INTO processes(
                    id,host_id,pid,parent_process_id,workload_class,config_dir,cwd,
                    state,first_seen_at,last_seen_at,metadata
                ) VALUES(?,?,?,?,?,?,?,'active',?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at,
                    state='active',
                    cwd=COALESCE(excluded.cwd,processes.cwd)
                """,
                (
                    process_id,
                    host_id,
                    None,
                    os.environ.get("FLEET_PARENT_PROCESS_ID"),
                    workload,
                    os.environ.get("CLAUDE_CONFIG_DIR"),
                    safe.get("cwd"),
                    now,
                    now,
                    process_metadata,
                ),
            )
            if event_name == "SessionEnd":
                conn.execute(
                    "UPDATE processes SET state='stopped',last_seen_at=? WHERE id=?",
                    (now, process_id),
                )
        if node_id:
            if parent_id == node_id:
                parent_id = None
            if parent_id:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO execution_nodes(
                        id,node_type,session_id,status,created_at,updated_at,metadata
                    ) VALUES(?,'session',?,'active',?,?,?)
                    """,
                    (parent_id, session_id, now, now, "{}"),
                )
            conn.execute(
                """
                INSERT INTO execution_nodes(
                    id,node_type,session_id,parent_id,process_id,agent_id,
                    team_name,teammate_name,worktree,status,created_at,updated_at,
                    metadata
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    parent_id=COALESCE(excluded.parent_id,execution_nodes.parent_id),
                    process_id=COALESCE(excluded.process_id,execution_nodes.process_id),
                    agent_id=COALESCE(excluded.agent_id,execution_nodes.agent_id),
                    team_name=COALESCE(excluded.team_name,execution_nodes.team_name),
                    teammate_name=COALESCE(
                        excluded.teammate_name,execution_nodes.teammate_name
                    ),
                    worktree=COALESCE(excluded.worktree,execution_nodes.worktree),
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    metadata=excluded.metadata
                """,
                (
                    node_id,
                    node_type,
                    session_id,
                    parent_id,
                    process_id,
                    agent_id,
                    safe.get("team_name"),
                    safe.get("teammate_name"),
                    safe.get("worktree_path"),
                    node_status,
                    now,
                    now,
                    json_dump(safe),
                ),
            )
        conn.execute(
            """
            INSERT INTO hook_events(
                at,event_name,session_id,execution_node_id,process_id,payload
            ) VALUES(?,?,?,?,?,?)
            """,
            (now, event_name, session_id, node_id, process_id, json_dump(safe)),
        )
        FleetDB.event(conn, f"hook.{event_name}", session_id, safe)
        if binding_error:
            FleetDB.event(
                conn,
                "session.rebind_failed",
                session_id,
                {
                    "execution_hint": execution_hint,
                    "error": binding_error,
                },
            )
    if event_name == "SessionEnd" and session_id:
        control = FleetControl(db)
        # The provisional process can emit SessionEnd after its identity was
        # rebound to a live background worker. It is a tombstone, not authority
        # to release the canonical writer.
        if control.session_alias_target(session_id) is None:
            try:
                control.release_writer(
                    session_id,
                    writer_holder,
                    retain_route=True,
                )
            except StaleWriter:
                # A late SessionEnd from a fenced old process must never release
                # the current writer. Preserve hook evidence and ownership.
                pass
    return {
        "ok": True,
        "event": event_name,
        "session_id": session_id,
        "node_id": node_id,
        "execution_id": binding["id"] if binding else None,
        "binding_error": binding_error,
    }


def read_stdin_payload() -> Dict[str, Any]:
    try:
        value = json.load(__import__("sys").stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
