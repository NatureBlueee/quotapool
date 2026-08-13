// `computeRetryAfter` decides the number in the terminal 429 and the cap on
// each hold-loop sleep.
//
// It previously read only `rateLimitedUntil || quota.resetsAt`. `resetsAt` is a
// standard-quota (API-key) field and is null on every OAuth account, so for a
// subscription-only fleet the function ALWAYS returned its 60-second default —
// while the real wait was the next weekly reset, often more than a day away.
// The operator was told "retry in 60s" by a fleet that would not serve for 31
// hours.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { computeRetryAfter } from '../src/server.js';

const MIN = 60_000;
const HOUR = 3600_000;

const acct = (quota = {}, extra = {}) => ({ quota, ...extra });

test('no timing information at all falls back to 60s', () => {
  assert.equal(computeRetryAfter([acct()]), 60);
});

test('an empty fleet falls back to 60s', () => {
  assert.equal(computeRetryAfter([]), 60);
});

test('the weekly reset is used — the case that always returned 60 before', () => {
  const seconds = computeRetryAfter([
    acct({ unified7d: 1, unified7dReset: Date.now() + 31 * HOUR }),
  ]);
  assert.ok(seconds > 30 * 3600, `expected ~31h, got ${seconds}s`);
  assert.ok(seconds <= 31 * 3600 + 5);
});

test('the 5h reset is used', () => {
  const seconds = computeRetryAfter([
    acct({ unified5h: 1, unified5hReset: Date.now() + 2 * HOUR }),
  ]);
  assert.ok(seconds > 7000 && seconds <= 7205, `got ${seconds}s`);
});

test('family buckets count too', () => {
  const fable = computeRetryAfter([
    acct({ unified7dFable: 1, unified7dFableReset: Date.now() + 3 * HOUR }),
  ]);
  assert.ok(fable > 10000 && fable <= 10805, `got ${fable}s`);

  const sonnet = computeRetryAfter([
    acct({ unified7dSonnet: 1, unified7dSonnetReset: Date.now() + 3 * HOUR }),
  ]);
  assert.ok(sonnet > 10000 && sonnet <= 10805, `got ${sonnet}s`);
});

test('the soonest recovery across the whole fleet wins', () => {
  const seconds = computeRetryAfter([
    acct({ unified7d: 1, unified7dReset: Date.now() + 31 * HOUR }),
    acct({ unified5h: 1, unified5hReset: Date.now() + 5 * MIN }),
    acct({ unified7d: 1, unified7dReset: Date.now() + 12 * HOUR }),
  ]);
  assert.ok(seconds > 250 && seconds <= 305, `expected ~5min, got ${seconds}s`);
});

test('a rate limit is considered alongside quota windows', () => {
  const seconds = computeRetryAfter([
    acct({ unified7d: 1, unified7dReset: Date.now() + 31 * HOUR },
      { rateLimitedUntil: Date.now() + 30_000 }),
  ]);
  assert.ok(seconds <= 35, `got ${seconds}s`);
});

test('resets already in the past are ignored', () => {
  // A stale reset would otherwise produce a negative or zero wait and turn the
  // hold loop into a spin.
  const seconds = computeRetryAfter([
    acct({ unified7d: 1, unified7dReset: Date.now() - HOUR }),
  ]);
  assert.equal(seconds, 60);
});

test('a disabled account does not set the fleet-wide expectation', () => {
  // primary-current is disabled and auth-broken; its windows say nothing about
  // when the fleet can serve.
  const seconds = computeRetryAfter([
    acct({ unified5h: 1, unified5hReset: Date.now() + MIN }, { disabled: true }),
    acct({ unified7d: 1, unified7dReset: Date.now() + 4 * HOUR }),
  ]);
  assert.ok(seconds > 3 * 3600, `disabled account leaked into the estimate: ${seconds}s`);
});

test('the result is always at least one second', () => {
  const seconds = computeRetryAfter([
    acct({ unified5h: 1, unified5hReset: Date.now() + 10 }),
  ]);
  assert.ok(seconds >= 1);
});

test('garbage timestamps do not produce NaN', () => {
  const seconds = computeRetryAfter([
    acct({ unified7d: 1, unified7dReset: 'not a date' }),
  ]);
  assert.equal(Number.isFinite(seconds), true);
  assert.equal(seconds, 60);
});
