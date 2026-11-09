import { close, hash, HEX64, readBytes, send } from './tunnel.js';

// A room holds the log of one team: sealed entries that each member Mac copies into its own store. The relay sees
// IDs, times and sizes, never content. docs/team-log.md is the contract: the body of an entry, the invite link, the
// rules of a Mac, and the test vectors. This file is the relay side of it.
//
// A member is one Mac. Its ID is its public Ed25519 key, and it sends `Authorization: Bearer <member token>`. The
// relay stores only the SHA-256 hash of a token or a ticket, as the tunnel does. `hash` below is such a hash.
// `proof` is the signature of the member key over the room ID and that hash. Without it, a Mac could take the ID of
// a removed member, and then replace the entries that this member left.
//
// - `PUT /v1/rooms/<id>` with `{member, hash, proof}` makes the room, and that member is its admin: 201. A repeat
//   with the same values answers 200, and a room of another admin answers 409.
// - `POST /v1/rooms/<id>/tickets` with `{hash, ttl, uses, welcome}`, admin only, allows `uses` joins for `ttl`
//   seconds: 204. `welcome` is the sealed room key in base64. 409 when the room has its limit of open tickets.
// - `DELETE /v1/rooms/<id>/tickets/<hash>`, admin only: 204.
// - `POST /v1/rooms/<id>/members` with `Authorization: Ticket <ticket>` and `{member, hash, proof}` adds a member:
//   200 with `{welcome}`, and the ticket loses one use. A repeat with the same values gets the welcome again without
//   a use, until the ticket expires, so a Mac that lost the answer can ask again. 401 for a ticket that is unknown,
//   expired or without a use left. 409 when the member exists with another token, or the room is full.
// - `GET /v1/rooms/<id>/members`: 200 with `{members: [{member, admin, joined}]}`.
// - `DELETE /v1/rooms/<id>/members/<member>`, by the admin or by that member: 204. The token stops, the socket
//   closes, and the kept entries of the member go. 409 when the member is the admin and the room has other members.
//   When the admin leaves a room without other members, the room goes.
// - `POST /v1/rooms/<id>/entries` with `{id, kept, body}` writes an entry: 200 with `{seq, first}`. A write with an
//   entry ID that exists replaces that entry, and only its own member can do that: 409 for another member. 413 for a
//   body over the limit, 429 over the write limit of the member, 507 when the entries of the member are full.
// - `GET /v1/rooms/<id>/entries?after=<seq>`: 200 with `{entries, last}`: the entries with a higher `seq`, in order,
//   up to a count and a size. `last` is the highest `seq` in the room.
// - `GET /v1/rooms/<id>/socket` as a WebSocket: the relay sends `{"seq": n}` when the socket opens and after each
//   write of another member. `ping` gets the automatic reply `pong`, without waking the Durable Object. A long poll
//   would keep the object in memory, and Cloudflare bills that time. A socket that hibernates has no such cost.
// - `DELETE /v1/rooms/<id>`, admin only: 204 deletes the room with all its data.
// - A missing room answers 404. A token that is not a member's and a proof that fails answer 401. A member that is
//   not the admin gets 403 on a route of the admin.
// - Close codes: 1000 when the room goes, 1003 for a message other than `ping`, 1008 for a removed member, 1012 when
//   the same member opens a newer socket.
//
// The Worker checks each request and then calls the object: the operation is the path, its values are the query,
// the hash of the caller's token or ticket is `x-hash`, and bytes are the body.

// Tests set smaller values with the ROOM_LIMITS binding, as JSON. The deployed relay does not set it.
const LIMITS = {
  members: 64, tickets: 8, minTtl: 60, maxTtl: 7 * 86400, uses: 64, welcome: 1024,
  minBody: 99, maxBody: 1024 * 1024, keptEntries: 512, keptBytes: 64 * 1024 * 1024, otherBytes: 64 * 1024 * 1024,
  retentionSeconds: 90 * 86400, writesPerMinute: 300, readEntries: 256, readBytes: 1024 * 1024,
  creationsPerHour: 10, idleSeconds: 180 * 86400, sweepSeconds: 86400,
};
const roomLimits = env => ({ ...LIMITS, ...JSON.parse(env.ROOM_LIMITS || '{}') });

