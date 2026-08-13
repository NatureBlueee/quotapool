import http from 'node:http';
import https from 'node:https';
import { timingSafeEqual } from 'node:crypto';
import { createWriteStream } from 'node:fs';
import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { ensureCerts, createConnectHandler, authMode, authAudit } from './mitm.js';
import { patchAccountUuid } from './account-uuid-rewrite.js';
import { sanitizeToolPairs } from './tool-pair-sanitize.js';
import { parseRequestModel, parseAdvisorModel } from './account-manager.js';
import { TopLevelFieldFinder, modelGlobMatches } from './model.js';
import { BodyWriter } from './request-log.js';
import { upstreamFetch } from './upstream-fetch.js';
import { tunnelTls } from './sx.js';


// How many consecutive holds with an EMPTY exclude set before re-stating the
// expected recovery. At the 60s poll cadence this is roughly every 5 minutes:
// often enough to be useful in a tail -f, rare enough not to become the flood
// it exists to explain.
const HOLD_ESCALATION_ITERATIONS = 5;

// Who is currently waiting for capacity, keyed by request id.
//
// The hold itself is correct and deliberately unchanged: a held request resumes
// by itself when quota returns, which is the whole point — nobody has to come
// back and re-send. What was wrong is that the wait was INVISIBLE. 9,948
// "holding connection" lines existed only in the server log while the session
// showed nothing, so "waiting for quota" and "the system is stuck" looked
// identical. That ambiguity is why the last incident ran for hours: an entire
// account's worth of quota was available the whole time.
//
// In memory on purpose — these are live connections, and a process restart
// drops them anyway.
const waiting = new Map();

// Event loop lag. The one number that says whether this process is turning.
//
// `/teamclaude/status` was measured at 11.3s, 12.5s and 20.1s on a process
// whose status handler does no I/O at all — it reads three in-memory objects
// and serialises them. A handler like that cannot be slow; it can only be
// QUEUED. But nothing recorded that, so the cause had to be guessed at, and
// the first guess (scheduling priority) was wrong: the same process in the
// same band answered in under a millisecond after a restart.
//
// A one-second timer that reports how late it actually fired costs nothing and
// answers the question directly the next time.
const LOOP_SAMPLE_MS = 1000;
const loopLag = { max: 0, last: 0, since: Date.now() };
let loopExpected = Date.now() + LOOP_SAMPLE_MS;
const loopTimer = setInterval(() => {
  const now = Date.now();
  loopLag.last = Math.max(0, now - loopExpected);
  if (loopLag.last > loopLag.max) loopLag.max = loopLag.last;
  loopExpected = now + LOOP_SAMPLE_MS;
}, LOOP_SAMPLE_MS);
// Never hold the process open on account of a diagnostic.
loopTimer.unref?.();

export function loopLagSnapshot() {
  return {
    lastMs: loopLag.last,
    maxMs: loopLag.max,
    sinceMinutes: Math.round((Date.now() - loopLag.since) / 60000),
  };
}

export function waitingSnapshot(now = Date.now()) {
  const entries = [...waiting.values()];
  // `everExcluded` separates the two cases that must never be conflated:
  // a request that knocked out its own healthy capacity (avoidable, a fault)
  // from a fleet that is genuinely spent (not a fault, just no quota).
  const avoidable = entries.filter(entry => entry.everExcluded);
  const soonest = entries.length
    ? Math.min(...entries.map(entry => entry.retryAfterSeconds ?? Infinity))
    : null;
  return {
    count: entries.length,
    avoidable: avoidable.length,
    longestWaitSeconds: entries.length
      ? Math.round(Math.max(...entries.map(entry => now - entry.since)) / 1000)
      : 0,
    soonestRecoverySeconds: Number.isFinite(soonest) ? soonest : null,
    sessions: entries.slice(0, 12).map(entry => ({
      session: entry.sessionId,
      waitedSeconds: Math.round((now - entry.since) / 1000),
      retryAfterSeconds: entry.retryAfterSeconds,
      avoidable: entry.everExcluded,
    })),
  };
}

export const HOP_BY_HOP_HEADERS = new Set([
  'host', 'connection', 'keep-alive', 'transfer-encoding',
  'te', 'trailer', 'upgrade', 'proxy-authorization', 'proxy-authenticate',
]);
// Path prefix for the deprecated URL-based account pin (superseded by TC_ACCT).
const PIN_PREFIX = '/tc-acct/';
const INLINE_RETRY_AFTER_MAX_SECONDS = 15;
// How long the proxy will absorb a rate-limit 429's retry-after inline (waiting
// on the SAME account) before surfacing a 429 + retry-after to the client. A
// rate-limit 429 never rotates accounts (that just moves the burst); it pauses
// the account so concurrent requests wait, then retries the same account.
const RATE_LIMIT_ABSORB_MAX_SECONDS =
  Number(process.env.TEAMCLAUDE_RATE_LIMIT_ABSORB_MAX_SECONDS) || 15;

// Response header names that are connection-specific and thus illegal on an
// HTTP/2 response (Node's Http2ServerResponse.writeHead rejects them). Also
// hop-by-hop on h1, so stripping them is correct on both paths.
const CONNECTION_SPECIFIC_HEADERS = new Set([
  'connection', 'keep-alive', 'transfer-encoding', 'upgrade',
  'proxy-connection', 'te', 'trailer',
]);

// Constant-time proxy-API-key comparison (both the HTTP gate and the CONNECT
// gate use it). Returns false on any type/length mismatch without leaking timing.
export function safeKeyEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  const ba = Buffer.from(a);
  const bb = Buffer.from(b);
  if (ba.length !== bb.length) return false;
  return timingSafeEqual(ba, bb);
}

// True if a socket's remote address is loopback — the proxy-key gate exempts
// localhost on both the HTTP and CONNECT paths.
export function isLoopbackAddr(addr) {
  return addr === '127.0.0.1' || addr === '::1' || addr === '::ffff:127.0.0.1';
}

export function filterUpstreamRequestHeaders(input) {
  const headers = {};
  for (const [key, value] of Object.entries(input || {})) {
    const lk = key.toLowerCase();
    if (lk.startsWith(':')) continue;
    if (HOP_BY_HOP_HEADERS.has(lk)) continue;
    if (lk === 'x-api-key') continue;
    if (lk.startsWith('x-fleet-')) continue;
    if (lk === 'accept-encoding') continue;
    headers[key] = value;
  }
  return headers;
}

