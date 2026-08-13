// Quota-bucket expiry, and the "value without reset" leak.
//
// `_clearExpiredQuotas` clears a bucket only when its paired `*Reset` is
// truthy. `applyUsageData` sets value and reset independently and
// `normalizeUsageBucket` can legitimately return `{utilization: 0.99,
// resetAt: null}`, so a bucket can hold a value that nothing will ever clear.
// Live proof today: hey@/hello@ carry `unified7dFable: 0` with a null reset —
// harmless only because the value is 0. At >= switchThreshold the same shape
// pins the account out until the process restarts.
//
// These tests pin CURRENT behaviour, including the defect, so Phase 1.3 has to
// change them deliberately rather than by accident.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { AccountManager } from '../src/account-manager.js';

const HOUR = 3600_000;

function manager(quota) {
  const m = new AccountManager(
    [{ name: 'a', type: 'oauth', credential: 'x', refreshToken: 'y' }],
    0.98,
  );
  Object.assign(m.accounts[0].quota, quota);
  return m;
}

test('an expired bucket with a reset timestamp is cleared', () => {
  const m = manager({ unified7d: 0.99, unified7dReset: Date.now() - HOUR });
  const { changed } = m._clearExpiredQuotas(m.accounts[0]);
  assert.equal(changed, true);
  assert.equal(m.accounts[0].quota.unified7d, null);
});

test('a future reset is left alone', () => {
  const m = manager({ unified7d: 0.99, unified7dReset: Date.now() + HOUR });
  const { changed } = m._clearExpiredQuotas(m.accounts[0]);
  assert.equal(changed, false);
  assert.equal(m.accounts[0].quota.unified7d, 0.99);
});

// --- value without reset ----------------------------------------------------
// All four buckets shared this defect: every expiry rule requires the paired
// `*Reset` to be truthy, so a value with a null reset was unclearable and could
// pin an account out for the life of the process. Each bucket is asserted
// separately — fixing only the family buckets would have been a half fix.

const BUCKETS = [
  ['unified5h', 'unified5hReset'],
  ['unified7d', 'unified7dReset'],
  ['unified7dSonnet', 'unified7dSonnetReset'],
  ['unified7dFable', 'unified7dFableReset'],
];

for (const [value, reset] of BUCKETS) {
  test(`${value} with a null reset is kept while it is still fresh`, () => {
    // A freshly-reported undated value is a real measurement. Dropping it
    // immediately would make the fleet route into an account it was just told
    // is spent.
    const m = manager({ [value]: 0.99, [reset]: null });
    const { changed } = m._clearExpiredQuotas(m.accounts[0]);
    assert.equal(changed, false);
    assert.equal(m.accounts[0].quota[value], 0.99);
  });

  test(`${value} with a null reset is dropped to unknown once stale`, () => {
    const m = manager({ [value]: 0.99, [reset]: null });
    // First pass records when the undated value was first seen.
    m._clearExpiredQuotas(m.accounts[0]);
    // Age it past the trust window.
    m.accounts[0].quota._undatedSince[value] = Date.now() - 9 * 24 * 3600_000;
    const { changed } = m._clearExpiredQuotas(m.accounts[0]);
    assert.equal(changed, true);
    assert.equal(
      m.accounts[0].quota[value], null,
      `${value} must fall back to unknown so the account can be re-probed`,
    );
  });
}

test('a stale undated bucket no longer pins the account out forever', () => {
  // The point of the fix: this is not a display bug, it removed the account
  // from selection permanently with no log line and no way back.
  const m = manager({ unified7d: 0.99, unified7dReset: null });
  assert.equal(m.isAccountAvailable(m.accounts[0]), false);

  m._clearExpiredQuotas(m.accounts[0]);
  m.accounts[0].quota._undatedSince.unified7d = Date.now() - 9 * 24 * 3600_000;
  m._clearExpiredQuotas(m.accounts[0]);

  assert.equal(
    m.isAccountAvailable(m.accounts[0]), true,
    'the account must become selectable again once the undated value is dropped',
  );
});

test('a dated bucket is never touched by the undated path', () => {
  const future = Date.now() + HOUR;
  const m = manager({ unified7d: 0.99, unified7dReset: future });
  m._clearExpiredQuotas(m.accounts[0]);
  m._clearExpiredQuotas(m.accounts[0]);
  assert.equal(m.accounts[0].quota.unified7d, 0.99);
  assert.equal(m.accounts[0].quota.unified7dReset, future);
});

