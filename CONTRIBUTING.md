# Contributing

Thanks for looking. This is a small project with an unusual amount of test
discipline, and the reasons are worth stating before the mechanics.

## Run it first

```sh
git clone https://github.com/NatureBlueee/quotapool
cd quotapool
pip install -e '.[dev]'      # the only dependency is pytest, and only for tests

./scripts/release-gate.sh --release   # syntax, plists, schema round-trip
python3 -m pytest tests/ -q           # 486 tests
cd vendor/teamclaude && node --test   # 118 tests
```

Everything runs offline. There are no live-service tests, no fixtures that need
credentials, and no secrets in CI. If something asks you for a token to run a
test, that is a bug — please report it.

## The one thing that is not negotiable

**A test that cannot fail is worse than no test**, because it converts "unknown"
into "verified" without doing any work. This repository has been bitten by that
repeatedly, and the fixes are recorded in the test docstrings:

- three probe tests passed while reading a string left behind by an unrelated
  stub two hundred lines away
- a collision check was deleted and every test stayed green, because nothing
  called the function
- `JSON.stringify` threw before the code under test ran, so the cleanup
  assertion never executed

So: **after writing a test, break the code it covers and watch it fail.** Not
"convince yourself it would fail" — actually change the source, run the test,
see red, put the source back. If it stays green, the test is measuring
something else.

The mutations that matter most are usually not "is the rule right" but **"is
the rule called at all"** — delete the call site, not just the logic.

You will see this in commit messages as e.g. `7 条变异全部被对应测试抓到`
("all 7 mutations caught by their intended test"), with the mutations named.
That is the format; a list of what you broke and which test noticed is the most
useful thing a reviewer can be handed.

## What a change should look like

- **Say why in the code, not just in the PR.** Comments here explain the failure
  that motivated the line, because the reader six months later needs the reason
  more than the mechanism. Look at any file for the house style.
- **Small and behaviour-preserving by default.** This scheduler runs unattended
  and holds people's OAuth credentials. A refactor that improves elegance and
  risks behaviour is a bad trade here.
- **No new runtime dependencies.** Both planes currently have zero. That is a
  security property, not an aesthetic one — every dependency is a package that
  can reach the credential file.
- **Never weaken a check to make it pass.** If a gate refuses your change and
  the gate is wrong, fix the gate in its own commit and say why.

## Things that will be refused

- Anything that lets the program spend outside a Claude subscription — API keys,
  usage credits, third-party upstreams. See `NOTICE`, "Deliberate omissions".
- Anything that routes requests on behalf of somebody else. This is a local
  single-user tool and stays one.
- Anything that installs the local CA into a system or browser trust store.
- Silently swallowing an error in a supervisory path. Loud and wrong beats quiet
  and wrong; the whole `THREAT-MODEL.md` section on silent capacity loss is
  about what quiet costs.

## Reporting a bug

`quotapool doctor` prints structured JSON with a per-check result. Paste it.
Without it a report is usually not actionable, because most failures here are
environmental — a missing timer, an unreachable peer, a stale token — and
`doctor` names which.

If the bug is security-relevant, read `SECURITY.md` first and do not open a
public issue.

## Where things are

| | |
|---|---|
| `src/claude_fleet/` | control plane — decides which subscription serves a request. Holds no tokens. |
| `vendor/teamclaude/` | data plane — a fork of TeamClaude, holds the credentials. See `NOTICE`. |
| `scripts/` | operational tools: rollback, watchdog, backup, escape hatch, release builder |
| `tests/`, `vendor/teamclaude/test/` | 604 tests |
| `docs/DEPLOYMENT.md` | what it takes over, and the one deployment we can vouch for |
| `THREAT-MODEL.md` | who the adversary is, and the known-weak areas, stated rather than hidden |

## A note on the vendored fork

`vendor/teamclaude/` is derived from
[TeamClaude](https://github.com/KarpelesLab/teamclaude) (MIT, KarpelesLab).
Changes there are ours to maintain but the design is not — before changing that
directory, read `vendor/teamclaude/VENDOR-NOTICE.md` for what upstream still
owns. Fixes that belong upstream are better sent upstream.
