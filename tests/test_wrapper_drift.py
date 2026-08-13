"""What runs and what is committed have to be the same thing.

Six shell scripts ran this system in production with no source anywhere in the
repository — including `claude-fleet-shell`, which is what the `claude` command
actually resolves to. `pip install claude-fleet-local` succeeding said nothing
about whether Fleet would start, and no clean machine could have been brought to
the running state from source.

They are tracked now, in `bin/`, imported byte-for-byte so that "did importing
them change anything?" is answerable with `cmp` rather than with hope.

That moves the failure rather than removing it. The installed copy and the
tracked copy can drift, and a rollback that restores the repository would leave
the drifted copy running — the same class of problem as the peer whose restart
failed while the probe said everything answered.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_fleet.doctor import _wrapper_drift

REPO = Path(__file__).resolve().parent.parent


def _fake_install(tmp_path, monkeypatch, files):
    """A prefix with bin/, and a fake ~/.local/bin to compare it against."""
    prefix = tmp_path / "repo"
    (prefix / "bin").mkdir(parents=True)
    installed = tmp_path / "home" / ".local" / "bin"
    installed.mkdir(parents=True)
    for name, (source, target) in files.items():
        (prefix / "bin" / name).write_text(source, encoding="utf-8")
        if target is not None:
            (installed / name).write_text(target, encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return {"prefix": str(prefix)}


def test_matching_wrappers_pass(tmp_path, monkeypatch):
    runtime = _fake_install(tmp_path, monkeypatch, {
        "claude-fleet-shell": ("#!/bin/sh\nexec fleet run\n",) * 2,
    })
    ok, detail = _wrapper_drift(runtime)
    assert ok is True and "match" in str(detail)


def test_a_drifted_wrapper_is_reported_by_name(tmp_path, monkeypatch):
    """The one that matters: a hand-edit on the running machine that no commit
    knows about. A rollback would restore the repo copy and leave this running."""
    runtime = _fake_install(tmp_path, monkeypatch, {
        "claude-fleet-shell": ("#!/bin/sh\nexec fleet run\n",
                               "#!/bin/sh\nexec fleet run --tweaked\n"),
    })
    ok, detail = _wrapper_drift(runtime)
    assert ok is False
    assert detail["differs_from_repo"] == ["claude-fleet-shell"]


def test_a_wrapper_that_was_never_installed_is_reported(tmp_path, monkeypatch):
    runtime = _fake_install(tmp_path, monkeypatch, {
        "claude-fleet-canary": ("#!/bin/sh\n", None),
    })
    ok, detail = _wrapper_drift(runtime)
    assert ok is False
    assert detail["not_installed"] == ["claude-fleet-canary"]


def test_an_older_install_without_bin_is_not_a_failure(tmp_path, monkeypatch):
    """This check must not turn every machine that predates `bin/` into a
    degraded doctor — that is how a useful signal gets ignored."""
    prefix = tmp_path / "repo"
    prefix.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    ok, _detail = _wrapper_drift({"prefix": str(prefix)})
    assert ok is True


# ---------------------------------------------------------------------------
# what the imported wrappers must not contain
# ---------------------------------------------------------------------------

def _wrappers():
    directory = REPO / "bin"
    return [p for p in sorted(directory.iterdir()) if p.is_file()]


def test_the_wrappers_are_actually_tracked():
    """The point of the whole exercise. If this list empties, the repository has
    silently gone back to being unable to reproduce the running system."""
    names = {p.name for p in _wrappers()}
    assert "claude-fleet-shell" in names, (
        "`claude` resolves to this; without it a clean machine cannot start Fleet"
    )
    assert len(names) >= 6


@pytest.mark.parametrize("wrapper", _wrappers(), ids=lambda p: p.name)
def test_no_wrapper_carries_a_credential(wrapper):
    """These are going into a public repository. The coordinator token is read
    at runtime from teamclaude.json's fleetCoordinator.token — verified — and
    must never be baked into a script."""
    text = wrapper.read_text(encoding="utf-8", errors="replace")
    assert "sk-ant-" not in text
    for line in text.splitlines():
        if "TOKEN=" in line or "token" in line.lower():
            assert "$(" in line or "json" in line or "export" in line, (
                "a token that is not read from config at runtime is a literal: %r"
                % line
            )


@pytest.mark.parametrize("wrapper", _wrappers(), ids=lambda p: p.name)
def test_no_wrapper_hardcodes_a_home_directory(wrapper):
    """They resolve themselves with `dirname $0` and `$HOME`, which is why they
    can be published at all. A single absolute path would make the repository
    machine-specific again."""
    text = wrapper.read_text(encoding="utf-8", errors="replace")
    assert "/Users/nature" not in text
    assert "/home/nature" not in text
