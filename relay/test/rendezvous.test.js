import test from 'node:test';
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';
import worker from '../src/worker.js';

const token = () => randomBytes(32).toString('hex');
const sealed = (size = 64) => randomBytes(size);

function relay(t, { create = 100, limit = 1000, seconds = 600 } = {}) {
  const runtime = new Miniflare(convertV4MiniflareOptions({
    compatibilityDate: '2026-09-10', compatibilityFlags: ['nodejs_compat'],
    modules: [
      { type: 'ESModule', path: 'entry.js', contents: `
        import { handleRendezvous } from './rendezvous.js';
        export { Rendezvous } from './rendezvous.js';
        export default { async fetch(request, env) {
          return (await handleRendezvous(request, env)) || new Response(null, { status: 404 });
        }};` },
      ...['rendezvous.js', 'tunnel.js'].map(name => (
        { type: 'ESModule', path: name, contents: readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8') })),
    ],
    bindings: { RENDEZVOUS_SECONDS: String(seconds) },
    durableObjects: { RENDEZVOUS: { className: 'Rendezvous', useSQLite: true } },
    ratelimits: {
      RENDEZVOUS_CREATE: { namespace_id: '5', simple: { limit: create, period: 60 } },
      RENDEZVOUS_LIMIT: { namespace_id: '6', simple: { limit, period: 60 } },
    },
  }));
  t.after(() => runtime.dispose());
  const url = (id, suffix = '') => `https://relay.test/v1/rendezvous/${id}${suffix}`;
  const phone = (method, id, phoneToken, query = '') => runtime.dispatchFetch(url(id, query), {
    method, headers: phoneToken ? { authorization: `Bearer ${phoneToken}` } : {},
  });
  return {
    create: (id, phoneToken) => phone('PUT', id, phoneToken),
    read: (id, phoneToken, wait = 0) => phone('GET', id, phoneToken, `?wait=${wait}`),
    remove: (id, phoneToken) => phone('DELETE', id, phoneToken),
    send: (id, body) => runtime.dispatchFetch(url(id, '/message'), { method: 'POST', body }),
  };
}

const bytes = async response => new Uint8Array(await response.arrayBuffer());

test('the phone gets the sealed message that the Mac sends', async t => {
  const { create, read, send } = relay(t);
  const id = token(), phone = token(), message = sealed();
  assert.equal((await create(id, phone)).status, 201);
  assert.equal((await read(id, phone)).status, 204);
  assert.equal((await send(id, message)).status, 204);
  const response = await read(id, phone);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.deepEqual(await bytes(response), new Uint8Array(message));
});

test('a waiting read returns as soon as the message arrives', async t => {
  const { create, read, send } = relay(t);
  const id = token(), phone = token(), message = sealed();
  await create(id, phone);
  const started = Date.now();
  const waiting = read(id, phone, 25);
  await new Promise(resolve => setTimeout(resolve, 300));
  assert.equal((await send(id, message)).status, 204);
  const response = await waiting;
  assert.equal(response.status, 200);
  assert.deepEqual(await bytes(response), new Uint8Array(message));
  assert.ok(Date.now() - started < 10000);
});

test('a waiting read without a message ends with 204', async t => {
  const { create, read } = relay(t);
  const id = token(), phone = token();
  await create(id, phone);
  const started = Date.now();
  assert.equal((await read(id, phone, 1)).status, 204);
  assert.ok(Date.now() - started >= 900);
});

test('the box takes one message only', async t => {
  const { create, read, send } = relay(t);
  const id = token(), phone = token(), message = sealed();
  await create(id, phone);
  assert.equal((await send(id, message)).status, 204);
  assert.equal((await send(id, sealed())).status, 409);
  assert.deepEqual(await bytes(await read(id, phone)), new Uint8Array(message));
});

test('the message stays until the phone deletes the box', async t => {
  const { create, read, remove, send } = relay(t);
  const id = token(), phone = token(), message = sealed();
  await create(id, phone);
  await send(id, message);
  assert.equal((await read(id, phone)).status, 200);
  assert.equal((await read(id, phone)).status, 200);
  assert.equal((await remove(id, phone)).status, 204);
  assert.equal((await read(id, phone)).status, 404);
  assert.equal((await send(id, sealed())).status, 404);
  assert.equal((await create(id, token())).status, 201);
});

test('only the phone that made the box can read or delete it', async t => {
  const { create, read, remove, send } = relay(t);
  const id = token(), phone = token(), other = token();
  assert.equal((await create(id, phone)).status, 201);
  assert.equal((await create(id, phone)).status, 200);
  assert.equal((await create(id, other)).status, 409);
  await send(id, sealed());
  assert.equal((await read(id, other)).status, 401);
  assert.equal((await remove(id, other)).status, 401);
  assert.equal((await read(id, undefined)).status, 401);
  assert.equal((await read(id, phone)).status, 200);
});

test('a message without a box is refused', async t => {
  const { send } = relay(t);
  assert.equal((await send(token(), sealed())).status, 404);
});

test('the box expires', async t => {
  const { create, read, send } = relay(t, { seconds: 1 });
  const id = token(), phone = token(), late = token();
  await create(id, phone);
  await send(id, sealed());
  await create(late, phone);
  await new Promise(resolve => setTimeout(resolve, 1200));
  assert.equal((await read(id, phone)).status, 404);
  assert.equal((await send(late, sealed())).status, 404);
});

test('a waiting read ends when the box expires', async t => {
  const { create, read } = relay(t, { seconds: 1 });
  const id = token(), phone = token();
  await create(id, phone);
  const started = Date.now();
  assert.equal((await read(id, phone, 25)).status, 404);
  assert.ok(Date.now() - started < 5000);
});

test('the message size must fit a sealed setup', async t => {
  const { create, send } = relay(t);
  const id = token();
  await create(id, token());
  assert.equal((await send(id, sealed(28))).status, 400);
  assert.equal((await send(id, sealed(4097))).status, 413);
  assert.equal((await send(id, sealed(4096))).status, 204);
});

test('requests are rate limited for each IP', async t => {
  const created = relay(t, { create: 1 });
  assert.equal((await created.create(token(), token())).status, 201);
  assert.equal((await created.create(token(), token())).status, 429);
  const other = relay(t, { limit: 1 });
  const id = token(), phone = token();
  await other.create(id, phone);
  assert.equal((await other.read(id, phone)).status, 204);
  assert.equal((await other.read(id, phone)).status, 429);
  assert.equal((await other.send(id, sealed())).status, 429);
});

test('invalid paths, methods and credentials are refused', async t => {
  const { create } = relay(t);
  assert.equal((await create(token(), undefined)).status, 401);
  const env = {};
  const call = (path, init) => worker.fetch(new Request(`https://relay.test${path}`, init), env);
  assert.equal((await call(`/v1/rendezvous/${'A'.repeat(64)}`, { method: 'PUT' })).status, 404);
  assert.equal((await call(`/v1/rendezvous/${token()}`, { method: 'POST' })).status, 405);
  assert.equal((await call(`/v1/rendezvous/${token()}/message`, { method: 'GET' })).status, 405);
  assert.equal((await call(`/v1/rendezvous/${token()}`, { method: 'GET', headers: { authorization: 'Bearer x' } })).status, 401);
});

test('the Worker sends rendezvous routes to the box of the ID', async () => {
  let forwarded;
  const env = {
    RENDEZVOUS_LIMIT: { limit: async () => ({ success: true }) },
    RENDEZVOUS: { idFromName: name => name, get: name => ({ fetch: input => { forwarded = [name, String(input)]; return new Response(null, { status: 418 }); } }) },
  };
  const id = token();
  const request = new Request(`https://relay.test/v1/rendezvous/${id}/message`, { method: 'POST', body: sealed(100) });
  assert.equal((await worker.fetch(request, env)).status, 418);
  assert.deepEqual(forwarded, [id, 'https://rendezvous/message']);
});
