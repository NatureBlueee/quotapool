import { EventEmitter } from 'node:events';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { nodeToWeb } from '../src/upstream-fetch.js';

function response() {
  const res = new EventEmitter();
  res.complete = false;
  res.readableEnded = false;
  res.pause = () => {};
  res.resume = () => {};
  res.destroy = () => {};
  return res;
}

test('a socket close before HTTP body completion rejects the stream', async () => {
  const res = response();
  const reader = nodeToWeb(res).getReader();
  const pending = reader.read();
  res.emit('close');
  await assert.rejects(pending, (err) => {
    assert.equal(err.code, 'ECONNRESET');
    assert.match(err.message, /before the body completed/);
    return true;
  });
});

test('an explicit upstream abort rejects the stream', async () => {
  const res = response();
  const reader = nodeToWeb(res).getReader();
  const pending = reader.read();
  res.emit('aborted');
  await assert.rejects(pending, { code: 'ECONNRESET' });
});

test('a normal response end still closes the stream cleanly', async () => {
  const res = response();
  const reader = nodeToWeb(res).getReader();
  const pending = reader.read();
  res.complete = true;
  res.readableEnded = true;
  res.emit('end');
  res.emit('close');
  assert.deepEqual(await pending, { done: true, value: undefined });
});
