// The Miniflare setup of the room routes, shared by room.test.js and room-server.mjs.
import { readFileSync } from 'node:fs';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';

export function roomRuntime({ limits = {}, create = 100, requests = 100000, ...options } = {}) {
  return new Miniflare(convertV4MiniflareOptions({
    ...options,
    compatibilityDate: '2026-09-10', compatibilityFlags: ['nodejs_compat'],
    modules: [
      { type: 'ESModule', path: 'entry.js', contents: `
        import { handleRoom } from './room.js';
        export { Room } from './room.js';
        export default { async fetch(request, env) {
          return (await handleRoom(request, env)) || new Response(null, { status: 404 });
        }};` },
      ...['room.js', 'tunnel.js'].map(name => (
        { type: 'ESModule', path: name, contents: readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8') })),
    ],
    bindings: { ROOM_LIMITS: JSON.stringify(limits) },
    durableObjects: { ROOMS: { className: 'Room', useSQLite: true } },
    ratelimits: {
      ROOM_CREATE: { namespace_id: '7', simple: { limit: create, period: 60 } },
      ROOM_LIMIT: { namespace_id: '8', simple: { limit: requests, period: 60 } },
    },
  }));
}
