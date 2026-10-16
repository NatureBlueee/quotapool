// Detect a successful-looking Anthropic SSE response that contains only hidden
// thinking and then stops.  Claude Desktop renders no assistant message for
// that shape, so a completed HTTP response leaves the user staring at a stale
// "running" indicator after a tool has already returned.

const DEFAULT_PROBE_BYTES = 512 * 1024;

function dataPayload(event) {
  const lines = String(event).split(/\r?\n/);
  const data = lines
    .filter(line => line.startsWith('data:'))
    .map(line => line.slice(5).trimStart())
    .join('\n');
  if (!data || data === '[DONE]') return null;
  try {
    return JSON.parse(data);
  } catch {
    // A malformed/unknown event must pass through.  Treating it as empty could
    // replay a response whose semantics this guard did not understand.
    return { type: '__unclassified__' };
  }
}

function blockIsMeaningful(block) {
  if (!block || typeof block !== 'object') return false;
  if (block.type === 'thinking' || block.type === 'redacted_thinking') return false;
  if (block.type === 'text') return Boolean(String(block.text || '').trim());
  // A tool_use (including server-side tools) is an actionable assistant turn,
  // even before its input JSON has finished streaming.  Unknown future block
  // types fail open so a protocol addition can never be replayed by accident.
  return Boolean(block.type);
}

function observeUsage(event, usage) {
  const data = dataPayload(event);
  if (!data || data.type === '__unclassified__') return;
  if (data.type === 'message_start' && data.message?.usage) {
    usage.inputTokens = Math.max(
      usage.inputTokens,
      Number(data.message.usage.input_tokens) || 0,
    );
  } else if (data.type === 'message_delta' && data.usage) {
    usage.outputTokens = Math.max(
      usage.outputTokens,
      Number(data.usage.output_tokens) || 0,
    );
  }
}

export function eventHasMeaningfulAssistantOutput(event) {
  const data = dataPayload(event);
  if (!data) return false;
  if (data.type === '__unclassified__' || data.type === 'error') return true;

  if (data.type === 'message_start') {
    return Array.isArray(data.message?.content)
      && data.message.content.some(blockIsMeaningful);
  }
  if (data.type === 'content_block_start') {
    return blockIsMeaningful(data.content_block);
  }
  if (data.type === 'content_block_delta') {
    if (data.delta?.type === 'text_delta') {
      return Boolean(String(data.delta.text || '').trim());
    }
    // thinking/signature deltas are hidden reasoning.  Any other delta is a
    // future/actionable protocol feature and therefore fails open.
    return !['thinking_delta', 'signature_delta'].includes(data.delta?.type);
  }
  return false;
}

export function requestEndsWithToolResult(body, path = '', contentType = '') {
  if (!/\/v1\/messages\/?(?:\?|$)/.test(String(path))) return false;
  if (contentType && !/json/i.test(String(contentType))) return false;
  try {
    const parsed = JSON.parse(Buffer.from(body).toString('utf8'));
    const messages = Array.isArray(parsed.messages) ? parsed.messages : [];
    // Claude Code can append same-turn user context after a tool_result (for
    // example hook output).  Look only after the most recent assistant turn:
    // this catches that wire shape without ever replaying a later ordinary
    // prompt after an assistant has already answered.
    const lastAssistant = messages.findLastIndex(message => message?.role === 'assistant');
    const sameTurnUserMessages = messages.slice(lastAssistant + 1)
      .filter(message => message?.role === 'user');
    return sameTurnUserMessages.some(message => {
      const blocks = Array.isArray(message.content)
        ? message.content
        : (message.content && typeof message.content === 'object' ? [message.content] : []);
      return blocks.some(block => block?.type === 'tool_result');
    });
  } catch {
    return false;
  }
}

export function probeAssistantJSON(buffer) {
  const usage = { inputTokens: 0, outputTokens: 0 };
  try {
    const message = JSON.parse(Buffer.from(buffer).toString('utf8'));
    if (message?.type === 'error') {
      return { empty: false, meaningful: true, usage };
    }
    usage.inputTokens = Number(message?.usage?.input_tokens) || 0;
    usage.outputTokens = Number(message?.usage?.output_tokens) || 0;
    const blocks = Array.isArray(message?.content) ? message.content : [];
    const meaningful = blocks.some(blockIsMeaningful);
    const completedAssistant = message?.type === 'message'
      && message?.role === 'assistant'
      && message?.stop_reason === 'end_turn';
    return { empty: completedAssistant && !meaningful, meaningful, usage };
  } catch {
    // Unknown response shapes pass through unchanged. Never replay a response
    // whose semantics the guard cannot prove are empty.
    return { empty: false, meaningful: true, usage };
  }
}

function replayStream(reader, chunks, sourceDone) {
  let cursor = 0;
  let finished = sourceDone;
  return new ReadableStream({
    async pull(controller) {
      if (cursor < chunks.length) {
        controller.enqueue(chunks[cursor++]);
        return;
      }
      if (finished) {
        controller.close();
        return;
      }
      try {
        const { done, value } = await reader.read();
        if (done) {
          finished = true;
          controller.close();
        } else {
          controller.enqueue(value);
        }
      } catch (err) {
        controller.error(err);
      }
    },
    cancel(reason) {
      return reader.cancel(reason);
    },
  });
}

// Read only the prefix needed to classify the stream.  A normal response is
// replayed byte-for-byte, then continues directly from the original reader.
// The cap is a fail-open bound: unusually long thinking is streamed normally
// rather than retained without limit.
export async function probeAssistantSSE(webStream, maxBytes = DEFAULT_PROBE_BYTES) {
  const reader = webStream.getReader();
  const decoder = new TextDecoder();
  const chunks = [];
  let bytes = 0;
  let buffer = '';
  let meaningful = false;
  let sourceDone = false;
  const usage = { inputTokens: 0, outputTokens: 0 };

  while (!meaningful && bytes <= maxBytes) {
    const { done, value } = await reader.read();
    if (done) {
      sourceDone = true;
      break;
    }
    chunks.push(value);
    bytes += value.byteLength;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split(/\r?\n\r?\n/);
    buffer = events.pop() || '';
    for (const event of events) observeUsage(event, usage);
    meaningful = events.some(eventHasMeaningfulAssistantOutput);
  }

  if (!meaningful && sourceDone && buffer.trim()) {
    observeUsage(buffer, usage);
    meaningful = eventHasMeaningfulAssistantOutput(buffer);
  }

  return {
    // Only a fully consumed, fully understood thinking-only stream is empty.
    // Hitting the byte cap is deliberately inconclusive and therefore relayed.
    empty: sourceDone && !meaningful,
    meaningful,
    probeBytes: bytes,
    stream: replayStream(reader, chunks, sourceDone),
    usage,
  };
}

export function emptyTurnRetryLimit(env = process.env) {
  const raw = Number(env.TEAMCLAUDE_EMPTY_TURN_RETRIES);
  if (!Number.isFinite(raw)) return 2;
  return Math.max(0, Math.min(3, Math.floor(raw)));
}
