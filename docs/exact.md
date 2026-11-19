# Coconut on Exact

`exact/` is the core loop of Coconut (Home and a chat) rebuilt as an Exact app. It is the first step of moving the
client to Exact, Exact Build and AppDrop. The Swift app in `ios/` and `mac/` is unchanged and still ships through
TestFlight and Sparkle. The Python service in `backend/` is unchanged.

- Exact: github.com/expo/exact at `8ed52642a2c4a6227c8abfafd80ecf501b1eb5aa` (2026-11-17). Build with that value as `exact`.
- Name Coconut, bundle id `com.gumroad.coconut.exact`. Never `com.gumroad.coconut` or `com.gumroad.coconut.mac`.
- Data: the Mac's Coconut once the phone is paired, read through the relay with the tunnel of the Swift app (see Live
  data). Before a pairing, "Look around with demo data" shows the preview fixture of the Swift app
  (`ios/Coconut/Fixtures/PreviewState.json`, copied to `exact/preview.json`) and the Gumroad team of its team preview
  (`exact/team-preview.json`).

## What is in it

| File | What |
|---|---|
| `app.contract` | The root: Home, a chat, the New project sheet, the composer and the menus |
| `parts.contract` | Parts that only draw: face, unread badge, chat row, Needs attention row, bubble, cards, working line |
| `shapes.contract` | The records `app.ts` answers |
| `app.ts` | The data module: the pairing, the connection to the Mac, the Mac's state and its cache, attachments, and the fixture for the demo |
| `tunnel.ts` | The phone's end of the tunnel (`docs/relay-transport.md`): pairing setups and the rendezvous box, Noise IK and IKpsk2 as the initiator, frames, requests as streams side by side |
| `crypto.ts` | X25519 and Poly1305 of TweetNaCl, and SHA-256, HMAC, HKDF and ChaCha20 from their specs, in TypeScript: Exact's `crypto.subtle` has no X25519 and no ChaCha20-Poly1305 |
| `app.forma` | The palette of `Theme.swift`: page, card, user bubble, warm accent, green, the red tints, the eight faces |
| `app.json`, `assets/` | Manifest (iOS and web hosts, the update channel and its signing key) and the icon of the Swift app |

Home. Needs attention at the top, with the four rules of `Attention.swift` (a card waiting over 4 hours, an agent that
cannot run, blocked work, an unseen notice of the team); the fixture alone breaches none, so `app.ts` applies what
`AppStore.applyAttentionPreview` applies for the UI test of Home. Then one flat list of chats, newest first, in two tabs
under the title with the count of each, Working and Idle (`AgentsFilter`): Working has the chats whose row shows work
(a working line or a card that waits), Idle the rest, chats with people too, and the phone keeps the choice (a
`persist` state). Each row has the layout of `ChatRowLayout`: face, name, age, a line that is the status or the last
message, the unread badge, the green dot while the agent works. At the bottom a message box that is always open, with
a picker that names the project the message goes to and ends with New project. Send starts a chat with that project.

