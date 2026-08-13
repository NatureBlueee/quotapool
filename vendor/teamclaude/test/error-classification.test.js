// Requirement (a)'s real defence: a broken egress path must not be read as a
// broken account.
//
// The measured outage this pins: `sx.mode: "off"` on disk, sx.org still armed
// in the running process, every request failing with
// `sx.org proxy refused CONNECT: HTTP/1.1 503 Service Unavailable`. That is a
// bare `new Error(...)` — no `code`, no matching `name` — so it classified as
// an account fault. Two accounts holding 83% and 70% of their weekly quota were
// added to ctx.tried and the request parked for six hours. 2,259 parks in one
// six-hour window while those accounts moved 0.08 -> 0.17 and 0.21 -> 0.30.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classifyUpstreamError } from '../src/server.js';

function err(message, props = {}) {
  return Object.assign(new Error(message), props);
}

// --- the regression that motivated this -------------------------------------

test('sx.org CONNECT refusal is transport, not an account fault', () => {
  assert.equal(
    classifyUpstreamError(
      err('sx.org proxy refused CONNECT: HTTP/1.1 503 Service Unavailable'),
    ),
    'transport',
  );
});

test('proxy CONNECT timeout is transport', () => {
  assert.equal(
    classifyUpstreamError(err('sx.org proxy CONNECT timed out after 10000ms')),
    'transport',
  );
});

test('socket hang up is transport', () => {
  assert.equal(classifyUpstreamError(err('socket hang up')), 'transport');
});

test('tunnel failure is transport', () => {
  assert.equal(
    classifyUpstreamError(err('tunnel api.anthropic.com:443 failed: ')),
    'transport',
  );
});

// --- previously-recognised transport faults must not regress ----------------

for (const code of [
  'ECONNRESET', 'ECONNREFUSED', 'ETIMEDOUT',
  'UND_ERR_CONNECT_TIMEOUT', 'UND_ERR_HEADERS_TIMEOUT', 'UND_ERR_BODY_TIMEOUT',
  'TEAMCLAUDE_HEADERS_TIMEOUT', 'TEAMCLAUDE_BODY_TIMEOUT',
  'TEAMCLAUDE_ADMISSION_TIMEOUT',
]) {
  test(`${code} is transport`, () => {
    assert.equal(classifyUpstreamError(err('boom', { code })), 'transport');
  });
}

for (const name of ['TimeoutError', 'AbortError']) {
  test(`${name} is transport`, () => {
    assert.equal(classifyUpstreamError(err('boom', { name })), 'transport');
  });
}

test('fetch failed is transport', () => {
  assert.equal(classifyUpstreamError(err('fetch failed')), 'transport');
});

// --- DNS / routing faults ---------------------------------------------------

for (const code of ['EAI_AGAIN', 'ENOTFOUND', 'EHOSTUNREACH', 'ENETUNREACH', 'EPIPE']) {
  test(`${code} is transport`, () => {
    assert.equal(classifyUpstreamError(err('boom', { code })), 'transport');
  });
}

// --- the other direction: do not over-classify -------------------------------
// Failing over is the correct response to an account-specific fault. Marking
// everything transport would make the fleet retry a genuinely broken account
// forever instead of moving on.

test('an unrecognised error stays an account fault', () => {
  assert.equal(classifyUpstreamError(err('something nobody has seen')), 'account');
});

test('a non-Error throw is an account fault', () => {
  assert.equal(classifyUpstreamError('a bare string'), 'account');
  assert.equal(classifyUpstreamError(null), 'account');
  assert.equal(classifyUpstreamError(undefined), 'account');
});

test('an error with no message does not throw the classifier', () => {
  assert.equal(classifyUpstreamError(new Error()), 'account');
});
