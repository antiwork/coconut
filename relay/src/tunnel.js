import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';

// The tunnel relays end-to-end encrypted Noise traffic between one Mac (the host) and its iPhones
// (the clients). The relay never sees plaintext. It stores only SHA-256 hashes of tokens and tickets.
//
// Wire format, agreed with RelayFrame in ios/Tunnel/TunnelFrames.swift:
// - Binary messages on the host socket are `channel u32 big-endian | payload`, at most 70 KB.
//   - Channel 0 carries plain JSON control messages between the host and the relay.
//   - Any other channel carries the ciphertext of one client. The relay adds the channel toward the
//     host and removes it toward the client, so binary messages on a client socket are ciphertext only.
// - Text messages come from the relay itself, never from the other peer:
//   - `ping` gets the automatic reply `pong`, on both sockets, without waking the Durable Object.
//   - On a client socket, `{"type":"error","code":c}` comes right before the relay closes it:
//     `mac_offline` (no host connected in time, or the host socket ended), `host_reconnected`, `revoked`,
//     or `closed` (by the host).
// - A client that connects while no host is connected waits up to 5 seconds for one. The relay keeps the client's
//   first messages, about 1 KB. When a host connects, the relay sends `open` and those messages to it. Otherwise
//   the client gets `mac_offline`. Cloudflare can reset a long-lived host socket at any time, for example when it
//   restarts a server, so the sidecar connects again at once, and a phone does not see the short gap.
// - Host to relay, on channel 0 (a hash is the lowercase hex SHA-256 of the token string):
//   - `{"type":"add_ticket","hash":h,"ttl":s}` allows one client connection with that pairing ticket
//     for s seconds (1 to 600).
//   - `{"type":"add_client","hash":h}` and `{"type":"remove_client","hash":h}` allow or revoke a
//     client token. Removal also closes the channels that this token opened.
//   - `{"type":"close_channel","id":n}` closes one client socket.
// - Relay to host, on channel 0:
//   - `{"type":"open","id":n,"auth":"token"|"ticket","hash":h}` when a client connects.
//   - `{"type":"closed","id":n}` when a client socket closes or the channel does not exist.
//   - `{"type":"error","code":c}` when a control message is invalid or a limit is full.
// - Close codes:
//   - 1003 for a text message other than `ping`, or a host message shorter than 4 bytes.
//   - 1008 for the channels of a removed client token.
//   - 1009 for a message over 70 KB.
//   - 1012 for clients when a new host socket replaces the old one.
//   - 1013 when the route gets more than 3,000 messages in a minute.
//   - 4001 for mac_offline.
//   Workerd can deliver a close that another socket's event starts about 10 seconds late, so a
//   client must treat a text error message as the end of its socket.

const MAX_FRAME = 70 * 1024;
const MAX_CHANNELS = 8;
const MAX_CLIENT_TOKENS = 32;
const MAX_TICKETS = 8;
const FRAMES_PER_MINUTE = 3000;
// How long a client waits for a host.
const HOST_WAIT_MS = 5000;
// The relay keeps the messages of a waiting client in its socket attachment, so they survive hibernation. An
// attachment can hold 2,048 bytes, so the whole attachment stays at or below this size as JSON. The first Noise
// message of the phone is 96 bytes, 128 bytes in base64.
const MAX_WAITING_ATTACHMENT = 1536;
const REGISTRATIONS_PER_HOUR = 10;
const HEX64 = /^[a-f0-9]{64}$/;

export const hash = value => createHash('sha256').update(value).digest('hex');
// A host message: `channel u32 big-endian | bytes`.
const framed = (channel, bytes) => {
  const frame = new Uint8Array(4 + bytes.length);
  new DataView(frame.buffer).setUint32(0, channel);
  frame.set(bytes, 4);
  return frame;
};
const control = value => framed(0, new TextEncoder().encode(JSON.stringify(value)));
const close = (ws, code, reason) => { try { ws.close(code, reason); } catch {} };
const send = (ws, data) => { try { ws.send(data); } catch {} };

// Reads the body as a stream and stops at the cap. It returns null for a larger body.
export async function readBytes(request, limit) {
  if (Number(request.headers.get('content-length') || 0) > limit) return null;
  if (!request.body) return Buffer.alloc(0);
  const reader = request.body.getReader(), chunks = [];
  let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.length;
    if (size > limit) { await reader.cancel(); return null; }
    chunks.push(value);
  }
  return Buffer.concat(chunks);
}

