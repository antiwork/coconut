# Coconut on Exact

`exact/` is the core loop of Coconut (Home and a chat) rebuilt as an Exact app. It is the first step of moving the
client to Exact, Exact Build and AppDrop. The Swift app in `ios/` and `mac/` is unchanged and still ships through
TestFlight and Sparkle. The Python service in `backend/` is unchanged.

- Exact: github.com/expo/exact at `8ed52642a2c4a6227c8abfafd80ecf501b1eb5aa` (2026-11-17). Build with that value as `exact`.
- Name Coconut, bundle id `com.gumroad.coconut.exact`. Never `com.gumroad.coconut` or `com.gumroad.coconut.mac`.
- Data: the preview fixture of the Swift app (`ios/Coconut/Fixtures/PreviewState.json`, copied to `exact/preview.json`)
  and the Gumroad team of its team preview (`exact/team-preview.json`). Nothing is connected to a Mac yet.

## What is in it

| File | What |
|---|---|
| `app.contract` | The root: Home, a chat, the New project sheet, the composer and the menus |
| `parts.contract` | Parts that only draw: face, unread badge, chat row, Needs attention row, bubble, cards, working line |
| `shapes.contract` | The records `app.ts` answers |
| `app.ts` | The data module over the fixture. State lives in memory until the app launches again |
| `app.forma` | The palette of `Theme.swift`: page, card, user bubble, warm accent, green, the red tints, the eight faces |
| `app.json`, `assets/` | Manifest (iOS and web hosts, no updates yet) and the icon of the Swift app |

Home. Needs attention at the top, with the four rules of `Attention.swift` (a card waiting over 4 hours, an agent that
cannot run, blocked work, an unseen notice of the team); the fixture alone breaches none, so `app.ts` applies what
`AppStore.applyAttentionPreview` applies for the UI test of Home. Then one flat list of chats, newest first, in the
layout of `ChatRowLayout`: face, name, age, a line that is the status or the last message, the unread badge, the green
dot while the agent works. At the bottom a message box that is always open, with a picker that names the project the
message goes to and ends with New project. Send starts a chat with that project.

Chat. Messages with Markdown and the time of each under it (`MessageTimes.label`), activity lines, cards with their options (an answer keeps the option chosen), files,
the notices of a team, and the working line: the phase, the time of the turn to the second, the model. The composer is
one box: a short draft is one line with the plus inside it and Send beside it. A draft over 80 characters, a newline,
an attachment, or focus opens it, with the model and the reasoning level under the text (`ComposerExpansion`). Send
shows when there is a draft. Stop shows while the agent works (`sendControls`).

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
`dev.rebuild.typescript` to `save`, which is the default. Three loops exist; none was measured here:

1. Web host (`bun host/web/dev.mjs`, or `bun exact.mjs web`): the fast one. It needs the Rust toolchain and a compile, so
   it needs a machine with 10 GB free. `scripts/agent.mjs` takes screenshots of it.
2. Updates to a phone: `bun exact.mjs push --watch` publishes the working tree on each save to an installed build that
   takes signed updates, in seconds, and refuses what needs a new binary (a grant, native code). It needs
   `deploy.store.ios: "A"`, a key (`bun <exact>/scripts/deploy.mjs keygen coconut`, public half in
   `deploy.signing.keys`), a channel in `deploy.channels` whose origin is `"appdrop"` (AppDrop's origin for the key, with
   no server of ours), and a build made with `EXACT_UPDATE_GENESIS=1`.
   `app.json` says `deploy.store.ios: "0"` now, so the first build takes no updates. Whether Exact Build produces such
   a build is not known.
3. Rebuild and `npx appdrop --update`: a build of about 4 seconds with `base`, plus the upload and the install.

## Numbers

Measured on 2026-11-17 with snapshot `exact-2026-11-14T0909Z`:

| Build | Worker time | Wall time | Steps |
|---|---|---|---|
| First successful build | 33.7 s | 86 s with polling and the IPA | cache restore 4.3, cargo 12.8, swift 6.5, package 7.1 |
| Rebuild with `base=<id>`, same code | 6.6 s | 11 s with upload and IPA | |
| A build that fails in the bake | about 7.5 s | 10 to 20 s | free for the first 30 minutes a week |

Not yet measured: the time from save to screen on the web host, and a `push --watch` update.

## Gaps found

- Exact Build compiles with the newest snapshot of its catalog, and the catalog lags main: on 2026-11-17 it listed main
  of 2026-11-14, and the build status said so in `sdk.note`. That snapshot refuses a Forma token name as a colour
  (`background-color="surface.card"`), and has no `"section"` style for `formatDay` and no `"month-day"` style for
  `formatDate` (`"medium"`, `"month-year"` and `"iso"` only). So the rows write the values of `app.forma` out, and a
  date older than a week, in a message and in the list of chats, reads like "Nov 10, 2026". Name the tokens again
  once `sdk.note` shows a newer snapshot.
- Exact Build builds iOS only. The Mac app needs a Mac with Xcode and Rust (`bun exact.mjs mac`).
- Its manual lists `widgets`, `activities`, `intents` and `alarms` as `extensions`. The notification service and the
  share extension are not named there, so whether the cloud build includes them is unverified.
- The generated `exact/AGENTS.md` tells agents to run `bun exact.mjs share`, which builds natively and locally. On the
  shared Mac use Exact Build. Edits to that file need approval, which did not come, so the note is here.
