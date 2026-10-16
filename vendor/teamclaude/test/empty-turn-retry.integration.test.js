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

function sse(events) {
  return events.map(value => `data: ${JSON.stringify(value)}\n\n`).join('');
}

function emptyTurn(sequence) {
  return sse([
    {
      type: 'message_start',
      message: { content: [], usage: { input_tokens: 100 + sequence } },
    },
    {
      type: 'content_block_start',
      index: 0,
      content_block: { type: 'thinking', thinking: '' },
    },
    {
      type: 'content_block_delta',
      index: 0,
      delta: { type: 'signature_delta', signature: `discard-${sequence}` },
    },
    {
      type: 'message_delta',
      delta: { stop_reason: 'end_turn' },
      usage: { output_tokens: 20 + sequence },
    },
    { type: 'message_stop' },
  ]);
}

function visibleTurn() {
  return sse([
    { type: 'message_start', message: { content: [], usage: { input_tokens: 103 } } },
    { type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } },
    { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: '已自动恢复' } },
    { type: 'message_delta', delta: { stop_reason: 'end_turn' }, usage: { output_tokens: 23 } },
    { type: 'message_stop' },
  ]);
}

async function fixture(upstreamBodies, contentType = 'text/event-stream') {
  let hits = 0;
  const upstream = await listen((_req, res) => {
    const body = upstreamBodies[Math.min(hits, upstreamBodies.length - 1)];
    hits += 1;
    res.writeHead(200, { 'content-type': contentType });
    res.end(body);
  });

  const events = [];
  const accountManager = new AccountManager([{
    name: 'max-test',
    type: 'oauth',
    accessToken: 'test-token',
    upstream,
  }]);
  const proxy = await listen(createProxyRequestListener({
    accountManager,
    upstream,
    hooks: { onEmptyTurn: (_id, event) => events.push(event) },
  }));
  return { accountManager, events, get hits() { return hits; }, proxy };
}

async function postMessages(proxy, messages) {
  return fetch(`${proxy}/v1/messages`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-claude-code-session-id': 'session-empty-turn-test',
    },
    body: JSON.stringify({ model: 'claude-opus-5', stream: true, messages }),
  });
}

test('thinking-only responses after tool_result retry invisibly until text appears', async () => {
  const f = await fixture([emptyTurn(1), emptyTurn(2), visibleTurn()]);
  const response = await postMessages(f.proxy, [
    { role: 'assistant', content: [{ type: 'tool_use', id: 'tool-1', name: 'browser' }] },
    { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'tool-1', content: 'ok' }] },
  ]);
  const body = await response.text();

  assert.equal(response.status, 200);
  assert.equal(f.hits, 3);
  assert.equal(f.events.length, 2);
  assert.equal(f.events.every(event => event.willRetry), true);
  assert.deepEqual(
    f.events.map(event => [event.inputTokens, event.outputTokens]),
    [[101, 21], [102, 22]],
  );
  assert.match(body, /已自动恢复/);
  assert.doesNotMatch(body, /discard-1|discard-2/);
  assert.equal(f.accountManager.accounts[0].usage.totalRequests, 3);
});

test('retry exhaustion is bounded and relays the final response for Desktop fallback', async () => {
  const f = await fixture([emptyTurn(1), emptyTurn(2), emptyTurn(3), visibleTurn()]);
  const response = await postMessages(f.proxy, [
    { role: 'assistant', content: [{ type: 'tool_use', id: 'tool-1', name: 'browser' }] },
    { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'tool-1', content: 'ok' }] },
  ]);
  const body = await response.text();

  assert.equal(f.hits, 3);
  assert.equal(f.events.length, 3);
  assert.equal(f.events.at(-1).willRetry, false);
  assert.match(body, /discard-3/);
  assert.doesNotMatch(body, /discard-1|discard-2|已自动恢复/);
});

test('thinking-only JSON responses after tool_result are also retried', async () => {
  const emptyJson = JSON.stringify({
    type: 'message', role: 'assistant',
    content: [{ type: 'thinking', thinking: '', signature: 'discard-json' }],
    stop_reason: 'end_turn',
    usage: { input_tokens: 201, output_tokens: 31 },
  });
  const visibleJson = JSON.stringify({
    type: 'message', role: 'assistant',
    content: [{ type: 'text', text: 'JSON 已自动恢复' }],
    stop_reason: 'end_turn',
    usage: { input_tokens: 202, output_tokens: 32 },
  });
  const f = await fixture([emptyJson, visibleJson], 'application/json');
  const response = await postMessages(f.proxy, [
    { role: 'assistant', content: [{ type: 'tool_use', id: 'tool-json', name: 'browser' }] },
    { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'tool-json', content: 'ok' }] },
  ]);
  const body = await response.text();

  assert.equal(response.status, 200);
  assert.equal(f.hits, 2);
  assert.equal(f.events.length, 1);
  assert.equal(f.events[0].responseMode, 'json');
  assert.match(body, /JSON 已自动恢复/);
  assert.doesNotMatch(body, /discard-json/);
});

test('an ordinary prompt is never replayed by the tool-result guard', async () => {
  const f = await fixture([emptyTurn(1), visibleTurn()]);
  const response = await postMessages(f.proxy, [
    { role: 'user', content: '普通消息' },
  ]);
  const body = await response.text();

  assert.equal(response.status, 200);
  assert.equal(f.hits, 1);
  assert.equal(f.events.length, 0);
  assert.match(body, /discard-1/);
  assert.doesNotMatch(body, /已自动恢复/);
});
