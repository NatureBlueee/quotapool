from __future__ import annotations

import json
import os
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List


def _run(args: List[str]) -> str:
    try:
        result = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def observe() -> Dict[str, Any]:
    raw = _run(["claude", "agents", "--json", "--all"])
    try:
        agents = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        agents = []
    if not isinstance(agents, list):
        agents = []

    sessions: List[str] = []
    summaries = []
    for item in agents:
        if not isinstance(item, dict):
            continue
        session_id = item.get("sessionId") or item.get("session_id") or item.get("id")
        if session_id:
            sessions.append(str(session_id))
        # Session display names can contain the original prompt. Keep the
        # observer metadata-only even when Claude returns verbose names.
        summaries.append({
            key: item.get(key)
            for key in (
                "sessionId",
                "session_id",
                "id",
                "status",
                "cwd",
                "sessionKind",
                "entrypoint",
            )
            if key in item
        })

    settings_path = Path.home() / ".claude" / "settings.json"
    settings: Dict[str, Any] = {}
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    duplicates = sorted(
        session_id for session_id, count in Counter(sessions).items() if count > 1
    )
    return {
        "claude_version": _run(["claude", "--version"]).strip() or None,
        "agent_count": len(summaries),
        "agents": summaries,
        "duplicate_session_ids": duplicates,
        "settings": {
            "path": str(settings_path),
            "has_process_wrapper": bool(settings.get("processWrapper")),
            "env_keys": sorted((settings.get("env") or {}).keys()),
            "hook_events": sorted((settings.get("hooks") or {}).keys()),
        },
        "environment": {
            "config_dir_set": "CLAUDE_CONFIG_DIR" in os.environ,
            "fleet_base_url_set": os.environ.get("ANTHROPIC_BASE_URL", "").startswith(
                "http://127.0.0.1:"
            ),
        },
        "anomalies": (
            [{"type": "DUPLICATE_WRITER_CANDIDATE", "session_id": value}
             for value in duplicates]
            + ([] if settings.get("processWrapper") else [
                {"type": "PROCESS_WRAPPER_NOT_CONFIGURED"}
            ])
        ),
        "note": "No credentials, prompt bodies, or transcript contents were read.",
    }
