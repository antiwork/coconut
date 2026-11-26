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
| `parts.contract` | Parts that only draw: face, unread badge, chat row, Needs attention row, the rows of a chat (bubble, quote, sender, day, New line, time, photo tile, file, Library page, background process, notice), the card with its options, draft, Undo and receipt, and the working line |
| `shapes.contract` | The records `app.ts` answers |
| `app.ts` | The data module: the pairing, the connection to the Mac, the Mac's state and its cache, attachments, and the fixture for the demo |
| `tunnel.ts` | The phone's end of the tunnel (`docs/relay-transport.md`): pairing setups and the rendezvous box, Noise IK and IKpsk2 as the initiator, frames, requests as streams side by side |
| `crypto.ts` | X25519 and Poly1305 of TweetNaCl, and SHA-256, HMAC, HKDF and ChaCha20 from their specs, in TypeScript: Exact's `crypto.subtle` has no X25519 and no ChaCha20-Poly1305 |
| `app.forma` | The palette of `Theme.swift`: page, card, user bubble, warm accent, green, the red tints, the eight faces |
| `app.json`, `assets/` | Manifest (iOS and web hosts, the update channel and its signing key), the icon of the Swift app, and the two charts of the demo (`assets/demo`) |

Home. Needs attention at the top, with the four rules of `Attention.swift` (a card waiting over 4 hours, an agent that
cannot run, blocked work, an unseen notice of the team); the fixture alone breaches none, so `app.ts` applies what
`AppStore.applyAttentionPreview` applies for the UI test of Home. Then one flat list of chats, the chats with an
unread message first and then the newest (the person, 2026-11-25: "Put unread chats at top"), with no tabs (the person
found that nobody taps them, 2026-11-24): working and idle chats, chats with people too, and the archived chats of the
Mac (the person merged Idle and Archived, so an archived chat sits among the others by its newest message, with no
Archived row). A muted chat counts no unread, as in the Swift app (`BotChatMute.unread`): it shows no badge and does
not move up. Each row has the layout of `ChatRowLayout`: face,
name, age (for a chat that shows its step, the time of that step to the second), a line that is the status or the last
message, the unread badge, the green dot while the agent works. At the bottom a message box that is always open, with
a picker that names the project the message goes to and ends with New project. Send starts a chat with that project
and leaves it closed: the chat is the newest row of the list, and the box keeps the keyboard for the next one (as
`NewChatBar` of the Swift app).