// A sealed welcome: the ChaChaPoly nonce, the tag, and at least one byte.
const MIN_WELCOME = 12 + 16 + 1;
const HEX32 = /^[a-f0-9]{32}$/;
const BASE64 = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;
const noStore = { 'Cache-Control': 'no-store' };
const status = code => new Response(null, { status: code });
// A pattern converts its input to text, so a value that is not text needs its own check.
const hex = (pattern, value) => typeof value === 'string' && pattern.test(value);
// Standard base64 with padding. Buffer accepts other text without an error, so the pattern checks it first.
const decode = text => (typeof text === 'string' && BASE64.test(text) ? Buffer.from(text, 'base64') : null);
const whole = (value, low, high) => Number.isInteger(value) && value >= low && value <= high;

// Reads a JSON object of at most `limit` bytes. It returns undefined for a larger body and null for other input.
async function readObject(request, limit) {
  const bytes = await readBytes(request, limit);
  if (bytes === null) return undefined;
  try {
    const value = JSON.parse(bytes.toString('utf8'));
    return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
  } catch { return null; }
}

// Reads `{member, hash, proof}` of a Mac that makes or joins a room. It returns the status of a refusal, or the
// values for the object. The proof is the Ed25519 signature of the member key over the bytes below.
async function readMember(request, room) {
  const input = await readObject(request, 1024);
  if (input === undefined) return 413;
  const proof = decode(input?.proof);
  if (!input || !hex(HEX64, input.member) || !hex(HEX64, input.hash) || proof?.length !== 64) return 400;
  try {
    const key = await crypto.subtle.importKey('raw', Buffer.from(input.member, 'hex'), { name: 'Ed25519' }, false, ['verify']);
    const signed = Buffer.concat([Buffer.from('coconut room member v1'), Buffer.from(room, 'hex'), Buffer.from(input.hash, 'hex')]);
    if (!(await crypto.subtle.verify({ name: 'Ed25519' }, key, proof, signed))) return 401;
  } catch { return 401; }
  return { member: input.member, hash: input.hash };
}

// The routes of a room: who can call each one, and what the Worker checks before it calls the object.
const ROUTES = {
  'PUT ': {
    async run({ request, env, id, ip, call }) {
      if (!(await env.ROOM_CREATE.limit({ key: ip })).success) return status(429);
      const member = await readMember(request, id);
      if (typeof member === 'number') return status(member);
      // A repeat gets its answer from the room. Only a request that makes a room counts for the hour.
      const answer = await call('create', { ...member, check: '1' });
      if (answer.status !== 404) return answer;
      // The binding allows at most 60 seconds per window, so a Durable Object for each IP counts the hour.
      const limiter = env.ROOMS.get(env.ROOMS.idFromName(`ip:${hash(ip)}`));
      if ((await limiter.fetch('https://room/limit', { method: 'POST' })).status !== 200) {
        return new Response(null, { status: 429, headers: { 'Retry-After': '3600' } });
      }
      return call('create', member);
    },
  },
  'DELETE ': { auth: 'Bearer', run: ({ call }) => call('destroy') },
  'POST tickets': {
    auth: 'Bearer',
    async run({ request, limits, call }) {
      const input = await readObject(request, 4096);
      if (input === undefined) return status(413);
      const welcome = decode(input?.welcome);
      if (!input || !hex(HEX64, input.hash) || !whole(input.ttl, limits.minTtl, limits.maxTtl)
        || !whole(input.uses, 1, limits.uses) || !welcome || welcome.length < MIN_WELCOME) return status(400);
      if (welcome.length > limits.welcome) return status(413);
      return call('ticket', { hash: input.hash, ttl: input.ttl, uses: input.uses }, { body: welcome });
    },
  },
  'DELETE tickets/item': { auth: 'Bearer', run: ({ item, call }) => call('unticket', { hash: item }) },
  'POST members': {
    auth: 'Ticket',
    async run({ request, id, call }) {
      const member = await readMember(request, id);
      return typeof member === 'number' ? status(member) : call('join', member);
    },
  },
  'GET members': { auth: 'Bearer', run: ({ call }) => call('members') },
  'DELETE members/item': { auth: 'Bearer', run: ({ item, call }) => call('remove', { member: item }) },
  'POST entries': {
    auth: 'Bearer',
    async run({ request, limits, call }) {
      // Base64 needs 4 bytes for each 3 bytes of the body. The rest is the entry ID and the JSON around them.
      const input = await readObject(request, Math.ceil(limits.maxBody / 3) * 4 + 1024);
      if (input === undefined) return status(413);
      const body = decode(input?.body);
      if (!input || !hex(HEX32, input.id) || typeof input.kept !== 'boolean' || !body || body.length < limits.minBody) {
        return status(400);
      }
      if (body.length > limits.maxBody) return status(413);
      return call('write', { id: input.id, kept: input.kept ? '1' : '0' }, { body });
    },
  },
  'GET entries': {
    auth: 'Bearer',
    async run({ url, call }) {
      const after = url.searchParams.get('after') ?? '0';
      if (!/^\d{1,15}$/.test(after)) return status(400);
      // The Worker holds the answer for a slow Mac, so the object does not stay in memory for that Mac.
      const answer = await call('read', { after });
      return new Response(await answer.arrayBuffer(), { status: answer.status, headers: answer.headers });
    },
  },
  'GET socket': {
    auth: 'Bearer',
    run: ({ request, call }) => (request.headers.get('upgrade')?.toLowerCase() === 'websocket'
      ? call('socket', {}, { method: 'GET', headers: { upgrade: 'websocket' } }) : status(426)),
  },
};
const SHAPES = new Set(Object.keys(ROUTES).map(route => route.split(' ')[1]));

