// Fleet must keep executor and advisor quota constraints separate.
//
// Claude Code advertises an advisor model on the outer request even when the
// executor is Opus. If every Fable bucket is exhausted, that must not park the
// Opus request: the executor can still run and only a later advisor invocation
// may fail. A top-level Fable request remains governed by Fable quota.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { FleetCoordinator } from '../src/fleet-coordinator.js';

function account(name, index, quota = {}) {
  return {
    name,
    index,
    type: 'oauth',
    accountUuid: `uuid-${name}`,
    quota,
    status: 'active',
  };
}

function request() {
  return {
    headers: { 'x-fleet-launch-id': 'launch-model-separation' },
    url: '/v1/messages',
  };
}

function context(model, advisorModel = null) {
  return {
    tried: new Set(),
    model,
    advisorModel,
    sessionId: `session-${model}`,
  };
}

function harness(accounts, isAccountAvailable, selectedId) {
  const bodies = [];
  const coordinator = new FleetCoordinator({ baseUrl: 'http://127.0.0.1:1' });
  coordinator._post = async (_path, body) => {
    bodies.push(body);
    return {
      response: { ok: true, status: 200 },
      result: { account_id: selectedId },
    };
  };
  return {
    coordinator,
    bodies,
    accountManager: { accounts, isAccountAvailable },
  };
}

test('Opus keeps running when all nested Fable advisor buckets are exhausted', async () => {
  const accounts = [account('max-2', 0), account('max-3', 1)];
  const isAccountAvailable = (_account, model, advisorModel) => {
    assert.match(model, /opus/);
    return advisorModel == null;
  };
  const { coordinator, bodies, accountManager } = harness(
    accounts,
    isAccountAvailable,
    'uuid-max-2',
  );

  const selected = await coordinator._select({
    accountManager,
    ctx: context('claude-opus-5', 'claude-fable-5-1'),
    req: request(),
  });

  assert.equal(selected.name, 'max-2');
  assert.equal(bodies[0].model, 'claude-opus-5');
  assert.equal(bodies[0].advisor_model, null,
    'the unavailable advisor is a preference, not an executor capacity gate');
  assert.deepEqual(bodies[0].accounts.map(item => item.eligible), [true, true]);
});

test('Fleet preserves the advisor constraint while a dual-eligible account exists', async () => {
  const accounts = [account('max-2', 0), account('max-3', 1)];
  const isAccountAvailable = (candidate, _model, advisorModel) =>
    advisorModel == null || candidate.name === 'max-3';
  const { coordinator, bodies, accountManager } = harness(
    accounts,
    isAccountAvailable,
    'uuid-max-3',
  );

  const selected = await coordinator._select({
    accountManager,
    ctx: context('claude-opus-5', 'claude-fable-5-1'),
    req: request(),
  });

  assert.equal(selected.name, 'max-3');
  assert.equal(bodies[0].advisor_model, 'claude-fable-5-1');
  assert.deepEqual(bodies[0].accounts.map(item => item.eligible), [false, true]);
});

test('an excluded dual-eligible account cannot keep Opus tied to Fable', async () => {
  const accounts = [account('max-2', 0), account('max-3', 1)];
  const isAccountAvailable = (candidate, _model, advisorModel) =>
    advisorModel == null || candidate.name === 'max-3';
  const { coordinator, bodies, accountManager } = harness(
    accounts,
    isAccountAvailable,
    'uuid-max-2',
  );
  const ctx = context('claude-opus-5', 'claude-fable-5-1');
  ctx.tried.add(1);

  const selected = await coordinator._select({
    accountManager,
    ctx,
    req: request(),
  });

  assert.equal(selected.name, 'max-2');
  assert.equal(bodies[0].advisor_model, null);
  assert.deepEqual(bodies[0].accounts.map(item => item.eligible), [true, false]);
  assert.deepEqual(bodies[0].exclude_account_ids, ['uuid-max-3']);
});

test('a top-level Fable request still uses only Fable-eligible accounts', async () => {
  const accounts = [
    account('max-2', 0, {
      unified7d: 0.40,
      unified7dFable: 0.99,
      unified7dFableReset: '2026-09-05T00:00:00Z',
    }),
    account('max-3', 1, {
      unified7d: 0.70,
      unified7dFable: 0.20,
      unified7dFableReset: '2026-09-06T00:00:00Z',
    }),
  ];
  const isAccountAvailable = candidate => candidate.quota.unified7dFable < 0.98;
  const { coordinator, bodies, accountManager } = harness(
    accounts,
    isAccountAvailable,
    'uuid-max-3',
  );

  const selected = await coordinator._select({
    accountManager,
    ctx: context('claude-fable-5-1'),
    req: request(),
  });

  assert.equal(selected.name, 'max-3');
  assert.equal(bodies[0].model, 'claude-fable-5-1');
  assert.equal(bodies[0].advisor_model, null);
  assert.deepEqual(bodies[0].accounts.map(item => item.eligible), [false, true]);
  assert.equal(
    bodies[0].accounts[1].quota.unified7dFableReset,
    '2026-09-06T00:00:00Z',
    'the coordinator receives the Fable reset window used for model-specific ranking',
  );
});
