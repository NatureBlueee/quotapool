"""A service restart must not cost the user a command.

Failing closed is right: starting Claude without a coordinator means starting it
unfenced, and that is worse than not starting it. But refusing on the FIRST
failed probe turns a four-second restart into "the user re-runs it", which is
the failure mode this fleet exists to remove.

Found by accident, which is the honest version of how. The live outage test —
stop the coordinator, confirm an in-flight request waits rather than dies — never
reached the data plane at all: `fleet run` refused before starting Claude. Two
different paths share one symptom, and only the in-session one had been fixed.
"""

from __future__ import annotations

import urllib.error

import pytest

from claude_fleet import launcher


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _runtime():
    return {"coordinator_port": 17891, "proxy_port": 13456}


def test_a_restart_window_is_survived(monkeypatch):
    """Down for the first two probes, up on the third. The launch proceeds."""
    attempts = {"n": 0}

    def probe(url, timeout=2):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise urllib.error.URLError("connection refused")
        return _Response()

    monkeypatch.setattr(launcher.urllib.request, "urlopen", probe)
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)

    launcher.check_services(_runtime())          # must not raise
    assert attempts["n"] >= 3


def test_a_service_that_is_really_down_still_refuses(monkeypatch):
    """The guard is not weakened, only given a grace period.

    Starting Claude with no coordinator means starting it unfenced. Retrying
    for ever would trade one bad outcome for a worse one — a launch that hangs
    with no explanation.
    """
    def probe(url, timeout=2):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(launcher.urllib.request, "urlopen", probe)
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)

    with pytest.raises(RuntimeError) as caught:
        launcher.check_services(_runtime())
    assert "refusing direct Claude access" in str(caught.value)
    assert "attempts" in str(caught.value), (
        "the message must say it retried, or the next person reads a single "
        "failed probe into it and re-tightens the loop"
    )


def test_a_healthy_service_is_probed_once(monkeypatch):
    """No added latency on the normal path."""
    attempts = {"n": 0}

    def probe(url, timeout=2):
        attempts["n"] += 1
        return _Response()

    monkeypatch.setattr(launcher.urllib.request, "urlopen", probe)
    monkeypatch.setattr(launcher.time, "sleep", lambda _: pytest.fail(
        "a healthy fleet must never sleep in the launch path"))

    launcher.check_services(_runtime())
    assert attempts["n"] == 2, "one probe per service, no retries"
