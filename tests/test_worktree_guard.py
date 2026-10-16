from __future__ import annotations

import os
import subprocess
from pathlib import Path

from claude_fleet import worktree_guard


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _repository(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    root = tmp_path / "worktrees"
    repo.mkdir()
    root.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "guard@example.test")
    _git(repo, "config", "user.name", "Guard Test")
    (repo / "readme.txt").write_text("base\n", encoding="utf-8")
    old_env = dict(os.environ)
    old_env.update({
        "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z",
    })
    _git(repo, "add", "readme.txt")
    _git(repo, "commit", "-m", "base", env=old_env)
    return repo, root


def test_parser_keeps_locked_and_detached_flags():
    rows = worktree_guard._parse_worktrees(
        "worktree C:/repo\nHEAD abc\nbranch refs/heads/main\n\n"
        "worktree C:/repo/wt\nHEAD def\ndetached\nlocked task running\n\n"
    )

    assert rows[1]["detached"] is True
    assert rows[1]["locked"] == "task running"


def test_dry_run_finds_old_clean_recoverable_worktree_without_removing(
    tmp_path, monkeypatch
):
    repo, root = _repository(tmp_path)
    worktree = root / "old-task"
    _git(repo, "branch", "old-task")
    _git(repo, "worktree", "add", str(worktree), "old-task")
    old = 1_600_000_000
    os.utime(worktree / "readme.txt", (old, old))
    os.utime(worktree / ".git", (old, old))
    os.utime(worktree, (old, old))
    monkeypatch.setattr(worktree_guard, "_process_commands", lambda: (True, []))

    state = worktree_guard.maintain_worktrees(
        [repo], [root], state_path=tmp_path / "state.json",
        max_age_days=14, dry_run=True, now=1_800_000_000,
    )

    item = next(row for row in state["items"] if row["path"] == str(worktree))
    assert item["status"] == "eligible"
    assert state["summary"]["eligible"] == 1
    assert worktree.exists()
    assert _git(repo, "show-ref", "--verify", "refs/heads/old-task")


def test_real_cleanup_removes_only_clean_worktree_and_preserves_branches(
    tmp_path, monkeypatch
):
    repo, root = _repository(tmp_path)
    clean = root / "clean-task"
    dirty = root / "dirty-task"
    _git(repo, "branch", "clean-task")
    _git(repo, "branch", "dirty-task")
    _git(repo, "worktree", "add", str(clean), "clean-task")
    _git(repo, "worktree", "add", str(dirty), "dirty-task")
    (dirty / "uncommitted.txt").write_text("keep me", encoding="utf-8")
    old = 1_600_000_000
    for path in (clean / "readme.txt", clean / ".git", clean, dirty / "readme.txt", dirty / ".git"):
        os.utime(path, (old, old))
    monkeypatch.setattr(worktree_guard, "_process_commands", lambda: (True, []))

    state = worktree_guard.maintain_worktrees(
        [repo], [root], state_path=tmp_path / "state.json",
        max_age_days=14, now=1_800_000_000,
    )

    assert not clean.exists()
    assert dirty.exists()
    assert state["summary"]["removed"] == 1
    assert any(row.get("reason") == "dirty-worktree" for row in state["items"])
    assert _git(repo, "show-ref", "--verify", "refs/heads/clean-task")


def test_codex_owned_worktrees_are_never_approved(tmp_path):
    root = tmp_path / ".codex" / "worktrees"
    candidate = root / "task"
    candidate.mkdir(parents=True)

    assert worktree_guard._under_direct_root(candidate, [root]) is False
