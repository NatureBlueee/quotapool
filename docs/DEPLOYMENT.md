# Deployment

Written to be read by whoever — or whatever — is doing the install. It states
what this program assumes, what it takes over, and exactly how the one
deployment we can vouch for is put together. It does not try to anticipate your
machine; it tries to give you enough to decide what maps and what does not.

The reference deployment below is the only one that has ever run. Everything
else in this file is derived from reading it, not from testing elsewhere.

---

## 1. What it takes over

Install it and these stop being yours alone. Read this list before running
anything; the uninstaller reverses all of it except the credentials.

| It touches | What happens | Reversible by |
|---|---|---|
| `~/.local/bin/claude` | Becomes a symlink to `claude-fleet-shell`, so every `claude` goes through the scheduler | `./install.sh --uninstall` |
| `~/.local/bin/*` | One symlink per wrapper in `bin/` | same |
| `~/.config/quotapool/` | `runtime.json`, `teamclaude.json` (your OAuth tokens), local CA | left in place deliberately — deleting credentials needs a browser to undo |
| `~/.local/state/quotapool/` | SQLite state, logs, backups, known-good marks | left in place |
| launchd / systemd | Long-running services and timers, listed in §3 | `--uninstall`, or `install-timers.sh --remove` |
| A local CA | Generated per machine, used to terminate TLS for `api.anthropic.com` so the chosen account's token can be injected | scoped to the child process via `NODE_EXTRA_CA_CERTS`; **never** installed in the system or browser trust stores |

If a name it wants is already taken by a real file, it refuses and says so
rather than replacing it. Identical bytes are treated as an earlier hand-made
install of the same thing and upgraded to a symlink; anything that differs is
someone's work and is left alone.

## 2. What must already be true

- **Claude Code installed**, with at least one version under
  `~/.local/share/claude/versions/`. The installer refuses without it, and
  writes nothing.
- **Node 20+** and **Python 3.9+**. Ours run Node 26.5 / Python 3.9.6 (macOS
  system) and Node 22 / Python 3.11 (Linux).
- **One or more Claude subscriptions.** One works. Two or more is the point.
- **macOS or Linux.** macOS uses launchd, Linux uses systemd; both are in
  `packaging/`.

A note on the account count: `quotapool doctor` reports
`two_enabled_oauth_accounts` as a failure when fewer than two accounts are
usable. With a single subscription that is red on a healthy machine and there is
nothing to fix — it is telling you there is no second account to fail over to.
It does not block anything.

## 3. The reference deployment

Two hosts sharing the same set of subscriptions. **Not symmetric** — one is the
control host and the other is not, and it matters.

### Host A — laptop, macOS, the control host

| | |
|---|---|
| Services | `coordinator` (17891), `dataplane` (13456) |
| Timers | `tokens` 60s · `quotasync` 600s · `watchdog` 300s · `markgood` 3600s · `health` 900s · `maintenance` daily · `authprobe` 3600s |
| `peer_host` | set — this host probes the other |
| `outbound_proxy` | set; everything that talks to a model goes through it |
| Repository | a git checkout; this is the only host with git |

### Host B — server, Linux, always on

| | |
|---|---|
| Services | `coordinator` (17891), `dataplane` (13456) |
| Timers | `tokens` 60s · `health` 900s · `maintenance` daily |
| `peer_host` | **not set** — it never probes back |
| Repository | an rsync target, **not** a git checkout |

### What the asymmetry means

- **Only Host A watches anything.** The watchdog runs there and probes both.
  There is deliberately not a second watchdog on Host B: two watchdogs, each
  able to trigger a two-host rollback, is a split brain rather than redundancy.
- **So when Host A is asleep, Host B is unmonitored.** On a laptop that is most
  nights. Measured on 2026-08-12: gaps of 208, 244 and 244 minutes.
