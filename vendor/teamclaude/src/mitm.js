// MITM forward-proxy support: local cert lifecycle + terminating CONNECT proxy.
//
// When a claude instance is launched with HTTPS_PROXY pointed at teamclaude it
// sends `CONNECT api.anthropic.com:443`. Rather than byte-relaying the tunnel, we
// TERMINATE it with a real Node HTTP/2 server (allowHTTP1, so an h1 client works
// too) presenting our locally-minted leaf, then forward each request with a
// buffering, retrying client — the SAME path the base proxy uses
// (createProxyRequestListener). That gives per-request account selection, body
// account_uuid rewriting, and — critically — the ability to resend a request on a
// different account when one returns a quota 429, instead of surfacing it. A host
// routing table decides per-CONNECT behavior:
//   api.anthropic.com → terminate + forward,  www.example.org → local test server,
//   anything else      → blind tunnel.

import { execFile } from 'node:child_process';
import { readFile, writeFile, mkdir, rename } from 'node:fs/promises';
import { X509Certificate } from 'node:crypto';
import { dirname, join } from 'node:path';
import net from 'node:net';
import tls from 'node:tls';
import http2 from 'node:http2';
import { getConfigPath } from './config.js';
import { generateCertChain } from './x509.js';
import { createProxyRequestListener, safeKeyEqual, isLoopbackAddr, relayUpgrade, resolveAccountPin } from './server.js';

const CA_CERT = 'teamclaude-ca.pem';
const LEAF_CERT = 'teamclaude-leaf.pem';
const LEAF_KEY = 'teamclaude-leaf.key';

// A built-in host the MITM proxy always intercepts and answers itself (never
// forwarded upstream). Lets you verify the proxy + CA end-to-end with no
// credentials, e.g.:
//   curl --proxy http://localhost:3456 --cacert <ca.pem> https://www.example.org/
export const TEST_HOST = 'www.example.org';

const certDir = () => dirname(getConfigPath());
const fpath = (n) => join(certDir(), n);

// ── outbound proxy for blind tunnels ────────────────────────────────────────
// The data plane is started with HTTPS_PROXY set from runtime.json's
// outbound_proxy, but that only reaches Node's fetch. CONNECT tunnels are raw
// sockets and need this explicitly.

export function outboundProxyEndpoint() {
  const raw = process.env.HTTPS_PROXY || process.env.https_proxy
    || process.env.HTTP_PROXY || process.env.http_proxy || '';
  if (!raw) return null;
  try {
    const url = new URL(raw);
    const port = parseInt(url.port, 10) || (url.protocol === 'https:' ? 443 : 80);
    if (!url.hostname) return null;
    return { host: url.hostname, port, auth: url.username ? `${decodeURIComponent(url.username)}:${decodeURIComponent(url.password || '')}` : null };
  } catch { return null; }
}

// Honour NO_PROXY so loopback and internal ranges keep going direct — sending
// 127.0.0.1 through an egress proxy would break the local test server path.
export function isNoProxyHost(host) {
  const raw = process.env.NO_PROXY || process.env.no_proxy || '';
  if (!raw) return false;
  const target = String(host || '').toLowerCase();
  for (const entry of raw.split(',')) {
    const rule = entry.trim().toLowerCase();
    if (!rule) continue;
    if (rule === '*') return true;
    if (rule.includes('/')) continue;  // CIDR: not matched by name
    if (target === rule) return true;
    if (rule.startsWith('.') && target.endsWith(rule)) return true;
    if (target.endsWith(`.${rule}`)) return true;
  }
  return false;
}

