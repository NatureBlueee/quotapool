from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List

from .control import FleetError


PASSIVE_HOOK_EVENTS = (
    "SessionStart",
    "SessionEnd",
    "SubagentStart",
    "SubagentStop",
    "TaskCompleted",
    "TeammateIdle",
)


def _hook_command(fleet_binary: str, db_path: str, event: str) -> str:
    # Install paths are rejected if they contain shell metacharacters. The
    # standard installer uses ~/.local paths without spaces.
    for value in (fleet_binary, db_path):
        if any(char in value for char in "\n\r;&|`$<>"):
            raise FleetError("unsafe character in Fleet hook path")
    return f'"{fleet_binary}" --db "{db_path}" hook {event}'


def proposed_settings(
    existing: Dict[str, Any],
    *,
    wrapper_path: str,
    fleet_binary: str,
    db_path: str,
) -> Dict[str, Any]:
    result = json.loads(json.dumps(existing))
    current_wrapper = result.get("processWrapper")
    if current_wrapper and current_wrapper != wrapper_path:
        raise FleetError(
            f"processWrapper is already set to {current_wrapper!r}; refusing to overwrite"
        )
    result["processWrapper"] = wrapper_path
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise FleetError("Claude settings hooks must be an object")
    for event in PASSIVE_HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise FleetError(f"Claude hook {event} must be a list")
        command = _hook_command(fleet_binary, db_path, event)
        already = any(
            hook.get("command") == command
            for entry in entries if isinstance(entry, dict)
            for hook in entry.get("hooks", []) if isinstance(hook, dict)
        )
        if not already:
            entries.append({"hooks": [{"type": "command", "command": command}]})
    return result


def install_settings(
    settings_path: str,
    *,
    wrapper_path: str,
    fleet_binary: str,
    db_path: str,
) -> Dict[str, Any]:
    path = Path(settings_path).expanduser()
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        existing = {}
    except json.JSONDecodeError as exc:
        raise FleetError(f"invalid Claude settings JSON: {path}") from exc
    if not isinstance(existing, dict):
        raise FleetError("Claude settings must be a JSON object")
    updated = proposed_settings(
        existing,
        wrapper_path=wrapper_path,
        fleet_binary=fleet_binary,
        db_path=db_path,
    )
    if updated == existing:
        return {
            "settings_path": str(path),
            "backup_path": None,
            "process_wrapper": wrapper_path,
            "hook_events": list(PASSIVE_HOOK_EVENTS),
            "changed": False,
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_name(
            f"{path.name}.before-claude-fleet-{time.time_ns()}"
        )
        shutil.copy2(path, backup)
    temporary = path.with_name(f".{path.name}.claude-fleet-{os.getpid()}")
    temporary.write_text(
        json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return {
        "settings_path": str(path),
        "backup_path": str(backup) if backup else None,
        "process_wrapper": wrapper_path,
        "hook_events": list(PASSIVE_HOOK_EVENTS),
        "changed": True,
    }


def proposed_settings_removal(
    existing: Dict[str, Any],
    *,
    wrapper_path: str,
    fleet_binary: str,
    db_path: str,
) -> Dict[str, Any]:
    """Remove only the settings entries installed by Claude Fleet.

    This is deliberately structural rather than backup restoration: a user may
    legitimately edit other Claude settings after Fleet was installed.  A
    rollback must preserve those later edits and unrelated hooks.
    """
    result = json.loads(json.dumps(existing))
    if result.get("processWrapper") == wrapper_path:
        result.pop("processWrapper", None)

    hooks = result.get("hooks")
    if not isinstance(hooks, dict):
        return result

    for event in PASSIVE_HOOK_EVENTS:
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        command = _hook_command(fleet_binary, db_path, event)
        retained_entries = []
        for entry in entries:
            if not isinstance(entry, dict):
                retained_entries.append(entry)
                continue
            hook_items = entry.get("hooks")
            if not isinstance(hook_items, list):
                retained_entries.append(entry)
                continue
            retained_hooks = [
                hook for hook in hook_items
                if not (
                    isinstance(hook, dict)
                    and hook.get("type") == "command"
                    and hook.get("command") == command
                )
            ]
            if retained_hooks:
                updated_entry = json.loads(json.dumps(entry))
                updated_entry["hooks"] = retained_hooks
                retained_entries.append(updated_entry)
        if retained_entries:
            hooks[event] = retained_entries
        else:
            hooks.pop(event, None)
    if not hooks:
        result.pop("hooks", None)
    return result


def remove_settings(
    settings_path: str,
    *,
    wrapper_path: str,
    fleet_binary: str,
    db_path: str,
) -> Dict[str, Any]:
    path = Path(settings_path).expanduser()
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "settings_path": str(path),
            "backup_path": None,
            "changed": False,
        }
    except json.JSONDecodeError as exc:
        raise FleetError(f"invalid Claude settings JSON: {path}") from exc
    if not isinstance(existing, dict):
        raise FleetError("Claude settings must be a JSON object")
    updated = proposed_settings_removal(
        existing,
        wrapper_path=wrapper_path,
        fleet_binary=fleet_binary,
        db_path=db_path,
    )
    if updated == existing:
        return {
            "settings_path": str(path),
            "backup_path": None,
            "changed": False,
        }
    backup = path.with_name(
        f"{path.name}.before-claude-fleet-removal-{time.time_ns()}"
    )
    shutil.copy2(path, backup)
    temporary = path.with_name(f".{path.name}.claude-fleet-remove-{os.getpid()}")
    temporary.write_text(
        json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return {
        "settings_path": str(path),
        "backup_path": str(backup),
        "changed": True,
    }
