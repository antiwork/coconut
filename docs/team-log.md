# Team log

A team is a set of Macs that share one log. The log lives on the Cloudflare relay as sealed entries. Each member Mac copies the log into its own store. The relay sees IDs, times and sizes, never content.

The log carries everything that a team shares: the people and bots of the team, the messages of the Team chat, and the shared pages. A phone talks only to its own Mac, as before.

Status: the relay has the room (`relay/src/room.js`). The Mac side does not exist yet, so no Mac makes or joins a room.

## Terms

- **Room:** the object on the relay that holds one log, its members and its tickets. A team has one room.
- **Member:** one Mac in a room. One person and the bots of that person are behind it.
- **Admin:** the member that made the room. Only the admin invites and removes members.
- **Entry:** one sealed item of the log.
- **Epoch:** the number of the room key. It starts at 0 and grows by 1 with each key change.

## Keys

| Key | Made by | Use |
| --- | --- | --- |
| Room key | The admin's Mac | 32 random bytes. Seals each entry of its epoch. |
| Member key | Each Mac, for each room | Ed25519. Signs each entry of the Mac, and the proof of a join. Its public key in lowercase hex is the member ID. |
| Agreement key | Each Mac, for each room | X25519. Receives the new room key after a key change. |
| Member token | Each Mac, for each room | 32 random bytes in lowercase hex. Authorizes the Mac at the relay. |
| Invite secret | The admin's Mac, for each invite | 32 random bytes. Gives the relay ticket and the welcome key. |

The relay stores only the SHA-256 hash of a token or a ticket, as lowercase hex of the hash of the token string. The tunnel does the same.

The Mac backend uses only the Python standard library, so a Swift helper with CryptoKit seals, opens, signs and verifies. One Swift file holds the format, as `ios/Tunnel/Noise.swift` does for the tunnel.

## Entry

The relay keeps these fields for each entry:

| Field | Set by | Meaning |
| --- | --- | --- |
| `seq` | Relay | The position in the log. Each write gets the next number. A number is never used again. |
| `first` | Relay | The `seq` of the first write of this entry ID. It stays when the entry is replaced. |
| `id` | Member | The entry ID: 16 random bytes in lowercase hex. |
| `member` | Relay | The member ID of the token that wrote the entry. |
| `at` | Relay | The time of the write, in milliseconds since 1970. |
| `kept` | Member | `true`: the entry stays until its member replaces it. `false`: the relay deletes it after the retention time. |
| `body` | Member | The sealed bytes. |

A write with an entry ID that already exists replaces that entry. Only the member that wrote the entry can replace it. The relay keeps only the newest write of each entry ID.

This one rule gives message edits, a reply that grows while a bot writes, and page versions. A Mac sorts a chat by `first`, so a replaced message keeps its place.

### Body

```
body      = version u8 (1) | epoch u32 (big-endian) | nonce (12 bytes) | ciphertext | tag (16 bytes)
plaintext = signature (64 bytes) | payload
```

- The seal is ChaChaPoly with the room key of the epoch. The nonce is 12 random bytes, new for each seal.
- The additional data is `version | epoch | room ID (32 bytes) | entry ID (16 bytes) | member ID (32 bytes)`: 85 bytes.
- The signature is Ed25519 with the member key over `"coconut room entry v1" | room ID | entry ID | epoch | payload`.
- The payload is one JSON object in UTF-8.

The seal proves that the writer has the room key. The signature proves which member wrote the entry. The additional data and the signed bytes bind an entry to its room, its entry ID and its member. So the relay cannot move a body to another place.

### Payload

Each payload has these fields:

| Field | Meaning |
| --- | --- |
| `kind` | The kind of the entry. |
| `rev` | An integer from 1. It grows with each replacement of the entry. |
| `at` | The time of the author's Mac, ISO 8601 in UTC. |

