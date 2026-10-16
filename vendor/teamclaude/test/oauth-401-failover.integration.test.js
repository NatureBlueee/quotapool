import http from 'node:http';
import { once } from 'node:events';
import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { AccountManager } from '../src/account-manager.js';
import { createProxyRequestListener } from '../src/server.js';

const servers = [];

afterEach(async () => {
  while (servers.length) {
    const server = servers.pop();
    server.closeAllConnections();
    server.close();
    await once(server, 'close');
  }
});

async function listen(listener) {
  const server = http.createServer(listener);
  servers.push(server);
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return `http://127.0.0.1:${server.address().port}`;
}

async function fixture(refreshFn) {
  const attempts = [];
  const upstream = await listen((req, res) => {
    attempts.push(req.headers.authorization);
    if (req.headers.authorization === 'Bearer secondary') {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ type: 'message', content: [{ type: 'text', text: 'ok' }] }));
    } else {
      res.writeHead(401, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ type: 'error', error: { type: 'authentication_error', message: 'rejected' } }));
    }
  });
  const manager = new AccountManager([
    { name: 'first', type: 'oauth', accessToken: 'old', refreshToken: 'refresh', priority: 0, upstream },
    { name: 'second', type: 'oauth', accessToken: 'secondary', priority: 1, upstream },
  ], 0.98, { refreshFn });
  const proxy = await listen(createProxyRequestListener({ accountManager: manager, upstream }));
  return { attempts, manager, proxy };
}

async function request(proxy) {
  const response = await fetch(`${proxy}/v1/messages`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ model: 'claude-opus-5-5', messages: [{ role: 'user', content: 'test' }] }),
  });
  return { status: response.status, body: await response.json() };
}

test('rejected refreshed token moves a live request to another eligible account', async () => {
  const { attempts, manager, proxy } = await fixture(async () => ({
    accessToken: 'new', refreshToken: 'next-refresh', expiresAt: Date.now() + 3600_000,
  }));
  const result = await request(proxy);
  assert.equal(result.status, 200);
  assert.deepEqual(attempts, ['Bearer old', 'Bearer new', 'Bearer secondary']);
  assert.equal(manager.accounts[0].status, 'error');
  assert.equal(manager.accounts[1].status, 'active');
});

test('transient refresh failure fails over this request without quarantining the account', async () => {
  const { attempts, manager, proxy } = await fixture(async () => {
    const error = new Error('temporary refresh network failure');
    error.status = 503;
    throw error;
  });
  const result = await request(proxy);
  assert.equal(result.status, 200);
  assert.deepEqual(attempts, ['Bearer old', 'Bearer old', 'Bearer secondary']);
  assert.equal(manager.accounts[0].status, 'active');
});
