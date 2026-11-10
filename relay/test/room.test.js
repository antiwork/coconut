import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash, generateKeyPairSync, randomBytes, sign } from 'node:crypto';
import worker from '../src/worker.js';
import { roomRuntime } from './room-runtime.mjs';

const hash = value => createHash('sha256').update(value).digest('hex');
const token = () => randomBytes(32).toString('hex');
const sealed = (size = 120) => randomBytes(size);
const entry = (size, kept = false) => ({ id: randomBytes(16).toString('hex'), kept, body: sealed(size) });
// A Mac of a test: its member key, its member ID (the public key), and its member token.
const mac = () => {
  const { publicKey, privateKey } = generateKeyPairSync('ed25519');
  return { member: publicKey.export({ format: 'der', type: 'spki' }).subarray(-32).toString('hex'), token: token(), key: privateKey };
};
// What a Mac sends to make or join a room. The proof is its signature over the room ID and the hash of its token.
const claim = (who, room, signer = who) => {
  const digest = hash(who.token);
  const signed = Buffer.concat([Buffer.from('coconut room member v1'), Buffer.from(room, 'hex'), Buffer.from(digest, 'hex')]);
  return { member: who.member, hash: digest, proof: sign(null, signed, signer.key).toString('base64') };
};
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

function relay(t, options) {
  const runtime = roomRuntime(options);
  t.after(() => runtime.dispose());
  const call = (method, path, { bearer, ticket, body, headers = {}, text } = {}) => {
    const authorization = bearer ? `Bearer ${bearer}` : ticket ? `Ticket ${ticket}` : undefined;
    return runtime.dispatchFetch(`https://relay.test/v1/rooms/${path}`, {
      method, headers: { ...headers, ...(authorization ? { authorization } : {}) },
      body: text ?? (body === undefined ? undefined : JSON.stringify(body)),
    });
  };
  return { call };
}

// Wraps a socket so that a test can await its next message or its close.
function socket(ws) {
  const messages = [], waiters = [];
  let closed;
  const wake = () => { for (const check of waiters.splice(0)) check(); };
  ws.addEventListener('message', event => { messages.push(event.data); wake(); });
  ws.addEventListener('close', event => { closed = { code: event.code, reason: event.reason }; wake(); });
  ws.accept();
  const until = (condition, wait) => new Promise((resolve, reject) => {
    const origin = new Error('Timed out');
    const timer = setTimeout(() => reject(origin), wait);
    const check = () => (condition() ? (clearTimeout(timer), resolve()) : waiters.push(check));
    check();
  });
  return {
    status: 101,
    send: data => ws.send(data),
    async next() {
      await until(() => messages.length || closed, 5000);
      if (!messages.length) throw new Error(`Closed with ${closed.code}`);
      return messages.shift();
    },
    async seq() { return JSON.parse(await this.next()).seq; },
    // Workerd delivers a close that another socket's event starts about 10 seconds late.
    async closed() { await until(() => closed, 15000); return closed; },
  };
}

// Makes a room with its admin, and gives the calls of a Mac.
async function room(t, options) {
  const { call } = relay(t, options);
  const id = token(), admin = mac();
  const make = (who, room = id) => call('PUT', room, { body: claim(who, room) });
  assert.equal((await make(admin)).status, 201);
  const invite = async ({ uses = 1, ttl = 600, welcome = sealed(60), by = admin } = {}) => {
    const ticket = token();
    const response = await call('POST', `${id}/tickets`, {
      bearer: by.token, body: { hash: hash(ticket), ttl, uses, welcome: welcome.toString('base64') },
    });
    return { ticket, welcome, status: response.status };
  };
  const join = async (ticket, who = mac()) => {
    const response = await call('POST', `${id}/members`, { ticket, body: claim(who, id) });
    const welcome = response.status === 200 ? Buffer.from((await response.json()).welcome, 'base64') : null;
    return { who, status: response.status, welcome };
  };
  const member = async () => (await join((await invite()).ticket)).who;
  const write = (who, { id: entryId, kept, body }) => call('POST', `${id}/entries`, {
    bearer: who.token, body: { id: entryId, kept, body: body.toString('base64') },
  });
  const read = async (who, after = 0) => {
    const response = await call('GET', `${id}/entries?after=${after}`, { bearer: who.token });
    return response.status === 200 ? { status: 200, headers: response.headers, ...(await response.json()) } : { status: response.status };
  };
  const members = async who => (await (await call('GET', `${id}/members`, { bearer: who.token })).json()).members;
  const remove = (who, target) => call('DELETE', `${id}/members/${target.member}`, { bearer: who.token });
  const connect = async who => {
    const response = await call('GET', `${id}/socket`, { bearer: who.token, headers: { upgrade: 'websocket' } });
    return response.webSocket ? socket(response.webSocket) : { status: response.status };
  };
  return { id, admin, call, make, invite, join, member, write, read, members, remove, connect };
}

