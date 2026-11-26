# Coconut relay

A Cloudflare Worker that delivers encrypted notifications through APNs and forwards the encrypted phone-to-Mac tunnel. It also holds the sealed one-time setup that a Mac sends to an iPhone for a pairing code (`docs/relay-transport.md`, "Pairing code from the iPhone"), and the sealed log that the Macs of a team share (`docs/team-log.md`). Users need no Apple key, Developer account or relay account. The relay sees routing IDs, timing and sizes, never content.

## Secrets

Store these with Wrangler. Use an APNs key restricted to Coconut's bundle ID.

- `APNS_PRODUCTION_KEY` and `APNS_PRODUCTION_KEY_ID` serve TestFlight and App Store builds.
- `APNS_DEVELOPMENT_KEY` and `APNS_DEVELOPMENT_KEY_ID` serve development builds.

- `ANTHROPIC_API_KEY` is Antiwork's model key for the Personal budget (`src/budget.js`, `docs/personal-budget.md`). Without it the budget routes answer 503.

The APNs key never enters installation packages or iPhone registration responses.

## Deploy

```sh
cd relay
npm ci
npm run deploy
```

## Glance of the Home Screen

After the person leaves the app, the Mac sends the glance of the widgets and the Live Activity at each change, sealed for the iPhone with the key of its notifications (`backend/coconut/home_screen.py`). The relay keeps only the newest glance of a route.

- `POST /v1/routes/<route>/glance`, send token: `{id, expires, envelope, live, urgent, widgets}`. `live` is `update`, `end` or null: the relay sends the envelope as the content state of the Live Activity (`{"sealed": envelope}`), at priority 10 when `urgent` (a new card) and 5 otherwise, and forgets the activity's token after `end`. `widgets` sends a WidgetKit push (iOS 26) that makes the widgets read the glance again. Glances count 30 a minute and 3,000 a day, apart from notifications.
- `GET /v1/routes/<route>/glance`, manage token: `{envelope}`, or 204 when the Mac sent none or it expired. The widget extension reads it.
- `PUT /v1/routes/<route>/live`, manage token: `{activity, widgets}`, the APNs tokens of the Live Activity and of the widgets. A key that the body leaves out keeps its token, and null removes it. A revoked route forgets them and its glance.

The Live Activity and widget pushes use the topics `<bundle>.push-type.liveactivity` and `<bundle>.push-type.widgets`, so the APNs key must serve them as it serves the app.

## Rules

- Do not enable request-body logging.
- Do not add a plaintext field to the relay contract. The Mac encrypts each preview and its routing IDs; the iPhone decrypts them. The flags of a glance (`live`, `urgent`, `widgets`) say what to send, never what it says.