export function createProxyServer(accountManager, config, hooks = {}, sx = null) {
  const upstream = config.upstream || 'https://api.anthropic.com';
  const proxyApiKey = config.proxy?.apiKey;
  const logDir = config.logDir || null;
  const holdMs = (config.holdSeconds || 0) * 1000;

  if (logDir) {
    mkdir(logDir, { recursive: true }).catch(() => {});
  }

  const requestHandler = async (req, res) => {
    try {
      // Auth check — skip for localhost connections.
      const clientKey = req.headers['x-api-key'];
      const isLocal = isLoopbackAddr(req.socket.remoteAddress);
      if (proxyApiKey && !safeKeyEqual(clientKey, proxyApiKey) && !isLocal) {
        res.writeHead(401, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          type: 'error',
          error: { type: 'authentication_error', message: 'Invalid proxy API key' },
        }));
        return;
      }

      // Forward-proxy request (HTTP_PROXY): an absolute-form URL is a tool
      // proxying plain HTTP to some host. Account logic is only for hosts we
      // manage (the Anthropic upstream, which is HTTPS-only and never arrives
      // this way); forward anything else transparently instead of hijacking it.
      if (/^https?:\/\//i.test(req.url || '')) { relayHttpForward(req, res); return; }

      // Liveness. Deliberately does NOT touch the account manager, the
      // session tracker, the waiting map or the database — it answers from
      // constants, so its latency measures one thing only: whether the event
      // loop is turning.
      //
      // `/teamclaude/status` cannot do that job. It is a display endpoint that
      // serialises every account, every route and every waiting session, and
      // the launcher was using it as the liveness probe with a 2s timeout.
      // Measured on a process that had been up five hours: status answered in
      // 11.3, 12.5, 20.1 and 2.1 seconds while nothing was wrong with the
      // fleet, so `fleet run` refused to start Claude at all — the gate failed
      // on the cost of the report, not on the health of the service.
      //
      // Ordered before the status branch so a stalled status handler cannot
      // delay it.
      if (req.method === 'GET' && req.url === '/teamclaude/ready') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          ok: true,
          pid: process.pid,
          uptimeSeconds: Math.round(process.uptime()),
          // Cheap enough to include here: it is two reads of a plain object,
          // and it is the number anyone debugging a slow response wants first.
          loop: loopLagSnapshot(),
          // What flipping FLEET_AUTH_MODE to enforce would have refused. The
          // decision to flip is supposed to be made from this number after a
          // long enough window, not from confidence.
          auth: { mode: authMode(), ...authAudit },
        }));
        return;
      }

      // Status endpoint
      if (req.method === 'GET' && req.url === '/teamclaude/status') {
        const status = accountManager.getStatus();
        const extra = hooks.getStatusExtra?.() || {};
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(
          { ...extra, ...status, waiting: waitingSnapshot() }, null, 2));
        return;
      }

      // Reload endpoint — re-sync accounts from config without a restart. This
      // is the headless equivalent of pressing 'R' in the TUI. Local control
      // only (no upstream calls); the auth gate above already applies.
      if (req.method === 'POST' && req.url === '/teamclaude/reload') {
        if (!hooks.reload) {
          res.writeHead(501, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ ok: false, error: 'reload not supported' }));
          return;
        }
        try {
          const added = await hooks.reload();
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ ok: true, added: added || 0 }));
        } catch (err) {
          res.writeHead(500, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ ok: false, error: err.message }));
        }
        return;
      }

      // On-demand zero-spend quota refresh for the local desktop helper. The
      // prober enforces its own cooldown; this endpoint never sends a model
      // message and is unavailable when quota probing is disabled.
      if (req.method === 'POST' && req.url === '/teamclaude/probe') {
        if (!hooks.probeQuota) {
          res.writeHead(501, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ ok: false, error: 'quota probe not supported' }));
          return;
        }
        try {
          const result = await hooks.probeQuota();
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify(result || { ok: true }));
        } catch (err) {
          res.writeHead(500, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ ok: false, error: err.message }));
        }
        return;
      }

      return forward(req, res);
    } catch (err) {
      console.error('[TeamClaude] Unhandled error:', err);
    }
  };

  const forward = createProxyRequestListener({ accountManager, upstream, logDir, hooks, sx, holdMs, config });
  const server = http.createServer(requestHandler);

  // Forward-proxy support (always on, so multiple claude instances can use
  // either ANTHROPIC_BASE_URL or HTTPS_PROXY against the same server). A CONNECT
  // to the upstream host is a transparent MITM relay (rewrite only auth); the
  // test host is answered locally; anything else is blind-tunneled. Certs are
  // minted lazily on the first intercepted CONNECT.
  const mitmHost = (() => { try { return new URL(upstream).hostname; } catch { return 'api.anthropic.com'; } })();
  let certsPromise = null;
  const ensureLeaf = async () => {
    // Reset the memo on failure so a transient cert error doesn't wedge the MITM
    // path permanently (a cached rejected promise would re-throw on every CONNECT).
    certsPromise ||= ensureCerts(mitmHost).catch((err) => { certsPromise = null; throw err; });
    const c = await certsPromise;
    return { key: c.leafKeyPem, cert: c.leafCertPem };
  };
  server.on('connect', createConnectHandler({ config, accountManager, ensureLeaf, logDir, hooks, log: console.error, sx }));
  // Remote Control's real-time channel is a WebSocket, not a request/response
  // call — Node fires 'upgrade' for that handshake, never 'request', so it
  // needs its own listener (base-URL routing path; the MITM path wires the
  // same relayUpgrade onto its own terminating server in mitm.js).
  server.on('upgrade', (req, socket, head) => relayUpgrade(req, socket, head, upstream, sx));

  return server;
}

/**
 * Resolve an account pin to an index, or null.
 *
 * Accepted forms, first match wins:
 *   - `accountUuid/orgUuid` — fully qualified, the only form that distinguishes
 *     one person's accounts across several orgs
 *   - `accountUuid`
 *   - `orgUuid`
 *   - the display name (`email` or `email (Org)`), or the bare email
 *
 * UUIDs are the identity to use for anything scripted or long-lived: display
 * names are rewritten in place when an email gains a second org (see
 * accountsCommand), so a name is a convenience, not an identifier.
 *
 * The rotation index is deliberately NOT accepted. It is array position, so
 * deleting an account would silently repoint every later pin at a DIFFERENT
 * account — a wrong-account misroute rather than an honest failure.
 */
export function resolveAccountPin(accountManager, token) {
  const accounts = accountManager.accounts || [];
  const norm = (s) => (s || '').trim().toLowerCase();
  const t = norm(token);
  if (!t) return null;

  const at = (pick) => accounts.findIndex(a => norm(pick(a)) === t);
  const qualified = accounts.findIndex(a => a.accountUuid && a.orgUuid
    && `${norm(a.accountUuid)}/${norm(a.orgUuid)}` === t);

  for (const i of [
    qualified,
    at(a => a.accountUuid),
    at(a => a.orgUuid),
    at(a => a.name),
    at(a => (a.name || '').split(' (')[0]), // display name minus the org suffix
  ]) if (i >= 0) return i;

  return null;
}

// Paths that must reach upstream with the client's own credential (never a
// rotated account token): the Remote Control channel and attachment transfers.
// teamclaude applies its account logic (rotation, exhaustion, token injection)
// ONLY to hosts it manages — the Anthropic upstream. Anything else must be
// forwarded transparently, never hijacked into "all accounts exhausted". For
// HTTPS this is already true (the CONNECT tunnel in mitm.js blind-relays
// non-upstream hosts). This is the plain-HTTP counterpart: a tool honoring
// HTTP_PROXY sends an ABSOLUTE-form request (`GET http://host/path`), which
// otherwise gets misrouted to Anthropic. Blind-relay it to its target with the
// client's own headers — no account selection, no token injection,
// content-encoding passed through (a transparent forward proxy). Anthropic is
// HTTPS-only, so in practice this only ever sees third-party hosts.
export function relayHttpForward(req, res) {
  let target;
  try { target = new URL(req.url); } catch {
    res.writeHead(400, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ type: 'error', error: { type: 'invalid_request_error', message: 'Malformed forward-proxy URL' } }));
    return;
  }
  const transport = target.protocol === 'http:' ? http : https;
  const headers = {};
  for (const [key, value] of Object.entries(req.headers)) {
    const lk = key.toLowerCase();
    // Drop hop-by-hop + proxy-control headers; `host` is reset from the target.
    if (lk.startsWith(':') || HOP_BY_HOP_HEADERS.has(lk) || lk === 'proxy-connection') continue;
    headers[key] = value;
  }

  const upstreamReq = transport.request(target, { method: req.method, headers }, (upstreamRes) => {
    const responseHeaders = {};
    for (const [key, value] of Object.entries(upstreamRes.headers)) {
      if (CONNECTION_SPECIFIC_HEADERS.has(key)) continue;
      responseHeaders[key] = value;
    }
    res.writeHead(upstreamRes.statusCode, responseHeaders);
    upstreamRes.pipe(res);
  });
  upstreamReq.on('error', (err) => {
    console.error(`[TeamClaude] HTTP forward to ${target.host} failed:`, err.message);
    if (!res.headersSent) {
      res.writeHead(502, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ type: 'error', error: { type: 'proxy_error', message: 'Upstream unreachable' } }));
    }
  });
  res.on('close', () => upstreamReq.destroy());
  if (['GET', 'HEAD'].includes(req.method)) upstreamReq.end();
  else req.pipe(upstreamReq);
}

const CLIENT_CREDENTIAL_PATHS = ['/v1/code/', '/api/oauth/files/', '/api/oauth/file_upload'];

// Client housekeeping. These need an ACCOUNT CREDENTIAL — they go to Anthropic
// — but they do not need a writer fence, because they do not write a transcript.
// Fencing exists to stop two windows appending to one session; fetching your
// settings appends nothing.
//
// Sending them through the coordinator produced 288 refusals a day on the
// server, every one logged as `holder is required; launch Claude through
// Fleet`, and for months the answer to "what is bypassing the launcher" was
// "something on a schedule". Nothing was bypassing anything: Claude Code polls
// these on its own, with no session and no model, and Fleet demanded a writer
// for them.
const FENCELESS_PATHS = ['/api/claude_code/'];

/**
 * Build the core proxy request listener — buffer the body, then forward with
 * account selection + retry (forwardRequest). Shared by the base HTTP server and
 * the MITM's terminating h2/h1 server, so both get identical buffering, model-
 * aware routing, and retry-on-quota behavior. Control endpoints (status/reload)
 * and the proxy-API-key gate live in the base server's wrapper, not here.
 */
