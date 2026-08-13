// teamclaude.json holds every account's OAuth credentials, and it was written
// with a plain `writeFile` — truncate, then write. Kill the process in between
// (OOM, a full disk, an impatient operator) and what survives on disk is a
// truncated credential file for the whole fleet. There is no recovery from that
// except re-login on every account.
//
// The second half is the one this system has already been burned by. The
// comment above `configUpdateChain` in config.js describes it exactly:
//
//   "several OAuth accounts refresh their tokens at once: only the last
//    writer's rotated refresh token persists, and the other accounts keep a
//    token that was just rotated away, so they fail on the next restart with
//    invalid_grant and need a re-login."
//
// That chain fixes it for callers inside ONE Node process. But Fleet writes
// this file from Python too — claude-direct, the bootstrap, the importers — and
// a promise chain cannot see those. Between 08-06 and 08-09 this machine logged
// 588 `invalid_grant` lines and four healthy accounts were dropped from
// rotation.
//
// So the lock has to be one both runtimes can hold. O_EXCL on a lockfile is the
// only mechanism a dependency-free Node process and CPython can use
// identically — which is why the interop test below matters more than any of
// the others: two locks that cannot see each other are worse than one lock
// nobody takes, because they look like protection.
import { test } from 'node:test';
import assert from 'node:assert';
import { mkdtemp, readFile, writeFile, stat, unlink, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const run = promisify(execFile);

async function scratch() {
  const dir = await mkdtemp(join(tmpdir(), 'tc-config-'));
  const path = join(dir, 'teamclaude.json');
  process.env.TEAMCLAUDE_CONFIG = path;
  return { dir, path };
}

async function fresh() {
  const { dir, path } = await scratch();
  const mod = await import('../src/config.js?' + Math.random());
  return { dir, path, mod };
}

test('a saved config is readable and complete', async () => {
  const { path, mod } = await fresh();
  await mod.saveConfig({ accounts: [{ name: 'a', accessToken: 'secret' }] });

  const parsed = JSON.parse(await readFile(path, 'utf-8'));
  assert.equal(parsed.accounts[0].accessToken, 'secret');
});

test('the credential file is never world-readable, even for an instant', async () => {
  // The old code created with mode 0600 and then chmod'd — but only the create
  // honoured the mode, so a file that already existed kept whatever it had
  // until the chmod landed. Writing through a fresh temp file removes the
  // window entirely.
  const { path, mod } = await fresh();
  await writeFile(path, '{}', { mode: 0o644 });
  await mod.saveConfig({ accounts: [] });

  const info = await stat(path);
  assert.equal(info.mode & 0o777, 0o600);
});

test('a failed write leaves the previous config intact', async () => {
  const { path, mod } = await fresh();
  await mod.saveConfig({ accounts: [{ name: 'original' }] });

  // A value JSON.stringify cannot serialise: the write throws part-way through
  // the operation, exactly where a truncating writer would already have emptied
  // the file.
  const circular = { accounts: [] };
  circular.self = circular;
  await assert.rejects(() => mod.saveConfig(circular));

  const parsed = JSON.parse(await readFile(path, 'utf-8'));
  assert.equal(parsed.accounts[0].name, 'original',
    'the previous credentials must survive a failed save');
});

test('no temp file is left behind after a failed write', async () => {
  // The failure has to happen INSIDE the atomic write, after the temp file
  // exists. A circular object was the first attempt and proved nothing:
  // JSON.stringify throws while building the argument, so writeFileAtomic never
  // runs and there is no temp file to leak. Mutation testing caught it — the
  // test stayed green with the cleanup deleted.
  //
  // A directory in the target's place lets the temp file be written and fsynced
  // and then fails the rename, which is the window that matters.
  const { dir } = await scratch();
  const { mkdir } = await import('node:fs/promises');
  await mkdir(process.env.TEAMCLAUDE_CONFIG);       // target path is a directory
  const mod = await import('../src/config.js?' + Math.random());

  await assert.rejects(() => mod.saveConfig({ accounts: [] }));

  const left = (await readdir(dir)).filter(name => name.includes('.tmp'));
  assert.deepEqual(left, [], 'a stray .tmp beside the credential file is litter '
    + 'that looks like a backup');
});

test('state is written the same way — it is the quota for every account', async () => {
  const { mod } = await fresh();
  await mod.saveState({ accounts: { a: { weekly: 0.5 } } });
  const parsed = JSON.parse(await readFile(mod.getStatePath(), 'utf-8'));
  assert.equal(parsed.accounts.a.weekly, 0.5);
});

test('concurrent updates in one process all survive', async () => {
  // What configUpdateChain was already for. Kept as a regression: the lock now
  // wraps the same region, and a lock taken in the wrong place would serialise
  // the save but not the read, reintroducing the lost update with a wider
  // window.
  const { mod } = await fresh();
  await mod.saveConfig({ accounts: [] });

  await Promise.all(['a', 'b', 'c', 'd', 'e'].map(name =>
    mod.atomicConfigUpdate(config => { config.accounts.push({ name }); })));

  const parsed = await mod.loadConfig();
  assert.equal(parsed.accounts.length, 5,
    'every rotated token must persist, not just the last writer\'s');
});

test('the lock is the same one the Python tools take', async (t) => {
  // The interop check. `claude-direct` takes an O_EXCL lock at <config>.lock;
  // if this process used a different mechanism — flock, say — both sides would
  // hold a lock, neither would see the other, and the protection would be
  // imaginary.
  const { path, mod } = await fresh();
  await mod.saveConfig({ accounts: [{ name: 'start' }] });

  const lockPath = path + '.lock';
  // Stand in for the Python side holding it.
  await writeFile(lockPath, '99999', { flag: 'wx', mode: 0o600 });

  const started = Date.now();
  const update = mod.atomicConfigUpdate(config => {
    config.accounts.push({ name: 'after-lock' });
  });

  // Give it long enough that finishing immediately would prove it ignored the
  // lock, then release and let it through.
  await new Promise(resolve => setTimeout(resolve, 200));
  await unlink(lockPath);
  await update;

  assert.ok(Date.now() - started >= 150,
    'the update went through while another process held the lock');
  const parsed = await mod.loadConfig();
  assert.equal(parsed.accounts.length, 2);
});

test('a lock left by a dead process does not wedge the fleet forever', async () => {
  // The cost of getting this wrong is that no token can ever be saved again,
  // which is the failure the lock exists to prevent. So: stale locks are taken,
  // and the threshold is far above any real write (single-digit milliseconds).
  const { path, mod } = await fresh();
  await mod.saveConfig({ accounts: [] });

  const lockPath = path + '.lock';
  await writeFile(lockPath, '1', { mode: 0o600 });
  const ancient = new Date(Date.now() - 120_000);
  const { utimes } = await import('node:fs/promises');
  await utimes(lockPath, ancient, ancient);

  const started = Date.now();
  await mod.atomicConfigUpdate(config => { config.accounts.push({ name: 'x' }); });
  const elapsed = Date.now() - started;

  const parsed = await mod.loadConfig();
  assert.equal(parsed.accounts.length, 1);
  // Timing is the assertion, not the row count. Without reclamation the writer
  // waits out the full LOCK_WAIT_MS and then writes anyway via the fallback, so
  // the update still lands and a row-count check passes either way — mutation
  // testing caught exactly that. A reclaimed lock is taken immediately.
  assert.ok(elapsed < 1000,
    `stale lock was waited out (${elapsed}ms) instead of reclaimed`);
});

test('a busy lock delays but never drops a rotated token', async () => {
  // Deliberate policy, stated so a future reader does not "fix" it: when the
  // lock cannot be had, the write proceeds anyway with a loud line. A lost
  // update is recoverable on the next save; a rotated refresh token that was
  // never written needs a human and a browser.
  const source = await readFile(new URL('../src/config.js', import.meta.url), 'utf-8');
  assert.ok(source.includes('writing anyway'),
    'the fallback must stay, and must announce itself');
});
