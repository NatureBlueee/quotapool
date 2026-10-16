"""Until now there was no way to get from a clone to a running system.

Every step existed as its own verb — `fleet settings install`, `fleet shell
install`, `bootstrap_config.py`, `install-systemd.sh` — and none of them was
assembled. The README opened with 「当前本机状态」, which is an operator's
notebook: it describes a machine that already works rather than how to get one.
`pip install` succeeding meant nothing, because the wrappers that actually run
Fleet were not in the repository at all until they were imported into `bin/`.

Everything here is driven by running the real script against a scratch HOME,
because the two things it found could not have been found any other way:

- `fleet` itself was not installed. Every wrapper is `exec "$BIN_DIR/fleet" …`,
  so nothing worked. Invisible on this machine, where it has been in place since
  July.
- `fleet settings install` needs three arguments the installer was not passing,
  and it failed silently into a warning.

Services are deliberately out of scope for these tests. `install-timers.sh`
calls `launchctl bootout/bootstrap gui/$(id -u)`, which acts on the real login
session no matter what HOME says — a test that ran it would replace the running
jobs with ones pointing at a temp directory.
"""

from __future__ import annotations

import os
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "install.sh"


def test_bootstrap_preserves_an_existing_cross_host_ca(tmp_path):
    config = tmp_path / "config"
    state = tmp_path / "state"
    config.mkdir()
    cross_host_ca = tmp_path / "wsl-teamclaude-ca.pem"
    cross_host_ca.write_text("test-ca\n", encoding="utf-8")
    (config / "runtime.json").write_text(
        json.dumps({"ca_path": str(cross_host_ca)}), encoding="utf-8"
    )

    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "bootstrap_config.py"),
            "--config-dir", str(config),
            "--state-dir", str(state),
            "--prefix", str(REPO),
            "--claude-binary", str(tmp_path / "claude"),
            "--token=test-token",
            "--node-binary", str(tmp_path / "node"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    runtime = json.loads((config / "runtime.json").read_text(encoding="utf-8"))
    assert runtime["ca_path"] == str(cross_host_ca)


def test_bootstrap_accepts_explicit_windows_ports_and_ca(tmp_path):
    config = tmp_path / "config"
    state = tmp_path / "state"
    local_ca = config / "teamclaude-ca.pem"

    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "bootstrap_config.py"),
            "--config-dir", str(config),
            "--state-dir", str(state),
            "--prefix", str(REPO),
            "--claude-binary", str(tmp_path / "claude"),
            "--token=test-token",
            "--node-binary", str(tmp_path / "node"),
            "--proxy-port=13457",
            "--coordinator-port=17893",
            "--ca-path", str(local_ca),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    runtime = json.loads((config / "runtime.json").read_text(encoding="utf-8"))
    team = json.loads((config / "teamclaude.json").read_text(encoding="utf-8"))
    assert runtime["proxy_port"] == 13457
    assert runtime["coordinator_port"] == 17893
    assert runtime["ca_path"] == str(local_ca)
    assert team["proxy"]["port"] == 13457
    assert team["fleetCoordinator"]["url"] == "http://127.0.0.1:17893"


@pytest.fixture()
def home(tmp_path):
    """A HOME with a Claude Code binary and nothing else."""
    versions = tmp_path / ".local" / "share" / "claude" / "versions"
    versions.mkdir(parents=True)
    binary = versions / "2.1.100"
    binary.write_text("#!/bin/sh\necho '2.1.100 (Claude Code)'\n", encoding="utf-8")
    binary.chmod(0o755)
    return tmp_path


def _config(home):
    """New installs use the project's own name; an existing `claude-fleet`
    directory is honoured instead, because that is where a running fleet's
    credentials and services already live."""
    legacy = home / ".config" / "claude-fleet"
    return legacy if legacy.exists() else home / ".config" / "quotapool"


