"""Shared fixtures.

The control plane is directly testable with no refactor and no mocking:
``FleetDB(path)`` runs its own ``initialize()``, so pointing it at a tmpdir
gives a real schema-v3 database. Every test here exercises the same code the
coordinator runs, not a reimplementation of it.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_fleet.control import FleetControl  # noqa: E402
from claude_fleet.db import FleetDB  # noqa: E402


@pytest.fixture
def db(tmp_path):
    return FleetDB(str(tmp_path / "fleet.sqlite3"))


@pytest.fixture
def control(db):
    return FleetControl(db)


@pytest.fixture
def now():
    return time.time()


@pytest.fixture
def coordinator_server(tmp_path):
    """A real coordinator, on a real socket, answering real HTTP.

    `serve_coordinator()` was never called anywhere in this test suite — grep it
    and the only hits are its definition and the production entry point. So
    every check that only exists at the HTTP boundary went unverified:
    authentication, the endpoint allow-list, body-size limits, the JSON error
    shapes. That is the layer the next changes touch, and the layer whose
    failure mode is "the whole fleet returns 401 and the soak looks clean".

    Yields (base_url, token). Port 0 lets the OS pick a free one, so this can
    never collide with the production coordinator on 17891.
    """
    import threading

    from claude_fleet.coordinator import serve_coordinator

    token = "test-token-not-a-credential"
    server = serve_coordinator(str(tmp_path / "fleet.sqlite3"),
                               host="127.0.0.1", port=0, token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1], token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def quota(control, account_id, *, weekly=None, five_hour=None,
          weekly_reset=None, five_reset=None):
    """Set an account's observed quota the way the coordinator does."""
    control.report_quota(
        account_id,
        five_hour_used=five_hour,
        five_hour_reset_at=five_reset,
        weekly_used=weekly,
        weekly_reset_at=weekly_reset,
    )