test('the first Mac makes a room and is its admin', async t => {
  const { id, admin, call, make, members, read } = await room(t);
  assert.equal((await make(admin)).status, 200);
  assert.equal((await make(mac())).status, 409);
  const list = await members(admin);
  assert.equal(list.length, 1);
  assert.equal(list[0].member, admin.member);
  assert.equal(list[0].admin, true);
  assert.ok(Math.abs(list[0].joined - Date.now()) < 60000);
  assert.deepEqual(await read(admin).then(({ entries, last }) => ({ entries, last })), { entries: [], last: 0 });
  assert.equal((await read(mac())).status, 401);
  // A room that nobody made answers 404 on each route.
  const missing = token();
  assert.equal((await call('GET', `${missing}/entries`, { bearer: admin.token })).status, 404);
  assert.equal((await call('POST', `${missing}/members`, { ticket: token(), body: claim(mac(), missing) })).status, 404);
  assert.equal((await call('GET', `${missing}/socket`, { bearer: admin.token, headers: { upgrade: 'websocket' } })).status, 404);
  assert.equal((await call('DELETE', missing, { bearer: admin.token })).status, 404);
  assert.equal((await call('PUT', token(), { body: { member: 'x', hash: hash('y') } })).status, 400);
  assert.equal((await call('PUT', token(), { text: 'no json' })).status, 400);
});

test('a member reads the entries that another member writes', async t => {
  const { admin, member, write, read } = await room(t);
  const first = entry(120), other = await member();
  const written = await write(admin, first);
  assert.equal(written.status, 200);
  assert.equal(written.headers.get('cache-control'), 'no-store');
  assert.deepEqual(await written.json(), { seq: 1, first: 1 });
  const { status, headers, entries, last } = await read(other);
  assert.equal(status, 200);
  assert.equal(headers.get('cache-control'), 'no-store');
  assert.equal(last, 1);
  assert.equal(entries.length, 1);
  const { at, body, ...fields } = entries[0];
  assert.deepEqual(fields, { seq: 1, first: 1, id: first.id, member: admin.member, kept: false });
  assert.ok(Math.abs(at - Date.now()) < 60000);
  assert.deepEqual(Buffer.from(body, 'base64'), first.body);
  assert.deepEqual(await read(other, 1).then(({ entries, last }) => ({ entries, last })), { entries: [], last: 1 });
});

test('an entry of the largest size comes back unchanged', async t => {
  const { admin, write, read } = await room(t);
  const large = entry(1024 * 1024, true);
  assert.equal((await write(admin, large)).status, 200);
  const { entries } = await read(admin);
  assert.ok(Buffer.from(entries[0].body, 'base64').equals(large.body));
  assert.equal((await write(admin, entry(1024 * 1024 + 1))).status, 413);
  assert.equal((await write(admin, entry(3 * 1024 * 1024))).status, 413);
});

test('a write with the same entry ID replaces the entry, and the entry keeps its place', async t => {
  const { admin, write, read } = await room(t);
  const message = entry(120), next = entry(130);
  await write(admin, message);
  await write(admin, next);
  const edited = { id: message.id, kept: true, body: sealed(140) };
  assert.deepEqual(await (await write(admin, edited)).json(), { seq: 3, first: 1 });
  const { entries, last } = await read(admin);
  assert.equal(last, 3);
  assert.deepEqual(entries.map(({ seq, first, id, kept }) => ({ seq, first, id, kept })), [
    { seq: 2, first: 2, id: next.id, kept: false }, { seq: 3, first: 1, id: message.id, kept: true },
  ]);
  assert.deepEqual(Buffer.from(entries[1].body, 'base64'), edited.body);
  // A Mac that already read the first two entries gets only the replacement.
  assert.deepEqual((await read(admin, 2)).entries.map(({ seq }) => seq), [3]);
});

