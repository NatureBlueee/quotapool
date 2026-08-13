// Fleet mode is subscription-only, and the owner's constraint is blunt: usage
// credits are out of the question because anything outside a subscription is
// extremely expensive. So an API key, a per-account upstream, or a global
// upstream pointing anywhere but Anthropic is not a preference — it is either
// money leaving through a door nobody opened, or a real OAuth token being
// handed to a host of somebody else's choosing.
//
// The rules existed. They lived inline in `serverCommand()`, which meant they
// ran exactly once, at startup, and everything that can add an account later —
// the TUI's R key, `POST /teamclaude/reload`, the CLI's notify-after-change —
// went around them. A live process could be given what it would have refused to
// start with.
//
// Found by `codex-security scan` (scan d25efebf, findings 4 and 5) against the
// threat model's "spending outside a subscription is a finding" clause. Rules
// that hold at startup and not afterwards are a habit, not a control.

import { test } from 'node:test';
import assert from 'node:assert';
import { readFileSync } from 'node:fs';
import { applyFleetPolicy, isApprovedUpstream, FLEET_UPSTREAM }
  from '../src/config.js';

const OK = { name: 'good', type: 'oauth', accountUuid: 'u-1' };


// ── the one upstream a credential may be sent to ────────────────────────────

test('the approved upstream, and nothing that merely looks like it', () => {
  for (const value of [undefined, null, '', FLEET_UPSTREAM,
                       'https://api.anthropic.com/',
                       'https://api.anthropic.com:443']) {
    assert.equal(isApprovedUpstream(value), true, String(value));
  }

  for (const value of [
    'http://api.anthropic.com',            // the token in clear text
    'https://api.anthropic.com:8443',      // a different service entirely
    'https://api.anthropic.com.evil.io',   // suffix, not host
    'https://evil.io/api.anthropic.com',   // path, not host
    'https://api.anthropic.com.',          // trailing dot resolves the same
    'not a url',
  ]) {
    assert.equal(isApprovedUpstream(value), false, String(value));
  }
});

test('a rejected upstream falls back rather than being left as it was', () => {
  // Reporting the violation and then using the value anyway would be worse than
  // not checking: it reads as enforcement in the log and is not.
  const { config, violations } = applyFleetPolicy(
    { upstream: 'https://evil.example.com', accounts: [OK] });
  assert.equal(config.upstream, FLEET_UPSTREAM);
  assert.equal(violations.length, 1);
  assert.match(violations[0], /evil\.example\.com/);
});


// ── what may hold a credential ──────────────────────────────────────────────

test('an API-key account cannot be admitted', () => {
  const { config, violations } = applyFleetPolicy(
    { accounts: [OK, { name: 'billed', type: 'apikey', apiKey: 'sk-x' }] });
  assert.deepEqual(config.accounts.map(a => a.name), ['good']);
  assert.match(violations.join(' '), /billed/);
});

test('an OAuth account carrying an apiKey cannot either', () => {
  // The shape the type check alone would let through.
  const { config, violations } = applyFleetPolicy(
    { accounts: [OK, { name: 'sneaky', type: 'oauth', apiKey: 'sk-x' }] });
  assert.deepEqual(config.accounts.map(a => a.name), ['good']);
  assert.match(violations.join(' '), /订阅|subscription/);
});

test('a per-account upstream cannot either', () => {
  const { config } = applyFleetPolicy(
    { accounts: [OK, { name: 'elsewhere', type: 'oauth',
                       upstream: 'https://somewhere.else' }] });
  assert.deepEqual(config.accounts.map(a => a.name), ['good']);
});

test('sx.org routing stays forbidden', () => {
  const { config, violations } = applyFleetPolicy(
    { accounts: [OK], sx: { apiKey: 'k', mode: 'always' } });
  assert.deepEqual(config.sx, { mode: 'off' });
  assert.match(violations.join(' '), /sx\.org/);
});

test('one bad account does not take the good ones with it', () => {
  // A reload usually carries a refreshed token for an account that is fine.
  // Refusing the whole load to punish a different entry turns a config mistake
  // into an outage, which is the failure this system keeps rediscovering.
  const { config, violations } = applyFleetPolicy({
    accounts: [OK, { name: 'bad', type: 'apikey', apiKey: 'x' },
               { name: 'also-good', type: 'oauth', accountUuid: 'u-2' }],
  });
  assert.deepEqual(config.accounts.map(a => a.name), ['good', 'also-good']);
  assert.equal(violations.length, 1);
});

test('a clean config produces nothing to report and is left alone', () => {
  // Without this the suite cannot tell a working policy from one that rejects
  // everything, and production runs on a config that has to pass it.
  const input = { upstream: FLEET_UPSTREAM, accounts: [OK], sx: { mode: 'off' } };
  const { config, violations } = applyFleetPolicy(input);
  assert.deepEqual(violations, []);
  assert.deepEqual(config.accounts, [OK]);
  assert.equal(config.upstream, FLEET_UPSTREAM);
});


// ── and it has to be called on the path that was open ───────────────────────

function functionBody(source, name) {
  const start = source.indexOf(name);
  assert.ok(start > 0, `${name} not found`);
  return source.slice(start, source.indexOf('\n  };', start));
}

test('the reload path checks before it adds', () => {
  // A source-shape assertion, and weaker than driving the real reload — that
  // needs a running server, a TUI and a live AccountManager. It is here because
  // the mutation that matters is not a wrong rule, it is a deleted call site:
  // startup already had a guard and the reload is where the hole was. This
  // catches deleting it, and catches checking the config after the accounts
  // have already been taken from it.
  const source = readFileSync(new URL('../src/index.js', import.meta.url), 'utf-8');
  const body = functionBody(source, 'const reloadAccounts = async () =>');

  const checked = body.indexOf('applyFleetPolicy');
  const used = body.indexOf('syncAccountsFromDisk');
  assert.ok(checked > 0, 'reload must apply the Fleet policy at all');
  assert.ok(used > 0, 'sanity: this is still the function that adds accounts');
  assert.ok(checked < used,
    '策略必须在账号被取走之前跑 —— 之后再查就只是记录, 不是拦截');
});

test('startup refuses rather than filters', () => {
  // The other branch, and deliberately different. Nothing is running yet, so
  // there is no service to preserve, and starting on a config the operator
  // never saw corrected is worse than not starting.
  const source = readFileSync(new URL('../src/index.js', import.meta.url), 'utf-8');
  const guard = source.slice(source.indexOf('const fleetEnabled'),
                             source.indexOf('// --log-to <dir>'));
  assert.match(guard, /applyFleetPolicy/);
  assert.match(guard, /process\.exit\(1\)/);
});
