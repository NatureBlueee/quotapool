"""`fleet run` and the data plane must resolve the SAME coordinator.

The outage this pins: the address was hardcoded to 127.0.0.1 inside
check_services while the data plane read `fleetCoordinator.url` from
teamclaude.json. Binding the coordinator to a tailnet address satisfied the data
plane and broke `fleet run` — which every session enters through, since
shims/claude is `exec fleet run` — so the host stopped working entirely with:

    coordinator is unavailable on 127.0.0.1:17891; refusing direct Claude access

Two components resolving different coordinators is a split control plane. It is
silent until something fails, and then it fails everywhere at once.
"""

from __future__ import annotations

import os

import pytest

from claude_fleet.launcher import (
    coordinator_endpoint,
    dataplane_endpoint,
)


def test_loopback_is_the_default_not_a_hardcode():
    assert coordinator_endpoint({}) == "http://127.0.0.1:17891"
    assert coordinator_endpoint({"coordinator_port": 9999}) == "http://127.0.0.1:9999"


def test_an_explicit_url_wins_over_host_and_port():
    runtime = {
        "coordinator_url": "http://100.124.86.125:17891",
        "coordinator_host": "127.0.0.1",
        "coordinator_port": 17891,
    }
    assert coordinator_endpoint(runtime) == "http://100.124.86.125:17891"


def test_a_remote_host_is_honoured():
    runtime = {"coordinator_host": "100.124.86.125", "coordinator_port": 17891}
    assert coordinator_endpoint(runtime) == "http://100.124.86.125:17891"


def test_a_trailing_slash_is_normalised():
    runtime = {"coordinator_url": "http://example.internal:17891/"}
    assert coordinator_endpoint(runtime) == "http://example.internal:17891"


def test_the_environment_override_wins(monkeypatch):
    """Used by shadow canaries, so a candidate coordinator can be exercised
    without touching the real config."""
    monkeypatch.setenv("FLEET_COORDINATOR_URL", "http://10.0.0.9:17891")
    runtime = {"coordinator_url": "http://100.124.86.125:17891"}
    assert coordinator_endpoint(runtime) == "http://10.0.0.9:17891"


def test_an_empty_value_falls_through_rather_than_producing_a_bad_url():
    assert coordinator_endpoint({"coordinator_url": "   "}) == "http://127.0.0.1:17891"
    assert coordinator_endpoint({"coordinator_host": ""}) == "http://127.0.0.1:17891"


def test_the_data_plane_is_always_local():
    """It holds this host's credentials and reaches Anthropic itself. A remote
    data plane address would mean executing with someone else's credentials —
    exactly what CROSS-HOST-AUDIT-001 established is not possible."""
    assert dataplane_endpoint({"proxy_port": 13456}) == "http://127.0.0.1:13456"
    # Even if a coordinator override is set, the data plane stays local.
    assert dataplane_endpoint(
        {"proxy_port": 13456, "coordinator_url": "http://100.124.86.125:17891"}
    ) == "http://127.0.0.1:13456"


def test_check_services_names_the_real_target_and_fails_closed():
    """The error must show where it actually looked, and must not fall back.

    Port 1 is reserved, so nothing listens there.
    """
    from claude_fleet.launcher import check_services

    runtime = {"coordinator_url": "http://127.0.0.1:1", "proxy_port": 13456}
    with pytest.raises(RuntimeError) as excinfo:
        check_services(runtime)
    message = str(excinfo.value)
    assert "http://127.0.0.1:1/health" in message, "must name the actual target"
    assert "refusing direct Claude access" in message, "must fail closed"


def test_no_token_can_leak_through_the_endpoint():
    """Endpoint resolution must never carry a credential — the URL ends up in
    error messages that get pasted into issues."""
    runtime = {"coordinator_url": "http://100.124.86.125:17891"}
    assert "@" not in coordinator_endpoint(runtime)
    assert "token" not in coordinator_endpoint(runtime).lower()
