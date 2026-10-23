// The Miniflare setup of the tunnel routes, shared by tunnel.test.js and tunnel-server.mjs.
import { readFileSync } from 'node:fs';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';

export function tunnelRuntime({ register = 100, connect = 1000, ...options } = {}) {
  return new Miniflare(convertV4MiniflareOptions({
    ...options,
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
      TUNNEL_REGISTER: { namespace_id: '3', simple: { limit: register, period: 60 } },
      TUNNEL_CONNECT: { namespace_id: '4', simple: { limit: connect, period: 60 } },
    },
  }));
}
