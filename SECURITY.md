# Security

## What this program holds

OAuth credentials for every Claude subscription you give it, in
`~/.config/claude-fleet/teamclaude.json`, mode 0600. It also generates a local
certificate authority so it can inject the right token into requests that
hardcode `api.anthropic.com`.

Both of those are exactly as sensitive as they sound. Everything below is about
keeping the blast radius small.

## Reporting a vulnerability

Open a GitHub security advisory (Security → Report a vulnerability) rather than
a public issue, and please include a way to reproduce it. If you would rather
not use GitHub, open an issue titled "security contact request" with no details
in it and we will find another channel.

There is no bounty. There is a commitment to answer.

## The trust boundary, stated plainly

**Loopback is not an authentication boundary.** Binding to `127.0.0.1` keeps the
network out; it does not distinguish Claude Code from a browser extension, a
build script, or a malicious transitive npm dependency. All of them run as the
same user and can all open a socket to the data plane.

This mattered here: for most of this program's life any local process could
`CONNECT api.anthropic.com` through the proxy and have a real OAuth token
injected into its request, spending a subscription with nothing recording that
it was not Fleet.

What now stands between a local process and your credentials:

| Surface | Control |
|---|---|
| Coordinator (`127.0.0.1:17891`) | Bearer token from `teamclaude.json`, constant-time compare |
| Data plane CONNECT (`127.0.0.1:13456`) | Under `enforce`: the proxy key, **or** a Fleet transport identity / account pin — see the honest reading below |
| Data plane HTTP | Same |
| Credentials on disk | 0600, enforced on every write via a fresh temp file (no window where it exists wider) |
| Local CA private key | 0600 |
| Egress | Loopback bind only; no remote listener exists to attack |

### FLEET_AUTH_MODE

The CONNECT control ships as `audit` and not `enforce`.

In `audit` nothing is refused; anonymous connections are counted and reported on
`/teamclaude/ready` under `auth`. In `enforce` they are refused with a 407.

This is deliberate and it is a real gap, so it is written down rather than
implied. Enforcing blind risks cutting off a legitimate path that does not carry
an identity — an Agent View worker that strips headers, a nested `claude -p`,
something not yet thought of — and taking the fleet down is a worse outcome than
the exposure being closed. The intended sequence is: run in audit until the
counter has stayed at zero across every launch path you use, then set
`FLEET_AUTH_MODE=enforce`. Reverting is an environment variable and a restart.

If you are running on a shared or multi-user machine, set it to `enforce` now
and deal with whatever breaks. The exposure is worse there.

It is a per-host setting, and that is deliberate. On this project's own fleet the
laptop measured 436 routing decisions with zero anonymous connections and was
switched to `enforce`; the server showed two anonymous connections an hour and
stayed in `audit`. Enforcing on the host you cannot account for is how a
security fix becomes an outage.

The audit records who opened the connection, not just that somebody did, and on
the server that answered it:

```
20:05:17 → claude-real (pid 4107459) | api.anthropic.com:443
20:05:18 → claude-real (pid 4107459) | api.anthropic.com:443
```

A Claude Code process that Fleet itself launched — `FLEET_LAUNCH_ID` and
`FLEET_WORKLOAD_ID` are in its environment, it has been running for thirteen
days, and its ordinary traffic does carry the transport identity. Something
inside it opens two connections an hour on a path that does not.

That is the answer the audit existed to produce, and it says **do not enforce on
that host yet**: the caller being refused would be legitimate. It is also the
reason the flip is measured rather than reasoned about. Two connections an hour
from a thirteen-day-old session is not something anybody would have predicted
from reading the code.

#### What `enforce` actually buys, which is less than it sounds like

Two of the three accepted credentials are **not secrets**.

- A Fleet transport identity is `fleet-holder-v1.<base64 of host:launch-id>`.
  Any local process can construct one; the check is that it parses, not that it
  was issued.
- An account pin is a UUID or a name. Both appear in status output.
- The proxy key is a real capability. It is the only one of the three.

So on a default install — where `teamclaude.json` carries `proxy: {port, host}`
and no `apiKey` — `enforce` moves the bar from *nothing* to *know the format*.
That stops an unaware caller, a stray `curl`, a tool that inherited `HTTPS_PROXY`
without meaning to. It does not stop an attacker who has read this file.

Whether that matters depends on what the attacker already has. The adversary
here is a process running as you, and `teamclaude.json` is 0600 — same user, so
it can simply read every token. Getting one injected is not an escalation; it is
a **detour that also launders the attribution**, because Fleet's own records then
say the request was Fleet's.

The fix is known and not yet built: have the launcher mint a random per-launch
capability, register it with the data plane, and verify it in constant time —
so the identity on the wire is something that was *issued* rather than something
that merely *parses*. Until then, treat `enforce` as a filter against accidents,
set `proxy.apiKey` if you want an actual credential on that boundary, and rely on
file permissions for the rest.

(Found by `codex-security scan`, scan `d25efebf`, finding 2. It is written here
rather than quietly fixed later because a document that overstates a control is
worse than a weak control somebody knows about.)

## The local certificate authority

Generated per machine and used to terminate TLS for `api.anthropic.com` so the
correct account's token can be injected.

It is **not** installed into the system or browser trust stores. Only the Claude
Code child process trusts it, through `NODE_EXTRA_CA_CERTS` in its environment.
A CA in the system store would let anything holding that key impersonate any
site to your whole machine; scoping it to one child process is the difference
between a tool and a foothold.

The private key never leaves the machine that generated it.

## What is deliberately impossible

- **No API keys, no usage credits.** Nothing here can create spend outside a
  subscription you already hold.
- **No third-party upstreams.** Removed from the vendored data plane.
- **No IP evasion.** Upstream can provision a residential proxy to work around
  IP rate limits; that is refused at startup and again on every config reload,
  so a live process cannot be handed one it would have declined to start with.
- **No remote listener.** Both services bind loopback only.

## Credentials and the repository

The published tree carries no credentials, and the full history has been scanned
for them. Account identifiers and email addresses that appeared in comments and
test fixtures are replaced with stable fakes under `example.com`.

If you fork this, note that `~/.config/claude-fleet/` and
`~/.local/state/claude-fleet/` are outside the repository and hold the real
thing. Database snapshots taken by `quotapool backup` contain account policy and
routing history and are written 0600 for that reason.

## Known limitations

- **Two hosts sharing a subscription can overshoot a reserve.** Each learns
  usage from its own responses, so both can act on a stale figure. `host_budget`
  bounds how far either can push the total; it does not eliminate the overshoot.
  See `NOTICE` and the `host_budget` docs.
- **One account in two organisations is refused, not merged.** The account key
  does not yet include the organisation. Rather than silently merging two
  subscriptions into one row, the coordinator excludes both and alarms. Run
  `fleet-identity-check` to see whether this affects you.
- **`fleet-rollback` does not roll back the database.** It snapshots first, and
  `quotapool restore` is a separate deliberate step, because a schema written by a
  newer version is not something git can undo.