const readCapped = async (request, limit) => (await readBytes(request, limit))?.toString('utf8') ?? null;

// Handles the tunnel routes of the Worker. It returns null for any other path.
export async function handleTunnel(request, env) {
  const path = new URL(request.url).pathname;
  const ip = request.headers.get('cf-connecting-ip') || 'unknown';
  if (path === '/v1/tunnels' && request.method === 'POST') {
    if (!(await env.TUNNEL_REGISTER.limit({ key: ip })).success) return new Response(null, { status: 429 });
    const text = await readCapped(request, 1024);
    if (text === null) return new Response(null, { status: 413 });
    let route;
    try { route = JSON.parse(text).route; } catch { return new Response(null, { status: 400 }); }
    if (typeof route !== 'string' || !HEX64.test(route)) return new Response(null, { status: 400 });
    // The binding allows at most 60 seconds per window, so a Durable Object for each IP counts the hour.
    const limiter = env.TUNNELS.get(env.TUNNELS.idFromName(`ip:${hash(ip)}`));
    if ((await limiter.fetch('https://tunnel/limit', { method: 'POST' })).status !== 200) {
      return new Response(null, { status: 429, headers: { 'Retry-After': '3600' } });
    }
    return env.TUNNELS.get(env.TUNNELS.idFromName(route)).fetch('https://tunnel/register', { method: 'POST' });
  }
  const match = /^\/v1\/tunnels\/([a-f0-9]{64})\/(host|client)$/.exec(path);
  if (!match) return null;
  if (request.method !== 'GET') return new Response(null, { status: 405 });
  if (request.headers.get('upgrade')?.toLowerCase() !== 'websocket') return new Response(null, { status: 426 });
  if (!/^(Bearer|Ticket) [a-f0-9]{64}$/.test(request.headers.get('authorization') || '')) {
    return new Response(null, { status: 401 });
  }
  if (!(await env.TUNNEL_CONNECT.limit({ key: ip })).success) return new Response(null, { status: 429 });
  return env.TUNNELS.get(env.TUNNELS.idFromName(match[1])).fetch(new Request(`https://tunnel/${match[2]}`, request));
}

