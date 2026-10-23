# Relay transport

Status: the encryption and frame code exists, but the app and the Mac do not use it yet. The phone still connects through Tailscale.

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
