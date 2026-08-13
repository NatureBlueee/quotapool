# Test matrix

Which tests defend which requirement. Run everything with:

```sh
./scripts/release-gate.sh
```

Before this existed, README referenced both this file and `release-gate.sh` and
neither was present — and `find` for any test file across the package returned
nothing. Treat a README capability claim as unverified until grepped.

## The four requirements

| # | Requirement | Defended by |
|---|---|---|
| (a) | Spend quota before its window resets; no account idle at reset while another is drained | `tests/test_quota_utilization.py` |
| (b) | The shared account is never fully consumed | `tests/test_shared_floor.py`, `tests/test_account_policy_preserved.py` |
| (c) | Manual control of which account is used now and next, daemons included | `tests/test_route_precedence.py` |
| (d) | Never fall back to paid API / Usage Credits — park and wait | `tests/test_shared_floor.py::test_floored_shared_account_is_skipped_not_fatal_when_alternatives_exist`, `tests/test_route_precedence.py::test_all_accounts_excluded_parks` |

**No test may ever assert a successful route to a non-`oauth` account.** The
guarantee is enforced in three independent places — the launcher's env scrub
with its deliberately-invalid OAuth sentinel, the data plane's
`type !== 'oauth'` guard, and a doctor check. A test that routed to one would be
asserting the opposite of requirement (d).

## By defect

Each row is a defect found by measurement on the live system, and the test that
now holds it closed.

| Defect | Test |
|---|---|
| A refused proxy CONNECT (sx.org 503) classified as an account fault, so healthy accounts with quota were excluded and the request parked for 6h | `vendor/teamclaude/test/error-classification.test.js` |
| A quota bucket with a value and no reset timestamp was unclearable, pinning an account out for the process lifetime | `vendor/teamclaude/test/quota-buckets.test.js` |
| `computeRetryAfter` always returned 60s for a subscription-only fleet, while the real wait was the next weekly reset | `vendor/teamclaude/test/retry-after.test.js` |
| A load pre-filter ran before the optimizer and dropped accounts holding quota that expired within the hour | `tests/test_quota_utilization.py::test_expiring_capacity_beats_an_idle_account` |
| `ACTIVE_FAMILY_LOAD_COST` at 50,000 outweighed a 25-hour deadline difference | same file; `test_load_still_breaks_a_tie_between_equal_accounts` pins the other direction |
| A bare `fleet account add <id>` reset kind to owned and both reserve floors to zero | `tests/test_account_policy_preserved.py` |
| `route.changed` is keyed by session id and `execution.parked` by execution id, so a diagnostic querying one returned half the history | `tests/test_why.py::test_reports_the_chosen_account_by_name` |
| A stale `outbound_proxy` fails every upstream request while looking like Anthropic being unreachable | `tests/test_runtime_portability.py` |

## By invariant

Properties that must not regress, independent of any particular defect.

| Invariant | Test |
|---|---|
| Writer generation never decreases | `tests/test_writer_fence.py::test_takeover_increments_generation_and_never_decrements` |
| A stale generation can neither renew nor route | `tests/test_writer_fence.py` |
| The shared floor is not relaxed for an existing session | `tests/test_shared_floor.py::test_floor_survives_existing_session` |
| A pin to an ineligible account parks rather than silently rerouting elsewhere | `tests/test_route_precedence.py::test_pin_to_disabled_account_parks_rather_than_silently_rerouting` |
| `next` is one-shot and clears itself | `tests/test_route_precedence.py::test_next_is_one_shot_and_then_pin_resumes` |
| `drain` stops new sessions without interrupting existing ones | `tests/test_route_precedence.py::test_draining_stops_new_sessions_but_not_existing_ones` |
| Priority outranks urgency; the shared floor outranks both | `tests/test_quota_utilization.py` |
| An unrecognised upstream error still fails over (do not over-classify as transport) | `vendor/teamclaude/test/error-classification.test.js::an unrecognised error stays an account fault` |
| Schema migrates forward cleanly with FK integrity intact | `scripts/release-gate.sh` |
| A pin cannot breach a shared account's reserve floor, and fails closed when the coordinator is unreachable | `vendor/teamclaude/test/pin-guard.test.js`, `tests/test_pin_cannot_breach_shared_floor.py` |
| A manual control left pointing at a renamed account is reported, with its effect | `tests/test_reconcile_dangling.py` |
| `--resume <display-name>` cannot create an execution nothing can match | `tests/test_resume_by_name.py` |
| An account with no quota reading is reported, and probed only when a probe would buy a decision | `tests/test_reconcile_dangling.py`, `tests/test_probe_quota_targets.py` |

## What is not covered

Stated plainly, because an unlabelled gap in a matrix reads as coverage.

- **No end-to-end test through the data plane.** Everything JS-side is a unit
  test over pure functions. The request path itself — `forwardRequest`, the hold
  loop, SSE forwarding, account UUID rewrite — has no automated coverage, and
  the two `node --test` files do not exercise it.
- **The optimizer has no golden-snapshot test.** `test_quota_utilization.py`
  asserts behaviour at the `decide_route` level, which is the level that
  matters, but a change to the cost model could shift decisions in ways no test
  would notice. Historical `account.runtime_synced` events can be replayed
  through `solve_rolling_horizon` for this; it has been done by hand, not
  committed.
- **The window-following behaviour of the desktop panel is not in this gate.**
  It was measured rather than asserted: `desktop/SIZING.md` records 13 sizing
  strategies run against a hardcoded ground truth, including animation-frame
  sampling to distinguish real interpolation from a jump. That is stronger
  evidence than a unit test would give, but it is a one-time measurement and
  nothing re-runs it. `/tmp/panel-test/run-all.sh` reproduces it by hand.

### Corrected 2026-07-31

Two entries here were stale and read as gaps that no longer exist:

- ~~**Nothing runs on Linux.**~~ The full suite now runs on the server on every
  sync — 145 Python and 70 node, same counts as the Mac. `test_runtime_portability`
  is still only a config-shape test, but it is no longer the only Linux signal.
- ~~**Live OAuth is never exercised.**~~ Still true of the gate, but the quota
  probe specifically has now been run end to end against a real account: a
  pinned four-token request moved `hello@` from unmeasured to 0.33 on the server,
  matching the Mac's independent reading. `scripts/fleet-probe-quota` is that
  procedure made repeatable, and `tests/test_probe_quota_targets.py` pins the
  decision of WHICH accounts are worth spending a probe on — the part that can
  regress silently. Token refresh and real 429 handling remain uncovered.
