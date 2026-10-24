# Relay transport

Status: the Mac has the tunnel sidecar and pairing v2 (R3), and the app pairs and connects through the tunnel (R4). A phone that paired before R4 keeps its Tailscale and TLS connection until it pairs again with a version 2 link. R5 removes TLS.

The iPhone and the Mac will talk through the Cloudflare relay, not through Tailscale. The relay forwards ciphertext. It sees route IDs, timing, and sizes, never content.

## Encryption

The iPhone and the Mac tunnel sidecar compile the same files: `ios/Tunnel/Noise.swift` and `ios/Tunnel/TunnelFrames.swift`. They use CryptoKit only.

- **Pairing:** `Noise_IKpsk2_25519_ChaChaPoly_SHA256`. The pre-shared key comes from the one-time secret in the pairing link.
- **Normal sessions:** `Noise_IK_25519_ChaChaPoly_SHA256`.

The iPhone is always the initiator. It knows the Mac's static key from the pairing link, so only that Mac can finish the handshake.

The Mac learns the iPhone's static key from the first message. The backend binds that key to the device during pairing.

The first handshake message carries no application data. Each WebSocket connection starts with a new handshake.

Nonces count up strictly in each direction. A changed, repeated, or skipped message closes the session. The iPhone then reconnects with a new handshake.

Each side rekeys its send key after 2^16 messages or 30 minutes. It sends a REKEY frame first. A REKEY frame has stream 0, no flags, and no payload.

The handshake accepts any iPhone key. After it reads the first message, the Mac must check the iPhone's static key against its paired devices before it answers.