export class Tunnel {
  constructor(ctx, env) {
    this.ctx = ctx; this.env = env;
    this.sql = ctx.storage.sql;
    this.sql.exec(`CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value);
      CREATE TABLE IF NOT EXISTS clients (hash TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS tickets (hash TEXT PRIMARY KEY, expires INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS frames (minute INTEGER PRIMARY KEY, count INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS registrations (hour INTEGER PRIMARY KEY, count INTEGER NOT NULL);`);
    ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair('ping', 'pong'));
  }

  meta(key) { return this.sql.exec('SELECT value FROM meta WHERE key = ?', key).toArray()[0]?.value; }
  setMeta(key, value) { this.sql.exec('INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)', key, value); }

  // Counts in SQLite so that the window survives hibernation.
  count(table, column, window, limit) {
    this.sql.exec(`DELETE FROM ${table} WHERE ${column} < ?`, window);
    const { count } = this.sql.exec(`INSERT INTO ${table} (${column}, count) VALUES (?, 1)
      ON CONFLICT (${column}) DO UPDATE SET count = count + 1 RETURNING count`, window).one();
    return count <= limit;
  }

  host() {
    const generation = this.meta('host_generation');
    return this.ctx.getWebSockets('host').find(ws => ws.readyState === WebSocket.OPEN
      && ws.deserializeAttachment()?.generation === generation);
  }

  clients() { return this.ctx.getWebSockets('client').filter(ws => ws.readyState === WebSocket.OPEN); }

  async fetch(request) {
    const path = new URL(request.url).pathname;
    const now = Date.now();
    if (path === '/limit') {
      return new Response(null, { status: this.count('registrations', 'hour', Math.floor(now / 3600000), REGISTRATIONS_PER_HOUR) ? 200 : 429 });
    }
    if (path === '/register') {
      if (this.meta('host_hash')) return Response.json({ error: 'Route exists' }, { status: 409 });
      const token = randomBytes(32).toString('hex');
      this.setMeta('host_hash', hash(token));
      return Response.json({ host_token: token }, { headers: { 'Cache-Control': 'no-store' } });
    }
    const hostHash = this.meta('host_hash');
    if (!hostHash) return new Response(null, { status: 404 });
    const [scheme, secret] = request.headers.get('authorization').split(' ');
    const digest = hash(secret);
    if (path === '/host') {
      if (scheme !== 'Bearer' || !timingSafeEqual(Buffer.from(digest), Buffer.from(hostHash))) {
        return new Response(null, { status: 401 });
      }
      // A newer host socket replaces the older one. The old Noise sessions end with it.
      for (const ws of this.ctx.getWebSockets('host')) close(ws, 1000, 'replaced');
      for (const ws of this.clients()) if (!ws.deserializeAttachment()?.waiting) this.end(ws, 1012, 'host_reconnected');
      const generation = (this.meta('host_generation') || 0) + 1;
      this.setMeta('host_generation', generation);
      const [client, server] = Object.values(new WebSocketPair());
      this.ctx.acceptWebSocket(server, ['host']);
      server.serializeAttachment({ role: 'host', generation });
      this.adopt(server, generation);
      return new Response(null, { status: 101, webSocket: client });
    }
    let auth;
    if (scheme === 'Bearer' && this.sql.exec('SELECT 1 FROM clients WHERE hash = ?', digest).toArray().length) auth = 'token';
    if (scheme === 'Ticket') {
      this.sql.exec('DELETE FROM tickets WHERE expires <= ?', now);
      if (this.sql.exec('SELECT 1 FROM tickets WHERE hash = ?', digest).toArray().length) auth = 'ticket';
    }
    if (!auth) return new Response(null, { status: 401 });
    if (this.clients().length >= MAX_CHANNELS) return new Response(null, { status: 429 });
    // A full route keeps the ticket, so the iPhone can pair after a channel closes.
    if (auth === 'ticket') this.sql.exec('DELETE FROM tickets WHERE hash = ?', digest);
    const channel = this.meta('next_channel') || 1;
    this.setMeta('next_channel', channel >= 0xffffffff ? 1 : channel + 1);
    const [client, server] = Object.values(new WebSocketPair());
    this.ctx.acceptWebSocket(server, ['client', `channel:${channel}`, `key:${digest}`]);
    const host = this.host();
    if (host) {
      server.serializeAttachment({ role: 'client', channel, generation: this.meta('host_generation') });
      send(host, control({ type: 'open', id: channel, auth, hash: digest }));
    } else {
      // The host socket can be gone for a moment, for example after Cloudflare resets it. The client waits for the next one.
      server.serializeAttachment({ role: 'client', channel, waiting: { since: now, auth, hash: digest, frames: [] } });
      await this.wake(now + HOST_WAIT_MS);
    }
    return new Response(null, { status: 101, webSocket: client });
  }

  // Sets the alarm to `at`, unless an earlier alarm is already set.
  async wake(at) {
    const current = await this.ctx.storage.getAlarm();
    if (current === null || current > at) await this.ctx.storage.setAlarm(at);
  }

  // Ends the clients that waited too long for a host, and wakes again for the others.
  async alarm() {
    const now = Date.now();
    let next;
    for (const ws of this.clients()) {
      const waiting = ws.deserializeAttachment()?.waiting;
      if (!waiting) continue;
      const deadline = waiting.since + HOST_WAIT_MS;
      if (deadline <= now) this.end(ws, 4001, 'mac_offline');
      else next = Math.min(next ?? deadline, deadline);
    }
    if (next !== undefined) await this.wake(next);
  }

  // Gives the waiting clients to a new host: `open`, then the messages that each client sent while it waited.
  adopt(host, generation) {
    for (const ws of this.clients()) {
      const attachment = ws.deserializeAttachment();
      if (!attachment?.waiting) continue;
      const { auth, hash: digest, frames } = attachment.waiting;
      ws.serializeAttachment({ role: 'client', channel: attachment.channel, generation });
      send(host, control({ type: 'open', id: attachment.channel, auth, hash: digest }));
      for (const data of frames) send(host, framed(attachment.channel, Buffer.from(data, 'base64')));
    }
  }

  end(ws, code, reason) {
    send(ws, JSON.stringify({ type: 'error', code: reason }));
    close(ws, code, reason);
  }

  webSocketMessage(ws, message) {
    const attachment = ws.deserializeAttachment();
    const size = typeof message === 'string' ? Buffer.byteLength(message) : message.byteLength;
    if (size > MAX_FRAME) return close(ws, 1009, 'too_large');
    if (!this.count('frames', 'minute', Math.floor(Date.now() / 60000), FRAMES_PER_MINUTE)) {
      return close(ws, 1013, 'rate_limited');
    }
    if (typeof message === 'string') return close(ws, 1003, 'binary_only');
    const bytes = new Uint8Array(message);
    if (attachment?.waiting) {
      // A client that sends more than the attachment holds gets mac_offline, never an exception.
      attachment.waiting.frames.push(Buffer.from(bytes).toString('base64'));
      if (JSON.stringify(attachment).length > MAX_WAITING_ATTACHMENT) return this.end(ws, 4001, 'mac_offline');
      try { ws.serializeAttachment(attachment); } catch { this.end(ws, 4001, 'mac_offline'); }
      return;
    }
    if (attachment?.role === 'client') {
      const host = this.host();
      if (!host) return this.end(ws, 4001, 'mac_offline');
      return send(host, framed(attachment.channel, bytes));
    }
    if (attachment?.role !== 'host' || bytes.length < 4) return close(ws, 1003, 'invalid');
    const channel = new DataView(bytes.buffer, bytes.byteOffset).getUint32(0);
    if (channel === 0) return this.control(ws, bytes.subarray(4));
    const target = this.ctx.getWebSockets(`channel:${channel}`)[0];
    if (target?.readyState === WebSocket.OPEN) send(target, bytes.subarray(4));
    else send(ws, control({ type: 'closed', id: channel }));
  }

  control(host, bytes) {
    const fail = code => send(host, control({ type: 'error', code }));
    let input;
    try { input = JSON.parse(new TextDecoder().decode(bytes)); } catch { return fail('invalid'); }
    const { type, hash: digest, ttl, id } = input || {};
    if (['add_ticket', 'add_client', 'remove_client'].includes(type) && !(typeof digest === 'string' && HEX64.test(digest))) {
      return fail('invalid');
    }
    if (type === 'add_ticket') {
      if (!Number.isInteger(ttl) || ttl < 1 || ttl > 600) return fail('invalid');
      const now = Date.now();
      this.sql.exec('DELETE FROM tickets WHERE expires <= ?', now);
      if (this.sql.exec('SELECT COUNT(*) AS n FROM tickets').one().n >= MAX_TICKETS) return fail('limit');
      this.sql.exec('INSERT OR REPLACE INTO tickets (hash, expires) VALUES (?, ?)', digest, now + ttl * 1000);
    } else if (type === 'add_client') {
      if (this.sql.exec('SELECT COUNT(*) AS n FROM clients').one().n >= MAX_CLIENT_TOKENS) return fail('limit');
      this.sql.exec('INSERT OR IGNORE INTO clients (hash) VALUES (?)', digest);
    } else if (type === 'remove_client') {
      this.sql.exec('DELETE FROM clients WHERE hash = ?', digest);
      for (const ws of this.ctx.getWebSockets(`key:${digest}`)) this.end(ws, 1008, 'revoked');
    } else if (type === 'close_channel' && Number.isInteger(id) && id > 0) {
      for (const ws of this.ctx.getWebSockets(`channel:${id}`)) this.end(ws, 1000, 'closed');
    } else return fail('invalid');
  }

  webSocketClose(ws, code) {
    close(ws, code === 1005 || code === 1006 ? 1000 : code, '');
    this.closed(ws);
  }
  webSocketError(ws) { this.closed(ws); }

  closed(ws) {
    const attachment = ws.deserializeAttachment();
    if (attachment?.role === 'client') {
      const host = this.host();
      if (host && attachment.generation === this.meta('host_generation')) send(host, control({ type: 'closed', id: attachment.channel }));
    } else if (attachment?.role === 'host' && attachment.generation === this.meta('host_generation')) {
      // The Noise sessions end with the host socket. A client that waits for the next host keeps waiting.
      for (const client of this.clients()) if (!client.deserializeAttachment()?.waiting) this.end(client, 4001, 'mac_offline');
    }
  }
}
