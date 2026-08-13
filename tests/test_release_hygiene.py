"""Publishing is irreversible, and the repository is also an operations record.

This repository holds deploy logs, repair notes, cross-host incident write-ups
and an account inventory. All of that is worth keeping and none of it is anyone
else's business. So publication is an allow-list — a deny-list would have to
anticipate every future file, while an allow-list only names what a stranger
needs and excludes anything new until somebody decides otherwise.

The scan exists separately because being on the list is not evidence of being
clean. `desktop/verify/snap.json` was a legitimate test fixture carrying five
real account UUIDs and three real email addresses.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "scripts" / "fleet-make-release"


def _module():
    spec = importlib.util.spec_from_loader("make_release", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__dict__["__file__"] = str(SOURCE)
    text = SOURCE.read_text(encoding="utf-8")
    marker = 'if __name__ == "__main__":'
    if marker in text:
        text = text[: text.index(marker)]
    exec(compile(text, str(SOURCE), "exec"), module.__dict__)
    return module


# ---------------------------------------------------------------------------
# the tree that would actually ship
# ---------------------------------------------------------------------------

def test_the_publishable_tree_is_clean():
    """The gate itself. If this fails, do not publish — read what it names."""
    module = _module()
    tracked = module._tracked()
    problems = module.scan(module._published(tracked))
    assert problems == [], "\n" + "\n".join(problems[:20])


def test_the_operations_record_is_not_published():
    """Deploy logs and the account inventory stay here."""
    module = _module()
    published = set(module._published(module._tracked()))
    for private in ("ACCOUNTS.md", "DEPLOY-LOG-20260730.md",
                    "REPAIR-NOTES-20260730.md", "CROSS-HOST-RESULT.md"):
        assert private not in published, private


def test_a_new_file_is_excluded_until_somebody_decides(monkeypatch):
    """The property that makes an allow-list worth the friction: a file nobody
    has thought about does not ship by accident."""
    module = _module()
    assert module._published(["something-nobody-listed.md"]) == []


def test_the_things_a_stranger_needs_are_all_there():
    module = _module()
    published = set(module._published(module._tracked()))
    for required in ("LICENSE", "NOTICE", "SECURITY.md", "README.md",
                     "install.sh", "pyproject.toml"):
        assert required in published, required
    assert any(p.startswith("bin/") for p in published), "包装脚本"
    assert any(p.startswith("src/claude_fleet/") for p in published), "控制面"
    assert any(p.startswith("vendor/teamclaude/") for p in published), "数据面"


# ---------------------------------------------------------------------------
# the scanner's own judgement
# ---------------------------------------------------------------------------

# Assembled at run time rather than written out. A file that contains literal
# examples of what must never ship would itself fail the scan — and exempting it
# would carve a hole in the one check that stands between this repository and a
# published credential.
_JOIN = "".join
_CASES = [
    (_JOIN(["token = 'sk-", "ant-oat01-", "A" * 60, "'"]), True),
    (_JOIN(["token = 'sk-", "ant-oat01-live'"]), False),        # test fixture
    (_JOIN(["'sk-", "ant-oat01-", "x" * 45, "-not-a-credential'"]), False),
    (_JOIN(["contact alice@real", "company.com"]), True),
    ("contact alice@example.com", False),
    (_JOIN(["path = '/Users/", "somebody/.config'"]), True),
    ("path = '/home/someone/.config'", False),                  # documentation
    (_JOIN(["ssh hs-", "tn"]), True),
]


@pytest.mark.parametrize("text,should_flag", _CASES)
def test_the_scanner_separates_real_from_fixture(tmp_path, monkeypatch, text,
                                                  should_flag):
    """A scanner that flags thirty lines of honest test data gets ignored within
    a week, and then it is worth nothing. Length and the `not-a-credential`
    convention are what separate a real token from a fixture."""
    module = _module()
    probe = tmp_path / "probe.py"
    probe.write_text(text, encoding="utf-8")
    monkeypatch.setattr(module, "REPO", tmp_path)

    problems = module.scan(["probe.py"])
    assert bool(problems) is should_flag, (text, problems)


def test_upstream_files_are_exempt_but_named():
    """The vendored README carries its author's address, and publishing it as
    received is the correct thing to do. Exempting it silently would not be."""
    module = _module()
    assert "vendor/teamclaude/README.md" in module.EXEMPT
    assert "vendor/teamclaude/LICENSE" in module.EXEMPT


# ---------------------------------------------------------------------------
# attribution, which was buried where nobody would find it
# ---------------------------------------------------------------------------

def test_the_root_carries_a_licence_and_a_notice():
    """`pyproject.toml` claimed MIT with no licence text anywhere, and the
    derivation from TeamClaude was recorded only in vendor/, which nobody
    landing on the repository would see."""
    assert (REPO / "LICENSE").exists()
    notice = (REPO / "NOTICE").read_text(encoding="utf-8")
    assert "TeamClaude" in notice
    assert "KarpelesLab" in notice
    assert "MIT" in notice
    assert "not an Anthropic product" in notice


def test_the_vendored_licence_is_still_there():
    """Removing it while keeping the code would be a licence violation."""
    upstream = (REPO / "vendor" / "teamclaude" / "LICENSE").read_text(encoding="utf-8")
    assert "MIT" in upstream and "KarpelesLab" in upstream


def test_security_policy_names_the_gap_it_ships_with():
    """FLEET_AUTH_MODE ships as audit, which means the CONNECT hole is measured
    rather than closed. A security policy that omitted that would be worse than
    none."""
    policy = (REPO / "SECURITY.md").read_text(encoding="utf-8")
    assert "FLEET_AUTH_MODE" in policy
    assert "audit" in policy
    assert "Loopback is not an authentication boundary" in policy


# ---------------------------------------------------------------------------
# no leftovers
# ---------------------------------------------------------------------------

def test_no_backup_files_are_tracked():
    """Three `.bak` copies of the optimizer were committed, one of them named
    `.broken`."""
    tracked = subprocess.run(["git", "-C", str(REPO), "ls-files"],
                             capture_output=True, text=True).stdout.split()
    leftovers = [n for n in tracked
                 if n.endswith(".bak") or ".broken" in n or ".partial" in n]
    assert leftovers == [], leftovers


def test_no_state_database_is_tracked():
    """`cli.py`'s `default_db()` falls back to `./work/claude-fleet.sqlite3` when
    there is no runtime config, so any command run from the repository root
    without one creates a database here — and four of those files were tracked.

    Empty, as it turned out. The harm was not the content: SQLite's `-wal` and
    `-shm` go dirty the moment anything writes them, and a dirty tree is exactly
    what makes `fleet-rollback` refuse to run. An emergency rollback blocked by a
    file the tooling created for itself is the worst possible time to find out.
    """
    tracked = subprocess.run(["git", "-C", str(REPO), "ls-files"],
                             capture_output=True, text=True).stdout.split()
    databases = [n for n in tracked
                 if n.endswith((".sqlite3", ".sqlite3-wal", ".sqlite3-shm",
                                ".sqlite3.writer.lock"))]
    assert databases == [], databases


def test_service_templates_carry_no_absolute_home():
    """They are generated per machine now. A published plist with somebody's
    home directory in it is both a leak and wrong for the reader."""
    for template in (REPO / "packaging" / "launchd").glob("*.plist.in"):
        text = template.read_text(encoding="utf-8")
        assert "@HOME@" in text, template.name
        assert "/Users/" not in text, template.name
