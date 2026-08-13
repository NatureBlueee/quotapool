# Threat model

Written for a security reviewer — human or automated — so that findings land on
what matters here rather than on generic web-app patterns this program does not
have. It is also the `--knowledge-base` input for `codex-security scan`.

## What this program is

A local scheduler for Claude Code across several Claude subscriptions owned by
one person. Two processes, both bound to loopback:

- **Control plane** (Python, `src/claude_fleet/`) — decides which subscription
  serves each request. SQLite state. HTTP on `127.0.0.1:17891`.
- **Data plane** (Node, `vendor/teamclaude/`, forked from TeamClaude) — holds
  the OAuth credentials, runs an HTTPS forward proxy with a local CA, and makes
  the upstream request to Anthropic. HTTP/CONNECT on `127.0.0.1:13456`.

Claude Code is launched with `HTTPS_PROXY` pointing at the data plane, so its
requests are intercepted, given the chosen account's token, and forwarded.

## What is worth stealing

In rough order of how bad it would be:

1. **OAuth access and refresh tokens** for every subscription, in
   `~/.config/claude-fleet/teamclaude.json` (0600). A refresh token is a
   long-lived credential to somebody's paid account.
2. **The ability to spend a subscription** without holding the token — anything
   that can get the data plane to inject a token into an attacker's request has
   the money without needing the credential.
3. **The local CA's leaf private key**, `teamclaude-leaf.key` (0600). Combined
   with the CA cert it can impersonate `api.anthropic.com` to anything that
   trusts the CA.
4. **The coordinator token**, in `teamclaude.json` under
   `fleetCoordinator.token`. Grants routing decisions, account state changes and
   the pin/writer APIs.
5. **Routing history and account policy**, in `~/.local/state/claude-fleet/`.
   Reveals usage patterns and which subscriptions exist.

## Who the adversary is

**A process running as the same user.** This is the whole model. There is no
remote listener; both services bind `127.0.0.1`, so a network attacker has no
surface. What there is instead:

- a malicious or compromised transitive npm/PyPI dependency in any project on
  the machine
- a browser extension that can reach localhost
- a build script, a CI runner, a shared-machine coworker's process
- Claude Code itself running attacker-supplied instructions in a repository

Every one of those can open a socket to `127.0.0.1:13456`.

**Loopback is not an authentication boundary.** Binding to localhost keeps the
network out and distinguishes nothing else. This program got that wrong for most
of its life. `FLEET_AUTH_MODE` is the partial answer: `audit` (measure, allow)
ships by default, `enforce` refuses.

Read `enforce` honestly. It accepts three things, and only one of them is a
secret: the proxy key. A Fleet transport identity
(`fleet-holder-v1.<base64>`) is checked for shape, not issuance, and an account
pin is a UUID or a name that appears in status output — both are locally
constructible. `enforce` therefore filters accidents and unaware callers; it is
not a boundary against an adversary who has read the source. The fix — a random
per-launch capability, registered by the launcher and verified in constant time
— is known and not built. See `SECURITY.md` for the full reading.

## Trust boundaries, and what crosses each

| Boundary | What crosses | Control |
|---|---|---|
| Local process → data plane CONNECT | A request that will get a real token injected | Fleet transport identity, account pin, or proxy key. `FLEET_AUTH_MODE=enforce` required for this to be enforced |
| Local process → coordinator HTTP | Routing decisions, account state, writer leases | Bearer token, constant-time compare. `if not expected: return True` was a fail-open and is now mode-gated |
| Data plane → Anthropic | The credential itself | TLS to Anthropic; the local CA never signs for anything but the interception point |
| Claude Code → local CA | Trusts a CA that can impersonate `api.anthropic.com` | Scoped to the child process via `NODE_EXTRA_CA_CERTS`; never installed in the system or browser trust stores |
| Host → host (two-machine fleets) | Quota readings, over SSH | The operator's own SSH trust. Credentials are NOT distributed automatically |
| Repository → public | Source, tests, docs | `fleet-make-release` allow-list plus a scan for credentials, emails and home directories |

## What we are specifically defending against

Ordered by how much a reviewer's attention is worth here:

1. **Token exfiltration.** Any path by which a local process obtains an access
   or refresh token, or gets one injected into a request it controls. Includes
   logs, error messages, process arguments, the status line, crash dumps, and
   the backup files under `~/.local/state/claude-fleet/backups/`.
2. **Credential file corruption.** `teamclaude.json` holds every account. A
   torn write loses all of them at once and needs a browser to recover. Writes
   go through a temp file + fsync + rename, under a lock both the Node and
   Python sides take (`O_EXCL`, because Node has no portable flock).
3. **Refresh-token races.** OAuth refresh tokens are single-use. Two refreshers
   racing produce `invalid_grant` and retire a healthy account. This has
   happened here: 588 false "needs re-login" lines and four accounts dropped
   from rotation over three days.
4. **Privilege escalation via the CA.** Anything that widens the CA's trust
   beyond the one child process, or that leaks the leaf key.
5. **Spending outside a subscription.** By design nothing here can use an API
   key, usage credits, or a third-party upstream. A path that reintroduces one
   is a finding.
