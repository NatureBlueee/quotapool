"""A shadow that touches production is not a shadow.

Everything queued after this — authentication fail-closed, the account identity
key, crash-safe credential writes — sits in the request path of a system people
are actively using. The plan for all of them is the same: rehearse somewhere
else first. That plan is worth exactly as much as the isolation is, so the
isolation is what gets tested here, not the convenience.

The failure mode being defended against is not "the shadow breaks". It is "the
shadow quietly works on production and reports success", which is how the port
literals were wrong for months: `doctor` fell back to 3456 and `reconcile`
hardcoded 13456, so a check aimed at one instance answered about another and
the output looked entirely correct.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parent.parent / "scripts" / "fleet-shadow"


def _module(monkeypatch=None, root=None):
    spec = importlib.util.spec_from_loader("fleet_shadow", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__dict__["__file__"] = str(SOURCE)
    source = SOURCE.read_text(encoding="utf-8")
    marker = 'if __name__ == "__main__":'
    if marker in source:
        source = source[: source.index(marker)]
    exec(compile(source, str(SOURCE), "exec"), module.__dict__)
    if root is not None:
        module.ROOT = Path(root)
    return module


# ---------------------------------------------------------------------------
# isolation, which is the entire value proposition
# ---------------------------------------------------------------------------

def test_it_refuses_to_run_on_the_production_ports(tmp_path):
    module = _module(root=tmp_path)
    module.COORDINATOR_PORT = 17891
    assert module._guard_against_production(module._paths()) is not None

    module.COORDINATOR_PORT = 27891
    module.PROXY_PORT = 13456
    assert module._guard_against_production(module._paths()) is not None


def test_it_refuses_to_use_the_production_database(monkeypatch, tmp_path):
    module = _module(root=tmp_path)
    real = Path.home() / ".local" / "state" / "claude-fleet"
    module.ROOT = real
    problem = module._guard_against_production(module._paths())
    assert problem is not None and "database" in problem


def test_it_refuses_to_write_into_the_production_config_dir(tmp_path):
    module = _module(root=tmp_path)
    module.ROOT = Path.home() / ".config" / "claude-fleet"
    problem = module._guard_against_production(module._paths())
    assert problem is not None and "config" in problem


def test_a_clean_shadow_root_passes_the_guard(tmp_path):
    module = _module(root=tmp_path / "shadow")
    assert module._guard_against_production(module._paths()) is None


# ---------------------------------------------------------------------------
# the credentials it generates
# ---------------------------------------------------------------------------

def test_no_real_credential_can_reach_the_shadow(tmp_path):
    """There must be no code path that copies production credentials in. A
    shadow able to spend a real subscription by accident is worse than no
    shadow, because it would do so while everyone believed it was inert."""
    source = SOURCE.read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#"))
    for forbidden in ("accessToken\"]", "refreshToken\"]", "credentials.json"):
        assert forbidden not in code

    module = _module(root=tmp_path / "shadow")
    module._write_config(module._paths())
    config = json.loads(module._paths()["teamclaude"].read_text(encoding="utf-8"))
    for account in config["accounts"]:
        assert "not-a-credential" in account["accessToken"]
        assert "not-a-credential" in account["refreshToken"]


def test_the_generated_fleet_contains_the_case_production_cannot_rehearse(tmp_path):
    """Two accounts sharing one accountUuid across two organisations.

    That is the exact shape the identity change has to get right, and there is
    no way to set it up on the real fleet without buying a second subscription
    in another org. Losing this from the generated config would remove the only
    place that change can be exercised before it ships.
    """
    module = _module(root=tmp_path / "shadow")
    module._write_config(module._paths())
    accounts = json.loads(
        module._paths()["teamclaude"].read_text(encoding="utf-8"))["accounts"]

    by_uuid = {}
    for account in accounts:
        by_uuid.setdefault(account["accountUuid"], set()).add(account["orgUuid"])
    assert any(len(orgs) > 1 for orgs in by_uuid.values()), (
        "the shadow must contain one accountUuid under two orgUuids"
    )


def test_the_generated_config_is_not_world_readable(tmp_path):
    """Fake credentials today; the file's permissions are what stop it becoming
    a habit that carries over to something real."""
    import os

    module = _module(root=tmp_path / "shadow")
    module._write_config(module._paths())
    for key in ("runtime", "teamclaude"):
        mode = os.stat(module._paths()[key]).st_mode & 0o777
        assert oct(mode) == "0o600"


def test_the_shadow_runtime_points_only_at_itself(tmp_path):
    module = _module(root=tmp_path / "shadow")
    module._write_config(module._paths())
    runtime = json.loads(module._paths()["runtime"].read_text(encoding="utf-8"))

    assert runtime["coordinator_port"] == module.COORDINATOR_PORT
    assert runtime["proxy_port"] == module.PROXY_PORT
    assert str(tmp_path) in runtime["db_path"]
    assert str(tmp_path) in runtime["teamclaude_config"]


def test_the_environment_it_hands_to_children_overrides_every_path(tmp_path):
    """Missing one variable means the child reads a production path while
    everything else about it looks isolated."""
    module = _module(root=tmp_path / "shadow")
    env = module._env(module._paths())

    for key in ("FLEET_RUNTIME_CONFIG", "FLEET_DB", "TEAMCLAUDE_CONFIG"):
        assert str(tmp_path) in env[key], key
    assert env["FLEET_COORDINATOR_PORT"] == str(module.COORDINATOR_PORT)
    assert env["FLEET_COORDINATOR_TOKEN"] == module.SHADOW_TOKEN