- `exact/exact.mjs` names the bun of this Mac (`/opt/homebrew/Cellar/bun/1.4.4`) in its fallback. `bun exact.mjs <verb>`
  works anywhere bun is on the path. The Exact checkout is found at `../../exact` (beside the repository) or by `EXACT`.
- `symbol:sf/<name>` draws blank on the web host (a warning). Eight are used: `chevron.down`, `brain`, `paperclip`,
  `circle.inset.filled`, and `hourglass`, `bolt.slash.fill`, `exclamationmark.octagon.fill`, `megaphone.fill` for the
  rows of Needs attention.
- `preview.json` is a copy. An app folder cannot read outside itself, so it must be copied again when the fixture
  changes. The data module reads the fixture's `task_id` links, `updated_at` of a working task (there is no
  `turn_started_at`) and the bots with `chat_of`.
- The picker of the Swift message box (`NewChatBar`) starts with All projects and ends with Open and All agents. This one
  lists the projects and ends with New project, as asked. A message from Home always starts a new chat with the project.
- Not ported: slash commands, mentions, the access menu, editing a queued message, link and GitHub previews, photos,
  the schedule of a message, the fifth rule of Needs attention (stale data, which a preview has none of).

## Plan for the rest

The Swift app is 36,371 lines in `ios/Coconut`, 901 in `ios/Tunnel`, 207 in `ios/PushShared` and 4,652 in `mac/Sources`.
Keep it shipping until each phase reaches parity, and keep the bundle id of the Exact app different until the last one.

| Phase | Swift to port | Exact |
|---|---|---|
| 1 Live data | `APIClient`, `AppStore` (4,102), `Models`, `TunnelConnection`, `Onboarding`, pairing | Sources in `app.ts` that call the service through the tunnel; a SQLite cache (`sqlite.open`); pairing by a deep link (`protocol_handlers`); the secret in the Keychain (`secret.keep`) |
| 2 Conversation | `ConversationView` (1,642), `Cards`, `MessagePhotos`, `MessageQueue`, `BotMentions`, `ModelSettingsView`, `LiveView`, previews | More parts and routes in `parts.contract`; photos by the file picker and `image`; a virtualized list for long chats |
| 3 Team | `TeamChat` (2,093), `TeamChatView` (1,710), `TeamViews`, `Team`, `PeopleView` | A second family of chats and a notice board, over the same list |
| 4 Business and library | `BusinessDashboard` and the other Business files, `Library`, `DocumentPage`, `UsageViews`, `DecisionInboxView`, `AgentProject`, `Bento`, `ChatDeckView` | Routes and tabs (`select(nav, name)`); Markdown for documents; charts as `svg` |
| 5 Release | Bundle id, signing, TestFlight | Name the app `com.gumroad.coconut` so it replaces the Swift app; sign the IPA on CI as `testflight.yml` does (Exact has an EAS and a CI recipe); keys and a channel for updates |

Native parts:

| Part | Swift today | On Exact |
|---|---|---|
| Noise tunnel | `ios/Tunnel` (Noise IK and IKpsk2, 25519, ChaChaPoly, SHA-256; frames of `TunnelFrames`), `URLSessionWebSocketTask` to the relay | First try TypeScript in the data module: `net.websocket` grant, a stream resource for the socket, pure-JS crypto (`@noble`), the same frames, checked against test vectors of `Noise.swift`. If it costs too much battery or time, a Swift `ExactModule` in `modules/apple/` with the same CryptoKit code, or a Rust data crate. The Mac sidecar keeps `Noise.swift` unchanged |
| Push | `PushNotifications.swift`, APNs from the service | `@exact/push`: `subscribePush()` and `pushSubscription()` give `{apple: {deviceToken}}`; the app sends it to the service, which sends APNs with `apns-topic` `com.gumroad.coconut.exact` (then the final id) and a top-level `navigate`. Grants `device.notifications purpose.notifications` and `device.push`. Needs the App ID with Push and a key valid for the topic |
| Notification service | `CoconutNotificationService`, `PushShared` (207) | Swift in `modules/apple/notification-service/NotificationService.swift`, with the `PushShared` files copied there (links are refused). It runs no Contract or TypeScript. It shares the App Group and the Keychain group of the app, so it can read the key the app keeps |
| Share extensions | `CoconutShare`, `ShareShared`, `CoconutMacShare` | `share_target` in `app.json` derives an Apple share extension (presentation `compose` or `confirmation`); a route such as `/share` reads `searchParam(entry, "text")` and `sharedFiles(entry, "files")`, and the app asks for the project. `apps/fieldnotes` does it. The extension of the Mac is not checked |
| Mac menu bar | The status item of `mac/Sources` | A `button` with `-exact-menu="status"` is the status item; its `popovertarget` menu is the menu |
| Sparkle | `mac/Sources/CoconutApp/Updater.swift`, the feed in `updates/` | Exact updates the Contract, assets and `app.ts` of an installed Mac app with signed updates (`deploy.store.macos`). A new grant or native code still needs a new binary, so Sparkle or a notarized download stays for those |
| Live Activities | `LiveActivity.swift` | The `activities` extension of Exact Build (not tried) |

Decisions the first builds should settle: whether Exact Build takes `deploy.store.ios: "A"`, whether it includes the
notification service and the share extension, and what a build of this app costs in worker time.