6. **Silent capacity or credential loss.** Not a classical vulnerability, but
   the failure mode this system actually suffers: four accounts disabled for
   four days with nothing reporting it.

## What is out of scope, and why

- **Remote attackers.** No remote listener exists. A finding that assumes one is
  describing a different program.
- **Anthropic's own API security.** Not ours to reason about.
- **Multi-tenant isolation.** This is a single-user tool. On a shared machine
  the correct posture is `FLEET_AUTH_MODE=enforce` and OS-level user separation;
  the program does not attempt to isolate users from each other.
- **The optimizer's economics.** Choosing a suboptimal account wastes quota. It
  is a correctness concern, not a security one.

## Known-weak areas, stated rather than hidden

A reviewer should look hardest here, and should not have to discover these:

- **`FLEET_AUTH_MODE` defaults to `audit`**, so the CONNECT gap is measured
  rather than closed on a fresh install.
- **The account key is `accountUuid`, not `accountUuid/orgUuid`.** One Claude
  account in two organisations would collapse into one row. The coordinator now
  refuses and alarms rather than merging, but the underlying key is unchanged.
  `fleet-identity-check` reports whether this affects a given fleet.
- **Two hosts can overshoot a reserve.** Each learns usage from its own
  responses. `host_budget` bounds the overshoot; it does not eliminate it.
- **The vendored data plane is a fork**, at `vendor/teamclaude/` — upstream
  TeamClaude 1.1.11 with the changes listed in `NOTICE`. Findings there may or
  may not be ours; check `vendor/teamclaude/VENDOR-NOTICE.md`.
- **The MITM terminates TLS for `api.anthropic.com`.** That is the mechanism,
  not a bug: it is how a request that hardcodes the endpoint gets the right
  account's token. The security property is the CA's scope, not its absence.

## What a review already found and what was done about it

`codex-security scan`, scan `d25efebf`, over the credential-handling and
auth-boundary files (`mitm.js`, `config.js`, `oauth.js`, `account-manager.js`,
`coordinator.py`, `accounts.py`, `claude-direct`). Nine findings. Two were
fixed, seven are recorded here — because a finding that is understood and
written down is a different thing from one that is buried, and a reviewer should
not have to rediscover them.

The triage rule: the adversary is a process running as this user, and
`teamclaude.json` is 0600 — so it can already read every token. A finding that
only lets it *use* a token grants nothing it could not do directly; those are
attribution gaps. A finding that sends credentials somewhere new, or spends
outside a subscription, is a real escalation.

**Fixed**

- **#4 — a global upstream was not pinned.** `config.upstream` fed both the
  certificate host and the credential sink with no allow-list. Now
  `applyFleetPolicy` requires scheme, host and port to equal
  `https://api.anthropic.com`.
- **#5 — live reload could admit forbidden accounts.** The subscription-only
  rules ran once at startup; the TUI's R key, `POST /teamclaude/reload` and the
  CLI notify all went around them, so a running process could be handed an
  API-key account it would have refused to start with. Same validator, now on
  both paths.

**Recorded, not fixed**

- **#1 — `audit` is the default**, so an anonymous CONNECT is counted rather
  than refused on a fresh install. Already stated above; the reasoning is in
  `SECURITY.md`.
- **#2 — `enforce` accepts non-secret identities.** See above. Documented
  rather than overstated.
- **#3 — the coordinator fails open with no bearer token configured.** Mode-
  gated, and an install with no token has no secret to check; the same file
  permissions argument applies.
- **#6 — refresh and persistence are not serialised end to end.** Two halves.
  `claude-direct` refuses to refresh at all while the data plane is listening,
  which is the mitigation for its half. The other half is real: `withConfigLock`
  writes anyway after a stale-lock timeout, and that fallback can drop a rotated
  token. It exists so a dead lock cannot wedge the data plane permanently —
  losing a token is recoverable, a wedged fleet during an outage is not — but
  the trade is a choice, not an oversight.
- **#7 — a caller-chosen session id inherits a live writer lease** without
  proving it belongs to that launch family.
- **#8 — `pin_check` does not apply the organisation-collision refusal** that
  `decide` does, so a pin could select a colliding credential. Needs an actual
  collision to matter; `fleet-identity-check` reports none on this fleet, and
  the underlying single-key design is already listed above.
- **#9 — the OAuth login callback** processes an error and rejects the login
  promise before validating `state`, and binds without an explicit loopback
  host. Upstream code, login-time only.

## Things that look like findings and are not

Stated so a reviewer does not spend effort on them:

- `sk-ant-oat01-claude-fleet-local-sentinel-not-a-credential` in
  `launcher.py` is a deliberately invalid token. Claude Code refuses to build an
  inference request without one; the data plane replaces it. It cannot authorise
  anything.
- `sk-ant-…-fleet-shadow-…-not-a-credential` in `scripts/fleet-shadow` are fake
  credentials for the isolated test instance, which must not be able to spend a
  real subscription even by accident.
- Short `sk-ant-oat01-*` strings throughout `tests/` are fixtures.
- `/health` on the coordinator takes no credentials. Deliberate: every
  supervisor in the system depends on it, and it carries nothing. The
  authenticated probe used for real health checks is `/v1/pin/check`.