Chat. Messages with Markdown and the time of each under it (`MessageTimes.label`), activity lines, cards with their options (an answer keeps the option chosen), files,
the notices of a team, and the working line: the phase, the time of the turn to the second, the model. The composer is
one box: a short draft is one line with the plus inside it and Send beside it. A draft over 80 characters, a newline,
an attachment, or focus opens it, with a row under the text for the plus and Send (`ComposerExpansion`). Send shows
when there is a draft. Stop shows while the agent works (`sendControls`). A draft that starts with a slash shows the
slash menu over the box (`ComposerSlash`): `/model` lists the models and `/effort` the efforts of the model, with a
mark on the one in use, and a row changes the model or the effort of every chat, as in the Swift app, where one Hermes
profile runs the chats and options have no other place on the screens (#570). Send runs the first row. The model has no
chip under the text: the Swift app removed its chips (e6c152f).

## Live data

Pairing. Without a pairing the app shows `pair <code>`, a one-time code of 16 random bytes, and makes the rendezvous box
of the code on the relay (`docs/relay-transport.md`, "Pairing code from the iPhone"). The person sends that line to
Coconut on the Mac, whose agent runs `coconut pair <code>`, which seals a setup into the box. The app opens it, pairs
with Noise IKpsk2 and `POST /v1/pair` (device name "Coconut on Exact"), and keeps the Mac's route and key, its own key,
the device token and the relay's client token in the Keychain (`secret.keep coconut-pairing`). A link of the Mac
(`coconut://pair/...`) would open the Swift app, so the Exact app takes the code. "Look around with demo data" shows the
fixture instead.

Connection. Exact's sockets send no headers, so the relay gives a socket ticket for the client token
(`POST /v1/tunnels/<route>/client-ticket`, one use, 60 seconds) and the app opens `/client?ticket=`. A round is one
socket: `prepare` gets the ticket, `socket` is the stream resource, and `sync` does the handshake and the reads. The
socket's mapper only queues bytes and wakes the answer that waits; the handshake and the frames run in answers that
`await scheduler.yield()` between steps. A waiting answer goes on only when another answer is asked, so `deliver` is
asked for each event of the socket. An answer may send on the socket only in its own turns, which Exact restores after
an `await`: chain requests with `await`, never with a plain `.then`.

Reads. The first read is the whole state, `GET /v1/state` (recent, gzip: about 150 KB on the wire for 620 KB of
state). Each read after it is `GET /v1/updates?after=<cursor>&wait=20` (`X-Coconut-Updates: 4`, as the Swift app
sends it): the Mac holds it until something changes or 20 seconds pass, and the app merges what changed into the state
it keeps, as `StateUpdates.applying` does (bots whole, tasks, messages, cards and files by id, the removed ones out, or a
whole state in `reset` when the Mac cannot list the changes, for example after a new chat). The next read starts as the
last one ends. The state outlives a round: the first read of a
new socket asks only for the changes since its cursor, without waiting, so a round after a failure clears the banner
at once. A round whose socket closed or whose read failed gives way to a new one after 15 seconds. No read is asked
again while it runs, which would let it go with its timeout, and none starts while an update waits.

Requests side by side. `tunnel.ts` sends each request as its own stream, as the Swift app does, so a Send or an answer
goes while a read of updates waits on the Mac. Both keys number their messages, so one answer at a time seals and one
reads: the reader opens the next message and hands its frame to the request of its stream. Each request waits for
each frame for its own time, 20 seconds, or 45 for a read of updates; one that runs out sends CANCEL and fails alone,
and an ended socket fails them all. A write no longer asks for a read after it: the change it makes ends the read that
waits.

What a chat of the Mac does here: its messages, cards and files, the working line, Send with words and files, answers
to cards, Stop and read marks, and `/model` and `/effort`. Those two send `PUT /v1/settings/model` (model,
provider and effort, as `ModelOverride`), which changes the model of every chat of the Mac, and take the catalog of its
answer. The app reads the catalog and the chats that have a model of their own from `GET /v1/state/settings` with the
first state, again when an update says the settings changed (or brings a whole state), and at most once a minute, as
the Swift app does; the working line names the model of the chat. A Mac that refuses a model says why in the chat. It
does not make projects (they are fixed), show team chats, or get push yet.

Attachments. The plus of the composer offers Attach file and Photo library, Exact's pickers, which copy each file into
`app:/tmp/picked`. Send shows the message at once (`queueMessage`), with a row for each file, and then sends it
(`sendMessage`): each file goes up with `POST /v1/attachments` (the agent, the name and the bytes in base64, with its own
idempotency key, as `AppStore.uploadAttachment`), and the message names them in `attachment_ids`. Files alone go with
the text `[attachment]`, which the chat does not show (`AttachmentOnly`). A file of the file picker goes as it is, from
1 byte to 4 MB. A photo goes as `FileFit` sends it in an agent chat: as it is when it fits 2400 pixels and 4 MB and is
no HEIC or HEIF image, a GIF always, and any other photo as a JPEG of at most 2400 pixels and 4 MB at quality 0.8
(`storage.fs.compressImage` into `app:/tmp/upload`, removed after its upload). The base64 goes straight into the bytes
of the body, part by part with a pause between, so a file of 4 MB makes no string of 5.6 MB. The rows of a message
stay (`outgoing`) while new states of the Mac come, until a state shows it. The Mac answers a message with its task,
not the message, so a state shows it when it holds a message of the person with that text and an id that the chat did
not have at Send; a state that comes before the answer of the send does too, so no row shows twice. A send that went
and that two states do not show goes with the third. A send that fails says why under its rows until the next send of
the chat. Reading the files needs the grants `fs.read app:/tmp/picked`, `fs.read
app:/tmp/upload` and `fs.write app:/tmp/upload`, so attachments need a new binary: build 5 keeps its last update.

Cache. The phone keeps the last state of the Mac in SQLite (`app:/data/coconut.db`, one row of the table `snapshot`:
the route of the Mac and its state as JSON), as the Swift app keeps `snapshot.json`. The first `sync` of a launch with
a pairing reads it before the socket opens (three times in all while the storage does not answer), so Home and the
chats show at once, also without the Mac, with "Connecting to <Mac>…" or the reason that the relay or the Mac does not
answer until it does (a ticket of the relay that fails says why with the next answer of `sync`). Sends then fail with
that reason. The first read of the launch still takes the whole state, which replaces the kept one, so a kept state
never grows with the updates of earlier launches; the Swift app goes on from the kept cursor instead. While changes
stream, the state is written at most every 10 seconds of the Mac's clock, because a data module reads no clock, and a
read that brings no change writes what is left. The first write for another Mac removes the state of the last one, and
a phone that is no longer paired removes it with the pairing. An open database locks its file, so the uses of the cache
wait for each other. The grant `sqlite.open app:/data/coconut.db` needs a new binary, as the grants of attachments do.

Checked. Every primitive of `crypto.ts` against `node:crypto` with random inputs (SHA-256 and HMAC at every length to
200 bytes and beyond, HKDF, ChaCha20-Poly1305 both ways and a changed byte, X25519 keys and secrets), the test vector of
the rendezvous box, a version 3 link, and a rekey. `tunnel.ts` under bun against the live relay and this Mac: box,
`coconut pair`, IKpsk2, pairing, IK, the state. The web build of the app (`bun exact.mjs web`) in Chromium against the
live relay: the code, the pairing, Home with the Mac's 17 chats, a chat, and a launch again with the kept pairing.
Chromium ran without web security for that check, because the relay sends no CORS headers. Not checked: a phone,
whose Hermes may be slower than this Mac (without a JIT, here: an X25519 in 17 ms, a 60 KB frame in 17 ms), and Send,
answers and Stop against the Mac, which would have written in the person's chats.

The requests side by side, under bun against a Mac in memory with real ciphers, so a message opened out of order fails:
a write answered while a read of updates waits, two answers of 1.5 MB at once with their window grants, a write and a
reader that each run out of time alone (the write sends CANCEL), an ended socket that fails every request, a ping, an
upload of 2.5 MB beside a read, and eight requests at once. The merge of updates against the service of main in
fixture mode (an empty data folder, no relay): a state, then a new chat and its turn read as updates (one page was a
`reset`), gave the same messages, bots, cards, files, task statuses and chats as a fresh state. Not checked: the reads
in Exact itself, on the web host or a phone (the disk had 7.4 GB free), and the socket of the relay.

`/model` and `/effort` under bun against the fixture: a slash alone offers both, `/m` completes to `/model `, the models
and the efforts of the model list with a mark on the one in use, a name filters them, a model brings its own default
effort, an effort the model lacks is refused, and the working line names the new model. Checked by reading: the body
of `PUT /v1/settings/model` against `Service._model_override` and the Swift `ModelOverride`, and the catalog against
`ModelCatalog`. Not checked: the route on a Mac, because the service in fixture mode has no catalog and the person's
Mac would have changed the model of every chat.

Attachments under bun against the service of main in fixture mode, with a stand-in of the tunnel that sends each
request to it and a storage in memory: the body is JSON whose base64 is the file at every length around a part, words
and a PDF of 1.5 MB show at once and keep their rows while a new state comes, the Mac keeps the same bytes, files alone
go with the hidden marker, a HEIC photo of 4032 pixels goes as a JPEG through `compressImage` with 2400 pixels, 4 MB and
0.8 and its copy goes away, a JPEG of 1200 pixels and a GIF of 3000 go as they are, a file over 4 MB and 21 files do not
go and the chat says why, the rows stay until a state shows the message, a state that comes before the answer of the
send shows it once, a send that states do not show goes with the third, and the demo names the files. Not checked:
Exact's pickers and `compressImage` on a phone,
and the time an upload of 4 MB takes there (base64, ChaCha20-Poly1305 in TypeScript, the relay).

The cache under bun against the service of main in fixture mode, with bun:sqlite in place of Exact's SQLite and its
lock (a second open fails while one is open): the first state is kept for the route of the pairing, a change within 10
seconds of the last write waits and a read with no change writes it, a launch without the Mac shows the kept chats at
once with Connecting and a chat with its messages and times, a Mac that does not answer keeps them with its reason,
the first read when the Mac answers is the whole state and Connecting goes, another Mac shows nothing of the first and
its first state replaces the row, a phone that is no longer paired keeps no state, a damaged row shows nothing, a read
of the cache that fails is tried again, and no use of the database met another. Not checked: Exact's SQLite on a phone, and the time the JSON of a real state takes
there (620 KB for this Mac's, against 2 KB in the fixture).

On a phone. Gianfranco installed build 0.1.0 (5) from AppDrop on his iPhone on 2026-11-19 and reported the steps of
issue #573 (the pairing, his chats, a chat, Send, a card, Stop and a relaunch) as done. AppDrop's sign-in to Apple
failed twice before that with "Couldn't sign in to Apple", and an app-specific password got the install through. The
first update of that build's stream titles the list of Home Chats, as the Swift app does; it checks that updates reach
a phone. It reached his iPhone, but the title read Chats only after he quit the app and opened it again. An update
that carries `app.ts` applies only when no request runs, and the 15-second read was asked again whether or not the
last one had ended: asking again lets the last read go with its timeout, so a socket that died while the phone was in
the background kept a read running for good, and the update waited for the next launch. Now `syncing` starts no read
while one runs or while an update waits (`exactDelivery().staged`), and the number of the running update is part of
the round, so the new data module of an applied update opens its own socket instead of waiting on the old one's.
Gianfranco loaded that fix with two launches. The next visible update, the tabs Working and Idle under the title
(7fa2af5), applied while the app stayed open: the tabs showed up in the first minute, without a relaunch or a switch
to another app, and his chats and the pairing stayed. That ended the check of build 5 on a phone (#573, closed).

Build 0.1.0 (19), built by the workflow on 2026-11-19, adds the grants of attachments and the cache, so it is a new
binary with a stream of its own (`e06cea81`), whose first update carries main at f7d0ad1. Build 5 cannot take a module
with those grants and keeps its last update (f434da9: the reads of updates, `/model` and `/effort`). Build 19 is on the
AppDrop link, and its check on a phone is #575: the install over build 5, a photo and a file, `/model` and `/effort`,
and a launch in Airplane Mode.

## What was checked, and what was not

Checked by running. `app.ts` under bun against the fixture: every answer is exactly its declared shape, the sources
and their argument counts are the ones `app.contract` calls, and the behaviour holds (four Needs attention rows, seven
chats newest first, a message starts a chat, a second one waits for the next turn, Stop, an answered card, model and
effort, a new project takes its letter). The check is a scratch script, not in the repository, which has no tests.

Checked by reading. The contract files against the tables of Exact (`kernel/tables/schema.json`: attributes, roles,
fonts, colours, button styles, symbols) and against idioms taken from its patterns and apps.

Built. Exact Build compiled the app on 2026-11-17 (build `166a3e29`): a 19.4 MB IPA, published with AppDrop from the
Mac that holds the update key (`npx appdrop links` there prints it). The first upload failed in the bake with 19
errors, because Exact Build compiles with the newest snapshot of its catalog, not the commit sent as `exact`. See Gaps.

Not done:

- No screenshots, and no run on a phone by an agent.
- The local web host was not run: the disk had 3.6 to 7.7 GB free (the rule is 10 GB).

## Build

Exact Build compiles in the cloud and returns the diagnostics of the compiler as JSON. Do not build Exact hosts on the
shared Mac (`bun exact.mjs share`, `ios`, `mac`, `web`, `verify` and `setup` build Rust and Xcode workspaces).

```sh
C=8ed52642a2c4a6227c8abfafd80ecf501b1eb5aa
H="Authorization: Bearer $(cat ~/.exact-build-token)"     # never print it, never run with set -x
tar -czhf /tmp/coconut-exact.tar.gz -C exact .             # 0.99 MiB; app.contract at the root; -h follows links
ID=$(curl -sS -X POST "https://exact.appdrop.app/build?exact=$C" -H "$H" -H 'Content-Type: application/gzip' \
  --data-binary @/tmp/coconut-exact.tar.gz | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
curl -sS "https://exact.appdrop.app/$ID/" -H "$H"          # status, steps with times, diagnostics {file,line,col,id,severity,message}
curl -sS -o Coconut.ipa "https://exact.appdrop.app/$ID.ipa" -H "$H"
npx appdrop Coconut.ipa                                    # prints only the share link; keep the update link private
```

The budget is 15 minutes of worker time an hour, 60 a day, 4 hours a week and 4 builds at once; `GET /usage` shows it.
`base=<id>` reuses the binary of the last successful build of the same app when no native code changed, which Exact Build
says takes about 4 seconds. Fix what `diagnostics` lists and build again.

AppDrop signs each copy with the Apple ID of the person and gives it a bundle id with a suffix. A copy gets no push from
the Coconut server, no app extensions (share, notification service, widgets) and no Sign in with Apple. TestFlight stays
for the real app; AppDrop is for previews.

## Hot reload

The target of Exact is 100 ms from saving an edit to seeing it, on the web host and the simulator. `app.json` sets
`dev.rebuild.typescript` to `save`, which is the default. Three loops exist; only the second was timed (Numbers):

1. Web host (`bun host/web/dev.mjs`, or `bun exact.mjs web`): the fast one. It needs the Rust toolchain and a compile, so
   it needs a machine with 10 GB free. `scripts/agent.mjs` takes screenshots of it.
2. Updates to a phone, over the air: the workflow `Exact updates` (`.github/workflows/exact-updates.yml`). `build`
   makes a release binary with an update store on GitHub's `xcode-27` runner (an IPA in the artifact
   `coconut-exact-ipa`, which goes to AppDrop with `npx appdrop app.ipa --update <link>`) and publishes nothing.
   `push` publishes the Contract, the assets and `app.ts` to every installed binary that can take them, which takes
   them the next time the app comes to the foreground (`deploy.activate` is `immediate`) and no request runs: the app
   starts no read while an update waits, and a launch boots one that could not apply; `check` prints what `push`
   would publish and publishes nothing. A stream is one binary's line of updates (a compatibility id). The first push
   after a build starts the stream of the new binary from its build receipt (`receipt.json` in the IPA, taken from the
   newest artifact), and later pushes find it on the origin. Its first update reloads the app once, as every update
   does, so the build leaves that to the next push and a first launch can pair undisturbed. A binary built before a
   grant cannot take a module that declares it, so it keeps what it has; Exact's own `push` then refuses the whole
   update and `deploy` never starts the new binary's stream, so CI applies the patch in `.github/exact-patches`, which
   leaves such a stream as it is. Check whether Exact still needs the patch when `EXACT_COMMIT` moves.
   The channel is `"appdrop"`, AppDrop's origin for the key. The private key is the secret `EXACT_SIGNING_KEY` of the
   environment `exact-updates`, and on the Mac that made it `~/.config/exact/keys/coconut.pem`; the public half is in
   `app.json`. Exact Build cannot make such a binary: it builds development binaries, which never check the channel
   of `app.json`, and a release build of this Exact needs Xcode 27.1, which this Mac lacks (26.6).
3. Rebuild and `npx appdrop --update`: a build of about 4 seconds with `base`, plus the upload and the install.

## Numbers

Measured on 2026-11-17 with snapshot `exact-2026-11-14T0909Z`:

| Build | Worker time | Wall time | Steps |
|---|---|---|---|
| First successful build | 33.7 s | 86 s with polling and the IPA | cache restore 4.3, cargo 12.8, swift 6.5, package 7.1 |
| Rebuild with `base=<id>`, same code | 6.6 s | 11 s with upload and IPA | |
| A build that fails in the bake | about 7.5 s | 10 to 20 s | free for the first 30 minutes a week |

An update over the air (loop 2), measured on 2026-11-19 with the update that added Working and Idle: 14 minutes from
`gh workflow run` to the publish (the publish itself took 18 s), then it showed on Gianfranco's iPhone within a minute
of opening the app, without a relaunch.

Not yet measured: the time from save to screen on the web host, and a `push --watch` update.

## Gaps found

- Exact Build compiles with the newest snapshot of its catalog, and the catalog lags main: on 2026-11-17 it listed main
  of 2026-11-14, and the build status said so in `sdk.note`. That snapshot refuses a Forma token name as a colour
  (`background-color="surface.card"`), and has no `"section"` style for `formatDay` and no `"month-day"` style for
  `formatDate` (`"medium"`, `"month-year"` and `"iso"` only). So the rows write the values of `app.forma` out. The
  binary that takes updates comes from the workflow `Exact updates` with the pinned Exact, which has `"month-day"`,
  so a date older than a week reads "Nov 10" again; an upload to Exact Build fails on it until its catalog is newer.
- Exact Build builds iOS only. The Mac app needs a Mac with Xcode and Rust (`bun exact.mjs mac`).
- Its manual lists `widgets`, `activities`, `intents` and `alarms` as `extensions`. The notification service and the
  share extension are not named there, so whether the cloud build includes them is unverified.
- The generated `exact/AGENTS.md` tells agents to run `bun exact.mjs share`, which builds natively and locally. On the
  shared Mac use Exact Build. Edits to that file need approval, which did not come, so the note is here.
- `exact/exact.mjs` names the bun of this Mac (`/opt/homebrew/Cellar/bun/1.4.4`) in its fallback. `bun exact.mjs <verb>`
  works anywhere bun is on the path. The Exact checkout is found at `../../exact` (beside the repository) or by `EXACT`.
- `symbol:sf/<name>` draws blank on the web host (a warning). Eight are used: `cpu`, `brain`, `paperclip`,
  `circle.inset.filled`, and `hourglass`, `bolt.slash.fill`, `exclamationmark.octagon.fill`, `megaphone.fill` for the
  rows of Needs attention.
- `preview.json` is a copy. An app folder cannot read outside itself, so it must be copied again when the fixture
  changes. The data module reads the fixture's `task_id` links, `updated_at` of a working task (there is no
  `turn_started_at`) and the bots with `chat_of`.
- The picker of the Swift message box (`NewChatBar`) starts with All projects and ends with Open and All agents. This one
  lists the projects and ends with New project, as asked. A message from Home always starts a new chat with the project.
- Not ported: the slash commands other than `/model` and `/effort` (new, compact, stop, mute, help), mentions, the access menu, editing a queued message, link and GitHub previews, photos,
  the schedule of a message, the fifth rule of Needs attention (stale data, which a preview has none of).
- The pairing code lives in the memory of the data module. An update that applies while a phone pairs reloads the app,
  which then shows a new code, and the Mac seals its setup for the old one; send the new code. Keeping the code in the
  Keychain until the phone pairs would end this.

## Plan for the rest

The Swift app is 36,371 lines in `ios/Coconut`, 901 in `ios/Tunnel`, 207 in `ios/PushShared` and 4,652 in `mac/Sources`.
Keep it shipping until each phase reaches parity, and keep the bundle id of the Exact app different until the last one.

| Phase | Swift to port | Exact |
|---|---|---|
| 1 Live data | `APIClient`, `AppStore` (4,102), `Models`, `TunnelConnection`, `Onboarding`, pairing | Done: the pairing, the tunnel, the state and its updates, the model and effort (`/model`, `/effort`), attachments and the SQLite cache for a launch without the Mac are in `app.ts`, `tunnel.ts` and `crypto.ts` (see Live data). Attachments and the cache need the binary of their grants |
| 2 Conversation | `ConversationView` (1,642), `Cards`, `MessagePhotos`, `MessageQueue`, `BotMentions`, `ModelSettingsView`, `LiveView`, previews | More parts and routes in `parts.contract`; photos by the file picker and `image`; a virtualized list for long chats |
| 3 Team | `TeamChat` (2,093), `TeamChatView` (1,710), `TeamViews`, `Team`, `PeopleView` | A second family of chats and a notice board, over the same list |
| 4 Business and library | `BusinessDashboard` and the other Business files, `Library`, `DocumentPage`, `UsageViews`, `DecisionInboxView`, `AgentProject`, `Bento`, `ChatDeckView` | Routes and tabs (`select(nav, name)`); Markdown for documents; charts as `svg` |
| 5 Release | Bundle id, signing, TestFlight | Name the app `com.gumroad.coconut` so it replaces the Swift app; sign the IPA on CI as `testflight.yml` does (Exact has an EAS and a CI recipe); keys and a channel for updates |

Native parts:

| Part | Swift today | On Exact |
|---|---|---|
| Noise tunnel | `ios/Tunnel` (Noise IK and IKpsk2, 25519, ChaChaPoly, SHA-256; frames of `TunnelFrames`), `URLSessionWebSocketTask` to the relay | TypeScript in the data module (`tunnel.ts`, `crypto.ts`): a stream resource for the socket under `net.websocket`, the same frames. If a phone finds it too slow, a Swift `ExactModule` in `modules/apple/` with the same CryptoKit code. The Mac sidecar keeps `Noise.swift` unchanged |
| Push | `PushNotifications.swift`, APNs from the service | `@exact/push`: `subscribePush()` and `pushSubscription()` give `{apple: {deviceToken}}`; the app sends it to the service, which sends APNs with `apns-topic` `com.gumroad.coconut.exact` (then the final id) and a top-level `navigate`. Grants `device.notifications purpose.notifications` and `device.push`. Needs the App ID with Push and a key valid for the topic |
| Notification service | `CoconutNotificationService`, `PushShared` (207) | Swift in `modules/apple/notification-service/NotificationService.swift`, with the `PushShared` files copied there (links are refused). It runs no Contract or TypeScript. It shares the App Group and the Keychain group of the app, so it can read the key the app keeps |
| Share extensions | `CoconutShare`, `ShareShared`, `CoconutMacShare` | `share_target` in `app.json` derives an Apple share extension (presentation `compose` or `confirmation`); a route such as `/share` reads `searchParam(entry, "text")` and `sharedFiles(entry, "files")`, and the app asks for the project. `apps/fieldnotes` does it. The extension of the Mac is not checked |
| Mac menu bar | The status item of `mac/Sources` | A `button` with `-exact-menu="status"` is the status item; its `popovertarget` menu is the menu |
| Sparkle | `mac/Sources/CoconutApp/Updater.swift`, the feed in `updates/` | Exact updates the Contract, assets and `app.ts` of an installed Mac app with signed updates (`deploy.store.macos`). A new grant or native code still needs a new binary, so Sparkle or a notarized download stays for those |
| Live Activities | `LiveActivity.swift` | The `activities` extension of Exact Build (not tried) |

Decisions the first builds should settle: whether Exact Build takes `deploy.store.ios: "A"`, whether it includes the
notification service and the share extension, and what a build of this app costs in worker time.
