# Coconut relay

A Cloudflare Worker that delivers encrypted notifications through APNs and forwards the encrypted phone-to-Mac tunnel. It also holds the sealed one-time setup that a Mac sends to an iPhone for a pairing code (`docs/relay-transport.md`, "Pairing code from the iPhone"), and the sealed log that the Macs of a team share (`docs/team-log.md`). Users need no Apple key, Developer account or relay account. The relay sees routing IDs, timing and sizes, never content.

## Secrets

Store these with Wrangler. Use an APNs key restricted to Coconut's bundle ID.

- `APNS_PRODUCTION_KEY` and `APNS_PRODUCTION_KEY_ID` serve TestFlight and App Store builds.
- `APNS_DEVELOPMENT_KEY` and `APNS_DEVELOPMENT_KEY_ID` serve development builds.

The APNs key never enters installation packages or iPhone registration responses.

## Test and deploy

```sh
cd relay
npm ci
npm test
npm run deploy
```

## Rules

- Do not enable request-body logging.
- Do not add a plaintext field to the relay contract. The Mac encrypts each preview and its routing IDs; the iPhone decrypts them.
