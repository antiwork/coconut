// Serves the room routes of the relay on 127.0.0.1 for scripts/test_team_log.py. It is not a test file:
// `npm test` runs only *.test.js. It prints the relay URL as its first line, and stops on SIGTERM.
//
//     node test/room-server.mjs [port] [folder]
//
// With a folder, the rooms stay in that folder, so a test can stop the server and start it again with the same rooms.
import { roomRuntime } from './room-runtime.mjs';

const [port, folder] = process.argv.slice(2);
const runtime = roomRuntime({ host: '127.0.0.1', port: Number(port || 0), ...(folder ? { resourcePersistencePath: folder } : {}) });
const url = await runtime.ready;
console.log(url.origin);
const stop = async () => { await runtime.dispose(); process.exit(0); };
process.on('SIGTERM', stop);
process.on('SIGINT', stop);
