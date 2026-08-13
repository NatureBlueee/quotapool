---
name: Bug report
about: Something behaves differently from what the docs say
labels: bug
---

## What happened

<!-- What you expected, what happened instead. -->

## `quotapool doctor`

<!-- Paste the output. Most failures here are environmental — a missing timer,
     an unreachable peer, a stale token — and `doctor` names which one. A report
     without it usually cannot be acted on.

     It prints structured JSON and contains no credentials. -->

```json

```

## Setup

- OS and version:
- One host or two:
- Number of Claude subscriptions:
- Installed via `install.sh`, or by hand:

## If it involves a second host

<!-- The link between hosts is the fragile part, not the schedulers. Please run
     the probe the way the timer actually runs it — no agent, no reused
     connection: -->

```sh
env -i HOME="$HOME" PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  ssh -o ControlMaster=no -o ControlPath=none -o BatchMode=yes <peer> true
```