export function createProxyRequestListener({
  accountManager,
  upstream,
  logDir = null,
  hooks = {},
  sx = null,
  holdMs = 0,
  config = {},
  forcedPin = null,
  transportHolder = null,
}) {
  let counter = 0;
  return async (req, res) => {
    try {
      // Claude Code's telemetry (`/api/event_logging/*`) is high-volume noise in
      // the activity log. `config.eventLogging` (read live so the TUI toggle takes
      // effect immediately): 'show' forwards + displays; 'hide' (default) forwards
      // but suppresses the activity entry; 'block' answers 200 locally without
      // forwarding (no upstream round-trip, no account/token spent).
      const eventLogging = config?.eventLogging || 'hide';
      const isEventLog = (req.url || '').startsWith('/api/event_logging');
      if (isEventLog && eventLogging === 'block') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end('{}');
        return;
      }
      const hideActivity = isEventLog && eventLogging !== 'show';
      // Client token refresh: pass through untouched (the proxy manages its own
      // tokens via ensureTokenFresh; rewriting client refreshes would conflict).
      if (req.method === 'POST' && req.url === '/v1/oauth/token') { await relayRaw(req, res, upstream, sx); return; }
      // Remote Control (/v1/code/*) is bound to the session's paired claude.ai
      // identity — forward with the client's OWN credential (streamed), never a
      // rotated account token, which would 403 the worker event stream.
      // Attachment transfers (/api/oauth/files/*, /api/oauth/file_upload) are
      // likewise account-bound: files uploaded from claude.ai belong to the
      // paired identity, so fetching them with a rotated token 403s and Claude
      // Code silently drops the image from the message.
      if (CLIENT_CREDENTIAL_PATHS.some((p) => (req.url || '').startsWith(p))) { await relayStream(req, res, upstream, sx); return; }

      // Account pin: a request to `/tc-acct/<name-or-index>/...` (e.g. via
      // ANTHROPIC_BASE_URL=http://host:port/tc-acct/deepseek) is forced onto that
      // one account, bypassing rotation. Used by the keep-warm scheduler and for
      // manual per-account testing. The prefix is stripped before forwarding.
      let pinnedIndex = null;
      // DEPRECATED: the path-prefix pin. Superseded by TC_ACCT, which works in
      // MITM mode too (this form cannot — inside a CONNECT tunnel the path is
      // the real upstream one). Kept for the warmer and for direct API callers.
      // One segment only, so the fully-qualified `accountUuid/orgUuid` form is
      // not expressible here; use TC_ACCT for that.
      const url = req.url || '';
      const afterPrefix = url.startsWith(PIN_PREFIX) ? url.slice(PIN_PREFIX.length) : null;
      // The token runs to the next '/', which also begins the real request path.
      const tokenEnd = afterPrefix == null ? -1 : afterPrefix.indexOf('/');
      if (tokenEnd > 0) {
        const token = decodeURIComponent(afterPrefix.slice(0, tokenEnd));
        pinnedIndex = resolveAccountPin(accountManager, token);
        if (pinnedIndex == null) {
          const reqId = ++counter;
          const sessionId = req.headers['x-claude-code-session-id'] || null;
          if (!hideActivity) hooks.onRequestEnd?.(reqId, { method: req.method, path: req.url, account: `(unknown pin: "${token}")`, status: 404, model: null, sessionId, pinned: false });
          res.writeHead(404, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ type: 'error', error: { type: 'not_found_error', message: `Unknown account pin "${token}"` } }));
          return;
        }
        req.url = afterPrefix.slice(tokenEnd);
      }

      // MITM-mode pin. A CONNECT carrying `Proxy-Authorization: Basic <acct>:…`
      // has no URL to hang a `/tc-acct/` prefix on — the path inside the tunnel
      // is the real Anthropic one — so the pin arrives as a listener bound to
      // that account (see createConnectHandler). Resolved per request rather
      // than at CONNECT time: a hot reload can renumber accounts while a tunnel
      // is open, and a name outliving an index is the safer half of that race.
      if (pinnedIndex == null && forcedPin != null) {
        pinnedIndex = resolveAccountPin(accountManager, forcedPin);
        if (pinnedIndex == null) {
          const reqId = ++counter;
          const sessionId = req.headers['x-claude-code-session-id'] || null;
          if (!hideActivity) hooks.onRequestEnd?.(reqId, { method: req.method, path: req.url, account: `(unknown pin: "${forcedPin}")`, status: 404, model: null, sessionId, pinned: false });
          res.writeHead(404, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ type: 'error', error: { type: 'not_found_error', message: `Unknown account pin "${forcedPin}" (from TC_ACCT)` } }));
          return;
        }
      }

      const reqId = ++counter;
      // Claude Code tags each session's requests with this header (present on
      // /v1/messages and count_tokens). Read from headers up front so it drives
      // session-aware routing (issue #109) and colors the TUI activity stream.
      const sessionId = req.headers['x-claude-code-session-id'] || null;
      if (!hideActivity) hooks.onRequestStart?.(reqId, { method: req.method, path: req.url, sessionId, pinned: pinnedIndex != null });

      // Buffer request body (needed to resend on a different account after a 429).
      // Peek the top-level `model` field incrementally as chunks arrive so the
      // TUI can show it the instant it appears in the stream — usually the first
      // frame — rather than waiting for the whole body and the request to finish.
      const bodyChunks = [];
      const modelFinder = new TopLevelFieldFinder('model');
      for await (const chunk of req) {
        bodyChunks.push(chunk);
        if (!modelFinder.done) {
          const found = modelFinder.push(chunk);
          if (found && !hideActivity) hooks.onRequestModel?.(reqId, { model: found });
        }
      }
      const body = Buffer.concat(bodyChunks);

      const model = modelFinder.done ? modelFinder.value : parseRequestModel(body);
      // An advisor request (Claude Code's advisor tool) carries a SECOND model
      // nested in tools[]; the advisor sub-inference runs on the selected
      // account, so selection must be eligible for it too (issue #98).
      const advisorModel = parseAdvisorModel(body);

      // Model blocklist (issue #116): reject a request for a blocked model right
      // here instead of forwarding it. A model no account can serve (e.g. Fable
      // once it left base plans) otherwise gets rate-limited upstream and hangs
      // the pipeline; a fast, non-retryable 400 lets the client move on. Read
      // live from the shared config so the TUI editor takes effect immediately.
      const blockedBy = model ? (config?.blockedModels || []).find((p) => modelGlobMatches(p, model)) : null;
      if (blockedBy) {
        if (!res.headersSent) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ type: 'error', error: { type: 'invalid_request_error', message: `Model "${model}" is blocked by teamclaude (matched "${blockedBy}").` } }));
        }
        hooks.onRequestEnd?.(reqId, { method: req.method, path: req.url, account: '(blocked)', status: 400, model, sessionId });
        return;
      }

      const ctx = {
        account: null,
        status: null,
        tried: new Set(),
        // `tried` is only one selection epoch's exclusion set. If every
        // candidate was transiently rejected, run one fresh selection epoch
        // before entering the long-hold loop; otherwise a recovered account
        // remains excluded forever on this request.
        reconsideredTried: false,
        reauthed: new Set(),
        model,
        advisorModel,
        pinnedIndex,
        holdBudgetMs: holdMs,
        sessionId,
        // Local-only CONNECT closure metadata. It is never copied into request
        // headers, request logs, account pins, or the upstream hop.
        transportHolder,
      };
      // Hold the session "in flight" across the WHOLE request (incl. retries and
      // a multi-minute streaming completion) so it stays counted as active and
      // never expires mid-request.
      accountManager.beginSession(sessionId);
      try {
        await forwardRequest(req, res, body, accountManager, upstream, 0, hooks, reqId, ctx, logDir, sx);
      } catch (err) {
        const publicStatus = Number(err?.fleetStatus) || 502;
        const publicError = err?.fleetPublicError || {
          type: 'proxy_error',
          message: 'Internal proxy error',
        };
        ctx.status = ctx.status || publicStatus;
        console.error('[TeamClaude] Unhandled error:', err);
        if (!res.headersSent) {
          res.writeHead(publicStatus, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ type: 'error', error: publicError }));
        }
      } finally {
        waiting.delete(reqId);
        ctx.stopFleetHeartbeat?.();
        accountManager.endSession(sessionId);
        if (!hideActivity) hooks.onRequestEnd?.(reqId, { method: req.method, path: req.url, account: ctx.account, status: ctx.status, model: ctx.model, sessionId, pinned: ctx.pinnedIndex != null });
      }
    } catch (err) {
      console.error('[TeamClaude] Unhandled error:', err);
    }
  };
}

// Per-request https.Agent tunneled through sx.org — one-shot (no keep-alive
// reuse, matching upstream-fetch.js's proxiedFetch), so a fresh sx tunnel is
// dialed for this connection only.
function sxAgent(sx, targetHost) {
  const proxy = sx.getProxy();
  const agent = new https.Agent({ keepAlive: false });
  agent.createConnection = (_options, cb) => {
    tunnelTls({ proxy, targetHost, targetPort: 443, tlsOptions: sx.tlsOptions || {} })
      .then((sock) => cb(null, sock))
      .catch((err) => cb(err));
    return undefined;
  };
  return agent;
}

/**
 * Relay a request to upstream with the client's OWN headers intact (including
 * its authorization) — used for Remote Control (/v1/code/*), whose event
 * stream is a long-poll: the client keeps the request open indefinitely and
 * the upstream may withhold response headers for minutes between events. No
 * buffering, no timeout, no reconstruction — just pipe bytes both ways as they
 * arrive, exactly like a transparent proxy would.
 */