// Open a CONNECT tunnel through an HTTP proxy. Returns the socket immediately;
// onReady fires once the proxy answers 200, onError on any failure.
function connectViaProxy(proxy, host, port, { onReady, onError }) {
  const socket = net.connect(proxy.port, proxy.host);
  let buffer = '';
  let settled = false;
  const fail = (err) => {
    if (settled) return;
    settled = true;
    socket.removeListener('data', onData);
    onError(err);
  };
  const onData = (chunk) => {
    if (settled) return;
    buffer += chunk.toString('latin1');
    const end = buffer.indexOf('\r\n\r\n');
    if (end < 0) {
      if (buffer.length > 65536) fail(new Error('CONNECT response too large'));
      return;
    }
    const statusLine = buffer.slice(0, buffer.indexOf('\r\n'));
    const match = statusLine.match(/^HTTP\/\d\.\d\s+(\d{3})/);
    if (!match || match[1] !== '200') { fail(new Error(`proxy refused CONNECT: ${statusLine}`)); return; }
    settled = true;
    socket.removeListener('data', onData);
    // Hand back any bytes the proxy already sent past the header.
    const rest = Buffer.from(buffer.slice(end + 4), 'latin1');
    if (rest.length) socket.unshift(rest);
    onReady();
  };
  socket.once('connect', () => {
    const lines = [
      `CONNECT ${host}:${port} HTTP/1.1`,
      `Host: ${host}:${port}`,
    ];
    if (proxy.auth) lines.push(`Proxy-Authorization: Basic ${Buffer.from(proxy.auth).toString('base64')}`);
    lines.push('Proxy-Connection: keep-alive', '', '');
    socket.write(lines.join('\r\n'));
    socket.on('data', onData);
  });
  socket.once('error', fail);
  return socket;
}

/** Path to the CA cert clients should trust via NODE_EXTRA_CA_CERTS. */
export function caCertPath() {
  return fpath(CA_CERT);
}

async function readIf(p) {
  try { return await readFile(p, 'utf8'); } catch { return null; }
}

async function atomicWrite(path, data, mode) {
  const tmp = `${path}.tmp${process.pid}`;
  await writeFile(tmp, data, { mode });
  await rename(tmp, path);
}

// Is the stored leaf signed by the stored CA and valid for every host in `hosts`?
function leafCovers(caCertPem, leafCertPem, hosts) {
  try {
    const ca = new X509Certificate(caCertPem);
    const leaf = new X509Certificate(leafCertPem);
    if (!leaf.verify(ca.publicKey)) return false;
    const names = (leaf.subjectAltName || '').split(',').map((s) => s.trim());
    return hosts.every((h) => names.includes(`DNS:${h}`));
  } catch {
    return false;
  }
}

/**
 * Ensure a CA cert + a leaf for `host` exist in the config dir, generating them
 * if missing/mismatched. The CA *private* key is never persisted — we regenerate
 * the whole chain when needed, so the only on-disk secret is the leaf key (0600),
 * which only authenticates as `host` to a process that already trusts our CA.
 * Returns { caPath, caCertPem, leafCertPem, leafKeyPem }.
 */
export async function ensureCerts(host) {
  const hosts = host === TEST_HOST ? [TEST_HOST] : [host, TEST_HOST];
  const [caCertPem, leafCertPem, leafKeyPem] = await Promise.all([
    readIf(fpath(CA_CERT)), readIf(fpath(LEAF_CERT)), readIf(fpath(LEAF_KEY)),
  ]);

  if (caCertPem && leafCertPem && leafKeyPem && leafCovers(caCertPem, leafCertPem, hosts)) {
    return { caPath: fpath(CA_CERT), caCertPem, leafCertPem, leafKeyPem };
  }

  const chain = generateCertChain(hosts); // caKeyPem intentionally discarded
  await mkdir(certDir(), { recursive: true });
  await atomicWrite(fpath(CA_CERT), chain.caCertPem, 0o644);
  await atomicWrite(fpath(LEAF_CERT), chain.leafCertPem, 0o644);
  await atomicWrite(fpath(LEAF_KEY), chain.leafKeyPem, 0o600);
  return {
    caPath: fpath(CA_CERT),
    caCertPem: chain.caCertPem,
    leafCertPem: chain.leafCertPem,
    leafKeyPem: chain.leafKeyPem,
  };
}

