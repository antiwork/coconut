import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';
import worker from '../src/worker.js';

const hash = value => createHash('sha256').update(value).digest('hex');
const token = () => randomBytes(32).toString('hex');
const frame = (channel, bytes) => {
  const data = new Uint8Array(4 + bytes.length);
  new DataView(data.buffer).setUint32(0, channel);
  data.set(bytes, 4);
  return data;
};
const control = value => frame(0, new TextEncoder().encode(JSON.stringify(value)));

async function relay(t, limits = { register: 100, connect: 1000 }) {
  const runtime = new Miniflare(convertV4MiniflareOptions({
    compatibilityDate: '2026-09-10', compatibilityFlags: ['nodejs_compat'],
    modules: [
      { type: 'ESModule', path: 'entry.js', contents: `
        import { handleTunnel } from './tunnel.js';
        export { Tunnel } from './tunnel.js';
        export default { async fetch(request, env) {
          return (await handleTunnel(request, env)) || new Response(null, { status: 404 });
        }};` },
      { type: 'ESModule', path: 'tunnel.js', contents: readFileSync(new URL('../src/tunnel.js', import.meta.url), 'utf8') },
    ],
    durableObjects: { TUNNELS: { className: 'Tunnel', useSQLite: true } },
    ratelimits: {
      TUNNEL_REGISTER: { namespace_id: '3', simple: { limit: limits.register, period: 60 } },
      TUNNEL_CONNECT: { namespace_id: '4', simple: { limit: limits.connect, period: 60 } },
    },
  }));
  t.after(() => runtime.dispose());
  const register = route => runtime.dispatchFetch('https://relay.test/v1/tunnels', {
    method: 'POST', body: JSON.stringify({ route }),
  });
  const connect = async (route, role, authorization) => {
    const response = await runtime.dispatchFetch(`https://relay.test/v1/tunnels/${route}/${role}`, {
      headers: { upgrade: 'websocket', authorization },
    });
    if (!response.webSocket) return { status: response.status };
    return socket(response.webSocket);
  };
  return { runtime, register, connect };
}

// Wraps a socket so that a test can await its next message or its close.
function socket(ws) {
  const messages = [], waiters = [];
  let closed;
  const wake = () => { for (const check of waiters.splice(0)) check(); };
  ws.addEventListener('message', event => {
    messages.push(typeof event.data === 'string' ? event.data : new Uint8Array(event.data)); wake();
  });
  ws.addEventListener('close', event => { closed = { code: event.code, reason: event.reason }; wake(); });
  ws.accept();
  const until = (condition, wait = 5000) => new Promise((resolve, reject) => {
    const origin = new Error('Timed out');
    const timer = setTimeout(() => reject(origin), wait);
    const check = () => condition() ? (clearTimeout(timer), resolve()) : waiters.push(check);
    check();
  });
  return {
    status: 101, ws,
    send: data => ws.send(data),
    async next() {
      await until(() => messages.length || closed);
      if (!messages.length) throw new Error(`Closed with ${closed.code}`);
      return messages.shift();
    },
    async control() {
      const data = await this.next();
      assert.equal(new DataView(data.buffer, data.byteOffset).getUint32(0), 0);
      return JSON.parse(new TextDecoder().decode(data.subarray(4)));
    },
    // Workerd delivers a close that another socket's event starts about 10 seconds late.
    async closed() { await until(() => closed, 15000); return closed; },
  };
}

// Waits until the relay has handled every earlier message from the host.
async function settle(host) {
  host.send(control({ type: 'barrier' }));
  assert.deepEqual(await host.control(), { type: 'error', code: 'invalid' });
}

// Registers a route, connects its host, and allows one client token.
async function tunnel(t, limits) {
  const r = await relay(t, limits);
  const route = token();
  const { host_token: hostToken } = await (await r.register(route)).json();
  const host = await r.connect(route, 'host', `Bearer ${hostToken}`);
  const clientToken = token();
  host.send(control({ type: 'add_client', hash: hash(clientToken) }));
  await settle(host);
  const client = async () => {
    const socket = await r.connect(route, 'client', `Bearer ${clientToken}`);
    const open = await host.control();
    assert.equal(open.type, 'open');
    assert.equal(open.auth, 'token');
    assert.equal(open.hash, hash(clientToken));
    socket.channel = open.id;
    return socket;
  };
  return { ...r, route, hostToken, host, clientToken, client };
}