`NoiseTests` checks the code against the official test vectors from [cacophony](https://github.com/haskell-cryptography/cacophony/blob/master/vectors/cacophony.txt).

## Frames

The relay forwards `RelayFrame`: `channel u32 (big-endian) | ciphertext`. A relay frame is at most 65,539 bytes: the channel and one Noise message.

- **Toward the Mac:** the relay sets the channel to the iPhone's socket.
- **Toward the iPhone:** the relay removes the channel.
- **Channel 0:** carries relay control messages from the Mac, never tunnel data.

One ciphertext holds one `TunnelFrame`: `type u8 | stream u32 (big-endian) | flags u8 | payload`. The payload is at most 60 KB, so a frame fits in one Noise message of at most 65,535 bytes.

| Type | Value | Payload |
| --- | --- | --- |
| REQ_HEAD | 1 | JSON `{method, path, headers}` |
| BODY | 2 | Part of a request or response body |
| END | 3 | Empty. The body of the stream is complete. |
| RESP_HEAD | 4 | JSON `{status, headers}` |
| CANCEL | 5 | Empty. The sender no longer wants the stream. |
| PING / PONG | 6 / 7 | Empty |
| REKEY | 8 | Empty. The sender rekeys its send key after this frame. |
| WINDOW | 9 | `u32` bytes that the receiver may now send on the stream |
| ERROR | 10 | JSON `{code}`: `mac_offline`, `backend_down`, or `too_large` |

The iPhone opens odd stream numbers. Each stream starts with a window of 1 MB, so a large state download, file transfers, and long polls share one socket.

## Mac tunnel sidecar

`scripts/tunnel-sidecar.swift` is the Mac end of the tunnel. `scripts/tunnel.py` compiles it with `ios/Tunnel/Noise.swift` and `ios/Tunnel/TunnelFrames.swift`, and installs it as the LaunchAgent `com.gumroad.coconut.tunnel`.

```bash
python3 scripts/tunnel.py install
```

- `install` creates the Mac's identity key, registers a route with the relay, and starts the LaunchAgent.
- `status` prints `tunnel/status.json`.
- `uninstall` stops the LaunchAgent. The identity key and the route stay.
- `rebuild` is the last step of each deploy. It runs only after the update is complete, so a rollback never leaves a newer sidecar running. When the sidecar is installed and its sources changed, it compiles the sidecar again and restarts it. A failed restart removes the build receipt, so the next `rebuild` tries again. When the deploy reports a failed sidecar build, the host update stays installed, and `scripts/automatic_update.py` runs `tunnel.py rebuild` again every 10 minutes until it succeeds, also when the files already match. The retry holds the deploy lock, so it never runs at the same time as a deployment. A Mac without the sidecar stays without it.

The deploy package carries `ios/Tunnel/*.swift`, so the live install can compile the sidecar. No setup step installs the sidecar on a new Mac yet. `scripts/setup_coconut.py` must install it in the same release as the app on the tunnel (R4). Until then, a Mac with the sidecar makes version 2 pairing links, which current app builds cannot read. Use `pairing.py --legacy` for those builds.

The sidecar keeps one WebSocket to the relay. When the socket fails, it connects again after 1 second, then after up to 60 seconds. A network change or a wake from sleep starts a new connection at once. The sidecar sends a text `ping` after 20 silent seconds, and ends a socket that stays silent for 45 seconds.

The sidecar sends each stream to the backend's plain HTTP listener on `127.0.0.1:8961` as one HTTP request. It sets `X-Coconut-Peer` to the iPhone's static key in lowercase hex. The backend reads this header only on that listener.

The sidecar ends the session when a REQ_HEAD has a control character in its method, path, or a header. Such a character could add a header or a second request. A request body over 7 MB, or more than 32 MB of bodies in the sidecar at one time, gets the ERROR `too_large`. A backend that fails before the end of a response gets the ERROR `backend_down`, never END.

The sidecar writes `status.json` when its state changes, and once a minute.

The relay holds at most 8 pairing tickets for a route. A ninth link waits on the Mac, and goes to the relay when a ticket is used or expires.

If the sidecar cannot save the new key in `peers.json`, the phone gets the ERROR `backend_down` instead of the pairing answer, and must pair again. Without the saved key, the phone would lose the Mac at the next start of the sidecar.

The sidecar keeps its files in `<data>/tunnel`. Each file has mode 0600, and the folder has mode 0700.

| File | Content |
| --- | --- |
| `identity.key` | The Mac's Curve25519 private key |
| `route.json` | `{relay, route, host_token, mac_key}` |
| `peers.json` | The iPhone keys that paired through the tunnel, each with the hash of its relay client token |
| `tickets/<hash>.json` | `{psk, expires}` for one pairing link, until its use or expiry |
| `status.json` | `{state, relay, route, channels, connected_since, last_error, updated, pid}` |

## Pairing v2

`scripts/pairing.py` makes a version 2 link when `tunnel/route.json` exists. `--share` prints it. `--legacy` makes a version 1 link for app builds that use TLS.

```
coconut://pair?setup=<base64url {version:2, relay, route, mac_key, secret, expires, name}>
```

- `secret` is 32 random bytes in unpadded base64url. It is valid once, for 10 minutes.
- `mac_key` is the Mac's static public key in unpadded base64url.
- `expires` is a Unix time in seconds.

The iPhone and the Mac derive these values with HKDF-SHA256, an empty salt, and a 32-byte output:

| Value | Input | Label |
| --- | --- | --- |
| Relay ticket | `secret` | `coconut pairing ticket`, as lowercase hex |
| Noise pre-shared key | `secret` | `coconut pairing psk` |
| Relay client token | Handshake hash of the pairing session | `coconut relay client`, as lowercase hex |

The pairing steps:

1. `pairing.py` gets a secret from the backend. The backend stores only the hash of the secret.
2. `pairing.py` writes the ticket hash and the pre-shared key to `tunnel/tickets`. The sidecar sends the ticket hash to the relay.
3. The iPhone connects with `Authorization: Ticket <ticket>` and an IKpsk2 handshake.
4. The iPhone sends `POST /v1/pair {code: secret, device_name}` through the tunnel. The backend keeps the rate limit of 5 attempts each minute.
5. The backend binds the `X-Coconut-Peer` key to the new device.
6. Before the sidecar sends the 200 answer, it allows the client token on the relay and records the key in `peers.json`.
7. The iPhone connects again with `Authorization: Bearer <client token>` and an IK handshake.

On a ticket session, the sidecar forwards only `POST /v1/pair` and `GET /health` until pairing succeeds. On a token session, the sidecar answers the handshake only for the iPhone key that belongs to that token.

The backend accepts a tunnel secret only with an `X-Coconut-Peer` key, and a TLS code only without one. A device that paired through the tunnel must send the same key with each request.

## iPhone

`ios/Coconut/TunnelConnection.swift` is the iPhone end. `APIClient` keeps its API and sends each request through it.

- The app keeps one socket while it is in the foreground. Each request is one stream, with a 1 MB window in each direction. The phone extends the Mac's window after it reads 256 KB.
- A short foreground read has 6 seconds, including the connection. Writes, long polls (`updates?wait=20`), and full downloads have 45 seconds. A timeout sends CANCEL and keeps the socket.
- The phone sends a text `ping` after 20 silent seconds, and ends a socket that stays silent for 45 seconds.
- The phone asks for gzip and removes it itself, because URLSession is not in the path.
- A failed socket fails its streams. The next request opens a new socket with a new handshake.
- In the background, read streams end at once. The socket closes after the writes in flight finish. Notifications stay on APNs.

The app shows these connection states:

| State | Cause | What the app does |
| --- | --- | --- |
| `connecting` | A handshake is in progress. | Shows the cached state. |
| `connected` | The last request succeeded. | Normal use. |
| `macOffline` | The relay sends `mac_offline`, the Mac does not answer the handshake on an open socket, or the backend is down. | A calm banner. The cached state stays visible, and the phone tries again. |
| `relayUnreachable` | The phone cannot open a socket, or the socket fails. | A calm banner. The phone tries again. |
| `needsRepair` | The relay refuses the token or route (401 or 404, or `revoked`), the Mac refuses the phone key or answers with another Mac key, or the backend answers 401. | A banner with Pair. The phone stops polling until it pairs again. |

The phone's static key and the relay client token live in the Keychain (`local.coconut.tunnel`). Each pairing makes a new key. `pairing.json` holds `{version: 2, relay, route, macKey, deviceId, macName}`. A pending checkout recovery belongs to the route and the Mac key.

A version 1 link shows "Coconut no longer needs Tailscale. Ask your Hermes for a new pairing link." `scripts/setup_coconut.py` installs the sidecar before it makes the link, so it always makes a version 2 link.

## Transition

Until R5, the backend listens on two ports:

- TLS on `:8960`, for phones that paired before R4. They keep working until they pair with a version 2 link.
- Plain HTTP on `127.0.0.1:8961`, for the sidecar. `host.py` adds `--tunnel-port 8961` to the service command.

## Loopback test

`scripts/test_tunnel_loopback.py` runs the test client, a Miniflare relay, the sidecar, and the backend on one Mac. The test client uses the app's `TunnelConnection` to pair and to send its requests. The client pairs, gets `/v1/state`, uploads a 5 MB attachment, and downloads 3.9 MB. It also checks that another iPhone key and a used ticket get no session, and that the sidecar reconnects. It needs the Xcode Command Line Tools and `npm ci` in `relay/`.

```bash
python3 scripts/test_tunnel_loopback.py
```
