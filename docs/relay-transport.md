# Relay transport

The iPhone and the Mac talk only through the Cloudflare relay. The relay forwards ciphertext. It sees route IDs, timing, and sizes, never content. The backend listens only on `127.0.0.1`, so the Mac needs no VPN and no open port.

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

`scripts/tunnel-sidecar.swift` is the Mac end of the tunnel. `scripts/tunnel.py` compiles it with `ios/Tunnel/Noise.swift`, `ios/Tunnel/TunnelFrames.swift`, and `scripts/tunnel-events.swift`, and installs it as the LaunchAgent `com.gumroad.coconut.tunnel`.

```bash
python3 scripts/tunnel.py install
```

- `install` creates the Mac's identity key, registers a route with the relay, and starts the LaunchAgent.
- `status` prints `tunnel/status.json`.
- `uninstall` stops the LaunchAgent. The identity key and the route stay.
- `rebuild` is the last step of each deploy. It runs only after the update is complete, so a rollback never leaves a newer sidecar running. When the sidecar is installed and its sources changed, it compiles the sidecar again and restarts it. A failed restart removes the build receipt, so the next `rebuild` tries again. When the deploy reports a failed sidecar build, the host update stays installed. To try again, run `python3 scripts/tunnel.py rebuild`. A Mac without the sidecar stays without it.

The deploy package carries `ios/Tunnel/*.swift`, so the live install can compile the sidecar. No setup step installs the sidecar on a new Mac yet. `scripts/setup_coconut.py` must install it in the same release as the app on the tunnel (R4). Until then, a Mac with the sidecar makes version 2 pairing links, which current app builds cannot read. Use `pairing.py --legacy` for those builds.

The sidecar keeps one WebSocket to the relay. When a socket that was up for more than 60 seconds ends, the sidecar connects again at once. When an attempt fails, it waits 1 second, then up to 60 seconds. A network change or a wake from sleep starts a new connection after 0.2 seconds. The sidecar sends a text `ping` after 20 silent seconds, and ends a socket that stays silent for 45 seconds.

### Host socket resets

Cloudflare resets long-lived WebSockets from time to time. It restarts the servers of its network for releases, and a Durable Object restarts for a runtime update or a deploy of the Worker. The sidecar then sees a reset by the peer without a close frame, for example `network_error` with `posix` 54 (`ECONNRESET`) or 96 (`ENODATA`), after a few minutes to about 45 minutes. The relay's own code closes the host socket only with a close frame, which the sidecar logs as `relay_closed`, for example 1000 `replaced`. The text `ping` every 20 silent seconds keeps the socket inside Cloudflare's idle timeout.

A reset ends the Noise sessions, because their keys live in the sidecar's channels. The phone does not see the gap:

1. The relay sends `mac_offline` to each open phone session. The phone ends that session with `connectionLost`, which it retries once after 0.4 seconds.
2. The sidecar connects again at once.
3. A phone socket that arrives while no host is connected waits in the relay for up to 5 seconds. The relay keeps its first messages in the socket attachment, which holds 2,048 bytes, so they survive hibernation. The phone's first Noise message is 96 bytes. A phone that sends more than about 1 KB while it waits gets `mac_offline`. The relay gives the kept messages to the next host after `open`. After 5 seconds without a host, the phone gets `mac_offline`.

The sidecar sends each stream to the backend's plain HTTP listener on `127.0.0.1:8961` as one HTTP request. It sets `X-Coconut-Peer` to the iPhone's static key in lowercase hex. The backend reads this header only on that listener.

The sidecar ends the session when a REQ_HEAD has a control character in its method, path, or a header. Such a character could add a header or a second request. A request body over 7 MB, or more than 32 MB of bodies in the sidecar at one time, gets the ERROR `too_large`. A backend that fails before the end of a response gets the ERROR `backend_down`, never END.

The sidecar writes `status.json` when its state changes, and once a minute. `status.json` also holds `reconnects`, the connected sockets after the first one since the sidecar started, and `last_disconnect`, the end of the last connected socket: `{at, cause, code, domain, reason, detail}`. `coconut status` prints `last_disconnect` on the line `Last relay disconnect`.

### Connection events

The sidecar writes its connection events to `tunnel/events.log` as JSON lines. Each line has `at`, a UTC ISO 8601 time, and `event`. A private queue writes the lines, so slow disk I/O never blocks the tunnel. At about 256 KB the file becomes `events.log.1`, and the sidecar starts a new file. Both files have mode 0600.

