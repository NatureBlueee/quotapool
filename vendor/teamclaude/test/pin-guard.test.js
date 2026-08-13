// The pin guard must fail CLOSED, and the first version did not.
//
// It keyed the fail-open decision on `account.kind === 'shared'`. But `kind`
// lives in the control plane's accounts table and `makeAccount`
// (account-manager.js:54) does not carry it — checked against the live config,
// no account entry has the field either. So `kind` was ALWAYS undefined and
// every account, shared included, took the fail-open branch. The guard would
// have evaporated during exactly the outage it exists for.
//
// A second hole: `_post` resolves for any HTTP status, so a 500 or 401 left
// `result.allowed` undefined, and the old code treated anything other than an
// explicit `false` as permission.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { FleetCoordinator } from '../src/fleet-coordinator.js';

function coordinator(post) {
  const instance = new FleetCoordinator({ url: 'http://127.0.0.1:1' });
  instance._post = post;
  return instance;
}

const account = { name: 'shared@example.com', accountUuid: 'u-1' };

test('an explicit yes permits the pin', async () => {
  const fleet = coordinator(async () => ({
    response: { ok: true, status: 200 }, result: { allowed: true },
  }));
  assert.equal((await fleet.isPinPermitted({ account })).allowed, true);
});

test('an explicit no refuses, carrying the reason through', async () => {
  const fleet = coordinator(async () => ({
    response: { ok: true, status: 200 },
    result: { allowed: false, reason: 'at its reserve floor' },
  }));
  const verdict = await fleet.isPinPermitted({ account });
  assert.equal(verdict.allowed, false);
  assert.match(verdict.reason, /reserve floor/);
});

test('a transport failure refuses — regardless of kind', async () => {
  // The regression: `kind` is undefined in the data plane, so a rule keyed on
  // it fails open for shared accounts too.
  const fleet = coordinator(async () => { throw new Error('ECONNREFUSED'); });
  for (const candidate of [account, { ...account, kind: 'shared' }, { ...account, kind: 'owned' }]) {
    const verdict = await fleet.isPinPermitted({ account: candidate });
    assert.equal(verdict.allowed, false, `kind=${candidate.kind}`);
    assert.match(verdict.reason, /unreachable/);
  }
});

test('a coordinator 500 refuses rather than passing through', async () => {
  const fleet = coordinator(async () => ({
    response: { ok: false, status: 500 }, result: { error: 'internal_error' },
  }));
  const verdict = await fleet.isPinPermitted({ account });
  assert.equal(verdict.allowed, false);
  assert.match(verdict.reason, /500/);
});

test('an unauthorized coordinator refuses', async () => {
  const fleet = coordinator(async () => ({
    response: { ok: false, status: 401 }, result: { error: 'unauthorized' },
  }));
  assert.equal((await fleet.isPinPermitted({ account })).allowed, false);
});

test('a malformed body refuses', async () => {
  // `_post` swallows JSON parse errors and yields {}, which must not read as
  // permission.
  const fleet = coordinator(async () => ({ response: { ok: true, status: 200 }, result: {} }));
  assert.equal((await fleet.isPinPermitted({ account })).allowed, false);
});

test('a truthy-but-not-true allowed value refuses', async () => {
  const fleet = coordinator(async () => ({
    response: { ok: true, status: 200 }, result: { allowed: 'yes' },
  }));
  assert.equal((await fleet.isPinPermitted({ account })).allowed, false);
});

test('no account at all is permitted', async () => {
  // Nothing to protect; refusing here would break unrelated paths.
  const fleet = coordinator(async () => { throw new Error('never called'); });
  assert.equal((await fleet.isPinPermitted({ account: null })).allowed, true);
});