function relayStream(req, res, upstream, sx) {
  const target = new URL(`${upstream}${req.url}`);
  const headers = {};
  for (const [key, value] of Object.entries(req.headers)) {
    const lk = key.toLowerCase();
    if (lk.startsWith(':') || HOP_BY_HOP_HEADERS.has(lk) || lk === 'accept-encoding') continue;
    headers[key] = value;
  }

  const useProxy = !!(sx?.useByDefault() && sx.isProvisioned());
  const agent = useProxy ? sxAgent(sx, target.hostname) : undefined;
  const transport = target.protocol === 'http:' ? http : https;

  const upstreamReq = transport.request(target, { method: req.method, headers, agent }, (upstreamRes) => {
    const responseHeaders = {};
    for (const [key, value] of Object.entries(upstreamRes.headers)) {
      if (CONNECTION_SPECIFIC_HEADERS.has(key) || key === 'content-encoding' || key === 'content-length') continue;
      responseHeaders[key] = value;
    }
    res.writeHead(upstreamRes.statusCode, responseHeaders);
    upstreamRes.pipe(res);
  });

  upstreamReq.on('error', (err) => {
    console.error('[TeamClaude] Remote Control relay error:', err.message);
    if (!res.headersSent) {
      res.writeHead(502, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ type: 'error', error: { type: 'proxy_error', message: 'Upstream unreachable' } }));
    }
  });
  // Client disconnected (e.g. Claude Code closed the channel): tear down the
  // upstream side too instead of leaking an open connection.
  res.on('close', () => upstreamReq.destroy());

  if (['GET', 'HEAD'].includes(req.method)) upstreamReq.end();
  else req.pipe(upstreamReq);
}

/**
 * Relay a WebSocket upgrade (e.g. Remote Control's real-time
 * `/v1/session_ingress/ws/*` channel) to upstream with the client's own
 * headers intact. An HTTP server never emits 'request' for an Upgrade
 * handshake — only 'upgrade', with a raw socket instead of a response object —
 * so this needs its own relay rather than going through relayStream/res.
 * Reuses Node's http(s) client, which already knows how to speak the Upgrade
 * handshake (emits its own 'upgrade' event on a 101); once that fires it's
 * just two raw sockets spliced together.
 */