// Handles the room routes of the Worker. It returns null for any other path.
export async function handleRoom(request, env) {
  const url = new URL(request.url);
  const match = /^\/v1\/rooms\/([a-f0-9]{64})(?:\/(tickets|members|entries|socket)(?:\/([a-f0-9]{64}))?)?$/.exec(url.pathname);
  if (!match) return null;
  const [, id, part = '', item] = match, shape = `${part}${item ? '/item' : ''}`;
  const route = ROUTES[`${request.method} ${shape}`];
  if (!route) return status(SHAPES.has(shape) ? 405 : 404);
  const ip = request.headers.get('cf-connecting-ip') || 'unknown';
  const headers = {};
  if (route.auth) {
    const authorization = /^(Bearer|Ticket) ([a-f0-9]{64})$/.exec(request.headers.get('authorization') || '');
    if (authorization?.[1] !== route.auth) return status(401);
    if (!(await env.ROOM_LIMIT.limit({ key: ip })).success) return status(429);
    headers['x-hash'] = hash(authorization[2]);
  }
  const room = env.ROOMS.get(env.ROOMS.idFromName(id));
  const call = (operation, values = {}, init = {}) => room.fetch(`https://room/${operation}?${new URLSearchParams(values)}`, {
    method: 'POST', ...init, headers: { ...headers, ...init.headers },
  });
  return route.run({ request, env, url, id, item, ip, limits: roomLimits(env), call });
}

