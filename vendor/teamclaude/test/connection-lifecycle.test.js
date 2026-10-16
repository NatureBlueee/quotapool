import http from 'node:http';
import { once } from 'node:events';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { AccountManager } from '../src/account-manager.js';
import {
  createProxyRequestListener,
  requestPressureSnapshot,
  sweepClosedRequests,
  waitForResponseOrTimeout,
} from '../src/server.js';

async function until(predicate, timeoutMs = 1500) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  assert.fail('condition did not become true');
}

test('a held request is cancelled immediately when the client closes', async () => {
  const clientClosedBefore = requestPressureSnapshot().lifecycle.clientClosed;
  const accountManager = new AccountManager([]);
  const listener = createProxyRequestListener({
    accountManager,
    upstream: 'https://api.anthropic.com',
    holdMs: 60_000,
    config: { connectionManagement: { maxActiveRequests: 8, maxPerSession: 1 } },
  });
  const server = http.createServer(listener);
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const port = server.address().port;

  const first = http.request({
    host: '127.0.0.1', port, path: '/v1/messages', method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-claude-code-session-id': 'connection-lifecycle-test',
    },
  });
  first.on('error', () => {});
  first.end(JSON.stringify({ model: 'claude-opus-5', messages: [] }));
  await until(() => requestPressureSnapshot().active === 1);
  assert.equal(sweepClosedRequests(), 0,
    'the self-healer must never reap a live streaming request');
  assert.equal(requestPressureSnapshot().active, 1);

  const second = await fetch(`http://127.0.0.1:${port}/v1/messages`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-claude-code-session-id': 'connection-lifecycle-test',
    },
    body: JSON.stringify({ model: 'claude-opus-5', messages: [] }),
  });
  assert.equal(second.status, 503);
  assert.equal(second.headers.get('x-quotapool-overload'), 'request-limit');

  const closedAt = Date.now();
  first.destroy();
  await until(() => requestPressureSnapshot().active === 0);
  assert.ok(Date.now() - closedAt < 1000,
    'cleanup must not wait for the 60-second capacity poll');
  const after = requestPressureSnapshot();
  assert.ok(after.lifecycle.clientClosed >= clientClosedBefore + 1,
    'the dashboard must show that the abandoned connection was reclaimed');
  assert.ok(after.lifecycle.lastSweepAt,
    'the lifecycle snapshot must expose the self-healing sweep');

  server.close();
  await once(server, 'close');
});

test('wait helper distinguishes elapsed delay from a closed response', async () => {
  const listeners = new Map();
  const res = {
    destroyed: false,
    writableEnded: false,
    once(name, fn) { listeners.set(name, fn); },
    off(name) { listeners.delete(name); },
  };
  const waiting = waitForResponseOrTimeout(res, 10_000);
  listeners.get('close')();
  assert.equal(await waiting, false);
});
