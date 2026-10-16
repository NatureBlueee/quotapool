#!/usr/bin/env node
import { loadState, saveState } from '../src/config.js';
import { UsageAnalytics } from '../src/usage-analytics.js';

const positional = process.argv.slice(2).filter(value => !value.startsWith('--'));
const endpoint = positional[0] || 'http://127.0.0.1:13457/teamclaude/status';
const stopArg = process.argv.find(value => value.startsWith('--stop-pid='));
const stopPid = stopArg ? Number(stopArg.slice('--stop-pid='.length)) : 0;
const response = await fetch(endpoint, { signal: AbortSignal.timeout(10_000) });
if (!response.ok) throw new Error(`status endpoint returned ${response.status}`);
const status = await response.json();
if (stopPid) {
  if (Number(status.requestHealth?.active || 0) > 0) {
    throw new Error('refusing to stop the data plane while requests are active');
  }
  process.kill(stopPid, 'SIGKILL');
  await new Promise(resolve => setTimeout(resolve, 300));
}
const previous = await loadState() || {};
const analytics = new UsageAnalytics(previous.analytics);
const seeded = analytics.seedAccountTotals(status.accounts || []);
if (seeded) {
  analytics.observeQuotas(status.accounts || []);
  await saveState({ ...previous, analytics: analytics.exportState() });
}
const snapshot = analytics.snapshot();
process.stdout.write(JSON.stringify({
  seeded,
  requests: snapshot.totals.requests,
  inputTokens: snapshot.totals.inputTokens,
  outputTokens: snapshot.totals.outputTokens,
}, null, 2) + '\n');
