"""The escape hatch has to work on the day everything else does not.

`claude` is a symlink to `claude-fleet-shell`, which is `exec fleet run`. So a
broken Fleet does not degrade to a slower Claude — it takes `claude` with it,
and the tool you would reach for to diagnose the breakage is the thing that
broke. That is the loop the owner named: "改了就全坏, 坏了又因为坏了没法修".

Everything here defends one of two properties:

1. It does not depend on Fleet. Not a little. Not "only for the nice error
   message". Any import of `claude_fleet` puts the failure it exists to survive
   back into its own startup path.
2. It does not create the failure it is rescuing you from. OAuth refresh tokens
   are single-use, and two refreshers racing produce
   `invalid_grant: Refresh token not found or invalid` — the exact line that
   appeared 588 times in this machine's log between 08-06 and 08-09 and retired
   four accounts from rotation.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parent.parent / "scripts" / "claude-direct"


def _module():
    spec = importlib.util.spec_from_loader("claude_direct", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__dict__["__file__"] = str(SOURCE)
    source = SOURCE.read_text(encoding="utf-8")
    marker = 'if __name__ == "__main__":'
    if marker in source:
        source = source[: source.index(marker)]
    exec(compile(source, str(SOURCE), "exec"), module.__dict__)
    return module


def _config(tmp_path, accounts):
    path = tmp_path / "teamclaude.json"
    path.write_text(json.dumps({"accounts": accounts}), encoding="utf-8")
    return path


def _oauth(name, uuid, *, access="sk-ant-oat01-live", expires_ms=None, **extra):
    entry = {
        "type": "oauth",
        "name": name,
        "accountUuid": uuid,
        "accessToken": access,
        "refreshToken": "sk-ant-ort01-refresh",
    }
    if expires_ms is not None:
        entry["expiresAt"] = expires_ms
    entry.update(extra)
    return entry


# ---------------------------------------------------------------------------
# property 1: no dependency on the thing it rescues you from
# ---------------------------------------------------------------------------

def test_it_never_imports_fleet():
    """The single hard rule, asserted against the source text.

    A reviewer adding `from claude_fleet.db import FleetDB` for one tidy lookup
    would not notice they had made the escape hatch require a working Fleet.
    Measured: with `claude_fleet` replaced by a module that raises on import,
    the tool still resolved an account, injected the right token and exec'd.
    """
    source = SOURCE.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "import claude_fleet" not in code
    assert "from claude_fleet" not in code
    assert "sys.path" not in code, (
        "putting the repo's src/ on sys.path is how the dependency comes back "
        "in without an import statement ever being written"
    )


def test_it_only_needs_the_standard_library():
    """Anything third-party is another thing that can be missing at 3am."""
    source = SOURCE.read_text(encoding="utf-8")
    imports = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith("import ")
    }
    assert imports <= {
        "argparse", "errno", "fcntl", "json", "os", "socket", "sys", "time",
        "urllib",
    }


# ---------------------------------------------------------------------------
# property 2: it must not manufacture the invalid_grant it is rescuing from
# ---------------------------------------------------------------------------

def test_a_live_token_is_used_without_refreshing(tmp_path, monkeypatch):
    """The common path must touch nothing: no network, no write, no lock.

    Refreshing when it was not needed is not a harmless extra step — it rotates
    a single-use token and invalidates the data plane's copy.
    """
    module = _module()
    account = _oauth("live@", "uuid-live",
                     expires_ms=(module.time.time() + 7200) * 1000)

    def explode(*a, **k):
        raise AssertionError("refreshed a token that had two hours left")

    monkeypatch.setattr(module, "_refresh", explode)
    monkeypatch.setattr(module, "_write_back", explode)

    assert module._token_for(account, allow_refresh=False) == "sk-ant-oat01-live"


def test_it_refuses_to_race_the_data_plane_for_a_refresh(tmp_path, monkeypatch):
    """The defect this whole guard exists for.

    An expired token plus a listening data plane means two processes are about
    to refresh the same single-use credential. That is what produced 588 false
    "needs re-login" lines and retired four healthy accounts. Refusing and
    explaining beats succeeding half the time.
    """
    module = _module()
    account = _oauth("stale@", "uuid-stale",
                     expires_ms=(module.time.time() - 3600) * 1000)

    monkeypatch.setattr(module, "_dataplane_listening", lambda port=13456: True)
    monkeypatch.setattr(module, "_refresh", lambda *a, **k: pytest.fail(
        "refreshed while the data plane was up"))

    with pytest.raises(SystemExit):
        module._token_for(account, allow_refresh=False)


def test_force_refresh_is_the_only_way_past_that(monkeypatch):
    """The operator may know the data plane is wedged. It must be a deliberate
    act, never a fallback the tool takes on its own."""
    module = _module()
    account = _oauth("stale@", "uuid-stale",
                     expires_ms=(module.time.time() - 3600) * 1000)

    monkeypatch.setattr(module, "_dataplane_listening", lambda port=13456: True)
    monkeypatch.setattr(module, "_refresh", lambda *a, **k: {
        "accessToken": "sk-ant-oat01-fresh",
        "refreshToken": "sk-ant-ort01-rotated",
        "expiresAt": (module.time.time() + 3600) * 1000,
    })
    monkeypatch.setattr(module, "_write_back", lambda *a, **k: None)

    assert module._token_for(account, allow_refresh=True) == "sk-ant-oat01-fresh"


def test_a_rotated_token_is_written_back_atomically(tmp_path, monkeypatch):
    """Not writing back is how you manufacture the next invalid_grant: the
    endpoint has already voided the old refresh token, so whoever reads the
    file next is holding a dead credential."""
    module = _module()
    config = _config(tmp_path, [
        _oauth("other@", "uuid-other", access="sk-ant-oat01-untouched"),
        _oauth("target@", "uuid-target"),
    ])
    monkeypatch.setattr(module, "CONFIG", config)

    module._write_back("uuid-target", {
        "accessToken": "sk-ant-oat01-fresh",
        "refreshToken": "sk-ant-ort01-rotated",
        "expiresAt": 1234567890000,
    })

    written = json.loads(config.read_text(encoding="utf-8"))
    target = [a for a in written["accounts"] if a["accountUuid"] == "uuid-target"][0]
    other = [a for a in written["accounts"] if a["accountUuid"] == "uuid-other"][0]
    assert target["accessToken"] == "sk-ant-oat01-fresh"
    assert target["refreshToken"] == "sk-ant-ort01-rotated"
    assert other["accessToken"] == "sk-ant-oat01-untouched", (
        "a write that rewrites the whole file must not disturb the accounts it "
        "was not asked to change"
    )


def test_the_written_config_keeps_credential_permissions(tmp_path, monkeypatch):
    """This file holds every account's OAuth tokens. A temp file created with
    the default umask and renamed into place would silently widen it — which
    already happened once on this machine, to ~/.claude.json, 600 to 664."""
    module = _module()
    config = _config(tmp_path, [_oauth("a@", "uuid-a")])
    os.chmod(config, 0o600)
    monkeypatch.setattr(module, "CONFIG", config)

    module._write_back("uuid-a", {"accessToken": "sk-ant-oat01-new"})

    assert oct(config.stat().st_mode & 0o777) == "0o600"


def test_writing_back_an_account_that_vanished_changes_nothing(tmp_path, monkeypatch):
    """If the config was rewritten while we were on the network, the row we
    meant to update may be gone. Appending it back would resurrect a deleted
    account; guessing a different row would corrupt it."""
    module = _module()
    config = _config(tmp_path, [_oauth("a@", "uuid-a")])
    monkeypatch.setattr(module, "CONFIG", config)
    before = config.read_text(encoding="utf-8")

    with pytest.raises(SystemExit):
        module._write_back("uuid-gone", {"accessToken": "x"})

    assert config.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# selection and display
# ---------------------------------------------------------------------------

def test_a_uuid_can_be_pasted_straight_back_in():
    """`--list` prints uuids, so pasting one back as the positional argument is
    the natural move. Rejecting it would be the tool blaming the user for doing
    what its own output invited."""
    module = _module()
    accounts = [_oauth("alpha@", "6c84761e-814b-4dc9"),
                _oauth("beta@", "9f9010ad-f4ac-4cf5")]
    assert module._select(accounts, "6c84761e", None)["name"] == "alpha@"


def test_two_orgs_under_one_email_must_be_distinguishable():
    """The reason `--account` exists. A name match here is ambiguous by
    construction, and picking one silently would route to the wrong
    subscription — different quota, different reserve, different everything."""
    module = _module()
    accounts = [
        _oauth("same@", "uuid-personal", orgName="same@'s Organization"),
        _oauth("same@", "uuid-corp", orgName="Acme Inc"),
    ]
    with pytest.raises(SystemExit):
        module._select(accounts, "same@", None)

    assert module._select(accounts, None, "uuid-corp")["accountUuid"] == "uuid-corp"


def test_a_personal_org_name_is_not_repeated_back_at_you():
    """"alice@example.com (alice@example.com's Organization)" is twice the width
    and no extra information; it pushed every other column off the screen."""
    module = _module()
    personal = _oauth("alice@example.com", "u1",
                      orgName="alice@example.com's Organization")
    corporate = _oauth("alice@example.com", "u2", orgName="Acme Inc")

    assert module._label(personal) == "alice@example.com"
    assert module._label(corporate) == "alice@example.com (Acme Inc)"


# ---------------------------------------------------------------------------
# binary selection
# ---------------------------------------------------------------------------

def test_a_failed_download_is_not_a_usable_version(tmp_path, monkeypatch):
    """A partial update leaves a zero-byte file behind — 2.1.224 on this
    machine. Picking it by version number gives an exec failure at the worst
    possible moment, which is the moment this tool is for."""
    module = _module()
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "2.1.223").write_bytes(b"#!/bin/sh\n")
    (versions / "2.1.223").chmod(0o755)
    (versions / "2.1.224").write_bytes(b"")
    (versions / "2.1.224").chmod(0o755)
    monkeypatch.setattr(module, "VERSIONS_DIR", versions)

    assert module._find_binary(None).name == "2.1.223"


def test_versions_are_ordered_numerically_not_lexically(tmp_path, monkeypatch):
    """String ordering puts 2.1.9 above 2.1.10."""
    module = _module()
    versions = tmp_path / "versions"
    versions.mkdir()
    for name in ("2.1.9", "2.1.10"):
        (versions / name).write_bytes(b"#!/bin/sh\n")
        (versions / name).chmod(0o755)
    monkeypatch.setattr(module, "VERSIONS_DIR", versions)

    assert module._find_binary(None).name == "2.1.10"


# ---------------------------------------------------------------------------
# a corrupt config is the exact situation this tool is for
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# the other half: a failed launch has to point at the exit
# ---------------------------------------------------------------------------

def test_a_failed_launch_names_the_way_out(capsys, monkeypatch):
    """A stack trace is not an answer to "what do I do now".

    `claude` is this command, so a launch failure leaves the owner with no
    session at all — including the one he would use to diagnose it.
    """
    from claude_fleet import cli

    def boom(_args):
        raise OSError("data plane socket vanished")

    monkeypatch.setattr(cli, "run_claude", boom)
    assert cli.main(["run", "--", "--version"]) == 78

    err = capsys.readouterr().err
    assert "claude-direct" in err, "the escape hatch has to be named at the exit"
    assert "fleet-rollback now" in err
    assert "data plane socket vanished" in err, (
        "the fault itself must survive — guidance that replaces the error "
        "trades one dead end for another"
    )


def test_a_failed_launch_does_not_silently_start_a_direct_session(capsys, monkeypatch):
    """Falling back would have to pick an account on the owner's behalf, and
    would leave him unscheduled — no desktop reserve, no rotation, and an error
    instead of a wait when the window runs out."""
    from claude_fleet import cli

    monkeypatch.setattr(cli, "run_claude", lambda _a: (_ for _ in ()).throw(
        RuntimeError("nope")))
    monkeypatch.setattr(cli.os, "execve", lambda *a, **k: pytest.fail(
        "launched something on the owner's behalf after a failure"))

    assert cli.main(["run", "--", "--version"]) == 78


def test_a_truncated_config_gets_an_explanation_not_a_traceback(tmp_path, monkeypatch):
    """A half-written credential file is a failure mode this file is supposed to
    rescue you from, so it must not answer it with a JSONDecodeError stack."""
    module = _module()
    config = tmp_path / "teamclaude.json"
    config.write_text('{"accounts": [{"type": "oa', encoding="utf-8")
    monkeypatch.setattr(module, "CONFIG", config)

    with pytest.raises(SystemExit):
        module._load_config()