- **Deploys are one-directional.** `fleet-rollback` checks out on Host A and
  rsyncs `src/claude_fleet`, `vendor/teamclaude/src`, `scripts` and `packaging`
  to Host B, then restarts both. Host B has no git, so `mark` compares a content
  fingerprint of those paths rather than a commit.
- **Credentials are not distributed automatically.** Each host holds its own
  `teamclaude.json`. Adding an account on one does not add it to the other.

### How Host A reaches Host B

SSH, host alias from `peer_host` in `runtime.json`. Ours is a direct connection
over a Tailscale address.

It used to go through a jump host, and that is worth writing down because it is
the failure a second machine will most likely give you: on 2026-08-12 **45 of 50
probes could not reach Host B**, because the jump host was down. Nothing was
wrong with either fleet. The symptoms were:

- `fleet-watchdog --status` showing `<peer> 够不着: Connection closed by …`
- `markgood` refusing every hour for 20 hours, so the newest known-good point
  aged past a day
- No false rollback — "cannot reach" is deliberately not treated as "is down"

It hid for a while because an interactive `ssh` leaves a multiplexed connection
open for 30 minutes (`ControlPersist`), so probes run while you are working
succeed by riding on it. **Test the peer link the way the timer will run it:**

```sh
env -i HOME="$HOME" PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  ssh -o ControlMaster=no -o ControlPath=none -o BatchMode=yes <peer> true
```

If that fails while plain `ssh <peer>` works, the scheduler cannot see the other
host and you have not noticed yet.

### `host_budget`, and what ours is actually set to

Each host learns an account's usage from its own responses, so two hosts can
both act on a stale figure and together overshoot a reserve. `host_budget` in
`runtime.json` caps how far one host may push an account's *total* observed
usage.

**Neither of our hosts sets it, so both run at the default `1.0`** — meaning
neither is bounded and the overshoot is possible. That is a deliberate omission
for a two-host fleet with one light user, not a recommendation. If both hosts
are busy, set them ordered (e.g. `0.6` and `1.0`).

## 4. Mapping this onto something else

The parts that are structural, in rough order of how much they matter:

1. **One control host.** Whatever runs the watchdog must be able to reach every
   other host, and only one host should be able to trigger a rollback.
2. **The peer link is the fragile part**, not the schedulers. Test it under the
   environment the timer actually gets, not your shell's.
3. **Ports are per-host config**, not constants — `coordinator_port` and
   `proxy_port` in `runtime.json`. Two fleets on one machine is what
   `scripts/fleet-shadow` does, and it is the way to try changes without
   touching a live one.
4. **The data plane holds the credentials**; the control plane holds no tokens.
   Anything that needs to survive the scheduler being broken —
   `scripts/claude-direct` is the worked example — must read `teamclaude.json`
   directly and import nothing from `claude_fleet`.

Single host: everything above still applies, minus `peer_host`. Nothing probes a
peer, `mark` compares one fingerprint, and the watchdog watches one machine.

## 5. When it goes wrong

```sh
quotapool doctor          # what is actually wrong, as JSON
fleet-watchdog --status   # what the watchdog has seen, and whether it ever acted
fleet-rollback list       # versions proven good, newest first
fleet-rollback now        # go back to the newest one that is not running
claude-direct --list      # accounts and token validity, no scheduler involved
claude-direct <name>      # a session on one account, bypassing everything
```

`claude-direct` imports nothing from the rest of this on purpose. It reads one
JSON file and execs the real Claude Code binary, so it works when nothing else
does — including when `claude` itself is broken by a bad install.

Two failure shapes are worth recognising because both look like silence:

- **The recovery point stops advancing.** `markgood` refuses hourly into
  `~/.local/state/quotapool/markgood.error.log`. The watchdog raises a
  `known_good_stale` alarm after 24 hours and the status line shows it, but the
  log says why.
- **A permanently-failing `doctor` check jams the gate.** `mark` requires a
  clean `doctor`, minus a deny-list of checks that describe capacity rather than
  the version. A new check that describes capacity will jam it again; the
  staleness alarm is what makes that visible within a day.
