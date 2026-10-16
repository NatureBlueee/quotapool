"""The HTTP boundary had no tests at all.

`serve_coordinator()` appears twice in this repository: where it is defined, and
where production calls it. Nowhere in `tests/`. So everything that only exists
at that boundary was running unverified — authentication, the endpoint
allow-list, the body-size limit, the error shapes.

That gap matters more than the usual "some code is untested", because of what
the untested code does when it breaks. Authentication failing closed turns every
request in the fleet into a 401 — and the known-good machinery could not see it
(`/health` takes no credentials, and a fleet making no routing decisions writes
no slow-decision rows, so the soak reads perfectly clean). The next change on
the list is to that exact code path.

TEST-MATRIX.md has been honest about this: "没有穿过真实数据面的 E2E". This is
the control-plane half of closing that.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest


def test_coordinator_refuses_duplicate_listener(tmp_path):
    from claude_fleet.coordinator import serve_coordinator

    first = serve_coordinator(str(tmp_path / "first.sqlite3"), port=0)
    host, port = first.server_address
    try:
        with pytest.raises(OSError):
            serve_coordinator(str(tmp_path / "second.sqlite3"), host=host, port=port)
    finally:
        first.server_close()


def _post(base, path, payload, token=None, raw=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer %s" % token
    body = raw if raw is not None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(base + path, data=body, method="POST",
                                     headers=headers)
    # Never inherit a proxy: the fleet sets HTTPS_PROXY to its own data plane,
    # and routing a loopback test through it would test the wrong thing.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw_body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw_body)
        except json.JSONDecodeError:
            return exc.code, raw_body


def _get(base, path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base + path, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


# ---------------------------------------------------------------------------
# authentication — the regression the known-good machinery is blind to
# ---------------------------------------------------------------------------

def test_a_request_without_a_token_is_refused(coordinator_server):
    base, _token = coordinator_server
    status, _ = _post(base, "/v1/pin/check", {"account_id": "acct"})
    assert status == 401


def test_a_request_with_the_wrong_token_is_refused(coordinator_server):
    base, _token = coordinator_server
    status, _ = _post(base, "/v1/pin/check", {"account_id": "acct"},
                      token="not-the-token")
    assert status == 401


def test_the_right_token_reaches_the_handler(coordinator_server):
    """Not 200 — `pin_check` on an unknown account has its own opinion — but
    definitively past authentication. That distinction is the whole probe:
    `fleet-rollback` treats 401 as fatal and anything else as serving."""
    base, token = coordinator_server
    status, _ = _post(base, "/v1/pin/check", {"account_id": "acct"}, token=token)
    assert status != 401


def test_health_needs_no_credentials_and_that_is_the_trap(coordinator_server):
    """Documented deliberately. `/health` answering is what made a fleet with
    broken authentication look alive to every liveness check in the system, and
    it is why `fleet-rollback` now probes an authenticated endpoint instead."""
    base, _token = coordinator_server
    status, payload = _get(base, "/health")
    assert status == 200 and payload["ok"] is True
    assert payload["maintenance"]["enabled"] is True


def test_automatic_maintenance_records_a_bounded_summary(monkeypatch, tmp_path):
    from claude_fleet import coordinator as module
    from claude_fleet.db import FleetDB

    monkeypatch.setattr(module.maintenance_module, "sweep", lambda db, now=None: {
        "events_removed": 12,
        "executions_abandoned": 3,
        "at": now,
        "dry_run": False,
    })
    status = {}
    module._run_maintenance_once(
        FleetDB(str(tmp_path / "fleet.sqlite3")), status, now=1000,
    )
    assert status["running"] is False
    assert status["lastCompletedAt"] == 1000
    assert status["lastResult"] == {
        "events_removed": 12,
        "executions_abandoned": 3,
    }


def test_failed_maintenance_retries_soon_without_busy_loop(monkeypatch, tmp_path):
    from claude_fleet import coordinator as module
    from claude_fleet.db import FleetDB

    attempts = []

    def flaky_sweep(db, now=None):
        attempts.append(db)
        if len(attempts) == 1:
            raise OSError("temporary database access failure")
        return {"events_removed": 0}

    class StopAfterRecovery:
        def __init__(self):
            self.delays = []

        def is_set(self):
            return False

        def wait(self, seconds):
            self.delays.append(seconds)
            return len(self.delays) == 3

    monkeypatch.setattr(module.maintenance_module, "sweep", flaky_sweep)
    stop = StopAfterRecovery()
    status = {}
    module._maintenance_loop(
        FleetDB(str(tmp_path / "fleet.sqlite3")), status, stop,
        initial_delay=1, interval=6 * 60 * 60,
    )
    assert stop.delays == [1, 5 * 60, 6 * 60 * 60]
    assert status["consecutiveFailures"] == 0
    assert status["lastError"] is None
    assert status["lastCompletedAt"] is not None


def test_maintenance_retry_backoff_is_capped(monkeypatch, tmp_path):
    from claude_fleet import coordinator as module
    from claude_fleet.db import FleetDB

    def failing_sweep(db, now=None):
        raise OSError("database unavailable")

    class StopAfterFiveFailures:
        def __init__(self):
            self.delays = []

        def is_set(self):
            return False

        def wait(self, seconds):
            self.delays.append(seconds)
            return len(self.delays) == 6

    monkeypatch.setattr(module.maintenance_module, "sweep", failing_sweep)
    stop = StopAfterFiveFailures()
    status = {}
    module._maintenance_loop(
        FleetDB(str(tmp_path / "fleet.sqlite3")), status, stop,
        initial_delay=1, interval=6 * 60 * 60,
    )
    assert stop.delays == [1, 300, 600, 1200, 1800, 1800]
    assert status["consecutiveFailures"] == 5
    assert "lastCompletedAt" not in status


def test_a_coordinator_with_no_token_serves_but_counts_it(monkeypatch, tmp_path):
    """`if not expected: return True` authenticated nothing at all.

    On this machine the service wrapper does set FLEET_COORDINATOR_TOKEN — an
    unauthenticated request measurably gets 401 — but nothing required that, and
    the packaging scripts never set it, so anyone installing from source ran the
    fleet's routing wide open to any local process.

    Audit rather than an immediate refusal, for the same reason the data plane
    has the two modes: turning a misconfiguration straight into a total outage
    is how the worst day here started.
    """
    import threading

    from claude_fleet import coordinator as module
    from claude_fleet.coordinator import serve_coordinator

    module.AUTH_AUDIT["unauthenticated_serves"] = 0
    monkeypatch.delenv("FLEET_AUTH_MODE", raising=False)

    server = serve_coordinator(str(tmp_path / "f.sqlite3"), port=0, token=None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = "http://127.0.0.1:%d" % server.server_address[1]
        status, _ = _post(base, "/v1/pin/check", {"account_id": "x"})
        assert status != 401, "audit mode must not change behaviour"
        assert module.AUTH_AUDIT["unauthenticated_serves"] >= 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_enforce_refuses_a_coordinator_that_has_no_token(monkeypatch, tmp_path):
    """The point of the flip. A coordinator that cannot authenticate must stop
    answering rather than answer everyone."""
    import threading

    from claude_fleet.coordinator import serve_coordinator

    monkeypatch.setenv("FLEET_AUTH_MODE", "enforce")
    server = serve_coordinator(str(tmp_path / "f.sqlite3"), port=0, token=None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = "http://127.0.0.1:%d" % server.server_address[1]
        assert _post(base, "/v1/pin/check", {"account_id": "x"})[0] == 401
        # Liveness stays open on purpose — it carries nothing, and every
        # supervisor in the system depends on it.
        assert _get(base, "/health")[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_configured_token_is_enforced_in_both_modes(monkeypatch,
                                                       coordinator_server):
    """Audit must not weaken a coordinator that IS configured. It only covers
    the unconfigured case."""
    base, _token = coordinator_server
    for mode in ("audit", "enforce"):
        monkeypatch.setenv("FLEET_AUTH_MODE", mode)
        assert _post(base, "/v1/pin/check", {"account_id": "x"},
                     token="wrong")[0] == 401, mode


# ---------------------------------------------------------------------------
# the endpoint allow-list
# ---------------------------------------------------------------------------

def test_an_unknown_path_is_404_before_authentication(coordinator_server):
    """Order matters: the allow-list check runs first, so an unknown path does
    not become an oracle for whether a guessed token was right."""
    base, _token = coordinator_server
    status, _ = _post(base, "/v1/anything/else", {})
    assert status == 404


def test_get_is_only_health(coordinator_server):
    base, _token = coordinator_server
    assert _get(base, "/v1/route/decide")[0] == 404


# ---------------------------------------------------------------------------
# malformed input
# ---------------------------------------------------------------------------

def test_an_empty_body_is_rejected(coordinator_server):
    base, token = coordinator_server
    status, _ = _post(base, "/v1/route/decide", None, token=token, raw=b"")
    assert status == 413


def test_an_oversized_body_is_refused_and_the_server_survives(coordinator_server):
    """A request body is attacker-controlled in length, so the limit is what
    stops one request becoming a memory problem for the whole fleet.

    The observable property is deliberately not "the client sees 413". Measured:
    the server answers and closes without draining the rest of the body — which
    is the behaviour you want, since reading it would be the very thing being
    refused — and the client sees a broken pipe instead. Asserting on the status
    code would be asserting that the server politely reads 4 MB it has already
    decided to reject.

    What must hold is that the request does not succeed and the coordinator is
    still serving afterwards.
    """
    base, token = coordinator_server
    huge = json.dumps({"session_id": "s", "pad": "x" * (4 * 1024 * 1024)})

    try:
        status, _ = _post(base, "/v1/route/decide", None, token=token,
                          raw=huge.encode("utf-8"))
        assert status == 413
    except (urllib.error.URLError, ConnectionError):
        pass          # refused mid-upload, which is the stronger behaviour

    assert _get(base, "/health")[0] == 200, "one oversized body is not an outage"


def test_a_non_object_body_is_rejected(coordinator_server):
    base, token = coordinator_server
    status, _ = _post(base, "/v1/route/decide", None, token=token,
                      raw=b'["not", "an", "object"]')
    assert status >= 400


def test_broken_json_does_not_take_the_server_down(coordinator_server):
    """And the next request still works — a handler that dies on bad input
    would turn one malformed request into an outage."""
    base, token = coordinator_server
    assert _post(base, "/v1/route/decide", None, token=token,
                 raw=b"{not json")[0] >= 400
    assert _get(base, "/health")[0] == 200


# ---------------------------------------------------------------------------
# a real routing decision, end to end over HTTP
# ---------------------------------------------------------------------------

def test_a_routing_decision_travels_the_whole_path(coordinator_server):
    """Register an account through the same server, then ask it to route.

    This is the first test in the suite where the answer comes back over a
    socket rather than from a direct method call, which means it is the first
    one that would notice the request never reaching the handler at all.
    """
    base, token = coordinator_server
    accounts = [{
        "id": "acct-1", "name": "acct-1", "type": "oauth", "index": 0,
        "account_uuid": "acct-1", "priority": 0, "disabled": False,
        "status": "active", "eligible": True,
        "quota": {"unified7d": 0.1, "unified5h": 0.1},
    }]

    status, payload = _post(base, "/v1/route/decide", {
        "session_id": "session-1",
        "holder": "holder-1",
        "generation": 1,
        "workload_class": "interactive",
        "accounts": accounts,
    }, token=token)

    assert status == 200, payload
    assert payload.get("account_id") == "acct-1", payload


def test_desktop_workload_routes_through_the_interactive_policy(coordinator_server):
    base, token = coordinator_server
    status, payload = _post(base, "/v1/route/decide", {
        "session_id": "desktop-session",
        "holder": "desktop-window",
        "generation": 1,
        "workload_class": "desktop",
        "accounts": [{
            "id": "desktop-account", "name": "desktop-account", "type": "oauth",
            "index": 0, "account_uuid": "desktop-account", "priority": 0,
            "disabled": False, "status": "active", "eligible": True,
            "quota": {"unified7d": 0.1, "unified5h": 0.1},
        }],
    }, token=token)

    assert status == 200, payload
    assert payload.get("account_id") == "desktop-account", payload


def test_a_spent_fleet_answers_no_capacity_rather_than_failing(coordinator_server):
    """The owner's first rule: waiting is fine, erroring is the violation.

    A fleet with nothing left must give a deliberate answer the data plane can
    act on — 409 `no_capacity`, which `fleet-coordinator.js` turns into a parked
    request and a held connection. What it must never do is 5xx, because that
    reaches the session as a failure instead of a wait.
    """
    base, token = coordinator_server
    status, payload = _post(base, "/v1/route/decide", {
        "session_id": "session-2",
        "holder": "holder-2",
        "generation": 1,
        "workload_class": "interactive",
        "accounts": [{
            "id": "spent", "name": "spent", "type": "oauth", "index": 0,
            "account_uuid": "spent", "priority": 0, "disabled": False,
            "status": "active", "eligible": True,
            "quota": {"unified7d": 1.0, "unified5h": 1.0},
        }],
    }, token=token)

    assert status == 409, payload
    assert payload.get("error") == "no_capacity", payload
    assert status < 500, "a spent fleet is a wait, not a server failure"