test('a reset arriving later cancels the undated countdown', () => {
  const m = manager({ unified7d: 0.99, unified7dReset: null });
  m._clearExpiredQuotas(m.accounts[0]);
  assert.ok(m.accounts[0].quota._undatedSince.unified7d);

  // The next usage probe supplies the missing reset.
  m.accounts[0].quota.unified7dReset = Date.now() + HOUR;
  m._clearExpiredQuotas(m.accounts[0]);
  assert.equal(m.accounts[0].quota.unified7d, 0.99, 'still a valid measurement');
});

test('a healthy account is available', () => {
  const m = manager({ unified7d: 0.2, unified7dReset: Date.now() + HOUR });
  assert.equal(m.isAccountAvailable(m.accounts[0]), true);
});

test('the switch threshold is a floor, not an equality', () => {
  const under = manager({ unified7d: 0.97, unified7dReset: Date.now() + HOUR });
  assert.equal(under.isAccountAvailable(under.accounts[0]), true);
  const at = manager({ unified7d: 0.98, unified7dReset: Date.now() + HOUR });
  assert.equal(at.isAccountAvailable(at.accounts[0]), false);
});

test('a RETIRED account is never token-refreshed', async () => {
  // The original reasoning, kept: it cannot serve a request, and the attempt
  // never stops — the refresh fails, expiresAt stays in the past, and the next
  // check finds it expiring again. `primary-current` produced 2,314 attempts in
  // one log.
  let refreshes = 0;
  const manager = new AccountManager(
    [
      { name: 'retired', type: 'oauth', credential: 'x', refreshToken: 'r',
        expiresAt: 1, disabled: true, retired: true },
      { name: 'live', type: 'oauth', credential: 'y', refreshToken: 'r2',
        expiresAt: 1 },
    ],
    0.98,
    {
      refreshFn: async () => {
        refreshes += 1;
        return { accessToken: 'a', refreshToken: 'b', expiresAt: Date.now() + 3600_000 };
      },
    },
  );

  await manager.ensureTokenFresh(0);
  assert.equal(refreshes, 0, 'a retired account must not be refreshed');

  await manager.ensureTokenFresh(1);
  assert.equal(refreshes, 1, 'an enabled account still refreshes');
});

test('a PAUSED account keeps its token fresh', async () => {
  // The contract this replaces said `disabled` meant both things, and that froze
  // accounts that were only switched off for a while. Measured on this machine:
  // four accounts paused, and 100 hours later their tokens were still the ones
  // from the moment they were paused and their quota readings were 64 hours
  // stale. Turning one back on meant turning on something nobody could say
  // anything about.
  let refreshes = 0;
  const manager = new AccountManager(
    [{ name: 'paused', type: 'oauth', credential: 'x', refreshToken: 'r',
       expiresAt: 1, disabled: true }],
    0.98,
    {
      refreshFn: async () => {
        refreshes += 1;
        return { accessToken: 'a', refreshToken: 'b', expiresAt: Date.now() + 3600_000 };
      },
    },
  );

  await manager.ensureTokenFresh(0);
  assert.equal(refreshes, 1, 'a paused account must stay usable on return');
});

test('a paused account whose refresh is rejected stops on its own', async () => {
  // What makes the split safe to default the permissive way. Nobody has to
  // declare an account retired for the loop to end: a genuine auth rejection
  // lands it in `status === 'error'`, and that is the retired case arriving at
  // itself.
  let refreshes = 0;
  const manager = new AccountManager(
    [{ name: 'paused', type: 'oauth', credential: 'x', refreshToken: 'r',
       expiresAt: 1, disabled: true }],
    0.98,
    {
      refreshFn: async () => {
        refreshes += 1;
        const err = new Error('Token refresh failed (400): invalid_grant');
        err.status = 400;
        throw err;
      },
    },
  );

  await manager.ensureTokenFresh(0);
  assert.equal(manager.accounts[0].status, 'error');

  await manager.ensureTokenFresh(0);
  await manager.ensureTokenFresh(0);
  assert.equal(refreshes, 1, 'it must not keep retrying a rejected credential');
});
