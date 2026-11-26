import { createHash } from 'node:crypto';
export { PushDevice } from './device.js';
import { handleTunnel } from './tunnel.js';
export { Tunnel } from './tunnel.js';
import { handleRendezvous } from './rendezvous.js';
export { Rendezvous } from './rendezvous.js';
import { handleBudget } from './budget.js';
export { BudgetAccount } from './budget.js';
import { handleRoom } from './room.js';
export { Room } from './room.js';

export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (path === '/health' && request.method === 'GET') return Response.json({ ok: true, version: 1 });
    try {
      const tunnel = await handleTunnel(request, env);
      if (tunnel) return tunnel;
      const rendezvous = await handleRendezvous(request, env);
      if (rendezvous) return rendezvous;
      // The Personal budget streams model calls, so its routes come before the body cap.
      const budget = await handleBudget(request, env);
      if (budget) return budget;
      const room = await handleRoom(request, env);
      if (room) return room;
      // The 24 KB body cap applies to the push routes only.
      if (Number(request.headers.get('content-length') || 0) > 24000) return new Response(null, { status: 413 });
      const chunks = [];
      let size = 0;
      if (request.body) {
        const reader = request.body.getReader();
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          size += value.length;
          if (size > 24000) { await reader.cancel(); return new Response(null, { status: 413 }); }
          chunks.push(Buffer.from(value));
        }
      }
      const text = Buffer.concat(chunks).toString('utf8');
      if (request.method === 'POST' && ['/v1/challenge', '/v1/register'].includes(path)) {
        const rate = await env.REGISTRATION_LIMIT.limit({ key: request.headers.get('cf-connecting-ip') || 'unknown' });
        if (!rate.success) return new Response(null, { status: 429, headers: { 'Retry-After': '60' } });
        const input = JSON.parse(text);
        if (typeof input.key_id !== 'string' || !/^[A-Za-z0-9+/]{43}=$/.test(input.key_id)) {
          return new Response(null, { status: 400 });
        }
        const id = createHash('sha256').update(input.key_id).digest('hex');
        return env.DEVICES.get(env.DEVICES.idFromName(id)).fetch(new Request(request.url, {
          method: request.method, headers: { 'content-type': 'application/json', 'x-route-id': id }, body: text,
        }));
      }
      // A Mac sends a notification (POST /send) and a glance of the Home Screen (POST /glance) with the send token. The
      // iPhone revokes its route (DELETE), names the tokens of its Live Activity and widgets (PUT /live) and reads the
      // newest glance for its widgets (GET /glance) with the manage token.
      const match = /^\/v1\/routes\/([a-f0-9]{64})(\/send|\/glance|\/live)?$/.exec(path);
      const allowed = { '/send': ['POST'], '/glance': ['POST', 'GET'], '/live': ['PUT'] }[match?.[2]] ?? ['DELETE'];
      if (match && allowed.includes(request.method)) {
        if (!/^Bearer [a-f0-9]{64}$/.test(request.headers.get('authorization') || '')) return new Response(null, { status: 401 });
        const rate = await env.DELIVERY_LIMIT.limit({ key: request.headers.get('cf-connecting-ip') || 'unknown' });
        if (!rate.success) return new Response(null, { status: 429 });
        return env.DEVICES.get(env.DEVICES.idFromName(match[1])).fetch(new Request(request.url, {
          method: request.method, headers: request.headers, body: text || undefined,
        }));
      }
      return new Response(null, { status: 404 });
    } catch { return new Response(null, { status: 400 }); }
  },
};