| Event | Fields |
|---|---|
| `start` | `pid` |
| `connect_start` | `attempt`: the attempt number since the last connected socket |
| `connected` | `attempt`, `took_ms`, `reconnects` |
| `disconnected` | `cause`, `code`, `domain`, `reason`, `phase` (`connecting` or `connected`), `uptime_s` or `after_ms`, `channels` |
| `backoff` | `delay_ms` before the next attempt. A network change or a wake during a wait also adds its `cause`. |
| `channel_open` | `channel`, `auth` (`token` or `ticket`), `channels` |
| `channel_close` | `channel`, `by` (`relay` or `mac`), and for `mac` the `reason` (`handshake_failed`, `session_error`, or `seal_failed`) and the session `error`, for example `invalidRekey` or `decryptionFailed` |
| `channel_refused` | `channel`, `reason`: `limit` |
| `backend_unreachable` | `domain`, `code`, `reason`, `failures` since the last line. At most one line a minute. |
| `rotate_failed` | `errno` of the failed rename to `events.log.1`. The sidecar empties `events.log` to keep it bounded, and tries the rotation again at the next limit. |

The `cause` of a disconnect is one of these:

- `relay_closed`: the Worker closed the socket. `code` and `reason` come from its close frame, for example `1000` and `replaced` when another host socket takes the route.
- `network_error`: the network or TLS stack ended the socket. `domain` is `posix`, `dns`, `tls`, or a Foundation error domain, and `code` is the error code.
- `ping_timeout`: the relay sent nothing for 45 seconds.
- `wake`: the Mac woke from sleep.
- `network_changed`: the network status or the primary interface changed. `reason` names the new path, for example `satisfied en0`.
- `invalid_url`: the relay URL in `route.json` is not valid.

The log holds no tokens, keys, routes, ticket hashes, or payloads. The sidecar removes bearer tokens and strings of 22 or more key characters from each reason, and keeps at most 160 characters.

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

`scripts/pairing.py` makes a version 2 setup when `tunnel/route.json` exists. `--share` prints it in a version 3 link.

The setup has these values:

- `secret` is 32 random bytes in unpadded base64url. It is valid once, for 10 minutes.
- `mac_key` is the Mac's static public key in unpadded base64url.
- `expires` is a Unix time in seconds.

The version 3 link:

```
coconut://pair/<token>
```

The token is unpadded lowercase base32 (RFC 4648) of these bytes:

| Bytes | Value |
| --- | --- |
| 1 | Link version, 3 |
| 32 | `route` |
| 32 | `mac_key` |
| 32 | `secret` |
| 4 | `expires`, big-endian |
| 2 | Length of `relay`, big-endian |
| Length of `relay` | `relay`, UTF-8 |
| The rest | `name`, UTF-8 |

Hermes masks text that looks like a secret before its agent sends the link. Base64url JSON always starts with `eyJ`, so Hermes masks it as a JSON Web Token. No rule of Hermes 0.22.4 or 0.22.5 matches the token. `backend/tests/test_native_pairing_redaction.py` checks this with the redaction functions of the installed Hermes, in the native CI job for the minimum and the latest Hermes.

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

## Pairing code from the iPhone

The iPhone can start pairing without a link from the Mac. It makes a one-time pairing code, and the person sends the code to their Hermes. On the Mac, `coconut pair <code>` sends a version 2 setup to the iPhone through a rendezvous box on the relay. Then the iPhone pairs with the steps of pairing v2.

The code is 16 random bytes (128 bits), written as 26 characters of unpadded lowercase base32. No redaction rule of Hermes matches lowercase base32. The iPhone and the Mac derive these values from the 16 code bytes with HKDF-SHA256, an empty salt, and a 32-byte output:

| Value | Label |
| --- | --- |
| Box ID | `coconut rendezvous id`, as lowercase hex |
| Box key | `coconut rendezvous key` |

Test vector: the code `aaaqeayeaudaocajbifqydiob4` is the bytes 0 to 15. Its box ID is `1d228ab884d178483a0e0e42db07204474f127216879f5b13ccca51c1a965173`, and its box key in hex is `f68c3bdb1899514c12a0964dd1ea608b89d66740c96cfa5f355beba30cc103d2`.

The iPhone also makes a 32-byte phone token, as lowercase hex. The phone token never leaves the iPhone.