Chat. The rows of `TranscriptMessageRow`, in the order and with the rules of `TranscriptPresentation`: a message with
Markdown, a photo, a card, a file, a page of the Library, a background process of Hermes that ended, an activity line
and the notices of a team, each with its time under it (`MessageTimes.label`), and the working line: the phase, the
time of its newest step to the second, the model. The header names the chat itself (`Bot.chatLabel`). The day shows
in a pill above the first row of each day (`DayPill`), and Slack's green New line goes above the first row of an agent
that came since the person last read the chat, at the read mark that the chat opened with (`UnreadMark`). The
person's own rows never take it, and the words that the person writes after the mark read the chat, so no row after
them takes it either. A message between
agents names its sender and the others (`Gumclaw → Coconut`), a message of the person names the agents it went to, and
an answer quotes the message or the page that it answers (`MessageQuote`). Photos and cards are described below
(Photos and cards). The composer is
one box: a short draft is one line with the plus inside it and Send beside it. A draft over 80 characters, a newline,
an attachment, or focus opens it, with a row under the text for the plus and Send (`ComposerExpansion`). Send shows
when there is a draft. Stop shows while the agent works (`sendControls`). A draft that starts with a slash shows the
slash menu over the box (`ComposerSlash`): `/model` lists the models and `/effort` the efforts of the model, with a
mark on the one in use, and a row changes the model or the effort of every chat, as in the Swift app, where one Hermes
profile runs the chats and options have no other place on the screens (#570). Send runs the first row. The menu shows
only while the draft starts with a slash, so a chosen model or effort, which empties the box, closes it. The model has no
chip under the text: the Swift app removed its chips (e6c152f).

## Photos and cards

Photos (`MessagePhotos.swift`). A message of an agent that is one Markdown image, `![caption](address)`, with a web
address or a file of the Mac (`coconut-file://<artifact id>`), shows the photo, as does the older form (an image
address with a caption line above it) and an image that the person attached. Photos that one agent sent one after
another on one day are one row. One photo is a tile of 280 by 180 points on its side of the chat; more are tiles of
236 that scroll sideways, each with its caption under it and a hairline around it, so a white picture keeps its edge
on the white page (the Swift tile has none). A tap opens the photos full screen on black, with Close, the
caption, which one is in view and the next and the last one (`PhotoViewer`, without its zoom and swipe). The row of
Home reads "Photo: caption". A file of the Mac comes through the tunnel (`GET /v1/artifacts/<id>/content`), one in
each answer of `loadMedia`, the newest first, while the chat misses some and the socket is open. The phone keeps a
tile of 840 pixels and a copy of 2400 for the viewer in `app:/tmp/upload/photos`, which the grants of build 19 name,
and finds them there after a launch without a download; past 400 files the first look of a launch keeps only the open
chat's. A photo shows the spinner while it loads, and "The photo did not load." when the Mac refuses the file or it is
no image the phone reads; the next opening of the chat tries again. A photo that the phone sends keeps its copies for
the file it becomes on the Mac, so it never loads back. In the demo the photos are the charts in `assets/demo`.

Cards (`Cards.swift`). A card has the earlier choice of a follow-up, its title, its sources (kind and title, no link
yet), the size of a pull request to merge, and an X that dismisses it without an answer. While it waits it shows its
context in paragraphs, its risk (or "Assessment needs updating"), its previews as photos, a draft's Via, From, To,
Reply to, Subject and body (`MessageDraftContent`) with Send and Discard, or its options with the recommended one first
and marked, each with what it does and its risk. An option that approves waits until the previews have loaded. A
choice shows at once with Undo for 10 seconds (`AnswerUndoRow`, `answerUndoSeconds`), and only then goes to the Mac
with the card's revision and the hashes of the draft and the previews that the person saw (`AnswerRequest`); while it
goes the receipt has an arrow, and the receipt of an answered card has the choice with a check
(`DecisionSelection`): "Approved for sending" or "Discarded" for a draft, "Could not finish" with the reason for one
that failed, Dismissed, or what the Mac closed it with. Why a choice or a dismissal did not go shows under the card.
The demo answers at once, as the preview of the Swift app does. A card that the person answered in words leaves the
chat, and the answer quotes it.

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

Answers that Exact stops. Every answer shares the one session, and Exact can stop an answer on its way: a newer send
of a mutation that keeps the newest reply, or a resource asked again, lets the answer in flight go, whose pauses then
fail and whose sends throw, and the runner takes back what a turn sent when that turn went over its 100 ms. Nonces count
up strictly, and the Mac closes a session at a skipped one. Until build 19's second update, a write let go beside the
read of updates sealed its CANCEL, which took a nonce and never left, and a reader let go dropped a message it had
taken: either way the Mac closed the session, and every write until the next round, 15 seconds later, said "Your Mac
is not connected." That fits #575, where each `/model` and `/effort` on a phone said so and the connection went on and
off. Now a message is sealed on a copy of the send cipher and the session moves on only once it went, in a turn of its
own after the work that made it; a reader let go puts its message back for the next one, and only a message that does
not open ends the session; the writes are `queue` mutations, so a second tap waits for the first; a write waits up to
25 seconds for the next round instead of failing at once; and a round whose session read the Mac gives way to the next
after 3 seconds (`live.quick`), not 15. A page of updates no longer sorts every message of the Mac, and `fromSnapshot`
groups the messages by chat in one pass: a page of 900 messages took 0.8 ms under bun, against 1.6 ms on main, which a
phone without a JIT pays several times over on its main thread.

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
(`storage.fs.compressImage` into `app:/tmp/upload`, removed after its upload). Exact lets one answer send 1 MiB on a
socket in all its turns (`socket answer exceeds 1 MiB`), and a message and its files go in one answer, so the files of
a message take about 750 KB in all, as base64 in frames: a photo becomes a JPEG that fits what the message leaves, and
files that do not fit stop the message before anything goes, with "The files of one message can take about 750 KB in
this app." Before that limit was known, a larger upload failed in the middle and broke the session. Sending a file in
parts over more answers would lift it. The base64 goes straight into the bytes
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

The answers that Exact stops, under bun against a Mac in memory with real ciphers that closes a session at a message
that does not open, each request an answer of its own (AsyncLocalStorage keeps whose turn runs after an await, as
Exact's async helper does), whose pauses reject and whose sends throw once it is let go: a reader let go before the Mac
answers, a write let go while it waits beside the read of updates, and a message let go while it opens. On main the
request after each failed ("A message from the Mac did not decrypt.", "The connection to your Mac ended.") and the Mac
closed the session at a skipped nonce, or the session broke; with the fix each request after them went and the Mac kept
the session. Also under bun: a page of 900 messages shows the same chats as on main, a write that finds no connection
goes once the next one opens, and the files of a message stay within 1 MiB (two files of 700 KB in all go, 900 KB stop
before anything goes, a HEIC photo becomes a JPEG that fits beside a file). Not checked: Exact's runner itself (its
let-go answers and overruns on a phone), the quick round of `retryingSoon`, and the limit of 1 MiB on a device.

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

Gianfranco installed build 19 over build 5 on 2026-11-20. A photo and a file with words reached his agent, and a
launch in Airplane Mode showed his last chats with the reason at the top. `/model` and `/effort` listed his Mac's models
and efforts with the mark on the one in use, but each choice said "The model did not change: Your Mac is not
connected.", the connection went on and off, and the app dropped frames while he moved around in it. The cause was the
one of "Answers that Exact stops" under Live data, as far as a check without a phone shows (a check under bun makes
the same failures on main), and its fix is the next update of the stream; the frames dropped are the data module's work
on the main thread (placement `main`, which a worker cannot replace, because a worker has no socket to send on), of
which the reads of a page now do less.

That fix reached his iPhone as the second update of the stream (seq 2), and on 2026-11-20 a model he chose with
`/model` took effect on his Mac. The menu then stayed open, with the mark on the old model, until he started the app
again. A resource whose `when` turns false keeps the answer it showed (Exact's `when`, and its test
`closing_lets_go_of_the_request_and_keeps_the_value`), and the menu showed whenever `slash` had rows: a chosen model
empties the box, which closed the gate of `slash` and left its rows on the screen, and Send would then have run their
first row in place of sending the words. Now the menu and Send read the rows only while the draft starts with a slash,
an empty draft asks `suggest` once more, which empties them, and a model or an effort that the Mac took asks the menu
again, so a `/model` typed before the Mac answered moves its mark. It went out the same day as seq 3. Checked by
reading, against Exact's rules and tests of `when` and `current`; not checked: the menu itself in Exact, on a phone or
the web host.

Gianfranco did not report on seq 3 before #575 closed on 2026-11-24, which ended the check of build 19 on a phone. Not
checked there: the menu after a pick, and whether the app still drops frames.

## What was checked, and what was not

Checked by running. `app.ts` under bun against the fixture: every answer is exactly its declared shape, the sources
and their argument counts are the ones `app.contract` calls, and the behaviour holds (four Needs attention rows, seven
chats with the two unread ones first and then the newest, a muted unread chat of a state of the Mac with no badge
among the read ones, a message starts a chat, a second one waits for the next turn, Stop, an answered card, model and
effort, a new project takes its letter). The check is a scratch script, not in the repository, which has no tests.

Checked by reading. The contract files against the tables of Exact (`kernel/tables/schema.json`: attributes, roles,
fonts, colours, button styles, symbols) and against idioms taken from its patterns and apps.

Built. Exact Build compiled the app on 2026-11-17 (build `166a3e29`): a 19.4 MB IPA, published with AppDrop from the
Mac that holds the update key (`npx appdrop links` there prints it). The first upload failed in the bake with 19
errors, because Exact Build compiles with the newest snapshot of its catalog, not the commit sent as `exact`. See Gaps.

Phase 2, 2026-11-24. `app.ts` under bun (scratch scripts again): the demo's chats, and a hand-made state of the Mac
with each kind of row (photos of one agent grouped, a new day and the New line, a draft card with its earlier choice,
a merge card, a failed card, background processes, a Library page, an attached image, a message from another agent,
quotes) and the rows a chat leaves out; a choice with Undo, a second choice refused, and a choice that cannot reach the
Mac, which says why after the Undo and the 25 seconds that a write waits for a connection; and the photos of a chat
from a stand-in tunnel and file system (the newest first, one per answer, a refused file, a copy kept from an earlier
launch found without a download). The Contract compiles with the pinned Exact, and `app.ts` passes `tsc --strict`.
The local web host showed the demo at 390 by 844 points, light and dark: the day pill, the New line, a photo row and the
viewer, the draft card with Send and Discard, a card with its source, risk, preview and recommended option, its receipt
after a choice, a background process opened, and, with a temporary edit of the demo, the Undo row, a photo that loads
and options that wait for a preview.

Not done:

- No run on a phone by an agent. A photo from a file of the Mac and an answer to a card on the Mac are checked with
  stand-ins only, until a phone takes the update.
- The web host ran once, for phase 2, with 17 GB free; its toolchain was deleted afterwards.

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
the Coconut server and no app extensions (share, notification service, widgets). TestFlight stays for the real app;
AppDrop is for previews.

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
- `symbol:sf/<name>` draws blank on the web host (a warning). Twelve are used: `cpu`, `brain`, `paperclip`,
  `circle.inset.filled`, `hourglass`, `bolt.slash.fill`, `exclamationmark.octagon.fill`, `megaphone.fill` for the
  rows of Needs attention, and `checkmark.circle.fill`, `xmark.circle.fill`, `chevron.down` and `arrow.up` for a
  background process, an Undo and a choice on its way. The rest are roles, which every host draws.
- `preview.json` is a copy. An app folder cannot read outside itself, so it must be copied again when the fixture
  changes. The data module reads the fixture's `task_id` links, the phase, detail and `phase_started_at` of a working
  task (else its `updated_at`) and the bots with `chat_of`.
- The picker of the Swift message box (`NewChatBar`) lists the projects, then the agents of the teams that the person can
  write to (Gumclaw), and ends with All projects, and the iPhone shows the box on every tab with a project and a
  suggestion of its own (`NewChatPlace`, `NewChatSuggestion`). This one lists the projects and ends with New project, as
  asked; the app has no tabs or team chats yet, so it has no Brainstorm of Product either (`Brainstorm.swift`, a talk by
  voice that fills the box of Product). A message from Home always starts a new chat with the project.
- Not ported: the slash commands other than `/model` and `/effort` (new, compact, stop, mute, archive, help), mentions, the access menu, editing a queued message, link and GitHub previews,
  the schedule of a message, a pasted image in the message box or a composer (`AttachmentPaste`: Paste in the menu of
  the field on an iPhone, Command-V on a Mac), the fifth rule of Needs attention (stale data, which a preview has none of). Of the chat:
  Edit of a draft card (`MessageDraftEditor`), a reply to a card in words (`DecisionReplyComposer`), the links of a
  card's sources and its GitHub embeds, View revised proposal, opening a page of the Library, the zoom, swipe and
  share of the photo viewer, and the hold of a message or a photo (copy, reply, reactions). Also the nested Chats tab of
  the Swift app (`ChatList.sections`, `ChatListSection`, `ChatSectionRow`): Starred first with its chats, which holds
  every chat that the person starred with a swipe to the right on its row, Unread under it with every unread chat that
  is not starred (`ChatList.unreadSection`), then one row for each project, Coconut first, then Team chats, Other
  agents, Personal at the bottom and Archived last. An unread chat shows only in Unread until the person reads it, as in
  the Mac sidebar (`ChatList.tree`). Each row says what waits for the person and what works inside, counts the chats of
  its section, and opens them in place under it, set in as the branches of a tree (`ChatListRow`). Archived opens its
  chats on a page of their own (`ChatSectionRoute`), so an archived chat shows only there. The Exact app still lists
  every chat in one list, the unread ones first, archived chats among the others, and its title Agents in place of
  Chats. The rows name a further chat of a project after the project and its own title (`Bot.listName`, from the Mac's
  `chat_of` and `chat_title`), which a section of a project names by its title alone; the Mac sends `project_key`
  (coconut, gumroad, personal) for the order of the sections. Also the two buttons at the top right of the Swift app's
  Chats: Working (`WorkingFilterButton`), which lists only the chats that work or wait for the person, and the grid of
  the agents that work (`ChatGridView`), which the Exact app has no deck for.
- The pairing code lives in the memory of the data module. An update that applies while a phone pairs reloads the app,
  which then shows a new code, and the Mac seals its setup for the old one; send the new code. Keeping the code in the
  Keychain until the phone pairs would end this.

## Plan for the rest

The Swift app is 36,371 lines in `ios/Coconut`, 901 in `ios/Tunnel`, 207 in `ios/PushShared` and 4,652 in `mac/Sources`.
Keep it shipping until each phase reaches parity, and keep the bundle id of the Exact app different until the last one.

| Phase | Swift to port | Exact |
|---|---|---|
| 1 Live data | `APIClient`, `AppStore` (4,102), `Models`, `TunnelConnection`, `Onboarding`, pairing | Done: the pairing, the tunnel, the state and its updates, the model and effort (`/model`, `/effort`), attachments and the SQLite cache for a launch without the Mac are in `app.ts`, `tunnel.ts` and `crypto.ts` (see Live data). Attachments and the cache need the binary of their grants |
| 2 Conversation | `ConversationView` (1,642), `Cards`, `MessagePhotos`, `MessageQueue`, `BotMentions`, `ModelSettingsView`, `LiveView`, previews | Rows and cards done: days, the New line, senders and quotes, photos with their viewer, cards with sources, risk, previews, drafts, Undo and receipts, background processes and Library pages (see Photos and cards). Left: the editing and holding in Not ported, `MessageQueue`, `BotMentions`, `LiveView` |
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
| Widgets and the Live Activity | `CoconutWidgets` (the widgets, the New agent control and the one Live Activity of the working agents), fed by the glance that `HomeScreenPublisher.swift` writes (`HomeScreen`) | The `activities` extension of Exact Build for the Live Activity (not tried); the widgets are not checked |

Decisions the first builds should settle: whether Exact Build takes `deploy.store.ios: "A"`, whether it includes the
notification service and the share extension, and what a build of this app costs in worker time.
