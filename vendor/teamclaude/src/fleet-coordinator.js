// Claude Fleet integration for the controlled local fork.
//
// TeamClaude remains the OAuth/MITM/SSE data plane. Every unpinned subscription
// request asks the persistent Fleet coordinator for the serving account before
// any upstream bytes are sent. Coordinator transport failure is fail-closed;
// a deliberate no-capacity response returns null so TeamClaude's hold loop can
// park the request until a subscription window resets.

function accountId(account) {
  return account.accountUuid || account.name;
}

function header(req, name) {
  const value = req?.headers?.[name.toLowerCase()];
  return Array.isArray(value) ? value[0] : value;
}

function sessionIdFor(ctx, req) {
  return ctx.sessionId
    || header(req, 'x-fleet-session-id')
    || `anonymous:${
      header(req, 'x-fleet-process-id')
      || ctx.transportHolder
      || 'missing'
    }`;
}

export class FleetCoordinator {
  constructor(config = {}) {
    this.url = String(config.url || 'http://127.0.0.1:17891').replace(/\/$/, '');
    this.token = config.token || process.env.FLEET_COORDINATOR_TOKEN || '';
    // The coordinator is local and fail-closed. Its SQLite busy timeout is 10s;
    // aborting at 3s makes concurrent Agent View requests retry together and
    // amplifies a short lock convoy into a persistent 502 storm.
    // Old installers wrote 3000ms even though SQLite's busy timeout is 10s.
    // Clamp legacy/custom values upward so a stale config cannot recreate a
    // synchronized retry storm after the code has been upgraded.
    this.timeoutMs = Math.max(15_000, Number(config.timeoutMs || 15_000));
    this.leaseHeartbeatMs = Number(config.leaseHeartbeatMs || 30_000);
    // A Team/subagent fan-out can issue many simultaneous model requests for
    // one Claude session. They need one route lookup, not N identical HTTP
    // calls and N competing SQLite transactions.
    this.pendingSelections = new Map();
  }