def _env(home):
    """A HOME override is not isolation on its own.

    `install.sh` honours `XDG_CONFIG_HOME` and `XDG_STATE_HOME`, which is
    correct — but these tests only overrode `HOME` and let the XDG variables
    leak in from the environment. On a machine that sets them (GitHub's Linux
    runners do) the installer wrote its config **outside the scratch HOME**, so
    every assertion about `$HOME/.config/quotapool` failed — and, worse, the
    test suite wrote a `runtime.json` and a `teamclaude.json` into the real
    user's config directory.

    Found by CI within two minutes of this repository going public; invisible on
    the machine it was written on, which does not set them.
    """
    return {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
    }


def _run(home, *args, expect=0):
    result = subprocess.run(
        ["sh", str(INSTALLER), *args],
        cwd=str(REPO), capture_output=True, text=True,
        env=_env(home), timeout=180)
    if expect is not None:
        # The arguments and the code, not just the output. This assertion fired
        # once with both streams empty, which told the reader nothing at all
        # about which invocation had failed or how.
        assert result.returncode == expect, (
            "install.sh %s → 退出码 %d（期望 %d）\nstdout:\n%s\nstderr:\n%s"
            % (" ".join(args), result.returncode, expect,
               result.stdout or "(空)", result.stderr or "(空)"))
    return result


# ---------------------------------------------------------------------------
# the clean-machine round trip
# ---------------------------------------------------------------------------

def test_a_clean_machine_gets_a_complete_install(home):
    _run(home, "--no-services")

    installed = home / ".local" / "bin"
    for name in sorted(p.name for p in (REPO / "bin").iterdir() if p.is_file()):
        assert (installed / name).is_symlink(), name

    assert (installed / "quotapool").exists(), (
        "every wrapper execs the control-plane entry point; without it nothing runs"
    )
    assert (installed / "fleet").exists(), (
        "the old name has to keep working — a running install has it wired into "
        "launchd plists, the wrapper scripts and a shell shim"
    )
    for name in ("runtime.json", "teamclaude.json"):
        assert (_config(home) / name).exists(), name


def test_claude_is_routed_through_fleet(home):
    """The line that gives the whole thing effect — and the reason
    `claude-direct` exists."""
    _run(home, "--no-services")
    link = home / ".local" / "bin" / "claude"
    assert link.is_symlink()
    assert os.readlink(link) == "claude-fleet-shell"


def test_credentials_are_written_private(home):
    _run(home, "--no-services")
    for name in ("runtime.json", "teamclaude.json"):
        path = _config(home) / name
        assert oct(path.stat().st_mode & 0o777) == "0o600", name


def test_uninstall_puts_the_machine_back(home):
    _run(home, "--no-services")
    _run(home, "--uninstall")

    installed = home / ".local" / "bin"
    left = [p.name for p in installed.iterdir()] if installed.exists() else []
    assert left == [], "uninstall left %r behind" % left


def test_uninstall_keeps_the_credentials(home):
    """Deleting somebody's OAuth credentials is irreversible and needs a browser
    to undo. The uninstaller says where they are and stops."""
    _run(home, "--no-services")
    _run(home, "--uninstall")
    assert (_config(home) / "teamclaude.json").exists()


def test_uninstall_leaves_other_peoples_files_alone(home):
    """It removes only symlinks that point back at this checkout."""
    _run(home, "--no-services")
    stranger = home / ".local" / "bin" / "claude-pool"
    stranger.unlink()
    stranger.write_text("#!/bin/sh\necho not ours\n", encoding="utf-8")

    _run(home, "--uninstall")
    assert stranger.exists(), "a real file is not this installer's to delete"


# ---------------------------------------------------------------------------
# refusing to take over what it did not create
# ---------------------------------------------------------------------------

def test_it_refuses_a_name_collision(home):
    """Borrowed from install-systemd.sh, which already refuses to replace a unit
    it did not generate. A collision is an error, not a silent takeover."""
    target = home / ".local" / "bin"
    target.mkdir(parents=True)
    (target / "claude-pool").write_text("#!/bin/sh\necho somebody else\n",
                                        encoding="utf-8")

    result = _run(home, "--no-services", expect=1)
    assert "拒绝覆盖" in result.stderr


