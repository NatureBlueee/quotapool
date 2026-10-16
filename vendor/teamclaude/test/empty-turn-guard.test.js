import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  emptyTurnRetryLimit,
  eventHasMeaningfulAssistantOutput,
  probeAssistantJSON,
  probeAssistantSSE,
  requestEndsWithToolResult,
} from '../src/empty-turn-guard.js';

const enc = new TextEncoder();

function streamFrom(parts) {
  return new ReadableStream({
    start(controller) {
      for (const part of parts) controller.enqueue(enc.encode(part));
      controller.close();
    },
  });
}

async function read(stream) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let out = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) return out;
    out += decoder.decode(value, { stream: true });
  }
}

test('recognises a request whose last user turn contains a tool result', () => {
  const body = Buffer.from(JSON.stringify({
    messages: [
      { role: 'assistant', content: [{ type: 'tool_use', id: 't1' }] },
      { role: 'user', content: [{ type: 'tool_result', tool_use_id: 't1' }] },
    ],
  }));
  assert.equal(requestEndsWithToolResult(body, '/v1/messages', 'application/json'), true);
  assert.equal(requestEndsWithToolResult(body, '/v1/complete', 'application/json'), false);
});

test('does not guard a later ordinary user prompt', () => {
  const body = Buffer.from(JSON.stringify({
    messages: [
      { role: 'user', content: [{ type: 'tool_result', tool_use_id: 't1' }] },
      { role: 'assistant', content: [{ type: 'text', text: 'done' }] },
      { role: 'user', content: 'continue' },
    ],
  }));
  assert.equal(requestEndsWithToolResult(body, '/v1/messages', 'application/json'), false);
});

test('recognises same-turn tool results followed by generated user context', () => {
  const body = Buffer.from(JSON.stringify({
    messages: [
      { role: 'assistant', content: [{ type: 'tool_use', id: 't1' }] },
      { role: 'user', content: [{ type: 'tool_result', tool_use_id: 't1' }] },
      { role: 'user', content: [{ type: 'text', text: '<system-reminder>context</system-reminder>' }] },
    ],
  }));
  assert.equal(requestEndsWithToolResult(
    body, '/v1/messages/?beta=true', 'application/json; charset=utf-8',
  ), true);
});

test('thinking-only JSON end_turn is classified as empty', () => {
  const result = probeAssistantJSON(Buffer.from(JSON.stringify({
    type: 'message',
    role: 'assistant',
    content: [{ type: 'thinking', thinking: '', signature: 'hidden' }],
    stop_reason: 'end_turn',
    usage: { input_tokens: 438861, output_tokens: 1816 },
  })));
  assert.equal(result.empty, true);
  assert.equal(result.meaningful, false);
  assert.deepEqual(result.usage, { inputTokens: 438861, outputTokens: 1816 });
});

test('thinking and signature deltas are not user-visible output', () => {
  assert.equal(eventHasMeaningfulAssistantOutput(
    'data: {"type":"content_block_start","content_block":{"type":"thinking","thinking":""}}',
  ), false);
  assert.equal(eventHasMeaningfulAssistantOutput(
    'data: {"type":"content_block_delta","delta":{"type":"signature_delta","signature":"abc"}}',
  ), false);
});

test('non-whitespace text and tool use are meaningful', () => {
  assert.equal(eventHasMeaningfulAssistantOutput(
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"  ok"}}',
  ), true);
  assert.equal(eventHasMeaningfulAssistantOutput(
    'data: {"type":"content_block_start","content_block":{"type":"tool_use","id":"t2"}}',
  ), true);
});

test('a thinking-only end_turn is classified empty and replayed byte-for-byte', async () => {
  const source = [
    'event: message_start\ndata: {"type":"message_start","message":{"content":[],"usage":{"input_tokens":438861}}}\n\n',
    'event: content_block_start\ndata: {"type":"content_block_start","content_block":{"type":"thinking"}}\n\n',
    'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"hidden"}}\n\n',
    'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1816}}\n\n',
    'event: message_stop\ndata: {"type":"message_stop"}\n\n',
  ];
  const result = await probeAssistantSSE(streamFrom(source));
  assert.equal(result.empty, true);
  assert.deepEqual(result.usage, { inputTokens: 438861, outputTokens: 1816 });
  assert.equal(await read(result.stream), source.join(''));
});

test('a text response stops probing early and preserves the entire stream', async () => {
  const source = [
    'data: {"type":"message_start","message":{"content":[]}}\n\n',
    'data: {"type":"content_block_start","content_block":{"type":"thinking"}}\n\n',
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"visible"}}\n\n',
    'data: {"type":"message_stop"}\n\n',
  ];
  const result = await probeAssistantSSE(streamFrom(source));
  assert.equal(result.empty, false);
  assert.equal(result.meaningful, true);
  assert.equal(await read(result.stream), source.join(''));
});

test('probe cap fails open instead of buffering an unbounded thought stream', async () => {
  const source = [
    'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"long"}}\n\n',
    'data: {"type":"message_stop"}\n\n',
  ];
  const result = await probeAssistantSSE(streamFrom(source), 10);
  assert.equal(result.empty, false);
  assert.equal(result.meaningful, false);
  assert.equal(await read(result.stream), source.join(''));
});

test('retry limit defaults to two and is bounded', () => {
  assert.equal(emptyTurnRetryLimit({}), 2);
  assert.equal(emptyTurnRetryLimit({ TEAMCLAUDE_EMPTY_TURN_RETRIES: '0' }), 0);
  assert.equal(emptyTurnRetryLimit({ TEAMCLAUDE_EMPTY_TURN_RETRIES: '99' }), 3);
});
