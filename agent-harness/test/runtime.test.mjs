import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {authStatus} from '../dist/runtime.js';

test('status detects persisted login and logout without exposing credentials or using the network', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-auth-test-'));
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  const authPath = path.join(directory, 'auth.json');
  t.mock.method(globalThis, 'fetch', () => { throw Error('Auth status must not contact a provider'); });
  fs.writeFileSync(authPath, '{}');
  assert.equal((await authStatus(authPath)).configured, false);
  fs.writeFileSync(authPath, JSON.stringify({'openai-codex': {
    type: 'oauth', access: 'test-access-secret', refresh: 'test-refresh-secret', expires: Date.now() + 3600000,
  }}));
  const status = await authStatus(authPath);
  assert.equal(status.configured, true);
  assert.equal(status.provider, 'openai-codex');
  assert.ok(status.models.some(m => m.id === 'gpt-6-astra' && m.provider === 'openai-codex'));
  assert.equal(status.providers['openai-codex'].billing, 'subscription');
  assert.ok(!JSON.stringify(status).includes('secret'));
  fs.writeFileSync(authPath, '{}');
  assert.equal((await authStatus(authPath)).configured, false);
});