test('only its own member replaces an entry', async t => {
  const { admin, member, write, read } = await room(t);
  const message = entry(120), other = await member();
  await write(admin, message);
  assert.equal((await write(other, { ...message, body: sealed(120) })).status, 409);
  const { entries } = await read(other);
  assert.equal(entries.length, 1);
  assert.deepEqual(Buffer.from(entries[0].body, 'base64'), message.body);
  assert.equal(entries[0].member, admin.member);
});

test('a ticket admits as many Macs as it has uses', async t => {
  const { id, admin, call, invite, join, members } = await room(t);
  const { ticket, welcome, status } = await invite({ uses: 2 });
  assert.equal(status, 204);
  const first = await join(ticket);
  assert.equal(first.status, 200);
  assert.deepEqual(first.welcome, welcome);
  assert.equal((await join(ticket)).status, 200);
  assert.equal((await join(ticket)).status, 401);
  // A Mac that lost the answer asks again. It gets the welcome, and the ticket loses no use.
  const again = await join(ticket, first.who);
  assert.equal(again.status, 200);
  assert.deepEqual(again.welcome, welcome);
  assert.equal((await join(ticket, { ...first.who, token: token() })).status, 409);
  assert.equal((await join(token())).status, 401);
  assert.equal((await members(admin)).length, 3);
  // A member token of another Mac cannot be the token of a new member.
  const second = await invite();
  assert.equal((await join(second.ticket, { ...mac(), token: first.who.token })).status, 409);
  // The join needs a ticket, and each other route needs a member token.
  assert.equal((await call('POST', `${id}/members`, { bearer: admin.token, body: claim(mac(), id) })).status, 401);
  assert.equal((await call('GET', `${id}/members`, { ticket: second.ticket })).status, 401);
  assert.equal((await call('POST', `${id}/members`, { ticket: second.ticket, body: { member: 'x', hash: 'y' } })).status, 400);
});

test('only the admin makes and removes a ticket', async t => {
  const { id, admin, call, invite, join, member } = await room(t, { limits: { tickets: 2 } });
  const other = await member();
  assert.equal((await invite({ by: other })).status, 403);
  const kept = await invite(), removed = await invite();
  // The ticket that `member` used has no use left, so it is not an open ticket.
  assert.equal((await invite()).status, 409);
  assert.equal((await call('DELETE', `${id}/tickets/${hash(removed.ticket)}`, { bearer: other.token })).status, 403);
  assert.equal((await call('DELETE', `${id}/tickets/${hash(removed.ticket)}`, { bearer: admin.token })).status, 204);
  assert.equal((await join(removed.ticket)).status, 401);
  assert.equal((await invite()).status, 204);
  assert.equal((await join(kept.ticket)).status, 200);
});

test('a ticket must have a valid life, a valid number of uses, and a sealed welcome', async t => {
  const { invite, join } = await room(t, { limits: { minTtl: 1 } });
  assert.equal((await invite({ ttl: 0 })).status, 400);
  assert.equal((await invite({ ttl: 7 * 86400 + 1 })).status, 400);
  assert.equal((await invite({ ttl: 1.5 })).status, 400);
  assert.equal((await invite({ uses: 0 })).status, 400);
  assert.equal((await invite({ uses: 65 })).status, 400);
  assert.equal((await invite({ welcome: sealed(28) })).status, 400);
  assert.equal((await invite({ welcome: sealed(1025) })).status, 413);
  assert.equal((await invite({ welcome: { toString: () => 'not base64!' } })).status, 400);
  assert.equal((await invite({ welcome: sealed(1024), ttl: 7 * 86400, uses: 64 })).status, 204);
  const short = await invite({ ttl: 1 });
  await pause(1200);
  assert.equal((await join(short.ticket)).status, 401);
});

