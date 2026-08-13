// The coordinator naming an account this host cannot execute must not fail the
// request.
//
// It used to throw, and server.js turns any throw out of forwardRequest into
// `502 / proxy_error / "Internal proxy error"` — an error about the proxy, for
// a condition with nothing to do with the proxy. That misdirection is exactly
// what happened: the server 502'd every request on legacy account rows while
// everyone looked at the proxy.
//
// Failing is wrong for a second reason. A failed request makes a human re-send
// the message. Waiting does not — the session resumes by itself when capacity
// returns. So the rule is: exclude the unusable id, ask again, and if there is
// still nothing, return null so the normal hold path waits.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { FleetCoordinator } from '../src/fleet-coordinator.js';

function harness({ answers }) {
  const asked = [];
  const coordinator = new FleetCoordinator({ baseUrl: 'http://127.0.0.1:1' });
  coordinator._post = async (path, body) => {
    asked.push(body);
    const next = answers.shift();
    return { response: { ok: true, status: 200 }, result: next };
  };
  return { coordinator, asked };
}

const accountManager = {
  accounts: [{
    name: 'real', index: 0, type: 'oauth', accountUuid: 'uuid-real',
    quota: {}, status: 'active',
  }],
  isAccountAvailable: () => true,
};

function context() {
  return { tried: new Set(), model: 'claude-sonnet-5', sessionId: 's1' };
}

const request = { headers: { 'x-fleet-launch-id': 'launch-1' }, url: '/v1/messages' };

test('an unresolvable account is excluded and the coordinator re-asked', async () => {
  const { coordinator, asked } = harness({
    answers: [{ account_id: 'ghost' }, { account_id: 'uuid-real' }],
  });
  const ctx = context();

  const account = await coordinator._select({ accountManager, ctx, req: request });

  assert.equal(account?.name, 'real', 'the second answer should be used');
  assert.equal(asked.length, 2, 'exactly one retry');
  assert.ok(
    asked[1].exclude_account_ids.includes('ghost'),
    'the retry must tell the coordinator not to offer that id again — it has no '
    + 'account index, so ctx.tried cannot carry it',
  );
});

test('a second unresolvable answer waits instead of failing', async () => {
  const { coordinator, asked } = harness({
    answers: [{ account_id: 'ghost' }, { account_id: 'ghost-two' }],
  });
  const ctx = context();

  const account = await coordinator._select({ accountManager, ctx, req: request });

  assert.equal(account, null, 'null routes into the hold path, which waits');
  assert.equal(asked.length, 2, 'retries once, not forever');
});

test('a Claude not launched through Fleet gets told so, not a proxy error', async () => {
  // Refusing is the policy and stays. The defect is the label: server.js turns
  // every throw into `502 / proxy_error / "Internal proxy error"`, so a launch
  // mistake reads as a fault in the proxy. Measured on the server: 576 of these
  // a day, on a fixed schedule, and nobody could attribute a single one.
  const coordinator = new FleetCoordinator({ baseUrl: 'http://127.0.0.1:1' });
  coordinator._post = async () => ({
    response: { ok: false, status: 422 },
    result: { message: 'holder is required; launch Claude through Fleet' },
  });

  let caught;
  try {
    await coordinator._select({ accountManager, ctx: context(), req: request });
  } catch (err) {
    caught = err;
  }

  assert.ok(caught, 'the request is still refused');
  assert.notEqual(caught.fleetStatus, 502,
    'a launch mistake is not a proxy failure');
  assert.equal(caught.fleetPublicError.type, 'invalid_request_error');
  assert.match(caught.fleetPublicError.message, /not launched through Fleet/,
    'the message has to name the actual cause, or the next person spends an '
    + 'afternoon looking at the proxy again');
});

test('an unreachable coordinator waits instead of killing the request', async () => {
  // 246 of these in seven days, all while the fleet was struggling — 210
  // timeouts, 36 fetch failures. Every one killed its request, because
  // server.js turns any throw out of forwardRequest into 502/proxy_error, and
  // a dead request is one a human has to notice and re-send.
  //
  // A coordinator restart takes about four seconds. A routine operation must
  // not cost anyone their in-flight work.
  let attempts = 0;
  const coordinator = new FleetCoordinator({ baseUrl: 'http://127.0.0.1:1' });
  coordinator._post = async () => {
    attempts += 1;
    const err = new Error('Fleet coordinator unavailable: timeout');
    err.fleetCoordinatorUnreachable = true;
    throw err;
  };

  const account = await coordinator._selectOrWait(
    { accountManager, ctx: context(), req: request });

  assert.equal(account, null, 'null routes into the hold path, which waits');
  assert.equal(attempts, 2, 'exactly one retry — enough for a restart, not a spin');
});

test('a retry that succeeds is used', async () => {
  let attempts = 0;
  const coordinator = new FleetCoordinator({ baseUrl: 'http://127.0.0.1:1' });
  coordinator._post = async () => {
    attempts += 1;
    if (attempts === 1) {
      const err = new Error('Fleet coordinator unavailable: timeout');
      err.fleetCoordinatorUnreachable = true;
      throw err;
    }
    return { response: { ok: true, status: 200 }, result: { account_id: 'uuid-real' } };
  };

  const account = await coordinator._selectOrWait(
    { accountManager, ctx: context(), req: request });
  assert.equal(account?.name, 'real', 'the restart finished; the request lives');
});

test('an unrelated error is not swallowed', async () => {
  // Only the unreachable case waits. Anything else must keep propagating, or
  // this becomes a catch-all that hides real faults — the exact shape of the
  // proxy_error misdirection it exists to undo.
  const coordinator = new FleetCoordinator({ baseUrl: 'http://127.0.0.1:1' });
  coordinator._post = async () => { throw new Error('something else entirely'); };

  await assert.rejects(
    () => coordinator._selectOrWait({ accountManager, ctx: context(), req: request }),
    /something else entirely/,
  );
});