def test_an_identical_hand_made_install_is_upgraded_in_place(home):
    """This machine's own case: the wrappers were copied by hand months before
    they were tracked. Identical bytes means a symlink changes no behaviour, so
    refusing there would block every existing install from ever upgrading."""
    target = home / ".local" / "bin"
    target.mkdir(parents=True)
    shutil.copyfile(REPO / "bin" / "claude-pool", target / "claude-pool")

    _run(home, "--no-services")
    assert (target / "claude-pool").is_symlink()


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

def test_preflight_refuses_without_claude_and_writes_nothing(tmp_path):
    """A half-finished install of something that intercepts `claude` is worse
    than none: the shim can be in place while the service that gives it meaning
    is not."""
    result = _run(tmp_path, "--no-services", expect=1)
    assert "前置检查未通过" in result.stderr
    assert not (tmp_path / ".local" / "bin").exists()
    assert not (tmp_path / ".config" / "quotapool").exists()


def test_a_dry_run_changes_nothing(home):
    _run(home, "--dry-run")
    assert not (home / ".config" / "quotapool").exists()
    assert not (home / ".local" / "bin" / "claude").exists()


def test_a_zero_byte_claude_is_not_a_claude(tmp_path):
    """A failed update leaves one behind, and it sorts highest by version."""
    versions = tmp_path / ".local" / "share" / "claude" / "versions"
    versions.mkdir(parents=True)
    (versions / "2.1.224").write_bytes(b"")
    (versions / "2.1.224").chmod(0o755)

    result = _run(tmp_path, "--no-services", expect=1)
    assert "前置检查未通过" in result.stderr


# ---------------------------------------------------------------------------
# the entry point everything else depends on
# ---------------------------------------------------------------------------

