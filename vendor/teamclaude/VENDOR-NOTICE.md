# Vendored TeamClaude data plane

This directory is based on `@karpeleslab/teamclaude` 1.1.11, downloaded from
the npm registry on 2026-07-28.

- Upstream package integrity:
  `sha512-sm2xfYAmA8yv4bGnyNNgdhgNTU03NXn56NyWiUFwuYgFYobMjjDqrVgwAjF9JuS6PHbs+GPswBhp1jIyYz0ZFw==`
- Downloaded tarball SHA-256:
  `914bdc683a550514cda4da87bbc9651fafd7154752bd30160673d73294a72651`
- Upstream license: MIT; retained in `LICENSE`.

Fleet-specific changes:

1. Account selection can be delegated to the authenticated, loopback-only
   Claude Fleet coordinator before an upstream request begins.
2. Coordinator transport failure is fail-closed. Deliberate no-capacity state
   remains compatible with TeamClaude's connection hold loop.
3. Fleet mode accepts subscription OAuth accounts only, binds to loopback,
   disables keep-warm and self-update, and rejects sx.org routing.
4. TeamClaude continues to own OAuth refresh, MITM, request-body account UUID
   rewrite, SSE forwarding and pre-response quota retry.

The fork does not claim upstream endorsement.