| Kind | `kept` | Author | Fields |
| --- | --- | --- | --- |
| `member` | `true` | Each member, one entry | `name` (at most 80 characters), `avatar` (at most 80), `color` (at most 20), `agreement` (the public agreement key) |
| `delete` | `false` | The member of the replaced entry | None. It replaces an entry that its author deletes. |
| `bot` | `true` | The Mac that runs the bot | The name and the picture of a team bot. |
| `page` | `true` | The Mac that runs the bot of the page | One shared page. |
| `message` | `false` | Any member | One message of the Team chat, from a person or a bot. |
| `remove` | `true` | The admin | The removal of a member. See [Member removal](#member-removal-and-key-change). |
| `key` | `true` | The admin | The room key of the next epoch. |

The kinds `member` and `delete` are complete. Each other kind gets its fields in the pull request that builds it. A new kind needs no change of the relay and no change of the body.

A key in a payload is unpadded base64url, as in a pairing setup.

### Rules for a Mac that reads an entry

1. If the version of the body is later than the Mac knows, keep the bytes and try again after the next update of Coconut. This check comes first, because a later version can have another form.
2. Open the body with the room key of its epoch. Then check the signature with `member`. If one of them fails, drop the entry and write a log line.
3. If the Mac already has this entry ID, accept the entry only from the same member and only with a higher `rev`.
4. Accept `remove` and `key` only from the admin. The admin is the member ID that the welcome names.
5. If the kind is unknown, keep the payload and show nothing.

## Relay routes

The routes of a room start with `/v1/rooms/<room ID>`. The room ID is 32 random bytes in lowercase hex. A member sends `Authorization: Bearer <member token>`. A missing room answers 404, and a token that is not a member's answers 401. A member that is not the admin gets 403 on a route of the admin.

| Request | Who | Answer |
| --- | --- | --- |
| `PUT /v1/rooms/<id>`, body `{"member": m, "hash": h, "proof": p}` | The first Mac | 201 makes the room, with member `m` as the admin. 200 for a repeat with the same `m` and `h`. 409 for a room of another admin. 401 for a proof that fails. |
| `POST /v1/rooms/<id>/tickets`, body `{"hash": h, "ttl": s, "uses": n, "welcome": w}` | Admin | 204. 409 when 8 tickets are open. |
| `DELETE /v1/rooms/<id>/tickets/<h>` | Admin | 204 removes the ticket and its welcome. |
| `POST /v1/rooms/<id>/members`, `Authorization: Ticket <ticket>`, body `{"member": m, "hash": h, "proof": p}` | A new Mac | 200 with `{"welcome": w}`, and the ticket loses one use. 200 without a new use for a repeat with the same `m` and `h`. 401 for a proof that fails, and for a ticket that is unknown, expired or without a use left. 409 when `m` exists with another token, or the room is full. |
| `GET /v1/rooms/<id>/members` | Member | 200 with `{"members": [{"member": m, "admin": true, "joined": t}]}`. |
| `DELETE /v1/rooms/<id>/members/<m>` | Admin, or member `m` | 204. The relay removes the token, closes the socket and deletes the kept entries of `m`. 409 when `m` is the admin and the room has other members. When the admin leaves a room without other members, the relay deletes the room. |
| `POST /v1/rooms/<id>/entries`, body `{"id": i, "kept": k, "body": b}` | Member | 200 with `{"seq": n, "first": f}`. 409 when the entry ID belongs to another member. 413 for a body over the limit. 429 over the write limit. 507 when the entries of the member or of the room are full. |
| `GET /v1/rooms/<id>/entries?after=<seq>` | Member | 200 with `{"entries": [...], "last": n}`: the entries with a higher `seq`, in order. `last` is the highest `seq` that the room has now. Without `after`, the answer starts at the first entry. |
| `GET /v1/rooms/<id>/socket`, as a WebSocket | Member | 101. See [Socket](#socket). |
| `DELETE /v1/rooms/<id>` | Admin | 204 deletes the room with all its data, and closes each socket. |

- `hash` is the SHA-256 hash of a member token or of a ticket. `ttl` is in seconds.
- `proof` is the Ed25519 signature of the member key over `"coconut room member v1" | room ID (32 bytes) | hash (32 bytes)`. It shows that the Mac has the key of its member ID. Without it, a Mac could take the ID of a removed member and replace the entries that this member left.
- `welcome`, `body` and `proof` are standard base64 in JSON.
- An entry in an answer has the fields of the [entry table](#entry).
- One answer of `GET .../entries` holds at most 256 entries and at most 1 MiB of bodies. An entry that is larger than the rest of that limit comes alone. A Mac reads again while `last` is higher than the last `seq` that it got.

### Socket

The socket only tells a Mac that the log has new entries. The Mac then reads them with `GET .../entries`.

- The relay sends the text `{"seq": n}` after the connection opens, and after each write of another member. `n` is the highest `seq` of the room. The writer has the number in the answer of its write.
- The text `ping` gets the automatic reply `pong`, as on the tunnel sockets.
- A member has one socket in a room. A new socket of the same member closes the earlier one with 1012.
- The relay closes the socket of a removed member with 1008, and a socket that sends other text with 1003.
- When the room goes, the relay closes each socket with 1000.

A long poll would keep the Durable Object in memory, and Cloudflare bills that time. A socket that hibernates has no such cost while the room is quiet. The tunnel object uses the same method.

### Limits

| Limit | Value |
| --- | --- |
| Members of a room | 64 |
| Open tickets of a room | 8 |
| Life of a ticket (`ttl`) | 60 seconds to 7 days |
| Uses of a ticket | 1 to 64 |
| Welcome | At most 1,024 bytes |
| Body of an entry | 99 bytes to 1 MiB |
| Kept entries of one member | 512 entries and 64 MiB |
| Other entries of one member | 64 MiB |
| All entries of one room | 8 GiB |
| Retention of an entry that is not kept | 90 days after its last write |
| Writes of one member | 300 each minute |
| New rooms | 10 each hour for one IP address, as for tunnel routes |
| Other room requests | 3,000 each minute for one IP address |
| Room without a request of a member | Deleted after 180 days |

A member that leaves and joins again continues its counts: its entries that are not kept stay on the relay, and so does its count of writes in the minute.

A page can have 512 KiB of content (`MAX_CONTENT` in `backend/coconut/documents.py`), so a page fits in one entry. Each Mac keeps the full history of the chat in its own store. The retention time only limits how far back a new member or a Mac that was off can read.

## Invite and join

The link does not hold the room key. It holds an invite secret, and the relay holds the room key in a sealed welcome.

```
coconut://team/<token>
```

The token is unpadded lowercase base32 (RFC 4648) of these bytes. The pairing link uses the same encoding, because Hermes masks text that looks like a secret. `backend/tests/test_native_pairing_redaction.py` must check this link too.

| Bytes | Value |
| --- | --- |
| 1 | Link version, 1 |
| 32 | Room ID |
| 32 | Invite secret |
| 4 | `expires`, a Unix time in seconds, big-endian |
| 2 | Length of `relay`, big-endian |
| Length of `relay` | `relay`, UTF-8 |
| The rest | `name` of the team, UTF-8 |

`relay` is an HTTPS origin without a path, a query or account details, as in a pairing link, and its host is a name, not an address in numbers. So a changed link cannot send a Mac over plain HTTP or to an address that the link gives in numbers. The rule does not know where a host name points. `name` has 1 to 80 characters, no space at its start or end, and no control character, because the app shows it before the person joins. A Mac refuses a link or a welcome with another `relay` or `name`.

Both Macs derive these values from the invite secret with HKDF-SHA256, an empty salt, and a 32-byte output:

| Value | Label |
| --- | --- |
| Relay ticket | `coconut room ticket`, as lowercase hex |
| Welcome key | `coconut room welcome` |

The welcome is `nonce (12 bytes) | ciphertext | tag (16 bytes)`: ChaChaPoly with the welcome key and the 32 bytes of the room ID as the additional data. The plaintext is this JSON:

```json
{"version": 1, "epoch": 0, "key": "<room key>", "admin": "<member ID>", "name": "<team name>"}
```

To make a room:

1. The Mac makes the room ID, the room key of epoch 0, its member key, its agreement key and its member token.
2. The Mac sends `PUT /v1/rooms/<id>` with its proof.
3. The Mac writes its `member` entry.

To invite:

1. The admin's Mac makes an invite secret and seals the welcome.
2. The Mac sends the hash of the ticket and the welcome with `POST .../tickets`.
3. The person shares the link.

To join:

1. The person opens the link on the phone, and the phone gives it to its Mac.
2. The Mac makes its member key, its agreement key and its member token.
3. The Mac sends `POST .../members` with the ticket and its proof, and gets the welcome.
4. The Mac opens the welcome. If the welcome does not open, the Mac leaves the room with `DELETE .../members/<m>`.
5. The Mac writes its `member` entry, reads the log from `after=0`, and opens the socket.

The link passes through the chat service that carries it, for example Telegram. Someone who reads the link while its ticket has a use left can join the room. Each Mac then shows the new member, and the admin can remove it.

After its last use, a ticket admits no new member. The relay keeps the ticket and its welcome until the `ttl` ends. A Mac that lost the answer of its join can then ask again with the same `member` and `hash`. After the `ttl`, the relay deletes both, and the link is useless. The admin can remove a ticket earlier.

## Member removal and key change

The first pull requests do not build this part. The format already has what it needs: the epoch, the agreement key, and the kinds `remove` and `key`.

1. The admin's Mac removes the open tickets, then sends `DELETE .../members/<m>`. The removed Mac can read and write no more.
2. The admin's Mac writes a `remove` entry with the member ID and the name of the removed member.
3. The admin's Mac makes a new room key. It seals that key for the agreement key of each other member with HPKE (`Curve25519_SHA256_ChachaPoly`, as for push previews). It writes one `key` entry with these sealed keys, in the old epoch.
4. Each Mac opens its sealed key and uses the new epoch for its next writes.
5. Each Mac writes its kept entries again in the new epoch, so a later member can open them.

A removed Mac keeps what it already read. Without the key change, a removed member that also reads the relay's storage could open later entries. With it, that member cannot.

## What the relay sees

The relay stores the room ID, the member IDs, the hashes of tokens and tickets, and for each entry the fields of the entry table. It sees which member writes, when, and how many bytes. It sees no name, no message, no page, and no room key. `relay/README.md` allows no plaintext field, and this contract adds none.

The relay accepts a member ID only with a proof of its key, so one member cannot write as another member. The relay itself can hold back an entry or give an old one. It cannot make an entry, change one, or move one to another member, entry ID or room. The `rev` rule stops an old write of an entry on a Mac that already has a newer one.

## Test vectors

Three implementations agree on these values: Node's `crypto`, CryptoKit, and the Python standard library (HKDF and the link only). The HKDF is the one of the pairing code: it gives the rendezvous vector of [relay transport](relay-transport.md#pairing-code-from-the-iphone).

Inputs, as hex:

| Input | Value |
| --- | --- |
| Invite secret | The bytes `00` to `1f` |
| Room ID | The bytes `20` to `3f` |
| Entry ID | The bytes `40` to `4f` |
| Member key (the private seed) | The bytes `50` to `6f` |
| Room key | The bytes `70` to `8f` |
| Epoch | 0 |
| Nonce of the entry | The bytes `90` to `9b` |
| Nonce of the welcome | The bytes `a0` to `ab` |
| Payload | `{"kind":"member","rev":1,"name":"Ada"}` |
| Relay, name, expires | `https://relay.example`, `Example Team`, 1800000000 |
| Member token | The bytes `b0` to `cf`, as 64 hex characters |

Results:

- Relay ticket: `c1bcb375f34955ef6b9e75cc15059d55bf46a85b0e61fa98e4d7a9de16e3b5e1`
- Hash of the ticket: `f22e44d35d61359d57e918b9e46b805a3591a804b845080fd7416cdc2a612eb0`
- Welcome key: `7055dc78eb078daebc2c831cfd49248924bbc842d8b7b4aaa244737974aa3b83`
- Member ID: `3f7708d5f5cc2bc633b59d2b3a2ed92e7479220c6f08ade208bebcd8580ab93b`
- Signature (RFC 8032): `a796ab19cc05a31ddfb863a5101eb5a53c26bf3873c968293868fbebb5dd217f88ccf8e6a776b8b16bfa68a4bfe3a260cf1c8402a973652880ecca97963ed002`

Body, 135 bytes:

```
0100000000909192939495969798999a9b3dc6466e523666ef6e740edf8ec9e63d7cb603f63968e207c181b8a81597764cc292af10ed9bd3b58b46badbf645e0b0a2bb6315ccba94c7b089151d14927bf4494f5ccec298f0d72467ae0d11cca33a42029f69407325375b2dc8da1e906baa85545c47a14ef9ab7c4a29555af5b8ffd519c73fd373
```

CryptoKit adds random bytes to each Ed25519 signature, so a body that CryptoKit seals from these inputs differs. Each implementation must open the body above and get the payload. It must refuse the body with another entry ID, with another member ID, with another epoch in its header, or with one changed byte.

Plaintext of the welcome:

```json
{"version":1,"epoch":0,"key":"cHFyc3R1dnd4eXp7fH1-f4CBgoOEhYaHiImKi4yNjo8","admin":"3f7708d5f5cc2bc633b59d2b3a2ed92e7479220c6f08ade208bebcd8580ab93b","name":"Example Team"}
```

Welcome, 200 bytes. A seal of the plaintext with the nonce above must give exactly these bytes:

```
a0a1a2a3a4a5a6a7a8a9aaabda8922b84e01f1b512a2cf53cff6685c32a45a60612033abab0e79079016c0888927292eeeb3cc2d461f9c01fffdba1769b9351e70d02f7fb5d2111569322ca3b369c82d5c3fc250b9dac19e1b908c7bb52d8528191575c5337909dedb65a86dddba2d39407cce1a90e75318e7825291e5f0543b4085f777e121daa5157c498170b6cee22dc9d7c8f4444acd196b02877565f016aefc07ba26506462f1ae40e555299323d31f25e0ef44f5563707469473e4f402151b7e88aff20ed3
```

Proof of the member for this room and this member token:

- Hash of the member token: `796b2e4cd51a1e5c8c5346d578100dd667264b6c26b5a1dfca876f2e0fb104a5`
- Proof (RFC 8032), as base64: `9//Nt2eHY2C17r4Mb2qhzMYlw/Frovnl8OfRJZc6eTNDOFiq9+nfJ4umm2EeFWWcIcE/YnFIhBuQr7Cf7akrBg==`

The relay's check (WebCrypto) and CryptoKit accept this proof, and the relay accepts a proof that CryptoKit makes.

Link:

```
coconut://team/aeqccirdeqssmjzifevcwlbnfyxtamjsgm2dknrxha4tuoz4hu7d6aabaibqibiga4eascqlbqgq4dyqcejbgfavcylrqgi2dmob2hq7nne5eaaacvuhi5dqom5c6l3smvwgc6jomv4gc3lqnrsuk6dbnvygyzjakrswc3i
```