function upstreamHostOf(config) {
  try { return new URL(config?.upstream || 'https://api.anthropic.com').hostname; }
  catch { return 'api.anthropic.com'; }
}

/** Per-CONNECT behavior: 'rewrite' (intercept + token inject), 'test', or 'tunnel'. */
export function hostMode(host, config) {
  if (host === TEST_HOST) return 'test';
  if (host === upstreamHostOf(config)) return 'rewrite';
  return 'tunnel';
}

/**
 * Build a `connect` event handler implementing the terminating MITM described at
 * the top of this file.
 * @param ensureLeaf async () => { key, cert }   // current leaf PEMs
 */
export function createConnectHandler({ config, accountManager, ensureLeaf, logDir = null, hooks = {}, log = () => {}, sx = null }) {
  const upstream = config.upstream || 'https://api.anthropic.com';
  const proxyApiKey = config.proxy?.apiKey;
  const holdMs = (config.holdSeconds || 0) * 1000;

  // One terminating h2/h1 server per pin, minted lazily on the first intercepted
  // CONNECT that needs it (key '' = unpinned, the common case).
  // TLS uses our leaf; ALPN negotiates h2 or http/1.1 (allowHTTP1) with whatever
  // the client offers. It emits 'request' for BOTH protocols, so `forward` — the
  // shared buffering/retrying proxy listener — handles them identically. Each
  // CONNECT feeds it the raw tunnel socket; the client keeps the tunnel open and
  // multiplexes many requests over it, each independently account-selected.
  //
  // Keying by pin is what carries a TC_ACCT pin from the CONNECT to the requests
  // inside the tunnel. The alternative — tagging the raw socket and reading it
  // back from the request — means digging through a TLSSocket and, under h2, a
  // Proxy over the session socket. A listener bound to the account is the same
  // information with none of that. The map is bounded by the account count.
  const serverPromises = new Map();
  const buildServer = async (pin = '', transportHolder = null) => {
    const { key, cert } = await ensureLeaf();
    const srv = http2.createSecureServer({ key, cert, allowHTTP1: true });
    srv.on('request', createProxyRequestListener({
      accountManager,
      upstream,
      logDir,
      hooks,
      sx,
      holdMs,
      config,
      forcedPin: pin || null,
      transportHolder,
    }));
    // Remote Control's real-time channel is a WebSocket (Upgrade handshake),
    // which never fires 'request' — only 'upgrade', with a raw socket instead
    // of a response object (h1-only; falls back to blind h2 passthrough is not
    // needed since WS clients negotiate h1 for the handshake).
    srv.on('upgrade', (req, socket, head) => relayUpgrade(req, socket, head, upstream, sx));
    srv.on('sessionError', (e) => log(`[TeamClaude] MITM session error: ${e.message}`));
    srv.on('clientError', (e, sock) => { try { sock.destroy(); } catch { /* already gone */ } });
    return srv;
  };
  const getServer = (pin = '', transportHolder = null) => {
    // A transport holder is scoped to one authenticated loopback CONNECT. Do
    // not put it in the long-lived pin cache: that would retain launch
    // identities forever and could attach one connection's identity to another.
    if (transportHolder) return buildServer(pin, transportHolder);
    let p = serverPromises.get(pin);
    if (p) return p;
    p = buildServer(pin, null).catch((err) => {
      // Don't let a transient cert/disk failure poison the memo forever: drop it
      // so the next intercepted CONNECT retries instead of re-awaiting a cached
      // rejection (which would leave the MITM path dead until a restart).
      serverPromises.delete(pin);
      throw err;
    });
    serverPromises.set(pin, p);
    return p;
  };

  return (req, clientSocket, head) => {
    clientSocket.on('error', () => {});

    // Auth gate — mirror the HTTP path: loopback is exempt, everything else must
    // present the proxy apiKey via Proxy-Authorization. Without this, a remote
    // client can CONNECT api.anthropic.com and have a rotated ACCOUNT TOKEN
    // injected (token theft), or blind-tunnel to arbitrary hosts (open relay /
    // SSRF) — the HTTP path already blocks the equivalent for remote clients.
    if (!connectAuthorized(req, clientSocket, proxyApiKey)) {
      try {
        clientSocket.write('HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="teamclaude"\r\nConnection: close\r\n\r\n');
      } catch { /* client already gone */ }
      clientSocket.destroy();
      return;
    }

    const [host, portStr] = (req.url || '').split(':');
    const port = parseInt(portStr, 10) || 443;
    const mode = hostMode(host, config);

    if (mode === 'tunnel') {
      // Until the upstream connects we still owe the client a CONNECT status
      // line. If we tore the socket down on an upstream failure without one,
      // the client reports "Proxy connection ended before receiving CONNECT
      // response" — so before the tunnel is live, surface failures as a real
      // proxy error status instead of a silent drop.
      let established = false, closed = false;
      // Tear down BOTH sockets when either errors or closes, so a one-sided
      // failure can't leave the paired socket lingering (FD leak). The `closed`
      // guard makes it idempotent (error+close both fire) and ensures we write
      // at most one status line.
      const teardown = (statusLine) => {
        if (closed) return;
        closed = true;
        if (!established && statusLine) {
          try { clientSocket.write(`HTTP/1.1 ${statusLine}\r\nConnection: close\r\n\r\n`); } catch { /* client already gone */ }
        }
        up.destroy(); clientSocket.destroy();
      };
      // Blind tunnels used to be a bare `net.connect(port, host)` — raw TCP
      // that ignores HTTPS_PROXY entirely. The service IS started with
      // HTTPS_PROXY=<outbound_proxy>, but that only covers Node's fetch, never
      // this socket. So every host other than api.anthropic.com left the data
      // plane as a DIRECT connection.
      //
      // That silently broke `claude setup-token`: the browser step succeeded
      // (it uses the system proxy), then the code exchange to
      // platform.claude.com came through here and hung. Measured from this Mac:
      //
      //   direct                   HTTP 000, timed out at 25s
      //   via the outbound proxy   HTTP 400 in 1.1s
      //
      // Anything Anthropic-related must use the configured egress, not just
      // model requests. When no outbound proxy is set this is exactly the old
      // direct connect, so hosts that need none are unaffected.
      const upstreamProxy = outboundProxyEndpoint();
      const up = upstreamProxy && !isNoProxyHost(host)
        ? connectViaProxy(upstreamProxy, host, port, {
            onReady: () => {
              established = true;
              reply200Raw(clientSocket);
              if (head && head.length) up.write(head);
              up.pipe(clientSocket); clientSocket.pipe(up);
            },
            onError: (err) => {
              log(`[TeamClaude] tunnel ${host}:${port} via proxy failed: ${err.message}`);
              teardown('502 Bad Gateway');
            },
          })
        : net.connect(port, host, () => {
            established = true;
            reply200Raw(clientSocket);
            if (head && head.length) up.write(head);
            up.pipe(clientSocket); clientSocket.pipe(up);
          });
      up.on('error', (err) => {
        // `err.message` alone can be empty — it was, for the failure that
        // mattered — and the message is not the useful part anyway. What
        // identifies a dial failure is the code, the syscall, and above all
        // WHICH ADDRESS was dialled.
        //
        // AliDNS returns a poisoned answer for platform.claude.com on the
        // server: 103.252.115.221 and a 2001::/32 Teredo address, neither of
        // which accepts a connection. It does not poison api.anthropic.com, so
        // the fleet kept serving while every new Claude process that touched
        // platform.claude.com died with ERR_SOCKET_CLOSED. The log said
        // `tunnel platform.claude.com:443 failed:` and stopped there — the one
        // fact that would have named the cause in a second, the address, was
        // the fact it omitted.
        if (!established) {
          // Address first, because it is the fact that names the cause.
          // `err.message` is only included when there is no code — otherwise
          // it just repeats the code and syscall back, and a log line that
          // says `ENOTFOUND getaddrinfo getaddrinfo ENOTFOUND host` is the
          // same unreadability in the other direction.
          const detail = [
            err.address ? `addr=${err.address}` : null,
            err.code || null,
            err.syscall || null,
            err.code ? null : (err.message || null),
          ].filter(Boolean).join(' ') || 'no detail';
          log(`[TeamClaude] tunnel ${host}:${port} failed: ${detail}`);
        }
        teardown('502 Bad Gateway');
      });
      // A FIN before the tunnel is live (no preceding 'error') is still a failed
      // dial from the client's view — surface a 502 rather than a silent drop.
      up.on('close', () => teardown('502 Bad Gateway'));
      clientSocket.on('close', () => teardown()); // client gone: nothing to write
      up.setTimeout(30_000, () => {
        // A stalled dial looks identical to a refused one from the client, and
        // a poisoned address produces exactly this: SYN out, nothing back.
        if (!established) {
          log(`[TeamClaude] tunnel ${host}:${port} timed out after 30s `
              + '(no response from the address it resolved to)');
        }
        teardown('504 Gateway Timeout');
      });
      return;
    }

    if (mode === 'test') {
      // The built-in test host is answered locally, never forwarded upstream.
      ensureLeaf().then(({ key, cert }) => {
        reply200Raw(clientSocket);
        serveTest(termClaude(clientSocket, head, key, cert, ['http/1.1']));
      }).catch((err) => { log(`[TeamClaude] MITM ${host}: ${err.message}`); reply502Raw(clientSocket); clientSocket.destroy(); });
      return;
    }

    // rewrite: terminate the tunnel and forward each request with buffering +
    // retry. Reply 200, hand the raw socket (ClientHello and all) to the h2/h1
    // server, which does TLS + protocol negotiation itself. If the terminating
    // server can't be minted (cert/disk/TLS-init failure) we haven't replied yet
    // — send a 502 so the client sees a real proxy error instead of "Proxy
    // connection ended before receiving CONNECT response".
    // Pin resolution is deliberately confined to `rewrite`. Clients send
    // Proxy-Authorization on EVERY CONNECT, including blind-tunneled third-party
    // hosts, where an account pin is meaningless — rejecting there would take
    // down unrelated traffic over a typo meant for Anthropic.
    const {
      pin,
      transportHolder,
      error,
    } = resolveConnectIdentity(
      req,
      clientSocket,
      accountManager,
      proxyApiKey,
    );
    if (error) {
      log(`[TeamClaude] CONNECT ${host}: ${error}`);
      try {
        clientSocket.write(`HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="teamclaude"\r\nConnection: close\r\n\r\n`);
      } catch { /* client already gone */ }
      clientSocket.destroy();
      return;
    }

    getServer(pin || '', transportHolder).then((srv) => {
      reply200Raw(clientSocket);
      if (head && head.length) clientSocket.unshift(head);
      srv.emit('connection', clientSocket);
    }).catch((err) => { log(`[TeamClaude] MITM ${host}: ${err.message}`); reply502Raw(clientSocket); clientSocket.destroy(); });
  };
}

