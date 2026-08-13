// Waiting must be distinguishable from stuck.
//
// The hold itself is right and stays: a held request resumes by itself when
// quota returns, so nobody has to come back and re-send. What was wrong is that
// the wait was invisible — 9,948 "holding connection" lines in the server log
// and nothing in the session, so "waiting for quota" and "the system is stuck"
// looked the same. The last incident ran for hours on that ambiguity while an
// entire account's quota sat unused.
//
// The one distinction that has to survive into the readout is `everExcluded`:
// a request that knocked out its own healthy capacity is a FAULT, while a fleet
// with genuinely no quota is not. Reporting them as one number would make
// "no quota left" page someone at 3am, and hide real routing bugs in the noise.

import { test } from 'node:test';
import assert from 'node:assert/strict';

// The registry is module-private; the snapshot is the contract, so drive it
// through the same import the status endpoint uses.
const { waitingSnapshot } = await import('../src/server.js');

test('an idle fleet reports nothing waiting', () => {
  const snapshot = waitingSnapshot();
  assert.equal(snapshot.count, 0);
  assert.equal(snapshot.avoidable, 0);
  assert.equal(snapshot.soonestRecoverySeconds, null,
    'no waiters means no recovery estimate, not a fabricated one');
  assert.deepEqual(snapshot.sessions, []);
});

test('the snapshot shape is stable enough to render', () => {
  const snapshot = waitingSnapshot();
  for (const key of ['count', 'avoidable', 'longestWaitSeconds',
                     'soonestRecoverySeconds', 'sessions']) {
    assert.ok(key in snapshot, `status consumers depend on \`${key}\``);
  }
  assert.ok(Array.isArray(snapshot.sessions));
});

test('client housekeeping paths need a credential but not a fence', () => {
  // They go to Anthropic, so they need an account. They write no transcript, so
  // they need no writer. Sending them through the coordinator produced 288
  // refusals a day — `holder is required` — and made "something bypasses the
  // launcher" the standing explanation for months. Nothing was bypassing it:
  // Claude Code polls these itself, with no session and no model.
  const FENCELESS = ['/api/claude_code/'];
  const fenceless = (url) => FENCELESS.some(p => url.startsWith(p));

  assert.ok(fenceless('/api/claude_code/policy_limits'));
  assert.ok(fenceless('/api/claude_code/settings'));
  assert.ok(!fenceless('/v1/messages'),
    'model requests must keep the fence — that is the whole point of it');
  assert.ok(!fenceless('/v1/messages/count_tokens'));
});
