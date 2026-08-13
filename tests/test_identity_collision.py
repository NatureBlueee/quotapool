"""Two organisations must not become one row.

`fleet-coordinator.js` keys an account as `accountUuid || name`, and the control
plane keeps `org_uuid` in metadata rather than in the key. One Claude account
belonging to two organisations therefore writes both subscriptions into a single
row — credentials, quota, reserve floor, pins and audit history merged. Silently.

The obvious repair is a composite primary key, and the read-only dry run
(`fleet-identity-check`) is why that is not this change:

- No collision exists on this fleet. Every accountUuid maps to exactly one
  orgUuid on both hosts, so the corruption is latent rather than active.
- The migration is a SQLite table rebuild behind a foreign key — 16,866 lease
  rows on the Mac — with no downgrade path.
- And it would currently break cross-host sync. Measured: the server's
  credential file carries no orgUuid for three of five accounts, so the same
  subscription would key as `<uuid>/<org>` on the Mac and `<uuid>/-` on the
  server. `fleet-quota-sync` matches on id, so readings would stop propagating —
  reintroducing the deadlock it was written to fix (674 server decisions in
  seven days, zero of them to the shared account).

So the danger is removed without the migration: a collision is refused rather
than absorbed. Losing the use of an account is recoverable; merging two
subscriptions' state is not.
"""

from __future__ import annotations

import json

import pytest

from claude_fleet.coordinator import Coordinator


def _account(account_id, uuid, org, **extra):
    entry = {
        "id": account_id, "name": account_id, "type": "oauth", "index": 0,
        "account_uuid": uuid, "org_uuid": org, "priority": 0,
        "disabled": False, "status": "active", "eligible": True,
        "quota": {"unified7d": 0.1, "unified5h": 0.1},
    }
    entry.update(extra)
    return entry


def test_one_account_in_two_orgs_is_refused_not_merged(db):
    coordinator = Coordinator(db)
    accounts = [
        _account("shared-uuid", "shared-uuid", "org-personal"),
        _account("shared-uuid", "shared-uuid", "org-corp"),
    ]
    assert coordinator._identity_collisions(accounts) == {"shared-uuid"}


def test_the_ordinary_fleet_is_untouched(db):
    """Every account on this fleet has one org. A guard that fired here would
    take the whole thing down."""
    coordinator = Coordinator(db)
    accounts = [
        _account("a", "uuid-a", "org-1"),
        _account("b", "uuid-b", "org-2"),
        _account("c", "uuid-c", "org-3"),
    ]
    assert coordinator._identity_collisions(accounts) == set()


def test_the_same_account_reported_twice_is_not_a_collision(db):
    """Duplicate entries for one subscription are a data-plane quirk, not two
    organisations. Refusing those would be a self-inflicted outage."""
    coordinator = Coordinator(db)
    accounts = [
        _account("a", "uuid-a", "org-1"),
        _account("a", "uuid-a", "org-1"),
    ]
    assert coordinator._identity_collisions(accounts) == set()


def test_a_missing_org_is_not_a_collision_on_its_own(db):
    """Three of the five accounts on the server carry no orgUuid at all.
    Treating absent-vs-absent as a conflict would exclude them all."""
    coordinator = Coordinator(db)
    accounts = [
        _account("a", "uuid-a", None),
        _account("b", "uuid-b", None),
    ]
    assert coordinator._identity_collisions(accounts) == set()


def test_present_versus_absent_org_under_one_uuid_is_a_collision(db):
    """The shape the cross-host gap would produce if it ever reached one
    coordinator: the same subscription, once with an org and once without."""
    coordinator = Coordinator(db)
    accounts = [
        _account("u", "u", "org-1"),
        _account("u", "u", None),
    ]
    assert coordinator._identity_collisions(accounts) == {"u"}


def test_a_collision_is_recorded_where_it_can_be_found_later(db):
    """A refusal nobody can explain afterwards is its own kind of outage."""
    coordinator = Coordinator(db)
    coordinator._identity_collisions([
        _account("u", "u", "org-1"),
        _account("u", "u", "org-2"),
    ])

    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT subject, payload FROM events WHERE kind=?",
            ("account.identity_collision",)).fetchall()
    assert rows, "the collision must leave a trace"
    payload = json.loads(rows[0]["payload"])
    assert sorted(payload["org_uuids"]) == ["org-1", "org-2"]
    assert payload["action"] == "excluded_from_routing"


def test_a_collision_never_breaks_routing_itself(db, monkeypatch):
    """Diagnostics are not allowed to be the thing that takes the fleet down —
    the same rule the slow-decision recorder follows."""
    coordinator = Coordinator(db)

    def explode(*a, **k):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(coordinator.db, "transaction", explode)
    assert coordinator._identity_collisions([
        _account("u", "u", "org-1"),
        _account("u", "u", "org-2"),
    ]) == {"u"}


def test_a_colliding_account_is_never_routed_to(db):
    """The wiring, not the helper.

    Mutation testing caught this gap: every other test here exercises
    `_identity_collisions` directly, so deleting the one line in `decide` that
    calls it left the guard as dead code with the whole suite green. A check
    nothing invokes is not a check.
    """
    coordinator = Coordinator(db)
    decision = coordinator.decide({
        "session_id": "s1", "holder": "h1", "generation": 1,
        "workload_class": "interactive",
        "accounts": [
            _account("collide", "collide", "org-1"),
            _account("collide", "collide", "org-2"),
            _account("clean", "clean-uuid", "org-3"),
        ],
    })
    assert decision["account_id"] == "clean", (
        "a merged identity must not be chosen while a sound account exists"
    )


def test_a_fleet_of_nothing_but_collisions_parks_rather_than_merging(db):
    """The harder half of the same decision. With no sound account left the
    answer is to wait — which recovers by itself — not to spend one of two
    subscriptions whose state has been merged into a single row."""
    from claude_fleet.control import NoCapacity

    coordinator = Coordinator(db)
    with pytest.raises(NoCapacity):
        coordinator.decide({
            "session_id": "s2", "holder": "h2", "generation": 1,
            "workload_class": "interactive",
            "accounts": [
                _account("collide", "collide", "org-1"),
                _account("collide", "collide", "org-2"),
            ],
        })


def test_malformed_entries_are_ignored_rather_than_crashing(db):
    coordinator = Coordinator(db)
    assert coordinator._identity_collisions([
        None, "not a dict", {}, {"account_uuid": "x"}, {"id": "y"},
    ]) == set()
