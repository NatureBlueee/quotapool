from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Dict

from .control import FleetError


BEGIN = "# >>> claude-fleet managed shell entry >>>"
END = "# <<< claude-fleet managed shell entry <<<"


def _block(shim_dir: str) -> str:
    if any(char in shim_dir for char in "\n\r`$"):
        raise FleetError("unsafe character in Fleet shim path")
    return (
        f"{BEGIN}\n"
        f'export PATH="{shim_dir}:$PATH"\n'
        f"{END}"
    )


def proposed_shell(existing: str, shim_dir: str) -> str:
    block = _block(shim_dir)
    if BEGIN in existing or END in existing:
        if block in existing:
            return existing
        raise FleetError("incomplete or different Claude Fleet shell block exists")
    separator = "" if not existing or existing.endswith("\n") else "\n"
    return f"{existing}{separator}{block}\n"


def proposed_shell_removal(existing: str, shim_dir: str) -> str:
    block = _block(shim_dir)
    if block not in existing:
        if BEGIN in existing or END in existing:
            raise FleetError("incomplete or different Claude Fleet shell block exists")
        return existing
    updated = existing.replace(f"{block}\n", "", 1)
    if updated == existing:
        updated = existing.replace(block, "", 1)
    return updated


def _write(path: Path, value: str, action: str) -> Dict[str, object]:
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if current == value:
        return {"shell_file": str(path), "backup_path": None, "changed": False}
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    target_mode = 0o600
    if path.exists():
        target_mode = path.stat().st_mode & 0o777
        backup = path.with_name(
            f"{path.name}.before-claude-fleet-{action}-{time.time_ns()}"
        )
        shutil.copy2(path, backup)
    temporary = path.with_name(f".{path.name}.claude-fleet-{os.getpid()}")
    temporary.write_text(value, encoding="utf-8")
    os.chmod(temporary, target_mode)
    os.replace(temporary, path)
    return {
        "shell_file": str(path),
        "backup_path": str(backup) if backup else None,
        "changed": True,
    }


def install_shell(shell_file: str, shim_dir: str) -> Dict[str, object]:
    path = Path(shell_file).expanduser()
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    return _write(path, proposed_shell(current, shim_dir), "install")


def remove_shell(shell_file: str, shim_dir: str) -> Dict[str, object]:
    path = Path(shell_file).expanduser()
    if not path.exists():
        return {"shell_file": str(path), "backup_path": None, "changed": False}
    current = path.read_text(encoding="utf-8")
    return _write(path, proposed_shell_removal(current, shim_dir), "removal")
