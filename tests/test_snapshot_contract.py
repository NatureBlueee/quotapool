"""`fleet-snapshot` must actually produce a snapshot.

It is the only path between the running fleet and the desktop panel, and it is
three hops long: the data plane's `/teamclaude/status` → this script → the Swift
struct's coding keys. Any hop that stops passing a field through leaves the panel
showing stale numbers with no error anywhere.

That is not a hypothetical. Bumping the token store's schema changed each bucket
from `[i, o, cw, cr]` to `{model: [i, o, w5m, w1h, cr]}`, and this script kept
indexing it as a flat list. It raised KeyError, which took down the WHOLE
snapshot — every field, not just tokens — and the panel simply kept rendering
whatever it had. Nobody noticed, because `fleet-tokens` was tested directly
afterwards and this script never was.

So the contract is asserted rather than assumed. These run against the live
machine when there is one and skip cleanly when there is not, because a test
that cannot run on a developer's laptop gets deleted rather than fixed.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SNAPSHOT = Path(__file__).resolve().parents[1] / "scripts" / "fleet-snapshot"
RUNTIME = Path(
    os.environ.get("FLEET_RUNTIME_CONFIG",
                   str(Path.home() / ".config" / "claude-fleet" / "runtime.json"))
).expanduser()

live = pytest.mark.skipif(
    not RUNTIME.exists(), reason="no local fleet configured"
)


@pytest.fixture(scope="module")
def snapshot():
    result = subprocess.run([str(SNAPSHOT)], capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, (
        "fleet-snapshot exited non-zero; the panel has no data at all.\n"
        + result.stderr[-2000:]
    )
    assert result.stdout.strip(), "fleet-snapshot produced nothing on stdout"
    return json.loads(result.stdout)


@live
def test_it_runs_at_all(snapshot):
    """The regression that started this file: one KeyError killed every field."""
    assert snapshot["hosts"], "a snapshot with no hosts is not a snapshot"


@live
def test_the_panel_s_fields_survive_the_trip(snapshot):
    """Every key the Swift side decodes, checked here rather than at render time.

    The panel fails silently — a missing key becomes nil and the row just does
    not appear — so this is the only place the absence is loud.
    """
    host = snapshot["hosts"][0]
    for key in ("host", "accounts", "dataplane_up", "running",
                "active_accounts", "waiting", "tokens"):
        assert key in host, f"the panel decodes `{key}` and it is missing"
    assert "accounts" in snapshot, "the merged per-subscription view is gone"


@live
def test_token_totals_are_not_silently_zero(snapshot):
    """A schema drift shows up as zeros long before it shows up as an error.

    `counted` folds both cache-write TTLs; reading the old four-slot layout
    against the five-slot store would quietly drop the 1h writes, which are the
    majority here.
    """
    tokens = snapshot["hosts"][0].get("tokens") or {}
    if not tokens:
        pytest.skip("token collector has not run yet")
    assert tokens.get("total", 0) > 0, (
        "token totals read as zero — the store's layout and this script's "
        "reader have drifted apart"
    )
    assert tokens["total"] >= tokens.get("last_24h", 0), (
        "an all-time total below the last day means the two windows are being "
        "computed from different stores"
    )


@live
def test_waiting_is_reported_even_when_nobody_waits(snapshot):
    """Absent and zero must not look the same.

    An empty `waiting` block would be indistinguishable from a data plane that
    does not report one — and "nobody is waiting" is exactly the state the panel
    needs to state positively.
    """
    waiting = snapshot["hosts"][0].get("waiting")
    assert isinstance(waiting, dict), "waiting must be an object, not null"
    if waiting:
        assert "count" in waiting and "avoidable" in waiting, (
            "the avoidable/genuine split is the whole point; without it a "
            "spent fleet and a routing bug look identical"
        )