export class Room {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.sql = ctx.storage.sql;
    this.limits = roomLimits(env);
    ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair('ping', 'pong'));
  }

  // The tables exist only after a Mac makes the room, so a request for a room that nobody made writes nothing. The
  // answer stays in memory until the object sleeps: `create` and `destroy` change it.
  exists() {
    this.live ??= this.sql.exec("SELECT COUNT(*) AS n FROM sqlite_master WHERE name = 'meta'").one().n > 0
      && !!this.meta('created');
    return this.live;
  }

  // `first` is the `seq` of the first write of the entry ID. A member row also counts the writes of its member in
  // the current minute and the entries that the member has now, so a write reads and writes that one row for them.
  schema() {
    this.sql.exec(`CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value);
      CREATE TABLE IF NOT EXISTS members (id TEXT PRIMARY KEY, hash TEXT NOT NULL UNIQUE, admin INTEGER NOT NULL,
        joined INTEGER NOT NULL, minute INTEGER NOT NULL DEFAULT 0, writes INTEGER NOT NULL DEFAULT 0,
        kept INTEGER NOT NULL DEFAULT 0, kept_bytes INTEGER NOT NULL DEFAULT 0, other_bytes INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS tickets (hash TEXT PRIMARY KEY, expires INTEGER NOT NULL, uses INTEGER NOT NULL,
        welcome BLOB NOT NULL);
      CREATE TABLE IF NOT EXISTS entries (seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, member TEXT NOT NULL,
        first INTEGER NOT NULL, at INTEGER NOT NULL, kept INTEGER NOT NULL, size INTEGER NOT NULL, body BLOB NOT NULL);`);
  }

  meta(key) { return this.sql.exec('SELECT value FROM meta WHERE key = ?', key).toArray()[0]?.value; }
  setMeta(key, value) { this.sql.exec('INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)', key, value); }
  row(query, ...values) { return this.sql.exec(query, ...values).toArray()[0]; }
  last() { return this.sql.exec('SELECT COALESCE(MAX(seq), 0) AS last FROM entries').one().last; }

  async fetch(request) {
    const url = new URL(request.url), operation = url.pathname.slice(1), values = url.searchParams, now = Date.now();
    if (operation === 'limit') return this.limit(now);
    // The body is read first. An input gate holds other events only while storage works, so the checks and the
    // writes of each operation below have no other await between them.
    const body = request.body ? await request.arrayBuffer() : null;
    if (operation === 'create') return this.create(values.get('member'), values.get('hash'), values.has('check'), now);
    if (!this.exists()) return status(404);
    const digest = request.headers.get('x-hash');
    if (operation === 'join') return this.join(digest, values.get('member'), values.get('hash'), now);
    const member = this.row('SELECT id, admin, minute, writes, kept, kept_bytes, other_bytes FROM members WHERE hash = ?', digest);
    if (!member) return status(401);
    this.seen(now);
    if (['ticket', 'unticket', 'destroy'].includes(operation) && !member.admin) return status(403);
    switch (operation) {
      case 'ticket': return this.addTicket(values.get('hash'), Number(values.get('ttl')), Number(values.get('uses')), body, now);
      case 'unticket':
        this.sql.exec('DELETE FROM tickets WHERE hash = ?', values.get('hash'));
        return status(204);
      case 'members':
        return Response.json({
          members: this.sql.exec('SELECT id, admin, joined FROM members ORDER BY joined, id').toArray()
            .map(row => ({ member: row.id, admin: !!row.admin, joined: row.joined })),
        }, { headers: noStore });
      case 'remove': return this.remove(member, values.get('member'));
      case 'write': return this.write(member, values.get('id'), values.get('kept') === '1', body, now);
      case 'read': return this.read(Number(values.get('after')));
      case 'socket': return this.socket(member);
      case 'destroy': return this.destroy();
      default: return status(404);
    }
  }

  // This object counts the new rooms of one IP address in an hour. It has no room, so its alarm deletes it.
  async limit(now) {
    const hour = Math.floor(now / 3600000);
    this.sql.exec('CREATE TABLE IF NOT EXISTS creations (hour INTEGER PRIMARY KEY, count INTEGER NOT NULL)');
    this.sql.exec('DELETE FROM creations WHERE hour < ?', hour);
    const { count } = this.sql.exec(`INSERT INTO creations (hour, count) VALUES (?, 1)
      ON CONFLICT (hour) DO UPDATE SET count = count + 1 RETURNING count`, hour).one();
    await this.ctx.storage.setAlarm((hour + 1) * 3600000);
    return status(count <= this.limits.creationsPerHour ? 200 : 429);
  }

  // With `check`, a room that does not exist answers 404 and nothing is made.
  async create(member, digest, check, now) {
    const admin = this.exists() && this.row('SELECT id, hash FROM members WHERE admin = 1');
    if (admin) return status(admin.id === member && admin.hash === digest ? 200 : 409);
    if (check) return status(404);
    this.schema();
    this.sql.exec('INSERT INTO members (id, hash, admin, joined) VALUES (?, ?, 1, ?)', member, digest, now);
    this.setMeta('created', now);
    this.setMeta('seen', now);
    this.live = true;
    this.used = now;
    await this.ctx.storage.setAlarm(now + this.limits.sweepSeconds * 1000);
    return status(201);
  }

  // Notes that a member used the room. The alarm deletes a room that no member used for a long time. One write in
  // an hour is enough for that.
  seen(now) {
    this.used ??= this.meta('seen') || 0;
    if (now - this.used < Math.min(3600, this.limits.idleSeconds / 4) * 1000) return;
    this.setMeta('seen', now);
    this.used = now;
  }

  addTicket(digest, ttl, uses, welcome, now) {
    this.sql.exec('DELETE FROM tickets WHERE expires <= ?', now);
    // A ticket without a use left stays until it expires, for a repeat of a join. It is not an open ticket.
    const open = this.sql.exec('SELECT COUNT(*) AS n FROM tickets WHERE uses > 0 AND hash != ?', digest).one().n;
    if (open >= this.limits.tickets) return status(409);
    this.sql.exec('INSERT OR REPLACE INTO tickets (hash, expires, uses, welcome) VALUES (?, ?, ?, ?)',
      digest, now + ttl * 1000, uses, welcome);
    return status(204);
  }

  join(ticket, member, digest, now) {
    this.sql.exec('DELETE FROM tickets WHERE expires <= ?', now);
    const found = this.row('SELECT uses, welcome FROM tickets WHERE hash = ?', ticket);
    if (!found) return status(401);
    const welcome = () => Response.json({ welcome: Buffer.from(found.welcome).toString('base64') }, { headers: noStore });
    const existing = this.row('SELECT hash FROM members WHERE id = ?', member);
    if (existing) return existing.hash === digest ? welcome() : status(409);
    if (found.uses < 1) return status(401);
    if (this.sql.exec('SELECT COUNT(*) AS n FROM members').one().n >= this.limits.members
      || this.row('SELECT 1 FROM members WHERE hash = ?', digest)) return status(409);
    this.sql.exec('INSERT INTO members (id, hash, admin, joined) VALUES (?, ?, 0, ?)', member, digest, now);
    this.sql.exec('UPDATE tickets SET uses = uses - 1 WHERE hash = ?', ticket);
    return welcome();
  }

  remove(caller, target) {
    if (!caller.admin && caller.id !== target) return status(403);
    const member = this.row('SELECT admin FROM members WHERE id = ?', target);
    if (!member) return status(204);
    if (member.admin) {
      // An admin that leaves a room with other members would leave it without anyone who can invite or remove.
      return this.sql.exec('SELECT COUNT(*) AS n FROM members').one().n > 1 ? status(409) : this.destroy();
    }
    this.sql.exec('DELETE FROM members WHERE id = ?', target);
    this.sql.exec('DELETE FROM entries WHERE member = ? AND kept = 1', target);
    for (const ws of this.ctx.getWebSockets(`member:${target}`)) close(ws, 1008, 'removed');
    return status(204);
  }

  write(member, id, kept, body, now) {
    const minute = Math.floor(now / 60000), size = body.byteLength, { limits } = this;
    const writes = member.minute === minute ? member.writes + 1 : 1;
    if (writes > limits.writesPerMinute) return status(429);
    const old = this.row('SELECT seq, member, first, kept, size FROM entries WHERE id = ?', id);
    if (old && old.member !== member.id) return status(409);
    // What the member has without the entry that this write replaces, and then with the new entry. A member that
    // joins again after a removal replaces entries that its new row never counted, so a value can go below zero.
    let count = member.kept - (old?.kept ? 1 : 0);
    let keptBytes = member.kept_bytes - (old?.kept ? old.size : 0);
    let otherBytes = member.other_bytes - (old && !old.kept ? old.size : 0);
    if (kept) { count += 1; keptBytes += size; } else otherBytes += size;
    if (count > limits.keptEntries || keptBytes > limits.keptBytes || otherBytes > limits.otherBytes) return status(507);
    // A number is never used again, also after the relay deletes the entry that had it.
    const seq = this.sql.exec(`INSERT INTO meta (key, value) VALUES ('seq', 1)
      ON CONFLICT (key) DO UPDATE SET value = value + 1 RETURNING value`).one().value;
    const first = old ? old.first : seq;
    if (old) this.sql.exec('DELETE FROM entries WHERE seq = ?', old.seq);
    this.sql.exec('INSERT INTO entries (seq, id, member, first, at, kept, size, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
      seq, id, member.id, first, now, kept ? 1 : 0, size, body);
    this.sql.exec('UPDATE members SET minute = ?, writes = ?, kept = ?, kept_bytes = ?, other_bytes = ? WHERE id = ?',
      minute, writes, Math.max(0, count), Math.max(0, keptBytes), Math.max(0, otherBytes), member.id);
    // The writer has the number in its answer. Each other member gets it on its socket.
    const own = new Set(this.ctx.getWebSockets(`member:${member.id}`));
    for (const ws of this.ctx.getWebSockets()) if (!own.has(ws)) send(ws, JSON.stringify({ seq }));
    return Response.json({ seq, first }, { headers: noStore });
  }

  read(after) {
    // The first query reads no body: it finds how many entries fit in one answer.
    let until = after, bytes = 0, count = 0;
    for (const row of this.sql.exec('SELECT seq, size FROM entries WHERE seq > ? ORDER BY seq LIMIT ?', after, this.limits.readEntries)) {
      if (count && bytes + row.size > this.limits.readBytes) break;
      bytes += row.size; count += 1; until = row.seq;
    }
    const entries = this.sql.exec(`SELECT seq, first, id, member, at, kept, body FROM entries
      WHERE seq > ? AND seq <= ? ORDER BY seq`, after, until).toArray().map(row => ({
      seq: row.seq, first: row.first, id: row.id, member: row.member, at: row.at, kept: !!row.kept,
      body: Buffer.from(row.body).toString('base64'),
    }));
    return Response.json({ entries, last: this.last() }, { headers: noStore });
  }

  socket(member) {
    for (const ws of this.ctx.getWebSockets(`member:${member.id}`)) close(ws, 1012, 'replaced');
    const [client, server] = Object.values(new WebSocketPair());
    this.ctx.acceptWebSocket(server, [`member:${member.id}`]);
    send(server, JSON.stringify({ seq: this.last() }));
    return new Response(null, { status: 101, webSocket: client });
  }

  // `ping` never reaches this method: the automatic reply answers it. A member sends nothing else.
  webSocketMessage(ws) { close(ws, 1003, 'unsupported'); }
  webSocketClose(ws, code) { close(ws, code === 1005 || code === 1006 ? 1000 : code, ''); }
  webSocketError() {}

  // The alarm goes first: `deleteAll` keeps a pending alarm, and a later `deleteAlarm` would store data again.
  async destroy() {
    for (const ws of this.ctx.getWebSockets()) close(ws, 1000, 'deleted');
    await this.ctx.storage.deleteAlarm();
    await this.ctx.storage.deleteAll();
    this.live = false;
    this.used = undefined;
    return status(204);
  }

  // Deletes what is too old, and the room itself when no member used it for a long time. An object without a room
  // counted the new rooms of an IP address, and the alarm deletes it.
  async alarm() {
    const now = Date.now(), cutoff = now - this.limits.retentionSeconds * 1000;
    if (!this.exists() || now - (this.meta('seen') || 0) > this.limits.idleSeconds * 1000) {
      await this.destroy();
      return;
    }
    const old = this.sql.exec('SELECT member, SUM(size) AS bytes FROM entries WHERE kept = 0 AND at <= ? GROUP BY member', cutoff);
    for (const { member, bytes } of old.toArray()) {
      this.sql.exec('UPDATE members SET other_bytes = MAX(other_bytes - ?, 0) WHERE id = ?', bytes, member);
    }
    this.sql.exec('DELETE FROM entries WHERE kept = 0 AND at <= ?', cutoff);
    this.sql.exec('DELETE FROM tickets WHERE expires <= ?', now);
    await this.ctx.storage.setAlarm(now + this.limits.sweepSeconds * 1000);
  }
}
