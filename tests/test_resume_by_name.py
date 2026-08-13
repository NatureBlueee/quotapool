"""`--resume <name>` must not be treated as a session id.

Reproduced from a live failure. The owner renamed a session to 空港, closed it,
then ran `cf --resume 空港`. It opened, but sending a message failed with Fleet
errors about the name.

Cause: explicit_session_id returned the display name, so the preflight
registered an execution keyed on "空港" and took a 24-hour writer lease on it.
Claude meanwhile resumed the real session, which has a UUID. The lease guarded a
key nothing would ever match, and every later resume of that name hit
LeaseConflict against the orphan.

One such row was found in the production database with its writer still live.
"""

from __future__ import annotations

import pytest

from claude_fleet.cli import explicit_session_id
from claude_fleet.control import LeaseConflict

UUID = "0a8623a1-9350-4983-abc2-de428397e069"


def test_a_real_session_uuid_is_still_returned():
    assert explicit_session_id(["--resume", UUID]) == UUID
    assert explicit_session_id(["--session-id", UUID]) == UUID
    assert explicit_session_id(["-r", UUID]) == UUID


def test_uppercase_uuid_is_accepted():
    assert explicit_session_id(["--resume", UUID.upper()]) == UUID.upper()


def test_a_display_name_is_not_treated_as_a_session_id():
    """The exact failing input."""
    assert explicit_session_id(["--resume", "空港"]) is None


def test_other_name_shapes_are_also_rejected():
    for name in ("my-session", "ppt master", "2026-07-31", "abc123", "空港 v2"):
        assert explicit_session_id(["--resume", name]) is None, name


def test_a_truncated_or_malformed_uuid_is_rejected():
    for bad in (UUID[:-1], UUID + "x", UUID.replace("-", ""), "0a8623a1-9350-4983-abc2"):
        assert explicit_session_id(["--resume", bad]) is None, bad


def test_a_bare_resume_with_no_argument_is_safe():
    assert explicit_session_id(["--resume"]) is None
    assert explicit_session_id(["--resume", "--verbose"]) is None


def test_fork_still_returns_none():
    """A fork makes a NEW session; fencing the parent would block a legitimate
    branch."""
    assert explicit_session_id(["--resume", UUID, "--fork-session"]) is None


def test_the_orphan_lease_scenario_no_longer_arises(control):
    """End to end: the preflight must not create an execution keyed on a name.

    With the old code this sequence left a writer lease on '空港' that made
    every subsequent resume of that name fail.
    """
    assert explicit_session_id(["--resume", "空港"]) is None

    # What the preflight would have done with the old return value, shown here
    # so the consequence is on record rather than implied.
    control.upsert_account("acct")
    control.ensure_execution("空港", workload_class="interactive")
    control.acquire_writer("空港", "holder-A")
    with pytest.raises(LeaseConflict):
        control.acquire_writer("空港", "holder-B")
