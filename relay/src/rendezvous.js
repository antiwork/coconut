import { timingSafeEqual } from 'node:crypto';
import { hash, readBytes } from './tunnel.js';

// A rendezvous box carries one sealed pairing setup from a Mac to an iPhone. The iPhone makes a one-time code and
// shows it in the message for Hermes. Both sides derive the box ID and the box key from that code. The relay sees only
// the box ID and the ciphertext, so it can neither read nor forge the setup.
//
// - `PUT /v1/rendezvous/<id>` with `Authorization: Bearer <phone token>` makes the box. The box lives for 600 seconds.
//   The phone token never leaves the iPhone, so only the iPhone can read the box. A repeat with the same token answers
//   200. Another token answers 409.
// - `POST /v1/rendezvous/<id>/message` puts the sealed setup in the box: 29 to 4096 bytes. The box takes one message
//   only: a second message answers 409. A missing or expired box answers 404.
// - `GET /v1/rendezvous/<id>?wait=s` with the phone token returns the message (200), or 204 when no message comes
//   within s seconds (0 to 25). The relay keeps the message until the iPhone deletes the box or the box expires, so a
//   reply that the iPhone loses (for example while iOS suspends the app) does not lose the setup.
// - `DELETE /v1/rendezvous/<id>` with the phone token removes the box. The iPhone does this after it opens the setup.
//
// The box ID and the phone token are 256 bits, so the relay limits only the requests of each IP.

const MIN_MESSAGE = 12 + 16 + 1; // ChaChaPoly nonce, tag, and at least one byte.
const MAX_MESSAGE = 4096;
const MAX_WAIT = 25;

const noStore = { 'Cache-Control': 'no-store' };

// Handles the rendezvous routes of the Worker. It returns null for any other path.
export async function handleRendezvous(request, env) {
  const match = /^\/v1\/rendezvous\/([a-f0-9]{64})(\/message)?$/.exec(new URL(request.url).pathname);
  if (!match) return null;
  const ip = request.headers.get('cf-connecting-ip') || 'unknown';
  const box = () => env.RENDEZVOUS.get(env.RENDEZVOUS.idFromName(match[1]));
  if (match[2]) {
    if (request.method !== 'POST') return new Response(null, { status: 405 });
    if (!(await env.RENDEZVOUS_LIMIT.limit({ key: ip })).success) return new Response(null, { status: 429 });
    const body = await readBytes(request, MAX_MESSAGE);
    if (body === null) return new Response(null, { status: 413 });
    if (body.length < MIN_MESSAGE) return new Response(null, { status: 400 });
    return box().fetch('https://rendezvous/message', { method: 'POST', body });
  }
  if (!['PUT', 'GET', 'DELETE'].includes(request.method)) return new Response(null, { status: 405 });
  const authorization = /^Bearer ([a-f0-9]{64})$/.exec(request.headers.get('authorization') || '');
  if (!authorization) return new Response(null, { status: 401 });
  const limit = request.method === 'PUT' ? env.RENDEZVOUS_CREATE : env.RENDEZVOUS_LIMIT;
  if (!(await limit.limit({ key: ip })).success) return new Response(null, { status: 429 });
  const wait = Math.min(MAX_WAIT, Math.max(0, Math.floor(Number(new URL(request.url).searchParams.get('wait')) || 0)));
  return box().fetch(`https://rendezvous/box?wait=${wait}`, {
    method: request.method, headers: { 'x-phone': hash(authorization[1]) },
  });
}

export class Rendezvous {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.seconds = Number(env.RENDEZVOUS_SECONDS) || 600;
    // Open GET requests that wait for the message. They live only in memory: a waiting request keeps the object awake.
    this.waiters = new Set();
  }

  // Returns the live box, and removes an expired one.
  async box() {
    const box = await this.ctx.storage.get('box');
    if (box && box.expires <= Date.now()) { await this.clear(); return undefined; }
    return box;
  }

  async clear() {
    await this.ctx.storage.deleteAll();
    await this.ctx.storage.deleteAlarm();
    for (const wake of this.waiters) wake();
  }

  async alarm() { await this.clear(); }

  async fetch(request) {
    const url = new URL(request.url);
    // The body is read first. An input gate holds other events only while storage works, so the check and the write
    // below must have no other await between them, or two Macs could both see an empty box.
    const sent = url.pathname === '/message' ? new Uint8Array(await request.arrayBuffer()) : null;
    const box = await this.box();
    if (sent) {
      if (!box) return new Response(null, { status: 404 });
      if (box.message) return new Response(null, { status: 409 });
      await this.ctx.storage.put('box', { ...box, message: sent });
      for (const wake of this.waiters) wake();
      return new Response(null, { status: 204 });
    }
    const phone = request.headers.get('x-phone');
    const owner = box && timingSafeEqual(Buffer.from(box.phone), Buffer.from(phone));
    if (request.method === 'PUT') {
      if (owner) return new Response(null, { status: 200, headers: noStore });
      if (box) return new Response(null, { status: 409 });
      const expires = Date.now() + this.seconds * 1000;
      await this.ctx.storage.put('box', { phone, expires });
      await this.ctx.storage.setAlarm(expires);
      return new Response(null, { status: 201, headers: noStore });
    }
    if (!box) return new Response(null, { status: 404 });
    if (!owner) return new Response(null, { status: 401 });
    if (request.method === 'DELETE') {
      await this.clear();
      return new Response(null, { status: 204 });
    }
    const message = box.message || await this.wait(Number(url.searchParams.get('wait')), box.expires);
    if (!message) return new Response(null, { status: (await this.box()) ? 204 : 404, headers: noStore });
    return new Response(message, { headers: { ...noStore, 'Content-Type': 'application/octet-stream' } });
  }

  // Waits for the message, at most for the given seconds and never past the expiry of the box.
  async wait(seconds, expires) {
    if (seconds > 0) {
      await new Promise(resolve => {
        const wake = () => { clearTimeout(timer); this.waiters.delete(wake); resolve(); };
        const timer = setTimeout(wake, Math.min(seconds * 1000, expires - Date.now()));
        this.waiters.add(wake);
      });
    }
    return (await this.box())?.message;
  }
}
