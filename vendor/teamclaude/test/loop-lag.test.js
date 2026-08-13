// The status endpoint was measured at 11.3s, 12.5s and 20.1s on a process
// whose status handler does no I/O — it reads three in-memory objects and
// serialises them. Such a handler cannot be slow, only queued. Nothing
// recorded that, so the cause had to be guessed, and the first guess
// (scheduling priority) was wrong: the same process in the same band answered
// in under a millisecond after a restart.
//
// `/teamclaude/ready` answers from constants plus this. It is the number that
// distinguishes "the service is wedged" from "the report is expensive", which
// is the distinction the launch gate got wrong for months.
import { test } from 'node:test';
import assert from 'node:assert';
import { loopLagSnapshot } from '../src/server.js';

test('the ready snapshot reports loop lag without doing any work', () => {
  const snap = loopLagSnapshot();
  assert.equal(typeof snap.lastMs, 'number');
  assert.equal(typeof snap.maxMs, 'number');
  assert.ok(snap.lastMs >= 0, 'lag cannot be negative');
  assert.ok(snap.maxMs >= snap.lastMs,
    'max must bound the latest sample, or a spike disappears the moment the loop recovers');
});

test('a blocked loop is visible in the next sample', async () => {
  const before = loopLagSnapshot().maxMs;
  // Block the loop deliberately, longer than the sample interval.
  const until = Date.now() + 1400;
  while (Date.now() < until) { /* spin */ }
  await new Promise(resolve => setTimeout(resolve, 1200));
  const after = loopLagSnapshot().maxMs;
  assert.ok(after > before,
    `a 1.4s stall left maxMs unchanged (${before} -> ${after}); the metric '
    + 'cannot report the only condition it exists for`);
});
