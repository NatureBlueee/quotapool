"""An account row the scheduler may pick but the executor cannot serve.

This has already 502'd the fleet once: legacy `max-a/b/c` rows sat frozen at
weekly_used=1.0, unselectable only because "spent" happens to disqualify an
account. Clearing their long-passed windows made them the most attractive
accounts in the fleet, and every request against them failed because the data
plane keys on uuid and cannot resolve a name.

`UNSERVABLE_ACCOUNT_ENABLED` exists to catch that. It did not.

Reproduced by accident: a diagnostic script posted a decide payload building
each id as `accountUuid || name` — the coordinator's own identity rule — against
a `/teamclaude/status` that did not serialise `accountUuid`. Every id came out
as an email address, and `sync_account_runtime` created five enabled, healthy
account rows keyed by email. Run against that database, the check reported
nothing: it matched the live accounts by NAME, and a row whose id IS a name
matches.

The lenient match was itself a fix — reading a display endpoint as if it
carried identity had flagged all five healthy accounts on the check's first
run. So the endpoint now serialises the identity, and the fallback survives
only for a data plane that predates the field, because flagging everything
during a rolling deploy is the worse of the two mistakes.
"""

from __future__ import annotations

from claude_fleet import reconcile as reconcile_module


def _unservable(db, monkeypatch, usable):
    monkeypatch.setattr(
        reconcile_module, "_dataplane_accounts", lambda port=13456: usable)
    report = reconcile_module.reconcile_report(db)
    return [
        a for a in report["anomalies"]
        if a["type"] == "UNSERVABLE_ACCOUNT_ENABLED"
    ]


def test_a_row_keyed_by_name_is_flagged(control, db, monkeypatch):
    """The shape that was minted by accident and reported as healthy."""
    control.upsert_account("00000000-0000-4000-8000-000000000001")
    control.upsert_account("second@example.com")

    # What the data plane really serves: uuids.
    flagged = _unservable(db, monkeypatch, {"00000000-0000-4000-8000-000000000001"})

    assert [a["account_id"] for a in flagged] == ["second@example.com"], (
        "an enabled account the data plane cannot resolve was reported as "
        "healthy; the scheduler may route there and every request 502s"
    )


def test_the_accounts_it_can_serve_are_not_flagged(control, db, monkeypatch):
    """The louder mistake, guarded.

    Flagging healthy accounts is worse than missing a ghost: it is what this
    check did on its first run, and a check that is always red is a check
    nobody reads.
    """
    control.upsert_account("00000000-0000-4000-8000-000000000001")
    control.upsert_account("00000000-0000-4000-8000-000000000002")

    flagged = _unservable(db, monkeypatch, {
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
    })
    assert flagged == []


def test_an_unreachable_data_plane_flags_nothing(control, db, monkeypatch):
    """No answer is not the same as "nothing is usable"."""
    control.upsert_account("00000000-0000-4000-8000-000000000001")
    assert _unservable(db, monkeypatch, None) == []


def test_the_identity_parser_prefers_id_and_falls_back(monkeypatch):
    """Strict when the field is served, lenient when it is not.

    A data plane that predates `id` must not turn every account into an
    anomaly the moment the control plane is deployed first.
    """
    import json

    class _Response:
        def __init__(self, body):
            self._body = json.dumps(body).encode()

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    import urllib.request

    def make(body):
        class _Opener:
            def open(self, url, timeout=4):
                return _Response(body)
        monkeypatch.setattr(urllib.request, "build_opener",
                            lambda *a, **k: _Opener())

    make({"accounts": [
        {"id": "uuid-1", "name": "hey@example.com"},
        {"id": "uuid-2", "name": "hi@example.com", "disabled": True},
    ]})
    assert reconcile_module._dataplane_accounts() == {"uuid-1"}, (
        "with `id` served, a name must NOT be accepted as identity — that "
        "acceptance is the blind spot"
    )

    make({"accounts": [{"name": "hey@example.com"}]})
    assert reconcile_module._dataplane_accounts() == {"hey@example.com"}, (
        "an older data plane serves no id; falling back is what keeps a "
        "rolling deploy from flagging every account"
    )
