import { EventEmitter } from 'node:events';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  bindRelayResponseLifecycle,
  clientCredentialProxyFor,
} from '../src/server.js';

function withEnv(vars, body) {
  const saved = {};
  for (const [key, value] of Object.entries(vars)) {
    saved[key] = process.env[key];
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  try { return body(); } finally {
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

const CLEAN_PROXY_ENV = process.platform === 'win32'
  ? {
      HTTPS_PROXY: undefined,
      ALL_PROXY: undefined,
      NO_PROXY: undefined,
    }
  : {
      HTTPS_PROXY: undefined,
      https_proxy: undefined,
      ALL_PROXY: undefined,
      all_proxy: undefined,
      NO_PROXY: undefined,
      no_proxy: undefined,
    };

test('Remote Control follows the Windows outbound proxy', () => {
  withEnv({ ...CLEAN_PROXY_ENV, HTTPS_PROXY: 'http://127.0.0.1:7897' }, () => {
    const route = clientCredentialProxyFor('https://api.anthropic.com/v1/code/events', null);
    assert.equal(route.source, 'environment');
    assert.equal(route.proxy.host, '127.0.0.1');
    assert.equal(route.proxy.port, 7897);
  });
});

test('Remote Control still respects NO_PROXY', () => {
  withEnv({
    ...CLEAN_PROXY_ENV,
    HTTPS_PROXY: 'http://127.0.0.1:7897',
    NO_PROXY: 'api.anthropic.com',
  }, () => {
    assert.equal(clientCredentialProxyFor('https://api.anthropic.com/v1/code/events', null), null);
  });
});

test('an enabled sx route takes precedence over the environment proxy', () => {
  const sxProxy = { host: 'sx.example', port: 1234, username: 'u', password: 'p' };
  const sx = {
    useByDefault: () => true,
    isProvisioned: () => true,
    getProxy: () => sxProxy,
    tlsOptions: { rejectUnauthorized: false },
  };
  withEnv({ ...CLEAN_PROXY_ENV, HTTPS_PROXY: 'http://127.0.0.1:7897' }, () => {
    const route = clientCredentialProxyFor('https://api.anthropic.com/v1/code/events', sx);
    assert.equal(route.source, 'sx');
    assert.equal(route.proxy, sxProxy);
    assert.deepEqual(route.tlsOptions, sx.tlsOptions);
  });
});

test('an aborted upstream response closes the Desktop relay', () => {
  const upstream = new EventEmitter();
  let destroyed = 0;
  const downstream = {
    destroyed: false,
    writableEnded: false,
    destroy() { destroyed += 1; this.destroyed = true; },
  };
  const messages = [];
  bindRelayResponseLifecycle(upstream, downstream, (message) => messages.push(message));
  upstream.emit('aborted');
  assert.equal(destroyed, 1);
  assert.match(messages[0], /Remote Control response interrupted/);
});

test('a normal upstream end is not later treated as an interruption', () => {
  const upstream = new EventEmitter();
  let destroyed = 0;
  const downstream = {
    destroyed: false,
    writableEnded: false,
    destroy() { destroyed += 1; this.destroyed = true; },
  };
  bindRelayResponseLifecycle(upstream, downstream, () => {});
  upstream.emit('end');
  upstream.emit('error', new Error('late socket error'));
  assert.equal(destroyed, 0);
});
