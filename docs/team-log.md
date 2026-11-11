# Team log

A team is a set of Macs that share one log. The log lives on the Cloudflare relay as sealed entries. Each member Mac copies the log into its own store. The relay sees IDs, times and sizes, never content.

The log carries everything that a team shares: the people and bots of the team, the messages of the Team chat, and the shared pages. A phone talks only to its own Mac, as before.

Status: the relay has the room (`relay/src/room.js`). The Mac backend makes, joins, leaves and copies a room for the tools on the Mac, and it learns of a new entry at once through the socket of the room (see [Mac](#mac)). It shares bots and their pages, and the Library of the phone lists the team pages of the other Macs (see [Bots and pages](#bots-and-pages)). It keeps the Team chat of the people and serves it to the phone (see [Team chat](#team-chat)). A message of another member reaches this Mac through the socket in about one second. These parts are not built yet:

- A call of `GET .../members` that finds a member that left without a `delete` entry.
- A record of the open invites on the Mac.
- The member removal and the key change.
- The mentions of the Team chat in the app. The app shows each team in its Chats list, and the chat of the team sends and shows text only.
- A bot that reads or writes the Team chat, and a push notification for a team message.

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

This one rule gives message edits and page versions. A Mac sorts a chat by `first`, so a replaced message keeps its place.

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
| `rev` | A whole number from 1 to 2^53 - 1 (9007199254740991). It grows with each replacement of the entry. |
| `at` | The time of the author's Mac, ISO 8601 in UTC. |

| Kind | `kept` | Author | Fields |
| --- | --- | --- | --- |
| `member` | `true` | Each member, one entry | `name` (1 to 80 characters), `avatar` (at most 80), `color` (at most 20), `agreement` (the public agreement key) |
| `delete` | `false` | The member of the replaced entry | None. It replaces an entry that its author deletes. |
| `bot` | `true` | The Mac that runs the bot, one entry for each bot | `name` (1 to 80 characters), `avatar` (at most 80), `color` (at most 20) |
| `page` | `true` | The Mac that runs the bot of the page, one entry for each document | `bot` (the entry ID of the `bot` entry), `key`, `title`, `format`, `summary` (or null), `content`, `version` (the version of the document on its Mac), `expires_at` (or null) |
| `message` | `false` | Any member | `text` (1 to 16,000 characters), `reply_to` (an entry ID, or absent), `mentions` (at most 16 entry IDs, or absent), `bot` (the entry ID of a `bot` entry of the same member, or absent). See [Team chat](#team-chat). |
| `remove` | `true` | The admin | The removal of a member. See [Member removal](#member-removal-and-key-change). |
| `key` | `true` | The admin | The room key of the next epoch. |

The kinds `member`, `delete`, `bot`, `page` and `message` are complete. See [Bots and pages](#bots-and-pages) for the rules of `bot` and `page`, and [Team chat](#team-chat) for `message`. Each other kind gets its fields in the pull request that builds it. A new kind needs no change of the relay and no change of the body.

A key in a payload is unpadded base64url, as in a pairing setup.

A text field must be valid Unicode text. JSON can carry a lone surrogate, for example `"\ud800"`, and the store of a Mac cannot keep it as text.

The Mac counts Unicode code points for the limits of `name`, `avatar` and `color`, because the Python standard library cannot count the characters that a person sees. A `member` entry that does not fit, or that has a `name`, `avatar` or `color` that is not valid Unicode text, stays in the store and changes no member. When a member has more than one `member` entry, the one with the highest `seq` counts, so each Mac shows the same member.

A Mac writes the `name` of its own `member` entry by a stricter rule: 1 to 80 code points, no space at its start or end, no line break, no control character, and no format character except the zero width joiner (U+200D) and the tag characters (U+E0020 to U+E007F) of emoji sequences. So a name with an emoji sequence, such as a woman and a laptop with a joiner between them, is valid, and a mark that changes the direction of text is not. A Mac reads each `name` that is valid Unicode text, so the rule of the reader is wider than the rule of the writer.

### Rules for a Mac that reads an entry

1. If the version of the body is later than the Mac knows, keep the bytes and try again after the next update of Coconut. This check comes first, because a later version can have another form.
2. Open the body with the room key of its epoch. Then check the signature with `member`. If one of them fails, drop the entry and write a log line. Do the same when the payload is not a JSON object with a `kind` (1 to 64 characters of valid Unicode text) and a whole `rev` from 1 to 2^53 - 1.
   - If the Mac has no room key for the epoch, and the epoch is before the first epoch that the Mac has a key for, skip the entry. A member that joins later never gets the keys of earlier epochs.
   - If the epoch is later, keep the bytes until the key comes in a `key` entry.
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
- Each number of an answer (`seq`, `first`, `at` and `last`) is a whole number from 0 to 2^53 - 1, so JavaScript and SQLite keep it exact. A Mac takes an answer with another number as no answer.
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

A page can have 512 KiB of content (`MAX_CONTENT` in `backend/coconut/documents.py`), and one entry holds a whole page. JSON can make the content larger, so a save of a team page checks that its payload fits (see [Bots and pages](#bots-and-pages)). Each Mac keeps the full history of the chat in its own store, also after the relay deletes an old `message` entry. The retention time only limits how far back a new member or a Mac that was off for longer than 90 days can read.

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

`relay` is an HTTPS origin without a path, a query or account details, as in a pairing link, and its host is a name, not an address in numbers. So a changed link cannot send a Mac over plain HTTP or to an address that the link gives in numbers. The rule does not know where a host name points. `name` has 1 to 80 characters, no space at its start or end, and no control character, because the app shows it before the person joins. The helper counts the characters that a person sees, and refuses each control and format character (`validName` in `ios/Tunnel/TeamLog.swift`). The Mac backend checks a new team name before the helper: it counts code points, and it refuses each Unicode category that starts with "C" and each line break. So its rule is never wider than the rule of the helper, and a team name has no emoji with a joiner. A Mac refuses a link or a welcome with a `relay` or a `name` that does not follow these rules. A Mac also refuses a link after its `expires`.

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

The room takes its name from the welcome, because the admin sealed it. The name in the link is only for the screen before the join. The Mac keeps its keys and its token before step 3. When the relay does not answer step 3, the same link asks again with the same member and token. A second join of that room must name the same relay, because the first relay can have taken the join. Else the Mac refuses the join, and the member token goes to no other relay. The person then leaves the room on the Mac and joins again.

To leave:

1. A member replaces its `member` entry with a `delete` entry, so each other Mac takes it out of its list.
2. The Mac sends `DELETE .../members/<m>`.
3. The Mac deletes its copy of the log and its keys.

A member can always leave. When the Mac cannot seal the `delete` entry, for example without its helper, it leaves without that entry. The other Macs then show the member until the member removal is built. The admin leaves only a room without other members, and the relay then deletes the room.

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

## Mac

The Mac backend does the steps of this contract for the Team screen of the app and for the tools on the Mac. The modules are in `backend/coconut`: `team.py` (the room operations and the routes), `team_sync.py` (the copy of each log), `team_socket.py` (the socket of each room), `team_store.py` (the tables and the reading rules), `team_keys.py` (the secrets), `team_relay.py` (the relay routes) and `team_helper.py` (the helper).

A Mac that runs a Hermes agent with no Coconut can run Coconut for a team only. See [Team-only mode](#team-only-mode).

A Mac with no person at it, for example a Mac that only runs a Hermes agent, uses the `coconut` tool of Coconut.app (`Contents/Helpers/coconut`). `coconut team` prints the teams of the Mac. `coconut team join` reads the invite link from standard input and joins. `coconut team add-bot <bot>` and `coconut team remove-bot <bot>` add and remove a bot of the Mac by its ID or its name. `coconut team leave --confirm <name of the team>` leaves. The tool calls the routes below with the token of the bridge file, and it prints the error text of the service. It never prints the link. Only a person makes a team and an invite, on the Team screen.

### Helper

`scripts/team-helper.swift` compiles with `ios/Tunnel/TeamLog.swift`, which holds the format. It seals, opens, signs, and reads and makes links and welcomes. It takes no argument and touches no file: it reads one JSON object from standard input and writes one to standard output. So a key, a token or a secret is never in a command line, a file or a log. A failure is `{"error": "<code>"}`, and a code names no value of the input.

- Coconut.app carries the helper in `Contents/Helpers/team-helper`, as a binary for both architectures. `coconut service` and `coconut update` name it in `COCONUT_TEAM_HELPER`.
- A source install compiles it into `<data>/team/team-helper` with `python3 scripts/team.py build`. `deploy.py` runs that command in each update, and the receipt `helper.sha256` stops a second build of the same sources. With `COCONUT_TEAM_HELPER` set, the command compiles nothing.
- The service uses `COCONUT_TEAM_HELPER` when it names an executable file. Else it uses `<data>/team/team-helper` when that file is private: no symbolic link, and no permission for the group or others.
- The helper also has a watch mode that does not end. It holds the socket of each room. See [Socket on the Mac](#socket-on-the-mac).

### Keys and tables

`<data>/team/<room ID>.json` holds the secrets of one room: the member key, the agreement key, the member token, and the room key of each epoch. The folder has mode 0700 and each file mode 0600. The Mac replaces a file in one step.

Everything else is in `coconut.sqlite3`:

| Table | Holds |
| --- | --- |
| `team_rooms` | One row for each room: its relay, its name, the member ID of this Mac, the admin, the state, the epoch, the read position (`seq`), and the profile that this Mac writes in its `member` entry. |
| `team_entries` | The newest accepted write of each entry ID, with its payload. |
| `team_members` | The members that a Mac shows: the newest valid `member` entry of each member. |
| `team_pending` | The bodies that the Mac cannot open yet, for a later version or a later epoch. |
| `team_shares` | The bots that this Mac shares with each room, each with the entry ID of its `bot` entry. |
| `team_page_entries` | The entry ID of each document of this Mac that a room has or had as a `page` entry. One document keeps one entry ID. |
| `team_bots` | A view of `team_entries`: the valid `bot` entries of the other members, and the `bot` entries of this Mac with the profile that it wrote. |
| `team_pages` | A view of `team_entries`: the `page` entries of the other members, with the pin of this Mac and the time of the first write that this Mac saw, and the `page` entries of this Mac with the bot and the version that it wrote. |
| `team_messages` | A view of `team_entries`: one row for each valid `message` entry, with its `first`, member, bot, text, reply, mentions, the times of the relay, and whether it was edited. An index on the room and `first` gives a page of messages and the unread count without a scan of the room. |
| `team_names` | The last name that the store showed for each member and each bot of a room. A `delete` entry does not remove it. |
| `team_reads` | The read position of this Mac in the chat of each room. |

A room is `joining` (the relay did not answer the join yet), `active`, `removed` (the relay answered 401) or `gone` (the relay answered 404). The copy of a `removed` or `gone` room stays until the person leaves the room on the Mac. The Library does not show the bots and the pages of such a room.

### Sync

One thread, `coconut-team-sync`, reads each `active` room after its `seq`, one answer at a time. Each answer goes into the store in one transaction. Before that transaction, and without the lock of the service, the Mac reads each payload and checks the fields of each `member`, `bot` and `page` entry. So a page that is slow to check slows only the thread, not the requests of the phone. The transaction then applies the rules that need the store. The thread reads again while `last` is higher.

- The thread reads a room without an open socket every 30 seconds, and a room with an open socket every 5 minutes. A room operation, a save of a team page and `POST /v1/team/sync` start a pass of each room at once. A new `seq` on the socket of a room starts a pass of that room. See [Socket on the Mac](#socket-on-the-mac).
- After a failure, a room waits 60, 120, 240 and 480 seconds, then 900 seconds for each later try.
- After each start of the service, the thread tries the waiting bodies again, because an update of Coconut starts the service again.
- When the log shows no `member` entry of this Mac, the thread writes it, at most every 5 minutes. This repairs a stop between a join and its first write.
- After the read, the thread makes the log equal to the bots and the pages that this Mac shares. See [Bots and pages](#bots-and-pages).
- The profile of a room on this Mac follows each accepted `member` entry of this Mac, also one that the sync reads back after a write without an answer. So a later change of one field, or a new `member` entry after a loss, never writes an older name.
- A write of this Mac goes into the store at once. When a write gets no answer, the relay can have it. So before the next write or leave in that room, the Mac reads the room to its end. Its next `rev` is then higher, and a leave finds a `member` entry that only the relay has.
- A leave holds the lock of the room from its `delete` entry until the relay removed the member and the Mac deleted its copy. So the thread cannot write the `member` entry again in between. When the relay does not answer the leave, the room stays `active`, and the Mac is still a member.
- The member token of a room goes only to the relay of that room.
- When the store cannot take one entry for a fault of the Mac code, the Mac drops that entry with a log line, and the next entries of the answer go in. A database failure, for example a full disk, drops no entry: the answer rolls back, and the room tries again later.
- An unexpected failure in one room fails only that room, with the same waits. The other rooms go on.
- No call to the relay or to the helper holds the lock of the service.

A log line names a room by the start of its ID, and an entry by its ID and its `seq`. The relay sees these values too. No log line and no error text has an invite link, a token, a key, a ticket, a welcome or a proof. `backend/tests/test_team_routes.py` checks this.

### Socket on the Mac

The Python standard library has no WebSocket client, so the helper holds the sockets in its watch mode. `team_socket.py` starts one process of the helper while the Mac has an active room, and stops it when the Mac has none or the service closes.

The backend writes one line of JSON to the standard input of the helper for each change of the set of rooms: a join, a leave, and a room that becomes `removed` or `gone`. A new line replaces the set.

```json
{"operation": "watch", "rooms": [{"room": "<room ID>", "relay": "<relay>", "token": "<member token>"}]}
```

The helper writes one line of JSON to its standard output for each event. No line has a token or an address.

| Line | Meaning |
| --- | --- |
| `{"event": "ready", "version": 1}` | The first line, after the first line of rooms. |
| `{"event": "open", "room": r}` | The socket of the room is open. |
| `{"event": "seq", "room": r, "seq": n}` | The relay sent `{"seq": n}`. |
| `{"event": "closed", "room": r, "code": c, "status": s, "retry": t}` | The socket closed or did not open. `code` is the close code and `status` the HTTP status of a refused upgrade, each only when there is one. |
| `{"event": "error", "error": "malformed"}` | A line of rooms that the helper refused. It keeps the earlier set. |

- The helper sends the member token only in the `Authorization` header, to the relay of the room, and follows no redirect. It takes a relay only by the rule of a link: an HTTPS relay, and the loopback relay in a debug build. An HTTPS relay gets `wss`.
- The helper sends `ping` every 30 seconds on each open socket. A socket without an answer by the next ping is dead.
- After a close, the helper connects again after 1 second. The wait doubles after each close, up to 60 seconds. A socket that stayed open for 30 seconds resets the wait.
- After 401, 404, 1000 or 1008 the helper does not connect again until the next line, because the token is no longer a member's or the room is gone. Its line has `retry` false, and the backend starts a pass of the room. The read then finds the room `removed` or `gone`, and the next line leaves it out.
- The helper ends when its standard input closes. So it also ends when the service stops without a clean close.
- A `seq` that the store has starts no pass, for example the number that the relay sends when a socket opens. A higher one starts a pass of that room.
- When the process ends, the backend starts it again after 2 seconds. The wait doubles after each end, up to 5 minutes. A process that ran for 60 seconds resets the wait.
- No call holds the lock of the service. The thread of the watch takes it only to list the rooms, and it reads the key files without it.

The timer stays as the safety net. While the socket of a room is open, the timer reads the room every 5 minutes. A quiet room then costs the relay one small read in 5 minutes, and the read finds an entry that a dead socket missed before its next ping. Without an open socket, the timer reads the room every 30 seconds, as before: with no helper, an older helper, or a relay that refuses the socket.

An older helper has no watch mode. It reads to the end of its standard input before it answers, so it writes no `ready` line while the input is open. After 10 seconds without `ready`, the backend closes the input, and the older helper answers `{"error": "malformed"}`. The backend writes one log line, keeps the identity of the file (path, inode, size and time of change), and does not start that file again until it changes, for example after `scripts/team.py build` or an update of Coconut. No pass gets an error.

### Routes

Two callers use these routes, and one handler (`Team.team_request`) serves both:

- A paired device, for the Team screen of the app. It calls the routes on the tunnel listener or on the main listener, with the authorization of its other routes: its device token, and on the tunnel listener the static key of the phone.
- A tool on this Mac, for example `scripts/team.py` or `coconut team`. It calls the routes on the main listener with the token of `<data>/gateway/bridge.json`.

A request without a valid authorization gets 401.

| Request | Answer |
| --- | --- |
| `GET /v1/team/rooms` | The rooms with their members, their bots and their sync state, `helper`, and `mac_bots`: the `id` and `name` of each bot of this Mac that is not archived or deleted, so a tool can find a bot by its name. |
| `POST /v1/team/rooms`, body `{name, member_name, avatar, color, relay}` | Makes a room. This Mac is its admin. |
| `PATCH /v1/team/rooms/<room>`, body `{member_name, avatar, color}` | Changes what the team sees for this Mac. A field that the body leaves out keeps its value. The Mac first reads back an earlier write without an answer. Then it takes each field that the body leaves out from what the log has for this Mac, and writes its `member` entry again with the next `rev`. A failure changes nothing on this Mac. A room that is not `active` answers 409. |
| `POST /v1/team/rooms/<room>/invites`, body `{ttl, uses}` | The invite link. Only the admin makes one. The defaults are one day and one use. |
| `POST /v1/team/join`, body `{link, member_name, avatar, color}` | Joins a room. |
| `DELETE /v1/team/rooms/<room>` | Leaves the room, and deletes its copy and its keys. A failure of the relay stops the leave of an `active` room. A `joining` room always goes: see [Leave a join that did not finish](#leave-a-join-that-did-not-finish). |
| `POST /v1/team/sync` | Starts a pass, waits up to 60 seconds for it, and returns the rooms with `complete`. |
| `POST /v1/team/rooms/<room>/bots`, body `{bot_id}` | Shares a bot of this Mac with the room. An archived or deleted bot answers 409 or 404. |
| `DELETE /v1/team/rooms/<room>/bots/<bot_id>` | Stops sharing the bot with the room. |
| The routes of the Team chat | See [Team chat](#team-chat). |

A room in an answer has what the Team screen shows, so the app needs no other request:

| Field | Value |
| --- | --- |
| `room`, `name`, `state`, `created_at` | The room ID, the name of the team, `joining`, `active`, `removed` or `gone`, and the time that this Mac made or joined the room. |
| `is_admin`, `can_invite` | True when this Mac is the admin. `can_invite` is true only for the admin of an `active` room. |
| `members` | In the order of their first `member` entry: `member` (the member ID), `name`, `avatar`, `color`, `admin` and `this_mac`. |
| `bots` | Each bot of the team, while the room is `active` and the member of the bot is in `members`: `id`, `name`, `avatar`, `color`, `member` (the ID of the Mac that runs the bot), `member_name` and `this_mac`. A bot of this Mac also has `bot_id`, its ID on this Mac, and its profile comes from that bot. An archived or deleted bot of this Mac is not in the list. The bots follow the order of the members, then their names. |
| `sync` | `synced_at`, and `error`: one sentence for a person, or null. Also `seq`, `entries`, `waiting`, `refused`: the writes that the relay refused (see [Bots and pages](#bots-and-pages)), and `socket`: true while the socket of the room is open. The app does not show `socket`. |
| `unread`, `last_message` | The unread count of the Team chat, from 0 to 99, and the newest message that the chat shows: `first`, `member`, `member_name`, `this_mac`, `bot`, `bot_name`, `text` (its first 200 characters) and `created_at`, or null. `member`, `this_mac` and `bot` have the values of a message in a page, so the Chats list of the app can show "You" for a message of this Mac's person: `this_mac` is true and `bot` is null. An older service sends none of the three. See [Team chat](#team-chat). |
| `relay`, `member`, `admin`, `epoch` | For the tools. |

The `id` of a bot is `t-<room ID>-<entry ID>`, as in `team_bots` of `GET /v1/documents`.

Each error text of these routes is for a person, because the app shows it: what happened and what to do, without a word of the code. For example: "This Mac is no longer in the team." A Mac without a helper answers 503 with "Update Coconut on this Mac, then try again."

When something changes that the Team screen shows, the Mac adds one `team.updated` event with `{"team": <room ID>}` in the same transaction: this Mac makes, joins or leaves a room, a room becomes `active`, `removed` or `gone`, a member joins, leaves or changes its name, avatar or color, a bot enters or leaves the team, or `sync.error` or `sync.refused` appears, changes or clears. A failed try with the same error adds no event. `GET /v1/updates` then has `team_changed`, and the app reads `GET /v1/team/rooms` again. A pass that changes nothing adds no event. The event type is in the `known` set of `GET /v1/updates` and changes no Work row, so an app of today does not read its whole state again. A change of a bot of this Mac, for example a new name, comes with its own `bot.updated` event.

No answer has a key, a token or an invite secret, and so no answer has the public agreement key of a member. Only the answer of the invite route has the link. The request log of the tunnel listener keeps the route without its IDs and never a body, so the link of a join stays out of it. `scripts/team.py join` and `coconut team join` read the link from standard input, so the link is never an argument of a command. `scripts/team.py share <room> <bot>` and `unshare <room> <bot>` call the bot routes.

#### Repeat a write

The app sends a write again with the same `Idempotency-Key` when the answer does not come: after a lost connection, a timeout or a relay that does not answer (`APIClient.swift`). A team write calls the relay and can take seconds, so the first request can still run or be finished. Without a rule, a repeat makes a second team, or a join answers "already in this team" after a join that worked.

- A paired device can send an `Idempotency-Key` (a UUID) with each write: each route of the table except the `GET` and `POST /v1/team/sync`. A key that is not a UUID answers 400.
- The Mac keeps the first successful answer for a key of a device. A second request with the same key from the same device gets that answer and does nothing else.
- A second request that comes while the first one runs waits for it, then gets its answer. When the first request fails, the Mac keeps no answer, and the second request runs.
- The same key with another method, path or body answers 409. The same key from another device runs as its own request.
- The answers stay in the memory of the service only, for 10 minutes, and at most 256 of them. The sync thread drops an old answer on its next pass, at most 30 seconds later, also on a Mac without a room. A new answer drops the oldest answer over the limit at once. A stop of the service drops them all. A later repeat runs again.
- The answer of the invite route has the invite link, so the answers never go to the database or to a log. The `requests` table of the other writes of the phone keeps each answer with no end, so these routes do not use it.
- A request without a key runs each time, as before. A tool on this Mac sends no key, and the Mac ignores a key from a tool.

#### Leave a join that did not finish

A room stays `joining` when the relay does not answer the join. The relay can never answer, for example when the link named a server that does not exist. So the leave of a `joining` room always removes the room from this Mac:

1. The Mac asks the relay to remove its member, as for an `active` room, because the relay can have taken the join.
2. When the relay does not answer or refuses, the Mac writes a log line without a secret.
3. The Mac deletes its copy and its key file, and answers `{"room": <room ID>, "left": true}`.

A relay that took the join keeps this Mac as a member without a Mac: the member token stays valid there, and the admin sees no `member` entry of this Mac. This stays until the member removal of the contract exists. For an `active` room, a failure of the relay still stops the leave, because the Mac is still a member there.

### Bots and pages

`backend/coconut/team_pages.py` shares the bots of this Mac and their pages. `TeamStore` reads the bots and pages of the other members.

To share:

- The person shares a bot of this Mac with a room. The Mac makes the entry ID of its `bot` entry once.
- `coconut_save_document` and `POST /v1/gateway/document` take `audience`: `"private"` or `"team"`. Outside a Coconut chat turn, `coconut_save_document` saves through `POST /v1/gateway/document`, so a shared bot can keep a team page from another messaging app too (see [architecture](architecture.md#messages-outside-a-coconut-turn)). A new document without the field is private. A save of an existing document without the field keeps its audience. The document row and what a bot reads about its documents show the audience.
- `"team"` means each team that the bot is shared with. A save with `"team"` of a bot that is shared with no team gets 409 with the reason.
- The sealed body is the payload and 97 bytes, and the relay takes at most 1 MiB. So a save of a team page checks the size of its payload and answers 413 when the page does not fit. The payload is JSON without ASCII escapes, so text outside ASCII does not grow.

To write:

- A save only changes the document and calls `team_wake`. A save holds a transaction, and no call to the relay or the helper holds the lock of the service.
- In each pass, after the read of a room, the Mac derives what it wants to share: its shared bots that exist and are not archived, and their documents with audience `team` that are not past their end. It compares that with its own `bot` and `page` entries in `team_entries`. It keeps no second copy of the state.
- A bot or a page that the log does not have, or has in an older form, gets a kept entry. The form of a bot is its name, avatar and color. The form of a page is its bot entry and its version, because each save of a document gives a new version. The store keeps the form of each own entry in the columns of `team_bots` and `team_pages`, also for an own page that the reading rules refuse. So a pass parses no payload, and a pass after the writes writes nothing and opens no write transaction.
- An own `bot` or `page` entry that the Mac no longer wants gets a `delete` entry: the document is deleted, private again or past its end, or the bot is no longer shared, archived or deleted.
- A bot can have a longer name, avatar or color than the limits of the entry. The entry takes the start of each.
- The relay can refuse one write for the entry itself: 413 for a body over the limit, 507 when the kept entries are full, or another status of the 400s except 401, 404 and 429. Then the other writes go on. The Mac keeps the refusal in memory until the form of the entry changes, and `sync.refused` of the room shows it. A failure of the relay or of the helper stops the pass of the room, as a read does.
- A leave needs no extra step: the reading rules below hide the bots and pages of a member that left.

Rules for a Mac that reads `bot` and `page` entries:

1. A `bot` entry is valid with the name, avatar and color rules of a `member` entry. An entry that does not fit stays in the store and shows nothing.
2. A `page` entry is valid only when its `bot` is an entry ID, its `expires_at` is null or a UTC time as `YYYY-MM-DDTHH:MM:SSZ`, each text field is valid Unicode text, and the page passes the rules of a local save (`page_to_save`: key, title, format, content size, summary, and for HTML no finding of `page_problems`). The Mac does not trust the check of the writer. HTML that the parser of the Mac cannot read breaks the page rules. A page that does not fit stays in the store and shows nothing.
   - When a check of a `member`, `bot` or `page` entry fails with an error, the entry also stays in the store and shows nothing, and the log of the service has the reason. Another error in the part of the read without the database drops only that entry. So one entry never stops the sync of a room.
3. A page shows only while its `bot` names a valid `bot` entry of the same member, and while that member is in the member list. So a page of a bot of another member shows nothing, a `delete` over the `bot` entry hides its pages, and a member that leaves takes its bots and pages from each Library.
4. A page past its `expires_at` shows nothing.
5. The times come from the relay, not from the payload: `updated_at` is the `at` of the entry, and `created_at` is the `at` of the first write that this Mac saw. A time after the year 9999 makes the page not valid. `version` for the phone is the `rev` of the entry.
6. The Mac that wrote a page has the document already. Its own entries show nothing more in its Library.
7. The Library shows the bots and the pages of a room only while the room is `active` on this Mac. A person that is no longer in a team does not keep its pages, and the pages of a room that is gone can never get an update.

The Library of the phone:

- `GET /v1/documents` lists the team pages with the local documents. A team page has each field that the app needs for a document, `pinned`, and `team` with `room` and `name`. Its `id` is `t-<room ID>-<entry ID>`, and its `bot_id` is the ID of its team bot in the same form. A local ID is a UUID, so the two never meet.
- The answer also has `team_bots`: for each team bot `id`, `name`, `avatar`, `color`, `team` (`room` and `name`) and `member` (`id` and `name` of the Mac that runs it). The app of today reads only `documents`, and it hides a document of a bot that it does not know.
- A local document with audience `team` has `"audience": "team"`.
- `GET /v1/documents/<id>` of a team page adds `content`. `PATCH` sets the pin of this Mac. `DELETE` answers 403, because only the Mac of the page can delete it. A page past its end answers 404 as an ended local document does.
- When a read changes the team pages or bots that the Library shows, or a room with team bots stops being `active` or goes from this Mac, the Mac adds one `document.updated` event with `{"team": <room ID>}`. `GET /v1/updates` then has `documents_changed`, also for a local document, and the app can read the list again. The app of today knows the event type, so the event does not make it read the whole state again.
- The list reads the views `team_bots` and `team_pages` and parses no payload. The content stays in the payload of the entry, and the Mac reads it when a page opens.

### Team chat

`backend/coconut/team_messages.py` serves the Team chat of each room. One message is one `message` entry, not kept. The relay holds it for its retention time, and each Mac keeps the history in `team_messages`.

The payload of a `message` entry:

| Field | Value |
| --- | --- |
| `text` | The message: 1 to 16,000 code points of valid Unicode text. It is Markdown, as in a bot chat. The Mac does not interpret it. |
| `reply_to` | The entry ID of the message that this one answers, or absent. |
| `mentions` | A list of at most 16 entry IDs of `member` or `bot` entries that the message names, or absent. |
| `bot` | The entry ID of a `bot` entry of the same member when a bot of that Mac wrote the message, or absent for a message of the person. |

- An edit is a new write of the same entry ID with the next `rev`. A removal is a `delete` entry over it. Only the member of a message can do either (rule 3).
- A Mac of today writes no `bot`. A later step writes it, with one entry for each complete message of a bot. So `edited_at` means an edit for the message of a bot too.

Rules for a Mac that reads a `message` entry:

1. A message is valid only when `text` has 1 to 16,000 code points of valid Unicode text, `reply_to` and `bot` are entry IDs, and `mentions` is a list of at most 16 entry IDs. A reader takes null as an absent field. A message that does not fit stays in the store and shows nothing.
2. A field that the reader does not know stays in the payload, and the reader ignores it. So a later field, for example an attachment, does not break a Mac of today.
3. A message with a `bot` shows only when `bot` names a valid `bot` entry of the same member. The bot entry can come after the message, so the check runs when the Mac reads the chat, not when it accepts the entry. A `delete` over the bot entry does not hide the messages of the bot that the chat already had.
4. The newest write of an entry counts, as the relay keeps only that write. A `delete` over a message removes its row. A later `message` write over the `delete` shows the message again, also on a Mac that joins later and reads only that write. A Mac of today never writes it.
5. The chat sorts the messages by `first`, so an edit keeps its place.
6. The times come from the relay, not from the payload: `created_at` is the `at` of the first write that this Mac saw, and `edited_at` is the `at` of the newest write. A message is edited when its `rev` is over 1, so each Mac gives the same answer. A time after the year 9999 makes the message not valid. This Mac stores its own write at once with its own clock, and takes the time of the relay when it reads the write back. Before an edit, the Mac reads the room to its end, because the edit replaces the earlier write on the relay. So `created_at` gets the time of the relay also when the person edits a message before the next pass. An edit costs one more read of the relay.
7. A message of a member that left stays in the history. The relay deletes the kept entries of that member, and a `delete` entry replaces its `member` entry. So `team_names` keeps the last name that the store showed for each member and each bot. A Mac that joins after the member left never saw a name, and `member_name` is null.

What a Mac keeps after the relay drops an old entry: the row of each message in `team_messages`, its payload in `team_entries`, and the names in `team_names`. A Mac that joins later reads only what the relay still has: the messages of the last 90 days.

The read position:

- `team_reads` keeps, for each room, the `first` of the newest message that the person read. The unread count is the number of shown messages of the other members after that position, up to 99. Only a message of this Mac's person counts as read. A message with a `bot` of this Mac counts as unread, because the person did not write it.
- The position starts at the `first` of the own `member` entry of this Mac. So the history from before a join does not count as unread.
- The position only moves forward, and never past the newest message of the room.

The routes have the authorization of the other team routes, and a write with an `Idempotency-Key` is safe to repeat (see [Repeat a write](#repeat-a-write)). A write goes to the relay inside the request. A failure is an error for the person and changes nothing on this Mac. A write in a room that is not `active` answers 409.

| Request | Answer |
| --- | --- |
| `GET /v1/team/rooms/<room>/messages?before=<first>&limit=<n>` | One page, newest first: `{room, messages, before}`. `limit` is 1 to 100, and 50 without it. `before` of the answer is the `first` to ask for the next page, or null when no older message exists. Also a room that is not `active` has its history. |
| `POST /v1/team/rooms/<room>/messages`, body `{text, reply_to?, mentions?}` | Writes a message of the person and answers `{room, message}`. `reply_to` is the `id` of a message that the chat shows. `mentions` is a list of member IDs of `members` and `id` values of `bots` of `GET /v1/team/rooms`. The Mac writes the entry IDs of the contract. With an `Idempotency-Key`, the entry ID of the message comes from the room, the device and the key. So a repeat after a write without an answer finds that write and sends no second message. |
| `PATCH /v1/team/rooms/<room>/messages/<id>`, body `{text}` | Changes the text of a message of this Mac's person, and answers `{room, message}`. The other fields stay. The same text writes nothing. A message of another member or of a bot answers 403. |
| `DELETE /v1/team/rooms/<room>/messages/<id>` | Removes a message of this Mac's person with a `delete` entry, and answers `{room, id, deleted: true}`. When the entry is already a `delete` entry of this Mac, for example after a delete without an answer, the Mac gives the same answer and writes nothing. An ID that the store does not have answers 404, and an entry of another member 403. |
| `POST /v1/team/rooms/<room>/read`, body `{first}` | Moves the read position, and answers `{room, read, unread}`. |

A message in an answer:

| Field | Value |
| --- | --- |
| `id`, `first` | The entry ID and the `first` of the entry. |
| `member`, `member_name`, `this_mac` | The member ID of the writer, its name (see rule 7), and true for a message of this Mac. |
| `bot`, `bot_name` | The `t-<room ID>-<entry ID>` ID of the team bot that wrote the message, and the last name that `team_names` has for it, also for a bot that the team no longer has. Both are null for a message of a person. |
| `text`, `reply_to` | The text, and the `id` of the message that this one answers, or null. |
| `reply` | The message that `reply_to` names, so the app can quote it without a second request: `id`, `member`, `member_name`, `bot`, `bot_name`, and the first 200 code points of its `text`. Null when `reply_to` is null, or when the chat does not show that message: removed, not valid, or never seen by this Mac. One query reads the answered messages of a whole page. |
| `mentions` | For each mention: the member ID of a member, or the `t-` ID of a team bot. A mention that the store cannot name stays out. |
| `created_at`, `edited_at` | The times of rule 6. `edited_at` is null for a message without an edit. |

When the chat of a room changes on this Mac, the Mac adds one `team_messages.updated` event with `{"team": <room ID>}` in the same transaction: a new message, an edit, a removal, a new time from the relay, a new or changed name in `team_names` of a room with messages (for example a rename, or a `bot` entry that makes a message of its bot show), or a new read position, for example from another device of the person. The same name written again adds no event. `GET /v1/updates` then has `team_messages_changed`: the IDs of these rooms. A pass that changes nothing adds no event. The event type is in the `known` set of `GET /v1/updates` and changes no Work row, so an app of today does not read its whole state again.

No log line has the text of a message. A log line names a message by its entry ID and its `seq`, as for each other entry.

### Team-only mode

A Mac can run a production Hermes agent with no person at it and no Coconut. Such a Mac runs Coconut "team only" (`backend/coconut/team_only.py`), so its agent can publish pages to the team. The full service does much more than a team needs, and it changes the Hermes profiles of the Mac. The team-only service does none of that.

The service starts in this mode with `python3 -m coconut --team-only`, or with `COCONUT_MODE=team` in its environment, so a login item can set it. Coconut.app sets up a Mac in this mode with `--set-up --team-only`, and its login item sets the variable. See [A Mac without a person](install-and-update.md#a-mac-without-a-person). Another value of `COCONUT_MODE` stops the start. The service uses the same data folder as a full Mac: `coconut.sqlite3`, `<data>/team/`, `<data>/gateway/bridge.json`, `process.json` (with `"mode": "team"`) and the same logs. It takes the service lock of the folder, so a full service and a team-only service never share one folder. At its first start, the service writes a mark in its store (the table `service_mode`), and from then on the folder is a team-only folder, also without a bot or a room. The two modes never take the folder of the other (`service_base.check_data_folder`): a folder with the data of a full install (a paired phone, a task, a message, a Hermes profile or a bot) stops a team-only start, and a folder with the mark stops the start of the full service and of its refuse mode without Hermes. Each refusal is one line that names the cause and says to use another data folder, and it writes nothing.

What the service does:

- It keeps the team rooms of this Mac in step with the relay: the store, the sync thread, the socket watcher and the helper, as on a full Mac.
- It serves the team routes on the main listener with the token of the bridge file, so `coconut team` and `scripts/team.py` work as on a full Mac. Only the routes of this section, the team routes and `POST /v1/gateway/document` need the token. Each other route, and a pair request, answers 503 with `{"error": ..., "code": "team_only"}`. `GET /health` works.
- It keeps the bots that this Mac declares, and it saves their pages.

What the service never does: it imports no Hermes module and no module of the full service (`service.py` and what it loads), and it runs no contract check of Hermes. It reads and changes no file under a Hermes home. It starts and stops no gateway, loads and installs no plugin and no skill, and copies no key. It runs no scheduler, no kanban adapter and no worker. It opens no tunnel listener, also with `--tunnel-port`, pairs no phone and sends no push notification. It keeps no event, because no phone reads one. Its only child process is the team helper.

A bot of a team-only Mac is a record, because the Mac scans no Hermes profile. A record has an ID, a name, an avatar and a color with the rules of a `member` entry, and the Hermes home that the bot stands for. The home is a full path, and the service keeps its real path (`realpath`). Without a home, the service takes its own: `HERMES_HOME`, else `~/.hermes`. The service only stores and compares the path. It never opens it. One home has one bot, and one name (case does not matter) has one bot, because a tool finds a bot by its name. The routes, each with the token of the bridge file:

| Request | Answer |
| --- | --- |
| `GET /v1/team/status` | `mode` (`"team"`), `version` (the commit of the checkout that runs, or null), `helper`, `bots` (the count), and each room in short form: `room`, `name`, `state`, `members` and `bots` (counts), `synced_at`, `error` and `socket`. |
| `GET /v1/team/bots` | `bots`: for each bot `id`, `name`, `avatar`, `color`, `home` and `teams` (the room IDs that it is shared with). |
| `POST /v1/team/bots`, body `{name, avatar, color, home}` | Declares a bot. A second bot for one home, or for one name, answers 409. |
| `PATCH /v1/team/bots/<bot>`, body `{name, avatar, color}` | Changes what the team sees for the bot. The home stays: a body with `home` answers 400. |
| `DELETE /v1/team/bots/<bot>` | Stops sharing the bot with each room, as `DELETE /v1/team/rooms/<room>/bots/<bot_id>` does, deletes its pages, and deletes the bot, in one transaction. The next pass writes the `delete` entries. |

`GET /v1/team/rooms` also has `"mode": "team"`, and `mac_bots` lists the declared bots. `coconut team new-bot <name> [--home <path>]` declares a bot. `coconut team add-bot <bot>` on a team-only Mac without that bot does not guess: it prints one line that says how to declare it. On a full Mac, `new-bot` says that the Mac takes its bots from its Hermes profiles.

Pages: `POST /v1/gateway/document` works as on a full Mac. The home of the request names the declared bot, and a save that changes nothing answers `unchanged`. No person reads the Library on a team-only Mac, so each save must name `"audience": "team"`. A save without it answers 400 and stores nothing. A page of a shared bot goes to each team of the bot. A page of a bot that no team has yet stays on this Mac, and the sync writes it when the bot is shared. The limits and checks of a page are those of a full Mac.

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
