import { test } from 'node:test';
import assert from 'node:assert/strict';
import { environmentTunnelAgent } from '../src/upstream-fetch.js';

test('environment proxy tunnel agents are bounded and reused per target', () => {
  const proxy = { host: '127.0.0.1', port: 7897, username: '', password: '' };
  const first = environmentTunnelAgent('https://api.anthropic.com/v1/messages', proxy);
  const second = environmentTunnelAgent('https://api.anthropic.com/v1/models', proxy);

  assert.equal(first, second, 'same proxy and target should reuse its keep-alive pool');
  assert.equal(first.keepAlive, true);
  assert.ok(Number.isFinite(first.maxSockets));
  assert.ok(first.maxSockets > 0 && first.maxSockets <= 32);
  assert.ok(first.maxFreeSockets >= 0 && first.maxFreeSockets <= 8);
});

test('environment proxy pools do not cross targets or credentials', () => {
  const base = { host: '127.0.0.1', port: 7897, username: '', password: '' };
  const api = environmentTunnelAgent('https://api.anthropic.com/v1/messages', base);
  const platform = environmentTunnelAgent('https://platform.claude.com/api', base);
  const authenticated = environmentTunnelAgent(
    'https://api.anthropic.com/v1/messages',
    { ...base, username: 'u', password: 'p' },
  );

  assert.notEqual(api, platform);
  assert.notEqual(api, authenticated);
});
