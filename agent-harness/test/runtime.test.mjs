import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

// The locked-mode provider is read when the module loads; each query string loads a fresh instance.
async function lockedRuntime(provider) {
  if (provider) process.env.GRATING_PI_PROVIDER = provider; else delete process.env.GRATING_PI_PROVIDER;
  return import(`../dist/runtime.js?provider=${provider || 'none'}`);
}

function signIn(authPath) {
  fs.writeFileSync(authPath, JSON.stringify({'openai-codex': {
    type: 'oauth', access: 'test-access-secret', refresh: 'test-refresh-secret', expires: Date.now() + 3600000,
  }}));
}

test('locked mode offers no provider unless the server names one', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-auth-test-'));
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  const authPath = path.join(directory, 'auth.json');
  t.mock.method(globalThis, 'fetch', () => { throw Error('Auth status must not contact a provider'); });
  signIn(authPath);
  const {authStatus} = await lockedRuntime(null);
  const status = await authStatus(authPath);
  assert.equal(status.configured, false);
  assert.equal(status.provider, null);
  assert.deepEqual(status.models, []);
});

test('status detects persisted login and logout without exposing credentials or using the network', async t => {
  const {authStatus} = await lockedRuntime('openai-codex');
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-auth-test-'));
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  const authPath = path.join(directory, 'auth.json');
  t.mock.method(globalThis, 'fetch', () => { throw Error('Auth status must not contact a provider'); });
  fs.writeFileSync(authPath, '{}');
  assert.equal((await authStatus(authPath)).configured, false);
  signIn(authPath);
  const status = await authStatus(authPath);
  assert.equal(status.configured, true);
  assert.equal(status.provider, 'openai-codex');
  assert.ok(status.models.some(m => m.id === 'gpt-6-astra' && m.provider === 'openai-codex'));
  assert.equal(status.providers['openai-codex'].billing, 'subscription');
  assert.ok(!JSON.stringify(status).includes('secret'));
  fs.writeFileSync(authPath, '{}');
  assert.equal((await authStatus(authPath)).configured, false);
});