// The Basic username from a CONNECT's `Proxy-Authorization`, or null. This is
// the only pin channel expressible in an HTTPS_PROXY URL, which is what
// `teamclaude run` has to work with in MITM mode (there is no request path to
// carry a `/tc-acct/` prefix — inside the tunnel the path is the real upstream
// one). Clients send this preemptively on every CONNECT.
export function connectPinToken(req) {
  const header = (req?.headers?.['proxy-authorization'] || '').trim();
  if (!header.toLowerCase().startsWith('basic ')) return null;
  const dec = Buffer.from(header.slice('basic '.length).trim(), 'base64').toString('utf8'); // "user:pass"
  const colon = dec.indexOf(':');
  return (colon >= 0 ? dec.slice(0, colon) : dec) || null;
}

export const FLEET_TRANSPORT_USER_PREFIX = 'fleet-holder-v1.';
const FLEET_HOLDER_RE = /^[A-Za-z0-9._:-]{1,256}$/;

export function decodeFleetTransportHolderToken(token) {
  if (!token?.startsWith(FLEET_TRANSPORT_USER_PREFIX)) return null;
  const encoded = token.slice(FLEET_TRANSPORT_USER_PREFIX.length);
  if (!encoded || !/^[A-Za-z0-9_-]+$/.test(encoded)) {
    throw new Error('Invalid Fleet transport identity');
  }
  const holder = Buffer.from(encoded, 'base64url').toString('utf8');
  const canonical = Buffer.from(holder, 'utf8').toString('base64url');
  if (canonical !== encoded || !FLEET_HOLDER_RE.test(holder)) {
    throw new Error('Invalid Fleet transport identity');
  }
  return holder;
}