def test_the_entry_point_finds_its_checkout_through_a_symlink(tmp_path):
    """The old shim computed `$BIN_DIR/../share/claude-fleet`, which is right on
    exactly one machine and silently wrong everywhere else. Resolving its own
    symlink works wherever the checkout lives."""
    link = tmp_path / "quotapool"
    link.symlink_to(REPO / "bin" / "quotapool")

    result = subprocess.run([str(link), "--version"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip(), "version should print"


def test_the_old_command_name_still_works(tmp_path):
    """A running install has `fleet` in its launchd plists, its wrapper scripts
    and its shell shim. The name collides with JetBrains Fleet and Rancher
    Fleet, which is why it is not the name any more — but breaking a live system
    to tidy that up would be the wrong trade."""
    link = tmp_path / "fleet"
    link.symlink_to(REPO / "bin" / "fleet")
    result = subprocess.run([str(link), "--version"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


def test_a_token_that_starts_with_a_hyphen_still_installs(home, tmp_path):
    """About one fresh install in sixty-four used to fail here.

    `secrets.token_urlsafe` draws from the URL-safe base64 alphabet, which
    includes `-`, so roughly 1/64 of generated coordinator tokens begins with
    one. Passed as `--token $token`, argparse reads it as an option and exits 2
    with `--token: expected one argument` — no explanation of what actually went
    wrong, on the very first thing a new user runs.

    It reached this suite as flakiness: twelve standalone runs clean, then two
    failures in ten under pytest. A 1.5% failure that looks like a flaky test is
    the most likely kind of bug to be shipped.

    The shim forces the case rather than waiting for it, which is the only way
    a probabilistic bug becomes a test.
    """
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    real = shutil.which("python3")
    shim = shim_dir / "python3"
    shim.write_text(
        "#!/bin/sh\n"
        "# The token generator, and only it: everything else is the real one.\n"
        'case "$*" in\n'
        "  *'import secrets'*) echo '-leading-hyphen-token-aaaaaaaaaaaa'; exit 0 ;;\n"
        "esac\n"
        'exec "' + real + '" "$@"\n',
        encoding="utf-8")
    shim.chmod(0o755)

    result = subprocess.run(
        ["sh", str(INSTALLER), "--no-services"], cwd=str(REPO),
        capture_output=True, text=True, timeout=180,
        env={**_env(home),
             # The other value on that command line that does not come from this
             # script. Nobody's proxy URL starts with a hyphen, but the argument
             # is passed the same way and so is fixed the same way, and an
             # untested "while we're here" change is how the next one of these
             # gets introduced.
             "OUTBOUND_PROXY": "-not-a-real-proxy",
             "PATH": "%s:%s" % (shim_dir, os.environ["PATH"])})
    assert result.returncode == 0, result.stdout + result.stderr

    runtime = json.loads(
        (_config(home) / "runtime.json").read_text(encoding="utf-8"))
    assert runtime, "配置得真生成出来, 不是安装脚本没报错就算过"


def test_quotapool_does_not_spawn_an_interpreter_to_find_itself():
    """It runs on the path of every `claude` invocation. Paying a Python startup
    to compute a path would be paying it forever."""
    source = (REPO / "bin" / "quotapool").read_text(encoding="utf-8")
    body = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#"))
    assert body.count("python3") == 1, (
        "only the exec should start an interpreter"
    )


# ---------------------------------------------------------------------------
# 装了脚本, 却没有人去跑它
# ---------------------------------------------------------------------------

def _installable_labels():
    """Every launchd job a fresh install would end up with.

    Two sources, deliberately both: `install.sh` loops over the templates, and
    `install-timers.sh` writes two plists inline."""
    templates = {p.name[: -len(".plist.in")]
                 for p in (REPO / "packaging" / "launchd").glob("*.plist.in")}
    timers = (REPO / "packaging" / "launchd" / "install-timers.sh").read_text(
        encoding="utf-8")
    for label in ("com.claude-fleet.health", "com.claude-fleet.maintenance"):
        if label in timers:
            templates.add(label)
    return templates


def test_every_job_this_repo_depends_on_can_actually_be_installed():
    """`scripts/fleet-tokens` shipped, was published, was in the release
    allow-list — and nothing anywhere started it. It runs every sixty seconds on
    both of this project's hosts from a plist that exists in no repository, so a
    machine rebuilt from this checkout would silently stop collecting quota,
    which is the input every routing decision is made from.

    `fleet-probe-auth` was worse: its plist was written on 2026-08-02 and never
    loaded once, so it has never run. `fleet-health` calls its absence DEGRADED,
    which means every fresh install is degraded out of the box — and a check
    that is red on a healthy machine is a check people learn to ignore.

    Both are the same shape as everything else found this session: the artifact
    exists, nothing wires it up, and the only complaint goes to a log file.
    """
    installable = _installable_labels()
    required = {
        "com.claude-fleet.coordinator": "控制面, 不装就什么都没有",
        "com.claude-fleet.dataplane": "数据面, 同上",
        "com.claude-fleet.tokens": "额度读数; 停了之后调度依据会静默冻结",
        "com.claude-fleet.authprobe": "fleet-health 把它的缺席直接算成 DEGRADED",
        "com.claude-fleet.markgood": "没有它就永远不会有新的已知良好点",
        "com.claude-fleet.watchdog": "坏了自动回滚, 以及告诉人",
        "com.claude-fleet.quotasync": "不在路由的那台机器靠它更新额度",
    }
    missing = {label: why for label, why in required.items()
               if label not in installable}
    assert not missing, "有脚本没有安装路径:\n" + "\n".join(
        "  %s — %s" % (label, why) for label, why in missing.items())


@pytest.mark.skipif(shutil.which("plutil") is None,
                    reason="plutil 是 macOS 的; Linux 上 launchd 模板不适用")
def test_every_template_survives_plutil(tmp_path):
    """A malformed plist is accepted silently by the file system and refused by
    launchd, so the job simply never runs — the same failure being fixed here,
    arrived at a different way.

    The templates are linted rather than a generated install: `--no-services`
    skips generation entirely (it cannot be tested against a scratch HOME
    because `launchctl` acts on the real login session), so waiting for a real
    install to catch this would mean catching it on a user's machine.
    """
    for template in sorted((REPO / "packaging" / "launchd").glob("*.plist.in")):
        rendered = tmp_path / template.name.replace(".in", "")
        rendered.write_text(
            template.read_text(encoding="utf-8").replace("@HOME@", str(tmp_path)),
            encoding="utf-8")
        result = subprocess.run(["plutil", "-lint", str(rendered)],
                                capture_output=True, text=True)
        assert result.returncode == 0, "%s: %s" % (template.name, result.stdout)