test('the admin removes a member, and a member can leave', async t => {
  const { id, admin, call, member, write, read, members, remove } = await room(t);
  const removed = await member(), leaver = await member(), other = await member();
  const page = entry(200, true), message = entry(120);
  await write(removed, page);
  await write(removed, message);
  assert.equal((await remove(other, removed)).status, 403);
  assert.equal((await remove(admin, removed)).status, 204);
  assert.equal((await remove(admin, removed)).status, 204);
  assert.equal((await read(removed)).status, 401);
  assert.equal((await write(removed, entry(120))).status, 401);
  // The kept entries of the removed member go. Its other entries stay until the retention time ends.
  assert.deepEqual((await read(admin)).entries.map(({ id: entryId }) => entryId), [message.id]);
  assert.equal((await remove(leaver, leaver)).status, 204);
  assert.deepEqual((await members(admin)).map(({ member: memberId }) => memberId).sort(), [admin.member, other.member].sort());
  // The admin leaves only a room without other members, and the room then goes.
  assert.equal((await remove(admin, admin)).status, 409);
  assert.equal((await remove(admin, other)).status, 204);
  assert.equal((await remove(admin, admin)).status, 204);
  assert.equal((await call('GET', `${id}/members`, { bearer: admin.token })).status, 404);
});

test('only the admin deletes the room', async t => {
  const { id, admin, call, make, member, write, read } = await room(t);
  const other = await member();
  await write(admin, entry(120));
  assert.equal((await call('DELETE', id, { bearer: other.token })).status, 403);
  assert.equal((await call('DELETE', id, { bearer: admin.token })).status, 204);
  assert.equal((await read(admin)).status, 404);
  // The ID is free again, and the new room is empty.
  assert.equal((await make(other)).status, 201);
  assert.deepEqual(await read(other).then(({ entries, last }) => ({ entries, last })), { entries: [], last: 0 });
});

test('an entry must have a valid ID, a kept flag, and a sealed body', async t => {
  const { id, admin, call, write } = await room(t);
  const post = body => call('POST', `${id}/entries`, { bearer: admin.token, body });
  const valid = entry(99);
  assert.equal((await write(admin, valid)).status, 200);
  assert.equal((await write(admin, entry(98))).status, 400);
  const body = valid.body.toString('base64');
  assert.equal((await post({ id: 'A'.repeat(32), kept: false, body })).status, 400);
  assert.equal((await post({ id: token(), kept: false, body })).status, 400);
  assert.equal((await post({ id: valid.id, kept: 1, body })).status, 400);
  assert.equal((await post({ id: valid.id, kept: false, body: `${body.slice(0, -4)}!!!!` })).status, 400);
  assert.equal((await post({ id: valid.id, kept: false })).status, 400);
  assert.equal((await post([valid.id])).status, 400);
  assert.equal((await call('GET', `${id}/entries?after=-1`, { bearer: admin.token })).status, 400);
  assert.equal((await call('GET', `${id}/entries?after=x`, { bearer: admin.token })).status, 400);
});

test('a member keeps a limited number of entries and bytes', async t => {
  const { admin, member, write } = await room(t, { limits: { keptEntries: 2, keptBytes: 400, otherBytes: 700 } });
  const first = entry(120, true), second = entry(120, true);
  assert.equal((await write(admin, first)).status, 200);
  assert.equal((await write(admin, second)).status, 200);
  assert.equal((await write(admin, entry(120, true))).status, 507);
  // A replacement does not count twice, and an entry that is not kept does not count.
  assert.equal((await write(admin, { ...first, body: sealed(200) })).status, 200);
  assert.equal((await write(admin, entry(300))).status, 200);
  assert.equal((await write(admin, { ...first, body: sealed(300) })).status, 507);
  // An entry that is no longer kept gives its place back.
  assert.equal((await write(admin, { ...second, kept: false })).status, 200);
  assert.equal((await write(admin, entry(120, true))).status, 200);
  // The entries that are not kept have their own limit of bytes: 300 and 120 are in use now.
  const plain = entry(250);
  assert.equal((await write(admin, plain)).status, 200);
  assert.equal((await write(admin, entry(120))).status, 507);
  assert.equal((await write(admin, { ...plain, body: sealed(130) })).status, 200);
  assert.equal((await write(admin, entry(120))).status, 200);
  // Each limit is for one member.
  const other = await member();
  assert.equal((await write(other, entry(120, true))).status, 200);
  assert.equal((await write(other, entry(600))).status, 200);
});

