import test from 'node:test';
import assert from 'node:assert/strict';
import { generateKeyPairSync, createSign } from 'node:crypto';
import { BudgetAccount, handleBudget, costMicros, streamUsage, monthOf, resetsAt } from '../src/budget.js';
import { verifyAppleToken, resetAppleKeys } from '../src/apple.js';

const env = {
  ANTHROPIC_API_KEY: 'antiwork-key', BUDGET_LIMIT_USD: '200', BUDGET_MODEL: 'claude-opus-5-5',
  BUDGET_PRICES: '{"input":4,"output":20,"cache_read":0.2,"cache_write_5m":5,"cache_write_1h":8,"web_search":10000}',
  APP_BUNDLE_ID: 'com.gumroad.coconut', APPLE_TEAM_ID: '66LJACJ2U7',
  REGISTRATION_LIMIT: { limit: async () => ({ success: true }) },
};

class Storage {
  constructor() { this.map = new Map(); }
  async get(key) { return structuredClone(this.map.get(key)); }
  async put(key, value) { this.map.set(key, structuredClone(value)); }
}

// One Durable Object per account, as the runtime gives them, behind the worker's handler.
function harness(upstream) {
  const objects = new Map();
  const calls = [];
  const fetchImpl = async (url, init) => { calls.push({ url, init }); return upstream(url, init); };
  const budgets = {
    idFromName: name => name,
    get: name => {
      if (!objects.has(name)) objects.set(name, new BudgetAccount({ storage: new Storage() }, fullEnv, fetchImpl));
      return objects.get(name);
    },
  };
  const fullEnv = { ...env, BUDGETS: budgets };
  const call = (path, init = {}, verify) => handleBudget(new Request(`https://relay${path}`, init), fullEnv, verify);
  return { call, objects, calls };
}

const signIn = async (h, sub = 'apple-user-1') => {
  const response = await h.call('/v1/budget/accounts', { method: 'POST', body: JSON.stringify({ identity_token: 't' }) },
    async () => sub);
  assert.equal(response.status, 200);
  return (await response.json()).token;
};

const message = (token, body = {}) => ({ method: 'POST', headers: { 'x-api-key': token, 'content-type': 'application/json' },
  body: JSON.stringify({ model: 'claude-opus-5-5', max_tokens: 10, messages: [], ...body }) });

test('cost uses the list prices of each token kind', () => {
  const price = JSON.parse(env.BUDGET_PRICES);
  assert.equal(costMicros({ input_tokens: 1000, output_tokens: 1000 }, price), 24000);
  assert.equal(costMicros({ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 1000000,
    cache_creation_input_tokens: 100, cache_creation: { ephemeral_5m_input_tokens: 0, ephemeral_1h_input_tokens: 100 } }, price),
  200000 + 800);
  assert.equal(costMicros({ cache_creation_input_tokens: 1000 }, price), 5000);
  assert.equal(costMicros({ server_tool_use: { web_search_requests: 2 } }, price), 20000);
});

test('stream usage takes input from message_start and output from message_delta', () => {
  const text = [
    'event: message_start', 'data: {"type":"message_start","message":{"usage":{"input_tokens":500,"output_tokens":1,"cache_read_input_tokens":20}}}',
    'event: content_block_delta', 'data: {"type":"content_block_delta","delta":{"text":"hi"}}',
    'event: message_delta', 'data: {"type":"message_delta","usage":{"output_tokens":300}}', 'data: {"cut',
  ].join('\n');
  assert.deepEqual(streamUsage(text), { input_tokens: 500, output_tokens: 300, cache_read_input_tokens: 20 });
});

test('months are UTC calendar months', () => {
  const at = Date.parse('2026-11-30T23:30:00Z');
  assert.equal(monthOf(at), '2026-11');
  assert.equal(resetsAt(at), '2026-12-01T00:00:00.000Z');
  assert.equal(resetsAt(Date.parse('2026-12-05T00:00:00Z')), '2027-01-01T00:00:00.000Z');
});

test('one Apple ID is one account: signing in again shares the ledger', async () => {
  const h = harness(async () => Response.json({ usage: { input_tokens: 1000, output_tokens: 1000 } }));
  const first = await signIn(h);
  const second = await signIn(h);
  assert.notEqual(first, second);
  assert.equal(first.split('.')[1], second.split('.')[1]);
  assert.equal((await h.call('/v1/budget/anthropic/v1/messages', message(first))).status, 200);
  await [...h.objects.values()][0].metering;
  const usage = await (await h.call('/v1/budget/usage', { headers: { authorization: `Bearer ${second}` } })).json();
  assert.equal(usage.spent_usd, 0.024);
  assert.equal(usage.limit_usd, 200);
  assert.equal(usage.paused, false);
  const other = await signIn(h, 'apple-user-2');
  assert.notEqual(other.split('.')[1], first.split('.')[1]);
});

test('the proxy sends Antiwork key upstream and never the account token', async () => {
  const h = harness(async () => Response.json({ usage: { input_tokens: 1, output_tokens: 1 } }));
  const token = await signIn(h);
  await h.call('/v1/budget/anthropic/v1/messages', message(token));
  const { url, init } = h.calls[0];
  assert.equal(url, 'https://api.anthropic.com/v1/messages');
  assert.equal(init.headers['x-api-key'], 'antiwork-key');
  assert.ok(!JSON.stringify(init).includes(token.split('.')[2]));
});

