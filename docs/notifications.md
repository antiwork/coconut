# Notifications

Coconut uses a shared Cloudflare Worker to deliver alerts through Apple Push Notification service (APNs).
Users pair their Mac and allow notifications. They need no Apple key, Developer account, or relay account.
The iPhone does not need Coconut open or Tailscale connected to receive an alert.
Opening its task requires access to the mini. iOS notification permissions and Focus settings control presentation.

The Mac creates a concise preview from the saved decision or completed reply.
The preview shows the bot name and actual content. Failed tasks show their title without raw tool errors.
The Mac encrypts the preview and routing IDs before sending them to the relay.
The notification extension decrypts them locally before iOS displays the alert.
Tapping an alert opens its task and, when present, its decision. The app verifies the pairing identity before routing.
The app suppresses banners while open because conversations already receive live updates.
The app-icon badge counts pending Inbox cards, excluding cards that need a source refresh.
The Mac includes the count only inside encrypted notifications. Legacy direct APNs messages omit the count; the open app updates it from its Inbox after a saved or fresh snapshot is available.
Opening without a snapshot leaves the last push badge intact until synchronization. iOS Settings controls badge permission.
A delayed alert can briefly show an older count. Opening Coconut reconciles it with the current Inbox.

## Pairing and privacy

The signed app registers through Apple's App Attest protocol.
The relay verifies the app identity, challenge, attestation, and subsequent assertion counters.
Development attestations cannot register production push tokens.
The phone receives separate credentials for delivery and revocation. It shares only delivery access with its paired Mac.
The relay restricts each delivery credential to one phone and its registered encryption key.

The phone creates a Curve25519 key and stores it in shared Keychain storage for the app and notification extension.
The key uses `AfterFirstUnlockThisDeviceOnly` protection. It does not sync through iCloud.
Pairing also records the Mac's public key through the existing pinned HTTPS connection.
The Mac uses authenticated HPKE with Curve25519, HKDF-SHA256, and ChaCha20-Poly1305 through Apple's CryptoKit.
The authenticated header binds the notification ID, recipient key, and expiry.
The relay cannot decrypt previews or forge a message from the paired Mac.

The relay stores registration metadata and delivery receipts. It does not store notification bodies or ciphertext.
Cloudflare and Apple can observe delivery metadata. They receive no preview plaintext from this protocol.
The app queues revocation when notifications are disabled or pairing is removed.
Removing pairing also removes the phone's decryption key.

The extension needs no network request to show a preview.
If iOS cannot run the extension or access its key, it shows a short fallback instead.
This can occur before the first unlock after a phone restart. Coconut never disables device protection to reveal a preview.

## Events and recovery

Pending decisions, completed tasks, and failed tasks create notifications.
The backend records each notification in the same SQLite transaction as its source event.
It checks the bot preference and current task or decision again before delivery.
Answered decisions, deleted bots, expired work, and disabled devices do not produce stale alerts.
Peer replies return to their originating task, which sends the completion notification.

The queue survives backend restarts. Accepted notifications do not resend after restart.
Network failures retry with backoff for up to 24 hours. Retries reuse the notification ID and collapse ID.
The Mac persists the encrypted envelope before sending. Retries reuse its exact bytes.
The relay records accepted deliveries and returns their receipts without sending again.
An interrupted request can leave delivery uncertain; APNs collapse handling limits duplicates but does not guarantee exactly-once presentation.
APNs acceptance confirms receipt by Apple, not presentation on the phone.

## Maintaining the shared service

Coconut uses Gumroad's Apple team, `66LJACJ2U7`, and bundle ID `com.gumroad.coconut`.
The relay runs at `https://coconut-push.gumroad.workers.dev`.
Source files remain in the local `relay` folder. Deployment does not require a GitHub repository.
Use a topic-specific APNs key restricted to Coconut. Do not reuse another app's key.

Store these secrets through Wrangler:

- `APNS_PRODUCTION_KEY` and `APNS_PRODUCTION_KEY_ID` serve TestFlight and App Store builds.
- `APNS_DEVELOPMENT_KEY` and `APNS_DEVELOPMENT_KEY_ID` serve development builds.

The app key never enters user installation packages or iPhone registration responses.
Each Mac stores only its own encryption identity and scoped delivery credentials.
The installer compiles the CryptoKit helper with Apple Command Line Tools. It adds no dependency to Hermes.

```sh
cd relay
npm ci
npm test
npm run deploy
```

The Worker uses a durable object per app installation, request limits, and delivery quotas.
Do not enable request-body logging or add a plaintext preview field to the relay contract.
The previous direct APNs sender remains available only for phones that have not registered with the new app.
An encrypted registration cannot downgrade to direct delivery. Remove the old Mac APNs keys after migration verification.
Removing Coconut's runtime deletes its local keys and delivery credentials. It does not delete the shared Worker.

## API

`GET /v1/push/config` returns the relay address and Mac public key over the paired connection.
`PUT /v1/device/push` registers the authenticated phone's APNs token, environment, preference, and scoped relay grant.
It requires the existing pairing bearer token and an idempotency key.
`DELETE /v1/device/push` disables delivery to that phone.
`GET /v1/state` includes `push_configured`.

The `push_devices`, `push_routes`, and `push_outbox` tables contain private delivery records.
The encrypted envelope contains the preview and task route. The relay receives only opaque delivery IDs and encrypted bytes.
Host migration clears push registrations and pending deliveries along with pairing credentials.

## Verification

The backend tests cover queue persistence, retries, stale decisions, device registration, and notification preferences.
Transport tests verify separate environment credentials and reject missing or exposed keys before sending.
The physical iPhone tests receive an APNs alert and tap it to open its task.
They check an existing conversation and a fully closed app.
The relay tests also run the APNs sender in Cloudflare's runtime.
They verify signatures for both environments and prevent redirects from receiving Apple credentials.

After `build-for-testing`, connect and unlock the paired iPhone. Leave it untouched during the test.
Run `python3 scripts/test_notifications.py --xctestrun PATH_TO_IPHONEOS_XCTESTRUN`.
The test sends two diagnostic alerts for existing completed work. It does not run or change a task.
Use `--case conversation` or `--case closed` to run one case.

See [Apple's APNs provider documentation](https://developer.apple.com/documentation/usernotifications/setting-up-a-remote-notification-server).
See [Apple's notification extension documentation](https://developer.apple.com/documentation/usernotifications/modifying-content-in-newly-delivered-notifications).
See [Apple's App Attest verification rules](https://developer.apple.com/documentation/devicecheck/validating-apps-that-connect-to-your-server).
