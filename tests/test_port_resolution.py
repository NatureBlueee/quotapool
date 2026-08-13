"""A verification instance must not be verified against production.

The plan for every risky change ahead of this one is the same: bring a second
Fleet up on different ports, exercise the change there, and only then touch the
machine people are using. That plan rests on the tools agreeing about which
instance they are looking at — and they did not.

Three different opinions lived in the tree at once:

    launcher.py   runtime.get("proxy_port", 3456)
    doctor.py     runtime.get("proxy_port", 3456)
    reconcile.py  def _dataplane_accounts(port=13456)  ← and the only caller
                                                         passed nothing

So `fleet reconcile`, pointed at an isolated runtime.json, went and asked
production's data plane which accounts it could serve, and reported the answer
as if it were about the instance under test. That is worse than an outright
failure: the output looks right.

3456 was also the wrong number to fall back to. From the README: "13456 是刻意
选择的隔离端口；本机 3456 已被其他浏览器辅助服务占用." A default that probes a
stranger's service is not a safe default.
"""

from __future__ import annotations

import json

import pytest

from claude_fleet import reconcile
from claude_fleet.launcher import (
    DEFAULT_COORDINATOR_PORT,
    DEFAULT_PROXY_PORT,
    coordinator_port,
    dataplane_url,
    proxy_port,
)


def test_the_ports_come_from_the_config():
    runtime = {"proxy_port": 23456, "coordinator_port": 27891}
    assert proxy_port(runtime) == 23456
    assert coordinator_port(runtime) == 27891
    assert dataplane_url(runtime, "/teamclaude/ready") == \
        "http://127.0.0.1:23456/teamclaude/ready"


def test_the_fallback_is_not_someone_elses_service():
    """3456 belongs to another program on the owner's machine. Falling back
    there means probing a stranger and believing the answer."""
    assert DEFAULT_PROXY_PORT == 13456
    assert DEFAULT_COORDINATOR_PORT == 17891
    assert proxy_port({}) == 13456
    assert coordinator_port({}) == 17891


def test_an_empty_port_is_treated_as_absent():
    """`"proxy_port": null` or 0 in a hand-edited config must not become port 0,
    which binds to a random free port and looks like it worked."""
    assert proxy_port({"proxy_port": None}) == DEFAULT_PROXY_PORT
    assert proxy_port({"proxy_port": 0}) == DEFAULT_PROXY_PORT


def test_reconcile_probes_the_instance_it_was_pointed_at(monkeypatch, tmp_path):
    """The one that actually reached production.

    `_dataplane_accounts()` took `port=13456` as a default and its only caller
    passed nothing, so it never read runtime.json at all. Verified against a live
    isolated config: it now asks 127.0.0.1:23456 rather than 13456.
    """
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({
        "proxy_port": 23456,
        "teamclaude_config": str(tmp_path / "tc.json"),
        "db_path": str(tmp_path / "fleet.sqlite3"),
    }), encoding="utf-8")
    monkeypatch.setenv("FLEET_RUNTIME_CONFIG", str(runtime))

    asked = []

    class _Opener:
        def open(self, url, timeout=None):
            asked.append(getattr(url, "full_url", url))
            raise OSError("nothing is listening, which is not what is under test")

    import urllib.request
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: _Opener())

    assert reconcile._dataplane_accounts() is None
    assert asked and "23456" in asked[0], (
        "a reconcile run pointed at an isolated instance must not describe "
        "production; got %r" % (asked or None,)
    )


def test_reconcile_falls_back_safely_without_a_config(monkeypatch):
    """No runtime.json is a normal state on a machine that has never installed
    Fleet. It must not raise out of a report."""
    monkeypatch.setenv("FLEET_RUNTIME_CONFIG", "/nonexistent/runtime.json")

    asked = []

    class _Opener:
        def open(self, url, timeout=None):
            asked.append(getattr(url, "full_url", url))
            raise OSError("no data plane")

    import urllib.request
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: _Opener())

    assert reconcile._dataplane_accounts() is None
    assert str(DEFAULT_PROXY_PORT) in asked[0]