/**
 * Resolve account pin and local transport identity from CONNECT userinfo.
 *
 * The reserved Fleet form is never an account pin or credential. It is only
 * accepted on a loopback socket, and remains inside this CONNECT's terminating
 * server closure. Authenticated remote clients cannot assert one.
 */
export function resolveConnectIdentity(req, socket, accountManager, proxyApiKey) {
  const token = connectPinToken(req);
  if (!token) return { pin: null, transportHolder: null, error: null };
  if (token.startsWith(FLEET_TRANSPORT_USER_PREFIX)) {
    if (!isLoopbackAddr(socket?.remoteAddress)) {
      return {
        pin: null,
        transportHolder: null,
        error: 'Fleet transport identity requires loopback',
      };
    }
    try {
      return {
        pin: null,
        transportHolder: decodeFleetTransportHolderToken(token),
        error: null,
      };
    } catch {
      return {
        pin: null,
        transportHolder: null,
        error: 'Invalid Fleet transport identity',
      };
    }
  }
  if (proxyApiKey && safeKeyEqual(token, proxyApiKey)) {
    return { pin: null, transportHolder: null, error: null };
  }
  if (resolveAccountPin(accountManager, token) == null) {
    return {
      pin: null,
      transportHolder: null,
      error: `Unknown account pin "${token}"`,
    };
  }
  return { pin: token, transportHolder: null, error: null };
}

