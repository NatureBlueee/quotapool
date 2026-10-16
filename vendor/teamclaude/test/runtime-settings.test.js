import { test } from 'node:test';
import assert from 'node:assert/strict';

import { applyReloadableRuntimeSettings } from '../src/runtime-settings.js';

test('reload applies switch threshold to the live account manager', () => {
  const live = { switchThreshold: 0.98 };
  const manager = { switchThreshold: 0.98 };

  const changed = applyReloadableRuntimeSettings(
    { switchThreshold: 0.99 }, live, manager,
  );

  assert.equal(live.switchThreshold, 0.99);
  assert.equal(manager.switchThreshold, 0.99);
  assert.deepEqual(changed, { switchThreshold: 0.99 });
});

test('reload ignores invalid switch thresholds', () => {
  for (const value of [0, -1, 1.01, 'not-a-number', null]) {
    const live = { switchThreshold: 0.98 };
    const manager = { switchThreshold: 0.98 };
    applyReloadableRuntimeSettings({ switchThreshold: value }, live, manager);
    assert.equal(live.switchThreshold, 0.98, `value=${value}`);
    assert.equal(manager.switchThreshold, 0.98, `value=${value}`);
  }
});

test('reload replaces future connection admission limits without closing requests', () => {
  const live = { connectionManagement: { maxPerSession: 64 } };
  const manager = { switchThreshold: 0.98 };

  const changed = applyReloadableRuntimeSettings({
    connectionManagement: {
      maxConnections: 256,
      maxActiveRequests: 96,
      maxPerSession: 12,
    },
  }, live, manager);

  assert.deepEqual(live.connectionManagement, {
    maxConnections: 256,
    maxActiveRequests: 96,
    maxPerSession: 12,
  });
  assert.deepEqual(changed.connectionManagement, live.connectionManagement);
});