test('a route registers once', async t => {
  const r = await relay(t);
  const route = token();
  const response = await r.register(route);
  assert.equal(response.status, 200);
  const { host_token: hostToken } = await response.json();
  assert.match(hostToken, /^[a-f0-9]{64}$/);
  assert.equal((await r.register(route)).status, 409);
  assert.equal((await r.register('not-a-route')).status, 400);
  assert.equal((await r.connect(route, 'host', `Bearer ${hostToken}`)).status, 101);
});

test('registration allows 10 routes an hour for each IP', async t => {
  const r = await relay(t);
  for (let i = 0; i < 10; i++) assert.equal((await r.register(token())).status, 200);
  assert.equal((await r.register(token())).status, 429);
});

test('wrong, missing, or misplaced credentials are refused', async t => {
  const { connect, route, hostToken } = await tunnel(t);
  assert.equal((await connect(route, 'host', `Bearer ${token()}`)).status, 401);
  assert.equal((await connect(route, 'host', 'Bearer short')).status, 401);
  assert.equal((await connect(route, 'host', `Ticket ${hostToken}`)).status, 401);
  assert.equal((await connect(route, 'client', `Bearer ${hostToken}`)).status, 401);
  assert.equal((await connect(route, 'client', `Bearer ${token()}`)).status, 401);
  assert.equal((await connect(token(), 'client', `Bearer ${token()}`)).status, 404);
  assert.equal((await connect(token(), 'host', `Bearer ${hostToken}`)).status, 404);
});

test('a newer host socket replaces the older one', async t => {
  const { connect, route, hostToken, host, client } = await tunnel(t);
  const phone = await client();
  const second = await connect(route, 'host', `Bearer ${hostToken}`);
  assert.equal((await host.closed()).code, 1000);
  assert.deepEqual(JSON.parse(await phone.next()), { type: 'error', code: 'host_reconnected' });
  assert.equal((await phone.closed()).code, 1012);
  const clientToken = token();
  second.send(control({ type: 'add_client', hash: hash(clientToken) }));
  await settle(second);
  const next = await connect(route, 'client', `Bearer ${clientToken}`);
  assert.equal((await second.control()).type, 'open');
  next.send(new Uint8Array([7]));
  assert.deepEqual([...(await second.next()).subarray(4)], [7]);
});

test('frames travel both ways with channel IDs', async t => {
  const { host, client } = await tunnel(t);
  const first = await client(), second = await client();
  assert.notEqual(first.channel, second.channel);
  first.send(new Uint8Array([1, 2, 3]));
  assert.deepEqual([...await host.next()], [...frame(first.channel, [1, 2, 3])]);
  second.send(new Uint8Array([4]));
  assert.deepEqual([...await host.next()], [...frame(second.channel, [4])]);
  host.send(frame(second.channel, [5, 6]));
  assert.deepEqual([...await second.next()], [5, 6]);
  host.send(frame(first.channel, [7]));
  assert.deepEqual([...await first.next()], [7]);
  host.send(frame(999, [8]));
  assert.deepEqual(await host.control(), { type: 'closed', id: 999 });
  host.send(control({ type: 'close_channel', id: first.channel }));
  assert.deepEqual(JSON.parse(await first.next()), { type: 'error', code: 'closed' });
  assert.equal((await first.closed()).code, 1000);
  first.ws.close();
  assert.deepEqual(await host.control(), { type: 'closed', id: first.channel });
});

test('pings get automatic replies', async t => {
  const { host, client } = await tunnel(t);
  const phone = await client();
  phone.send('ping');
  assert.equal(await phone.next(), 'pong');
  host.send('ping');
  assert.equal(await host.next(), 'pong');
});

test('a route allows at most 8 client channels', async t => {
  const { connect, route, host, clientToken, client } = await tunnel(t);
  const phones = [];
  for (let i = 0; i < 8; i++) phones.push(await client());
  assert.equal((await connect(route, 'client', `Bearer ${clientToken}`)).status, 429);
  phones[0].ws.close();
  await phones[0].closed();
  assert.deepEqual(await host.control(), { type: 'closed', id: phones[0].channel });
  assert.equal((await client()).status, 101);
});

