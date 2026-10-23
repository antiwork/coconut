// Serves the tunnel routes of the relay on 127.0.0.1 for scripts/test_tunnel_loopback.py. It is not a test file:
// `npm test` runs only *.test.js. It prints the relay URL as its first line, and stops on SIGTERM.
import { tunnelRuntime } from './tunnel-runtime.mjs';

const runtime = tunnelRuntime({ host: '127.0.0.1', port: Number(process.argv[2] || 0) });
const url = await runtime.ready;
console.log(url.origin);
const stop = async () => { await runtime.dispose(); process.exit(0); };
process.on('SIGTERM', stop);
process.on('SIGINT', stop);
