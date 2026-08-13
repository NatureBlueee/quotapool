// Loopback was being used as an authentication boundary, and it is not one.
//
// Binding to 127.0.0.1 keeps the network out. It does not distinguish Claude
// Code from a browser extension, a build script, or a malicious transitive npm
// dependency — all run as the same user and can all open a socket to
// 127.0.0.1:13456. Any of them could CONNECT api.anthropic.com and have a real
// OAuth token injected into their request, spending a subscription with no
// record that it was not Fleet.
//
// Production makes it worse than the code suggests: teamclaude.json carries
// `proxy: {port, host}` with no apiKey, so `if (!proxyApiKey) return true` was
// the first line and nothing below it ever ran.
//
// The fix is not "add a key" — there is already an identity on the wire.
// launcher.py gives every Claude it starts a CONNECT userinfo of
// `fleet-holder-v1.<base64>`, and a TC_ACCT pin carries an account id. The only
// shape that says nothing at all is the one that was being let through.
//
// Two modes, because enforcing blind is how this system's worst day started. In
// `audit` nothing changes and the gap is counted; the flip to `enforce` is made
// from that number after a window long enough to cover every launch path.
import { test } from 'node:test';
import assert from 'node:assert';
import { connectVerdict, connectAuthorized, authAudit, authMode,
         FLEET_TRANSPORT_USER_PREFIX } from '../src/mitm.js';
import { readFileSync } from 'node:fs';

const LOOPBACK = { remoteAddress: '127.0.0.1' };
const REMOTE = { remoteAddress: '203.0.113.9' };

function basic(user, pass = 'x') {
  return { headers: { 'proxy-authorization':
    'Basic ' + Buffer.from(`${user}:${pass}`).toString('base64') } };
}

const fleetHolder = FLEET_TRANSPORT_USER_PREFIX
  + Buffer.from('host:launch-1').toString('base64url');

function withMode(mode, fn) {
  const previous = process.env.FLEET_AUTH_MODE;
  if (mode) process.env.FLEET_AUTH_MODE = mode;
  else delete process.env.FLEET_AUTH_MODE;
  try { return fn(); } finally {
    if (previous === undefined) delete process.env.FLEET_AUTH_MODE;
    else process.env.FLEET_AUTH_MODE = previous;
  }
}


test('audit is the default — an unset variable must not enforce', () => {
  withMode(null, () => assert.equal(authMode(), 'audit'));
});

test('a Fleet-launched Claude is recognised by the identity it already sends', () => {
  const verdict = connectVerdict(basic(fleetHolder, 'local'), LOOPBACK, null);
  assert.equal(verdict.allowed, true);
  assert.equal(verdict.identity, 'fleet');
  assert.equal(verdict.wouldRefuseUnderEnforce, false);
});

test('an explicit account pin is an identity too', () => {
  // TC_ACCT is a documented upstream feature; enforcing must not break it.
  const verdict = connectVerdict(basic('085a6c9c-7105-48aa-a3a9-fe0353648636'),
                                 LOOPBACK, null);
  assert.equal(verdict.allowed, true);
  assert.equal(verdict.identity, 'pin');
});

test('an anonymous loopback CONNECT is the gap, and audit lets it through', () => {
  withMode('audit', () => {
    const verdict = connectVerdict({ headers: {} }, LOOPBACK, null);
    assert.equal(verdict.allowed, true, 'audit must change nothing for anyone');
    assert.equal(verdict.wouldRefuseUnderEnforce, true);
  });
});

test('enforce refuses it', () => {
  withMode('enforce', () => {
    const verdict = connectVerdict({ headers: {} }, LOOPBACK, null);
    assert.equal(verdict.allowed, false);
  });
});

test('enforce still admits every legitimate identity', () => {
  // The whole risk of flipping. If this ever fails, enforcing takes the fleet
  // down rather than closing a hole.
  withMode('enforce', () => {
    assert.equal(connectVerdict(basic(fleetHolder, 'local'), LOOPBACK, null).allowed,
                 true, 'a Fleet-launched Claude must survive enforcement');
    assert.equal(connectVerdict(basic('some-account-uuid'), LOOPBACK, null).allowed,
                 true, 'an account pin must survive enforcement');
    assert.equal(connectVerdict({ headers: {} }, LOOPBACK, 'key').allowed,
                 false, 'no key, no identity: still refused');
    assert.equal(connectVerdict(basic('anything', 'key'), LOOPBACK, 'key').allowed,
                 true, 'the proxy key is still a valid credential');
  });
});

test('a remote client without the key is refused in either mode', () => {
  for (const mode of ['audit', 'enforce']) {
    withMode(mode, () => {
      const verdict = connectVerdict(basic('whatever'), REMOTE, 'key');
      assert.equal(verdict.allowed, false, mode);
      assert.equal(verdict.wouldRefuseUnderEnforce, false,
        'a remote refusal is not the thing being measured');
    });
  }
});

test('the audit counter is what the flip decision gets made from', () => {
  const before = authAudit.anonymousLoopbackConnects;
  withMode('audit', () => {
    connectAuthorized({ headers: {}, url: 'api.anthropic.com:443' }, LOOPBACK, null);
    connectAuthorized({ headers: {}, url: 'api.anthropic.com:443' }, LOOPBACK, null);
  });
  assert.equal(authAudit.anonymousLoopbackConnects, before + 2);
  assert.ok(authAudit.samples.length > 0, 'a bare count cannot identify a caller');
  assert.ok(authAudit.samples[0].target, 'the sample has to say where it was going');
});

test('a legitimate connection never touches the counter', () => {
  // A counter that ticks for normal traffic can never reach zero, so the flip
  // could never be justified and the audit would be permanent.
  const before = authAudit.anonymousLoopbackConnects;
  connectAuthorized(basic(fleetHolder, 'local'), LOOPBACK, null);
  connectAuthorized(basic('some-account-uuid'), LOOPBACK, null);
  assert.equal(authAudit.anonymousLoopbackConnects, before);
});


test('an anonymous connection records who opened it, not just that it happened', async () => {
  // A count is not something you can act on. Two anonymous connections an hour
  // appeared on the server, and ruling out suspects one at a time — the auth
  // probe, a nine-day-old session, every scheduled job — took an evening and
  // still did not name the caller. On loopback the peer port answers it.
  //
  // Verified end to end against a real data plane in the shadow instance:
  //   来源: curl (pid 68484) → api.anthropic.com:443
  const before = authAudit.samples.length;
  connectAuthorized({ headers: {}, url: 'api.anthropic.com:443' },
                    { remoteAddress: '127.0.0.1', remotePort: 65000 }, null);
  assert.ok(authAudit.samples.length > before);

  const sample = authAudit.samples[authAudit.samples.length - 1];
  assert.ok('peer' in sample,
    'the sample has to have somewhere to put the answer');
  assert.equal(sample.target, 'api.anthropic.com:443');
});

test('looking up the peer can never break a connection', async () => {
  // It shells out, on a path that only runs while something is already wrong.
  // An exception or a hang there would turn a diagnostic into an outage.
  const source = readFileSync(new URL('../src/mitm.js', import.meta.url), 'utf-8');
  const fn = source.slice(source.indexOf('function identifyPeer'));
  assert.ok(fn.includes('timeout'), 'the lookup must be bounded');
  assert.ok(fn.includes('if (err || !stdout) return'),
    'a failed lookup is a missing name, not an error');
});