/**
 * Resolve the account pin on a CONNECT, or a rejection reason.
 *
 * The username slot is overloaded: the documented remote form is
 * `--proxy http://<key>@host:port`, where it holds the proxy apiKey, not an
 * account. So the key wins over any account of the same name — an operator who
 * names an account after their proxy key gets auth, not a surprise pin.
 *
 * An unrecognized username is an ERROR rather than a silently ignored pin: a
 * typo'd account name that quietly served from the wrong account is exactly the
 * failure mode this feature exists to remove.
 *
 * @returns {{pin: string|null, error: string|null}}
 */
export function resolveConnectPin(req, accountManager, proxyApiKey) {
  const { pin, error } = resolveConnectIdentity(
    req,
    { remoteAddress: '127.0.0.1' },
    accountManager,
    proxyApiKey,
  );
  return { pin, error };
}

// Loopback was being used as an authentication boundary, and it is not one.
//
// Binding to 127.0.0.1 keeps the network out. It does not distinguish Claude
// Code from a browser extension, a build script, or a malicious transitive npm
// dependency — all of which run as the same user and can all open a socket to
// 127.0.0.1:13456. Any of them could CONNECT api.anthropic.com and have a real
// OAuth token injected into their request, spending a subscription with no
// record that it was not Fleet.
//
// Production made this worse than the code suggests: teamclaude.json carries
// `proxy: {port, host}` with no apiKey at all, so the first line used to be
// `if (!proxyApiKey) return true` and nothing below it ever ran.
//
// There IS already an identity on the wire. `launcher.py` gives every Claude it
// starts a CONNECT userinfo of `fleet-holder-v1.<base64>`, and a `TC_ACCT` pin
// carries an account id. So the discriminator is not "is it loopback" but
// "did it say who it is" — and the only shape that says nothing at all is the
// one this was letting through.
//
// FLEET_AUTH_MODE controls what happens to that shape:
//
//   audit    (default) allow it, and count it — nothing changes for anyone
//   enforce  refuse it
//
// Audit first because the cost of being wrong is asymmetric. If some legitimate
// path connects without an identity — an Agent View worker that strips headers,
// a nested `claude -p`, something not yet thought of — enforcing blind would
// take the fleet down, and the previous outage in this system started with a
// change that looked equally safe. The counter answers it with evidence
// instead: flip only after a long enough window reads zero.
export function authMode() {
  return process.env.FLEET_AUTH_MODE === 'enforce' ? 'enforce' : 'audit';
}

