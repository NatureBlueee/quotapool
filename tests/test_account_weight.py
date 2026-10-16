import json

from claude_fleet.control import FleetControl


def test_runtime_sync_preserves_manual_weight(control):
    control.upsert_account("account-1")
    with control.db.transaction() as conn:
        conn.execute("UPDATE accounts SET metadata=? WHERE id=?",
                     (json.dumps({"routing_weight": 6}), "account-1"))

    result = control.sync_account_runtime(
        "account-1", name="max-2", metadata={"source": "teamclaude"},
    )

    assert result["metadata"]["routing_weight"] == 6
    assert result["metadata"]["source"] == "teamclaude"


def test_invalid_or_absent_weight_defaults_to_one(control):
    control.upsert_account("account-1")
    with control.db.transaction() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", ("account-1",)).fetchone()
        assert FleetControl._routing_weight(row) == 1
        conn.execute("UPDATE accounts SET metadata=? WHERE id=?",
                     (json.dumps({"routing_weight": 99}), "account-1"))
        row = conn.execute("SELECT * FROM accounts WHERE id=?", ("account-1",)).fetchone()
        assert FleetControl._routing_weight(row) == 1
