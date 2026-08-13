## What this changes, and why

<!-- The failure that motivated it, not only the mechanism. -->

## Mutations

<!-- Required for behaviour changes. Break the code your test covers, run the
     test, confirm it goes red, put the code back. List what you broke and
     which test noticed.

     The mutation that matters most is usually not "is the rule right" but
     "is the rule called at all" — try deleting the call site. -->

| Mutation | Caught by |
|---|---|
|  |  |

## Checks

- [ ] `./scripts/release-gate.sh --release`
- [ ] `python3 -m pytest tests/ -q`
- [ ] `cd vendor/teamclaude && node --test`
- [ ] No new runtime dependency (both planes currently have zero)
