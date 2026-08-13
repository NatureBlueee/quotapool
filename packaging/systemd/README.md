# Claude Fleet on Linux (Harness authority server)

`ARCHITECTURE.md` promised "equivalent systemd user services" but the package
shipped only the two macOS LaunchAgents — `grep -r systemd` over the source tree
returned nothing. These units close that gap.

## What the units do differently from macOS

**System services running as an unprivileged user.** Per `ARCHITECTURE.md`, the
Harness authority server manages Fleet with root-owned units that execute as
`nature`, rather than as user services. Consequences the units handle explicitly:

- `HOME` is not set for a system service. Both wrapper scripts resolve
  `$HOME/.config/claude-fleet/runtime.json`, so the installer writes `HOME` and
  `XDG_*` into each unit from `getent passwd`. Without this the services fail at
  startup with a confusing "settings overlay is missing".
- Logs go to files through `fleet log-sink`, not to journald. This is deliberate:
  `fleet doctor`'s runtime checks read the service logs, so on a headless box
  where nobody is tailing anything, keeping them as files is what makes the
  health check meaningful. The sink adds ISO timestamps and caps size, which the
  macOS `StandardErrorPath` setup never did.

**One writer queue.** `db.py` derives its advisory lock as
`<db_path>.writer.lock`. If a unit and the operator CLI disagree about
`db_path` — trivially easy when one is root-managed — you get two lock files and
therefore two independent writer queues against one SQLite file, which is the
worst form of the lock convoy on the machine where you cannot watch it happen.
The units pin `FLEET_RUNTIME_CONFIG` so both sides resolve identically, and
`fleet doctor --db <path>` asserts the paths match.

**Existing Harness units are never touched.** The installer refuses to overwrite
any unit file it did not generate itself, and only ever writes the three
`claude-fleet-*` names.

## Verify on the server after installing

Two assertions that only the real machine can settle. Both are about the fact
that the unit's main PID is `fleet log-sink`, which owns the service as a child:

```sh
systemctl start claude-fleet-coordinator
pkill -f 'claude_fleet.cli .* coordinator'      # kill the child, not the sink
systemctl is-active claude-fleet-coordinator    # must show the restart happened
pgrep -af 'claude_fleet.cli .* coordinator'     # must show exactly one, the new one
```

The first confirms `Restart=on-failure` is reachable — it is not with the obvious
`ExecStart=/bin/sh -c '… | log-sink'` form, because a shell reports the
pipeline's last exit status and the sink always returns 0. The second confirms no
orphaned child survives a restart; `KillMode=control-group` should cover it, but
assert it rather than assume it.

## Install

```sh
sudo packaging/systemd/install-systemd.sh --user nature
```

Then, as `nature`:

```sh
fleet doctor --db "$(python3 -c 'import json;print(json.load(open("'"$HOME"'/.config/claude-fleet/runtime.json"))["db_path"])')"
```

`--dry-run` prints the unit files without installing them.

## Before you install: the coordinator topology question

`ARCHITECTURE.md` forbids two active coordinators without distributed fencing,
and says a multi-machine deployment should put the coordinator on a single Linux
node with sidecars connecting in over an authenticated internal channel.

So enabling `claude-fleet-coordinator.service` here while the macOS
`com.claude-fleet.coordinator` LaunchAgent is still loaded gives you exactly the
configuration the architecture rules out. Decide first:

- **(a) Server is the only coordinator.** Unload the macOS coordinator agent and
  point the laptop's `runtime.json` at the server. Requires the authenticated
  internal channel; the current data plane only knows how to reach a loopback
  coordinator, so this is not a drop-in.
- **(b) Two fully independent installs.** Separate databases, separate account
  pools, no shared route state. Safe today, and the accounts must not overlap —
  two coordinators independently scheduling one subscription pool will each
  believe they own the quota.

The installer does not choose for you; it enables the units and prints this
warning if it detects an unreachable non-loopback coordinator URL.

## Routine maintenance

`claude-fleet-maintenance.timer` runs `fleet maintenance sweep` daily. It exists
because nothing in Fleet reclaimed state: one laptop-day and a half produced
158k events and 6,457 permanently-`active` executions, and `decide_route` reads a
six-hour events window *inside its write transaction* — so table size translates
directly into how long every route decision holds the single writer lock.

`fleet maintenance vacuum` is deliberately not on a timer. VACUUM takes an
exclusive lock for its full duration; run it in a window when the fleet is idle.
