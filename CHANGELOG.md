# Changelog

Notable changes. The format is loosely [Keep a Changelog](https://keepachangelog.com);
versions follow [SemVer](https://semver.org), and while the major is `0` the
minor may still change behaviour.

The repository's own history begins on 2026-07-30 and is not published — it is
142 commits of one fleet's operational life, including addresses and hostnames.
This file is the public record of what changed and why.

## [0.4.8] — 2026-08-12

First public release. Everything below already ran in production on a two-host
fleet before it was published; nothing here is aspirational.

### Recovery, which is the part the owner asked for

- **`fleet-watchdog`** — probes both hosts every 5 minutes with an
  *authenticated* request (`/v1/pin/check`, not `/health`: a fleet with broken
  authentication answers `/health` perfectly). Three consecutive failures roll
  back to the last known-good version and raise an alarm the status line shows.
  Never acts twice in a row; never rolls back to the version already running.
- **"Cannot reach" is not "is down".** From a laptop those look identical and
  call for opposite actions — rolling back both hosts because the laptop lost
  wifi would make the watchdog the outage. Blindness gets its own counter and
  its own alarm, and never triggers a rollback. Measured: 31 consecutive
  unreachable probes, zero false rollbacks.
- **`fleet-rollback`** — snapshots both databases, checks out, rsyncs, restarts,
  and probes. Every restart's exit code is checked: a failed restart leaves the
  old process holding the port, and a liveness probe cannot tell the difference.
- **`quotapool backup` / `restore`** — `VACUUM INTO`, taken automatically before
  any deploy.
- **`claude-direct`** — an escape hatch that imports nothing from the rest of
  this. It reads one JSON file and execs the real Claude Code binary, so it
  works when the scheduler is broken, including when `claude` itself is.
  Refuses to refresh a token while the data plane is running, because racing
  refreshers on a single-use token is what retired four accounts.

### Fixed, and each one was found the hard way

- **A banned account silently disarmed the entire recovery system.** One of six
  accounts left enabled → a capacity check failed → `doctor` was never clean →
  `mark` refused hourly → the newest known-good point aged to 128 hours → that
  commit predated the watchdog itself, so an automatic rollback would have
  deleted the watchdog as its first act. Marking now ignores checks that
  describe capacity rather than the version, and a rollback point older than
  24 hours raises an alarm.
- **Alarms were written to a field nothing read.** The status line had a
  whitelist of four kinds and silently dropped the rest. Alarms are now split
  into events (a rollback happened — history, kept) and conditions (nowhere to
  roll back to — true now, clears itself when fixed).
- **Live config reload could admit accounts that spend outside a subscription.**
  The subscription-only rules ran once at startup; the TUI, the reload endpoint
  and the CLI notify all went around them. One validator, both paths, plus the
  global upstream pinned to `https://api.anthropic.com` by scheme, host and
  port.
- **About 1 install in 64 failed.** `secrets.token_urlsafe` produces a leading
  `-` roughly 1/64 of the time, and `--token $tok` made argparse read it as an
  option. It surfaced as a flaky test.
- **A mark claimed a commit but verified one host.** The peer has no git, and
  "is it answering?" is answered perfectly well by a host running last week's
  code. Both hosts' deployed content is now fingerprinted and compared.
- **The account switch could be turned off but not on.** Two independent
  switches existed and the CLI read one while writing the other; failure was
  silent.

### Security

- `FLEET_AUTH_MODE` — the CONNECT gap is measured (`audit`, default) or refused
  (`enforce`). `SECURITY.md` states plainly that `enforce` filters accidents
  rather than adversaries, because two of the three credentials it accepts are
  not secrets.
- Credential writes are atomic (temp + fsync + rename) under a lock both the
  Python and Node sides take.
- `THREAT-MODEL.md` names the adversary, ranks what is worth stealing, and lists
  the known-weak areas rather than hiding them — including seven findings from
  an external scan that are recorded and not yet fixed.
- `fleet-make-release` publishes from an allow-list and scans the result for
  credentials, addresses and internal hostnames. It has caught a real leak.

### Not included, on purpose

No API keys, no usage credits, no third-party upstreams, no IP evasion. See
`NOTICE`, "Deliberate omissions".

[0.4.8]: https://github.com/NatureBlueee/quotapool/releases/tag/v0.4.8
