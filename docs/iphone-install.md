# Install and update Coconut on an iPhone

Coconut supports private TestFlight updates and direct development installation.
The source stays local. Agent execution and durable storage remain on the configured Mac.

## First development install

Sign into the Apple account for Coconut's Gumroad team in **Xcode → Settings → Accounts**.
Connect the iPhone and complete Xcode's device pairing and Developer Mode setup.

```sh
python3 scripts/iphone.py doctor
python3 scripts/iphone.py configure --team 66LJACJ2U7 --device DEVICE_ID
python3 scripts/iphone.py update
```

Use the device ID printed by `doctor`. Keep the phone connected and unlocked during installation.
After launch, open **Pair iPhone.command** and scan the code in Coconut Settings.

## Install the prepared build

Run this command from the `Coconut` folder after connecting the unlocked iPhone:

```sh
python3 scripts/iphone.py install --skip-build
```

The saved configuration selects the phone and team. Source builds use the fixed bundle ID `com.gumroad.coconut`.
The command verifies the signed build before installation. It reports installation and launch separately.
After launch, use **Pair iPhone.command** on the Mac and scan its code in Coconut Settings.

If the connection fails, run `python3 scripts/iphone.py doctor`.
Follow any trust or device preparation instructions in Xcode’s device manager.
See [Apple’s device pairing instructions](https://developer.apple.com/documentation/xcode/pairing-your-devices-with-your-mac).

## Future updates

Open **Update iPhone.command** with the phone connected and unlocked.
The updater rebuilds, signs, installs, and launches Coconut. It assigns a higher build number to each build attempt.
It uses the team and bundle ID in `.runtime/iphone.json` and installs over the existing app without uninstalling it.
Normal updates preserve the phone’s saved Mac pairing and local cache. Durable work stays on the Mac.
Build logs appear in `.build/iphone/build.log`.

After a build succeeds, `python3 scripts/iphone.py install --skip-build` retries installation without another build.
After cable pairing, Xcode also supports wireless use on the same Wi-Fi network with IPv6 enabled.
Coconut’s HTTPS connection is separate from this development connection.

For a new development Mac, configure the signing account and paired phone before running the updater:

```sh
python3 scripts/iphone.py doctor
python3 scripts/iphone.py configure --team TEAM_ID --device DEVICE_ID
python3 scripts/iphone.py update
```

Keep the existing signing team and bundle ID when updating this installation.
An app update does not update the backend. **Update Mac mini.command** handles backend changes.

## Private TestFlight

TestFlight supports automatic updates when the iPhone is away from the development Mac. Each uploaded build remains available for 90 days.
It does not require Developer Mode. See [Apple’s TestFlight instructions](https://testflight.apple.com/).

Coconut uses Gumroad's team, `66LJACJ2U7`, and bundle ID `com.gumroad.coconut`.
Its App Store Connect name is **Coconut for Hermes**. The installed app displays **Coconut**.
The [private group](https://appstoreconnect.apple.com/teams/69a6de79-1356-47e3-e053-5b8c7c11a4d1/apps/6810507745/testflight/groups/3c816f8d-3acf-49fd-b6dd-68e59ecc7069) has automatic distribution enabled.

Keep this group limited to Gianfranco. Do not add other testers or create a public link.
The upload command does not create groups or enforce their membership.
See [Apple’s internal tester instructions](https://developer.apple.com/help/app-store-connect/test-a-beta-version/add-internal-testers).

Open **Update TestFlight.command**, or run:

```sh
python3 scripts/iphone.py testflight
```

New configurations use the UTC build date as the version, such as `2026.9.13`.
The separate build number increases with each build and does not reset each day.
The command archives Release and asks Xcode to sign and upload it.
Use `configure --version 1.0.0` with the team and device arguments to select a fixed version.
Use `--version date` to return to calendar versions.
An upload with `--skip-build` keeps the existing archive's version and build number.
It sets `testFlightInternalTestingOnly` to prevent external testing and App Store distribution.
It requires the Gumroad team and Coconut bundle ID.
It records an upload only after Xcode reports success. Apple processing and tester availability require separate checks.
The log is `.build/iphone/testflight.log`.

After an interrupted upload, check App Store Connect before retrying the same build.
Install TestFlight on the iPhone, accept the private invitation, and enable automatic updates for Coconut.
Updates can arrive over cellular or Wi-Fi. Apple processing prevents immediate delivery.
The source and backend remain local. Apple receives only the signed app and its symbols.

### Notifications

Release builds register production APNs tokens. Development builds register sandbox tokens.
Coconut's shared relay holds the Apple push keys. Each Mac encrypts previews before sending them to the relay.
Users do not need their own Apple Developer account or push key.
Open an updated app once to register its token.

### Encryption declaration

The app uses Apple's URLSession TLS, Keychain, and CryptoKit for encrypted notifications. It bundles no third-party cryptography.
`ITSAppUsesNonExemptEncryption` is `false` for this implementation.
Review this declaration if the app adds cryptography outside Apple's operating system.
See [Apple’s encryption guidance](https://developer.apple.com/documentation/security/complying-with-encryption-export-regulations).

The `archive` and `export` commands remain available for local preparation without upload.

## App Store Connect API credentials

Before requiring an Xcode account, check `.runtime/appstoreconnect/config.json` in the primary checkout.
A release worktree may not contain this ignored directory.
The config records the upload key ID, issuer ID, team, and bundle ID.
The adjacent `AuthKey_<upload_key_id>.p8` file supplies the private key.
Use Xcode's `-authenticationKeyPath`, `-authenticationKeyID`, and `-authenticationKeyIssuerID` export arguments with the existing TestFlight export options.
These credentials work while the Mac is locked. Keep them outside Git.
After upload, verify the build state and internal tester access through App Store Connect.