// What enforcing WOULD have refused. Exposed on /teamclaude/ready so the
// decision to flip is made from a number, not a feeling.
export const authAudit = {
  anonymousLoopbackConnects: 0,
  firstAt: null,
  lastAt: null,
  samples: [],          // bounded; enough to identify a caller, not a log
};

// Who opened this socket?
//
// A count is not enough to act on. The audit found two anonymous connections an
// hour on the server, and ruling out suspects one at a time — the auth probe,
// the nine-day-old session, every scheduled job — took an evening and still did
// not name the caller. On loopback the answer is right there: the peer port
// identifies the process.
//
// Only ever runs for a connection that is about to be counted, which is rare by
// construction (a busy fleet produces none at all), so the cost of shelling out
// does not reach the request path anyone cares about. Fails silently to `null`:
// a missing name is worse than a count, and an exception here would be worse
// than both.
function identifyPeer(port, onFound) {
  if (!port) return;
  execFile('lsof', ['-nP', '-iTCP:' + port, '-sTCP:ESTABLISHED', '-Fcn'],
    { timeout: 3000 }, (err, stdout) => {
      if (err || !stdout) return;
      // -F output is one field per line, prefixed by its type: p<pid>, c<command>.
      let pid = null, command = null;
      for (const line of stdout.split('\n')) {
        if (line.startsWith('p')) pid = line.slice(1);
        else if (line.startsWith('c')) command = line.slice(1);
      }
      if (command) onFound({ pid, command });
    });
}

function noteAnonymous(remote, path, port) {
  authAudit.anonymousLoopbackConnects += 1;
  const now = Date.now();
  authAudit.firstAt ??= now;
  authAudit.lastAt = now;
  if (authAudit.samples.length < 20) {
    const sample = { at: now, remote: remote || '?', target: path || '?',
                     peer: null };
    authAudit.samples.push(sample);
    // Filled in a moment later. The sample object is already in the array, so
    // the answer lands wherever it is read from next.
    identifyPeer(port, found => {
      sample.peer = found.command + (found.pid ? ' (pid ' + found.pid + ')' : '');
      console.error(`[ClaudeFleet] anonymous loopback CONNECT came from `
        + `${sample.peer} → ${sample.target}`);
    });
  }
  if (authAudit.anonymousLoopbackConnects <= 5
      || authAudit.anonymousLoopbackConnects % 100 === 0) {
    console.error(
      `[ClaudeFleet] anonymous loopback CONNECT to ${path || '?'} `
      + `(#${authAudit.anonymousLoopbackConnects}). FLEET_AUTH_MODE=${authMode()}; `
      + 'under enforce this would be refused.');
  }
}

