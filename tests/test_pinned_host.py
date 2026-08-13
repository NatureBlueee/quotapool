"""An /etc/hosts pin is right until Anthropic moves, and then it is silent.

AliDNS — the server's first nameserver — returns a poisoned answer for
platform.claude.com: 103.252.115.221 and a 2001::/32 Teredo address, neither of
which accepts a connection. It does NOT poison api.anthropic.com. So the fleet
kept serving requests normally while every new Claude process that touched the
other host died within seconds:

    Failed to connect to platform.claude.com: ERR_SOCKET_CLOSED

Two background agents, dead at 11:16:18 and 11:16:37, against a main session
that carried on working — which is why it reads as a Claude fault and is a DNS
one. The data plane logged `tunnel platform.claude.com:443 failed:` and stopped
there, omitting the single fact that would have named the cause: the address it
dialled.

The fix is a hosts entry. Pinning an address is a debt. What makes it checkable
is that both names resolve to the SAME address and only one of them is
poisoned, so the unpoisoned one is a live reference for whether the pin has
gone stale.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "fleet-health"
    spec = importlib.util.spec_from_loader("fleethealth", loader=None)
    module = importlib.util.module_from_spec(spec)
    # A script that resolves its own location needs `__file__`, and every
    # real import provides it. Omitting it here made the loader unfaithful
    # in exactly the way that hides a working script behind a NameError.
    module.__dict__["__file__"] = str(path)
    source = path.read_text(encoding="utf-8")
    marker = 'if __name__ == "__main__":'
    if marker in source:
        source = source[: source.index(marker)]
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


def _hosts(monkeypatch, module, text):
    class _Path(type(Path())):
        pass

    monkeypatch.setattr(module, "Path", Path)
    original = Path.read_text

    def read_text(self, *args, **kwargs):
        if str(self) == "/etc/hosts":
            return text
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)


def test_no_pin_means_nothing_to_report(monkeypatch):
    """Silent on every host that never needed the workaround."""
    module = _module()
    _hosts(monkeypatch, module, "127.0.0.1 localhost\n")
    assert module._pinned_host_health() == {}


def test_a_pin_that_matches_the_reference_is_healthy(monkeypatch):
    module = _module()
    _hosts(monkeypatch, module, "160.79.104.10 platform.claude.com\n")
    monkeypatch.setattr(module, "_resolve_v4", lambda host: "160.79.104.10")
    monkeypatch.setattr(module.socket, "create_connection",
                        lambda *a, **k: _Socket())

    result = module._pinned_host_health()
    assert result["pinned"] == "160.79.104.10"
    assert not result.get("stale")
    assert not result.get("unreachable")


def test_a_pin_that_has_outlived_its_address_is_reported(monkeypatch):
    """The whole reason the reference host is checked at all."""
    module = _module()
    _hosts(monkeypatch, module, "160.79.104.10 platform.claude.com\n")
    monkeypatch.setattr(module, "_resolve_v4", lambda host: "160.79.200.55")

    result = module._pinned_host_health()
    assert result["stale"] is True
    assert result["reference_address"] == "160.79.200.55", (
        "the report has to carry the address to change it TO, or the reader "
        "knows something is wrong and not what to write"
    )


def test_a_resolver_failure_is_not_evidence_the_pin_is_wrong(monkeypatch):
    """Otherwise a transient DNS blip sends someone editing /etc/hosts."""
    module = _module()
    _hosts(monkeypatch, module, "160.79.104.10 platform.claude.com\n")
    monkeypatch.setattr(module, "_resolve_v4", lambda host: None)
    monkeypatch.setattr(module.socket, "create_connection",
                        lambda *a, **k: _Socket())

    result = module._pinned_host_health()
    assert not result.get("stale")


def test_a_pin_that_matches_but_refuses_connections_is_reported(monkeypatch):
    """Matching the reference is not the same as working.

    The address can be right and the route to it broken, which produces the
    identical symptom: agents dying at launch while the main session lives.
    """
    module = _module()
    _hosts(monkeypatch, module, "160.79.104.10 platform.claude.com\n")
    monkeypatch.setattr(module, "_resolve_v4", lambda host: "160.79.104.10")

    def refuse(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(module.socket, "create_connection", refuse)
    assert module._pinned_host_health()["unreachable"] is True


def test_a_commented_out_pin_does_not_count(monkeypatch):
    """A line someone disabled is not a pin."""
    module = _module()
    _hosts(monkeypatch, module, "# 160.79.104.10 platform.claude.com\n")
    assert module._pinned_host_health() == {}


class _Socket:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