test('a member that joins again keeps its limits', async t => {
  const { admin, invite, join, write, read, members, remove } = await room(t, { limits: { otherBytes: 300, writesPerMinute: 4 } });
  const { ticket } = await invite({ uses: 8 });
  let { who } = await join(ticket);
  const again = async () => {
    assert.equal((await remove(who, who)).status, 204);
    assert.ok(!(await members(admin)).some(({ member: memberId }) => memberId === who.member));
    who = { ...who, token: token() };
    assert.equal((await join(ticket, who)).status, 200);
  };
  // The entries that are not kept stay when the member leaves, and they still count after it joins again.
  const message = entry(250);
  assert.equal((await write(who, message)).status, 200);
  await again();
  assert.equal((await write(who, entry(120))).status, 507);
  assert.equal((await read(admin)).entries.length, 1);
  // The count of the writes continues too. Nine writes cross at most one minute boundary, so one minute has at
  // least five of them, and no part between two joins has more than three. This holds also for a member that has
  // only kept entries, whose row has no bytes that keep it.
  for (const replaced of [message, entry(100, true)]) {
    const statuses = [];
    for (let part = 0; part < 3; part += 1) {
      for (let index = 0; index < 3; index += 1) statuses.push((await write(who, { ...replaced, body: sealed(100) })).status);
      if (part < 2) await again();
    }
    assert.ok(statuses.includes(429), `${replaced.kept ? 'kept' : 'other'} entries: ${statuses}`);
    ({ who } = await join(ticket));
  }
});

test('a room holds a limited number of bytes', async t => {
  const { admin, member, write, remove } = await room(t, { limits: { roomBytes: 500 } });
  const other = await member(), page = entry(200, true), message = entry(250);
  assert.equal((await write(admin, message)).status, 200);
  assert.equal((await write(other, page)).status, 200);
  assert.equal((await write(other, entry(120))).status, 507);
  assert.equal((await write(admin, entry(120, true))).status, 507);
  // A write that adds no bytes fits in a full room, and it makes room for another write.
  assert.equal((await write(other, { ...page, body: sealed(100) })).status, 200);
  assert.equal((await write(other, entry(120))).status, 200);
  // The entries that a removed member leaves still count.
  assert.equal((await remove(admin, other)).status, 204);
  assert.equal((await write(admin, entry(140))).status, 507);
  assert.equal((await write(admin, entry(120))).status, 200);
});

test('a member writes a limited number of entries each minute', async t => {
  const { admin, member, write } = await room(t, { limits: { writesPerMinute: 3 } });
  // Seven writes cross at most one minute boundary, so one minute has at least four of them.
  const statuses = [];
  for (let index = 0; index < 7; index += 1) statuses.push((await write(admin, entry(120))).status);
  assert.ok(statuses.includes(429));
  assert.deepEqual(statuses.slice(0, 3), [200, 200, 200]);
  assert.equal((await write(await member(), entry(120))).status, 200);
});

test('one answer holds a limited number of entries and bytes', async t => {
  const { admin, write, read } = await room(t, { limits: { readEntries: 3, readBytes: 250 } });
  const written = [entry(120), entry(120), entry(120), entry(300), entry(100), entry(100), entry(100), entry(100)];
  for (const item of written) await write(admin, item);
  // Two entries of 120 bytes fit in 250 bytes. An entry over the limit comes alone. Then three entries are the limit.
  const pages = [];
  for (let after = 0, last = 1; after < last;) {
    const answer = await read(admin, after);
    assert.equal(answer.last, 8);
    pages.push(answer.entries.map(({ seq }) => seq));
    after = answer.entries.at(-1).seq;
    last = answer.last;
  }
  assert.deepEqual(pages, [[1, 2], [3], [4], [5, 6], [7, 8]]);
});