/**
 * Decide a CONNECT, and say why. Exported for tests and for the audit surface.
 *
 * `allowed`  — let it through now
 * `identity` — what it presented: proxy-key | fleet | pin | none
 * `wouldRefuseUnderEnforce` — true only for the shape being measured
 */
export function connectVerdict(req, socket, proxyApiKey) {
  const remote = socket?.remoteAddress;
  const loopback = isLoopbackAddr(remote);

  if (proxyApiKey) {
    const m = /^\s*(basic|bearer)\s+(.+?)\s*$/i.exec(
      req?.headers?.['proxy-authorization'] || '');
    if (m) {
      let presented = m[2];
      if (m[1].toLowerCase() === 'basic') {
        const dec = Buffer.from(m[2], 'base64').toString('utf8'); // "user:pass"
        const i = dec.indexOf(':');
        const user = i >= 0 ? dec.slice(0, i) : dec;
        const pass = i >= 0 ? dec.slice(i + 1) : '';
        presented = pass || user;
      }
      if (safeKeyEqual(presented, proxyApiKey)) {
        return { allowed: true, identity: 'proxy-key', wouldRefuseUnderEnforce: false };
      }
    }
  }

  if (!loopback) {
    // Unchanged: a remote client without the key never gets in. The server
    // binds loopback anyway, so this is defence in depth.
    return { allowed: false, identity: 'none', wouldRefuseUnderEnforce: false };
  }

  const token = connectPinToken(req);
  if (token) {
    // Either a Fleet transport holder or an explicit account pin. Both name
    // themselves; `resolveConnectIdentity` validates which, further down.
    const fleet = token.startsWith(FLEET_TRANSPORT_USER_PREFIX);
    return {
      allowed: true,
      identity: fleet ? 'fleet' : 'pin',
      wouldRefuseUnderEnforce: false,
    };
  }

  // The gap. Loopback, no proxy key, no identity of any kind.
  return {
    allowed: authMode() !== 'enforce',
    identity: 'none',
    wouldRefuseUnderEnforce: true,
  };
}

// Kept as the call-site shape, now with the audit side effect attached.
export function connectAuthorized(req, socket, proxyApiKey) {
  const verdict = connectVerdict(req, socket, proxyApiKey);
  if (verdict.wouldRefuseUnderEnforce) {
    noteAnonymous(socket?.remoteAddress, req?.url, socket?.remotePort);
  }
  return verdict.allowed;
}

function reply200Raw(sock) { sock.write('HTTP/1.1 200 Connection Established\r\n\r\n'); }
function reply502Raw(sock) { try { sock.write('HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n'); } catch { /* client already gone */ } }

function termClaude(clientSocket, head, key, cert, alpn) {
  if (head && head.length) clientSocket.unshift(head);
  const t = new tls.TLSSocket(clientSocket, { isServer: true, key, cert, ALPNProtocols: alpn });
  t.on('error', () => t.destroy());
  return t;
}

// Answer the built-in test host locally over h1 with a canned JSON response.
function serveTest(tlsSock) {
  let buf = Buffer.alloc(0);
  const onData = (chunk) => {
    buf = Buffer.concat([buf, chunk]);
    const idx = buf.indexOf('\r\n\r\n');
    if (idx < 0) { if (buf.length > 65536) tlsSock.destroy(); return; }
    tlsSock.removeListener('data', onData);
    const reqLine = buf.subarray(0, buf.indexOf('\r\n')).toString('latin1');
    const path = reqLine.split(' ')[1] || '/';
    const body = JSON.stringify({ teamclaude: 'mitm-proxy-ok', host: TEST_HOST, path });
    tlsSock.end(
      `HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: ${Buffer.byteLength(body)}\r\nconnection: close\r\n\r\n${body}`,
    );
  };
  tlsSock.on('data', onData);
  tlsSock.on('error', () => tlsSock.destroy());
}
