import { readFile, mkdir, chmod, open, rename, unlink, stat } from 'node:fs/promises';
import { join, dirname, basename } from 'node:path';
import { homedir } from 'node:os';
import { randomBytes } from 'node:crypto';

// How long to wait for another process to finish writing before giving up and
// writing anyway. Giving up is deliberate: the alternative is refusing to
// persist a freshly rotated refresh token, and a token that was rotated but not
// saved is precisely the `invalid_grant` this file's own comments describe. A
// lost update is recoverable; a lost credential needs a human and a browser.
const LOCK_WAIT_MS = 3000;
const LOCK_POLL_MS = 25;
// A lock older than this is assumed to belong to a process that died holding
// it. Generous: every writer here finishes in single-digit milliseconds.
const LOCK_STALE_MS = 30_000;

/**
 * Write a file so that a reader never sees a partial one.
 *
 * `writeFile` truncates and then writes. Kill the process in between — OOM, a
 * panicked operator, a full disk — and what is left on disk is a truncated
 * teamclaude.json, which is every account's OAuth credentials at once. There is
 * no recovery from that except re-login on every account.
 *
 * Same directory for the temp file so the rename stays on one filesystem and
 * therefore stays atomic. fsync before the rename or the rename can land while
 * the contents are still in the page cache; fsync the directory after, or the
 * rename itself may not survive a power loss.
 */
async function writeFileAtomic(path, contents, mode = 0o600) {
  const directory = dirname(path);
  const temp = join(directory, `.${basename(path)}.${process.pid}.tmp`);
  let handle;
  try {
    handle = await open(temp, 'w', mode);
    await handle.writeFile(contents, 'utf-8');
    await handle.sync();
    await handle.close();
    handle = null;
    await chmod(temp, mode).catch(() => {});
    await rename(temp, path);
  } catch (err) {
    if (handle) await handle.close().catch(() => {});
    await unlink(temp).catch(() => {});
    throw err;
  }
  let dirHandle;
  try {
    dirHandle = await open(directory, 'r');
    await dirHandle.sync();
  } catch {
    // Not every platform allows fsync on a directory handle. The rename is
    // still atomic; only its durability across a power cut is weakened.
  } finally {
    if (dirHandle) await dirHandle.close().catch(() => {});
  }
}

/**
 * A lock both this process and the Fleet Python tools can hold.
 *
 * `configUpdateChain` below serialises callers inside ONE Node process, and its
 * comment explains exactly why that matters: concurrent token refreshes
 * otherwise drop every rotated refresh token but the last, and those accounts
 * fail with `invalid_grant` on the next start. But Fleet also writes this file
 * from Python — `claude-direct`, the bootstrap, the account importers — and a
 * promise chain cannot see those at all.
 *
 * O_EXCL on a lock file rather than flock, because it is the only mechanism
 * both a dependency-free Node process and CPython can use identically.
 */
async function withConfigLock(path, fn) {
  const lockPath = path + '.lock';
  const deadline = Date.now() + LOCK_WAIT_MS;
  let held = false;

  while (Date.now() < deadline) {
    try {
      const handle = await open(lockPath, 'wx', 0o600);
      await handle.writeFile(String(process.pid), 'utf-8');
      await handle.close();
      held = true;
      break;
    } catch (err) {
      if (err.code !== 'EEXIST') throw err;
      try {
        const info = await stat(lockPath);
        if (Date.now() - info.mtimeMs > LOCK_STALE_MS) {
          await unlink(lockPath).catch(() => {});
          continue;
        }
      } catch {
        continue;   // vanished between EEXIST and stat; try to take it
      }
      await new Promise(resolve => setTimeout(resolve, LOCK_POLL_MS));
    }
  }

  if (!held) {
    console.error(
      `[TeamClaude] Config lock busy for ${LOCK_WAIT_MS}ms; writing anyway. ` +
      'A lost update is recoverable, an unsaved rotated token is not.');
  }
  try {
    return await fn();
  } finally {
    if (held) await unlink(lockPath).catch(() => {});
  }
}

export function getConfigPath() {
  if (process.env.TEAMCLAUDE_CONFIG) return process.env.TEAMCLAUDE_CONFIG;
  const configDir = process.env.XDG_CONFIG_HOME || join(homedir(), '.config');
  return join(configDir, 'teamclaude.json');
}

/**
 * Path to the runtime state file (a sibling of the config). This holds volatile
 * data learned at runtime — e.g. quota utilization observed passively from
 * traffic — kept out of the hand-editable config so config stays clean and
 * isn't rewritten on every state save.
 */
export function getStatePath() {
  const cfg = getConfigPath();
  return cfg.endsWith('.json') ? cfg.replace(/\.json$/, '.state.json') : cfg + '.state';
}

export async function loadState() {
  try {
    return JSON.parse(await readFile(getStatePath(), 'utf-8'));
  } catch (err) {
    if (err.code === 'ENOENT') return null;
    throw err;
  }
}

export async function saveState(state) {
  const path = getStatePath();
  await mkdir(dirname(path), { recursive: true });
  // Atomic: this file carries observed quota for every account, and a truncated
  // one makes the whole fleet look unmeasured until the next real response.
  await writeFileAtomic(path, JSON.stringify(state, null, 2) + '\n');
}