test('a pairing ticket works once and expires', async t => {
  const { connect, route, host } = await tunnel(t);
  const ticket = token(), late = token();
  host.send(control({ type: 'add_ticket', hash: hash(ticket), ttl: 600 }));
  host.send(control({ type: 'add_ticket', hash: hash(late), ttl: 1 }));
  host.send(control({ type: 'add_ticket', hash: hash(token()), ttl: 601 }));
  assert.deepEqual(await host.control(), { type: 'error', code: 'invalid' });
  await settle(host);
  assert.equal((await connect(route, 'client', `Bearer ${ticket}`)).status, 401);
  assert.equal((await connect(route, 'client', `Ticket ${ticket}`)).status, 101);
  assert.deepEqual(await host.control(), { type: 'open', id: 1, auth: 'ticket', hash: hash(ticket) });
  assert.equal((await connect(route, 'client', `Ticket ${ticket}`)).status, 401);
  await new Promise(resolve => setTimeout(resolve, 1100));
  assert.equal((await connect(route, 'client', `Ticket ${late}`)).status, 401);
});

test('a removed client token can no longer connect and its channels close', async t => {
  const { connect, route, host, clientToken, client } = await tunnel(t);
  const phone = await client();
  host.send(control({ type: 'remove_client', hash: hash(clientToken) }));
  assert.deepEqual(JSON.parse(await phone.next()), { type: 'error', code: 'revoked' });
  assert.equal((await phone.closed()).code, 1008);
  assert.deepEqual(await host.control(), { type: 'closed', id: phone.channel });
  assert.equal((await connect(route, 'client', `Bearer ${clientToken}`)).status, 401);
});

test('a frame over 70 KB closes the socket with 1009', async t => {
  const { host, client } = await tunnel(t);
  const phone = await client();
  phone.send(new Uint8Array(70 * 1024));
  assert.equal((await host.next()).length, 4 + 70 * 1024);
  phone.send(new Uint8Array(70 * 1024 + 1));
  assert.equal((await phone.closed()).code, 1009);
  host.send(frame(0, new Uint8Array(70 * 1024)));
  assert.equal((await host.closed()).code, 1009);
});

test('clients get mac_offline when no host is connected', async t => {
  const r = await relay(t);
  const route = token();
  const { host_token: hostToken } = await (await r.register(route)).json();
  const host = await r.connect(route, 'host', `Bearer ${hostToken}`);
  const ticket = token(), clientToken = token();
  host.send(control({ type: 'add_ticket', hash: hash(ticket), ttl: 60 }));
  host.send(control({ type: 'add_client', hash: hash(clientToken) }));
  await settle(host);
  const phone = await r.connect(route, 'client', `Ticket ${ticket}`);
  await host.control();
  host.ws.close(1000);
  assert.deepEqual(JSON.parse(await phone.next()), { type: 'error', code: 'mac_offline' });
  assert.equal((await phone.closed()).code, 4001);
  const late = await r.connect(route, 'client', `Bearer ${clientToken}`);
  assert.deepEqual(JSON.parse(await late.next()), { type: 'error', code: 'mac_offline' });
  assert.equal((await late.closed()).code, 4001);
});

test('a route allows 3,000 frames a minute', async t => {
  const { host, client } = await tunnel(t);
  const phone = await client();
  // The first test minute may end during the loop, so stay clear of a minute boundary.
  if (Date.now() % 60000 > 50000) await new Promise(resolve => setTimeout(resolve, 60000 - Date.now() % 60000));
  // The add_client and barrier control messages and the 2,998 frames below fill the minute's limit.
  for (let i = 0; i < 2998; i++) phone.send(new Uint8Array([1]));
  for (let i = 0; i < 2998; i++) await host.next();
  phone.send(new Uint8Array([2]));
  assert.equal((await phone.closed()).code, 1013);
});

test('connections are rate limited for each IP', async t => {
  const { connect, route, clientToken } = await tunnel(t, { register: 100, connect: 1 });
  assert.equal((await connect(route, 'client', `Bearer ${clientToken}`)).status, 429);
});

test('the push body cap does not apply to tunnel routes', async () => {
  let forwarded;
  const env = {
    TUNNEL_CONNECT: { limit: async () => ({ success: true }) },
    TUNNELS: { idFromName: name => name, get: () => ({ fetch: request => { forwarded = request; return new Response(null, { status: 418 }); } }) },
  };
  const request = new Request(`https://relay.test/v1/tunnels/${token()}/host`, {
    method: 'GET', headers: { upgrade: 'websocket', authorization: `Bearer ${token()}`, 'content-length': '30000' },
  });
  assert.equal((await worker.fetch(request, env)).status, 418);
  assert.equal(new URL(forwarded.url).pathname, '/host');
});