test('a wrong token, another model, or fast mode is refused without an upstream call', async () => {
  const h = harness(async () => Response.json({ usage: {} }));
  const token = await signIn(h);
  const forged = token.slice(0, -4) + '0000';
  assert.equal((await h.call('/v1/budget/anthropic/v1/messages', message(forged))).status, 401);
  assert.equal((await h.call('/v1/budget/anthropic/v1/messages', message('sk-ant-x'))).status, 401);
  assert.equal((await h.call('/v1/budget/anthropic/v1/messages', message(token, { model: 'claude-fable-5-2' }))).status, 400);
  assert.equal((await h.call('/v1/budget/anthropic/v1/messages', message(token, { speed: 'fast' }))).status, 400);
  assert.equal((await h.call('/v1/budget/anthropic/v1/complete', message(token))).status, 404);
  assert.equal(h.calls.length, 0);
});

test('at the limit the proxy refuses with the reason until the next month', async () => {
  const h = harness(async () => Response.json({ usage: { input_tokens: 0, output_tokens: 10000000 } }));
  const token = await signIn(h);
  assert.equal((await h.call('/v1/budget/anthropic/v1/messages', message(token))).status, 200);
  await [...h.objects.values()][0].metering;
  const refused = await h.call('/v1/budget/anthropic/v1/messages', message(token));
  assert.equal(refused.status, 403);
  assert.equal(refused.headers.get('x-coconut-budget'), 'paused');
  assert.match((await refused.json()).error.message, /\$200 of model use for \w+\. It starts again on \w+ 1\./);
  assert.equal(h.calls.length, 1);
  const usage = await (await h.call('/v1/budget/usage', { headers: { 'x-api-key': token } })).json();
  assert.equal(usage.paused, true);
  // The next month starts from zero.
  const account = [...h.objects.values()][0];
  const next = await account.usage(Date.parse(resetsAt()) + 1000);
  assert.equal(next.spent_usd, 0);
  assert.equal(next.paused, false);
});

test('a stream is metered to the end even when the Mac stops reading', async () => {
  const sse = 'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":1000000,"output_tokens":1}}}\n\n'
    + 'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":1000}}\n\n';
  const h = harness(async () => new Response(sse, { headers: { 'content-type': 'text/event-stream' } }));
  const token = await signIn(h);
  const response = await h.call('/v1/budget/anthropic/v1/messages', message(token, { stream: true }));
  await response.body.cancel();
  await [...h.objects.values()][0].metering;
  const usage = await (await h.call('/v1/budget/usage', { headers: { 'x-api-key': token } })).json();
  assert.equal(usage.spent_usd, 4.02);
});

test('an upstream error is passed on and costs nothing', async () => {
  const h = harness(async () => Response.json({ type: 'error', error: { type: 'overloaded_error' } }, { status: 529 }));
  const token = await signIn(h);
  assert.equal((await h.call('/v1/budget/anthropic/v1/messages', message(token))).status, 529);
  const usage = await (await h.call('/v1/budget/usage', { headers: { 'x-api-key': token } })).json();
  assert.equal(usage.spent_usd, 0);
  assert.equal([...h.objects.values()][0].inflight, 0);
});

test('signing out revokes only that token', async () => {
  const h = harness(async () => Response.json({ usage: {} }));
  const first = await signIn(h);
  const second = await signIn(h);
  assert.equal((await h.call('/v1/budget/token', { method: 'DELETE', headers: { 'x-api-key': first } })).status, 204);
  assert.equal((await h.call('/v1/budget/usage', { headers: { 'x-api-key': first } })).status, 401);
  assert.equal((await h.call('/v1/budget/usage', { headers: { 'x-api-key': second } })).status, 200);
});

test('without Antiwork key the budget is off', async () => {
  const response = await handleBudget(new Request('https://relay/v1/budget/usage'), { ...env, ANTHROPIC_API_KEY: '' });
  assert.equal(response.status, 503);
});

test('Sign in with Apple tokens are checked against Apple keys', async () => {
  const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
  const jwk = { ...publicKey.export({ format: 'jwk' }), kid: 'k1', alg: 'RS256', use: 'sig' };
  const keys = async () => Response.json({ keys: [jwk] });
  const now = Date.parse('2026-11-17T16:00:00Z');
  const sign = (claims, kid = 'k1') => {
    const head = Buffer.from(JSON.stringify({ alg: 'RS256', kid })).toString('base64url');
    const body = Buffer.from(JSON.stringify(claims)).toString('base64url');
    const signature = createSign('RSA-SHA256').update(`${head}.${body}`).sign(privateKey).toString('base64url');
    return `${head}.${body}.${signature}`;
  };
  const good = { iss: 'https://appleid.apple.com', aud: 'com.gumroad.coconut', sub: '001.abc', exp: now / 1000 + 600, iat: now / 1000 };
  resetAppleKeys();
  assert.equal(await verifyAppleToken(sign(good), env, keys, now), '001.abc');
  await assert.rejects(verifyAppleToken(sign({ ...good, aud: 'com.other.app' }), env, keys, now));
  await assert.rejects(verifyAppleToken(sign({ ...good, iss: 'https://evil' }), env, keys, now));
  await assert.rejects(verifyAppleToken(sign({ ...good, exp: now / 1000 - 1 }), env, keys, now));
  await assert.rejects(verifyAppleToken(sign(good, 'k2'), env, keys, now));
  const [h, b] = sign(good).split('.');
  const forged = Buffer.from(JSON.stringify({ ...good, sub: '001.other' })).toString('base64url');
  await assert.rejects(verifyAppleToken(`${h}.${forged}.${sign(good).split('.')[2]}`, env, keys, now));
  assert.ok(b);
});
