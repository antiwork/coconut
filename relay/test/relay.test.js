import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash, randomBytes, randomUUID } from 'node:crypto';
import { PushDevice } from '../src/device.js';
import worker from '../src/worker.js';
import { verifyRegistration } from '../src/attest.js';

class Storage {
  values = new Map();
  async get(key) { return structuredClone(this.values.get(key)); }
  async put(key, value) {
    if (typeof key === 'object') { for (const [k, v] of Object.entries(key)) await this.put(k, v); }
    else this.values.set(key, structuredClone(value));
  }
  async delete(key) { this.values.delete(key); }
  async list({ prefix }) { return new Map([...this.values].filter(([key]) => key.startsWith(prefix))); }
  async setAlarm() {}
}
const key = randomBytes(32).toString('base64');
const routeID = 'a'.repeat(64);
const env = { APP_BUNDLE_ID: 'com.gumroad.coconut', APPLE_TEAM_ID: '66LJACJ2U7' };
const makeRequest = (path, body, token, method = 'POST') => new Request(`https://push.example${path}`, {
  method, headers: { 'x-route-id': routeID, authorization: `Bearer ${token || ''}` },
  body: body ? JSON.stringify(body) : undefined,
});

async function fixture() {
  const ctx = { storage: new Storage() }, calls = [];
  const send = async (...args) => { calls.push(args); return { status: 200, reason: 'Accepted' }; };
  const verified = () => ({ publicKey: 'verified-apple-key', environment: 'production', signCount: 0 });
  const device = new PushDevice(ctx, env, verified, send);
  const challenge = await (await device.fetch(makeRequest('/v1/challenge', { key_id: key }))).json();
  const publicKey = randomBytes(32);
  const payload = { challenge: challenge.challenge, device_id: randomUUID(), token: 'b'.repeat(64),
    encryption_key: publicKey.toString('base64'), environment: 'production' };
  const input = { key_id: key, payload: Buffer.from(JSON.stringify(payload)).toString('base64'), attestation: 'test' };
  const credential = await (await device.fetch(makeRequest('/v1/register', input))).json();
  const id = randomUUID(), expires = Math.floor(Date.now() / 1000) + 600;
  const notification = { id, expires, collapse_id: 'c'.repeat(64), envelope: {
    version: 1, id, expires, keyId: createHash('sha256').update(publicKey).digest('hex'),
    encapsulatedKey: randomBytes(32).toString('base64'), ciphertext: randomBytes(128).toString('base64'),
  } };
  return { ctx, device, calls, send, verified, credential, notification, input };
}

test('two users cannot use each other’s delivery credentials', async () => {
  const first = await fixture(), second = await fixture();
  const response = await first.device.fetch(makeRequest(`/v1/routes/${routeID}/send`, first.notification, second.credential.send_token));
  assert.equal(response.status, 401);
  assert.equal(first.calls.length, 0);
});

test('concurrent retries and restarts send one accepted notification', async () => {
  const f = await fixture();
  const send = device => device.fetch(makeRequest(`/v1/routes/${routeID}/send`, f.notification, f.credential.send_token));
  const results = await Promise.all([send(f.device), send(f.device)]);
  assert.equal(f.calls.length, 1);
  assert.equal((await results[0].json()).status, 200);
  const restarted = new PushDevice(f.ctx, env, f.verified, f.send);
  assert.equal((await (await send(restarted)).json()).status, 200);
  assert.equal(f.calls.length, 1);
});

test('registration retries return the original credentials', async () => {
  const f = await fixture();
  const again = await (await f.device.fetch(makeRequest('/v1/register', f.input))).json();
  assert.deepEqual(again, f.credential);
});

test('an internal delivery failure remains retryable after restart', async () => {
  const f = await fixture();
  const failing = new PushDevice(f.ctx, env, f.verified, async () => { throw new TypeError('Signing unavailable'); });
  const request = () => makeRequest(`/v1/routes/${routeID}/send`, f.notification, f.credential.send_token);
  assert.equal((await failing.fetch(request())).status, 503);
  const receiptKey = `receipt:${f.notification.id}`;
  const receipt = await f.ctx.storage.get(receiptKey);
  await f.ctx.storage.put(receiptKey, { ...receipt, retryAt: Date.now() - 1 });
  const restarted = new PushDevice(f.ctx, env, f.verified, f.send);
  assert.equal((await (await restarted.fetch(request())).json()).status, 200);
  assert.equal((await (await restarted.fetch(request())).json()).status, 200);
  assert.equal(f.calls.length, 1);
});

test('reused IDs cannot carry changed ciphertext', async () => {
  const f = await fixture();
  await f.device.fetch(makeRequest(`/v1/routes/${routeID}/send`, f.notification, f.credential.send_token));
  f.notification.envelope.ciphertext = randomBytes(128).toString('base64');
  const response = await f.device.fetch(makeRequest(`/v1/routes/${routeID}/send`, f.notification, f.credential.send_token));
  assert.equal(response.status, 409);
  assert.equal(f.calls.length, 1);
});

test('only the phone can revoke its route', async () => {
  const f = await fixture();
  assert.equal((await f.device.fetch(makeRequest(`/v1/routes/${routeID}`, null, f.credential.send_token, 'DELETE'))).status, 401);
  assert.equal((await f.device.fetch(makeRequest(`/v1/routes/${routeID}`, null, f.credential.manage_token, 'DELETE'))).status, 200);
  assert.equal((await (await f.device.fetch(makeRequest(`/v1/routes/${routeID}/send`, f.notification, f.credential.send_token))).json()).status, 410);
  assert.equal(f.calls.length, 0);
});

test('plaintext fields, expired messages, and another recipient key are rejected', async () => {
  for (const mutate of [n => { n.envelope.title = 'Secret'; }, n => { n.expires = n.envelope.expires = 1; }, n => { n.envelope.keyId = '0'.repeat(64); }]) {
    const f = await fixture(); mutate(f.notification);
    assert.equal((await f.device.fetch(makeRequest(`/v1/routes/${routeID}/send`, f.notification, f.credential.send_token))).status, 400);
    assert.equal(f.calls.length, 0);
  }
});

test('relay state contains no notification plaintext', async () => {
  const f = await fixture();
  await f.device.fetch(makeRequest(`/v1/routes/${routeID}/send`, f.notification, f.credential.send_token));
  const stored = JSON.stringify([...f.ctx.storage.values]);
  assert.equal(stored.includes('ciphertext'), false);
  assert.equal(stored.includes('Desk'), false);
});

test('registration requires real Apple attestation', async () => {
  assert.throws(() => verifyRegistration({ key_id: key, attestation: 'Zm9yZ2Vk' }, Buffer.from('payload'), null, env));
  const ctx = { storage: new Storage() };
  const device = new PushDevice(ctx, env);
  const response = await device.fetch(makeRequest('/v1/register', { payload: Buffer.from('{}').toString('base64') }));
  assert.equal(response.status, 401);
  assert.equal(await ctx.storage.get('registration'), undefined);
});

test('worker limits streaming bodies without trusting content-length', async () => {
  const response = await worker.fetch(makeRequest('/v1/register', { huge: 'x'.repeat(25000) }), {});
  assert.equal(response.status, 413);
});
