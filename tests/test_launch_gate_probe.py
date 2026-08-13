"""The launch gate asks whether the service answers, not whether it can report.

It probed `/teamclaude/status`, which serialises every account, every route and
every waiting session. Measured on a data plane that had been up five hours,
with the fleet healthy, the coordinator answering in 1ms and nothing waiting:

    11.257s   12.469s   20.145s   2.055s   0.292s   0.207s

The gate allows two seconds. So `fleet run` refused to start Claude, and the
auth probe reported the credential chain broken — both because a display
endpoint could not be rendered in time. Nothing was wrong with the service.

`/teamclaude/ready` answers from constants: no account manager, no session
tracker, no waiting map, no database. Its latency measures the one thing the
gate cares about, whether the event loop is turning.

The 404 case is not a detail. Deploying a control-plane change before the data
plane restarts is the normal order; if the gate refused on 404 it would block
every launch until the data plane came back, which is the gate breaking the
fleet in order to protect it.
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


def test_the_data_plane_is_probed_on_ready(monkeypatch):
    seen = []

    def probe(url, timeout=2):
        seen.append(url)
        return _Response()

    monkeypatch.setattr(launcher.urllib.request, "urlopen", probe)
    launcher.check_services(_runtime())

    assert any(url.endswith("/teamclaude/ready") for url in seen), seen
    assert not any(url.endswith("/teamclaude/status") for url in seen), (
        "the gate is back on the display endpoint; its latency is the cost of "
        "the report, not the health of the service"
    )


@pytest.mark.parametrize("code", [400, 404, 405, 500, 503])
def test_any_status_line_counts_as_answering(monkeypatch, code):
    """The gate asks whether the process is turning, not what it implements.

    This is the second bug in one change, and the more instructive one. The
    first version allowed 404 only, reasoning that a data plane predating
    `/ready` would not know the route. It returns 400 for an unknown path — so
    the moment the control plane deployed, every launch was refused, which is
    precisely the failure the compatibility branch was written to prevent.

    A data plane that is up but broken is not this gate's job. The old status
    probe never caught that either, and refusing to launch over it is worse
    than launching.
    """
    def probe(url, timeout=2):
        if url.endswith("/teamclaude/ready"):
            raise urllib.error.HTTPError(url, code, "no", {}, None)
        return _Response()

    monkeypatch.setattr(launcher.urllib.request, "urlopen", probe)
    monkeypatch.setattr(launcher.time, "sleep", lambda _: pytest.fail(
        "an HTTP status is an answer — retrying it wastes the launch window"))

    launcher.check_services(_runtime())          # must not raise


def test_a_dead_data_plane_still_refuses(monkeypatch):
    """The gate is not weakened, only pointed at a cheaper question."""
    def probe(url, timeout=2):
        if url.endswith("/teamclaude/ready"):
            raise urllib.error.URLError("connection refused")
        return _Response()

    monkeypatch.setattr(launcher.urllib.request, "urlopen", probe)
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)

    with pytest.raises(RuntimeError):
        launcher.check_services(_runtime())