test('the socket tells each member about a write', async t => {
  const { id, admin, call, member, write, connect } = await room(t);
  const other = await member();
  await write(admin, entry(120));
  const adminSocket = await connect(admin), otherSocket = await connect(other);
  assert.equal(await adminSocket.seq(), 1);
  assert.equal(await otherSocket.seq(), 1);
  // The writer has the number in its answer, so only the other members get it on the socket.
  await write(other, entry(120));
  assert.equal(await adminSocket.seq(), 2);
  await write(admin, entry(120));
  assert.equal(await otherSocket.seq(), 3);
  await write(other, entry(120));
  assert.equal(await adminSocket.seq(), 4);
  adminSocket.send('ping');
  assert.equal(await adminSocket.next(), 'pong');
  assert.equal((await connect(mac())).status, 401);
  assert.equal((await call('GET', `${id}/socket`, { bearer: admin.token })).status, 426);
  // A member sends nothing but `ping`.
  otherSocket.send('hello');
  assert.equal((await otherSocket.closed()).code, 1003);
});

test('the relay closes the socket of a replaced, removed or deleted member', async t => {
  const { id, admin, call, member, remove, connect } = await room(t);
  const other = await member();
  const replaced = await connect(admin), removed = await connect(other);
  await replaced.seq();
  await removed.seq();
  const current = await connect(admin);
  assert.equal(await current.seq(), 0);
  assert.equal((await remove(admin, other)).status, 204);
  assert.equal((await call('DELETE', id, { bearer: admin.token })).status, 204);
  const [first, second, third] = await Promise.all([replaced.closed(), removed.closed(), current.closed()]);
  assert.deepEqual([first.code, second.code, third.code], [1012, 1008, 1000]);
});

test('the relay deletes an old entry that is not kept, and keeps the others', async t => {
  const { admin, write, read } = await room(t, { limits: { retentionSeconds: 1, sweepSeconds: 1, otherBytes: 200 } });
  const message = entry(120), page = entry(120, true);
  await write(admin, message);
  await write(admin, page);
  assert.equal((await write(admin, entry(120))).status, 507);
  // The deleted entry gives its bytes back to the member.
  await pause(3200);
  const late = entry(120);
  assert.deepEqual(await (await write(admin, late)).json(), { seq: 3, first: 3 });
  const { entries, last } = await read(admin);
  assert.deepEqual(entries.map(({ id: entryId }) => entryId), [page.id, late.id]);
  assert.equal(last, 3);
});

test('the relay keeps a number for one entry only, also after it deletes the newest entry', async t => {
  const { admin, write, read } = await room(t, { limits: { retentionSeconds: 1, sweepSeconds: 1 } });
  await write(admin, entry(120, true));
  await write(admin, entry(120));
  await pause(3200);
  // The newest entry is gone, so `last` is the kept one. A Mac that read to 2 has nothing more to read.
  assert.deepEqual(await read(admin, 2).then(({ entries, last }) => ({ entries, last })), { entries: [], last: 1 });
  assert.deepEqual(await (await write(admin, entry(120))).json(), { seq: 3, first: 3 });
});

test('the relay deletes a room that no member uses', async t => {
  const { admin, read, make } = await room(t, { limits: { idleSeconds: 1, sweepSeconds: 1 } });
  await pause(3200);
  assert.equal((await read(admin)).status, 404);
  assert.equal((await make(admin)).status, 201);
});

test('one IP address makes a limited number of rooms', async t => {
  const hourly = relay(t, { limits: { creationsPerHour: 2 } });
  const make = (calls, who = mac(), room = token()) => calls.call('PUT', room, { body: claim(who, room) });
  const first = mac(), room = token();
  assert.equal((await make(hourly, first, room)).status, 201);
  assert.equal((await make(hourly)).status, 201);
  const refused = await make(hourly);
  assert.equal(refused.status, 429);
  assert.equal(refused.headers.get('retry-after'), '3600');
  // A repeat makes no room, so it does not count, and it gets its answer also after the limit.
  assert.equal((await make(hourly, first, room)).status, 200);
  assert.equal((await make(hourly, mac(), room)).status, 409);
  const minute = relay(t, { create: 1 });
  assert.equal((await make(minute)).status, 201);
  assert.equal((await make(minute)).status, 429);
});

