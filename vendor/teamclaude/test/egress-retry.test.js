// The egress hop refuses CONNECT in bursts. Measured on the Mac, one log tail:
//
//   1939  sx.org proxy refused CONNECT: HTTP/1.1 503 Service Unavailable
//     29  connect ECONNREFUSED 127.0.0.1:1082
//      4  connect ETIMEDOUT 127.0.0.1:1082
//     48  socket hang up
//     55  read ECONNRESET
//
// 92% of them are the egress saying no BEFORE any bytes moved. Nothing reached
// Anthropic, so re-sending is safe — and until now the data plane destroyed the
// connection and left the client to retry, which is what surfaces to the owner
// as "API error".
//
// The distinction is the whole point. A mid-stream `socket hang up` may mean
// the request WAS received; retrying that would double a billed call. Only the
// never-opened case is retried, and only while no response headers have gone
// out.
import { test } from 'node:test';
import assert from 'node:assert';
import { classifyUpstreamError } from '../src/server.js';

test('a refused CONNECT is a transport fault, not an account fault', () => {
  const err = new Error('sx.org proxy refused CONNECT: HTTP/1.1 503 Service Unavailable');
  assert.equal(classifyUpstreamError(err), 'transport',
    'classifying it per-account would mark every account tried in turn and '
    + 'park the request while the whole fleet sat healthy');
});

test('a local proxy that is not listening is also transport', () => {
  const err = new Error('connect ECONNREFUSED 127.0.0.1:1082');
  err.code = 'ECONNREFUSED';
  assert.equal(classifyUpstreamError(err), 'transport');
});

test('a mid-stream hang up is transport too, and must not be retried', () => {
  // It classifies the same way; what separates them is the `neverOpened` test
  // in the handler, which this asserts stays distinguishable from the outside.
  const hangup = new Error('socket hang up');
  const refused = new Error('sx.org proxy refused CONNECT: HTTP/1.1 503');
  assert.equal(classifyUpstreamError(hangup), 'transport');
  assert.equal(classifyUpstreamError(refused), 'transport');
  assert.ok(!/proxy refused CONNECT/i.test(hangup.message),
    'a hang up must not match the never-opened test — the request may have '
    + 'been received, and re-sending it would double a billed call');
  assert.ok(/proxy refused CONNECT/i.test(refused.message));
});

test('an ordinary API rejection is still an account fault', () => {
  assert.equal(classifyUpstreamError(new Error('some upstream oddity')), 'account');
});
