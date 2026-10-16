from __future__ import annotations

import json

from claude_fleet.dashboard import _toggle_account
from claude_fleet.db import FleetDB


def test_dashboard_toggle_updates_data_and_control_planes(tmp_path):
    credentials = tmp_path / "teamclaude.json"
    credentials.write_text(json.dumps({"accounts": [{
        "type": "oauth",
        "name": "max-5",
        "accountUuid": "uuid-max-5",
        "accessToken": "test-only",
        "refreshToken": "test-only",
        "disabled": True,
    }]}), encoding="utf-8")
    runtime_path = tmp_path / "runtime.json"
    runtime = {
        "teamclaude_config": str(credentials),
        "db_path": str(tmp_path / "fleet.sqlite3"),
        "proxy_port": 29999,
    }
    runtime_path.write_text(json.dumps(runtime), encoding="utf-8")

    db = FleetDB(runtime["db_path"])
    from claude_fleet.control import FleetControl
    control = FleetControl(db)
    control.upsert_account("uuid-max-5")
    control.set_account_state("uuid-max-5", "disabled")

    _toggle_account(runtime, "max-5", runtime_path=str(runtime_path))

    saved = json.loads(credentials.read_text(encoding="utf-8"))
    assert saved["accounts"][0]["disabled"] is False
    with db.connect() as conn:
        row = conn.execute(
            "SELECT state FROM accounts WHERE id='uuid-max-5'"
        ).fetchone()
    assert row["state"] == "healthy"
