"""runtime.json carries the writing machine's absolute paths.

Copied to the Harness Linux server each stale path fails differently, and none
of them fails in a way that names itself. The worst is `outbound_proxy`: on the
laptop it points at a local proxy on 127.0.0.1:1082, on the server nothing
listens there, and every upstream request then fails with connection errors that
read as Anthropic being unreachable.

These tests construct exactly that config rather than waiting to discover it on
a machine where nobody is watching.
"""

from __future__ import annotations

from claude_fleet.launcher import runtime_portability_problems


def _fields(problems):
    return {item["field"] for item in problems}


def test_a_valid_local_config_reports_nothing(tmp_path):
    binary = tmp_path / "node"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    claude = tmp_path / "claude"
    claude.write_text("")

    problems = runtime_portability_problems({
        "node_binary": str(binary),
        "claude_binary": str(claude),
    })
    assert problems == []


def test_the_mac_config_on_a_server_names_every_broken_path(tmp_path):
    """The shape of this machine's runtime.json, evaluated where none of its
    paths exist.

    Deliberately NOT using the literal `/opt/homebrew/...` and
    `/home/someone/...` strings: on the laptop that wrote them they resolve, so
    the test would pass for the wrong reason here and fail for the wrong reason
    on the server. Non-existent paths under tmp_path are absent on every host,
    which is the condition actually under test.
    """
    absent = tmp_path / "does-not-exist"
    problems = runtime_portability_problems({
        "node_binary": str(absent / "bin" / "node"),
        "claude_binary": str(absent / "claude" / "versions" / "2.1.220"),
        # Port 1 is reserved; nothing will be listening on it.
        "outbound_proxy": "http://127.0.0.1:1",
    })
    assert _fields(problems) == {
        "node_binary", "claude_binary", "outbound_proxy",
    }


def test_a_dead_loopback_proxy_says_what_it_will_actually_break():
    problems = runtime_portability_problems({
        "outbound_proxy": "http://127.0.0.1:1",
    })
    assert len(problems) == 1
    impact = problems[0]["impact"]
    assert "EVERY upstream request fails" in impact
    # The reason this needs saying: the symptom points at the wrong component.
    assert "Anthropic being unreachable" in impact


def test_a_remote_proxy_is_not_probed():
    """Only loopback is checked. A remote egress proxy may legitimately be
    unreachable from wherever doctor happens to run, and guessing would produce
    a false failure on a working server."""
    problems = runtime_portability_problems({
        "outbound_proxy": "http://proxy.internal.example:3128",
    })
    assert problems == []


def test_an_empty_config_is_not_a_problem():
    """A field that is absent or empty is not misconfigured — the bootstrap
    legitimately omits outbound_proxy on a host with direct egress."""
    assert runtime_portability_problems({}) == []
    assert runtime_portability_problems({"outbound_proxy": ""}) == []


def test_a_non_executable_node_is_reported(tmp_path):
    binary = tmp_path / "node"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o644)  # present but not executable
    problems = runtime_portability_problems({"node_binary": str(binary)})
    assert _fields(problems) == {"node_binary"}


def test_version_pinning_is_called_out(tmp_path):
    """claude_binary pins an exact version directory, so it breaks on the next
    upgrade even on the machine that wrote it."""
    problems = runtime_portability_problems({
        "claude_binary": str(tmp_path / "versions" / "2.1.220"),
    })
    assert "breaks on upgrade" in problems[0]["impact"]