test('the other requests are rate limited for each IP', async t => {
  const { id, admin, call } = await room(t, { requests: 2 });
  const statuses = [];
  for (let index = 0; index < 3; index += 1) statuses.push((await call('GET', `${id}/members`, { bearer: admin.token })).status);
  assert.deepEqual(statuses, [200, 200, 429]);
});

test('invalid paths, methods and credentials are refused', async () => {
  const env = {};
  const call = (path, init) => worker.fetch(new Request(`https://relay.test${path}`, init), env);
  const id = token(), bearer = { authorization: `Bearer ${token()}` };
  assert.equal((await call(`/v1/rooms/${'A'.repeat(64)}`, { method: 'PUT' })).status, 404);
  assert.equal((await call(`/v1/rooms/${id}`, { method: 'POST' })).status, 405);
  assert.equal((await call(`/v1/rooms/${id}/entries`, { method: 'DELETE', headers: bearer })).status, 405);
  assert.equal((await call(`/v1/rooms/${id}/tickets`, { method: 'GET', headers: bearer })).status, 405);
  assert.equal((await call(`/v1/rooms/${id}/entries/${token()}`, { method: 'GET', headers: bearer })).status, 404);
  assert.equal((await call(`/v1/rooms/${id}/socket/${token()}`, { method: 'GET', headers: bearer })).status, 404);
  assert.equal((await call(`/v1/rooms/${id}/members`, { method: 'GET' })).status, 401);
  assert.equal((await call(`/v1/rooms/${id}/members`, { method: 'GET', headers: { authorization: 'Bearer x' } })).status, 401);
});

test('a Mac must prove that it has the key of its member ID', async t => {
  const { id, admin, call, make, invite, join, member, write, read, remove } = await room(t);
  const who = mac(), other = mac(), { ticket } = await invite({ uses: 8 });
  const post = body => call('POST', `${id}/members`, { ticket, body });
  // A proof of another key, for another room, or for another token does not count.
  assert.equal((await post(claim(who, id, other))).status, 401);
  assert.equal((await post(claim(who, token()))).status, 401);
  assert.equal((await post({ ...claim(who, id), hash: hash(token()) })).status, 401);
  assert.equal((await post({ ...claim(who, id), member: token() })).status, 401);
  assert.equal((await post({ ...claim(who, id), proof: 'AAAA' })).status, 400);
  const valid = claim(who, id);
  assert.equal((await post({ ...valid, member: [valid.member] })).status, 400);
  assert.equal((await post({ ...valid, hash: [valid.hash] })).status, 400);
  assert.equal((await post(valid)).status, 200);
  const fresh = token();
  assert.equal((await call('PUT', fresh, { body: claim(who, fresh, other) })).status, 401);
  assert.equal((await call('PUT', fresh, { body: { ...claim(who, fresh), member: [who.member] } })).status, 400);
  assert.equal((await make(who, fresh)).status, 201);
  // A removed member leaves entries that are not kept. Another Mac cannot take its ID and replace them.
  const removed = await member(), message = entry(120);
  await write(removed, message);
  await remove(admin, removed);
  assert.equal((await join(ticket, { ...removed, token: token(), key: other.key })).status, 401);
  // The removed Mac itself can join again, and then the entry is still its own.
  const again = { ...removed, token: token() };
  assert.equal((await join(ticket, again)).status, 200);
  assert.equal((await write(again, { ...message, body: sealed(130) })).status, 200);
  assert.equal((await read(admin)).entries.length, 1);
});

test('the Worker sends room routes to the room of the ID', async () => {
  const forwarded = [];
  const env = {
    ROOM_LIMIT: { limit: async () => ({ success: true }) },
    ROOMS: {
      idFromName: name => name,
      get: name => ({ fetch: (input, init) => { forwarded.push([name, String(input), init.headers]); return new Response(null, { status: 418 }); } }),
    },
  };
  const id = token(), secret = token();
  // A header of the caller never names the member: the Worker sets the hash of the token itself.
  const request = new Request(`https://relay.test/v1/rooms/${id}/entries?after=7`, {
    headers: { authorization: `Bearer ${secret}`, 'x-hash': hash('another token') },
  });
  assert.equal((await worker.fetch(request, env)).status, 418);
  assert.deepEqual(forwarded, [[id, 'https://room/read?after=7', { 'x-hash': hash(secret) }]]);
});
