"""Conservative lifecycle maintenance for explicitly registered Git worktrees.

The guard removes only clean, inactive worktrees whose HEAD is reachable from
another Git ref.  It never deletes branches and never manages Codex-owned
``.codex/worktrees`` directories.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from .desktop import hidden_subprocess_kwargs


DEFAULT_MAX_AGE_DAYS = 14.0
DEFAULT_STATE_NAME = "worktree-maintenance.json"


def _run(command: Sequence[str], timeout: float = 60) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(list(command), 1, "", str(exc))


def _git(repo: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-C", str(repo), *args], timeout=timeout)


def _parse_worktrees(output: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for raw in [*output.splitlines(), ""]:
        line = raw.rstrip()
        if not line:
            if current:
                rows.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key in {"detached", "bare"}:
            current[key] = True
        elif key in {"locked", "prunable"}:
            current[key] = value or True
        else:
            current[key] = value
    return rows


def _is_reparse_point(path: Path) -> bool:
    try:
        stat = path.lstat()
    except OSError:
        return True
    flag = getattr(stat, "st_file_attributes", 0)
    reparse = getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(flag & reparse) or path.is_symlink()


def _under_direct_root(candidate: Path, roots: Sequence[Path]) -> bool:
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return False
    if ".codex" in {part.casefold() for part in resolved.parts}:
        return False
    for root in roots:
        try:
            approved = root.resolve(strict=True)
        except OSError:
            continue
        if resolved.parent == approved:
            return True
    return False


def _process_commands() -> tuple[bool, list[str]]:
    """Return process command lines, failing closed when Windows cannot probe."""
    if os.name != "nt":
        return True, []
    powershell = shutil.which("powershell.exe")
    if not powershell:
        return False, []
    script = (
        "@(Get-CimInstance Win32_Process -ErrorAction Stop|"
        "ForEach-Object{[string]$_.CommandLine})|ConvertTo-Json -Compress"
    )
    result = _run(
        [powershell, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
        timeout=30,
    )
    if result.returncode != 0:
        return False, []
    try:
        value = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return False, []
    values = value if isinstance(value, list) else [value]
    return True, [str(item) for item in values if item]


def _path_variants(path: Path) -> tuple[str, ...]:
    windows = str(path).replace("/", "\\").casefold()
    variants = [windows]
    match = re.match(r"^([a-z]):\\(.*)$", windows)
    if match:
        variants.append(f"/{match.group(1)}/{match.group(2).replace('\\', '/')}")
    return tuple(variants)


def _referenced_by_process(path: Path, commands: Sequence[str]) -> bool:
    variants = _path_variants(path)
    return any(any(value in command.casefold().replace("\\\\", "\\") for value in variants) for command in commands)


def _claude_project_name(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", str(path))


def _latest_claude_activity(path: Path, projects_root: Path) -> float:
    project = projects_root / _claude_project_name(path)
    if not project.is_dir():
        return 0.0
    latest = project.stat().st_mtime
    try:
        for item in project.rglob("*.jsonl"):
            try:
                latest = max(latest, item.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        pass
    return latest


def _latest_tracked_activity(path: Path) -> float:
    latest = 0.0
    result = _git(path, "ls-files", "-z", timeout=120)
    if result.returncode != 0:
        return latest
    for relative in result.stdout.split("\0"):
        if not relative:
            continue
        try:
            latest = max(latest, (path / relative).stat().st_mtime)
        except OSError:
            continue
    return latest


def _head_activity(repo: Path, head: str) -> float:
    result = _git(repo, "show", "-s", "--format=%ct", head)
    try:
        return float(result.stdout.strip()) if result.returncode == 0 else 0.0
    except ValueError:
        return 0.0


def _other_refs(repo: Path, head: str, own_branch: str) -> list[str]:
    result = _git(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        f"--contains={head}",
        "refs/heads",
        "refs/remotes",
    )
    if result.returncode != 0:
        return []
    return sorted({line.strip() for line in result.stdout.splitlines() if line.strip() and line.strip() != own_branch})


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read_worktree_state(path: Optional[Path] = None) -> dict[str, Any]:
    target = path or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Quotapool" / "state" / DEFAULT_STATE_NAME
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def maintain_worktrees(
    repos: Sequence[Path],
    roots: Sequence[Path],
    *,
    state_path: Path,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    dry_run: bool = False,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Inspect and optionally remove expired worktrees under approved roots."""
    now_epoch = time.time() if now is None else now
    cutoff = now_epoch - max(1.0, max_age_days) * 86400
    process_ok, commands = _process_commands()
    projects_root = Path.home() / ".claude" / "projects"
    items: list[dict[str, Any]] = []
    removed = 0
    eligible = 0
    errors = 0

    approved_roots = [Path(value) for value in roots]
    for raw_repo in repos:
        try:
            repo = raw_repo.resolve(strict=True)
        except OSError as exc:
            items.append({"repo": str(raw_repo), "status": "repo-missing", "reason": str(exc)})
            errors += 1
            continue
        listing = _git(repo, "worktree", "list", "--porcelain")
        if listing.returncode != 0:
            items.append({"repo": str(repo), "status": "git-error", "reason": listing.stderr[-500:]})
            errors += 1
            continue
        rows = _parse_worktrees(listing.stdout)
        main_path = Path(str(rows[0].get("worktree"))) if rows else repo
        for record in rows:
            raw_path = str(record.get("worktree") or "")
            if not raw_path:
                continue
            path = Path(raw_path)
            item: dict[str, Any] = {
                "repo": str(repo),
                "path": str(path),
                "branch": str(record.get("branch") or "detached"),
                "head": str(record.get("HEAD") or "")[:12],
                "status": "protected",
            }
            items.append(item)
            if os.path.normcase(str(path)) == os.path.normcase(str(main_path)):
                item["reason"] = "main-worktree"
                continue
            if record.get("locked"):
                item["reason"] = "locked"
                continue
            if not _under_direct_root(path, approved_roots):
                item["reason"] = "outside-approved-roots"
                continue
            if _is_reparse_point(path) or not (path / ".git").is_file():
                item["reason"] = "unsafe-path"
                continue
            if not process_ok:
                item["reason"] = "process-probe-failed"
                continue
            if _referenced_by_process(path, commands):
                item["reason"] = "active-process"
                continue
            status = _git(path, "status", "--porcelain", "--untracked-files=normal", timeout=120)
            if status.returncode != 0:
                item["reason"] = "status-probe-failed"
                continue
            if status.stdout.strip():
                item["reason"] = "dirty-worktree"
                continue
            head = str(record.get("HEAD") or "")
            own_branch = str(record.get("branch") or "")
            refs = _other_refs(repo, head, own_branch)
            item["recoverableRefs"] = refs[:8]
            if not refs:
                item["reason"] = "head-not-in-other-ref"
                continue
            activity = max(
                path.stat().st_mtime,
                (path / ".git").stat().st_mtime,
                _latest_claude_activity(path, projects_root),
                _head_activity(repo, head),
            )
            # Most worktrees are recent. Avoid stat'ing every tracked file in
            # those trees; the full pass is only needed for an apparently old
            # candidate where a reverted edit may still prove recent use.
            if activity <= cutoff:
                activity = max(activity, _latest_tracked_activity(path))
            item["lastActivityAt"] = datetime.fromtimestamp(activity, timezone.utc).isoformat()
            item["ageDays"] = round(max(0.0, (now_epoch - activity) / 86400), 2)
            if activity > cutoff:
                item["reason"] = "recent-activity"
                continue
            eligible += 1
            item["status"] = "eligible" if dry_run else "removing"
            item["reason"] = "expired-clean-and-recoverable"
            if dry_run:
                continue
            drive_root = Path(path.anchor) if path.anchor else path
            try:
                before = shutil.disk_usage(drive_root).free
            except OSError:
                before = 0
            result = _git(repo, "worktree", "remove", str(path), timeout=900)
            if result.returncode == 0:
                item["status"] = "removed"
                removed += 1
                try:
                    item["reclaimedBytes"] = max(0, shutil.disk_usage(drive_root).free - before)
                except OSError:
                    item["reclaimedBytes"] = 0
            else:
                item["status"] = "remove-failed"
                item["reason"] = (result.stderr or result.stdout)[-500:].strip()
                errors += 1

    summary = {
        "scanned": len(items),
        "eligible": eligible,
        "removed": removed,
        "protected": sum(1 for item in items if item.get("status") == "protected"),
        "errors": errors,
        "reclaimedBytes": sum(int(item.get("reclaimedBytes") or 0) for item in items),
    }
    state = {
        "at": datetime.fromtimestamp(now_epoch, timezone.utc).isoformat(),
        "status": "error" if errors else ("dry-run" if dry_run else "healthy"),
        "policy": {
            "maxAgeDays": max_age_days,
            "branchesPreserved": True,
            "requiresCleanWorktree": True,
            "requiresOtherRecoverableRef": True,
            "activeProcessesProtected": True,
            "codexWorktreesProtected": True,
        },
        "roots": [str(path) for path in approved_roots],
        "summary": summary,
        "items": items,
    }
    _write_state(state_path, state)
    return state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", action="append", required=True)
    parser.add_argument("--root", action="append", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--max-age-days", type=float, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    state = maintain_worktrees(
        [Path(value) for value in args.repo],
        [Path(value) for value in args.root],
        state_path=Path(args.state),
        max_age_days=args.max_age_days,
        dry_run=args.dry_run,
    )
    return 1 if state["summary"]["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
