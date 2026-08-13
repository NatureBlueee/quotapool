"""It could be switched off, but not on, and the failure was silent.

Two switches, and `fleet` exposed the wrong one. The one that decides anything
is `disabled` in `teamclaude.json`, which the data plane reads. The control
plane has its own `accounts.state`, and that is what `fleet account
enable/disable` wrote. `fleet account list` read the first one.

Off worked by accident: the control plane stopped choosing the account, which is
enough. On did not — the control plane marked it healthy, the data plane went on
reporting `disabled: true` on every routing request, and the coordinator put it
straight back into the exclusion set. No error anywhere.

That is what 「手动调好像也调不了」 was, exactly.

And `disabled` was carrying three meanings at once. Measured: four accounts
switched off, and 100 hours later their access tokens were still the ones from
the moment they were switched off, their quota readings 64 hours stale. Turning
one back on would have meant turning on something nobody could say anything
about.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from claude_fleet.accounts import set_mode
from claude_fleet.db import FleetDB


@pytest.fixture()
def fleet(tmp_path, monkeypatch):
    credentials = tmp_path / "teamclaude.json"
    credentials.write_text(json.dumps({"accounts": [
        {"type": "oauth", "name": "alpha", "accountUuid": "u-alpha",
         "accessToken": "x", "refreshToken": "r"},
        {"type": "oauth", "name": "beta", "accountUuid": "u-beta",
         "accessToken": "y", "refreshToken": "r2"},
    ]}), encoding="utf-8")
    os.chmod(credentials, 0o600)

    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({
        "teamclaude_config": str(credentials),
        "db_path": str(tmp_path / "f.sqlite3"),
        # Nothing listens here; the reload is reported as unreachable, which is
        # the honest outcome and must not fail the operation.
        "proxy_port": 29999,
    }), encoding="utf-8")
    monkeypatch.setenv("FLEET_RUNTIME_CONFIG", str(runtime))
    return FleetDB(str(tmp_path / "f.sqlite3")), credentials


def _entry(credentials, name):
    config = json.loads(credentials.read_text(encoding="utf-8"))
    return [a for a in config["accounts"] if a["name"] == name][0]


# ---------------------------------------------------------------------------
# the switch that decides
# ---------------------------------------------------------------------------

def test_pausing_writes_the_switch_the_data_plane_reads(fleet):
    db, credentials = fleet
    set_mode(db, "alpha", "paused")
    assert _entry(credentials, "alpha")["disabled"] is True


def test_resuming_actually_resumes(fleet):
    """The direction that silently did nothing."""
    db, credentials = fleet
    set_mode(db, "alpha", "paused")
    set_mode(db, "alpha", "active")
    assert _entry(credentials, "alpha")["disabled"] is False


def test_paused_is_not_retired(fleet):
    """The distinction the freeze came from. A paused account keeps its token
    fresh and its quota read; a retired one is not worth spending anything on."""
    db, credentials = fleet
    set_mode(db, "alpha", "paused")
    assert _entry(credentials, "alpha")["retired"] is False

    set_mode(db, "alpha", "retired")
    entry = _entry(credentials, "alpha")
    assert entry["disabled"] is True and entry["retired"] is True


def test_it_records_who_and_when(fleet):
    """`disabledReason` and `disabledAt` did not exist, which is why "四个账号
    是谁关的、什么时候关的" had no answer anywhere."""
    db, credentials = fleet
    set_mode(db, "alpha", "paused")
    entry = _entry(credentials, "alpha")
    assert entry["disabledReason"] == "paused"
    assert entry["disabledAt"] > 0

    set_mode(db, "alpha", "active")
    assert "disabledReason" not in _entry(credentials, "alpha")


def test_the_control_plane_agrees(fleet):
    """Both switches, one command. Two that can disagree is the defect."""
    db, _credentials = fleet
    set_mode(db, "alpha", "paused")
    with db.transaction() as conn:
        row = conn.execute("SELECT state FROM accounts WHERE id=?",
                           ("u-alpha",)).fetchone()
    assert row is not None and row["state"] == "disabled"


def test_the_change_is_recorded_in_the_ledger(fleet):
    db, _credentials = fleet
    set_mode(db, "alpha", "retired")
    with db.transaction() as conn:
        rows = conn.execute("SELECT payload FROM events WHERE kind=?",
                            ("account.mode_set",)).fetchall()
    assert rows and json.loads(rows[-1]["payload"])["mode"] == "retired"


# ---------------------------------------------------------------------------
# not damaging anything else
# ---------------------------------------------------------------------------

def test_other_accounts_are_untouched(fleet):
    db, credentials = fleet
    set_mode(db, "alpha", "retired")
    assert "disabled" not in _entry(credentials, "beta")


def test_the_credential_file_stays_private(fleet):
    """It holds every account's OAuth tokens; a rewrite must not widen it."""
    db, credentials = fleet
    set_mode(db, "alpha", "paused")
    assert oct(os.stat(credentials).st_mode & 0o777) == "0o600"


def test_a_failed_write_leaves_no_temp_file(fleet, monkeypatch):
    db, credentials = fleet
    import claude_fleet.accounts as module

    monkeypatch.setattr(module.os, "replace", lambda *a: (_ for _ in ()).throw(
        OSError("disk full")))
    with pytest.raises(OSError):
        set_mode(db, "alpha", "paused")

    assert not list(credentials.parent.glob("*.tmp"))


def test_the_lock_is_released_even_when_the_write_fails(fleet, monkeypatch):
    """A lock left behind would block the data plane's own token refreshes —
    turning a failed operator command into a credential problem."""
    db, credentials = fleet
    import claude_fleet.accounts as module

    monkeypatch.setattr(module.os, "replace", lambda *a: (_ for _ in ()).throw(
        OSError("disk full")))
    with pytest.raises(OSError):
        set_mode(db, "alpha", "paused")

    assert not Path(str(credentials) + ".lock").exists()


def test_an_unknown_account_is_refused_by_name(fleet):
    db, _credentials = fleet
    with pytest.raises(RuntimeError) as caught:
        set_mode(db, "nobody", "paused")
    assert "nobody" in str(caught.value)


def test_an_unreachable_data_plane_is_reported_not_fatal(fleet):
    """The operator is often reaching for this precisely because the data plane
    is unwell. Writing the change and saying it takes effect on restart beats
    refusing to write it."""
    db, credentials = fleet
    result = set_mode(db, "alpha", "paused")
    assert _entry(credentials, "alpha")["disabled"] is True
    assert "落盘" in result["dataplane"]


def test_an_invalid_mode_is_refused(fleet):
    db, _credentials = fleet
    with pytest.raises(RuntimeError):
        set_mode(db, "alpha", "sortof")