  async _post(path, body) {
    let response;
    try {
      response = await fetch(`${this.url}${path}`, {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          ...(this.token ? { authorization: `Bearer ${this.token}` } : {}),
        },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch (error) {
      const unreachable = new Error(
        `Fleet coordinator unavailable: ${error.message}`);
      // Marked so `_select` can wait instead of killing the request. The error
      // still propagates for callers that genuinely cannot continue (renew),
      // but routing has a better option than dying.
      unreachable.fleetCoordinatorUnreachable = true;
      throw unreachable;
    }
    let result = {};
    try { result = await response.json(); } catch {}
    return { response, result };
  }

  async renew({ sessionId, holder, generation }) {
    const { response, result } = await this._post('/v1/writer/renew', {
      session_id: sessionId,
      holder,
      generation,
    });
    if (!response.ok) {
      throw new Error(
        `Fleet writer renewal rejected (${response.status}): `
        + `${result.message || result.error || 'unknown error'}`,
      );
    }
    return result;
  }

  _startHeartbeat(ctx, lease) {
    if (ctx.stopFleetHeartbeat) return;
    let stopped = false;
    const tick = async () => {
      if (stopped) return;
      try {
        await this.renew({
          sessionId: lease.sessionId,
          holder: lease.holder,
          generation: lease.generation,
        });
      } catch (error) {
        // Never turn heartbeat failure into a direct upstream path. An already
        // streaming response cannot be safely spliced or replayed.
        console.error(`[ClaudeFleet] writer heartbeat failed: ${error.message}`);
      }
    };
    const timer = setInterval(tick, this.leaseHeartbeatMs);
    timer.unref?.();
    ctx.stopFleetHeartbeat = () => {
      stopped = true;
      clearInterval(timer);
      delete ctx.stopFleetHeartbeat;
    };
  }

  async select(args) {
    const { accountManager, ctx, req } = args;
    const excluded = [...ctx.tried]
      .map(index => accountManager.accounts[index])
      .filter(Boolean)
      .map(accountId)
      .sort();
    const key = JSON.stringify([
      sessionIdFor(ctx, req),
      ctx.model || '',
      ctx.advisorModel || '',
      excluded,
    ]);
    const existing = this.pendingSelections.get(key);
    if (existing) return existing;
    const pending = this._selectOrWait(args).finally(() => {
      if (this.pendingSelections.get(key) === pending) {
        this.pendingSelections.delete(key);
      }
    });
    this.pendingSelections.set(key, pending);
    return pending;
  }

  /**
   * May this request pin this account?
   *
   * A pin bypasses scheduling by design. It must not bypass the shared-account
   * reserve, which protects another person's quota. Measured before this
   * existed: a `/tc-acct/<uuid>` request reached the upstream with zero
   * route.changed events recorded, so the floor never ran.
   *
   * Fails CLOSED on every transport failure, deliberately.
   *
   * The first version failed closed only for `account.kind === 'shared'` and
   * open otherwise. That was worthless: `kind` lives in the control plane's
   * accounts table and `makeAccount` (account-manager.js:54) does not carry it,
   * so the field is ALWAYS undefined here — every account, shared included,
   * took the fail-open branch. Checked against the live config: no account
   * entry has a `kind` field either.
   *
   * The data plane cannot know which account is shared without asking, so the
   * only safe rule is: no answer, no pin. Pinning is a rare manual override,
   * not the request path — scheduled traffic is untouched by this, and losing
   * pins during a coordinator outage is a far smaller cost than spending
   * someone else's quota.
   */
  async isPinPermitted({ account }) {
    if (!account) return { allowed: true };
    const id = accountId(account);
    let response;
    let result;
    try {
      ({ response, result } = await this._post('/v1/pin/check', { account_id: id }));
    } catch (error) {
      return {
        allowed: false,
        reason: `Fleet coordinator unreachable (${error.message}); `
          + 'refusing to pin because the shared-account reserve cannot be checked.',
      };
    }
    // Require an explicit yes. A 500, a 401 or a malformed body all leave
    // `result.allowed` undefined, and treating undefined as permission would
    // make any coordinator error a way through the reserve.
    if (!response?.ok || result?.allowed !== true) {
      const reason = result?.reason
        || `coordinator returned ${response?.status ?? 'no status'}`;
      console.error(`[ClaudeFleet] refused pin to ${account.name}: ${reason}`);
      return { allowed: false, reason };
    }
    return { allowed: true };
  }

  /**
   * A coordinator that cannot be reached is a reason to WAIT, not to fail.
   *
   * Measured: 246 of these in seven days, all of them on Jul 29-31 when the
   * fleet was struggling — 210 timeouts and 36 fetch failures, most likely the
   * coordinator being slow under the load that several other bugs were
   * creating. Every one killed its request, because server.js turns any throw
   * from forwardRequest into `502 / proxy_error`, and a dead request is one a
   * human has to notice and re-send.
   *
   * None in the four days since those bugs were fixed. Fixing it anyway: the
   * failure mode is wrong regardless of how often it fires, and a coordinator
   * restart takes about four seconds — a routine operation should not cost
   * anyone their in-flight work.
   *
   * One retry covers a restart. Beyond that, returning null routes into the
   * ordinary hold loop, which waits and retries on its own — the same answer
   * this fleet gives every other shortage.
   */
  async _selectOrWait(args) {
    try {
      return await this._select(args);
    } catch (error) {
      if (!error?.fleetCoordinatorUnreachable) throw error;
      console.error(
        '[ClaudeFleet] %s coordinator unreachable — retrying once, then holding. '
        + 'This is NOT a capacity shortage. cause=%s session=%s',
        new Date().toISOString(), error.message, args?.ctx?.sessionId || '?',
      );
      await new Promise(resolve => setTimeout(resolve, 2000));
      try {
        return await this._select(args);
      } catch (again) {
        if (!again?.fleetCoordinatorUnreachable) throw again;
        // The cause is the whole diagnosis. "Unreachable" covers a refused
        // connection (the service is down) and an AbortSignal timeout (the
        // service is up but a SQLite write transaction is queued behind
        // others) — opposite problems with opposite fixes. Logging only the
        // fixed sentence cost hours: 23 MB of identical undated lines that
        // could not even be dated, let alone attributed.
        console.error(
          '[ClaudeFleet] %s coordinator still unreachable; holding the request '
          + 'rather than failing it. cause=%s session=%s timeoutMs=%s',
          new Date().toISOString(), again.message,
          args?.ctx?.sessionId || '?', this.timeoutMs,
        );
        return null;
      }
    }
  }

  async _select({ accountManager, ctx, req }) {
    const accounts = accountManager.accounts.map(account => ({
      id: accountId(account),
      name: account.name,
      index: account.index,
      account_uuid: account.accountUuid,
      org_uuid: account.orgUuid,
      priority: account.priority || 0,
      disabled: !!account.disabled,
      status: account.status,
      // Usage-based API keys and third-party upstreams are deliberately outside
      // Fleet. No automatic route may create subscription-external spend.
      eligible: account.type === 'oauth'
        && !account.upstream
        && accountManager.isAccountAvailable(account, ctx.model, ctx.advisorModel),
      quota: account.quota || {},
    }));
    const excludeIds = [
      ...[...ctx.tried]
        .map(index => accountManager.accounts[index])
        .filter(Boolean)
        .map(accountId),
      // Ids the coordinator named that this data plane cannot resolve. They
      // have no index, so they cannot live in ctx.tried; without carrying them
      // separately the retry below would just be told the same account again.
      ...(ctx.unresolvable || []),
    ];
    // Claude normally supplies x-claude-code-session-id. Some headless print
    // and supervisor paths omit it even when Fleet launched an explicit
    // --session-id/--resume target. The launcher adds x-fleet-session-id as a
    // trusted fallback so the request keeps its preflight writer lease and
    // route instead of creating a second anonymous lineage.
    // Agent View may strip every request header, including Claude's native
    // session id and Fleet's fallback. The authenticated loopback CONNECT
    // still carries the launch-family holder, which is unique per window and
    // stable for its descendants.
    const sessionId = sessionIdFor(ctx, req);
    const hasFleetHeaders = Object.keys(req?.headers || {})
      .some(name => name.toLowerCase().startsWith('x-fleet-'));
    const headerHolder = header(req, 'x-fleet-launch-id')
      || header(req, 'x-fleet-process-id');
    // Agent View workers can strip ANTHROPIC_CUSTOM_HEADERS wholesale while
    // retaining the CONNECT proxy environment. Only a header-free request may
    // use the loopback CONNECT closure's non-secret holder. A partial header
    // set is rejected by the coordinator rather than mixed with transport
    // identity from another channel.
    const holder = headerHolder
      || (!hasFleetHeaders ? ctx.transportHolder : null);
    const body = {
      session_id: sessionId,
      // A writer is a top-level Claude launch family, not an individual child
      // process. Subagents and Team members may share the parent's Claude
      // session ID, so fencing each child PID would reject legitimate family
      // work. Distinct terminal windows still receive distinct launch IDs.
      holder,
      process_id: header(req, 'x-fleet-process-id'),
      execution_id: header(req, 'x-fleet-execution-id'),
      parent_execution_id: header(req, 'x-fleet-parent-execution-id'),
      workload_class: header(req, 'x-fleet-workload-class') || 'interactive',
      workload_id: header(req, 'x-fleet-workload-id'),
      model: ctx.model,
      advisor_model: ctx.advisorModel,
      exclude_account_ids: excludeIds,
      accounts,
    };

    const { response, result } = await this._post('/v1/route/decide', body);
    if (response.status === 409 && result.error === 'no_capacity') {
      // "All accounts exhausted — holding connection" is what the operator sees
      // when this returns null, and on its own it is indistinguishable between
      // "the subscriptions really are spent" and "this request excluded every
      // account and the fleet has capacity". Those need opposite responses, so
      // print the per-account verdict that produced the decision. One line per
      // decision, which is the same cadence as the "holding connection" line it
      // explains — a held request re-decides at most once every 60s.
      const verdict = accounts.map(item => {
        const reasons = [];
        if (excludeIds.includes(item.id)) reasons.push('tried-this-request');
        if (item.disabled) reasons.push('disabled');
        if (item.status === 'error') reasons.push('auth-error');
        if (!item.eligible && !item.disabled && item.status !== 'error') {
          const weekly = item.quota?.unified7d;
          const fiveHour = item.quota?.unified5h;
          reasons.push(
            `ineligible(7d=${weekly ?? '?'},5h=${fiveHour ?? '?'})`,
          );
        }
        return `${item.name}:${reasons.length ? reasons.join('+') : 'available'}`;
      });
      console.error(
        `[ClaudeFleet] no_capacity for session ${sessionId} — ${verdict.join(' ')}`,
      );
      return null;
    }
    if (!response.ok) {
      const error = new Error(
        `Fleet coordinator rejected route (${response.status}): `
        + `${result.message || result.error || 'unknown error'}`,
      );
      if (response.status === 422 && /holder is required/i.test(
        String(result.message || result.error || ''),
      )) {
        // This Claude was not launched through Fleet, so there is no writer to
        // fence and the coordinator refuses to route it.
        //
        // Refusing is the policy and is not weakened here. What IS wrong is the
        // label: the generic catch in server.js turns every throw into
        // `502 / proxy_error / "Internal proxy error"`, so a launch mistake
        // reads as a network fault in the proxy. Measured on the server: 576 a
        // day, on a fixed schedule, every one of them a 502 nobody could
        // attribute — the caller dies and the reason names the wrong component.
        error.fleetStatus = 400;
        error.fleetPublicError = {
          type: 'invalid_request_error',
          message: (
            'This Claude was not launched through Fleet, so Fleet cannot fence '
            + 'its writer and will not route it. Start it with `fleet run`, or '
            + 'inherit the launcher environment.'
          ),
        };
        // Name the caller. Without this the log says only that SOMETHING
        // bypassed the launcher — 576 times a day, in bursts, and after an
        // afternoon of digging the answer was still "something on a schedule".
        // These requests carry no x-fleet-* headers by definition, so log what
        // they DO carry.
        console.error(
          '[ClaudeFleet] refused an unfenced launch — '
          + `session=${header(req, 'x-claude-code-session-id') || '(none)'} `
          + `ua=${(header(req, 'user-agent') || '(none)').slice(0, 80)} `
          + `model=${ctx.model || '(none)'} `
          + `path=${req.url || '(none)'}`,
        );
      } else if (
        response.status === 422
        && ['lease_conflict', 'stale_writer'].includes(result.error)
      ) {
        // This is a deliberate single-writer fence, not a transient proxy
        // outage. Surface a non-retryable conflict so Claude Code does not turn
        // one duplicate-session mistake into ten misleading 502 retries.
        error.fleetStatus = 409;
        error.fleetPublicError = {
          type: 'invalid_request_error',
          message: (
            'Claude Fleet prevented concurrent writers for this session. '
            + 'Close the duplicate window or explicitly take over its writer lease.'
          ),
        };
      }
      throw error;
    }
    const selected = accountManager.accounts.find(
      account => accountId(account) === result.account_id,
    );
    if (!selected) {
      // The coordinator named an account this data plane has no credential for.
      //
      // This used to throw, and server.js turns any throw from forwardRequest
      // into `502 / proxy_error / "Internal proxy error"` — an error about the
      // proxy, for a condition with nothing to do with the proxy. That is what
      // sent everyone looking at the proxy while the server 502'd every request
      // on legacy account rows the coordinator could still select.
      //
      // Failing the request is the wrong response for a second reason: it makes
      // a human re-send the message. Waiting is not the problem; erroring is.
      // So exclude the id and ask once more, and if the fleet still has nothing
      // to offer, return null and let the hold loop wait for capacity the way
      // it does for any other shortage. The session then resumes by itself.
      (ctx.unresolvable ||= new Set()).add(result.account_id);
      console.error(
        `[ClaudeFleet] coordinator chose "${result.account_id}", which this host `
        + 'has no credential for — excluding it and re-asking. If this repeats, '
        + 'the accounts table holds ids the data plane cannot execute.',
      );
      if (!ctx.unresolvableRetried) {
        ctx.unresolvableRetried = true;
        return this._select({ accountManager, ctx, req });
      }
      return null;
    }
    if (
      selected.type !== 'oauth'
      || selected.upstream
      || ctx.tried.has(selected.index)
      || !accountManager.isAccountAvailable(selected, ctx.model, ctx.advisorModel)
    ) {
      throw new Error(`Fleet coordinator selected ineligible account "${selected.name}"`);
    }
    this._startHeartbeat(ctx, {
      sessionId,
      holder: result.writer_holder || body.holder,
      generation: result.writer_generation,
    });
    return selected;
  }
}
