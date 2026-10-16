import test from 'node:test';
import assert from 'node:assert/strict';
import { UsageAnalytics } from '../src/usage-analytics.js';

test('records durable request and token totals by day, account and model', () => {
  const analytics = new UsageAnalytics();
  analytics.recordRequest({
    at: new Date(2026, 7, 26, 9).getTime(),
    account: 'max-1', model: 'claude-fable-5',
    inputTokens: 120, outputTokens: 80, status: 200,
  });
  analytics.recordRequest({
    at: new Date(2026, 7, 26, 10).getTime(),
    account: 'max-2', model: 'claude-opus-5',
    inputTokens: 20, outputTokens: 10, status: 502, interrupted: true,
  });

  const snapshot = analytics.snapshot();
  assert.deepEqual(snapshot.totals, {
    requests: 2, inputTokens: 140, outputTokens: 90,
    successful: 1, interrupted: 1,
  });
  assert.equal(snapshot.days.at(-1).requests, 2);
  assert.equal(snapshot.accounts.find(item => item.name === 'max-1').outputTokens, 80);
  assert.equal(snapshot.models.find(item => item.name === 'claude-opus-5').interrupted, 1);
});

test('survives an export and restore without double counting', () => {
  const first = new UsageAnalytics();
  first.recordRequest({ account: 'max-1', model: 'fable', status: 200 });
  const restored = new UsageAnalytics(first.exportState());
  restored.recordRequest({ account: 'max-2', model: 'opus', status: 200 });
  assert.equal(restored.snapshot().totals.requests, 2);
});

test('can seed legacy in-memory account counters exactly once', () => {
  const analytics = new UsageAnalytics();
  assert.equal(analytics.seedAccountTotals([{
    name: 'max-1',
    usage: { totalRequests: 7, totalInputTokens: 110, totalOutputTokens: 230 },
  }], { at: new Date(2026, 7, 26, 12).getTime() }), true);
  assert.equal(analytics.seedAccountTotals([{
    name: 'max-1',
    usage: { totalRequests: 99, totalInputTokens: 99, totalOutputTokens: 99 },
  }]), false);
  const snapshot = analytics.snapshot();
  assert.deepEqual(snapshot.totals, {
    requests: 7, inputTokens: 110, outputTokens: 230,
    successful: 0, interrupted: 0,
  });
  assert.equal(snapshot.accounts[0].name, 'max-1');
  assert.equal(snapshot.models[0].name, '启用前汇总（模型未知）');
});

test('records quota left behind when a window advances', () => {
  const analytics = new UsageAnalytics();
  const reset = Date.now() + 60_000;
  analytics.observeQuotas([{
    id: 'id-1', name: 'max-1', quota: {
      unified7d: 0.72, unified7dReset: reset,
    },
  }], reset - 1000);
  analytics.observeQuotas([{
    id: 'id-1', name: 'max-1', quota: {
      unified7d: 0.01, unified7dReset: reset + 7 * 86400_000,
    },
  }], reset + 1000);

  const waste = analytics.snapshot().waste;
  assert.equal(waste.windows, 1);
  assert.ok(Math.abs(waste.totalRemainingRatio - 0.28) < 1e-9);
  assert.equal(waste.recent[0].account, 'max-1');
  assert.equal(waste.recent[0].bucket, 'weekly');
});

test('records an expired window even while the fresh reading is unknown', () => {
  const analytics = new UsageAnalytics();
  const reset = Date.now() - 1000;
  analytics.observeQuotas([{
    id: 'id-1', name: 'max-1', quota: {
      unified5h: 0.9, unified5hReset: reset,
    },
  }], reset - 1000);
  analytics.observeQuotas([{
    id: 'id-1', name: 'max-1', quota: {
      unified5h: null, unified5hReset: null,
    },
  }], reset + 1000);
  assert.ok(Math.abs(
    analytics.snapshot().waste.recent[0].remainingRatio - 0.1,
  ) < 1e-9);
});

test('recommends the required 7D pace as a multiple of yesterday usage', () => {
  const analytics = new UsageAnalytics();
  const yesterdayMorning = new Date(2026, 7, 25, 9).getTime();
  const yesterdayEvening = new Date(2026, 7, 25, 20).getTime();
  const now = new Date(2026, 7, 26, 9).getTime();
  const resetAt = now + 2 * 24 * 60 * 60 * 1000;
  analytics.observeQuotas([{
    name: 'max-1', quota: { unified7d: 0.2, unified7dReset: resetAt },
  }], yesterdayMorning);
  analytics.observeQuotas([{
    name: 'max-1', quota: { unified7d: 0.4, unified7dReset: resetAt },
  }], yesterdayEvening);
  const weekly = analytics.snapshot({ now }).quotaEfficiency.weekly;
  assert.equal(weekly.accounts, 1);
  assert.ok(Math.abs(weekly.remainingRatio - 0.6) < 1e-9);
  assert.ok(Math.abs(weekly.requiredDailyRatio - 0.3) < 1e-9);
  assert.ok(Math.abs(weekly.yesterdayConsumedRatio - 0.2) < 1e-9);
  assert.ok(Math.abs(weekly.multiplier - 1.5) < 1e-9);
  assert.ok(Math.abs(weekly.byAccount[0].multiplier - 1.5) < 1e-9);
});

test('keeps a separate 5H pace and does not invent a yesterday baseline', () => {
  const analytics = new UsageAnalytics();
  const now = new Date(2026, 7, 26, 9).getTime();
  analytics.observeQuotas([{
    name: 'max-1', quota: {
      unified5h: 0.25,
      unified5hReset: now + 3 * 60 * 60 * 1000,
    },
  }], now);
  const fiveHour = analytics.snapshot({ now }).quotaEfficiency.fiveHour;
  assert.equal(fiveHour.accounts, 1);
  assert.ok(Math.abs(fiveHour.remainingRatio - 0.75) < 1e-9);
  assert.equal(fiveHour.yesterdayConsumedRatio, 0);
  assert.equal(fiveHour.multiplier, null);
});
