"""A spent account must never look like an unmeasured one.

Measured on the Harness server: max-c reported `unified7d: 1.01` (over its
weekly window) alongside `unified7dFable: 0.11`. `_ratio` rejected 1.01 as
out-of-range and returned None, so `_effective_weekly` dropped the overall
bucket entirely and kept only Fable's 0.11. The control plane then recorded
weekly_used=0.11 for an account with nothing left, and the health check reported
"1 of 3 accounts usable" while all three were spent.

Reporting capacity that does not exist is strictly worse than reporting none:
it sends requests at an account that will refuse them.
"""

from __future__ import annotations

import pytest

from claude_fleet.coordinator import _effective_weekly, _ratio


def test_a_normal_ratio_passes_through():
    assert _ratio(0.0) == 0.0
    assert _ratio(0.42) == pytest.approx(0.42)
    assert _ratio(1.0) == 1.0


def test_slight_overshoot_is_spent_not_unknown():
    """The exact server value."""
    assert _ratio(1.01) == 1.0
    assert _ratio(1.5) == 1.0


def test_nonsense_is_still_unknown():
    assert _ratio(None) is None
    assert _ratio("abc") is None
    assert _ratio(-0.1) is None
    assert _ratio(float("nan")) is None
    # A percentage that leaked through un-normalised must not be clamped to
    # "spent" — that would be a different silent misreading.
    assert _ratio(101) is None


def test_the_server_case_now_reports_the_account_as_spent():
    """max-c's real payload: overall spent, Fable bucket nearly untouched.

    The governing value must be the overall window, not the family bucket.
    """
    quota = {
        "unified7d": 1.01,
        "unified7dReset": 1785758400000,
        "unified7dFable": 0.11,
        "unified7dFableReset": 1785758400000,
    }
    used, _reset = _effective_weekly(quota, "claude-opus-4")
    assert used == 1.0, "an account over its weekly window must read as spent"

    # For a Fable request both buckets are candidates and `_effective_weekly`
    # takes the max, so the spent overall window still wins. Note this is the
    # conservative direction by construction, not an implementation of umbrella
    # semantics — whether the overall weekly is a true umbrella over the family
    # buckets has not been established from an upstream response.
    used_fable, _ = _effective_weekly(quota, "claude-fable-5")
    assert used_fable == 1.0


def test_a_healthy_account_is_unaffected():
    quota = {
        "unified7d": 0.11,
        "unified7dReset": 1785758400000,
        "unified7dFable": 0.05,
        "unified7dFableReset": 1785758400000,
    }
    used, reset = _effective_weekly(quota, "claude-opus-4")
    assert used == pytest.approx(0.11)
    assert reset is not None


def test_missing_quota_stays_unknown():
    used, reset = _effective_weekly({}, "claude-opus-4")
    assert used is None and reset is None


def test_a_wrongly_stamped_database_self_heals(tmp_path):
    """A version stamp is not proof the migration ran.

    Found live: a database stamped user_version=3 whose `accounts` table had no
    `interactive_reserve` column — the stamp was written by a build whose ALTER
    did not exist yet, and every later start skipped the migration on the
    strength of it. The database would have stayed broken permanently, and it
    surfaced as an IndexError deep in the request path rather than as a
    migration error.
    """
    import sqlite3

    from claude_fleet.db import SCHEMA_VERSION, FleetDB

    path = str(tmp_path / "fleet.sqlite3")
    FleetDB(path)

    # Simulate the broken state: drop the column, keep the stamp.
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE accounts DROP COLUMN interactive_reserve")
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION:d}")
    conn.commit()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
    assert "interactive_reserve" not in columns
    conn.close()

    # Opening it must repair rather than trust the stamp.
    FleetDB(path)
    conn = sqlite3.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
    conn.close()
    assert "interactive_reserve" in columns