The sealed message is `nonce (12 bytes) | ciphertext | tag (16 bytes)`: ChaChaPoly with the box key, a random nonce, and the 32 bytes of the box ID as the additional data. The plaintext is the JSON of a version 2 setup, with the fields of the earlier link.

The relay routes (`relay/src/rendezvous.js`):

| Request | Who | Answer |
| --- | --- | --- |
| `PUT /v1/rendezvous/<id>`, `Authorization: Bearer <phone token>` | iPhone | 201 makes the box for 600 seconds. 200 for a repeat with the same token. 409 for another token. |
| `POST /v1/rendezvous/<id>/message`, body: the sealed message | Mac | 204. 404 for a missing or expired box. 409 for a second message. 400 under 29 bytes, 413 over 4096 bytes. |
| `GET /v1/rendezvous/<id>?wait=<0 to 25>`, same token | iPhone | 200 with the message, 204 when no message comes within the wait, 404 for a missing or expired box, 401 for another token. |
| `DELETE /v1/rendezvous/<id>`, same token | iPhone | 204 removes the box. |

The relay keeps the message until the iPhone deletes the box or the box expires. iOS can suspend Coconut while the person is in Telegram, so a reply that the iPhone does not receive must not lose the setup. The relay limits each IP to 10 box creations and 60 other rendezvous requests each minute.

The pairing steps:

1. The iPhone makes the code and the phone token, makes the box, and shows the message for Hermes.
2. Hermes runs `coconut pair <code>` on the Mac. The command makes a version 2 setup, seals it, and sends it to the box.
3. The iPhone reads the message, opens it with the box key, and deletes the box.
4. The iPhone pairs with the setup, as in steps 3 to 7 of pairing v2.

The relay sees only the box ID and the ciphertext, so it cannot read the setup or seal another one. The code passes through the chat service that carries the message to Hermes, for example Telegram, which does not encrypt a chat with a Telegram bot end to end. Someone who reads the code there within 10 minutes could seal a setup for another Mac. The code works for one box only, the box expires after 10 minutes, and the iPhone deletes it after it receives a setup. After that, the code is useless.

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
| `connecting` | A handshake is in progress, or an open socket ended (`connectionLost`). | Shows the cached state, and no banner. The next request connects again. |
| `connected` | The last request succeeded. | Normal use. |
| `macOffline` | The relay sends `mac_offline` during the handshake (no Mac for 5 seconds), the Mac does not answer the handshake on an open socket, or the backend is down. | A calm banner. The cached state stays visible, and the phone tries again. |
| `relayUnreachable` | The phone cannot open a socket, a request times out, or the Mac sends data that is not valid. | A calm banner. The phone tries again. |
| `needsRepair` | The relay refuses the token or route (401 or 404, or `revoked`), the Mac refuses the phone key or answers with another Mac key, or the backend answers 401. | A banner with Pair. The phone stops polling until it pairs again. |

The phone's static key and the relay client token live in the Keychain (`local.coconut.tunnel`). Each pairing makes a new key. `pairing.json` holds `{version: 2, relay, route, macKey, deviceId, macName}`.

`pairing.json` and the saved state have complete file protection. A locked iPhone refuses to read them, and a Mac with a locked screen too. An update often starts the Mac app again behind a locked screen. An app that starts then keeps its pairing and shows no pairing page. It reads the files again each second, and it connects when the read works. The app writes each such start to the system log (subsystem `com.gumroad.coconut`, category `pairing`).

## Backend listeners

The backend listens with plain HTTP on two loopback ports:

- `127.0.0.1:8960` for the tools on this Mac. A tool pairs there with a code from `host.py pair`.
- `127.0.0.1:8961` for the sidecar. Only this listener reads `X-Coconut-Peer`. The deploy checks `/health` here.

## Loopback test

`scripts/test_tunnel_loopback.py` runs the test client, a Miniflare relay, the sidecar, and the backend on one Mac. The test client uses the app's `TunnelConnection` to pair and to send its requests. The client pairs, gets `/v1/state`, uploads a 5 MB attachment, and downloads 3.9 MB. It also checks that another iPhone key and a used ticket get no session, and that the sidecar reconnects. At the end, the test stops the sidecar during a session and starts it again a second later. The phone's next request must succeed on its first attempt. It needs the Xcode Command Line Tools and `npm ci` in `relay/`.

```bash
python3 scripts/test_tunnel_loopback.py
```
