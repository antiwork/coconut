# Team log

A team is a set of Macs that share one log. The log lives on the Cloudflare relay as sealed entries. Each member Mac copies the log into its own store. The relay sees IDs, times and sizes, never content.

The log carries everything that a team shares: the people of the team, the authors of the shared pages, the messages of the Team chat, the direct messages between two people and between a person and an agent, and the shared pages. A phone talks only to its own Mac, as before.

A team has two kinds of Mac:

- **A person's Mac** (a full install): its agents are the person's, and nobody else can ask them. The person shares a page of an agent with a team by one act on the page, and stops when they want. The agent of the page is then the author of the page in the team, and nothing more.
- **A Mac for a team only** (see [Team-only mode](#team-only-mode)): its agents are the team's. Each page that such an agent saves with the audience "team" goes to the teams of the agent.

Status: the relay has the room (`relay/src/room.js`). The Mac backend makes, joins, leaves and copies a room for the tools on the Mac, and it learns of a new entry at once through the socket of the room (see [Mac](#mac)). It shares the pages that a person shares, and the Library of the phone lists the team pages of the other Macs (see [Agents and pages](#agents-and-pages)). It keeps the Team chat and serves it to the phone, and the app lists the team agents and the people that a person can name with `@` (see [Team chat](#team-chat) and [Mentions of a person](#mentions-of-a-person)). An agent of a Mac for a team only answers in the Team chat (see [An agent answers](#an-agent-answers)). A message of another member reaches this Mac through the socket in about one second, and a person's Mac notifies its phones (see [Notifications](#notifications)). The Mac keeps the direct messages between two people and serves them to the phone, and the app shows them (see [Direct messages](#direct-messages)). A person writes to an agent of a Mac for a team only in a direct chat, and the agent answers there (see [Direct chat with an agent](#direct-chat-with-an-agent)). The admin removes a member (see [Removal](#removal)), and the room then gets a new room key (see [Key change](#key-change)). The admin deletes a message of another member (see [The admin deletes a message](#the-admin-deletes-a-message)). Each member makes more chats with a name in a team (see [Chats of a team](#chats-of-a-team)). A person reacts to a message of a team chat or of a direct chat with an emoji (see [Reactions](#reactions)). A message of a person carries the link preview that the app of the sender made (see [Link previews](#link-previews)). A message of an agent can be an alert with a type and a level, and the app filters a chat by them (see [Alerts](#alerts)). A named chat that agents deliver alerts to is a list of notices, each a thread that the person opens and reads on its own (see [Inbox chats](#inbox-chats)). This part is not built yet:

- A call of `GET .../members` that finds a member that left without a `delete` entry.

## Terms

- **Room:** the object on the relay that holds one log, its members and its tickets. A team has one room.
- **Member:** one Mac in a room. One person and the agents of that person are behind it.
- **Admin:** the member that made the room. Only the admin removes members. Each member invites.
- **Entry:** one sealed item of the log.
- **Epoch:** the number of the room key. It starts at 0 and grows by 1 with each key change.

## Keys

| Key | Made by | Use |
| --- | --- | --- |
| Room key | The admin's Mac | 32 random bytes. Seals each entry of its epoch. |
| Member key | Each Mac, for each room | Ed25519. Signs each entry of the Mac, and the proof of a join. Its public key in lowercase hex is the member ID. |
| Agreement key | Each Mac, for each room | X25519. Makes the pair key of a key box after a key change, and the pair key of a direct message, with each other member. |
| Member token | Each Mac, for each room | 32 random bytes in lowercase hex. Authorizes the Mac at the relay. |
| Invite secret | The Mac of the member that invites, for each invite | 32 random bytes. Gives the relay ticket and the welcome key. |

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
| `member` | `true` | Each member, one entry | `name` (1 to 80 characters), `avatar` (at most 80), `color` (at most 20), `agreement` (the public agreement key), `bot_only` (`true` for a Mac without a person, or absent), `direct` (`true` for a person's Mac that reads direct messages, or absent) |
| `delete` | `false` | The member of the replaced entry | None. It replaces an entry that its author deletes. |
| `bot` | `true` | The Mac that runs the agent, one entry for each agent | `name` (1 to 80 characters), `avatar` (at most 80), `color` (at most 20), `open` (`true` for an agent that answers each member, or absent), `direct` (`true` for an agent that answers direct messages, or absent). See [Direct chat with an agent](#direct-chat-with-an-agent). |
| `page` | `true` | The Mac that runs the agent of the page, one entry for each document | `bot` (the entry ID of the `bot` entry), `key`, `title`, `format`, `summary` (or null), `content`, `version` (the version of the document on its Mac), `expires_at` (or null) |
| `message` | `false` | Any member | `text` (1 to 16,000 characters), `reply_to` (an entry ID, or absent), `mentions` (at most 16 entry IDs, or absent), `bot` (the entry ID of a `bot` entry of the same member, or absent), `chat` (the entry ID of a `chat` entry, or absent for the Team chat), `preview` (a link preview, or absent), `files` (1 to 4 files, or absent; then `text` can be empty), `alert` (the type and the level of an alert of an agent, or absent), `thread` (the entry ID of a message whose thread a later part of an answer joins, or absent). See [Team chat](#team-chat), [Link previews](#link-previews), [Files](#files), [Alerts](#alerts) and [Inbox chats](#inbox-chats). |
| `direct` | `false` | A person's Mac, and a Mac for a team only for its agents | `box`: the text of a direct message to one other member, sealed a second time with the pair key of the two members. The box of a message of a chat with an agent also has `bot` (the entry ID of the `bot` entry of the agent) and can have `page`. See [Direct messages](#direct-messages) and [Direct chat with an agent](#direct-chat-with-an-agent). |
| `remove` | `true` | The admin, one entry for each removed member | `member` (the member ID of the removed member), `name` (the last name that the admin's Mac showed for it, or absent). See [Removal](#removal). |
| `typing` | `false` | Any member, one entry for its person and one for each of its agents in each room | `on` (`true` or `false`), `bot` (the entry ID of a `bot` entry of the same member, or absent for the person of the Mac), `chat` (the entry ID of a `chat` entry, or absent for the Team chat). See [Typing](#typing). |
| `chat` | `true` | The admin, one entry for each chat that it makes | `name` (1 to 50 characters). See [Chats of a team](#chats-of-a-team). |
| `member_chat` | `true` | Each member but the admin, one entry for each chat that it makes | `name` (1 to 50 characters), as `chat`. See [Chats of a team](#chats-of-a-team). |
| `key` | `true` | The admin, one entry for each new epoch | `epoch` (the new epoch), `agreement` (the public agreement key of the admin), `boxes` (for each member ID, the room key of the new epoch in a box for that member). See [Key change](#key-change). |
| `erase` | `false` | The admin, one entry for each deleted message | `entry` (the entry ID of a `message` entry of another member). See [The admin deletes a message](#the-admin-deletes-a-message). |
| `reaction` | `false` | A person's Mac, one entry for each message that its person reacts to | `message` (the entry ID of a `message` entry), `emoji` (one of six emoji, or empty). See [Reactions](#reactions). |
| `file` | `false` | A person's Mac, one entry for each piece of a file of a message | `file` (the file ID), `index` (0 to 5), `data` (standard base64 of 1 to 737,280 bytes). See [Files](#files). |
| `usage` | `true` | A person's Mac, one entry in each room | `days` (at most 90 days, each with its tokens, turns, tool calls, time working and spend), `cost_known` (`true`, `false` or absent). See [Usage](#usage). |

The kinds `member`, `delete`, `bot`, `page`, `message`, `direct`, `remove`, `key` and `erase` are complete. See [Agents and pages](#agents-and-pages) for the rules of `bot` and `page`, [Team chat](#team-chat) for `message`, and [Direct messages](#direct-messages) for `direct`. Each other kind gets its fields in the pull request that builds it. A new kind needs no change of the relay and no change of the body.

The kinds `typing`, `chat`, `member_chat`, `reaction`, `file`, `notice`, `seen` and `usage` are complete too. See [Typing](#typing), [Chats of a team](#chats-of-a-team), [Notices](#notices), [Usage](#usage), [Reactions](#reactions) and [Files](#files).

A key in a payload is unpadded base64url, as in a pairing setup.

A text field must be valid Unicode text. JSON can carry a lone surrogate, for example `"\ud800"`, and the store of a Mac cannot keep it as text.

The Mac counts Unicode code points for the limits of `name`, `avatar` and `color`, because the Python standard library cannot count the characters that a person sees. A `member` entry that does not fit, or that has a `name`, `avatar` or `color` that is not valid Unicode text, stays in the store and changes no member. When a member has more than one `member` entry, the one with the highest `seq` counts, so each Mac shows the same member.

A Mac writes the `name` of its own `member` entry by a stricter rule: 1 to 80 code points, no space at its start or end, no line break, no control character, and no format character except the zero width joiner (U+200D) and the tag characters (U+E0020 to U+E007F) of emoji sequences. So a name with an emoji sequence, such as a woman and a laptop with a joiner between them, is valid, and a mark that changes the direction of text is not. A Mac reads each `name` that is valid Unicode text, so the rule of the reader is wider than the rule of the writer.

### Rules for a Mac that reads an entry

1. If the version of the body is later than the Mac knows, keep the bytes and try again after the next update of Coconut. This check comes first, because a later version can have another form.
2. Open the body with the room key of its epoch. Then check the signature with `member`. If one of them fails, drop the entry and write a log line. Do the same when the payload is not a JSON object with a `kind` (1 to 64 characters of valid Unicode text) and a whole `rev` from 1 to 2^53 - 1.
   - If the Mac has no room key for the epoch, and the epoch is before the first epoch that the Mac has a key for, skip the entry. A member that joins later never gets the keys of earlier epochs.
   - If the epoch is later, keep the bytes until the key comes in a `key` entry. Then open them again.
3. If the Mac already has this entry ID, accept the entry only from the same member and only with a higher `rev`.
4. Accept `remove`, `key`, `erase` and `chat` only from the admin. The admin is the member ID that the welcome names, and the Mac checked it with the relay at the join (see [Invite and join](#invite-and-join)).
5. If the kind is unknown, keep the payload and show nothing.

## Relay routes

The routes of a room start with `/v1/rooms/<room ID>`. The room ID is 32 random bytes in lowercase hex. A member sends `Authorization: Bearer <member token>`. A missing room answers 404, and a token that is not a member's answers 401. A member that is not the admin gets 403 on a route of the admin.

| Request | Who | Answer |
| --- | --- | --- |
| `PUT /v1/rooms/<id>`, body `{"member": m, "hash": h, "proof": p}` | The first Mac | 201 makes the room, with member `m` as the admin. 200 for a repeat with the same `m` and `h`. 409 for a room of another admin. 401 for a proof that fails. |
| `POST /v1/rooms/<id>/tickets`, body `{"hash": h, "ttl": s, "uses": n, "welcome": w}` | Member | 204. 409 when 8 tickets are open. |
| `DELETE /v1/rooms/<id>/tickets/<h>` | Admin | 204 removes the ticket and its welcome. |
| `POST /v1/rooms/<id>/members`, `Authorization: Ticket <ticket>`, body `{"member": m, "hash": h, "proof": p}` | A new Mac | 200 with `{"welcome": w}`, and the ticket loses one use. 200 without a new use for a repeat with the same `m` and `h`. 401 for a proof that fails, and for a ticket that is unknown, expired or without a use left. 409 when `m` exists with another token, or the room is full. |
| `GET /v1/rooms/<id>/members` | Member | 200 with `{"members": [{"member": m, "admin": true, "joined": t}]}`. |
| `DELETE /v1/rooms/<id>/members/<m>` | Admin, or member `m` | 204. The relay removes the token, closes the socket and deletes the kept entries of `m`. When the admin removes another member, the relay also deletes every ticket of the room, also when `m` left already. 409 when `m` is the admin and the room has other members. When the admin leaves a room without other members, the relay deletes the room. |
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
| Other entries of one member | 512 MiB |
| All entries of one room | 8 GiB |
| Retention of an entry that is not kept | 90 days after its last write |
| Writes of one member | 300 each minute |
| New rooms | 10 each hour for one IP address, as for tunnel routes |
| Other room requests | 3,000 each minute for one IP address |
| Room without a request of a member | Deleted after 180 days |

A member that leaves and joins again continues its counts: its entries that are not kept stay on the relay, and so does its count of writes in the minute.

The limit of the other entries of one member has room for the files of a team chat. The two limits of one member add up to 576 MiB. So in a room with more than 14 members, the limit of the room can stop a write before the limit of the member does.

A file of a message has at most 4 MiB, in pieces of at most 720 KiB, so each piece fits in one body. A person's Mac sends at most 320 MiB of files in one room in 90 days (see [Files](#files)).

A page can have 512 KiB of content (`MAX_CONTENT` in `backend/coconut/documents.py`), and one entry holds a whole page. JSON can make the content larger, so a save of a team page checks that its payload fits (see [Agents and pages](#agents-and-pages)). Each Mac keeps the full history of the chat in its own store, also after the relay deletes an old `message` entry. The retention time only limits how far back a new member or a Mac that was off for longer than 90 days can read.

## Invite and join

The link does not hold the room key. It holds an invite secret, and the relay holds the room key in a sealed welcome.

```
coconut://team/<token>
```

The link also has a web form, which is the same token on the site:

```
https://coconut.gumroad.com/i/<token>
```

A person shares the web form, because a mail or a chat keeps an https link, and the system opens it in the app through
the associated domain `applinks:coconut.gumroad.com` (`ios/Coconut/Coconut.entitlements`). The Worker in `links/` serves
that page and the file of the associated domain: it decodes the token to show the name of the team, keeps no copy and
writes no log. `TeamLog.invite(link:)` reads either form, so the app, `coconut team join` and the service take a link as
a person received it, also with a line break after it and with the slash that a browser adds at the end of a path.

The token is unpadded lowercase base32 (RFC 4648) of these bytes. The pairing link uses the same encoding, because Hermes masks text that looks like a secret.

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

1. The Mac of a member makes an invite secret and seals the welcome. The welcome names the admin of the room, not the Mac that invites.
2. The Mac sends the hash of the ticket and the welcome with `POST .../tickets`.
3. The person shares the link.

To join:

1. The person opens the link on the phone, and the phone gives it to its Mac.
2. The Mac makes its member key, its agreement key and its member token.
3. The Mac sends `POST .../members` with the ticket and its proof, and gets the welcome.
4. The Mac opens the welcome. If the welcome does not open, the Mac leaves the room with `DELETE .../members/<m>`.
5. The Mac reads `GET .../members` and checks that the relay names the `admin` of the welcome as the admin of the room. Else the Mac leaves the room the same way. Each member seals welcomes, so without this check a member could name itself in a welcome and get the rights of the admin on the new Mac. When the relay does not answer, the room stays `joining`, and the same link asks again.
6. The Mac writes its `member` entry, reads the log from `after=0`, and opens the socket.

The room takes its name from the welcome, because a member sealed it. The name in the link is only for the screen before the join. The Mac keeps its keys and its token before step 3. When the relay does not answer step 3, the same link asks again with the same member and token. A second join of that room must name the same relay, because the first relay can have taken the join. Else the Mac refuses the join, and the member token goes to no other relay. The person then leaves the room on the Mac and joins again.

To leave:

1. A member replaces its `member` entry with a `delete` entry, so each other Mac takes it out of its list.
2. The Mac sends `DELETE .../members/<m>`.
3. The Mac deletes its copy of the log and its keys.

A member can always leave. When the Mac cannot seal the `delete` entry, for example without its helper, it leaves without that entry. The other Macs then show the member until the admin removes it. The admin leaves only a room without other members, and the relay then deletes the room.

The link passes through the chat service that carries it, for example Telegram. Someone who reads the link while its ticket has a use left can join the room. Each Mac then shows the new member, and the admin can remove it.

After its last use, a ticket admits no new member. The relay keeps the ticket and its welcome until the `ttl` ends. A Mac that lost the answer of its join can then ask again with the same `member` and `hash`. After the `ttl`, the relay deletes both, and the link is useless. The admin can remove a ticket earlier, and a removal by the admin deletes every ticket of the room.

Each Mac keeps a record of each invite that it makes: the hash of the ticket and its end (`team_invites`). Only the admin's Mac uses its records. It writes the record before it sends the ticket, because the relay can take a ticket without an answer. A member removal removes the ticket of each record on the relay, whatever its saved end is, because the relay starts its count later than the Mac and its clock can differ. The relay answers 204 also for a ticket that it no longer has. The Mac forgets a record after the relay answered for it in a removal, or when its saved end is more than 7 days in the past: the longest life of a ticket. An invite that a version without the record made ends by itself after its time: one day for each invite of the app.

## Member removal and key change

The admin removes a member: a person who left the company, a Mac that was lost, or a member that left without its `delete` entry and still shows in each list. A removal has two parts: the removal, then the key change.

### Removal

On the admin's Mac, in this order:

1. The Mac removes the ticket of each record of its invites (see [Invite and join](#invite-and-join)) with `DELETE .../tickets/<h>`, so that a link that is still open cannot bring the member back. A failure stops the removal here, and the record stays.
2. The Mac sends `DELETE .../members/<m>`. The relay removes the token, closes the socket and deletes the kept entries of the member. It also deletes every ticket of the room, because each member makes tickets and step 1 names only the tickets of the admin. A member that the relay no longer has is no error: the relay answers 204, and it still deletes the tickets, because a member that left can hold a link that it made.
3. The Mac writes one kept `remove` entry: `{"kind": "remove", "member": "<member ID>", "name": "<last name of the member>"}`.

Each step is safe to repeat. The entry ID of the `remove` entry comes from the room and the member ID: the first 16 bytes of HMAC-SHA256 with the member token of the admin as the key, over `"coconut room remove" | room ID (32 bytes) | member ID (32 bytes)`. So a repeat writes the same entry. The key is a secret of the admin, so another member cannot write an entry with that ID first and block the removal. The admin's Mac keeps the member in its list until it has the `remove` entry, so a removal that stops after step 1 or 2 finishes when the admin asks again. A repeat after step 3 writes nothing.

The removed Mac gets 401 at its next request, and its room becomes `removed`, as for each other refused token. A removed person can join again later with a new invite. That Mac then has a new member ID.

Rules for a Mac that reads a `remove` entry:

1. Rule 4 of a reader accepts the entry only from the admin.
2. An entry whose `member` is not a member ID changes nothing. `name` is for the history only, so a `name` that does not fit hides no removal and changes no name.
   - When `team_names` has no name for the member, the Mac keeps the `name` of the entry there, by the rules of a name of a `member` entry. A Mac that reads the room after the removal gets no `member` entry of the member, because the relay deleted it, but it gets its messages, and they then have a name. A name that the store already has stays: the last name that this Mac showed wins.
3. The member leaves the member list, as after a `delete` over its `member` entry, also when the store still has that `member` entry. The relay deleted the kept entries of the member, and no `delete` entry comes for them, so the `remove` entry is the only sign. The Mac keeps the member in `team_removed` while the `remove` entry is in the store.
4. A later `member` entry of the same member ID does not bring the member back. It cannot come from the relay after step 2, but the reader does not trust that.
5. The agents and the pages of the member leave the Team screen and the Library, because both show an agent and a page only while its member is in the list. The chats that the member made go, as after a `delete` over each one (see [Chats of a team](#chats-of-a-team)). On a Mac for a team only, the waiting triggers of the messages of the member go, and a running turn of such a message stops (see [An agent answers](#an-agent-answers)).
6. The Team chat and each direct chat keep their history and the names of `team_names`. A direct chat with the member becomes read-only, because the member is no longer in the list.
7. The Mac adds the events of a member that leaves: `team.updated`, and `document.updated` when the member had an agent.
8. A `remove` entry that names the admin or this Mac changes nothing. The relay refuses a removal of the admin, and it tells a removed Mac by the refusal of its token.

### Key change

The removed Mac still has the room key. If that person also gets the storage of the relay, the key opens each later entry. So after a removal, the room gets a new room key that the removed member never gets. `backend/coconut/team_epochs.py` holds these steps.

A key change is due on the admin's Mac when its store has a `remove` entry that is newer than the newest `key` entry, or a `remove` entry and no `key` entry. `first` gives the order, because a new write of a `key` entry keeps its `first`. The admin's Mac makes the change at the end of a removal, after the `remove` entry. Each pass of the room checks again, so a change that did not finish finishes later. One change covers each removal before it.

On the admin's Mac, in this order:

1. The Mac makes the room key of the next epoch (32 random bytes). It saves the key in the key file of the room before anything goes to the relay. When the key file has a key for that epoch and the store has no `key` entry for it, an earlier try stopped, and the Mac uses that key.
2. For each member of the member list now, except itself, the Mac seals the new key in a box (see [The key box](#the-key-box)).
3. The Mac writes one kept `key` entry, sealed with the room key of the old epoch: `{"kind": "key", "epoch": <new epoch>, "agreement": "<public agreement key of the admin>", "boxes": {"<member ID>": "<box>", ...}}`. The entry ID is the first 16 bytes of HMAC-SHA256 with the member token of the admin as the key, over `"coconut room key" | room ID (32 bytes) | new epoch (4 bytes, big-endian)`. So a repeat writes the same entry, and no other member can take the ID first.
4. When the relay took the entry, the Mac moves its room to the new epoch. Each next write of the Mac uses it.

Each step is safe to repeat. A write that the relay took without an answer is found by the read before the next write. A failure of the key change is no failure of the removal: the removal answers as before, and the next pass tries the change again.

The entry has `agreement` for a Mac that joins at the time of the change (see [A member that the `key` entry missed](#a-member-that-the-key-entry-missed)). That Mac reads the log from its start. The admin wrote its `member` entry again in the new epoch (see [The kept entries again](#the-kept-entries-again)), so that Mac cannot open it before it has the new key. The admin signs the `key` entry, so `agreement` is as sure as the `member` entry of the admin.

#### The key box

```
box = nonce (12 bytes) | ciphertext (32 bytes) | tag (16 bytes)
```

- The seal is ChaChaPoly with the pair key of the admin and the member. The plaintext is the 32 bytes of the new room key. The nonce is 12 random bytes, new for each seal.
- The pair key is the pair key of a direct message (see [The box](#the-box)) with the label `"coconut room key"` in the place of `"coconut room direct"`. So a key box never opens as a direct message, and a direct message never opens as a key box.
- The additional data is `room ID (32 bytes) | entry ID (16 bytes) | member ID of the admin (32 bytes) | new epoch (4 bytes, big-endian)`: 84 bytes. So a box belongs to one room, one `key` entry, one admin and one epoch.
- A box in the payload is unpadded base64url: 80 characters.

`TeamLog.swift` has the seal and the open of a key box. The helper has the operations `seal_key` and `open_key`. See [Test vectors](#test-vectors).

#### Rules for a Mac that reads a `key` entry

A person's Mac and a Mac for a team only follow the same rules.

1. Rule 4 of a reader accepts the entry only from the admin. The entry is valid only when `epoch` is the epoch of its own body plus 1, `agreement` is a public agreement key, and `boxes` is an object of at most 64 member IDs with a text each. The store keeps no `key` entry that is not valid.
2. A Mac that has the key of that epoch in its key file already opens no box. It only moves its room to the epoch: the Mac is the admin, or a stop came between the save of the key and the move.
3. Else the Mac opens its own box with the pair key of itself and the admin, with `agreement` of the entry. The helper call runs outside a transaction, as for the boxes of direct messages.
4. The Mac saves the key in its key file first. Then the store moves the room to the new epoch. The epoch of a room only moves forward, and one epoch at a time, because the `key` entry of an epoch has a body of the epoch before it.
5. Then the Mac tries again each body that waits (`team_pending`). The bodies of the new epoch open now, also the bodies that came in the same answer as the `key` entry, before it or after it. One of them can be the next `key` entry, so the Mac looks again.
6. A Mac that finds no box of its own, or whose box does not open, keeps its epoch and writes one log line without a key and without a name. Entries of the new epoch wait. A new write of the `key` entry gets a new try.
7. An entry of an older epoch that comes after the change stays valid: a Mac that had not read the `key` entry yet wrote it. A reader opens each entry with the key of its epoch.

#### A member that the `key` entry missed

A Mac can join at the time of the change: it took its invite before the removal closed the invites, and its `member` entry comes after the `key` entry. It has the old key from its welcome and no box. One Mac can miss more than one change: the admin can remove two members before its Mac reads the `member` entry of the new Mac.

- A member of the list needs each `key` entry whose epoch is after the epoch of the body of its `member` entry, up to the epoch of the room. The admin's Mac looks for each such entry that has no box for the member. The admin itself and a removed member never count.
- The Mac writes each such entry again, in the order of the epochs: the same entry ID, the next `rev`, a body of the epoch before its own, and a box for each member that needs it. The boxes of the members that the list still has stay. A removed member gets no box, and its old box goes.
- The reader takes one epoch after the other, so the member opens one key after the other, in one read or across reads.
- Each step is safe to repeat. A member that the helper cannot seal a box for causes no second write of that entry.
- When the key file of the admin lacks a key that such a write needs, the Mac writes one log line without a key and goes on with the other entries.

#### The kept entries again

A Mac that joins after the change gets only the new key in its welcome, so it cannot open a kept entry of an older epoch. So each Mac, after it moved to a new epoch, writes each of its own kept `member`, `bot`, `page`, `chat` and `member_chat` entries again with the new key: the same entry ID, the next `rev`, and the same fields. The admin does not write its `remove` and `key` entries again.

- The store keeps the epoch of the body of each entry (`team_entries`). So the Mac finds the entries that are left after a stop, and a repeat writes nothing.
- The Mac writes at most 100 entries again in one minute in one room, and the rest at the next passes. The relay takes 300 writes of one member in one minute, so the person can still write in the chat.

A write again changes nothing that a person sees. A reader follows this rule:

- A new write of a `member`, `bot`, `page`, `chat` or `member_chat` entry with the same fields as the write that the store has (each field except `rev` and `at`) is a write again.
- A write again goes into `team_entries` as each other write does: its `seq`, its `rev`, its epoch and its payload. So the rule of `rev` and the work of the kept entries above stay correct.
- A write again changes nothing that a person sees. A page keeps its `created_at` and its `updated_at`, a member, an agent and a chat keep their rows and their names, and no event goes to the phone. The version of a page for the phone is the `rev` of its entry, so it can move by one, with no event. The Mac that wrote again keeps its own rows the same way.
- A Mac that reads the entry for the first time, for example a member that joins later, takes it as it comes.
- A real change after a write again, for example a new version of a page, gets a new time and an event, as before.
- The move of a room to a new epoch adds no event, because the phone shows no epoch.

An invite that a member makes after its Mac moved to the new epoch carries the new epoch and the new key in its welcome. A Mac that did not read the `key` entry yet seals the old key, and the Mac that joins with it is a member that the `key` entry missed (see above).

#### Limits

- A removed Mac keeps what it read before the change.
- An old message stays readable with the old key. That is history that the removed member already had.
- A kept entry of a Mac that was off at the change stays in the old epoch until that Mac comes back and writes it again. Until then, a member that joins later does not see it.
- A member that joins later reads nothing of an earlier epoch, as rule 2 of a reader says.
- Only a removal starts a key change. No Mac changes the key by hand or on a timer.

## What the relay sees

The relay stores the room ID, the member IDs, the hashes of tokens and tickets, and for each entry the fields of the entry table. It sees which member writes, when, and how many bytes. It sees no name, no message, no page, and no room key. For a direct message it sees the same fields as for each other entry, and not the kind, so it does not know that the entry is a direct message or for whom it is. For a file it sees the size of each piece, so it can tell the size of a file, but not its name, its type or its bytes. `relay/README.md` allows no plaintext field, and this contract adds none.

The relay accepts a member ID only with a proof of its key, so one member cannot write as another member. The relay itself can hold back an entry or give an old one. It cannot make an entry, change one, or move one to another member, entry ID or room. The `rev` rule stops an old write of an entry on a Mac that already has a newer one.

## Mac

The Mac backend does the steps of this contract for the Team screen of the app and for the tools on the Mac. The modules are in `backend/coconut`: `team.py` (the room operations and the routes), `team_sync.py` (the copy of each log), `team_socket.py` (the socket of each room), `team_store.py` (the tables and the reading rules), `team_keys.py` (the secrets), `team_relay.py` (the relay routes) and `team_helper.py` (the helper).

A Mac that runs a Hermes agent with no Coconut can run Coconut for a team only. See [Team-only mode](#team-only-mode).

A Mac with no person at it, for example a Mac that only runs a Hermes agent, uses the `coconut` tool of Coconut.app (`Contents/Helpers/coconut`). `coconut team` prints the teams of the Mac. `coconut team join` reads the invite link from standard input and joins. `coconut team add-agent <agent>` and `coconut team remove-agent <agent>` add and remove an agent of a Mac for a team only, by its ID or its name. On a person's Mac they print how to share a page in the Library. `coconut team leave --confirm <name of the team>` leaves. The tool calls the routes below with the token of the bridge file, and it prints the error text of the service. It never prints the link. Only a person makes a team and an invite, on the Team screen.

### Helper

`scripts/team-helper.swift` compiles with `ios/Tunnel/TeamLog.swift`, which holds the format. It seals, opens, signs, seals and opens the boxes of direct messages and the boxes of a room key, and reads and makes links and welcomes. It takes no argument and touches no file: it reads one JSON object from standard input and writes one to standard output. So a key, a token or a secret is never in a command line, a file or a log. A failure is `{"error": "<code>"}`, and a code names no value of the input.

- Coconut.app carries the helper in `Contents/Helpers/team-helper`, as a binary for both architectures. `coconut service` and `coconut update` name it in `COCONUT_TEAM_HELPER`.
- A source install compiles it into `<data>/team/team-helper` with `python3 scripts/team.py build`. `deploy.py` runs that command in each update, and the receipt `helper.sha256` stops a second build of the same sources. With `COCONUT_TEAM_HELPER` set, the command compiles nothing.
- The service uses `COCONUT_TEAM_HELPER` when it names an executable file. Else it uses `<data>/team/team-helper` when that file is private: no symbolic link, and no permission for the group or others.
- The helper also has a watch mode that does not end. It holds the socket of each room. See [Socket on the Mac](#socket-on-the-mac).

### Keys and tables

`<data>/team/<room ID>.json` holds the secrets of one room: the member key, the agreement key, the member token, and the room key of each epoch. The folder has mode 0700 and each file mode 0600. The Mac replaces a file in one step.

Everything else is in `coconut.sqlite3`:

| Table | Holds |
| --- | --- |
| `team_rooms` | One row for each room: its relay, its name, the member ID of this Mac, the admin, the state, the epoch, the read position (`seq`), the profile that this Mac writes in its `member` entry, and `notifications`: the choice of the person of this Mac to get notifications of the room. |
| `team_entries` | The newest accepted write of each entry ID, with its payload and the epoch of its body. |
| `team_members` | The members that a Mac shows: the newest valid `member` entry of each member, with its marks `bot_only` and `direct`. Each start of the service sets the marks of each row from its kept entry again, and the marks `open` and `direct` of each row of `team_bots` from its kept `bot` entry. A service of an older version kept these rows without the marks, and the other Mac does not write its entry again. The start reads each payload with the reader of the accept, and never with a JSON function of SQLite, which refuses some JSON that another member can write. A row whose entry does not read, or is over 1 MiB, keeps its marks. The step is a best effort: an error, or a write lock of another connection for over one second, ends it with a log line, and the next start tries again. It writes only when a row is wrong. A room with a corrected row gets `team.updated`. The store keeps the room in `team_marks` until the event goes out, so a stop before the event does not lose it. |
| `team_pending` | The bodies that the Mac cannot open yet, for a later version or a later epoch. Each start of the service and each new room key give them a new try. |
| `team_shares` | The agents of this Mac that author a page in each room, each with the entry ID of its `bot` entry. On a person's Mac a row comes with the first shared page of the agent and goes with the last one. |
| `team_page_shares` | The documents that the person of this Mac shares with each room. A Mac for a team only has no rows here. |
| `team_page_entries` | The entry ID of each document of this Mac that a room has or had as a `page` entry. One document keeps one entry ID. |
| `team_bots` | A view of `team_entries`: the valid `bot` entries of the other members with their marks `open` and `direct`, and the `bot` entries of this Mac with the profile that it wrote. |
| `team_pages` | A view of `team_entries`: the `page` entries of the other members, with the pin of this Mac and the time of the first write that this Mac saw, and the `page` entries of this Mac with the agent and the version that it wrote. |
| `team_messages` | A view of `team_entries`: one row for each valid `message` entry, with its `first`, member, agent, chat, text, reply, mentions, link preview, the times of the relay, whether it was edited, and the type and the level of an alert (`alert_type`, `alert_level`, both null for a plain message), and its thread (`thread`, see [Inbox chats](#inbox-chats)). An index on the room, the chat and `first` gives a page of messages and the unread count of one chat without a scan of the room. Two more indexes hold only the alerts, by type and by level (see [Alerts](#alerts)), and one more gives the messages of one thread. |
| `team_names` | The last name that the store showed for each member and each agent of a room. A `delete` entry does not remove it. |
| `team_reads` | The read position of this Mac in the Team chat of each room. |
| `team_chats` | A view of `team_entries`: the valid `chat` entries of the admin and the valid `member_chat` entries, with the name, the `first` and the time of the first write that this Mac saw. See [Chats of a team](#chats-of-a-team). |
| `team_chat_reads` | The read position of this Mac in each named chat that the person read: one row for each room and chat. |
| `team_chat_mutes` | The named chats that the person of this Mac muted: one row for each room and muted chat. See rule 4 of [Notifications](#notifications). |
| `team_chat_pins` | The chats that the person of this Mac pinned: one row for each room and pinned chat, with an empty `chat` for the Team chat. See [Starred chats](#starred-chats). |
| `team_direct_pins` | The direct chats that the person of this Mac pinned: one row for each room and direct chat, with the member ID of the other person or the team agent ID. See [Starred chats](#starred-chats). |
| `team_triggers` | Only on a team-only Mac: the messages that start a turn of an agent of this Mac, in the order in which the Mac accepted them, each with a mark for a turn that began. See [An agent answers](#an-agent-answers). |
| `team_directs` | The direct messages that this Mac shows: one row for each `direct` entry whose box opens for this Mac, with the other member of its chat, and `bot`, `page` and the link preview from the box. `bot` is null for a chat of two people. An index on the room, the member, `bot` and `first` gives a page and the unread count of one chat. See [Direct messages](#direct-messages) and [Direct chat with an agent](#direct-chat-with-an-agent). |
| `team_direct_reads` | The read position of this Mac in each direct chat of two people: one row for each room and other member. |
| `team_bot_direct_reads` | The read position of this Mac in each direct chat with an agent: one row for each room and `bot` entry. |
| `team_direct_waits` | The `direct` entries whose key comes later in the read. The end of the read tries each one again, then removes it. |
| `team_marks` | The lasting marks of this Mac, for all its rooms. `direct`: an app of the person asked for direct messages. A leave does not remove it. `corrected:<room ID>`: the open of the store corrected a row of the room, and the event for the phone did not go out yet. |
| `team_removed` | The members that a `remove` entry of the admin took out of the room, each with the entry ID of that entry. See [Removal](#removal). |
| `team_invites` | The hash of the ticket and the end of each invite that this Mac made. Only the admin's Mac uses them, at a removal. See [Invite and join](#invite-and-join). |
| `team_erased` | The entry IDs of the messages that an `erase` entry of the admin took out of the chat. See [The admin deletes a message](#the-admin-deletes-a-message). |
| `team_reactions` | One row for each entry that says a reaction, from its newest write: the message, the member, the emoji (empty for a reaction that its member took back), the `seq`, and for a reaction of a direct chat the other member of the chat (`peer`) and the `bot` entry of a chat with an agent. An index on the room, the message, `peer`, `bot`, the member, the `seq` and the emoji gives the reactions of a page in one pass over the rows of its chat. See [Reactions](#reactions). |

A room is `joining` (the relay did not answer the join yet), `active`, `removed` (the relay answered 401) or `gone` (the relay answered 404). The copy of a `removed` or `gone` room stays until the person leaves the room on the Mac. The Library does not show the agents and the pages of such a room.

### Sync

One thread, `coconut-team-sync`, reads each `active` room after its `seq`, one answer at a time. Each answer goes into the store in one transaction. Before that transaction, and without the lock of the service, the Mac reads each payload and checks the fields of each `member`, `bot` and `page` entry. So a page that is slow to check slows only the thread, not the requests of the phone. The transaction then applies the rules that need the store. The thread reads again while `last` is higher.

- The thread reads a room without an open socket every 30 seconds, and a room with an open socket every 5 minutes. A room operation, a save of a team page and `POST /v1/team/sync` start a pass of each room at once. A new `seq` on the socket of a room starts a pass of that room. See [Socket on the Mac](#socket-on-the-mac).
- After a failure, a room waits 60, 120, 240 and 480 seconds, then 900 seconds for each later try.
- After each start of the service, the thread tries the waiting bodies again, because an update of Coconut starts the service again. A new room key gives them a new try too (see [Key change](#key-change)).
- When the log shows no `member` entry of this Mac, the thread writes it, at most every 5 minutes. This repairs a stop between a join and its first write.
- After the read, the admin's Mac makes a key change that is due. See [Key change](#key-change).
- After the read, the thread makes the log equal to the pages that this Mac shares and their author agents. See [Agents and pages](#agents-and-pages). Then it writes each own kept entry of an older epoch again with the key of the room.
- The profile of a room on this Mac follows each accepted `member` entry of this Mac, also one that the sync reads back after a write without an answer. So a later change of one field, or a new `member` entry after a loss, never writes an older name.
- A write of this Mac goes into the store at once. When a write gets no answer, the relay can have it. So before the next write or leave in that room, the Mac reads the room to its end. Its next `rev` is then higher, and a leave finds a `member` entry that only the relay has.
- A leave holds the lock of the room from its `delete` entry until the relay removed the member and the Mac deleted its copy. So the thread cannot write the `member` entry again in between. When the relay does not answer the leave, the room stays `active`, and the Mac is still a member.
- The member token of a room goes only to the relay of that room.
- When the store cannot take one entry for a fault of the Mac code, the Mac drops that entry with a log line, and the next entries of the answer go in. A database failure, for example a full disk, drops no entry: the answer rolls back, and the room tries again later.
- An unexpected failure in one room fails only that room, with the same waits. The other rooms go on.
- No call to the relay or to the helper holds the lock of the service.

A log line names a room by the start of its ID, and an entry by its ID and its `seq`. The relay sees these values too. No log line and no error text has an invite link, a token, a key, a ticket, a welcome or a proof.

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

Who can call what depends on the kind of Mac:

- On a person's Mac, only the person acts in a team, through a paired device. An agent of the person can read the bridge file, so its token reaches only what the `coconut` tool needs: `GET /v1/team/rooms`, `POST /v1/team/join`, `DELETE /v1/team/rooms/<room>` and `POST /v1/team/sync`. Each other team route answers 403 with "Only the person can do this in the team. Use the Coconut app." The rule is an allow-list on the parsed route (`tool_route` in `backend/coconut/team.py`), so a new route stays closed to the token until a change opens it. The rule closes the plain path only: a process of the same macOS account can still read other files of this Mac.
- On a Mac for a team only, no person is there. The token of the bridge file reaches each team route, and no device pairs.

| Request | Answer |
| --- | --- |
| `GET /v1/team/rooms` | The rooms with their members, their agents and their sync state, `helper`, and `mac_bots`: the `id` and `name` of each agent of this Mac that is not archived or deleted, so a tool can find an agent by its name. |
| `POST /v1/team/rooms`, body `{name, member_name, avatar, color, relay}` | Makes a room. This Mac is its admin. |
| `PATCH /v1/team/rooms/<room>`, body `{member_name, avatar, color}` | Changes what the team sees for this Mac. A field that the body leaves out keeps its value. The Mac first reads back an earlier write without an answer. Then it takes each field that the body leaves out from what the log has for this Mac, and writes its `member` entry again with the next `rev`. A failure changes nothing on this Mac. A room that is not `active` answers 409. |
| `POST /v1/team/rooms/<room>/invites`, body `{ttl, uses}` | The invite link. Each member of an `active` room makes one. The defaults are one day and one use. The Mac keeps the hash of the ticket and its end (see [Invite and join](#invite-and-join)). |
| `DELETE /v1/team/rooms/<room>/members/<member>` | The admin removes another member (see [Removal](#removal)), and the answer is the room view. 403 on a Mac that is not the admin, 409 for the member of this Mac and for a room that is not `active`, and 404 for a member that the store does not have. A member that a removal took out before is no error: the steps run again and write nothing new. On a person's Mac, only a paired device can call it. |
| `POST /v1/team/join`, body `{link, member_name, avatar, color}` | Joins a room. |
| `DELETE /v1/team/rooms/<room>` | Leaves the room, and deletes its copy and its keys. A failure of the relay stops the leave of an `active` room. A `joining` room always goes: see [Leave a join that did not finish](#leave-a-join-that-did-not-finish). |
| `POST /v1/team/sync` | Starts a pass, waits up to 60 seconds for it, and returns the rooms with `complete`. |
| `PUT /v1/team/rooms/<room>/pages/<document>` | The person shares a page of this Mac with the room. See [Agents and pages](#agents-and-pages). |
| `DELETE /v1/team/rooms/<room>/pages/<document>` | The person stops sharing the page with the room. |
| `POST /v1/team/rooms/<room>/bots`, body `{bot_id}` | On a Mac for a team only: shares an agent of this Mac with the room. An archived or deleted agent answers 409 or 404. A person's Mac answers a paired device with 409 and the text "The agents of this Mac stay yours, so a team does not get them. To share a page with the team, open it in the Library of the Coconut app." An app of today shows that text. |
| `DELETE /v1/team/rooms/<room>/bots/<bot_id>` | On a Mac for a team only: stops sharing the agent with the room. A person's Mac answers 409 as above. |
| The routes of the Team chat | See [Team chat](#team-chat). |
| The routes of the direct messages | See [Direct messages](#direct-messages). |

A room in an answer has what the Team screen shows, so the app needs no other request:

| Field | Value |
| --- | --- |
| `room`, `name`, `state`, `created_at` | The room ID, the name of the team, `joining`, `active`, `removed` or `gone`, and the time that this Mac made or joined the room. |
| `is_admin`, `can_invite` | `is_admin` is true when this Mac is the admin. `can_invite` is true for each member of an `active` room. |
| `can_delete_messages` | True on the admin's Mac of an `active` room: the person can delete a message of another member or of an agent. The app shows the action only with this field, so an older service shows no action. |
| `members` | In the order of their first `member` entry: `member` (the member ID), `name`, `avatar`, `color`, `admin`, `this_mac`, `bot_only`, and `can_remove`: true on the admin's Mac of an `active` room for each other member. The app shows the action of a removal only for a member with `can_remove`, so an older service shows no action. |
| `bots` | Each agent of the team, while the room is `active` and the member of the agent is in `members`. `id`, `name`, `avatar`, `color`, `member` (the ID of the Mac that runs the agent), `member_name`, `this_mac` and `open`. An agent of a person's Mac is only the author of the pages that the person shares, and its `open` is false. The app lists and counts only the agents with `open` true: the agents that a teammate can ask. An agent of this Mac also has `bot_id`, its ID on this Mac, and its profile comes from that agent. An archived or deleted agent of this Mac is not in the list. The agents follow the order of the members, then their names. |
| `sync` | `synced_at`, and `error`: one sentence for a person, or null. Also `seq`, `entries`, `waiting`, `refused`: the writes that the relay refused (see [Agents and pages](#agents-and-pages)), and `socket`: true while the socket of the room is open. The app does not show `socket`. |
| `unread`, `last_message` | The unread count of the Team chat, from 0 to 99, and the newest message that the chat shows: `first`, `member`, `member_name`, `this_mac`, `bot`, `bot_name`, `text` (its first 200 characters) and `created_at`, or null. `member`, `this_mac` and `bot` have the values of a message in a page, so the Chats list of the app can show "You" for a message of this Mac's person: `this_mac` is true and `bot` is null. An older service sends none of the three. See [Team chat](#team-chat). |
| `mentioned`, `mention_first` | `mentioned` is true while an unread message of the Team chat names this Mac's person. The rule of the unread count decides which message is unread. `mention_first` is the `first` of the newest such message, and it is absent when `mentioned` is false. An older service sends neither, so the app shows people in its `@` list only for a room with `mentioned`. See [Mentions of a person](#mentions-of-a-person). |
| `typing` | Who types in the Team chat now, the earliest start first: `member`, `name`, `bot`, `bot_name` and `seconds` for each other subject. See [Typing](#typing). An older service sends none. |
| `chats`, `can_make_chats`, `can_manage_chats` | The named chats of the team, true on each Mac of an `active` room, and true on the admin's Mac of an `active` room. See [Chats of a team](#chats-of-a-team). An older service sends no `can_make_chats`, and an older one still sends none of the three. |
| `pinned` | Only on a person's Mac: true when the person pinned the Team chat. Each item of `chats` has its own `pinned` too. See [Starred chats](#starred-chats). |
| `relay`, `member`, `admin`, `epoch` | For the tools. |
| `directs` | Only for a request with `X-Coconut-Team: direct` from a paired device. Then each member also has `direct`. See [Direct messages](#direct-messages). |
| `bot_directs` | Only for a request with `X-Coconut-Team: direct` from a paired device. Then each item of `bots` also has `direct`. See [Direct chat with an agent](#direct-chat-with-an-agent). |

The `id` of an agent is `t-<room ID>-<entry ID>`, as in `team_bots` of `GET /v1/documents`.

Each error text of these routes is for a person, because the app shows it: what happened and what to do, without a word of the code. For example: "This Mac is no longer in the team." A Mac without a helper answers 503 with "Update Coconut on this Mac, then try again."

When something changes that the Team screen shows, the Mac adds one `team.updated` event with `{"team": <room ID>}` in the same transaction: this Mac makes, joins or leaves a room, a room becomes `active`, `removed` or `gone`, a member joins, leaves or changes its name, avatar or color, an agent enters or leaves the team, or `sync.error` or `sync.refused` appears, changes or clears. A failed try with the same error adds no event. `GET /v1/updates` then has `team_changed`, and the app reads `GET /v1/team/rooms` again. A pass that changes nothing adds no event. The event type is in the `known` set of `GET /v1/updates` and changes no Work row, so an app of today does not read its whole state again. A change of an agent of this Mac, for example a new name, comes with its own `bot.updated` event.

No answer has a key, a token or an invite secret, and so no answer has the public agreement key of a member. Only the answer of the invite route has the link. The request log of the tunnel listener keeps the route without its IDs and never a body, so the link of a join stays out of it. `scripts/team.py join` and `coconut team join` read the link from standard input, so the link is never an argument of a command. `scripts/team.py share <room ID> <agent ID>` and `unshare <room ID> <agent ID>` call the agent routes of a Mac for a team only.

#### Repeat a write

The app sends a write again with the same `Idempotency-Key` when the answer does not come: after a lost connection, a timeout or a relay that does not answer (`APIClient.swift`). A team write calls the relay and can take seconds, so the first request can still run or be finished. Without a rule, a repeat makes a second team, or a join answers "already in this team" after a join that worked.

- A paired device can send an `Idempotency-Key` (a UUID) with each write: each route of the table except the `GET` and `POST /v1/team/sync`. A key that is not a UUID answers 400.
- The Mac keeps the first successful answer for a key of a device. A second request with the same key from the same device gets that answer and does nothing else.
- A second request that comes while the first one runs waits for it, then gets its answer. When the first request fails, the Mac keeps no answer, and the second request runs.
- The same key with another method, path or body answers 409. The same key from another device runs as its own request.
- The answers stay in the memory of the service only, for 10 minutes, and at most 256 of them. The sync thread drops an old answer on its next pass, at most 30 seconds later, also on a Mac without a room. A new answer drops the oldest answer over the limit at once. A stop of the service drops them all. A later repeat runs again.
- The answer of the invite route has the invite link, so the answers never go to the database or to a log. The `requests` table of the other writes of the phone keeps each answer with no end, so these routes do not use it.
- A request without a key runs each time, as before. A tool on this Mac sends no key, and the Mac ignores a key from a tool.
- The routes `.../typing`, `.../notifications` and `.../pin` keep no answer, and each request with a key runs. A PUT of a state is safe to repeat by itself. A saved answer of a mute would cancel a later mute with the same key and body after an unmute, because the app sends the key of a write without an answer again.

#### Leave a join that did not finish

A room stays `joining` when the relay does not answer the join. The relay can never answer, for example when the link named a server that does not exist. So the leave of a `joining` room always removes the room from this Mac:

1. The Mac asks the relay to remove its member, as for an `active` room, because the relay can have taken the join.
2. When the relay does not answer or refuses, the Mac writes a log line without a secret.
3. The Mac deletes its copy and its key file, and answers `{"room": <room ID>, "left": true}`.

A relay that took the join keeps this Mac as a member without a Mac: the member token stays valid there, and the admin sees no `member` entry of this Mac. The admin cannot remove it, because no Mac shows the member. For an `active` room, a failure of the relay still stops the leave, because the Mac is still a member there.

### Agents and pages

`backend/coconut/team_pages.py` shares the pages of this Mac and their author agents. `TeamStore` reads the agents and pages of the other members.

To share on a person's Mac:

- The person shares one page with one team: `PUT /v1/team/rooms/<room>/pages/<document>`. `DELETE` on the same path stops. Both are safe to repeat, and the app asks no question before either. A Mac in more than one team shares each page with each team on its own.
- The answer of each is `{"document": <document>}`: the document as `GET /v1/documents` lists it. For example:

  ```json
  {"document": {"id": "<document ID>", "bot_id": "<agent ID>", "key": "weekly-report", "title": "Weekly report",
                "format": "markdown", "version": 3, "audience": "team",
                "teams": [{"room": "<room ID>", "name": "Example Team"}]}}
  ```

- A share answers 404 for a document that this Mac does not have, a team page of another Mac, a page past its end and a deleted agent, 409 for an archived agent and a room that is not `active`, and 413 for a page that does not fit in one entry.
- A shared page goes to the team with its agent as the author, and so does each later version that the agent saves. A stop, a delete of the page or of its agent, and a leave of the room take the page out. The phone, the agent and the tooling of the Mac (`DELETE /v1/gateway/document`) each delete a page.
- The author entry of an agent: the share row of the agent (`team_shares`, with the entry ID of its `bot` entry) comes in the transaction of the first shared page of that agent in the room. It goes in the transaction of the stop or the delete of the last one. The pass then writes or deletes the `bot` entry. While each shared page of an agent is past its end or its agent is archived, the pass deletes the `bot` entry too, and it writes it again with the same entry ID when a page shows again.
- An agent cannot share. `coconut_save_document` has no `audience` argument on a person's Mac, and a save through it or through `POST /v1/gateway/document` that names an audience gets 400 with "Only the person can share a page with a team. Save the page without audience." A save without it keeps the shares of the page.
- What an agent reads about its documents has `audience`: `"team"` while the person shares the page with at least one team, else `"private"`.
- A one-time step at the start of the service keeps each page that a team had before this rule: each document with the audience "team" becomes a share with each room that its agent was shared with, with the same entry IDs, and the document loses the field. An agent without a shared page loses its share row, and the next pass deletes its `bot` entry. The step finds nothing to do after its first run.

To share on a Mac for a team only, see [Team-only mode](#team-only-mode): a tool shares an agent with a room, and each page of the agent with the audience "team" goes to each room of the agent. A new document without the field is private there. A save of an existing document without the field keeps its audience.

The sealed body is the payload and 97 bytes, and the relay takes at most 1 MiB. So a save of a team page and a share check the size of the payload and answer 413 when the page does not fit. The payload is JSON without ASCII escapes, so text outside ASCII does not grow.

To write:

- A save only changes the document and calls `team_wake`. A save holds a transaction, and no call to the relay or the helper holds the lock of the service.
- In each pass, after the read of a room, the Mac derives what it wants to share: on a person's Mac, the shared pages of the room that are not past their end and whose agent exists and is not archived, and the author agents of these pages; on a Mac for a team only, its shared agents that exist and are not archived, and their documents with audience `team` that are not past their end. It compares that with its own `bot` and `page` entries in `team_entries`. It keeps no second copy of the state.
- An agent or a page that the log does not have, or has in an older form, gets a kept entry. The form of an agent is its name, avatar and color. The form of a page is its `bot` entry and its version, because each save of a document gives a new version. The store keeps the form of each own entry in the columns of `team_bots` and `team_pages`, also for an own page that the reading rules refuse. So a pass parses no payload, and a pass after the writes writes nothing and opens no write transaction.
- An own `bot` or `page` entry that the Mac no longer wants gets a `delete` entry: the page is deleted, no longer shared or past its end, or the agent is no longer shared, has no shared page, is archived or is deleted.
- An agent can have a longer name, avatar or color than the limits of the entry. The entry takes the start of each.
- The relay can refuse one write for the entry itself: 413 for a body over the limit, 507 when the kept entries are full, or another status of the 400s except 401, 404 and 429. Then the other writes go on. The Mac keeps the refusal in memory until the form of the entry changes, and `sync.refused` of the room shows it. A failure of the relay or of the helper stops the pass of the room, as a read does.
- A leave needs no extra step: the reading rules below hide the agents and pages of a member that left.

Rules for a Mac that reads `bot` and `page` entries:

1. A `bot` entry is valid with the name, avatar and color rules of a `member` entry. An entry that does not fit stays in the store and shows nothing.
   - `bot_only: true` in a `member` entry marks a Mac that runs Coconut for a team only: no person is at it. A missing `bot_only` and each other value are false. The Mac keeps the mark in `team_members`, and `GET /v1/team/rooms` gives `bot_only` for each member. The app counts only the other members as people. The Team screen lists such a Mac under "Agents" and not under "Members": each agent of the Mac (the `member` of the agent), or one row "No agent yet" when the Mac has no agent in the team. A team-only Mac writes its `member` entry again one time when its row in the store has another mark, for example after an update from an older version.
   - `direct: true` marks an agent that answers direct messages. It follows the rules of `open`: only a Mac for a team only writes it, only while the record of the agent has `chat` true, and a missing `direct` and each other value are false. The Mac keeps the mark in `team_bots`. See [Direct chat with an agent](#direct-chat-with-an-agent).
   - `open: true` marks an agent that answers each member in the team chat. Only a Mac that runs Coconut for a team only writes it, and only while the record of the agent has `chat` true. An agent of a person answers only that person, so a full Mac never writes it. A declared agent starts with `chat` false. The step that connects the agent to its Hermes gateway sets `chat` with `PATCH /v1/team/bots/<bot>`, so the app never offers an agent that stays silent. A missing `open` and each other value are false, and the agent still shows. The Mac keeps the mark in `team_bots`, and `GET /v1/team/rooms` gives `open` for each agent. A reader trusts the mark only for what it says about the chat: the list of the agents that a member can name with `@`, and on the Team screen "Pages only" for an agent of a Mac for a team only without the mark. The mark is part of the form of an own `bot` entry, so a change of `chat` writes the entry again in each team of the agent at the next pass.
2. A `page` entry is valid only when its `bot` is an entry ID, its `expires_at` is null or a UTC time as `YYYY-MM-DDTHH:MM:SSZ`, each text field is valid Unicode text, and the page passes the rules of a local save (`page_to_save`: key, title, format, content size, summary, and for HTML no finding of `page_problems`). The Mac does not trust the check of the writer. HTML that the parser of the Mac cannot read breaks the page rules. A page that does not fit stays in the store and shows nothing.
   - When a check of a `member`, `bot` or `page` entry fails with an error, the entry also stays in the store and shows nothing, and the log of the service has the reason. Another error in the part of the read without the database drops only that entry. So one entry never stops the sync of a room.
3. A page shows only while its `bot` names a valid `bot` entry of the same member, and while that member is in the member list. So a page of an agent of another member shows nothing, a `delete` over the `bot` entry hides its pages, and a member that leaves takes its agents and pages from each Library.
4. A page past its `expires_at` shows nothing.
5. The times come from the relay, not from the payload: `updated_at` is the `at` of the entry, and `created_at` is the `at` of the first write that this Mac saw. A time after the year 9999 makes the page not valid. `version` for the phone is the `rev` of the entry.
6. The Mac that wrote a page has the document already. Its own entries show nothing more in its Library.
7. The Library shows the agents and the pages of a room only while the room is `active` on this Mac. A person that is no longer in a team does not keep its pages, and the pages of a room that is gone can never get an update.

The Library of the phone:

- `GET /v1/documents` lists the team pages with the local documents. A team page has each field that the app needs for a document, `pinned`, and `team` with `room` and `name`. Its `id` is `t-<room ID>-<entry ID>`, and its `bot_id` is the ID of its team agent in the same form. A local ID is a UUID, so the two never meet.
- The answer also has `team_bots`: for each team agent `id`, `name`, `avatar`, `color`, `team` (`room` and `name`) and `member` (`id` and `name` of the Mac that runs it). The app of today reads only `documents`, and it hides a document of an agent that it does not know.
- A local document has `teams`: each `active` room that the person shares it with, as `room` and `name`, in the order of the names. It also has `audience` for an app of today: `"team"` when `teams` is not empty, else `"private"`. `GET /v1/documents/<id>` and the answer of a pin have the same fields. A share and a stop add one `document.updated` event, and a share or a stop that adds or removes an author agent adds one `team.updated` event.
- `GET /v1/documents/<id>` of a team page adds `content`. `PATCH` sets the pin of this Mac. `DELETE` answers 403, because only the Mac of the page can delete it. A page past its end answers 404 as an ended local document does.
- When a read changes the team pages or agents that the Library shows, or a room with team agents stops being `active` or goes from this Mac, the Mac adds one `document.updated` event with `{"team": <room ID>}`. `GET /v1/updates` then has `documents_changed`, also for a local document, and the app can read the list again. The app of today knows the event type, so the event does not make it read the whole state again.
- The list reads the views `team_bots` and `team_pages` and parses no payload. The content stays in the payload of the entry, and the Mac reads it when a page opens.

### Team chat

`backend/coconut/team_messages.py` serves the Team chat of each room. One message is one `message` entry, not kept. The relay holds it for its retention time, and each Mac keeps the history in `team_messages`.

The payload of a `message` entry:

| Field | Value |
| --- | --- |
| `text` | The message: 1 to 16,000 code points of valid Unicode text. It is Markdown, as in an agent chat. The Mac does not interpret it. |
| `reply_to` | The entry ID of the message that this one answers, or absent. |
| `mentions` | A list of at most 16 entry IDs of `member` or `bot` entries that the message names, or absent. |
| `bot` | The entry ID of a `bot` entry of the same member when an agent of that Mac wrote the message, or absent for a message of the person. |
| `preview` | The link preview of the first link of `text`: `{"url", "title", "site"}`, or absent. See [Link previews](#link-previews). |
| `files` | The files of the message: a list of 1 to 4 items, or absent. A message with files can have an empty `text`. See [Files](#files). |
| `alert` | The alert of a message of an agent: `{"type", "level"}`, or absent. See [Alerts](#alerts). |

- An edit is a new write of the same entry ID with the next `rev`. A removal is a `delete` entry over it. Only the member of a message can do either (rule 3). The admin can also take a message out of the chat with an `erase` entry (see [The admin deletes a message](#the-admin-deletes-a-message)).
- Only a team-only Mac writes `bot`: one entry for each complete message of an agent that answers in the chat. See [An agent answers](#an-agent-answers). It never edits such a message, so `edited_at` of a message of an agent means an edit of another version.

Rules for a Mac that reads a `message` entry:

1. A message is valid only when `text` has 1 to 16,000 code points of valid Unicode text, or `text` is empty and `files` has at least one item that fits, `reply_to` and `bot` are entry IDs, and `mentions` is a list of at most 16 entry IDs. A reader takes null as an absent field. A message that does not fit stays in the store and shows nothing.
2. A field that the reader does not know stays in the payload, and the reader ignores it. So a later field, for example an attachment, does not break a Mac of today. A `preview` that does not fit the rules of [Link previews](#link-previews) is left out, and the message shows without it. The same holds for `files` that do not fit the rules of [Files](#files), and for an `alert` that does not fit the rules of [Alerts](#alerts).
3. A message with a `bot` shows only when `bot` names a valid `bot` entry of the same member. The `bot` entry can come after the message, so the check runs when the Mac reads the chat, not when it accepts the entry. A `delete` over the `bot` entry does not hide the messages of the agent that the chat already had.
4. The newest write of an entry counts, as the relay keeps only that write. A `delete` over a message removes its row. A later `message` write over the `delete` shows the message again, also on a Mac that joins later and reads only that write. A Mac of today never writes it.
5. The chat sorts the messages by `first`, so an edit keeps its place.
6. The times come from the relay, not from the payload: `created_at` is the `at` of the first write that this Mac saw, and `edited_at` is the `at` of the newest write. A message is edited when its `rev` is over 1, so each Mac gives the same answer. A time after the year 9999 makes the message not valid. This Mac stores its own write at once with its own clock, and takes the time of the relay when it reads the write back. Before an edit, the Mac reads the room to its end, because the edit replaces the earlier write on the relay. So `created_at` gets the time of the relay also when the person edits a message before the next pass. An edit costs one more read of the relay.
7. A message of a member that left stays in the history. The relay deletes the kept entries of that member, and a `delete` entry replaces its `member` entry. So `team_names` keeps the last name that the store showed for each member and each agent. A Mac that joins after the member left never saw a name, and `member_name` is null.

What a Mac keeps after the relay drops an old entry: the row of each message in `team_messages`, its payload in `team_entries`, and the names in `team_names`. A Mac that joins later reads only what the relay still has: the messages of the last 90 days.

The read position:

- `team_reads` keeps, for each room, the `first` of the newest message that the person read. The unread count is the number of shown messages of the other members after that position, up to 99. Only a message of this Mac's person counts as read. A message with a `bot` of this Mac counts as unread, because the person did not write it.
- The position starts at the `first` of the own `member` entry of this Mac. So the history from before a join does not count as unread.
- The position only moves forward, and never past the newest message of the room.
- A message notifies the phones of a person's Mac only while it is after the position. So a message that the person reads on the Mac or on another device before the notification goes sends nothing (see [Notifications](#notifications)).

The routes have the authorization of the other team routes, and a write with an `Idempotency-Key` is safe to repeat (see [Repeat a write](#repeat-a-write)). A write goes to the relay inside the request. A failure is an error for the person and changes nothing on this Mac. A write in a room that is not `active` answers 409.

| Request | Answer |
| --- | --- |
| `GET /v1/team/rooms/<room>/messages?before=<first>&limit=<n>&chat=<id>` | One page, newest first: `{room, messages, before}`. `limit` is 1 to 100, and 50 without it. `before` of the answer is the `first` to ask for the next page, or null when no older message exists. Also a room that is not `active` has its history. With `alert_type=<type>` or `alert_level=<level>`, the page has only the alerts of that type and that level, and a page without `before` also has `alerts`: the counts of the alerts of the chat (see [Alerts](#alerts)). |
| `POST /v1/team/rooms/<room>/messages`, body `{text, reply_to?, mentions?, chat?, preview?, files?}` | Writes a message of the person and answers `{room, message}`. `files` are the IDs of drafts of this Mac, and with them `text` can be empty or absent (see [Files](#files)). `preview` is the link preview of the app, or null for none (see [Link previews](#link-previews)). `reply_to` is the `id` of a message that the chat shows. `mentions` is a list of member IDs of `members` and `id` values of `bots` of `GET /v1/team/rooms`. The Mac writes the entry IDs of the contract. With an `Idempotency-Key`, the entry ID of the message comes from the room, the device and the key. So a repeat after a write without an answer finds that write and sends no second message. |
| `PATCH /v1/team/rooms/<room>/messages/<id>`, body `{text}` | Changes the text of a message of this Mac's person, and answers `{room, message}`. The other fields stay, also `files`. A message with a file takes an empty text. The link preview stays only while the new text contains its `url`. The same text writes nothing. A message of another member or of an agent answers 403. |
| `DELETE /v1/team/rooms/<room>/messages/<id>` | Removes a message of this Mac's person with a `delete` entry, and answers `{room, id, deleted: true}`. When the entry is already a `delete` entry of this Mac, for example after a delete without an answer, the Mac gives the same answer and writes nothing. An ID that the store does not have answers 404. On the admin's Mac, a message of another member or of an agent gets an `erase` entry (see [The admin deletes a message](#the-admin-deletes-a-message)). On another Mac, an entry of another member answers 403. |
| `POST /v1/team/rooms/<room>/read`, body `{first, chat?}` | Moves the read position, and answers `{room, read, unread}`. |
| `PUT /v1/team/rooms/<room>/messages/<id>/reaction`, body `{emoji}` | Sets the reaction of this Mac's person on a message of a team chat, and answers `{room, message}`. See [Reactions](#reactions). |
| `POST /v1/team/rooms/<room>/typing`, body `{chat?}` | The person types in the Team chat, and answers `{room}`. See [Typing](#typing). |
| The routes of the named chats | See [Chats of a team](#chats-of-a-team). Without `chat`, each route above means the Team chat. |
| `PUT /v1/team/rooms/<room>/notifications`, body `{enabled}` | Turns the notifications of the room on or off for the phones of this Mac, and answers `{room, notifications}`. See [Notifications](#notifications). |
| `PUT /v1/team/rooms/<room>/chats/<id>/notifications`, body `{enabled}` | Mutes (`false`) or unmutes (`true`) one named chat for the phones of this Mac, and answers `{room, chat, notifications}`. See [Notifications](#notifications). |
| `PUT /v1/team/rooms/<room>/pin` and `PUT /v1/team/rooms/<room>/chats/<id>/pin`, body `{pinned}` | Pins or unpins the Team chat or one named chat on this Mac, and answers `{room, chat, pinned}` (`chat` is null for the Team chat). See [Starred chats](#starred-chats). |
| `PUT /v1/team/rooms/<room>/directs/<chat>/pin`, body `{pinned}` | Pins or unpins one direct chat on this Mac, and answers `{room, member, pinned}` or `{room, bot, pinned}`. See [Starred chats](#starred-chats). |

A message in an answer:

| Field | Value |
| --- | --- |
| `id`, `first` | The entry ID and the `first` of the entry. |
| `member`, `member_name`, `this_mac` | The member ID of the writer, its name (see rule 7), and true for a message of this Mac. |
| `bot`, `bot_name` | The `t-<room ID>-<entry ID>` ID of the team agent that wrote the message, and the last name that `team_names` has for it, also for an agent that the team no longer has. Both are null for a message of a person. |
| `text`, `reply_to` | The text, and the `id` of the message that this one answers, or null. |
| `chat` | The entry ID of the named chat of the message, or null for the Team chat. |
| `reply` | The message that `reply_to` names, so the app can quote it without a second request: `id`, `member`, `member_name`, `bot`, `bot_name`, and the first 200 code points of its `text`. Null when `reply_to` is null, or when the chat does not show that message: removed, not valid, or never seen by this Mac. One query reads the answered messages of a whole page. |
| `mentions` | For each mention: the member ID of a member, or the `t-` ID of a team agent. A mention that the store cannot name stays out. |
| `reactions` | The reactions on the message: one item `{emoji, members, mine}` for each emoji with at least one member, in the order of the six emoji. See [Reactions](#reactions). An older service sends none. |
| `preview` | The link preview `{url, title, site}`, or null. See [Link previews](#link-previews). An older service sends no key. |
| `files` | The files of the message, or an empty list. See [Files](#files). An older service sends no key. |
| `alert` | The alert of a message of an agent, `{type, level}`, or null for a plain message. See [Alerts](#alerts). An older service sends no key. |
| `created_at`, `edited_at` | The times of rule 6. `edited_at` is null for a message without an edit. |

When the chat of a room changes on this Mac, the Mac adds one `team_messages.updated` event with `{"team": <room ID>}` in the same transaction: a new message, an edit, a removal, a new time from the relay, a new or changed name in `team_names` of a room with messages (for example a rename, or a `bot` entry that makes a message of its agent show), a new, changed or removed reaction on a message that the chat has, or a new read position, for example from another device of the person. The same name written again adds no event. `GET /v1/updates` then has `team_messages_changed`: the IDs of these rooms. A pass that changes nothing adds no event. The event type is in the `known` set of `GET /v1/updates` and changes no Work row, so an app of today does not read its whole state again.

No log line has the text of a message. A log line names a message by its entry ID and its `seq`, as for each other entry.

#### The admin deletes a message

The admin of a team takes a wrong or harmful message of another person or of an agent out of the chat. The relay lets only the member of an entry replace it, so the admin cannot write a `delete` entry over that message. The admin's Mac writes an `erase` entry instead. The relay cannot read the kind of an entry, so the relay needs no change.

The payload: `{"kind": "erase", "entry": "<entry ID of the message>"}`. The entry is not kept. Its entry ID is the first 16 bytes of HMAC-SHA256 with the member token of the admin as the key, over `"coconut room erase" | room ID (32 bytes) | entry ID of the message (16 bytes)`. So a repeat writes the same entry, and another member cannot take the ID first.

On the admin's Mac, `DELETE /v1/team/rooms/<room>/messages/<id>` for a message that this Mac's person did not write:

1. The Mac reads back an earlier write without an answer, as for each write of the chat.
2. When the store has the message in `team_erased`, or the author deleted it already, the Mac writes nothing.
3. Else the Mac writes the `erase` entry, and the message goes from the chat of this Mac at once.
4. The answer is `{room, id, deleted: true}`, as for an own message.

Rules for a Mac that reads an `erase` entry:

1. Rule 4 of a reader accepts the entry only from the admin. An `erase` entry of another member changes nothing, and the Mac writes one log line with the entry ID and the start of the member ID, as for each refused entry.
2. An `entry` that is not an entry ID changes nothing.
3. When the store has the named entry and its kind is not `message` or `delete`, the entry changes nothing. So the admin cannot remove a `direct`, `page`, `member`, `bot`, `key` or `remove` entry. These kinds have their own rules, and the admin cannot read a direct message.
4. Else the Mac keeps the entry ID in `team_erased`, and removes the message as a `delete` over it does: from the chat, from the unread count, and from the notifications that wait (each plan and each send checks that the message still shows). On a team-only Mac, the triggers of the message that did not start go, and a running turn of it stops (see [An agent answers](#an-agent-answers)).
5. The removal holds. A later write of the message by its author, for example an edit with a newer `seq`, shows nothing. An `erase` entry that comes before its message counts when the message comes. A later write over the `erase` entry does not bring the message back. The author's Mac refuses an edit of an erased message with 404.
6. A message of an agent and a message of a removed member can be erased.

`team_erased` has one row of two IDs for each message that the admin deleted, and only the admin writes `erase` entries. The rows go when the room leaves this Mac.

A Mac with an older Coconut does not know the kind. It keeps the entry by rule 5 of a reader and still shows the message. The app shows the action "Delete" on a message of another member only when the room has `can_delete_messages`.

#### Notifications

A team message notifies the person like an agent reply: the same pipeline, the same timing and the same privacy (`backend/coconut/push.py`). Only a person's Mac does this. A Mac for a team only has no phone and no push.

1. A message notifies only when it is the first write (`rev` 1) of a `message` entry that this Mac sees for the first time, shows, counts as unread (a message of another member, or a message with a `bot`), is after the read position, and has an `at` under one hour old. The read position comes with the own `member` entry of this Mac, so the history that a Mac reads after a join notifies nothing. An edit, a removal and a read again notify nothing.
2. The transaction that accepts the entry writes one item of the kind `team` for each phone in `push_items`: the room in `team`, the entry ID in `message_id`, and no agent, task or turn. No network call runs in that transaction.
3. A phone gets an item when its notifications are on, it sent its preferences, and the [notification settings](#notification-settings) let the message notify: People is not Nothing, and no pause runs. Until the first change of those settings, the level of the phone decides instead, and Only urgent gets none. The switch for agent replies does not decide. A phone without stored preferences has an older app and gets none.
4. Each room has `notifications` on this Mac, on at the start. It is a choice of the person, not of the team: it never goes into the log or to the relay. `GET /v1/team/rooms` gives it for each room on a person's Mac, and `PUT /v1/team/rooms/<room>/notifications` changes it. Only a paired device calls that route, and a change adds `team.updated`. A Mac for a team only answers 409. The switch decides only for a message that does not name this Mac's person. A message whose `mentions` have the entry ID of a `member` entry of this Mac notifies also when the switch is off, and each other rule stays: the level, the work hours, the read position and the checks of rule 5. A message that names nobody, or only other members, follows the switch.

   Each named chat also has a choice of this Mac's person: muted or not, not muted at the start. The Team chat has no such choice: the switch of the room decides for it and for each named chat. The choice is the person's too: it never goes into the log or to the relay, and the Mac keeps it in `team_chat_mutes`. So the rule is: a message notifies when the switch is on and its chat is not muted, or when it names this Mac's person. A mention notifies also in a muted chat. `GET /v1/team/rooms` gives `notifications` for each named chat on a person's Mac: false for a muted chat. `PUT /v1/team/rooms/<room>/chats/<id>/notifications` changes it, with the rules of the route of the room: only a paired device, 409 on a Mac for a team only, and a change adds `team.updated`. A chat that the store does not show answers 404 with "This chat is no longer in the team." The same value again writes nothing. When the chat goes (a `delete` entry over it, a new write that does not fit, a leave of the team), its row goes too. The unread count of a muted chat does not change.
5. Each plan and each send checks the item again: the room is `active`, the message still shows, it is after the read position of its chat, and the rule of rule 4 holds: `notifications` is on and the chat is not muted, or the message names this Mac's person. So a message that the person read on another device, a removed message, a team that the person left, a quiet team and a muted chat send nothing. Only a message that names the person still notifies in a quiet team or a muted chat.
6. An item waits and groups as an agent reply does (level, work hours, the gap after the last notification, and the wait while the person works at a Mac, `mobile_delay`). It is never urgent. The app icon badge counts cards only.
7. The sealed alert has the name of the writer as its title (the name of the agent for a message with a `bot`), the name of the team as its subtitle, and the text as its body. When the store has no name for the writer, the title is the name of the team, and the alert has no subtitle. `thread-id` is the room ID. A phone without a relay route gets "Your team has a new message." and no text.
8. A group counts "team message" and "team messages" after "message". A group of the messages of one team has the thread of the team and `{"kind": "group", "count": n, "team": <room ID>}`, so a tap opens the chat. Each other group opens Inbox, and its fields for older apps come from an item that is not a team message.
9. The `coconut` part of one message is `{"version": 1, "device_id": ..., "kind": "team", "team": <room ID>}`. An older app finds no `bot_id` there, so a tap opens the app and nothing more.

No log line has the text of a message, the name of a writer or the name of a team.

#### Notification settings

The person has one set of notification settings for every device of this Mac, as an account of Slack has (`backend/coconut/notification_settings.py`). The page Notifications of the You tab shows them on the iPhone and on the Mac. They are a choice of the person, as `notifications` of a room: they never go into the log or to the relay.

- `GET /v1/state` gives `notifications`. `PUT /v1/settings/notifications` changes the keys of its body, answers the whole object and adds `settings.updated`. The apps call it. The Coconut chat changes them with the setting `notifications`.
- `people`: `all` lets each message of a team notify by rule 4 of [Notifications](#notifications). `mentions` lets only a direct message, a notice, a message that names this Mac's person and a message with a keyword notify. `nothing` lets none.
- `agents`: `all` notifies for cards and replies, `cards` for cards only, `urgent` only for a card that needs attention now.
- `keywords`: at most 30 words of at most 40 characters, each one once in any case. A team message that has one as a whole word, in any case, notifies as a mention does, also in a quiet team and in a muted chat.
- `paused_until`: a time in UTC, at most 30 days ahead. Until then nothing notifies, on a phone or as a banner of a Mac, but a card that needs attention now. What arrives in a pause never notifies later. A time in the past ends the pause.
- `mobile_delay`: null, or 0 to 60 minutes. While it is set and macOS shows the banners of Coconut, the Mac app reports the idle time of its keyboard and mouse with `PUT /v1/device/presence {idle}` every 30 seconds, and the service keeps the time of the last input in memory. While a Mac had input within the delay (at least one minute), each phone waits and the Mac shows the banners. After it, each item that the person did not read notifies as usual. Without a report, as after a restart of the service, a phone does not wait.
- Before the first change nothing is stored: each phone follows its own level and its switch for agent replies, as before, and the apps show the settings that match the newest phone (Only urgent gives `nothing` and `urgent`, replies off gives `cards`). After the first change the settings decide for each phone that sent its preferences, and each phone keeps its work hours. A phone of an older app keeps its own rule.
- The banners of the Mac app follow the same settings (`NotificationSettings` in the app): for team messages, for new cards and for new replies of an agent chat. A muted agent chat still shows its cards.
- `github`: `all` notifies each new thread of the GitHub inbox of the connected account, `mentions` (the default) a mention, a mention of a team, a review request and an assignment, `nothing` none. `mail`: `all` notifies each new unread mail in the Inbox of the Google connection of the agents, `important` (the default) a mail that Gmail marks important, `nothing` none. The apps show their rows only when the Mac sends both keys, because an older Mac refuses a key that it does not know.
- `backend/coconut/notification_sources.py` reads GitHub and Gmail each minute. The first read of an account is the baseline and notifies nothing, and an item older than an hour never notifies. A new item that the settings allow notifies once: as a push, which follows the pause, `mobile_delay` and the work hours, and as a banner of the Mac. `GET /v1/notifications/alerts` gives the newest 50 alerts, and `business.updated` with the source `alerts` says that they changed; it makes no page of the apps read again. What the person reads on GitHub or in Gmail leaves the alerts, and so does an older item of a GitHub thread: its banner goes away, also a banner of an earlier run at the start of the Mac app, and its push no longer sends. The iPhone removes a push that it showed once no alert opens its issue, pull request or mail, as it removes the push of a read chat: when the app comes to the front, and at each change of the alerts while it shows. A tap opens the issue or pull request in the app, a mail as a page of its thread that only shows it (`?mailbox=personal`), and any other page of GitHub in the browser.

#### Mentions of a person

A person names another person of the team with `@` in the chat, as the person names an agent, or by typing the name alone, as one swipe of the keyboard types it. The app sends the member ID in `mentions`, and the Mac writes the entry ID of the `member` entry of that member. The log format and the relay do not change.

- A mention of a person starts no turn of an agent. Rule 4 of [An agent answers](#an-agent-answers) names `bot` entries, and the `member` entry of a Mac for a team only, which is no person.
- A mention of this Mac's person notifies also in a quiet team and in a muted chat (see rule 4 of [Notifications](#notifications)).
- `GET /v1/team/rooms` gives `mentioned` and `mention_first` for each room. `mentioned` stays true until the read position passes each unread message that names this Mac's person. An own message, a removed message and a message that does not show never count. One query walks the index of the messages back from the newest message to the read position. It reads at most the newest 1,000 messages after the read position, also own messages, so a mention under them does not show.
- The Mac app makes a banner for a quiet team or a muted chat when `mention_first` grows: a new mention arrived since the last list, also when a plain message came after it. A plain message makes a banner only when the switch of the team is on and its chat is not muted.
- The `@` list of the app has the agents that a teammate can ask, then each other person of the team, without the Macs for a team only. The app lists the people only for a room with `mentioned`, because an older service does not notify a named person in a quiet team. A typed `@` name that an agent and a person share gives the agent. A tap gives the row that the person taps.
- A name without `@` names a person of that list (`BotMentions.namedPeople`): the whole name as a word of its own, in any case and with or without accents, or its first word when no other person has that first word. The composer shows each named person under the field ("Mentioned"), and the cross of a chip keeps the text and only drops the mention until the draft is sent. An agent answers only to `@`, because the name of an agent, such as Gumroad, is an everyday word, so a name that an agent has, a name that two people share and a name of fewer than three letters need `@`. A link, a mail address or a longer word that holds a name names nobody. The bubble of the named person shows the name in bold, with its `@` or without it (`TeamMentionEmphasis`). An older app sees the mention (the notification and the mark of the row) but shows a name without `@` as plain text.
- `@everyone` names each other person of the team, as @channel of Slack does. The `@` list offers it after the people, when the room has `mentioned` and one to 16 other people. The app sends the member ID of each of them in `mentions`, and no agent, so each of them gets a mention by the rules above, and the Mac and an older app see the mentions of a message that names those people. The bubble of a named person shows `@everyone` in bold.

#### Names of chats

A person names a chat of the team with `#` and its name, as a channel of Slack. The `#` list of the composer has the Team chat, by the name of the team, and then the named chats, also in a direct chat and in an edit. A pick writes `#` and the name. The message keeps the plain text, so the log format, the Mac and the relay do not change, and an older app shows the text. The app shows the longest name of a chat of the team after `#`, in any case and as a word of its own, as a link (`TeamChatLinks`): `coconut://open/team/<room>`, with `?chat=<chat ID>` for a named chat, the address of the widgets. A tap opens the chat: the iPhone pushes it, and the Mac selects it in its sidebar. A name inside a link, and a text with a code mark, show as they are.

#### Agents of different Macs

Two agents of different Macs talk in the Team chat of a team that both are shared with, for example the Coconut agent of a person's Mac and the Gumroad agent of a Mac for a team only.

- An agent of a person's Mac writes with the tool `coconut_team_message` (agent, optional team, text, optional `reply_to`). The message carries the agent's `bot` entry and names the other agent in `mentions` (`TeamMessages.team_agent_message`). Its entry ID comes from the turn and the text, so a repeat writes nothing twice.
- An agent of a Mac for a team only names another member's agent with "@Name" in its reply, and the reply then has that agent in `mentions` (`_team_named_agents`).
- On a Mac for a team only, such a message starts a turn by the rules of a trigger below.
- On a person's Mac, a message of another member's agent that names an agent of this Mac, or answers its message, goes to `team_agent_mail` (`TeamStore._queue_agent_mail`). The sync then hands it to the agent's own chat as a new turn, quoted, with the team, the sender and the message ID to answer (`_team_deliver_mail`). The person wrote none of it, so it grants no approval.
- At most 4 messages of agents in a row start a turn, on each Mac, and the tool refuses a fifth (`MAX_AGENT_CHAIN`, `agent_chain`). A message of a person ends the run, so two agents never answer each other forever.

#### An agent answers

An agent of a team-only Mac answers in the team chat when a person names it, and in a direct chat when a person writes to it there (see [The Mac for a team only](#the-mac-for-a-team-only)). An agent of a full Mac never does: it is the agent of its person, and only that person can ask it. `backend/coconut/team_turns.py` holds these rules. Only the team-only service sets `TeamStore.answers`, so a full Mac queues no trigger and starts no turn.

Rules of a trigger. A `message` entry starts a turn of an agent of this Mac when each of these holds:

1. This Mac sees the entry for the first time, and its `rev` is 1. An edit starts no turn, and neither does a read again of the same entry, also after a restart.
2. Its member is another member than this Mac, and that member is in the member list. The signature proves the member. No other field of the payload decides. An entry with `bot`, a message of another Mac's agent, counts only while at most 4 messages of agents in a row end its chat (see [Agents of different Macs](#agents-of-different-macs)).
3. The `at` of the relay is under one hour old. The history that a Mac reads after a join starts nothing.
4. `mentions` has the entry ID of the `bot` entry of the agent, or `reply_to` names a message of that agent on this Mac. A Mac for a team only is no person, so a mention of it can only mean its agent: when `mentions` has the entry ID of the `member` entry of this Mac, the mention names the agent that answers in the room and has the name of the member. The Mac compares the names after Unicode normalization to NFC and without case. When no such agent exists, it names the only agent that answers in the room. With two or more answering agents and none with the name, it names no agent. With two or more answering agents that have the name, it names no agent too. A message that names the member and the agent starts one turn. A member row that a service of an older version kept, or an unsent message from before a correction, can still name the member.
5. The agent is shared with the room, is not archived, and its chat is connected: `chat` is true in its record (`coconut team connect-chat`).

The Mac keeps the triggers in `team_triggers`, in the order in which it accepted them, in the transaction of the accept. At most 8 triggers wait for one agent in one room, for the Team chat, the named chats and the direct chats together. The Mac skips a trigger over that bound, with a log line. One thread runs the triggers of one agent in one room one at a time. Before a turn, one transaction checks rules 2, 3 and 5 again, checks that the message still shows (a removal skips the trigger), and marks the trigger as started. When the turn ends, the row goes. So a trigger runs at most once: a row that is started at a start of the service is a turn that a stop cut off, and the agent posts a notice instead of a second run, which could repeat a tool action. An update drains the service: no new turn starts, and the running turn ends first.

The turn goes to the Hermes gateway of the agent through the gateway bridge (`team` of `GatewayBridge.run_turn`): the text, the member ID and the name of the sender, the team as a group chat with the chat ID `<agent ID>:team:<room ID>` (`<agent ID>:team:<room ID>:<chat entry ID>` in a named chat, see [Chats of a team](#chats-of-a-team)), the rules of the chat, and as quoted context at most 20 messages of the same chat since the last message of the agent, each with the name of its writer. The context is data, not instructions. A turn has no task, no card and no row in `tasks`, `messages` or `decisions`. It stops after 30 minutes.

Rules of a reply:

1. Each complete segment of the reply and each commentary of Hermes is one `message` entry of this Mac with `bot`: the entry ID of the `bot` entry of the agent. No message shows before it is complete. While the turn runs, the team sees the agent as typing (see [Typing](#typing)).
2. The first message of a turn has `reply_to`: the trigger. A text over 16,000 code points goes into more than one message.
3. The entry ID comes from the room, the trigger, the agent and the number of the message in the turn. So a write that runs again adds no message.
4. An approval of a command is denied, with a reason that the agent can repeat: nobody can approve in the team chat. A clarify question goes into the chat as a message of the agent, with its choices as a list, and Hermes gets the answer to end the turn. The reply of a person starts the next turn.
5. A send of Hermes outside a turn in the chat of the agent, for example after a background process or a cron delivery, is a message of the agent without `reply_to`. See [Cron deliveries into a team chat](#cron-deliveries-into-a-team-chat).
6. The agent writes a message of a turn, and a notice, only while the agent answers in the room and the request stands: the trigger still shows, and its member is in the member list. A `delete` over the trigger, a member that leaves or is removed, an agent whose chat goes off or that leaves the team, and a leave of this Mac stop the running turn, also while it waits for the gateway. The last check comes directly before the bridge registers the turn, and a stop waits for that registration, so no stop gets lost. A send outside a turn answers no message, so for it only the agent must still answer.

What the chat shows when a turn fails:

| Case | The chat shows |
| --- | --- |
| No adapter of the gateway of the agent takes the turn within 2 minutes of its start, or the profile is parked. The bridge counts a gateway as there for 60 seconds after its last poll, so a gateway that just stopped looks there; the turn still waits until the 2 minutes end, and a gateway that restarts in that time takes it. A turn counts as taken only when the adapter that got it shows it: an event of the turn, or its next poll that lists it in `active`. A gateway that stops can still hold a poll for some seconds and take a turn that it drops. A poll of the same home that starts 5 seconds after the hand-out and does not list the turn takes it again, and a turn that no adapter shows for 30 seconds counts as not taken. A living adapter shows a turn at once, and the adapter runs a turn once also if it gets it again | One message of the agent: "Hermes on this Mac does not answer now, so I cannot answer. Write again later." |
| The adapter of the gateway of the agent polls, but its Hermes lacks a field that the team chat needs (`lacking` in each poll, see [The plugin mode `team`](architecture.md#the-plugin-mode-team)). The bridge gives that adapter no turn, and the service writes one log line for each turn | At once, one message of the agent: "Hermes on this Mac needs an update, so I cannot answer. Ask the person who runs this Mac to update it." |
| The gateway stops answering in a turn, the turn fails, it runs over 30 minutes, it ends without a word, Coconut has a fault in the turn, or the trigger waited in the queue over one hour | The complete messages, then one message: "I could not finish my answer. Write again if you still need it." |
| The service stops in a turn. Hermes ends the turn too: a clean stop stops each running turn while the listener still answers, and waits up to 10 seconds until the adapters collected the stops. After a crash, the adapter of the team mode stops a turn when a new service does not know it (`unknown` in the answer of a poll), or when it cannot reach the service for 30 seconds. An update drains the service first, so no turn runs while it restarts. A late reply of a stopped turn, and the answer of Hermes to its `/stop`, stay in the log of Hermes | The same message at the next start. The trigger does not run again. |
| The chat of the agent goes off, the agent leaves the team, this Mac leaves, the member leaves, or the message is removed | Nothing. The turn stops or does not start. |
| The relay refuses the write of a reply | Nothing. The relay client tries three times, then the service writes a log line. |

In a direct chat each row holds too: each message of the table is a direct message of the agent in that chat, without `reply_to`.

#### Typing

The Team chat shows who types: a person while that person changes the draft, and an agent while its turn runs. Typing is a small entry of the log that its writer replaces. So the relay and the helper need no change, and each replacement reaches each other Mac through the socket in about one second. `backend/coconut/team_typing.py` holds these rules.

The payload of a `typing` entry: `{"kind": "typing", "rev": n, "at": "...", "on": true, "bot": "<entry ID>"}`. The entry is not kept.

- A Mac keeps one `typing` entry for its person in each room, and one for each of its agents in each room, and replaces it with the next `rev`. So the relay holds one small entry for each subject, not one for each key press. The entry ID is the first 16 bytes of HMAC-SHA256 with the member token of the Mac as the key, over `"coconut room typing" | room ID (32 bytes)`, and for an agent also the 16 bytes of the entry ID of its `bot` entry. So the ID stays the same after a restart, and no other member can take it first.
- `on` is true or false. `bot` is the entry ID of a `bot` entry of the same member, or absent for the person of the Mac. A reader takes null as an absent field. An entry that does not fit shows nothing.

Rules for a Mac that reads a `typing` entry:

1. Only an entry of another member counts. The own person and the own agents of a Mac never show on that Mac.
2. `on` true with an `at` of the relay under 60 seconds from the clock of the Mac shows the subject as typing from the time of the read: a person for 10 seconds, an agent for 30 seconds. A later entry with `on` true starts the time again. An older entry changes nothing, so a first read of the log, or a read after a long stop, shows nothing.
3. `on` false ends it. A `message` entry of the same person ends the typing of that person. A message of an agent does not end the typing of the agent: an agent sends more than one message in a turn, and its Mac writes `on` false at the end of the turn.
4. A subject shows only while its member is in the member list, and an agent only while its `bot` entry is valid.
5. The state lives in memory only. A restart of the service forgets it.
6. A Mac with an older Coconut does not know the kind and shows nothing (rule 5 of a reader).

A person's Mac writes the entry of its person:

- `POST /v1/team/rooms/<room>/typing` writes or replaces the entry with `on` true, and answers `{room}`. Only a paired device calls it: `tool_route` refuses the token of the bridge file, and a Mac for a team only answers 409.
- The Mac writes at most one such entry in 4 seconds for a room. A call inside that time answers at once and writes nothing. A write that fails holds back no later call.
- A failed write answers as a failed send of a message does, and the app ignores it.
- The route keeps no answer for its `Idempotency-Key`, because a repeat writes nothing new and its many calls would push the other answers of [Repeat a write](#repeat-a-write) out of the memory.
- The app calls the route while the person changes the draft of the Team chat and the draft is not empty, at most one time in 5 seconds. It sends no call for an edit of a sent message, and none in a direct chat.

A Mac for a team only writes the entry of an agent while a turn of the agent runs (see [An agent answers](#an-agent-answers)):

- When the adapter of the gateway shows the turn, the Mac writes the entry with `on` true. While the turn runs, it writes it again every 20 seconds. At each end of the turn (an answer, a notice, a stop, a fault), it writes `on` false.
- The writes run on a thread of their own, so a failed write never fails or delays the turn. A failed write gives one log line.
- A turn that gets a notice at once (no adapter, an old Hermes) writes no entry.

What the phone gets:

- The room view of `GET /v1/team/rooms` has `typing`: a list, empty when nobody types, of `{member, name, bot, bot_name, seconds}` for each other subject that types now, the earliest start first. `bot` has the form of the agent ID of a message (`t-<room ID>-<entry ID>`), and null for a person. `seconds` is the time that is left, as a whole number. A room that is not `active` has an empty list.
- Each change of the list (a subject starts, types on, or ends by an entry or by a message) adds one `team_typing.updated` event with `{"team": <room ID>}`. `GET /v1/updates` then has `team_typing_changed`: the IDs of these rooms. The end by time needs no event, because the app has `seconds`. The type is in the `known` set, so an app of today does not read its whole state again.
- The app shows one row under the newest message, above the composer: the small face of each subject, the text, and three dots. The texts are "Grace is typing", "Grace and Lin are typing", and for three or more "3 people are typing". An agent counts as one subject with its name. The row of the team in the Chats list and in the Mac sidebar shows the same text in place of the newest message.

No log line has a name. A `typing` entry is sealed only with the room key, so it would tell each member that two people talk. So a direct chat has no `typing` entry. In a direct chat of a person with an agent, the Mac for a team only writes the typing sign of the agent as a `direct` entry with a box that only the person's Mac opens (rule 12 of [The Mac for a team only](#the-mac-for-a-team-only)). The typing of a person in a direct chat is not sent.

#### Notices

A notice is what the whole team must know and what stays true for a while: a freeze, an outage, a rule, a deadline. It is not a message: it stays on the board of the team until it ends or its author closes it, and each member sees who has seen it. `backend/coconut/team_notices.py` serves the routes.

The payload of a `notice` entry: `{"kind": "notice", "rev": n, "at": "...", "type": "status", "text": "...", "posted_at": "...", "ends_at": "...", "closed": true}`. The entry is kept. Its ID is 16 random bytes, or for a request with an `Idempotency-Key` it comes from the room, the device and the key, as for a chat.

- Each member writes notices. Only the author changes one: rule 3 of a reader refuses another member's write. A removal is a `delete` entry over it.
- `type` is `announcement`, `status`, `rule` or `deadline`. `text` has 1 to 500 code points of valid Unicode text. `posted_at` is the UTC time of the first write and stays the same in each later write. `ends_at` is an optional UTC time. `closed` is true or absent. A reader takes null as an absent field. An entry that does not fit stays in the store and shows nothing.
- A notice is active while it is not closed and its `ends_at`, if any, is still ahead.
- At most 50 active notices in a room. The Mac of the writer refuses the 51st.

The payload of a `seen` entry: `{"kind": "seen", "rev": n, "at": "...", "notices": ["<entry ID>", ...]}`. The entry is kept: one for each member and room, with at most 200 entry IDs, the newest notices first. The entry ID is the first 16 bytes of HMAC-SHA256 with the member token as the key, over `"coconut room seen" | room ID (32 bytes)`, so another member cannot take it first. Only the newest write counts, also one that does not fit.

Both kinds are written again after a key change (see [The kept entries again](#the-kept-entries-again)). A Mac with an older Coconut keeps both by rule 5 of a reader and shows nothing.

The room view of `GET /v1/team/rooms` has `notices`: the active ones first, the newest first in each group, each with `id`, `type`, `text`, `posted_at`, `updated_at`, `ends_at`, `closed`, `active`, `author`, `author_name`, `own`, `seen` (the person of this Mac has seen it, or wrote it) and `seen_by` (the names of the other members that have seen it). Only a paired device writes:

| Request | Answer |
| --- | --- |
| `POST /v1/team/rooms/<room>/notices`, body `{type, text, ends_at?}` | Posts a notice, and answers `{room, notice}`. The author has seen it. |
| `PATCH /v1/team/rooms/<room>/notices/<id>`, body `{text?, ends_at?, closed?}` | Changes or closes an own notice. Another member's notice answers 403. |
| `DELETE /v1/team/rooms/<room>/notices/<id>` | Removes an own notice. A repeat answers the same and writes nothing. |
| `POST /v1/team/rooms/<room>/notices/seen`, body `{notices}` | Adds the notices to the own `seen` entry. Notices that it names already write nothing. |

The token of the bridge file gets 403 for each write, so an agent cannot post a notice on its own. Each turn of an agent gets the active notices of the teams of its Mac as data: a turn in a team chat in the rules of its chat, and a turn of a person's agent as one record whose ID changes with the text.

A new notice of another member notifies each paired phone of a person's Mac, under the rules of a message
(`TRIGGER_SECONDS`, and nothing from the history that a Mac reads after a join), also in a quiet team: a notice is what
the whole team must know. The Mac sends it only while the notice is active and the person has not seen it. Its payload
has `kind` `notice`, the room in `team` and the entry ID in `notice`, and a tap opens the notice board of the team.

#### Usage

Each person of a team sees what each other person's agents cost, and how many tokens they use: the leaderboard of the Usage page, which ranks by cost. A person's Mac writes one `usage` entry in each team with the numbers of its agents by day. `backend/coconut/team_usage.py` writes and reads it.

The payload of a `usage` entry: `{"kind": "usage", "rev": n, "at": "...", "days": [{"day": "2026-11-22", "turns": 41, "input_tokens": 52000, "output_tokens": 79000, "cache_read_tokens": 4100000, "cache_write_tokens": 210000, "cost_usd": 17.22}, ...], "cost_known": true}`. The entry is kept: one for each person's Mac and room. The entry ID is the first 16 bytes of HMAC-SHA256 with the member token as the key, over `"coconut room usage" | room ID (32 bytes)`, as for a `seen` entry.

- `days` has at most 90 days, each date (`YYYY-MM-DD`, in the time of the writer's Mac) once. A day can have `turns`, `model_calls`, `tool_calls`, `working_seconds`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` and `reasoning_tokens`, each a whole number from 0 to 2^53 - 1, and `cost_usd`, a number from 0 to 10^9. An absent number is 0. A reader leaves other fields out.
- `cost_known` is true when Hermes knows a price for a model of the writer's agents. Absent is false.
- The numbers are those of the Usage page of the writer: the sum of its agents (`usage.py`). The entry has no chat, no project, no model and no text.
- The writer sends the last 84 days, the longest range of the page, and only the days with work, each with its numbers that are not 0. It checks the entry at most every 30 minutes, the first time a minute after its start, and writes only when the days changed.
- A reader takes the newest `usage` entry of each member that fits. An entry that does not fit shows no usage for its member.
- A Mac for a team only writes none: it opens no Hermes store, and no person is at it.

`GET /v1/usage` has `teams`: each active team with its `people` in the order of the Team screen, a Mac for a team only left out. Each person has `member`, `name`, `avatar`, `color`, `this_mac`, `days` (the days of the report, each day with each number, or null before the Mac of that person writes an entry), `cost_known` and `updated_at` (the time of the relay of the newest entry, or null for this Mac). The person of this Mac has the numbers of the report itself, so they are never older than its entry. A `usage` entry adds no event and notifies no phone: the page reads the store each time it opens.

#### Chats of a team

A team has the Team chat, and each member can make more chats with a name, for example "Design" and "Launch". Each member of the team reads each chat. A chat is one kept entry of the member who made it, and a message names its chat. So the relay and the helper need no change.

The payload of a chat of the admin: `{"kind": "chat", "rev": n, "at": "...", "name": "Design"}`. A chat of another member has the kind `member_chat` and the same fields: `{"kind": "member_chat", "rev": n, "at": "...", "name": "Design"}`. The entry is kept. The ID of a chat is its entry ID.

- The Mac of the member who makes the chat writes the entry: a `chat` entry on the admin's Mac, and a `member_chat` entry on each other Mac. Rule 4 of a reader accepts a `chat` entry only from the admin. So a chat of another member has its own kind: a Mac of an older version refuses a `chat` entry of another member and keeps no copy of it, but it keeps a `member_chat` entry by rule 5 of a reader, and shows the chat after its update (rule 6 below). The entry ID is 16 random bytes, or for a request with an `Idempotency-Key` it comes from the room, the device and the key, as for a message.
- A rename is a new write of the same entry with the next `rev` and the same kind. A removal is a `delete` entry over it. Only the member who made a chat renames or removes it, because only the member of an entry replaces it (rule 3 of a reader). So the admin cannot remove the chat of another member, except by the removal of that member.
- The Mac of the member writes a `name` by the strict rule of a member name, with 1 to 50 code points: no space at its start or end, no line break, no control character. A reader takes each `name` of 1 to 50 code points of valid Unicode text.
- The Mac of the member writes the entry again after a key change, as each other kept entry (see [The kept entries again](#the-kept-entries-again)).
- `message` and `typing` have the field `chat`: the entry ID of a `chat` or `member_chat` entry, or absent for the Team chat. A reader takes null as an absent field.
- At most 20 chats in a room, of all its members. The Mac of a member refuses the 21st chat that it shows. Two Macs that make a chat at the same time can pass 20, and a reader shows each chat. The relay keeps 512 kept entries of one member, so the chats fit.

Rules for a Mac that reads a `chat` or `member_chat` entry:

1. Rule 4 of a reader accepts a `chat` entry only from the admin. A `member_chat` entry comes from each member.
2. The entry is valid when `name` has 1 to 50 code points of valid Unicode text. An entry that does not fit stays in the store and shows nothing.
3. A valid entry shows a chat with that name. Its `created_at` is the `at` of the first write that this Mac saw. The chats of a room sort by `first`, so a rename keeps the place of a chat.
4. A removed chat, by a `delete` entry or by a new write that does not fit, goes on this Mac: the chat leaves the list, its rows of `team_messages`, its read position and the choice of the person to mute it go, its waiting notifications send nothing, its waiting triggers go, and a running turn of an agent in it stops. A message for it that comes later shows nothing.
5. A member that leaves the member list, by a `delete` over its `member` entry or by a `remove` entry (see [Removal](#removal)), takes its chats with it: each chat of its `member_chat` entries goes by rule 4. The relay deleted these entries, so no `delete` entry comes for them, and a Mac that joins later never sees these chats. A `member_chat` entry of a removed member shows nothing. The admin leaves only a room without another member, so the chats of the admin stay.
6. A Mac with an older Coconut does not know the kind. It keeps the entry by rule 5 of a reader and shows nothing. A Mac of the version with `chat` and without `member_chat` also shows none of the messages of such a chat, by rule 2 of a message below. Its update shows the chats (see the pass below).

Rules for a message in a named chat:

1. A message is valid only when `chat` is an entry ID or absent. A message that does not fit stays in the store and shows nothing.
2. A message with a `chat` shows only when `chat` names a chat that shows by the rules above: a valid `chat` entry of the admin, or a valid `member_chat` entry. The `chat` entry can come after the message, so the check runs when the Mac reads the chat, as for `bot`.
3. `reply_to` shows only when the answered message is in the same chat. A reply across chats has no quote, and it starts no turn of the agent of the answered message.
4. `erase` does not change: it names a message, in each chat.
5. A Mac with an older Coconut ignores the field `chat` (rule 2 of the readers of a message). So it shows a message of a named chat in its Team chat, and its agent answers there. Nothing gets lost. The chats separate again when that Mac gets the update (see the pass below).
6. A chat entry can come after the messages of its chat, for example after a rename, because the relay keeps only the newest write of the entry. When a chat starts to show, the Mac notes its messages for a notification by the rules of [Notifications](#notifications): a first write, under one hour old, after the read position. Such a message did not show before, and the item of a phone has the entry ID in its key, so no message notifies two times.

What each Mac keeps for each chat on its own, the Team chat and each named chat:

- The messages: `team_messages` has the `chat` of each message, and null for the Team chat. The index on the room, the chat and `first` gives a page, the unread count and the newest message of one chat without a scan of the room.
- The read position: `team_reads` for the Team chat, as before, and `team_chat_reads` for a named chat. The position of a named chat starts at the later of the `first` of the `member` entry of this Mac and the `first` of the `chat` entry. So the history from before a join, and the messages that came before the chat showed, do not count as unread. It moves only forward, and never past the newest message of that chat.
- The update from a version without named chats: in one step with the new column `chat` of `team_messages`, the Mac reads each kept `chat` entry of the admin, moves each message to its chat with the reader of a message, and gives each chat the read position of the Team chat, but never past the newest message of the chat, because the person read these messages in the Team chat of that version. A stop or an error in this pass takes the new column back, so the next start does the pass again. After the pass, a start does nothing more.
- The update from a version without `member_chat`: in one step with the mark `member_chats` in `team_marks`, the Mac reads each `member_chat` entry that it kept by rule 5 of a reader. Each one of a member in the member list shows its chat with the messages of the chat that the store has. The read position starts as for a new chat, and no message notifies. A stop or an error takes the step back, so the next start does the pass again. After the pass, a start does nothing more.
- The unread count, `mentioned` and `mention_first` follow the rules of the Team chat, with the messages and the read position of the chat.
- Typing: a Mac keeps one `typing` entry for each subject in a room, as before, so a person types in one chat at a time. The `chat` of the entry says where. A person's Mac writes at most one entry in 4 seconds for each room and chat.

The routes. Each one has the authorization of the other team routes, and a write with an `Idempotency-Key` is safe to repeat (see [Repeat a write](#repeat-a-write)). Only a paired device makes, renames and deletes a chat: the token of the bridge file gets 403. The Mac of each member makes a chat. Only the Mac of the member who made a chat renames and deletes it: another Mac answers 403 with "Only the member who made this chat can change it." A room that is not `active` answers 409.

| Request | Answer |
| --- | --- |
| `POST /v1/team/rooms/<room>/chats`, body `{name}` | Makes a chat, and answers `{room, chat}`: the room view and the ID of the chat. A name that another chat of the room has, compared with `casefold`, answers 409 with "This team already has a chat with this name." The 21st chat answers 409 with "A team can have at most 20 chats." |
| `PATCH /v1/team/rooms/<room>/chats/<id>`, body `{name}` | Renames the chat, and answers `{room, chat}`. The same name writes nothing. The rules of a name are those of a new chat. |
| `DELETE /v1/team/rooms/<room>/chats/<id>` | Removes the chat with a `delete` entry, and answers `{room, chat}`. A chat that is already removed gets the same answer and writes nothing. |
| `GET .../messages?chat=<id>`, `POST .../messages` with `chat`, `POST .../read` with `chat`, `POST .../typing` with `chat` | The routes of the Team chat for a named chat. Without `chat`, each route means the Team chat, as before. An unknown or removed chat answers 404 with "This chat is no longer in the team." `PATCH` and `DELETE` of a message need no chat, because the message has one. |

The room view of `GET /v1/team/rooms`:

- `chats`: one item for each named chat, in the order of `first`: `id`, `name`, `created_at`, `mine` (true when the member of this Mac made the chat), and the fields that the room has for the Team chat: `unread`, `last_message`, `mentioned`, `mention_first` (only when `mentioned` is true) and `typing`. On a person's Mac each item also has `notifications`: false when the person muted the chat (rule 4 of [Notifications](#notifications)). A Mac for a team only and an older service send none, and the app then shows no action "Mute".
- `can_make_chats`: true on each Mac of an `active` room. The app shows "New chat" only with this field.
- The app shows the actions of a chat, rename and delete, only when its `mine` is true.
- `can_manage_chats`: true on the admin's Mac of an `active` room, for an older app. Such an app shows "New chat" and the actions of each chat only with this field, so on the device of another member it makes no chat, and on the device of the admin the actions on the chat of another member answer 403. The app reads it only from an older service, which sends no `can_make_chats` and no `mine`.
- The fields of the room itself stay the fields of the Team chat, so an older app shows the Team chat as before and no named chat.

Events: a new, renamed or removed chat adds `team.updated`. A change inside a named chat adds `team_messages.updated` and `team_typing.updated` with `{"team": <room ID>}`, as for the Team chat.

Notifications follow the switch `notifications` of the room, and each member can also mute one named chat (rule 4 of [Notifications](#notifications)). The app shows "Mute" or "Unmute" for each member in the menu of the row of a named chat: on the Team screen, in the Chats list and in the Mac sidebar. A swipe of the row has no such action. A muted chat has the symbol `bell.slash` at the end of its row. A message in a named chat changes three parts of [Notifications](#notifications):

1. The subtitle of the sealed alert is the name of the chat and the name of the team: "Design · Example Team".
2. `thread-id` is the room ID, a colon, and the ID of the chat. So the alerts of one chat group together.
3. The `coconut` part has `"chat": <chat ID>`, as one message and as a group of the messages of that chat. A tap opens that chat. An older app reads `team` and opens the Team chat. A group of messages of more than one chat opens Inbox.

An agent of a Mac for a team only answers in the chat where a person names it (see [An agent answers](#an-agent-answers)):

- The trigger keeps the chat of its message. The quoted context has only messages of that chat. Each message of the reply, each notice and the `typing` entry of the agent carry the same `chat`.
- Each chat is its own session in Hermes: the chat ID is `<agent ID>:team:<room ID>` for the Team chat and `<agent ID>:team:<room ID>:<chat entry ID>` for a named chat. The name of the group chat for Hermes is "<team> · <chat>".
- The bound of 8 waiting triggers for one agent in one room stays, and one thread still runs the triggers of one agent in one room one at a time.

No log line has the name of a chat.

#### Starred chats

A person stars the Team chat, a named chat, or a direct chat with a person or an agent, as the person stars an agent.

- The choice stays on this Mac: in `team_chat_pins` for the chats of the team, and in `team_direct_pins` for a direct chat. It never goes into the log or to the relay.
- The Mac deletes the choice of a named chat when it removes the chat, and every choice of a room when it leaves the team. The choice of a direct chat stays, so a chat that the person can no longer write to keeps its place.
- `GET /v1/team/rooms` gives `pinned` for the Team chat and each named chat on a person's Mac, and for each item of `directs` and `bot_directs`.
- A Mac for a team only and an older service send no `pinned` field. The app then shows no action.
- `PUT /v1/team/rooms/<room>/pin` changes the Team chat choice with `{"pinned": true}` or `{"pinned": false}`.
- `PUT /v1/team/rooms/<room>/chats/<id>/pin` changes a named chat choice with the same body.
- `PUT /v1/team/rooms/<room>/directs/<chat>/pin` changes a direct chat choice with the same body, and answers `{room, member, pinned}` for a chat with a person or `{room, bot, pinned}` for a chat with an agent.
- Only a paired device can change the choice. A Mac for a team only returns 409. An unknown chat returns 404.
- A change adds `team.updated`. The same value again writes nothing. These routes keep no answer for an `Idempotency-Key`.
- The menu shows "Star" or "Unstar" with `star` or `star.slash`, above "Mute" for a named chat.
- "Star" shows only for an active team. "Unstar" shows in each state, so the person can remove a star without deleting history.
- The row shows the new choice at once. A failed request restores the old state and shows the error.
- VoiceOver says "Starred" for a starred chat.
- Starred chats keep one order: teams in the Mac order, then the Team chat before each named chat in its order, then the direct chats with a person and with an agent by their ID, so a message does not move a starred chat.
- On iPhone, starred chats show below the starred agent tiles, with a `star` mark, before the other rows.
- A starred chat shows where its row shows: in "All" and "People" for a chat with a person, and in "All" and "Agents" for a chat with an agent. The Chats list of the app shows every chat, with no filter.
- On Mac, "Starred" shows starred agents, starred chats, then starred pages. An unread message does not move a starred chat.

#### Reactions

A person reacts to a message with an emoji, as in WhatsApp: in the Team chat, in a named chat, in a direct chat of two people and in a direct chat with an agent. Each person has one reaction on a message. A new choice replaces the old one. `backend/coconut/team_reactions.py` serves the routes, and `TeamStore` keeps the reactions.

The emoji are exactly these six texts, in this order: 👍 (U+1F44D), ❤️ (U+2764 U+FE0F), 😂 (U+1F602), 😮 (U+1F62E), 😢 (U+1F622) and 🙏 (U+1F64F). An empty text takes the reaction back. The heart has the variation selector 16, and a heart without it is no reaction.

The payload of a `reaction` entry: `{"kind": "reaction", "rev": n, "at": "...", "message": "<entry ID>", "emoji": "👍"}`. The entry is not kept, as a message.

- A person's Mac writes one entry for each message that its person reacts to, and replaces it with the next `rev` for a new emoji and for an empty one. The entry ID is the first 16 bytes of HMAC-SHA256 with the member token of the Mac as the key, over `"coconut room reaction" | room ID (32 bytes) | entry ID of the message (16 bytes)`. So a change and a repeat write the same entry, and no other member can take the ID first (rule 3 of a reader).
- In a direct chat, the reaction is a `direct` entry with a box, as a direct message, so the other members do not learn who talks with whom. The plaintext of the box: `{"to": "<member ID>", "message": "<entry ID>", "emoji": "👍"}`, and `bot` (the entry ID of the `bot` entry) in a chat with an agent. It has no `text`. The entry ID uses the label `"coconut room direct reaction"` in the place of `"coconut room reaction"`.
- A Mac for a team only writes no reaction: it has no person, and its agents never react.

Rules for a Mac that reads a reaction:

1. A `reaction` entry is valid when `message` is an entry ID and `emoji` is one of the six emoji or empty. A reader takes null as an absent field. An entry that does not fit stays in the store and says no reaction: the earlier write of the entry no longer counts. So an emoji of a later version shows nothing on a Mac of today.
2. A box of a `direct` entry is a reaction when it has no `text` and no `on`, its `to` is the member that the reader expects (as for a direct message), its `message` and `emoji` fit rule 1, and its `bot` is an entry ID or absent. Only a person's Mac reads it. The typing sign of an agent (rule 12 of [The Mac for a team only](#the-mac-for-a-team-only)) has no `message` and no `emoji`, so neither reader takes the box of the other, and a box with the fields of both is neither. The box opens, waits and gets its try at the end of the read as the box of a direct message does.
3. Only the newest write of each entry counts, as for each other kind. A `delete` entry over the entry, a write of another kind, a box that does not open for this Mac, and a write that does not fit say no reaction. A member has one reaction on a message in a chat: of the entries of that member that name the message in that chat (the same `peer` and `bot`), the one with the highest `seq` counts, also when its emoji is empty. An empty emoji shows no reaction. So the reactions are a function of the newest write of each entry, and each Mac shows the same reactions for the same log, in whatever order it applies the entries: in the order of the log, only the newest writes, or an entry that waits for its key and comes at the end of the read. An honest Mac writes one entry for each message.
4. A reaction of a `reaction` entry shows only on a message of a team chat. A reaction of a box shows only on a message of the direct chat of the box: the chat of the writer and this Mac, and with `bot` the chat with that agent. So a member cannot put a reaction into a chat that it is not part of. The check runs when the Mac reads the chat, because a reaction can come before its message.
5. A message that does not show has no reaction that shows: a message that its member deleted, that the admin erased, or whose chat its member removed. A reaction of a member that left stays, as its messages stay.
6. A reaction changes no unread count and no read position, notifies no phone, starts no turn of an agent, and goes into no text for a model. A Mac for a team only opens the box of each `direct` entry for the chats of its agents, but it keeps nothing of the box of a reaction: no row in `team_reactions` and no row in `team_directs`. So a reaction of a person to an agent starts no turn, also as the first entry of that person to the agent and as a new write of an entry, and goes into no context, no page context and no notice. The agent writes no answer and no typing sign for it. A `reaction` entry of a team chat goes into `team_reactions` on each Mac, also on a Mac for a team only, and no turn and no context reads that table.
7. A Mac with an older Coconut keeps a `reaction` entry by rule 5 of a reader and shows nothing. It finds no `text` in the box of a reaction, so it shows no direct message.

The update from a version without reactions: in one step with the new table `team_reactions` and its index, the Mac reads each stored `reaction` entry with the reader above. Each statement of this pass finds its rows by the primary key or by the index. A stop or an error in this pass takes the table back, so the next start does the pass again. A reaction in a box that the older version stored does not show, because only the helper opens a box. It shows when its member changes it.

The routes. Each one has the authorization of the other team routes, and a write with an `Idempotency-Key` is safe to repeat (see [Repeat a write](#repeat-a-write)). The token of the bridge file gets 403 on a person's Mac, and a Mac for a team only answers 409 with "This Mac runs Coconut for a team only, so nobody reacts here."

| Request | Answer |
| --- | --- |
| `PUT /v1/team/rooms/<room>/messages/<id>/reaction`, body `{"emoji": "👍"}` | Sets the reaction of this Mac's person on a message of a team chat, and answers `{room, message}`: the message with its new `reactions`. |
| `PUT /v1/team/rooms/<room>/directs/<member or team agent ID>/messages/<id>/reaction`, body `{"emoji": "👍"}` | The same in a direct chat. The answer is `{room, member, message}`, or `{room, bot, message}` in a chat with an agent. |

- The route sets a value, so a repeat writes nothing: the same emoji, or an empty emoji when the person has no reaction. The app takes a reaction back when the person picks the same emoji again: it sends an empty `emoji`.
- 400 with "Choose one of these reactions: 👍 ❤️ 😂 😮 😢 🙏." for another value of `emoji`, also for a missing one.
- 404 with "This message is no longer in the chat." for a message that the chat of the route does not show: unknown, deleted, erased, in a removed chat, or in another chat.
- 409 for a room that is not `active`, as for each write of the chat. In a direct chat, a change that needs a write follows [Who can get a direct message](#who-can-get-a-direct-message) and [Who can write to an agent](#who-can-write-to-an-agent), with their sentences.

A message in an answer has `reactions`: a list, empty when nobody reacted, with one item for each emoji with at least one member, in the order of the six emoji. `members` are the member IDs, in the order of the log. `mine` is true when the member of this Mac is one of them. For example:

```json
{"room": "<room ID>", "message": {"id": "<entry ID>", "first": 42, "member": "<member ID of Ada>", "member_name": "Ada",
  "this_mac": false, "bot": null, "bot_name": null, "text": "Lunch at noon?", "reply_to": null, "reply": null,
  "chat": null, "mentions": [],
  "reactions": [{"emoji": "👍", "members": ["<member ID of Grace>"], "mine": true},
                {"emoji": "❤️", "members": ["<member ID of Lin>", "<member ID of Ada>"], "mine": false}],
  "created_at": "2026-11-14T12:00:00.000000Z", "edited_at": null}}
```

Events: a change of the reactions that a message of the store shows adds the event of a change of its chat, in the same transaction: `team_messages.updated` with `{"team": <room ID>}` for a team chat, `team_directs.updated` for a direct chat of two people, and `team_bot_directs.updated` for a direct chat with an agent. So an app of today reads the newest page again, and `GET /v1/updates` marks the chat as for an edit. No new event type exists. An older app ignores the field `reactions`.

No log line has an emoji or a name.

#### Link previews

A message of a person can carry one link preview of the first link of its text, as in WhatsApp: in the Team chat, in a named chat, in a direct chat of two people and in a direct chat with an agent. The app of the sender loads the page while the person writes, and sends the preview with the message when it is ready. A send never waits for a preview. A reader shows the preview from the message and loads nothing from the network for it, so the site of a link does not learn the address and the time of each reader.

The preview is `{"url": "https://example.com/a", "title": "Example page", "site": "example.com"}`: the field `preview` of the payload of a `message` entry, or of the box of a `direct` entry. A message without a preview has no field. It has no picture.

Rules of a preview that fits:

1. It is an object with exactly the keys `url`, `title` and `site`.
2. Each value is text of at most its limit as sent: 2,000 code points for `url`, 200 for `title` and 100 for `site`. Spaces around a value count toward the limit.
3. Each value is valid Unicode text without a line break (each break of `str.splitlines`).
4. `url` is not empty, starts with `http://` or `https://`, and `text` of the message contains it exactly.
5. `title` and `site` are not empty after a trim. The Mac writes and shows them trimmed.

A reader checks the count of the keys first, then the type and the length of each value, and only then reads a value. So a hostile preview of up to 1 MiB costs no work that grows with its size. The search of `url` in `text` reads at most 2,000 code points of `url` in at most 16,000 code points of `text`.

- A Mac writes only a preview that fits. A send route answers 400 with "The link preview does not fit the rules of a preview, so send the message without it." for each other value, and writes nothing. The app sends only a preview that fits, so a person does not see this. Null is no preview.
- A reader checks the same rules when it reads an entry or a box. A preview that does not fit is left out, and the message shows without it. A preview never makes a message invalid. The payload keeps the preview as its member wrote it.
- An edit (`PATCH`) takes no preview. The Mac keeps the preview of the message while the new text contains its `url` exactly, else the preview goes from the new write. A preview that went does not come back.
- `team_messages` and `team_directs` keep the preview that fits in the column `preview`, as JSON, or null. A store of an older version gets the column in `migrate`. The update reads no stored entry again: no version before the column writes a preview. A message with a preview that this Mac stored with an older version shows without it until an edit. A box of a direct message needs the helper, so a pass could not read it again anyway.
- Each message view of a team chat and of a direct chat has the key `preview`: the object, or null. An older service sends no key, and the app then knows that this Mac has no previews.
- A preview changes no unread count, no read position, no notification and no event: the alert has the text of the message only.
- A message of an agent has no preview: a Mac for a team only has no app that makes one. A reader still reads a preview in such a message by the rules above.
- No text for a model has a preview: the trigger text, the context of the chat, the page context and the quote of a reply have the text of each message only. The link is in the text. A Mac for a team only keeps the preview of a direct message of a person in `team_directs`, and no turn reads the column.
- An older Mac keeps the field by rule 2 of a `message` entry, and its reader of a box ignores a field that it does not know. So it shows the message without the preview. An older app ignores the key.

A message in an answer with a preview, and one without:

```json
{"room": "<room ID>", "message": {"id": "<entry ID>", "first": 43, "member": "<member ID of Ada>", "member_name": "Ada",
  "this_mac": true, "bot": null, "bot_name": null, "text": "Look at https://example.com/a", "reply_to": null,
  "reply": null, "chat": null, "mentions": [], "reactions": [],
  "preview": {"url": "https://example.com/a", "title": "Example page", "site": "example.com"},
  "created_at": "2026-11-15T12:00:00.000000Z", "edited_at": null}}
{"room": "<room ID>", "message": {"id": "<entry ID>", "first": 44, "member": "<member ID of Ada>", "member_name": "Ada",
  "this_mac": true, "bot": null, "bot_name": null, "text": "Lunch at noon?", "reply_to": null, "reply": null,
  "chat": null, "mentions": [], "reactions": [], "preview": null,
  "created_at": "2026-11-15T12:01:00.000000Z", "edited_at": null}}
```

No log line has a preview.

#### Files

An agent can send a file to the Team chat or a named chat through its Hermes gateway.
The team-only service checks the agent's home and sends the bytes through the same file transport as a person's Mac.
The same size and space limits apply. Direct chats do not take files.

A standalone file delivery shares a 25-second budget across its text, files, and retries.
Hermes waits only 30 seconds when its standalone sender runs in a fallback thread.
The service reserves time for each relay call. A retry keeps its message ID and file ID.
A retry adds no second file card.
A live file stays bound to its turn and request. Each live file links to its request.
Before each write, the service checks the active turn. A stop does not recall a write already sent to the relay.
Before the file message, the service reads new room entries to check for a delete or a member who left.
A file-only answer ends the turn without a failure notice.

A person adds up to 4 files to a message of the Team chat or of a named chat, for example a photo or a PDF. The bytes go into the log as entries of the kind `file`, so the relay and the helper need no change. `backend/coconut/team_files.py` serves the routes, and `TeamStore` keeps the files.

The field `files` of a `message` entry is a list of 1 to 4 items:

| Field | Value |
| --- | --- |
| `id` | The file ID: 16 random bytes in lowercase hex, as an entry ID. |
| `name` | 1 to 160 code points of valid Unicode text, with no `/`, no `\`, no control character, and not `.` or `..`. |
| `type` | The media type, for example `image/png`: 1 to 100 printable ASCII characters, or absent. |
| `size` | The number of bytes: 1 to 4 MiB (4,194,304). |
| `hash` | The SHA-256 of the bytes: 64 lowercase hex characters. |
| `width`, `height` | The size of an image in pixels: whole numbers from 1 to 20,000. Both or neither. |

The pieces. A file has `ceil(size / 737,280)` pieces of 720 KiB, and the last one can be shorter. The payload does not carry the number of pieces. Each piece is one `file` entry, not kept: `{"kind": "file", "rev": n, "at": "...", "file": "<file ID>", "index": 0, "data": "<standard base64>"}`. The base64 text of 720 KiB and the seal fit in the 1 MiB of a body. The entry ID is the first 16 bytes of HMAC-SHA256 with the member token of the Mac as the key, over `"coconut room file" | room ID (32 bytes) | file ID (16 bytes) | index (u32 big-endian)`. So a repeat writes the same entry, and no other member can take the ID first.

Rules for a Mac that reads a file:

1. A `file` entry is valid when `file` is a file ID, `index` is a whole number from 0 to 5, and `data` is standard base64 of 1 to 737,280 bytes. A reader checks the length of the text before the decode. An entry that does not fit stays in the store and shows nothing.
2. An item of `files` fits the table above. A reader takes null as an absent field. A `files` value that does not fit (one bad item, more than 4 items, the same ID two times, null) is left out, as a `preview` that does not fit. A message with an empty `text` is then not valid and shows nothing.
3. A file of a message is made only of the pieces that the member of the message wrote. A piece of another member never counts.
4. A file is complete when the Mac has each piece from 0 to n - 1 of that member, each piece but the last has 737,280 bytes, the sizes add up to `size`, and the SHA-256 of the joined bytes is `hash`. Until then, `complete` of the file is false. A piece can come before or after its message. A new piece of a shown file adds the event of its chat.
5. The Mac keeps the bytes of a piece once, as a BLOB in `team_file_pieces`. The payload of a `file` entry in `team_entries` has no `data`: in its place the Mac stores `size`, the number of bytes of a piece that fits. `team_message_files` keeps the files that each message names, with the size and the hash of each item. In `team_file_pieces` and `team_file_drafts` the BLOB is the last column, because SQLite walks the overflow pages of a large BLOB to read a column after it. An index on `received` gives the sweep of rule 7 the old pieces without a walk of the table.
6. The Mac checks a file when a piece of it or a message that names it changes, in the transaction of the accept. `checked` of `team_message_files` keeps the hash that passed, or null. So a view, also in a new process after a restart, reads `complete` from that column and never the bytes. Two members can name the same file ID, each with own pieces and an own hash. Each message has its own check, by rule 3.
7. The Mac removes the bytes of a piece when a `delete` entry replaces it. It removes the bytes of each file of a message when the message no longer shows (its author deleted it, the admin erased it, or the member of its chat removed the chat), or when a new write of the message no longer names the file. A file that another message of the same member names keeps its bytes. The writer of a piece keeps its own bytes until its `delete` entry over the piece (below), because its store can lack a later own message that names the same file, and the relay does not send an own piece again. The Mac removes the bytes of a piece that no message of the store names 7 days after this Mac got it. That sweep runs when the Mac accepts a new `file` entry or a draft, not on a timer.

The pieces leave the relay too. Only the writer of a piece can replace it (rule 3 of a reader), so each Mac writes a `delete` entry over its own pieces. A `delete` entry is small, so the relay frees the space. Each other Mac then removes the bytes by rule 7, and the count of the space below no longer counts the piece. A `delete` over a piece of a shown message cannot be taken back, so one place writes these entries: a pass of the sync of the room. It runs only when the sync read the room to its end, never while the service stops, and never while the room has a body of this member that waits for its room key. It first reads back an own write without an answer, then selects its work and writes its entries in one hold of the lock of the room, so no send of this Mac runs in between. It writes at most 48 entries in one run. It has two kinds of work:

- When this Mac's person deletes an own message with files, the route writes only the `delete` over the message, so the chat changes at once and the answer is fast. It notes the files of the message for the room, in memory, and asks for a pass of the sync of the room. The next pass, after its read to the end, writes a `delete` over each own piece of each noted file that no message names, with no wait of 24 hours: the person removed the message. A file leaves the note when its pieces are done, or when a message names it. After an error it stays for the next pass. A piece that is already a `delete` entry needs no write, so a repeat of the route writes nothing. A restart loses the note: then the second kind of work takes the pieces off after 24 hours.
- At most one time in an hour for each room, the pass finds the own `file` entries of the last 90 days that are older than 24 hours and whose file no message of the store names. A send takes no draft of 24 hours or older, so no later message can name such a piece. The pass writes a `delete` over them, within the same bound of 48 entries. The time of the last run is in memory only. So the relay keeps such a piece at most about one day after the last message that named it went. This covers a lost note, a send that failed and was never sent again, a message that the admin erased, and a chat that its member removed. The index `team_entries_kind` on the room, the kind, the member and `at` finds the rows, and the pass reads the small payload of each such entry, never the bytes. A Mac for a team only sends no file, so its pass finds nothing.

A later send of the same draft writes each piece again as a `file` entry with the next `rev`.
8. A Mac with an older Coconut keeps a `file` entry by rule 5 of a reader, with its base64 text, and ignores `files` by rule 2 of a message. So it shows a message with files and a text without the files, and it shows no message with an empty text.

The update from a version without files: in one step with the new column `files` of `team_messages`, the Mac moves the bytes of each stored `file` entry into `team_file_pieces`, takes `data` out of each payload, and reads each stored message with `files` again. A stop or an error in this pass takes the column back, so the next start does the pass again.

The routes. Each one has the authorization of the other team routes, and a write with an `Idempotency-Key` is safe to repeat (see [Repeat a write](#repeat-a-write)). The token of the bridge file gets 403 on a person's Mac. A Mac for a team only has no person, so the upload answers 409 with "This Mac runs Coconut for a team only, so nobody sends a file here."

| Request | Answer |
| --- | --- |
| `POST /v1/team/rooms/<room>/files`, body `{name, content, type?, width?, height?}` | `content` is standard base64. The Mac checks the rules of an item, keeps the bytes as a draft in `team_file_drafts`, and answers `{room, file}`: `{id, name, type, size, width, height}`. Nothing goes to the relay. An empty file or a file over 4 MiB answers 413 with "A file must have between 1 byte and 4 MB." Each upload removes the drafts that are 24 hours old or older, and runs the sweep of rule 7. A room that is not `active` answers 409. |
| `DELETE /v1/team/rooms/<room>/files/<id>` | Removes a draft of this Mac, and answers `{room, file, deleted: true}`. A draft that this Mac does not have answers 404. |
| `POST /v1/team/rooms/<room>/messages` with `files` | `files` is a list of 1 to 4 draft IDs of this room. With files, a `text` that is absent, empty or of white space only is an empty text. The Mac writes each piece of each file as a `file` entry, in order, then the `message` entry with the items, then removes the drafts. A piece whose bytes this Mac has with the size of the draft, from a send without an answer, is not written again. A piece without its bytes, for example after the sweep or after the delete of a message, is written again with the next `rev`. A repeat with the same `Idempotency-Key` finds the message before it checks the drafts, so it writes no second message and does not fail because the drafts are gone. A draft that does not exist, or that is 24 hours old or older, answers 400 with "This file is no longer on this Mac. Add it again." |
| `GET /v1/team/rooms/<room>/messages/<message>/files/<id>` | The bytes of a complete file of that message. The route names the message, because two members can name the same file ID: the answer is only the file of that message, made of the pieces of its member. The content type is `type`, or `application/octet-stream`. `Content-Disposition` names the file as the artifact route does. A message that does not show on this Mac, or that does not name the file, answers 404. A file that is not complete answers 409 with "This file is not on this Mac yet. Try again later." |

- A draft does not outlive its message. The Mac removes a draft when an own message that names its file ID comes into the store, by the send itself or by the sync after a send without an answer, and when the person deletes an own message, for each file of that message. So no later send can name the file of a message that existed: such a send answers 400 with "This file is no longer on this Mac. Add it again." A repeat with the same `Idempotency-Key` still gets its message, because the Mac finds the message before it checks the drafts.
- Each message view of a team chat has the key `files`: a list, empty without files, of `{id, name, type, size, width, height, complete}`. An older service sends no key, and the app then knows that this Mac has no files.
- Each short text of a message for a person shows, for a message with an empty text, "Photo" when the type of the first file starts with `image/`, else the name of the first file: the chat list (`last_message`), the sealed alert of a notification (also of a mention), the quote of a reply in a view (`reply.text`), and for an agent the quote of the answered message (`reply_to`) and the quoted context of the chat.
- A turn of an agent that a message with files starts gets one line for each file after the text: `[File: <name>, <type>, <size> bytes]`.

The space of a member. The relay keeps 512 MiB for the entries of one member that are not kept, for 90 days after each write. Over that, it refuses each write with 507, also a plain message. A piece of 720 KiB needs a body of about 960 KiB, so the pieces of 320 MiB of files use about 430 MiB, and the rest stays free for messages. Before it writes the pieces, the Mac adds the sizes of the pieces that this member wrote in this room in the last 90 days (the `size` of each stored `file` entry) and the sizes of the new files. A piece that a `delete` entry replaced counts nothing. So the space of a file is free again when the person deletes its message, or 90 days after the send. Over 320 MiB, it answers 413 with "Your space for files in this team is full. The space of a file is free again 90 days after you sent it." It gives the same answer when the relay answers 507 to a send with files.

What stays for later:

- Direct chats take no file. A direct message with `files` answers 400 with "A direct message cannot have a file yet. Send the file in a team chat." The views of direct messages have no key `files`.
- An agent gets the name, the type and the size of each file, not the bytes.

No log line has the name of a file or its bytes.

#### Alerts

An alert is a message of an agent in the Team chat or in a named chat that also carries a type and a level, for example a report of a cron job or of a watchdog. The team keeps its alerts in one place: a member makes a chat with a name such as "Alerts" (see [Chats of a team](#chats-of-a-team)), each sender writes there, and the app filters that chat by type and by level. An alert is a normal message of its chat in each other way: the same entry, order, unread count, mute, notification, reactions and replies. So the relay and the helper need no change, and a team needs no chat for each kind of alert. `backend/coconut/team_alerts.py` holds these rules.

The alert is the field `alert` of the payload of a `message` entry: `{"type": "risk", "level": "critical"}`. A message without the field is a plain message.

| Field | Value |
| --- | --- |
| `type` | A label that the sender chooses, for example `risk`, `deploy`, `payouts` or `infra`: 1 to 32 characters of `a` to `z`, `0` to `9`, `-` and `_`. |
| `level` | `info`, `notice` or `critical`. Absent means `notice`. |

Rules for a Mac that reads an alert:

1. An alert fits when it is an object, `type` follows the rule above, and `level` is one of the three levels or absent. A reader ignores each other key of the object, so a later version can add one.
2. Only a message with `bot` reads as an alert. A person writes no alert, and a reader ignores the field in a message of a person.
3. An alert that does not fit is left out, and the message shows as a plain message. An alert never makes a message invalid. The payload keeps the field as its member wrote it.
4. `team_messages` keeps the alert in the columns `alert_type` and `alert_level`, both null for a plain message. A store of an older version gets the columns in `migrate`, and in the same step the Mac reads each kept `message` payload with the field again, so an alert that came before the update shows as an alert after it. The pass changes no other column, no read position and no event.
5. An alert changes no unread count, no read position, no notification and no trigger: the sealed alert of a phone has the text of the message only.

How a sender writes an alert. Only a Mac for a team only writes one, because only its agents write into a team chat (see [An agent answers](#an-agent-answers)). Each way is a send outside a turn to `POST /v1/gateway/messages` with the token of the bridge file (`<data>/gateway/bridge.json`, the fields `url` and `token`):

- **A script on the Mac** adds `alert` to the body of its send:

  ```json
  {"kind": "send", "message_id": "outside:<an ID that the script makes>", "home": "<the Hermes home of the agent>",
   "chat_id": "<agent ID>:team:<room ID>:<chat ID>", "text": "Exposure is over the limit of the day.",
   "alert": {"type": "risk", "level": "critical"}}
  ```

  The request has the header `Authorization: Bearer <token>`. A repeat with the same `message_id` adds no second message. `level` can be absent.
- **A cron job of Hermes** names the alert at the end of the chat ID of its target: `deliver: coconut:<chat ID>:alert:<type>` or `deliver: coconut:<chat ID>:alert:<type>:<level>`. See [Cron deliveries into a team chat](#cron-deliveries-into-a-team-chat). Hermes gives a sender only a chat ID and a text: the live adapter also gets the ID of the job, and no sender gets its name. But Hermes gives the chat ID to the plugin as the job has it, so the chat ID carries the alert, and the service reads it (`team_alerts.split_target`). The plugin does not change, and the text of the job stays as it is: no line of the text names a type. The same chat ID works for each other send of Hermes to that target, for example its `send_message` tool.
- `alert` in the body counts when a send has both.
- The service trims the type and the level and makes them lower case. Each other value that does not fit answers 400 with "An alert has a type of 1 to 32 characters (a to z, 0 to 9, - and _) and a level: info, notice or critical. Send the message with such an alert, or without one." and writes nothing. So a cron job with a wrong target records a failed delivery, and its text does not arrive as a plain message.
- A text over 16,000 code points goes into more than one message, and each part carries the alert. So the count of a type counts each part.
- A direct chat takes no alert: a send to `<agent ID>:team:<room ID>:direct:<member ID>` with an alert writes the text as a plain direct message. A reply of an agent in a turn is never an alert.

The filter of the app. A page of a chat (`GET .../messages`) takes `alert_type=<type>` and `alert_level=<level>`, one or both, and then has only the alerts of that type and that level, newest first, with `before` as for each page. A value that no alert can have answers 400. Each page without `before`, with or without a filter, also has `alerts`, the counts of the shown alerts of the whole chat:

```json
{"total": 5, "critical": 2, "types": [{"type": "risk", "count": 3, "critical": 1},
                                      {"type": "deploy", "count": 2, "critical": 1}]}
```

`types` has the type with the most alerts first, then by name, and at most 64 types. A chat without an alert has `{"total": 0, "critical": 0, "types": []}`. Two indexes of `team_messages` hold only the rows of alerts, by the room, the chat, the type or the level, and `first`. So a page of one type or one level reads about as many rows as it returns, and the counts of a chat without an alert read no row. The counts read each alert of the chat one time: about 60 milliseconds for 80,000 alerts in one chat, measured with a store in memory, inside the lock of the service.

What the app shows (`ios/Coconut/TeamAlerts.swift`, `TeamAlertViews.swift`):

- A row of an alert has a small label above its bubble: the symbol of the level, the type and the level in words. A critical alert has a filled label, a filled symbol and its level in bold, so it differs from the other levels in form and in words, not only in color.
- A chat with at least one alert has a bar under its title: "All", "Critical" with its count when the chat has a critical alert, and one chip for each type with its count. A tap on a type shows only the alerts of that type, a second tap shows each type again, and "Critical" shows only the critical alerts, alone or with a type. With "Critical" on, each chip counts its critical alerts.
- A chat without an alert has no bar and looks as before. So does each chat of an older service, which sends no `alerts`.
- The choice lives in the app. It goes to no Mac and to no other device, and it goes when the chat closes, so a chat always opens with each message. While a choice hides messages, the app moves no read position. A message that the person sends shows each message again.

Older versions:

- An older Mac keeps the field by rule 2 of a `message` entry, relays nothing differently and shows the message as a plain message of the agent. Nothing is dropped. After its update, the pass of rule 4 gives its stored alerts their type and their level.
- An older app ignores the keys `alert` and `alerts` and shows a plain message.
- A newer app with an older service gets no key `alerts`, so it shows no bar, and it sends no query of a filter.
- A member's Mac that is older than the Mac of the sender stores the same entry as each other Mac. The relay never reads a payload.

No log line has the type, the level or the text of an alert.

#### Inbox chats

A named chat that agents deliver alerts to is an inbox chat: a list of notices, as an inbox of mail, not one long chat. The person asked for it: "Notices shouldn't be like a chat group but each one its own chat and then you can tap in and it'll mark as read and go away. Similar more to email than slack channel." Each notice is a thread of the chat: its first message with the answers to it. The person opens a notice as a chat of its own, which reads it, and a read notice leaves the top of the list. An answer brings the notice back to the top, unread. The log has no new kind: a thread is a view that each Mac makes from `reply_to` and from the field `thread` of a message. `backend/coconut/team_store.py` holds these rules, and the app shows them in `ios/Coconut/TeamInbox.swift`.

The thread of a message, which a Mac sets when the message first shows and keeps through an edit (`team_messages.thread`):

1. A message with `thread` joins the thread of the message that `thread` names, when that message shows in the same chat. `thread` is an entry ID of a message. A Mac for a team only writes it on each later part of the answer of an agent (`team_turns.Reply.say`): the first part answers the trigger with `reply_to`, and a later part names the trigger here, without a quote. A reader leaves a `thread` that is not an entry ID out, and the message stays. A Mac without this version keeps the field by rule 2 of a `message` entry and shows nothing new.
2. Else a message with `reply_to` joins the thread of the message that it answers, when that message shows in the same chat.
3. Else a message of an agent without an alert that comes at most 2 seconds after the message before it in the chat, when that message is of the same agent and in a thread that it did not start, joins that thread. A Mac for a team only before rule 1 wrote the later parts of an answer this way. An alert is always a notice of its own.
4. Else the message starts a thread of its own.

The relay keeps only the newest write of an entry, so a Mac that reads the log after a join can get an edited message after its answers. When such a message first shows, the threads of the messages that answer it join its thread. A store of an older version gets the column in one pass over its rows, in the order of `first`, with rules 2 to 4.

A named chat is an inbox chat while it has an alert. Agents write alerts only from the sends of a Mac for a team only, so a chat gets one only when a team points the deliveries of its agents there. The Team chat is never an inbox chat.

What each Mac keeps on its own for an inbox chat, besides the read position of the chat:

- `team_thread_reads`: the read position of each thread that the person read on its own. A message is read when it is at or before the position of its chat or of its thread, or when the person of this Mac wrote it. When a read of a thread leaves no unread message between the position of the chat and a later message, the position of the chat moves up to the newest message before the oldest unread one, and the rows of the threads at or before it go. So a row stays only for a thread that the person read ahead of an older unread one.
- The unread count of an inbox chat counts its unread threads, at most 99: the notices that wait for the person. The badge of the app, the row of the chat and its notifications use it. A mention reads the same rule.

The routes:

| Request | Answer |
| --- | --- |
| `GET /v1/team/rooms/<room>/threads?chat=<id>&before=<first>&limit=<n>` | The notices of a named chat: `{room, chat, threads, before, total}`. Without `before`, `threads` has each unread thread (at most 200) and then the `limit` read threads (default 50, at most 100) with the newest messages; with `before`, the next read threads whose newest message is before it. Each thread has `id` (the entry ID of its first message), `count`, `unread`, `root` (its first message that shows) and `latest` (its newest message), as messages of a page. `before` asks for the next page, or is null. `total` counts each thread of the chat, on the first page only. |
| `GET .../messages?chat=<id>&thread=<id>` | One page of the messages of one thread, newest first, with no `alerts`. |
| `POST .../read` with `{first, chat, thread}` | Moves the read position of one thread, and answers `{room, read, unread}` with the position of the thread and the unread count of the chat. A thread without `chat` answers 400. |

The room view of `GET /v1/team/rooms` has `inbox: true` on a named chat that is an inbox chat, and a message of a page has `thread`. An older app ignores both and shows the chat as a chat. A read of the whole chat (`POST .../read` without `thread`, and Read all of the app) reads each notice. A notification of a message of an inbox chat has `thread` in its `coconut` part, so a tap opens that notice over the list of notices. It notifies only while the message is unread by the rule above, so a notice that the person read on another device sends nothing.

What the app shows: the row of the chat in Chats and in the Mac sidebar stays one row, with the count of the unread notices. A tap opens the list: the unread notices first, the newest answer first, each with its title (the bold first line that an agent writes as the headline of a notice, else the first line), the type of its alert, the rest of its text or its newest answer, its age and the count of its unread messages. Read follows, folded, with its count on the right, as Archived in Chats, and pages of 50. A swipe or the menu of a row reads a notice without opening it, and Read all reads each one. A notice opens as a chat of the team at its top, as a mail does, and reads at once. A message there answers the newest message of the notice that another member or an agent wrote, so the agent of the notice gets it, and the quote of the message right above does not show.

No log line has the text of a notice.

### Direct messages

A direct message is a private chat between two people of a team. It is an entry of the kind `direct` in the log that the team has. Its text is sealed a second time, with a key that only the two Macs can make. No second room, no invite and no join is necessary. `backend/coconut/team_directs.py` serves the routes, and `TeamStore` keeps the chats. A person can also write to an agent of a Mac for a team only with the same entries: see [Direct chat with an agent](#direct-chat-with-an-agent).

#### The box

The payload of a `direct` entry is `{"kind": "direct", "rev": n, "at": "...", "box": "<unpadded base64url>"}`. The entry is not kept, so the relay holds it for 90 days, as a `message` entry.

```
box       = nonce (12 bytes) | ciphertext | tag (16 bytes)
plaintext = {"to": "<member ID>", "text": "...", "reply_to": "<entry ID>"}
```

- The seal is ChaChaPoly with the pair key. The nonce is 12 random bytes, new for each seal.
- The additional data is `room ID (32 bytes) | entry ID (16 bytes) | member ID of the writer (32 bytes)`: 80 bytes. So a box belongs to one room, one entry and one writer.
- The pair key: X25519 of the own private agreement key and the public agreement key of the other member (the `agreement` of its `member` entry). Then HKDF-SHA256 with an empty salt and 32 bytes of output. The input is the shared secret, and the info is `"coconut room direct" | room ID (32 bytes) | the lower member ID (32 bytes) | the higher member ID (32 bytes)`, compared as bytes. So both Macs make the same key, and a key of one room opens nothing in another room.
- The plaintext is one JSON object in UTF-8. `to` is the member that the message is for. `text` has 1 to 16,000 code points of valid Unicode text. `reply_to` is the entry ID of a direct message of the same chat, or absent. `preview` is the link preview of a message of a person, or absent: see [Link previews](#link-previews). A reader takes null as an absent field, and ignores a field that it does not know. A message of a chat with an agent also has `bot`, and can have `page` or `on`: see [Direct chat with an agent](#direct-chat-with-an-agent). A box without `text` can be a reaction: see [Reactions](#reactions).
- An edit is a new write of the same entry ID with the next `rev` and a new box. A removal is a `delete` entry over it. The rules of an entry (`rev`, the member of an entry) do not change.

`TeamLog.swift` has the pair key, the seal and the open of a box. The helper has the operations `seal_direct` and `open_direct`. See [Test vectors](#test-vectors).

#### What the other members see

The recipient is inside the box. Another member of the team can open the outer seal with the room key. So that member sees that the writer wrote a `direct` entry, its size, its time, and its edits and removal. That member cannot see for whom the message is, and cannot read it. The relay sees what it sees for each entry, and nothing more (see [What the relay sees](#what-the-relay-sees)).

#### Who can get a direct message

- A `member` entry has the optional field `direct`. `true` means "this person can read direct messages". A missing `direct` and each other value are false. Only a person's Mac writes it, never a Mac for a team only.
- A Mac writes `direct: true` only after an app of its person that knows direct messages asked for them. The sign is the header `X-Coconut-Team: direct` on `GET /v1/team/rooms` from a paired device. The first such request sets a lasting mark in the store (`team_marks`). From then on, the sync writes the `member` entry of this Mac again with `direct: true`, one time in each room, as for a changed `bot_only` mark. The next pass does it at once, also within 5 minutes of an earlier write. A change of the name, avatar or color keeps the field. A tool on this Mac can send the header too, and nothing happens.
- Without the mark, a Mac writes no `direct`, and nobody can send to it. So an app of today sees no change.
- A Mac sends a direct message only to a member that is in the member list now, has `direct` true, is not a Mac for a team only, and is not itself. The Mac itself must have the mark.
- A Mac for a team only gets no direct message for itself. A person writes to one of its agents instead, in a direct chat with that agent: see [Who can write to an agent](#who-can-write-to-an-agent).

#### Rules for a Mac that reads a `direct` entry

1. Only a person's Mac with the mark and a Mac for a team only read the kind. A Mac for a team only reads only the chats of its agents (see [The Mac for a team only](#the-mac-for-a-team-only)). Each other Mac keeps the entry by the rule of an unknown kind and shows nothing.
2. The Mac opens each box before the transaction of the answer, without the lock of the service, as it opens the bodies. No call to the helper or the relay runs in a transaction.
3. An entry of another member: the pair key takes the agreement key of the newest valid `member` entry of the writer that the Mac has at that point. The store has some, and the `member` entries before it in the same answer of the relay count, in their order. When the box opens, `to` is this Mac, and the fields fit, the entry is a message of the chat with the writer. This holds for a box without `bot`. A box with `bot` is a message of a chat with an agent: see [Direct chat with an agent](#direct-chat-with-an-agent). When the box does not open or names another member, the entry is not for this Mac and shows nothing.
4. An own entry that the store does not have: the Mac tries the pair key of each other member until the box opens and `to` names that member. This happens after a stop between the write to the relay and the write to the store, and when the store is behind the relay. A Mac never reads a room without its row in the store.
5. The relay keeps only the newest write of an entry and gives it at the `seq` of that write. So when a member writes its `member` entry again, for a new name or the mark `direct`, that entry comes after the direct entries from before the change. A Mac that reads the room from an earlier position meets these direct entries before the key that they need. So such an entry waits (`team_direct_waits`, in the transaction that accepts the entry): an entry of a writer without a `member` entry at that point, and an own entry that no key of the known members opened. When the Mac has read the room to its end, it tries each waiting entry one more time, outside a transaction, with the members that the store has now. Then the wait ends, also for an entry that does not open and for a writer that still has no `member` entry. A message that shows then is history: it adds the event of its chat and no notification. A `delete` over a waiting entry and a new write of it end its wait, and a new write gets its own try. A leave removes the waits of the room.
6. The newest write of an entry counts. A write that no longer opens, and a `delete` over the entry, remove the message from the chat on this Mac.
7. The times follow rule 6 of the Team chat: `created_at` is the `at` of the first write that this Mac saw, and `edited_at` is the `at` of the newest write when `rev` is over 1.
8. A chat keeps its history after the other member leaves or is removed. The name comes from `team_names`, as in the Team chat.

A direct message never goes into `team_messages`. The Mac keeps it in `team_directs`, and the read position of each chat of two people in `team_direct_reads`: one for each room and other member. So no query of the Team chat, of its unread count, of its last message, or of the notifications of the team reads a direct message. The triggers of the Team chat read no direct message either. Only on a Mac for a team only does a direct message of a chat with an agent start a turn of that agent.

The read position of a direct chat starts at 0, so a direct message that arrives counts as unread. It moves only forward, and never past the newest message of the chat. The unread count is the number of messages of the other member after it, up to 99.

#### Routes of the direct messages

Only a paired device reaches these routes on a person's Mac: `tool_route` refuses the token of the bridge file. A Mac for a team only answers 409 with "This Mac runs Coconut for a team only, so it has no direct messages." A write in a room that is not `active` answers 409, as in the Team chat. In each route, a team agent ID in the place of `<member>` names a chat with an agent: see [Routes of a direct chat with an agent](#routes-of-a-direct-chat-with-an-agent).

| Request | Answer |
| --- | --- |
| `GET /v1/team/rooms` with `X-Coconut-Team: direct` | As without the header, and: each member has `direct`, true when this Mac can send it a direct message now (see [Who can get a direct message](#who-can-get-a-direct-message)) and the room is `active`. Each room has `directs`: one item for each direct chat with a message that shows, newest chat first: `member`, `member_name`, `unread`, and `last_message` (`first`, `this_mac`, the first 200 code points of `text`, `created_at`). A chat with a member that left stays in the list. |
| `GET /v1/team/rooms/<room>/directs/<member>/messages?before=<first>&limit=<n>` | One page, newest first: `{room, member, messages, before}`, with the rules of a page of the Team chat. A value that is not a member ID (64 hex characters) and not a team agent ID of this room answers 404. |
| `POST /v1/team/rooms/<room>/directs/<member>/messages`, body `{text, reply_to?, preview?}` | Writes a direct message of the person and answers `{room, member, message}`. `reply_to` is the `id` of a message of the same chat. `preview` goes into the box (see [Link previews](#link-previews)). With an `Idempotency-Key`, the entry ID comes from the room, the device and the key, as in the Team chat. 409 with a sentence when the member cannot get a direct message now: it left the team, it has no `direct`, it is a Mac for a team only, it is this Mac, or this Mac has no mark. |
| `PATCH /v1/team/rooms/<room>/directs/<member>/messages/<id>`, body `{text}` | Changes the text of a message of this Mac's person, with a new box, and answers `{room, member, message}`. The reply stays. The link preview stays only while the new text contains its `url`. The same text writes nothing. A message of the other member answers 403. A member that can no longer get a direct message answers 409. |
| `DELETE /v1/team/rooms/<room>/directs/<member>/messages/<id>` | Removes a message of this Mac's person with a `delete` entry, and answers `{room, member, id, deleted: true}`. A repeat gives the same answer and writes nothing. |
| `POST /v1/team/rooms/<room>/directs/<member>/read`, body `{first}` | Moves the read position of the chat, and answers `{room, member, read, unread}`. |
| `PUT /v1/team/rooms/<room>/directs/<member>/messages/<id>/reaction`, body `{emoji}` | Sets the reaction of this Mac's person on a message of the chat, and answers `{room, member, message}`. See [Reactions](#reactions). |

A message in an answer: `id`, `first`, `this_mac`, `text`, `reply_to`, `reply` (`id`, `this_mac` and the first 200 code points of `text` of the message that `reply_to` names in the same chat, or null), `reactions` (as in the Team chat, see [Reactions](#reactions)), `preview` (as in the Team chat, see [Link previews](#link-previews)), `created_at` and `edited_at`.

When a direct chat of two people changes on this Mac, the Mac adds one `team_directs.updated` event with `{"team": <room ID>, "member": <member ID>}` in the same transaction: a new message, an edit, a removal, a new time from the relay, a new name of the other member, a new, changed or removed reaction, or a new read position. `GET /v1/updates` then has `team_directs_changed`: a list of `{team, member}`. The type is in the `known` set, so an app of today does not read its whole state again. A change of `direct` of a member adds `team.updated`, as each other change of a member.

#### Notifications of a direct message

A direct message notifies the phone like a team message (see [Notifications](#notifications)), with these differences:

1. The kind of the item is `direct`. The switch `notifications` of the room is for the Team chat, so it does not decide.
2. A direct message notifies only when it is the first write of a direct message of the other member that this Mac sees for the first time, shows, is after the read position of its chat, and has an `at` under one hour old.
3. Each plan and each send checks the item again: the room is `active`, the message still shows, and it is after the read position of its chat.
4. The sealed alert has the name of the writer as its title and the text as its body, with no subtitle. When the store has no name for the writer, the title is the name of the team. `thread-id` is the room ID, a colon, and the member ID of the other member. A phone without a relay route gets "Coconut" and "You have a new direct message."
5. The `coconut` part is `{"version": 1, "device_id": ..., "kind": "direct", "direct": {"team": <room ID>, "member": <member ID>}}`. A group whose items are all direct messages of one chat has the thread of that chat and `{"version": 1, "device_id": ..., "kind": "group", "count": n, "direct": {"team": <room ID>, "member": <member ID>}}`. Neither has `team` at the top, because the app of today would open the Team chat. Each other group opens Inbox, also a group of team messages and direct messages of one room.
6. A group counts "direct message" and "direct messages" after "team message".

Rules 4 and 5 give the title, the thread and the `direct` part of a message of a chat of two people: a box without `bot`. A message of a chat with an agent has the name of the agent, the thread of its `bot` entry and the team agent ID. See [Events and notifications of a chat with an agent](#events-and-notifications-of-a-chat-with-an-agent).

No log line has the text of a direct message, the name of a writer or the name of a team.

### Direct chat with an agent

A person of a team writes to an agent of a Mac for a team only alone, and the agent answers there. The chat is a direct chat: `direct` entries of the log, each with a box that only the Mac of the person and the Mac that runs the agent can open. The pair key of the two Macs seals the box, as for a direct message between two people. A person's Mac writes no mark `direct` in a `bot` entry and writes no answer for its agents, so its agents have no direct chat. The relay, the helper (`seal_direct`, `open_direct`) and the seal do not change.

A person's Mac reads, keeps, sends and serves a direct chat with an agent, shows the typing sign of the agent, and notifies its phones. A Mac for a team only marks its agents, reads the direct messages for them, and answers (see [The Mac for a team only](#the-mac-for-a-team-only)).

A team agent ID is `t-<room ID>-<entry ID of the bot entry>`, as in `bots` of `GET /v1/team/rooms`.

#### The mark of an agent

The payload of a `bot` entry has the optional field `direct`.

- `direct: true` means "this agent answers direct messages".
- Only a Mac for a team only writes it, and only while the record of the agent has `chat` true (the rule of `open`).
- A missing `direct` and each other value are false. The agent still shows.
- The mark is part of the form of an own `bot` entry, so a change of `chat` writes the entry again in each team.
- A reader keeps the mark in `team_bots` (`direct`). A store of an older version gets the column in one step. Each start of the service then sets the mark of each row from the kept `bot` entry of the row, as it sets `open`.
- A change of `direct` of an agent of another member adds `team.updated`, as each other change of an agent.

#### The box of a chat with an agent

The plaintext of the box of a `direct` entry has these optional fields:

| Field | Meaning |
| --- | --- |
| `bot` | The entry ID of the `bot` entry of the agent of the chat. With it, the entry is a message of the chat of one person with that agent. Without it, the entry is a message of the chat of two people, as before. |
| `page` | The entry ID of a `page` entry: the page that the message is about. Only a person writes it, and only with `bot`. |
| `on` | Only in the typing sign of an agent (rule 12 of [The Mac for a team only](#the-mac-for-a-team-only)): `true` or `false`, with `bot` and without `text`. |

```
from the person to the agent: {"to": "<member ID of the Mac that runs the agent>", "text": "...", "bot": "<entry ID>", "reply_to": "<entry ID>", "page": "<entry ID>"}
from the agent to the person: {"to": "<member ID of the person's Mac>", "text": "...", "bot": "<entry ID>"}
```

- A reader takes null as an absent field and ignores a field that it does not know.
- A `bot` or a `page` that is present and is not an entry ID does not fit: the entry shows nothing.
- A reader ignores `page` in a box without `bot`, and in a message of the Mac of the agent.

#### Who can write to an agent

A person's Mac sends a direct message to an agent only when each of these holds:

1. The room is `active`.
2. This Mac reads direct messages: it has the mark `direct` of `team_marks`.
3. The store has a valid `bot` entry with that entry ID, and its `direct` is true.
4. The member of that `bot` entry is in the member list now, and is not this Mac.

Else the route answers 409 with one sentence:

| Case | Sentence |
| --- | --- |
| No valid `bot` entry, or its member is not in the list | "This agent is no longer in the team." |
| The `bot` entry has no `direct`, or it is an entry of this Mac | "This agent does not answer direct messages now." |
| This Mac has no mark | "Direct messages are not on for this Mac yet. Open the Team screen of the Coconut app, then try again." |
| The room is not `active` | The sentence of a write in such a room, as in the Team chat. |

The Mac checks the mark first, then the `bot` entry and its member, then `direct`.

#### Rules for a person's Mac that reads a `direct` entry with `bot`

These rules add to [Rules for a Mac that reads a `direct` entry](#rules-for-a-mac-that-reads-a-direct-entry). The pair key, the wait for a `member` entry, the times and the newest write do not change.

1. An entry of another member whose box opens, names this Mac in `to`, and has `bot`: a message of the chat of this person with that agent. The store keeps the writer, the member (the writer) and the `bot` entry of the agent.
2. An own entry that the store does not have: the Mac tries the pair key of each other member, as before. When the box opens with `to` that member and has `bot`, the entry is a message of the chat with that agent, with its `page`.
3. A message with `bot` shows only while `bot` names a `bot` entry of the member of the chat that the store has a name for (`team_names`, kind `bot`), as a message of an agent in the Team chat. The `bot` entry can come after the message, so the check runs in each query of the chat, not when the Mac accepts the entry. A new name of the `bot` entry, also its first name, changes the chat.
4. A message with `bot` is never part of the chat of two people with that member, and a message without `bot` is never part of a chat with an agent. The page of messages, the newest message, the list of the chats, the unread count, the read position, the reply quote, an edit, a removal and the notification of each chat read only its own messages.
5. The chat keeps its history after the agent or its Mac leaves the team. The name comes from `team_names`.

A reader does not check that the Mac of the `bot` entry is a Mac for a team only. A member can write as an agent of its own Mac, as it can in the Team chat. So each item of `bot_directs` names the Mac of the agent (`member`, `member_name`), and the app lets a person write only to an agent of `bots` with `direct`.

The chat with an agent is one chat for each room and `bot` entry. Its read position is in `team_bot_direct_reads`, apart from the read position of the chat of two people with the same member. It starts at 0 and only moves forward, never past the newest message of the chat that shows. The unread count is the number of messages of the other side after it, up to 99.

#### Routes of a direct chat with an agent

The routes of the direct messages take a team agent ID in the place of the member ID. Only a paired device reaches them. A Mac for a team only answers 409 as for a member ID. An ID that is not a member ID and not a team agent ID of this room answers 404 with "This person is not in this team." A team agent ID of this room whose `bot` entry the store never showed is a chat without a message: a read gives an empty page, and a write answers 409 by [Who can write to an agent](#who-can-write-to-an-agent).

| Request | Answer |
| --- | --- |
| `GET /v1/team/rooms/<room>/directs/<team agent ID>/messages?before=<first>&limit=<n>` | One page, newest first: `{room, bot, messages, before}`. `bot` is the team agent ID. |
| `POST /v1/team/rooms/<room>/directs/<team agent ID>/messages`, body `{text, reply_to?, about_document_id?, preview?}` | Writes a direct message of the person to the agent and answers `{room, bot, message}`. The `Idempotency-Key` rule is that of a direct message. `reply_to` is the `id` of a message of the same chat. `about_document_id` is the Library ID of a team page (`t-<room ID>-<entry ID>`) of this room that the Library of this Mac shows now (valid, not past its end) and whose `bot` is the agent of the chat. Another value answers 409 with "Choose a page of this agent in the Library." 409 with a sentence of [Who can write to an agent](#who-can-write-to-an-agent) when the agent cannot get a direct message now. A chat of two people ignores `about_document_id`. |
| `PATCH .../directs/<team agent ID>/messages/<id>`, body `{text}` | Changes the text of a message of this Mac's person, with a new box, and answers `{room, bot, message}`. `bot`, `page` and `reply_to` stay. The link preview stays only while the new text contains its `url`. The same text writes nothing. A message of the agent answers 403. |
| `DELETE .../directs/<team agent ID>/messages/<id>` | Removes a message of this Mac's person with a `delete` entry, and answers `{room, bot, id, deleted: true}`. A message of the agent answers 403. |
| `POST .../directs/<team agent ID>/read`, body `{first}` | Moves the read position, and answers `{room, bot, read, unread}`. |
| `PUT .../directs/<team agent ID>/messages/<id>/reaction`, body `{emoji}` | Sets the reaction of this Mac's person on a message of the chat, and answers `{room, bot, message}`. See [Reactions](#reactions). |

A message in an answer has the fields of a direct message (`id`, `first`, `this_mac`, `text`, `reply_to`, `reply`, `reactions`, `preview`, `created_at`, `edited_at`) and `page`:

- `null` for a message without a page.
- `{"id": "t-<room ID>-<entry ID>", "title": "<title>"}` while the page shows in the Library of this Mac. The title comes from the store at the time of the read, not from the message.
- `{"id": null, "title": null}` for a page that no longer shows.

A message of a chat of two people has no `page`.

#### The list of the teams

`GET /v1/team/rooms` with `X-Coconut-Team: direct` from a paired device:

- Each item of `bots` has `direct`: true when this Mac can send the agent a direct message now (see [Who can write to an agent](#who-can-write-to-an-agent)). An agent of this Mac has `direct` false. Without the header, no item has the field.
- Each room has `bot_directs`: one item for each chat with an agent that has a message that shows, newest chat first: `bot` (the team agent ID), `bot_name` (the last name of `team_names`), `member` (the member ID of the Mac of the `bot` entry), `member_name` (the last name of that member in `team_names`, or null), `unread`, `last_message` (`first`, `this_mac`, the first 200 code points of `text`, `created_at`), and `typing` (the whole number of seconds that are left of the typing sign of the agent, or null). A sign shows only while the room is `active` and the member of the agent is in the member list. A chat with an agent that left stays in the list.
- `directs` stays the list of the chats of two people. An older app reads neither new field.

#### Events and notifications of a chat with an agent

- When a chat with an agent changes on this Mac (a new message, an edit, a removal, a new time from the relay, a new name of the agent, a new, changed or removed reaction, or a new read position), the Mac adds one `team_bot_directs.updated` event with `{"team": <room ID>, "bot": <team agent ID>}` in the same transaction. `GET /v1/updates` then has `team_bot_directs_changed`: a list of `{team, bot}`. The type is in the `known` set and changes no Work row, as `team_directs.updated`. `team_directs.updated` and `team_directs_changed` stay for the chats of two people, because an older app reads `member` in each item.
- A message of an agent notifies the phone by the rules of [Notifications of a direct message](#notifications-of-a-direct-message): the kind of the item is `direct`, and it notifies only as the first write of a message of the other side, after the read position of its chat, under one hour old. Each plan and each send checks the read position of the chat with the agent. The switch `notifications` of the room does not decide.
- The sealed alert has the name of the agent as its title and the text as its body, with no subtitle. When the store has no name for the agent, the title is the name of the team. `thread-id` is the room ID, a colon, and the entry ID of the `bot` entry. A phone without a relay route gets "Coconut" and "You have a new direct message."
- The `coconut` part is `{"version": 1, "device_id": ..., "kind": "direct", "direct": {"team": <room ID>, "bot": <team agent ID>}}`. It has no `member`, so an older app opens the app and nothing more. A group whose items are all messages of one chat with an agent has the thread of that chat and `{"version": 1, "device_id": ..., "kind": "group", "count": n, "direct": {"team": <room ID>, "bot": <team agent ID>}}`. Each other group opens Inbox, also a group of a chat with an agent and another chat.

#### The Mac for a team only

`backend/coconut/team_turns.py` and `backend/coconut/team_typing.py` hold these rules.

1. It writes `direct: true` in the `bot` entry of each agent whose record has `chat` true.
2. It reads the `direct` entries of the other members: it opens each box with the pair key of the writer. A box with `to` this Mac and a `bot` that is an entry ID is a message of the chat of the writer with that agent, and goes into `team_directs`, with its `page`. A box without `bot` shows nothing there. It keeps no read position and sends no notification. The helper opens the boxes before the transaction and without the lock of the service, and the wait for a `member` entry that comes later and the repair of an own entry hold as on a person's Mac. A message of the chat shows there only while `bot` names a `bot` entry of this Mac that the store has a name for. A message that waited for its key is history: it starts no turn. A box of a reaction (see [Reactions](#reactions)) is no message: this Mac keeps nothing of it, so a reaction starts no turn and goes into no context.
3. A `direct` entry starts a turn of an agent of this Mac when each of these holds (the rules of [An agent answers](#an-agent-answers)):
   - This Mac sees the entry for the first time, and its `rev` is 1.
   - Its member is another member than this Mac, and that member is in the member list.
   - The `at` of the relay is under one hour old.
   - Its box opens, names this Mac in `to`, has a valid `text`, and its `bot` is the entry ID of the `bot` entry of an agent of this Mac that is shared with the room, is not archived, and has `chat` true. The `member` entry of this Mac does not count here: a mention of this Mac is a rule of the team chat only (rule 4 of [An agent answers](#an-agent-answers)).
4. The trigger goes into `team_triggers`, with the bound of 8 waiting triggers for one agent in one room. One thread still runs the triggers of one agent in one room one at a time: the Team chat, the named chats and the direct chats.
5. The turn goes to Hermes with the chat ID `<bot ID>:team:<room ID>:direct:<member ID>`: one session for each person and agent. The name of the chat for Hermes is "<name of the person> · <name of the team>". The adapter gives the turn to Hermes as a group chat, as each turn of the team chat. It does not give the chat type `dm`, because Hermes adds a first-contact note to a `dm` chat and writes to the config of the Hermes home. The chat ID is its own, so the session is its own, and the rules tell the agent that one person reads the chat. The guard of the plugin keeps the session out of the other chats (see [What the other members and the Mac of the agent see](#what-the-other-members-and-the-mac-of-the-agent-see)).
6. The rules of the chat for Hermes: "You are in a direct chat with {name}, a member of the team {team}, in Coconut. Only {name} reads your reply. Keep this chat private: do not repeat in another chat what is written here. Do not repeat here what another member wrote to you in a direct chat. Coconut shows Markdown and has no cards here: when you need a choice or a fact, ask in your reply." The rules of the Team chat and of each named chat have the sentence "Do not repeat here what a member wrote to you in a direct chat." No rule depends on a name, because a name does not prove a person.
7. The quoted context: at most 20 messages of the same chat since the last message of the agent there and before the trigger. A reply to a message of the chat names that message, as in the Team chat. With `page`: the Mac finds its own document through `team_page_entries`, and the turn gets the title, the key and the current text of the page from the store of this Mac (at most the first 16,000 code points, with one line that says so for a longer page), as quoted context that is data and not instructions. A page that this Mac no longer has gives one line: the person asks about a page that is no longer in the Library.
8. Each complete segment of the reply and each commentary is one `direct` entry of this Mac for the person, with `bot`. No message has `reply_to`. A text over 16,000 code points goes into more than one message. The entry ID comes from the room, the trigger, the agent and the number of the message in the turn.
9. An approval of a command is denied: "Nobody can approve a command in this chat, so Coconut denied it. Tell {name} that you could not run it." A clarify question goes into the chat as a message with its choices as a list, and Hermes gets the answer to end the turn: "Coconut put your question in this chat. End your turn now. The reply of {name} starts a new turn."
10. A send of Hermes outside a turn in such a chat is a direct message of the agent to that member, while the agent answers in the room and the member is in the member list. A cron job of the agent delivers there with `deliver: coconut:<bot ID>:team:<room ID>:direct:<member ID>` (see [Cron deliveries into a team chat](#cron-deliveries-into-a-team-chat)).
11. The stops and the notices of a failed turn are those of the Team chat, as direct messages in the chat. A removal of the trigger message, a member that leaves or is removed, an agent whose chat goes off, an agent that leaves the team and a leave of this Mac stop the turn. A turn that a stop of the service cut off posts its notice in the same direct chat at the next start. Each message of the agent goes out only while the agent answers in the room, the request stands, and the person is in the member list. No call to the helper or the relay holds the lock of the service.
12. The typing sign: while the adapter shows the turn, the Mac writes one `direct` entry that is not kept, with a box for the person: `{"to": "<member ID>", "bot": "<entry ID>", "on": true}`. It writes it again every 20 seconds, and `on` false at each end of the turn. The entry ID is the first 16 bytes of HMAC-SHA256 with the member token of the Mac as the key, over `"coconut room direct typing" | room ID (32 bytes) | entry ID of the bot entry (16 bytes) | member ID of the person (32 bytes)`. A person's Mac that reads such a box with `on` true and an `at` of the relay under 60 seconds old shows the agent as typing in that chat for 30 seconds from the read. `on` false ends it. A message of the agent does not end it. The state lives in memory only. `typing` of an item of `bot_directs` is the whole number of seconds that are left, or null. A change adds `team_typing.updated` with `{"team": <room ID>}`. A Mac with an older Coconut finds no valid `text` in the box and shows nothing. The typing of the person is not sent.

#### Older versions

- A Mac of another member stores each `direct` entry and cannot open the box.
- A Mac for a team only with an older Coconut writes no `direct` in its `bot` entry, so no Mac sends it a direct message.
- A person's Mac with an older Coconut answers 404 for a team agent ID in a route of the direct messages, and sends no `direct` for an agent. So a new app shows no entry point with that Mac.
- An older app reads no `direct` of an agent and no `bot_directs`.

#### What the other members and the Mac of the agent see

- Another member sees that the person and the Mac of the agent write `direct` entries, with their size and time. It cannot read them or see for whom they are.
- The Mac that runs the agent keeps the chat in its store, and Hermes keeps its session there.
- The agent has one memory and one set of skills, and Hermes lets it search the sessions of each chat. The rules of each chat tell the agent to keep a direct chat private. The guard of the plugin also enforces it for these tools (see [The guard of the direct chats](architecture.md#the-guard-of-the-direct-chats)):
  - `session_search` reads a session of a direct chat only in a turn of that chat. In each other turn, also a turn outside Coconut on the same Hermes, a read is refused, and a search or a list of sessions shows no session of the direct chat.
  - In a direct chat, in the review that Hermes runs after its turn, and in a subagent of it, the agent writes no memory and no skill. Each other session keeps its memory and skill writes: the Team chat, the named chats, cron jobs, the command line and the curator of Hermes.
- The guard does not cover the terminal and the file tools of the agent: they can read `state.db` and the logs of Hermes, and the store of Coconut. It does not cover what the agent itself writes into a file or a cron job in a direct chat, another profile of the same Hermes root, or a memory provider plugin of Hermes.
- A Hermes without the middleware of the guard gets no turn of the team chat. The service answers each trigger of the Team chat, of a named chat and of a direct chat with the notice `team_turns.OUTDATED`.

No log line has the text of a message, the name of the agent or the name of the person.

### Team-only mode

A Mac can run a production Hermes agent with no person at it and no Coconut. Such a Mac runs Coconut "team only" (`backend/coconut/team_only.py`), so its agent can publish pages to the team. The full service does much more than a team needs, and it changes the Hermes profiles of the Mac. The team-only service does none of that.

The service starts in this mode with `python3 -m coconut --team-only`, or with `COCONUT_MODE=team` in its environment, so a login item can set it. Coconut.app sets up a Mac in this mode with `--set-up --team-only`, and its login item sets the variable. See [A Mac without a person](install-and-update.md#a-mac-without-a-person). Another value of `COCONUT_MODE` stops the start. The service uses the same data folder as a full Mac: `coconut.sqlite3`, `<data>/team/`, `<data>/gateway/bridge.json`, `process.json` (with `"mode": "team"`) and the same logs. It takes the service lock of the folder, so a full service and a team-only service never share one folder. At its first start, the service writes a mark in its store (the table `service_mode`), and from then on the folder is a team-only folder, also without an agent or a room. The two modes never take the folder of the other (`service_base.check_data_folder`): a folder with the data of a full install (a paired phone, a task, a message, a Hermes profile or an agent) stops a team-only start, and a folder with the mark stops the start of the full service and of its refuse mode without Hermes. Each refusal is one line that names the cause and says to use another data folder, and it writes nothing.

What the service does:

- It keeps the team rooms of this Mac in step with the relay: the store, the sync thread, the socket watcher and the helper, as on a full Mac.
- It serves the team routes on the main listener with the token of the bridge file, so `coconut team` and `scripts/team.py` work as on a full Mac. Only the routes of this section, the team routes, `POST /v1/gateway/document` and `DELETE /v1/gateway/document` need the token. Each other route, and a pair request, answers 503 with `{"error": ..., "code": "team_only"}`. `GET /health` works.
- It keeps the agents that this Mac declares, and it saves their pages.
- It gives the messages that name an agent with its chat connected to the Hermes gateway of that agent, and writes the replies into the chat. See [An agent answers](#an-agent-answers). It does the same for a direct message of a person to such an agent, and writes the replies as direct messages for that person. See [The Mac for a team only](#the-mac-for-a-team-only). For that, the main listener also serves `POST /v1/gateway/poll`, `/v1/gateway/events`, `/v1/gateway/messages` and `/v1/gateway/files` with the token of the bridge file. No other route of the gateway answers.

What the service never does: it imports no Hermes module and no module of the full service (`service.py` and what it loads), and it runs no contract check of Hermes. It reads and changes no file under a Hermes home. Only the command of the operator `coconut team connect-chat --confirm` adds the plugin to the Hermes home of an agent, in a process of its own (`scripts/team_chat.py`), and `coconut team disconnect-chat` takes it away. It starts and stops no gateway, loads and installs no plugin and no skill, and copies no key. It runs no scheduler, no kanban adapter and no worker. It opens no tunnel listener, also with `--tunnel-port`, pairs no phone and sends no push notification. It keeps no event, because no phone reads one. Its only child process is the team helper.

An agent of a team-only Mac is a record, because the Mac scans no Hermes profile. A record has an ID, a name, an avatar and a color with the rules of a `member` entry, and the Hermes home that the agent stands for. The home is a full path, and the service keeps its real path (`realpath`). Without a home, the service takes its own: `HERMES_HOME`, else `~/.hermes`. The service only stores and compares the path. It never opens it. One home has one agent, and one name (case does not matter) has one agent, because a tool finds an agent by its name. The routes, each with the token of the bridge file:

| Request | Answer |
| --- | --- |
| `GET /v1/team/status` | `mode` (`"team"`), `version` (the commit of the checkout that runs, or null), `helper`, `bots` (the count), and each room in short form: `room`, `name`, `state`, `members` and `bots` (counts), `synced_at`, `error` and `socket`. |
| `GET /v1/team/bots` | `bots`: for each agent `id`, `name`, `avatar`, `color`, `home`, `teams` (the room IDs that it is shared with), `chat` (true while it answers in the team chat and in its direct chats, and its `bot` entry has `open` and `direct`), `plugin` (true while its Hermes home has, or may have, the team chat) `adapter` (true while the adapter of its Hermes gateway polls) and `lacking` (the fields that the Hermes of that adapter lacks for the team chat, for example `"trusted_inbound"`, or null). |
| `POST /v1/team/bots`, body `{name, avatar, color, home}` | Declares an agent. A second agent for one home, or for one name, answers 409. |
| `PATCH /v1/team/bots/<bot>`, body `{name, avatar, color, chat, plugin}` | Changes what the team sees for the agent. The home stays: a body with `home` answers 400. `chat` and `plugin` are true or false. Only `coconut team connect-chat` and `disconnect-chat` set them: `plugin` is true from before the first change of the Hermes home until a clean-up that worked, and `chat` is true while the agent answers. A change of `chat` to false stops the running turns of the agent. |
| `DELETE /v1/team/bots/<bot>` | Stops sharing the agent with each room, as `DELETE /v1/team/rooms/<room>/bots/<bot_id>` does, deletes its pages, and deletes the agent, in one transaction. The next pass writes the `delete` entries. An agent with `chat` or `plugin` answers 409 with the command to run first: the plugin in its Hermes home would stay. |

`GET /v1/team/rooms` also has `"mode": "team"`, and `mac_bots` lists the declared agents. `coconut team new-agent <name> [--home <path>]` declares an agent. `coconut team add-agent <agent>` on a team-only Mac without that agent does not guess: it prints one line that says how to declare it. On a full Mac, `new-agent` says that the Mac takes its agents from its Hermes profiles.

Pages: `POST /v1/gateway/document` saves a page as on a full Mac, but here each page names its audience. The home of the request names the declared agent, and a save that changes nothing answers `unchanged`. No person reads the Library on a team-only Mac, so each save must name `"audience": "team"`. A save without it answers 400 and stores nothing. A page of a shared agent goes to each team of the agent. A page of an agent that no team has yet stays on this Mac, and the sync writes it when the agent is shared. The limits and checks of a page are those of a full Mac. `DELETE /v1/gateway/document` with `{home, key}` deletes a page of the declared agent of the home, with all its versions. A delete needs no audience. It answers `deleted`, or `absent` when the agent has no page with this key, and then it changes nothing. The next pass replaces the `page` entry with a `delete` entry, so the other Macs drop the page from their Library. The agent stays in each team that it is shared with.

#### Cron deliveries into a team chat

A Hermes cron job of an agent with its chat connected can deliver its result into a chat of the team. The `deliver` value of the job is `coconut:` and the chat ID of `team_turns.chat_id`:

| Chat | `deliver` |
| --- | --- |
| The Team chat | `coconut:<bot ID>:team:<room ID>` |
| A named chat | `coconut:<bot ID>:team:<room ID>:<chat ID>` |
| The direct chat with a person | `coconut:<bot ID>:team:<room ID>:direct:<member ID>` |
| The Team chat or a named chat, as an alert | The value of that chat, then `:alert:<type>` or `:alert:<type>:<level>`, for example `coconut:<bot ID>:team:<room ID>:<chat ID>:alert:deploy:critical` |

`<bot ID>` is the ID of the agent on this Mac (`GET /v1/team/bots`), and `<chat ID>` is the entry ID of the named chat. A target with `:alert:` delivers the text as an alert of that type, with the level `notice` when it names none (see [Alerts](#alerts)). A type or a level that does not fit fails the delivery, and the record of the job says why. Hermes gives the whole value after `coconut:` to the plugin as the chat ID. The message is a send outside a turn, so rule 5 of [An agent answers](#an-agent-answers) and rule 10 of [The Mac for a team only](#the-mac-for-a-team-only) apply.

A cron run can deliver in a process without the gateway. There the plugin sends with its `standalone_sender_fn` (`hermes_plugin/coconut/team.py`, `send_standalone`), to the same route as the adapter: `POST /v1/gateway/messages`. The two senders answer Hermes in different ways:

- A cron delivery through the live adapter of the gateway carries the `job_id` of the job in the metadata of the send (`cron/scheduler_delivery.py`, `_live_route_metadata`). The adapter of the mode `team` sends it with the tries of `send_standalone` (`team.send_tries`). For an answer `recorded: false`, and for a service that does not answer, it gives Hermes a failed send with the reason. When no part shows, Hermes then sends the same text again (`cron/scheduler_delivery.py`, `_deliver_standalone`). In a gateway process that send goes through the same live adapter, without metadata (`tools/send_message_tool.py`, `_send_via_adapter`), not through `send_standalone`. The adapter keeps the message ID of the failed delivery for 120 seconds (`team.CRON_FALLBACK_SECONDS`). A send of the same text to the same chat in that time uses that message ID, so the service writes only the missing parts and the chat shows no second copy. Its failure is a failure too, so the record of the job shows a delivery error. Hermes then tries no third path and sends no notice into the chat. When some parts show, after any try, the failed send has `partial_overflow`, so Hermes sends nothing again and the chat shows no part two times.
- A cron delivery while a turn of the agent runs in the same chat goes outside that turn: it is a message of the agent without `reply_to`, and it does not end or change the turn.
- Each other send of the live adapter outside a turn, for example after a background process, gives Hermes success for an answer `recorded: false`, and the adapter writes the reason in its log. Only an HTTP error (for example a 404) and a service that does not answer are errors there. The full mode (a person's Mac) does not change: there `recorded: false` for a cron delivery means that a routine task shows the run already, and it stays a success.
- `send_standalone` gives Hermes an error with the reason for each refusal: a 404, an answer `recorded: false`, and an empty text. The record of the job then shows a failed delivery and why.

Hermes cuts a cron delivery over 4,000 characters in the live lane, and adds the path of a file on the Mac, unless the adapter says that it splits long messages (`gateway/delivery.py`, `_cap_oversized_output`, which reads `splits_long_messages`). The adapter of the mode `team` sets `splits_long_messages`, because the service splits a long text into parts itself. So the chat shows the whole text. The adapter of the full mode does not set it: on a person's Mac, a cron delivery over 4,000 characters through the live adapter still arrives cut. The service of the full mode takes a text of at most 200,000 characters (`gateway_bridge.MAX_TEXT`), so the same mark there would turn a longer text into a refusal.

The service answers `recorded: false` when the log does not have each part of the text. A text over 16,000 code points has more than one part. The service writes the parts in order and stops at the first part that the log does not take, so the chat shows no later part before it. This applies to both senders: before, the live adapter showed the later parts after a gap. When only some parts are in the log, the answer is `recorded: false` with the reason "The chat shows only k of n parts of this message.", because the chat does not show the whole text. A repeat with the same message ID writes only the missing parts: the entry ID of each part comes from the message ID.

A failure of the relay or of the helper adds `retry: true` to `recorded: false`, because a repeat can write the missing parts. The service writes a part again only when the relay gives no answer, up to three times. A relay that answers with an error, for example a 429 or a 503, fails the part at once. An agent that does not answer in the chat, a removed chat and a person who left give `recorded: false` without `retry`.

`send_standalone` makes up to three tries with the same message ID, with waits of 1 and 2 seconds between them (the first two retry waits of the adapter). It tries again when the service does not answer (a refused connection, a missing bridge file, a 5xx answer or a timeout), and when the answer has `retry: true`. It does not try again after a refusal. A refused connection fails at once, so the tries cover a gap of about 3 seconds, not a whole restart of the service.

Each try waits 15 seconds for the service, as long as one relay try of the service. All tries take at most 3 × 15 + 1 + 2 = 48 seconds. Hermes stops a standalone send after 60 seconds (`cron.standalone_send_timeout_seconds`), or after 30 seconds when it must run the send in a thread (`cron/scheduler_delivery.py`, `_standalone_send`). The service can take longer than one try:

- One part, with a relay that does not answer: about 46 seconds (three relay tries of 15 seconds and two waits of 0.5 seconds).
- n parts: the sum over the parts, up to about n × 46 seconds. For example, five parts at 10 seconds each take 50 seconds.

A later try waits for the lock of the room and finds the entries of the earlier try, so it gets `recorded: true` when the earlier try ends in time. When all parts together take longer than 48 seconds (30 seconds in the thread case), Hermes records a failed delivery, and the chat still shows the text later.

The team chat takes files from this path through `POST /v1/gateway/files`. See [Files](#files).
If any file fails, the sender returns an error. It sets `partial_success` when text or another file arrived.
For a file-only send, the receipt names the delivered file message.

Before the scheduler runs a job with an agent, its preflight checks each platform of `deliver` and `failure_deliver`. A platform that it does not know stops the whole job, with its other targets, for example a Telegram target. The plugin registers `cron_deliver_env_var: COCONUT_HOME_CHANNEL` in the mode `team`, so `coconut` is a known target. The block of `connect-chat` (`enabled: true`) makes `coconut` a connected platform, so the second check of the preflight also passes. A target with a chat ID needs no value in the variable.

`COCONUT_HOME_CHANNEL` is only the home chat of a bare `deliver: coconut`:

- Without a value, a bare `coconut` resolves to no chat. The job runs, and its record has the delivery error "no delivery target resolved for deliver=coconut".
- With a value `<bot ID>:team:<room ID>[:<chat ID>]` in the `.env` of the Hermes home, a bare `coconut` delivers to that chat. Then `deliver: all` also delivers to that chat. `deliver: origin` of a job without an origin delivers there when no built-in platform of Hermes has a home chat.

Coconut sets no value. A full Coconut registers no variable: the service shows a cron run as a routine task, and its jobs deliver with `origin` or a bot-chat token, which the preflight does not check.

The plugin of the Hermes home is the link `<home>/plugins/coconut` to `<install folder>/hermes_plugin/coconut`, which `coconut team connect-chat --confirm` writes (`team_chat_config.py`). An update of Coconut replaces the files of that folder and keeps the link, so each Hermes process that starts after the update loads the new plugin: also a cron process, with no restart of the gateway. A gateway that runs keeps the plugin that it loaded until its next restart.

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

A direct message (see [Direct messages](#direct-messages)). The member ID above writes to a reader. Node's `crypto` made these values, and CryptoKit gets the same values. The Python standard library gets the pair key from the shared secret.

| Input | Value |
| --- | --- |
| Member ID of the reader | The bytes `10` to `2f` |
| Agreement key of the writer (the private key) | The bytes `c0` to `df` |
| Agreement key of the reader (the private key) | The bytes `e0` to `ff` |
| Nonce of the box | The bytes `f0` to `fb` |
| Plaintext | `{"to":"101112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f","text":"Hello Grace"}` |

The room ID and the entry ID are the ones above.

- Public agreement key of the writer: `3CzKMejkO72R3_fkdcyjNH60eBB9W9dlq6SuSjDDXUQ`
- Public agreement key of the reader: `c2hF1U6H3gnWuxFKpwQsUKSgFb2ZAdGgAm9ZVlM6FRk`
- Shared secret (X25519): `28150aa20f6f6c9c8177b9deded7464ebc5aac96029777ff7503cd8e8ca7143e`
- Pair key: `46fc42722c716d505c7d6f995ea6ee8b53045f1531ad140a5a036a6a2db93f00`. The member ID of the reader is lower, so the info is `coconut room direct`, the room ID, the member ID of the reader, then the member ID of the writer.

Box, 122 bytes. A seal of the plaintext with the nonce above must give exactly these bytes:

```
f0f1f2f3f4f5f6f7f8f9fafb33e43c192dd25841a9fdffa40e61265aa37d51c1c7363a4803ccf485d7ae13e09a75e006914d5f3feeddd09ed30dc1ddaaa3f4e5311aac1732bf7070e95d0273c0e0b87ec0d6d1cbc438d7deee6138dba44205e6c8f8decba995e6ddd5e9ed63f1d46e5bc09457d158d768b49c27
```

Each implementation must open the box with the pair key of the reader and get the plaintext. It must refuse the box with another room ID, entry ID, writer or pair key, or with one changed byte.

A key box (see [The key box](#the-key-box)). The member ID above is the admin, and it seals the room key of epoch 1 for the reader of the direct message, in the entry ID above. Node's `crypto` made these values, and CryptoKit gets the same values. The Python standard library gets the pair key from the shared secret above.

| Input | Value |
| --- | --- |
| New epoch | 1 |
| Room key of the new epoch | The bytes `80` to `9f` |
| Nonce of the key box | The bytes `d0` to `db` |

- Pair key: `19f3c68ac593a603428b74650a69601b6646dd3a77ded33bdf2e22ba1d5f9b87`. The info is `coconut room key`, the room ID, the member ID of the reader, then the member ID of the admin.

Key box, 60 bytes. A seal of the room key with the nonce above must give exactly these bytes:

```
d0d1d2d3d4d5d6d7d8d9dadb2c4dba36e7ae9260e0a7363b61bb113381f5cbf96e6a3986abf2aaeeb4e46564f9f6c1eb25ceffad16bc4c1cf077c77c
```

Each implementation must open the key box with the pair key of the reader and get the room key. It must refuse the key box with another room ID, entry ID, admin, epoch or pair key, with the pair key of a direct message, or with one changed byte. The pair key of a key box must open no box of a direct message.

Link:

```
coconut://team/aeqccirdeqssmjzifevcwlbnfyxtamjsgm2dknrxha4tuoz4hu7d6aabaibqibiga4eascqlbqgq4dyqcejbgfavcylrqgi2dmob2hq7nne5eaaacvuhi5dqom5c6l3smvwgc6jomv4gc3lqnrsuk6dbnvygyzjakrswc3i
```