export function relayUpgrade(req, socket, head, upstream, sx) {
  const target = new URL(`${upstream}${req.url}`);
  const headers = {};
  for (const [key, value] of Object.entries(req.headers)) {
    const lk = key.toLowerCase();
    // Unlike relayStream, do NOT strip 'upgrade'/'connection' here — they ARE
    // the handshake. Only 'host' (the client transport reconstructs it from
    // `target`) and h2 pseudo-headers are dropped.
    if (lk.startsWith(':') || lk === 'host') continue;
    headers[key] = value;
  }

  const useProxy = !!(sx?.useByDefault() && sx.isProvisioned());
  const agent = useProxy ? sxAgent(sx, target.hostname) : undefined;
  const transport = target.protocol === 'http:' ? http : https;

  const upstreamReq = transport.request(target, { method: req.method, headers, agent });

  upstreamReq.on('upgrade', (upstreamRes, upstreamSocket, upstreamHead) => {
    const headerLines = Object.entries(upstreamRes.headers)
      .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(', ') : v}`).join('\r\n');
    socket.write(`HTTP/1.1 ${upstreamRes.statusCode} ${upstreamRes.statusMessage}\r\n${headerLines}\r\n\r\n`);
    if (upstreamHead?.length) socket.write(upstreamHead);
    if (head?.length) upstreamSocket.write(head);
    socket.pipe(upstreamSocket);
    upstreamSocket.pipe(socket);
    // An upgraded socket defaults to half-open: the peer's FIN only ends the
    // READABLE side ('end'), it does NOT destroy the socket or fire 'close' —
    // so without this, one side hanging up (dropped wifi, killed CLI) leaves
    // the other socket open forever. destroy() is idempotent, so reacting to
    // both 'end' and 'close' on each side is a safe, redundant backstop.
    socket.on('end', () => upstreamSocket.destroy());
    upstreamSocket.on('end', () => socket.destroy());
    socket.on('close', () => upstreamSocket.destroy());
    upstreamSocket.on('close', () => socket.destroy());
  });

  upstreamReq.on('error', (err) => {
    console.error('[TeamClaude] Remote Control WebSocket relay error:', err.message);
    socket.destroy();
  });
  socket.on('error', () => upstreamReq.destroy());

  upstreamReq.end();
}

/**
 * Relay a request to upstream with no header rewriting — pure passthrough.
 */
async function relayRaw(req, res, upstream, sx) {
  const bodyChunks = [];
  for await (const chunk of req) bodyChunks.push(chunk);
  const body = Buffer.concat(bodyChunks);

  try {
    const upstreamRes = await upstreamFetch(`${upstream}${req.url}`, {
      method: req.method,
      headers: {
        'content-type': req.headers['content-type'] || 'application/json',
        'accept': req.headers['accept'] || 'application/json',
        'user-agent': req.headers['user-agent'] || 'node',
      },
      body: body.length > 0 ? body : undefined,
    }, sx, sx?.useByDefault());

    const responseBody = await upstreamRes.text();
    const responseHeaders = {};
    for (const [key, value] of upstreamRes.headers.entries()) {
      // `.text()` already decompressed the body, so drop content-encoding and
      // the now-stale content-length (both refer to the compressed bytes) — else
      // a gzip'd upstream response reaches the client mis-framed / truncated.
      if (key === 'transfer-encoding' || key === 'connection' ||
          key === 'content-encoding' || key === 'content-length') continue;
      responseHeaders[key] = value;
    }
    res.writeHead(upstreamRes.status, responseHeaders);
    res.end(responseBody);
  } catch (err) {
    console.error('[TeamClaude] Raw relay error:', err.message);
    if (!res.headersSent) {
      res.writeHead(502, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ type: 'error', error: { type: 'proxy_error', message: 'Upstream unreachable' } }));
    }
  }
}


function logTimestamp() {
  const d = new Date();
  const pad = (n, w = 2) => String(n).padStart(w, '0');
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}

// A per-request log that streams to disk as the request/response flow, instead
// of buffering the whole body in memory and writing once at the end. The file
// is opened on first write; header sections are written verbatim and bodies are
// streamed through BodyWriter (JSON pretty-printed on the fly, SSE/other raw),
// so even a ~1M-token response costs only the current chunk.
function openRequestLog(logDir, reqId) {
  const filename = `${logTimestamp()}_${String(reqId).padStart(5, '0')}.log`;
  const ws = createWriteStream(join(logDir, filename), { flags: 'a' });
  ws.on('error', (err) => console.error(`[TeamClaude] Failed to write log: ${err.message}`));
  let ended = false;
  const write = (s) => { if (!ended && s) ws.write(Buffer.from(String(s), 'latin1')); };
  return {
    write,
    // Stream a complete body buffer under a section header.
    body(label, buf, contentType) {
      if (!buf || !buf.length) { write(`\n\n=== ${label} ===\n(empty)`); return; }
      new BodyWriter(write, label, contentType || '').chunk(buf);
    },
    // A BodyWriter to append chunks incrementally (e.g. an SSE response).
    bodyWriter(label, contentType) { return new BodyWriter(write, label, contentType || ''); },
    end() { if (!ended) { ended = true; ws.end('\n'); } },
  };
}

function formatHeaders(headers) {
  if (headers.entries) {
    return [...headers.entries()].map(([k, v]) => `  ${k}: ${v}`).join('\n');
  }
  return Object.entries(headers).map(([k, v]) => `  ${k}: ${v}`).join('\n');
}

// Classify a thrown upstream error into what it says about the ACCOUNT.
//
//   'transport' — the path to Anthropic is broken. Says nothing about the
//                 account, and is identical for every account, so failing over
//                 cannot help: it only marks each account tried in turn until
//                 the request runs out of accounts and parks.
//   'account'   — plausibly specific to this account; fail over to another.
//
// This split exists because of a measured outage. With `sx.mode: "off"` on disk
// but sx.org still armed in the running process, every request got
// `sx.org proxy refused CONNECT: HTTP/1.1 503`. That error is a bare
// `new Error(...)` with no `code` and no matching `name`, so it fell through to
// 'account', which added two accounts holding 83% and 70% of their weekly quota
// to ctx.tried and then parked the request for six hours. Over one six-hour
// window: 2,259 parks while those two accounts moved 0.08 -> 0.17 and
// 0.21 -> 0.30 weekly utilisation. The capacity was there; a proxy fault was
// being read as an account fault.
//
// A bad credential arrives as a 401 *response*, never as a throw, so nothing
// here should ever sideline an account on transport evidence alone.
// The egress hop refusing CONNECT is transient and common: Shadowrocket's
// tunnel answers 503 in bursts while the fleet and the network are both fine.
//
// The first version allowed two attempts 400ms apart, on the guess that these
// were blips. Timestamps proved otherwise: 351 refusals arrived as ONE
// continuous burst lasting 29.8 seconds, 148 of them inside a single ten-second
// window. Eight hundred milliseconds of patience does not touch that, which is
// why the owner still saw the fleet hitching after the first fix.
//
// A request arriving at a random point in a 30-second outage needs about
// fifteen seconds of patience on average. This budget is ~21s across seven
// attempts with a widening gap — enough to ride out most of a burst of the one
// length actually measured, and bounded so a proxy that is genuinely down
// surfaces instead of hanging forever.
//
// Waiting is the right trade here and it is the owner's stated order of
// priorities: a session that waits recovers by itself, a session that errors
// needs a human. It is only safe because a REFUSED CONNECT never opened a
// tunnel — no bytes reached Anthropic, so nothing is duplicated by re-sending.
const EGRESS_RETRY_DELAYS_MS = [400, 800, 1600, 3000, 5000, 5000, 5000];
const EGRESS_RETRY_LIMIT = EGRESS_RETRY_DELAYS_MS.length;


export function classifyUpstreamError(err) {
  if (!(err instanceof Error)) return 'account';

  const code = err.code;
  const name = err.name;
  const message = String(err.message || '');

  if (
    code === 'TEAMCLAUDE_HEADERS_TIMEOUT' || code === 'TEAMCLAUDE_BODY_TIMEOUT' ||
    code === 'TEAMCLAUDE_ADMISSION_TIMEOUT' ||
    name === 'TimeoutError' || name === 'AbortError' ||
    message.includes('fetch failed') ||
    code === 'ECONNRESET' || code === 'ECONNREFUSED' ||
    code === 'ETIMEDOUT' || code === 'UND_ERR_CONNECT_TIMEOUT' ||
    code === 'UND_ERR_HEADERS_TIMEOUT' || code === 'UND_ERR_BODY_TIMEOUT'
  ) {
    return 'transport';
  }

  // Egress-path faults. These are thrown as bare Errors from the proxy tunnel
  // helper (sx.js) and from CONNECT handling, so they carry no code to match.
  if (
    /proxy refused CONNECT/i.test(message) ||
    /proxy CONNECT timed out/i.test(message) ||
    /proxy CONNECT response too large/i.test(message) ||
    /tunnel .* failed/i.test(message) ||
    /socket hang up/i.test(message) ||
    code === 'EAI_AGAIN' || code === 'ENOTFOUND' || code === 'EHOSTUNREACH' ||
    code === 'ENETUNREACH' || code === 'EPIPE'
  ) {
    return 'transport';
  }

  return 'account';
}

export async function forwardRequest(req, res, body, accountManager, upstream, retryCount, hooks, reqId, ctx, logDir, sx, useSx) {
  const maxRetries = accountManager.accounts.length;
  // This function is exported, so a caller may hand us a ctx built elsewhere.
  // The 401 path reads ctx.reauthed on every response; default it here rather
  // than trusting every construction site to include it.
  ctx.reauthed ??= new Set();
  // Whether THIS attempt dials via sx.org. Undefined on the first call → derive
  // from the default policy ('always' routes; 'off'/'429' start direct).
  const route = useSx === undefined ? !!(sx?.useByDefault()) : useSx;

  // Select account, skipping any already tried (and failed) this request.
  // The model scopes availability so a Fable-exhausted account is skipped only
  // for Fable requests (it still serves other models).
  // A pinned request (via /tc-acct/<name>) forces one exact account and never
  // rotates or fails over: once that account has been tried, `account` is null
  // and the caller gets the exhausted response rather than leaking to another.
  // A pin deliberately bypasses SCHEDULING — that is what it is for. It must
  // not bypass the shared-account reserve, which is a policy protecting someone
  // else's quota, not a routing preference.
  //
  // Measured before this guard existed: a `/tc-acct/<uuid>` request reached the
  // upstream while the coordinator recorded nothing at all (0 route.changed
  // events), so `_eligible` and the floor never ran. Anything on loopback could
  // spend a shared account to zero.
  if (ctx.pinnedIndex != null && hooks.isPinPermitted) {
    const verdict = await hooks.isPinPermitted({
      account: accountManager.accounts[ctx.pinnedIndex], ctx, req,
    });
    if (verdict && verdict.allowed === false) {
      ctx.status = 403;
      if (!res.headersSent) {
        res.writeHead(403, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          type: 'error',
          error: {
            type: 'permission_error',
            message: verdict.reason || 'Pinning this account is not permitted by Fleet policy.',
          },
        }));
      }
      return;
    }
  }

  const fenceless = FENCELESS_PATHS.some(prefix => (req.url || '').startsWith(prefix));
  const account = ctx.pinnedIndex != null
    ? (ctx.tried.has(ctx.pinnedIndex) ? null : accountManager.accounts[ctx.pinnedIndex])
    : (hooks.selectAccount && !fenceless)
      ? await hooks.selectAccount({ accountManager, ctx, req })
      : accountManager.getActiveAccount(ctx.tried, ctx.model, ctx.advisorModel, ctx.sessionId);
  if (!account) {
    // A pinned request concerns exactly one account: don't compute a fleet-wide
    // retry-after or sleep on other accounts' windows — return immediately.
    if (ctx.pinnedIndex != null) {
      ctx.status = 429;
      ctx.account = '(pinned account unavailable)';
      if (!res.headersSent) {
        res.writeHead(429, { 'Content-Type': 'application/json', 'retry-after': '5' });
        res.end(JSON.stringify({
          type: 'error',
          error: { type: 'rate_limit_error', message: 'Pinned account is unavailable (rate-limited, errored, or already tried). Retry shortly.' },
        }));
      }
      return;
    }

    // A request may have tried every account while quota/auth state was being
    // refreshed. Those exclusions are not durable account state: availability
    // is revalidated by AccountManager and the Fleet coordinator on every
    // selection. Give the request one immediate fresh selection epoch before
    // declaring the whole fleet exhausted. Without this reset, long-held
    // requests keep sending every recovered account in exclude_account_ids and
    // sleep forever in 60-second increments despite live capacity.
    if (ctx.tried.size > 0 && !ctx.reconsideredTried) {
      ctx.reconsideredTried = true;
      ctx.tried.clear();
      return forwardRequest(
        req, res, body, accountManager, upstream, retryCount,
        hooks, reqId, ctx, logDir, sx, route,
      );
    }

    ctx.status = 429;
    ctx.account = '(none available)';
    const status = accountManager.getStatus();
    const retryAfter = computeRetryAfter(status.accounts);

    // Long-hold mode: hold the HTTP connection and poll until an account
    // recovers or the budget (holdSeconds) runs out. Claude Code waits for
    // the first response byte, so this is transparent to the client as long
    // as API_TIMEOUT_MS on the Claude Code side is large enough.
    if (ctx.holdBudgetMs > 0) {
      // Cap the per-poll sleep to 60s so a newly-available account (e.g. one
      // manually enabled or whose quota reset early) is picked up within a
      // minute instead of sleeping the full retryAfter (often 3600s).
      const waitMs = Math.min(retryAfter * 1000, ctx.holdBudgetMs, 60_000);
      ctx.holdBudgetMs -= waitMs;

      // The poll cadence (60s) is not the wait. Saying only "retry in 60s" 360
      // times is what made a six-hour park read as a broken fleet. State the
      // actual expected recovery on the first hold and periodically after.
      //
      // Note `ctx.tried` is always empty here: the reconsider branch above
      // clears it before the first hold, and every hold iteration clears it
      // again. So whether this request excluded anything has to be carried
      // separately — `ctx.everExcluded` is set wherever an account is added to
      // ctx.tried. That distinction is the whole diagnostic value: "the fleet
      // is genuinely spent" and "this request knocked out its own capacity"
      // look identical in the logs but need opposite responses.
      ctx.holdIterations = (ctx.holdIterations || 0) + 1;
      const first = ctx.holdIterations === 1;
      const escalate = ctx.holdIterations % HOLD_ESCALATION_ITERATIONS === 0;
      if (first || escalate) {
        const wait = retryAfter >= 3600
          ? `${(retryAfter / 3600).toFixed(1)}h`
          : `${Math.ceil(retryAfter / 60)}m`;
        console.log(
          `[TeamClaude] Fleet parked — no account can serve this request. `
          + `Soonest recovery ~${wait} away; holding up to `
          + `${Math.ceil(ctx.holdBudgetMs / 3600_000)}h more`
          + (ctx.everExcluded
            ? ` (this request excluded ${ctx.everExcluded} account(s) along the `
              + 'way — check the exclusion reasons above)'
            : ' (nothing was excluded by this request — capacity is genuinely spent)'),
        );
      }
      // Registered, not logged-and-forgotten. Deregistration is in the outer
      // `finally` so it happens on every exit path, including client aborts.
      const existing = waiting.get(reqId);
      waiting.set(reqId, {
        sessionId: ctx.sessionId || '(anonymous)',
        since: existing ? existing.since : Date.now(),
        retryAfterSeconds: retryAfter,
        everExcluded: Boolean(ctx.everExcluded),
      });
      console.log(`[TeamClaude] All accounts exhausted — holding connection, retry in ${Math.ceil(waitMs / 1000)}s (${Math.ceil(ctx.holdBudgetMs / 1000)}s budget left)`);
      await new Promise(resolve => setTimeout(resolve, waitMs));
      if (res.destroyed) return;
      // Start a new selection epoch after the hold. Durable quota/auth state is
      // represented by AccountManager, not by this request-local Set.
      ctx.reconsideredTried = false;
      ctx.tried.clear();
      return forwardRequest(req, res, body, accountManager, upstream, retryCount, hooks, reqId, ctx, logDir, sx, route);
    }

    const exhaustedRetries = ctx.exhaustedRetries || 0;
    if (exhaustedRetries < 1 && retryAfter <= INLINE_RETRY_AFTER_MAX_SECONDS) {
      ctx.exhaustedRetries = exhaustedRetries + 1;
      console.log(`[TeamClaude] All accounts exhausted — waiting ${retryAfter}s before retry`);
      await new Promise(resolve => setTimeout(resolve, retryAfter * 1000));
      if (res.destroyed) return;
      return forwardRequest(req, res, body, accountManager, upstream, retryCount, hooks, reqId, ctx, logDir, sx, route);
    }
    res.writeHead(429, {
      'Content-Type': 'application/json',
      'retry-after': String(retryAfter),
    });
    res.end(JSON.stringify({
      type: 'error',
      error: {
        type: 'rate_limit_error',
        message: `All ${accountManager.accounts.length} accounts exhausted. Retry in ${retryAfter}s.`,
      },
    }));
    return;
  }

  // Track which account handles this request
  ctx.account = account.name;
  // Pin this session to the serving account (for affinity) and keep it "active"
  // in the running-sessions readout. Passive when distribution is off.
  accountManager.recordSession(ctx.sessionId, account.index);
  hooks.onRequestRouted?.(reqId, { account: account.name });

  // Refresh OAuth token if needed
  await accountManager.ensureTokenFresh(account.index);
  if (account.status === 'error' && retryCount < maxRetries) {
    // This fail-over used to be silent. It is one of only three paths that add
    // to ctx.tried, and a request whose ctx.tried fills up sends every account
    // in exclude_account_ids, which the coordinator answers with no_capacity —
    // so the fleet reports "All accounts exhausted" while `fleet status` shows
    // healthy accounts with quota left. Without this line that transition is
    // undiagnosable from the logs.
    console.error(
      `[TeamClaude] Account "${account.name}" is in error state after token `
      + `refresh — excluding it from this request and failing over `
      + `(attempt ${retryCount + 1}/${maxRetries})`,
    );
    ctx.tried.add(account.index);
    ctx.everExcluded = (ctx.everExcluded || 0) + 1;
    return forwardRequest(req, res, body, accountManager, upstream, retryCount + 1, hooks, reqId, ctx, logDir, sx, route);
  }

  // Build upstream request headers
  const isOAuth = account.type === 'oauth';
  // Pseudo/hop-by-hop/API-key headers, compression negotiation and Fleet's
  // local-only routing identity are stripped before the Anthropic hop.
  const headers = filterUpstreamRequestHeaders(req.headers);

  if (isOAuth) {
    headers['authorization'] = `Bearer ${account.credential}`;
  } else {
    headers['x-api-key'] = account.credential;
  }

  const upstreamUrl = `${account.upstream || upstream}${req.url}`;
  const method = req.method;

  // Strip orphaned tool_use / tool_result blocks so a client that compacted or
  // interrupted a turn can't wedge the session with Anthropic's non-retryable
  // 400 ("tool_use ids were found without tool_result blocks"). No-op (same
  // Buffer) for a well-formed body.
  let sendBody = sanitizeToolPairs(body, req.url, req.headers['content-type']);
  // Align the body's account_uuid (in metadata.user_id) with the account whose
  // token we're injecting (same-length patch; no-op if absent).
  if (account.accountUuid) sendBody = patchAccountUuid(sendBody, account.accountUuid);
  // Rewrite the model name for accounts that target a different upstream (e.g.
  // GLM), which uses different model identifiers than Anthropic.
  if (account.modelMap) sendBody = rewriteModel(sendBody, account.modelMap);
  // If the body changed length (sanitize or model rewrite), update Content-Length
  // so the upstream doesn't receive a mismatched framing and truncate or stall.
  if (sendBody !== body) headers['content-length'] = String(sendBody.length);

  // Streaming request log, opened lazily on the first terminal outcome (a
  // pure-429-then-retry attempt writes no file, matching prior behavior). The
  // request head+body are written once, just before the response is logged.
  let log = null;
  let reqLogged = false;
  const getLog = () => (logDir ? (log ||= openRequestLog(logDir, reqId)) : null);
  const logRequestHead = () => {
    const l = getLog();
    if (!l || reqLogged) return;
    reqLogged = true;
    const safeHeaders = { ...headers };
    if (safeHeaders['x-api-key']) safeHeaders['x-api-key'] = safeHeaders['x-api-key'].slice(0, 15) + '...';
    if (safeHeaders['authorization']) safeHeaders['authorization'] = safeHeaders['authorization'].slice(0, 20) + '...';
    l.write(`=== REQUEST (account: ${account.name}, retry: ${retryCount}) ===\n${method} ${upstreamUrl}\n${formatHeaders(safeHeaders)}`);
    if (body.length > 0) l.body('REQUEST BODY', body, req.headers['content-type']);
  };

  try {
    // Storm control: pace requests onto a freshly-switched account so a failover
    // burst doesn't slam it all at once and cascade (issue #84). The slot is held
    // only until the response headers arrive — long enough to stagger the burst,
    // then released so streaming bodies don't tie up concurrency. Fail-open: a
    // client that disconnects while waiting just drops out.
    if (!await accountManager.admit(account.index, () => res.destroyed)) return;
    let upstreamRes;
    try {
      upstreamRes = await upstreamFetch(upstreamUrl, {
        method,
        headers,
        body: ['GET', 'HEAD'].includes(method) ? undefined : sendBody,
        redirect: 'manual',
      }, sx, route);
    } finally {
      accountManager.release(account.index);
    }

    // Extract rate limit headers
    const rateLimitHeaders = {};
    for (const [key, value] of upstreamRes.headers.entries()) {
      if (key.startsWith('anthropic-ratelimit-')) {
        rateLimitHeaders[key] = value;
      }
    }
    accountManager.updateQuota(account.index, rateLimitHeaders);

    // Any non-429 response is live proof a rate-limit hold no longer binds —
    // this is what lets a revalidation probe (a throttled account selected by
    // _selectProbe) clear its own hold and return the fleet to service.
    if (upstreamRes.status !== 429) accountManager.clearRateLimited(account.index);

    // Two kinds of 429 are handled differently below: a quota rejection rotates
    // to another account; a transient rate-limit throttle pauses + retries the
    // same account (never rotates — see #84).
    if (upstreamRes.status === 429) {
      // Clamp Retry-After to a sane window: missing/invalid falls back to 60s,
      // and out-of-range values are bounded to [1, 300]. A negative value would
      // otherwise bypass the wait cap — setTimeout returns immediately and a
      // pause/hold would be armed in the past.
      let retryAfter = parseInt(upstreamRes.headers.get('retry-after'), 10);
      if (Number.isNaN(retryAfter)) retryAfter = 60;
      // Discard the 429 response body
      await upstreamRes.body?.cancel();

      // Durable quota exhaustion vs. a transient rate limit. A "rejected" unified
      // status means a quota bucket is spent, so waiting and retrying the SAME
      // account is futile — switch to another account now (updateQuota above
      // already recorded the spent bucket's utilization from the headers).
      const rl = rateLimitHeaders;
      const generalRejected = rl['anthropic-ratelimit-unified-5h-status'] === 'rejected'
        || rl['anthropic-ratelimit-unified-7d-status'] === 'rejected';
      const fableRejected = rl['anthropic-ratelimit-unified-7d_oi-status'] === 'rejected' && !generalRejected;
      if ((generalRejected || fableRejected) && retryCount < maxRetries) {
        // A Fable-only rejection leaves the account fine for other models, so we
        // do NOT throttle it globally — the recorded Fable utilization makes
        // selection skip it for Fable requests only. A general rejection spends a
        // shared bucket, so hold the whole account for its reset window.
        if (fableRejected) {
          console.log(`[TeamClaude] Fable weekly exhausted on "${account.name}" — switching account for this Fable request`);
        } else {
          const hold = Math.min(Math.max(retryAfter, 1), 3600);
          console.log(`[TeamClaude] Quota rejection (429) on "${account.name}" — throttling ${hold}s and switching account`);
          accountManager.markRateLimited(account.index, hold);
        }
        ctx.tried.add(account.index);
        ctx.everExcluded = (ctx.everExcluded || 0) + 1;
        if (res.destroyed) return;
        return forwardRequest(req, res, body, accountManager, upstream, retryCount + 1, hooks, reqId, ctx, logDir, sx, route);
      }

      retryAfter = Math.min(Math.max(retryAfter, 1), 300);

      // sx.org failover: 429s are IP-based, so retry via the proxy's egress IP.
      // 'always' is already on sx; '429' switches direct→sx now and skips the
      // wait (a fresh IP isn't throttled). Also arm the sticky window for MITM.
      const nextUseSx = !!(sx?.useOn429());
      const switchingToSx = nextUseSx && !route;
      sx?.noteRateLimited(retryAfter);

      // This is a rate-limit 429 (per-minute throttle), NOT quota exhaustion —
      // quota rejection is handled above and is the only thing that rotates.
      // Do NOT switch accounts here: moving the burst to the next account just
      // throttles it too (thundering herd, #84) and discards this account's KV
      // cache. Instead PAUSE this account so concurrent requests wait in admit()
      // (capped, then released through a fresh ramp) instead of piling on, and
      // retry the SAME account. The pause never marks the account throttled, so
      // selection keeps choosing it.
      accountManager.pauseAccount(account.index, Math.min(retryAfter, RATE_LIMIT_ABSORB_MAX_SECONDS));

      // sx fresh-IP retry (still the same account) takes precedence over waiting.
      // Bounded by retryCount like the inline-wait path below, so a persistently
      // 429ing upstream can't loop forever through sx.
      if (switchingToSx && retryCount < maxRetries) {
        console.log(`[TeamClaude] 429 on "${account.name}" — retrying via sx.org (fresh egress IP)`);
        if (res.destroyed) return;
        return forwardRequest(req, res, body, accountManager, upstream, retryCount + 1, hooks, reqId, ctx, logDir, sx, nextUseSx);
      }

      // Absorb short waits inline on the same account — the client never sees the
      // 429. Bounded by retryCount (maxRetries = account count) so a persistently
      // rate-limited account can't loop forever tying up the connection.
      if (retryAfter <= RATE_LIMIT_ABSORB_MAX_SECONDS && retryCount < maxRetries) {
        console.log(`[TeamClaude] Rate-limit 429 on "${account.name}" — waiting ${retryAfter}s, retrying same account (no switch)`);
        await new Promise(resolve => setTimeout(resolve, retryAfter * 1000));
        if (res.destroyed) return;
        return forwardRequest(req, res, body, accountManager, upstream, retryCount + 1, hooks, reqId, ctx, logDir, sx, nextUseSx);
      }

      // Longer retry-after (or retries exhausted): don't hold the connection and
      // don't rotate — surface the 429 with retry-after so the client backs off.
      // The pause above keeps other requests off this account meanwhile.
      console.log(`[TeamClaude] Rate-limit 429 on "${account.name}" — retry-after ${retryAfter}s over inline cap; returning 429 to client (no switch)`);
      ctx.status = 429;
      if (!res.headersSent && !res.destroyed) {
        res.writeHead(429, { 'Content-Type': 'application/json', 'retry-after': String(retryAfter) });
        res.end(JSON.stringify({ type: 'error', error: { type: 'rate_limit_error', message: `Rate limited; retry in ${retryAfter}s.` } }));
      }
      return;
    }

    // A 401 means the credential we injected was rejected. For an OAuth account
    // that usually means the access token was revoked BEFORE its clock expiry —
    // something else refreshed the same token family, so upstream reports it
    // revoked while it still looks fresh locally. ensureTokenFresh's expiry
    // check cannot see that (it only compares the clock), so the account would
    // otherwise keep serving a dead token until the token aged out, and every
    // request in between would surface a 401 to the client with no recovery.
    // Force one refresh and retry. If the refresh is itself rejected the refresh
    // token is dead too: ensureTokenFresh marks the account errored, and the
    // retry's status check rotates to another account. Bounded to one re-auth
    // per account per request, so a genuinely dead credential surfaces the 401
    // instead of looping.
    if (upstreamRes.status === 401 && account.type === 'oauth' && account.refreshToken
        && retryCount < maxRetries && !ctx.reauthed.has(account.index)) {
      ctx.reauthed.add(account.index);
      await upstreamRes.body?.cancel();
      console.log(`[TeamClaude] 401 on "${account.name}" — token rejected; forcing refresh and retrying`);
      await accountManager.ensureTokenFresh(account.index, true);
      if (res.destroyed) return;
      return forwardRequest(req, res, body, accountManager, upstream, retryCount + 1, hooks, reqId, ctx, logDir, sx, route);
    }

    // Log the request head (once) followed by the response headers, streaming
    // to disk from here on.
    logRequestHead();
    getLog()?.write(`\n\n=== RESPONSE ${upstreamRes.status} ===\n${formatHeaders(upstreamRes.headers)}`);

    ctx.status = upstreamRes.status;

    // Build response headers (skip hop-by-hop and encoding headers). The
    // connection-specific names are also illegal on an HTTP/2 response — when
    // this runs behind the MITM's h2 server, writeHead would otherwise throw.
    const responseHeaders = {};
    for (const [key, value] of upstreamRes.headers.entries()) {
      if (CONNECTION_SPECIFIC_HEADERS.has(key)) continue;
      // Strip content-encoding/content-length since fetch may auto-decompress
      if (key === 'content-encoding' || key === 'content-length') continue;
      responseHeaders[key] = value;
    }

    res.writeHead(upstreamRes.status, responseHeaders);

    if (!upstreamRes.body) {
      const l = getLog();
      if (l) { l.write('\n\n=== RESPONSE BODY ===\n(empty)'); l.end(); }
      res.end();
      return;
    }

    const contentType = upstreamRes.headers.get('content-type') || '';
    const isStreaming = contentType.includes('text/event-stream');

    if (isStreaming) {
      // Stream each chunk straight to the log as it is relayed — never hold the
      // whole (potentially ~1M-token) SSE body in memory.
      const l = getLog();
      const bw = l ? l.bodyWriter('RESPONSE BODY (streamed)', contentType) : null;
      await streamResponse(upstreamRes.body, res, account.index, accountManager, bw);
      l?.end();
    } else {
      const buf = Buffer.from(await upstreamRes.arrayBuffer());
      extractUsageFromBody(buf, account.index, accountManager);
      const l = getLog();
      if (l) { l.body('RESPONSE BODY', buf, contentType); l.end(); }
      res.end(buf);
    }
  } catch (err) {
    const errorClass = classifyUpstreamError(err);
    // Timestamped. Without one, 1,939 proxy refusals in a log tail say nothing
    // about whether they happened over a minute or a week — and the answer
    // changes what to do about them. This log had no clock at all; the same
    // omission cost hours on the tunnel line earlier today.
    console.error(
      `[TeamClaude] ${new Date().toISOString()} Upstream error `
      + `(account "${account.name}", ${errorClass}):`,
      err.message,
    );

    logRequestHead();
    const l = getLog();
    if (l) { l.write(`\n\n=== ERROR ===\n${err.stack || err.message}`); l.end(); }

    // Transport faults (stale-socket timeouts, egress/proxy failures, DNS):
    // close the connection and let the client retry. Failing over to another
    // account would not help — the fault is on the path, not the account — and
    // doing so would mark each account tried in turn until the request runs out
    // of accounts and parks for holdSeconds while capacity sits unused. The
    // fast failure also lets Node evict a dead socket so the retry reconnects
    // cleanly. If headers were already sent (a mid-stream body timeout),
    // destroy is the only option — the client sees a broken response and
    // retries.
    // A CONNECT that was REFUSED never opened a tunnel, so nothing reached
    // Anthropic and re-sending is safe. That distinction matters: a mid-stream
    // `socket hang up` may mean the request WAS received, and retrying it
    // would double a billed call. Only the never-opened case is retried.
    //
    // It is also the overwhelming majority. Of 2,134 upstream errors in one
    // log tail, 1,939 were `sx.org proxy refused CONNECT: 503` and 33 more
    // were the local proxy port refusing or timing out — 92% of them faults
    // where the egress hop said no before any bytes moved.
    //
    // The account is deliberately NOT excluded. The fault is on the path, and
    // marking accounts tried in turn would burn through the fleet and park the
    // request while every account sat healthy.
    const neverOpened = err instanceof Error && (
      /proxy refused CONNECT/i.test(String(err.message || ''))
      || /proxy CONNECT timed out/i.test(String(err.message || ''))
      || ((err.code === 'ECONNREFUSED' || err.code === 'ETIMEDOUT')
          && /127\.0\.0\.1|localhost/.test(String(err.message || '')))
    );
    if (errorClass === 'transport' && neverOpened
        && !res.headersSent && (ctx.egressRetries || 0) < EGRESS_RETRY_LIMIT) {
      const attempt = (ctx.egressRetries || 0);
      const delay = EGRESS_RETRY_DELAYS_MS[attempt];
      ctx.egressRetries = attempt + 1;
      console.error(
        `[TeamClaude] ${new Date().toISOString()} egress refused before the `
        + `tunnel opened; retrying the same account in ${delay}ms `
        + `(${ctx.egressRetries}/${EGRESS_RETRY_LIMIT})`,
      );
      await new Promise(resolve => setTimeout(resolve, delay));
      if (res.destroyed) return;
      return forwardRequest(req, res, body, accountManager, upstream,
                            retryCount, hooks, reqId, ctx, logDir, sx, route);
    }

    // Everything else on the transport class: close and let the client retry.
    // Failing over to another account would not help — the fault is on the
    // path, not the account — and the fast failure lets Node evict a dead
    // socket so the retry reconnects cleanly.
    if (errorClass === 'transport') {
      res.destroy();
      return;
    }

    // Anything left is not a recognised transport fault, so it may be specific
    // to this account. Still NOT proof the credentials are bad — a bad
    // credential comes back as a 401 *response*, never a throw — so don't
    // sideline the account durably (that would drop a healthy account from
    // rotation until a credential change). Skip it for the rest of THIS request
    // only and fail over.
    if (retryCount < maxRetries && !res.headersSent) {
      console.error(
        `[TeamClaude] Excluding "${account.name}" from this request `
        + `(unclassified upstream error) and failing over `
        + `(attempt ${retryCount + 1}/${maxRetries})`,
      );
      ctx.tried.add(account.index);
      ctx.everExcluded = (ctx.everExcluded || 0) + 1;
      return forwardRequest(req, res, body, accountManager, upstream, retryCount + 1, hooks, reqId, ctx, logDir, sx, route);
    }
    ctx.status = 502;

    if (!res.headersSent) {
      res.writeHead(502, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        type: 'error',
        error: { type: 'proxy_error', message: `Upstream error: ${err.message}` },
      }));
    } else if (!res.writableEnded) {
      // Error after headers were already sent (mid-stream) and it wasn't
      // classified transient: we can't send a status or fail over, and
      // streamResponse deliberately skipped res.end(). Destroy so the client
      // sees a broken response and retries instead of hanging on an open socket.
      res.destroy();
    }
  }
}

// Idle deadline for the RESPONSE BODY, complementing the headers timeout in
// upstream-fetch.js. The headers guard only covers time-to-first-byte; once
// headers arrive it is disarmed, so a network drop AFTER the stream starts would
// Optional operator watchdogs. They are disabled by default: Fleet must not
// turn a slow or temporarily silent Claude stream into a local 502. When
// explicitly configured, the body watchdog resets on every chunk and cancels
// the reader after the selected idle window.
const DEFAULT_BODY_IDLE_TIMEOUT_MS = 0;
const DEFAULT_FIRST_BODY_TIMEOUT_MS = 0;

function resolveBodyIdleTimeout() {
  const env = Number(process.env.TEAMCLAUDE_UPSTREAM_BODY_TIMEOUT_MS);
  return env > 0 ? env : DEFAULT_BODY_IDLE_TIMEOUT_MS;
}

function resolveFirstBodyTimeout() {
  const env = Number(process.env.TEAMCLAUDE_UPSTREAM_FIRST_BODY_TIMEOUT_MS);
  return env > 0 ? env : DEFAULT_FIRST_BODY_TIMEOUT_MS;
}

// Race a single reader.read() against an inactivity deadline. Resolves to the
// read result, or rejects with a transient TEAMCLAUDE_BODY_TIMEOUT if no chunk
// arrives within `ms`. The pending read is abandoned on timeout; the caller
// cancels the reader (evicting the socket) in its finally block.
export function readWithIdleTimeout(reader, ms, phase = 'stream') {
  if (ms <= 0) return reader.read();
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      const err = new Error(`upstream ${phase} idle for ${ms}ms`);
      err.code = 'TEAMCLAUDE_BODY_TIMEOUT';
      reject(err);
    }, ms);
    timer.unref?.();
  });
  const read = reader.read();
  // If the timeout wins the race, `read` is abandoned; swallow any later
  // rejection so it can't surface as an unhandledRejection.
  read.catch(() => {});
  return Promise.race([read, timeout]).finally(() => clearTimeout(timer));
}

/**
 * Stream an SSE response to the client, parsing usage data along the way.
 */
async function streamResponse(webStream, res, accountIndex, accountManager, bodyWriter) {
  const reader = webStream.getReader();
  const idleMs = resolveBodyIdleTimeout();
  const firstBodyMs = resolveFirstBodyTimeout();
  const decoder = new TextDecoder();
  let sseBuffer = '';
  let errored = false;
  let receivedChunk = false;

  try {
    while (true) {
      const { done, value } = await readWithIdleTimeout(
        reader,
        receivedChunk ? idleMs : firstBodyMs,
        receivedChunk ? 'stream' : 'first SSE chunk',
      );
      if (done) break;
      receivedChunk = true;

      // Client disconnected — stop reading from upstream
      if (res.destroyed) break;

      // Forward chunk immediately
      const ok = res.write(value);

      // Append to the log as it streams (no whole-body buffering)
      if (bodyWriter) bodyWriter.chunk(Buffer.from(value));

      const text = decoder.decode(value, { stream: true });

      // Parse SSE events for usage tracking
      sseBuffer += text;
      const events = sseBuffer.split('\n\n');
      sseBuffer = events.pop(); // keep incomplete event

      for (const event of events) {
        parseSSEUsage(event, accountIndex, accountManager);
      }

      // Handle backpressure — also bail out if client disconnects,
      // because 'drain' will never fire on a destroyed socket
      if (!ok) {
        await new Promise(resolve => {
          // Remove BOTH listeners when either fires: otherwise the un-fired one
          // (usually 'close') stays attached and accumulates one leaked listener
          // per backpressure cycle over a long SSE stream to a slow client.
          const done = () => { res.off('drain', done); res.off('close', done); resolve(); };
          res.once('drain', done);
          res.once('close', done);
        });
        if (res.destroyed) break;
      }
    }

    // Parse any remaining buffer
    if (sseBuffer.trim()) {
      parseSSEUsage(sseBuffer, accountIndex, accountManager);
    }
  } catch (err) {
    // A mid-stream idle timeout (or any read error) means the upstream went
    // silent after headers. Rethrow to the caller's transient handler, which
    // destroys the client connection so the truncated stream is NOT ended
    // cleanly (a clean res.end() would look like a complete response and
    // suppress the client's retry). reader.cancel() in finally evicts the socket.
    errored = true;
    throw err;
  } finally {
    // Cancel upstream reader to stop consuming data nobody needs (and, on the
    // timeout path, to destroy the dead socket so the pool drops it).
    reader.cancel().catch(() => {});
    if (!errored && !res.writableEnded) res.end();
  }
}

function parseSSEUsage(event, accountIndex, accountManager) {
  const dataLine = event.split('\n').find(l => l.startsWith('data: '));
  if (!dataLine) return;

  try {
    const data = JSON.parse(dataLine.slice(6));
    if (data.type === 'message_start' && data.message?.usage) {
      accountManager.updateUsage(accountIndex, data.message.usage.input_tokens, 0);
    } else if (data.type === 'message_delta' && data.usage) {
      accountManager.updateUsage(accountIndex, 0, data.usage.output_tokens);
    }
  } catch {
    // not valid JSON, skip
  }
}

function extractUsageFromBody(buffer, accountIndex, accountManager) {
  try {
    const json = JSON.parse(buffer.toString());
    if (json.usage) {
      accountManager.updateUsage(accountIndex, json.usage.input_tokens, json.usage.output_tokens);
    }
  } catch {
    // not JSON or no usage
  }
}

// Rewrite the `model` field in a JSON request body using a per-account map.
// Returns the original buffer unchanged if the model isn't in the map or the
// body isn't valid JSON, so non-messages endpoints pass through safely.
// Exported for tests.
export function rewriteModel(body, modelMap) {
  try {
    const obj = JSON.parse(body.toString('utf8'));
    if (obj.model && modelMap[obj.model]) {
      obj.model = modelMap[obj.model];
      return Buffer.from(JSON.stringify(obj), 'utf8');
    }
  } catch { /* not JSON — pass through unchanged */ }
  return body;
}

// Seconds until the fleet could plausibly serve again.
//
// This used to read only `rateLimitedUntil || quota.resetsAt`. For an
// OAuth-only fleet `resetsAt` is a standard-quota field and is null on every
// account, so the function always fell through to its 60-second default — the
// terminal 429 advertised "retry in 60s" when the real wait was the next weekly
// reset, often more than a day out. That single wrong number is most of the
// difference between a fleet that looks broken and one that is visibly parked.
//
// All windowed buckets are considered, since recovering ANY of them can make an
// account selectable again.
export function computeRetryAfter(accounts) {
  const now = Date.now();
  let soonest = Infinity;
  const consider = (value) => {
    if (!value) return;
    const ms = new Date(value).getTime() - now;
    if (Number.isFinite(ms) && ms > 0 && ms < soonest) soonest = ms;
  };

  for (const acct of accounts) {
    if (acct?.disabled) continue;
    consider(acct?.rateLimitedUntil);
    const q = acct?.quota || {};
    consider(q.resetsAt);
    consider(q.unified5hReset);
    consider(q.unified7dReset);
    consider(q.unified7dSonnetReset);
    consider(q.unified7dFableReset);
  }
  return soonest === Infinity ? 60 : Math.max(1, Math.ceil(soonest / 1000));
}
