# Quotapool

**English** · [简体中文](README.zh-CN.md)

[![CI](https://github.com/NatureBlueee/quotapool/actions/workflows/ci.yml/badge.svg)](https://github.com/NatureBlueee/quotapool/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-informational.svg)](LICENSE)
[![Zero dependencies](https://img.shields.io/badge/runtime%20deps-0-brightgreen.svg)](#no-dependencies)

Turn several Claude subscriptions you own into one pool that does not stop.

Claude Code keeps working when a window runs out. You do not pick an account,
you do not restart anything, and the share you keep for Claude Desktop stays
yours.

It runs entirely on your machine. Your credentials never leave it, and it only
ever routes your own requests to subscriptions you hold yourself.

```
claude          # same command, now scheduled across every account you own
```

---

## Who this is for

People with more than one Claude subscription who run Claude Code for hours at a
stretch — and especially people whose subscriptions are not only for writing
code. If some of that quota belongs to Claude Desktop or CoWork, this is the
part most tools get wrong.

If you have one subscription and use it interactively, you do not need this.

## What it does

**It does not stop.** When a window is spent, the request waits and resumes when
quota returns — it does not fail. A long task survives the boundary.

**You do not manage it.** Accounts switch mid-session without restarting Claude
Code. You never have to know which one you are on.

**It does not take what is not its.** Set aside a share of any account and Claude
Code cannot spend past it, however busy it gets. That reserve is what keeps your
desktop client working at 4pm.

---

## Install

Requires Node 20+, Python 3.9+, and Claude Code already installed.

```sh
git clone <this repository> ~/.local/share/quotapool
cd ~/.local/share/quotapool
./install.sh --dry-run     # every change it would make, in full
./install.sh
```

Then add your accounts — each one is an ordinary browser login:

```sh
quotapool login                       # repeat per subscription
claude-fleet-canary <a> <b>   # prove a real switch between two of them
claude
```

`./install.sh --uninstall` puts the machine back and leaves your credentials
where they are.

**Before you run it on a machine you care about**, `docs/DEPLOYMENT.md` lists
exactly what it takes over, what it assumes is already true, and how the one
deployment we can vouch for is put together — two hosts, every service and timer
on each, and the failure that link actually produced. It is written to be mapped
onto a different setup rather than followed literally.

### If it breaks

`claude` runs through Quotapool, so a broken install would otherwise take
`claude` with it. It does not:

```sh
claude-direct              # pick an account, connect directly, no scheduler
fleet-rollback now         # return to the last version proven good
quotapool doctor               # what is actually wrong
```

`claude-direct` imports nothing from the rest of this by design — it reads one JSON file and
execs the real binary. It works when everything else does not.

---

## How it decides

Most multi-account tools rotate on a threshold: use an account until it is 98%
spent, then move to the next. That is fine until the accounts have different
reset times, or one of them is half yours.

This plans instead. Every routing decision is solved as an integer minimum-cost
flow over a seven-day horizon in 30-minute slots, across both the five-hour and
weekly windows of every account at once. It answers "which account should serve
this, now, so that the whole week works out" rather than "is this one full yet".

`OPTIMIZER.md` has the objective function and, more usefully, the bounds of what
it can and cannot know.

```sh
quotapool why <session-id>     # why this session is on this account
quotapool capacity             # is now a good time to start a batch job?
```

---

## Reserving quota for your other Claude clients

The feature this exists for.

```sh
quotapool account add <account-id> --weekly-floor 0.4
```

Claude Code now stops at 60% of that account's weekly window. The rest stays
available to Claude Desktop, CoWork, or anything else signed into the same
subscription.

As the window nears its reset the reserve tapers, so quota you were never going
to use is not simply thrown away.

---

## Two machines

A laptop and a server can share the same subscriptions. Each runs its own
scheduler and stays useful if the other is down; quota readings propagate between
them, so neither gets stuck believing an account is spent when it is not.

Because neither can see the other's decisions in real time, both could act on a
stale figure and together overshoot a reserve. `host_budget` in `runtime.json`
bounds how far either host can push an account's total usage:

```json
{ "host_budget": 0.6 }
```

Set them ordered, with one host at `1.0`. This bounds the overshoot; it does not
eliminate it, and `SECURITY.md` says so plainly.

Worth knowing before you copy it: **neither of our own hosts sets it**, so both
run at the default `1.0` and the overshoot is possible on the deployment this
program was written for. That is a choice for a fleet with one light user, not a
recommendation. `docs/DEPLOYMENT.md` has the rest of what is asymmetric between
those two hosts — including that only one of them watches anything, so the
server is unmonitored whenever the laptop is asleep.

---

## What it will not do

Deliberate, and worth checking against what you need before adopting it:

- **No API keys, no usage credits.** Nothing here can spend outside a
  subscription you already pay for. When everything is spent, requests wait.
- **No third-party upstreams.** The vendored data plane can route to
  Anthropic-compatible APIs; that path is removed.
- **No IP evasion.** Upstream can provision a residential proxy to work around
  IP-based limits. Refused at startup and again on every config reload.
- **No remote listener.** Both services bind loopback only.

## Security

It holds OAuth credentials for every subscription you give it, and generates a
local CA so it can inject the right token into requests that hardcode
`api.anthropic.com`. That CA is trusted only by the Claude Code child process
through `NODE_EXTRA_CA_CERTS` — never installed into the system or browser trust
stores.

`SECURITY.md` covers the trust boundary, how to report a vulnerability, and the
one gap this ships with rather than hides: loopback is not an authentication
boundary, and the control that closes it defaults to `audit` so you can measure
before enforcing.

## Operating it

```sh
quotapool doctor                 # health, with skipped checks named as skipped
quotapool backup / quotapool restore # snapshot the state database, and put it back
fleet-rollback list          # versions proven good, and for how long
fleet-identity-check         # read-only: two orgs under one account?
fleet-shadow up              # a complete second instance on its own ports
./scripts/release-gate.sh --release
```

The operational scripts are named `fleet-*`. A *fleet* is the internal word for
your pool of accounts — it predates the project having a name, and renaming the
files would rewrite service definitions on machines already running them for no
gain.

A version is only recorded as "known good" after it has run for two hours,
served real traffic, and answered an authenticated request — because every
outage this system has had appeared minutes after a deploy that passed every
check at deploy time.

---

## No dependencies

Neither half has any. `pyproject.toml` declares no runtime dependencies and the
vendored data plane's `package.json` has none either — Python's standard library
and Node's, and nothing else.

That is a security property rather than an aesthetic one. This program holds
every OAuth token you own in one file; each dependency would be a package with
read access to it. `pip install -e '.[dev]'` adds exactly one thing, `pytest`,
and only for running the tests.

---

## The rest of the documentation

| | |
|---|---|
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | What it takes over on your machine, and the one deployment we can vouch for — every service and timer on both hosts |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | The two planes, and why the split is where it is |
| [`THREAT-MODEL.md`](THREAT-MODEL.md) | Who the adversary is, what is worth stealing, and the known-weak areas stated rather than hidden |
| [`SECURITY.md`](SECURITY.md) | Reporting, and an honest reading of what `FLEET_AUTH_MODE=enforce` actually buys |
| [`OPTIMIZER.md`](OPTIMIZER.md) | How an account gets chosen |
| [`TEST-MATRIX.md`](TEST-MATRIX.md) | Which tests defend which requirement |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | How to run the 604 tests, and the one rule that is not negotiable |
| [`CHANGELOG.md`](CHANGELOG.md) | What changed, and which failure motivated it |

---

## Built on TeamClaude

The data plane is a fork of
[TeamClaude](https://github.com/KarpelesLab/teamclaude) (MIT, KarpelesLab), which
owns OAuth refresh, the MITM proxy, SSE forwarding and quota-rejection retry. The
control plane — scheduling, policy, persistence, the operational tooling — is
original.

`NOTICE` records the derivation, the upstream hashes, and what was changed.

Not an Anthropic product, and not affiliated with Anthropic. The unrelated `claude-fleet`
package on npm is somebody else's project, and has no connection to this one.

## Licence

MIT. See `LICENSE`, and `vendor/teamclaude/LICENSE` for the fork's upstream.