// ── the Fleet safety contract, in one place ─────────────────────────────────
//
// Fleet mode is subscription-only by design, and the owner's constraint is
// blunt: usage credits are out of the question because anything outside a
// subscription is extremely expensive. So an API key, a per-account upstream,
// or a global upstream pointing anywhere but Anthropic is not a preference —
// it is either money leaving through a door nobody opened, or a credential
// being handed to a host of somebody else's choosing.
//
// The rules used to live inline in `serverCommand()`, which meant they ran
// exactly once, at startup. Everything that can add an account later — the
// TUI's R key, `POST /teamclaude/reload`, the CLI's notify-after-change —
// went around them. A live process could be given what it would have refused
// to start with. Rules that hold at startup and not afterwards are a habit,
// not a control, so they are one function now and both paths call it.

export const FLEET_UPSTREAM = 'https://api.anthropic.com';

/** Is this the one upstream Fleet is allowed to send a credential to? */
export function isApprovedUpstream(value) {
  if (value === undefined || value === null || value === '') return true;
  let url;
  try {
    url = new URL(String(value));
  } catch {
    return false;
  }
  // Scheme, host AND port, all three. `https://api.anthropic.com:8443` resolves
  // somewhere else entirely, and `http://` would put the token on the wire in
  // clear text.
  return url.protocol === 'https:'
    && url.hostname === 'api.anthropic.com'
    && (url.port === '' || url.port === '443')
    && (url.pathname === '' || url.pathname === '/');
}

/**
 * Everything about `config` that Fleet mode forbids, as sentences.
 *
 * Returns `{ config, violations }` where `config` is safe to use: forbidden
 * accounts are dropped and a rejected upstream falls back to the approved one.
 * Dropping rather than refusing the whole load is deliberate — one bad account
 * must not block a reload that is also carrying a refreshed token for a good
 * one, and a reload that silently does nothing is the failure mode this system
 * keeps rediscovering.
 */
export function applyFleetPolicy(config) {
  const violations = [];
  const safe = { ...config };

  if (!isApprovedUpstream(config.upstream)) {
    violations.push(
      `upstream ${JSON.stringify(config.upstream)} is not ${FLEET_UPSTREAM}; `
      + 'every request carries a real OAuth token and Fleet will not send one '
      + 'anywhere else');
    safe.upstream = FLEET_UPSTREAM;
  }

  if (config.sx?.apiKey || (config.sx?.mode && config.sx.mode !== 'off')) {
    violations.push('sx.org routing is forbidden in Fleet mode');
    safe.sx = { mode: 'off' };
  }

  const accounts = config.accounts || [];
  const allowed = accounts.filter(account => {
    const named = account?.name || account?.accountUuid || '(unnamed)';
    if (account?.type !== 'oauth') {
      violations.push(`account ${named}: type ${JSON.stringify(account?.type)} `
                      + '— Fleet accepts Claude subscription OAuth only');
      return false;
    }
    if (account.apiKey) {
      violations.push(`account ${named}: carries an apiKey, which would spend `
                      + 'outside the subscription');
      return false;
    }
    if (account.upstream) {
      violations.push(`account ${named}: has its own upstream `
                      + `${JSON.stringify(account.upstream)}`);
      return false;
    }
    return true;
  });
  if (allowed.length !== accounts.length) safe.accounts = allowed;

  return { config: safe, violations };
}

export function createDefaultConfig() {
  return {
    proxy: {
      port: 3456,
      apiKey: 'tc-' + randomBytes(24).toString('base64url'),
    },
    upstream: 'https://api.anthropic.com',
    switchThreshold: 0.98,
    holdSeconds: 0,
    connectionManagement: {
      maxConnections: 512,
      maxActiveRequests: 256,
      maxPerSession: 64,
    },
    distributeSessions: false,
    eventLogging: 'hide',
    blockedModels: [],
    accounts: [],
  };
}

export async function loadConfig() {
  const path = getConfigPath();
  try {
    return JSON.parse(await readFile(path, 'utf-8'));
  } catch (err) {
    if (err.code === 'ENOENT') return null;
    throw err;
  }
}

export async function loadOrCreateConfig() {
  let config = await loadConfig();
  if (!config) {
    config = createDefaultConfig();
    await saveConfig(config);
    console.log(`Created config at ${getConfigPath()}`);
  }
  return config;
}

export async function saveConfig(config) {
  const path = getConfigPath();
  await mkdir(dirname(path), { recursive: true });
  // Atomic, and 0600 enforced by the temp file's own mode rather than a chmod
  // after the fact — there is no window where this exists world-readable.
  await writeFileAtomic(path, JSON.stringify(config, null, 2) + '\n');
}

// Serialize config updates. atomicConfigUpdate is a read-modify-write, so two
// concurrent callers can both read the same config and then save in turn, and
// the later save silently drops the earlier caller's change. This bites hardest
// on startup, when several OAuth accounts refresh their tokens at once: only the
// last writer's rotated refresh token persists, and the other accounts keep a
// token that was just rotated away, so they fail on the next restart with
// invalid_grant and need a re-login. Chaining the updates keeps every write.
let configUpdateChain = Promise.resolve();

/**
 * Atomically update the config: re-reads from disk, calls updater(config),
 * then saves. Returns the updated config. This prevents overwriting changes
 * made by other processes (e.g. `teamclaude import` while the server runs), and
 * serializes concurrent callers so simultaneous updates queue instead of
 * clobbering one another.
 */
export function atomicConfigUpdate(updater) {
  const run = async () => withConfigLock(getConfigPath(), async () => {
    // Re-read INSIDE the lock. Reading outside it reintroduces the lost update
    // this is here to prevent — just with a wider window.
    const config = await loadConfig() || createDefaultConfig();
    await updater(config);
    await saveConfig(config);
    return config;
  });
  const result = configUpdateChain.then(run, run);
  configUpdateChain = result.then(() => {}, () => {});
  return result;
}
