// Blind CONNECT tunnels must use the configured egress, not a direct socket.
//
// The defect: `net.connect(port, host)` is raw TCP and ignores HTTPS_PROXY. The
// service IS started with HTTPS_PROXY=<outbound_proxy>, but that only reaches
// Node's fetch — so every host other than api.anthropic.com left the data plane
// as a DIRECT connection.
//
// It surfaced as `claude setup-token` hanging: the browser step succeeded (that
// uses the system proxy), then the code exchange to platform.claude.com came
// through this path. Measured from the affected Mac:
//
//   direct                   HTTP 000, timed out at 25s
//   via the outbound proxy   HTTP 400 in 1.1s
//
// The owner's rule after this: anything Anthropic-related goes through the
// proxy, not only model requests.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import net from 'node:net';
import { outboundProxyEndpoint, isNoProxyHost } from '../src/mitm.js';

function withEnv(vars, body) {
  const saved = {};
  for (const [k, v] of Object.entries(vars)) {
    saved[k] = process.env[k];
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
  try { return body(); } finally {
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
  }
}

const NO_PROXY_ENV = {
  HTTPS_PROXY: undefined, https_proxy: undefined,
  HTTP_PROXY: undefined, http_proxy: undefined,
};

test('no proxy configured means direct, exactly as before', () => {
  withEnv(NO_PROXY_ENV, () => {
    assert.equal(outboundProxyEndpoint(), null);
  });
});

test('HTTPS_PROXY is parsed into host and port', () => {
  withEnv({ ...NO_PROXY_ENV, HTTPS_PROXY: 'http://127.0.0.1:1082' }, () => {
    const proxy = outboundProxyEndpoint();
    assert.equal(proxy.host, '127.0.0.1');
    assert.equal(proxy.port, 1082);
    assert.equal(proxy.auth, null);
  });
});

test('a proxy with no port defaults by scheme', () => {
  withEnv({ ...NO_PROXY_ENV, HTTPS_PROXY: 'http://proxy.internal' }, () => {
    assert.equal(outboundProxyEndpoint().port, 80);
  });
  withEnv({ ...NO_PROXY_ENV, HTTPS_PROXY: 'https://proxy.internal' }, () => {
    assert.equal(outboundProxyEndpoint().port, 443);
  });
});

test('credentials in the proxy URL are decoded for Basic auth', () => {
  withEnv({ ...NO_PROXY_ENV, HTTPS_PROXY: 'http://user%40x:p%40ss@127.0.0.1:1082' }, () => {
    assert.equal(outboundProxyEndpoint().auth, 'user@x:p@ss');
  });
});

test('lowercase and HTTP_PROXY are honoured, HTTPS first', () => {
  withEnv({ ...NO_PROXY_ENV, https_proxy: 'http://127.0.0.1:1' }, () => {
    assert.equal(outboundProxyEndpoint().port, 1);
  });
  withEnv({ ...NO_PROXY_ENV, HTTP_PROXY: 'http://127.0.0.1:2' }, () => {
    assert.equal(outboundProxyEndpoint().port, 2);
  });
  withEnv({ ...NO_PROXY_ENV, HTTPS_PROXY: 'http://127.0.0.1:3', HTTP_PROXY: 'http://127.0.0.1:4' }, () => {
    assert.equal(outboundProxyEndpoint().port, 3, 'HTTPS_PROXY wins');
  });
});

test('a malformed proxy URL is treated as unset rather than throwing', () => {
  withEnv({ ...NO_PROXY_ENV, HTTPS_PROXY: 'not a url' }, () => {
    assert.equal(outboundProxyEndpoint(), null);
  });
});

// --- NO_PROXY ---------------------------------------------------------------
// Loopback must stay direct: the MITM answers www.example.org itself and the
// coordinator is on 127.0.0.1. Sending those through an egress proxy would
// break the local test path.

test('loopback is excluded when NO_PROXY says so', () => {
  withEnv({ NO_PROXY: 'localhost,127.0.0.1,::1' }, () => {
    assert.equal(isNoProxyHost('127.0.0.1'), true);
    assert.equal(isNoProxyHost('localhost'), true);
    assert.equal(isNoProxyHost('api.anthropic.com'), false);
  });
});

test('a leading-dot rule matches subdomains', () => {
  withEnv({ NO_PROXY: '.internal' }, () => {
    assert.equal(isNoProxyHost('host.internal'), true);
    assert.equal(isNoProxyHost('internal'), false);
  });
});

test('a bare domain rule matches its subdomains too', () => {
  withEnv({ NO_PROXY: 'example.com' }, () => {
    assert.equal(isNoProxyHost('example.com'), true);
    assert.equal(isNoProxyHost('api.example.com'), true);
    assert.equal(isNoProxyHost('notexample.com'), false);
  });
});

test('a CIDR entry does not match by name', () => {
  // The real NO_PROXY carries 10.0.0.0/8 etc. Those are ranges, and matching
  // them textually against a hostname would be wrong.
  withEnv({ NO_PROXY: '10.0.0.0/8,172.16.0.0/12' }, () => {
    assert.equal(isNoProxyHost('10.0.0.5'), false);
    assert.equal(isNoProxyHost('platform.claude.com'), false);
  });
});

test('an empty NO_PROXY excludes nothing', () => {
  withEnv({ NO_PROXY: '', no_proxy: '' }, () => {
    assert.equal(isNoProxyHost('platform.claude.com'), false);
  });
});

test('the real runtime config sends platform.claude.com through the proxy', () => {
  // The exact combination this machine runs with. platform.claude.com must NOT
  // be excluded, or setup-token hangs again.
  withEnv({
    HTTPS_PROXY: 'http://127.0.0.1:1082',
    NO_PROXY: 'localhost,127.0.0.1,::1,.local,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,198.18.0.0/15',
  }, () => {
    assert.ok(outboundProxyEndpoint(), 'proxy must be detected');
    assert.equal(isNoProxyHost('platform.claude.com'), false, 'must go through the proxy');
    assert.equal(isNoProxyHost('claude.ai'), false);
    assert.equal(isNoProxyHost('api.anthropic.com'), false);
    assert.equal(isNoProxyHost('127.0.0.1'), true, 'loopback stays direct');
  });
});
