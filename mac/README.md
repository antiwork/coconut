# Coconut.app

Coconut.app runs Coconut on the person's Mac. It is a menu bar app with two login items.

- `com.gumroad.coconut.service` runs `coconut service`. The tool finds a Python 3.11 or later and starts `host.py serve` from the install folder. host.py then finds the person's Hermes and runs the service under its Python.
- `com.gumroad.coconut.relay` runs `coconut sidecar`. The tool starts the bundled relay sidecar when the Mac has a relay route.

The install folder is `~/Coconut`. The app and its login items read another folder from the app's preferences:

```sh
defaults write com.gumroad.coconut.mac InstallDirectory /path/to/folder
```

`COCONUT_INSTALL_DIR` changes the folder for one `coconut` command in Terminal. The folder must be inside the home folder.
The app does not register its login items while the LaunchAgent of `host.py install-service` or `tunnel.py install` exists.

## Install and update

The app carries the Coconut source: `Contents/Resources/coconut-source.tar.gz`, its package ID, and `scripts/deploy.py`.
`coconut update` installs that source with `deploy.py receive`, the same installer that `remote.py` and the Mac mini updater use.

- **Set Up Coconut** in the menu registers the login items, then installs the source. The service tool waits until the source exists, and the install starts it.
- At each start, the app runs `coconut update --if-needed`. After an update of the app, the new source goes in at once.
- An update lets the running turns finish first, for up to ten minutes (`host.drain_for_update`). Then `host.py` writes `.runtime/host/update-hold` and stops the service. launchd starts the tool again, and the tool waits while the hold lasts. After the source changes, the hold goes away, the service starts, and `deploy.py` checks Hermes and health. A failed check restores the earlier source.
- The hold ends after 30 minutes, also when an update stops halfway.
- `scripts/tunnel.py` uses the sidecar of the app (`COCONUT_SIDECAR`). It registers a relay route once, then restarts the relay login item. It compiles nothing.
- **Move Coconut to This App** in the menu runs `coconut migrate` (`host.py migrate-to-app`). The running turns finish first, then the earlier LaunchAgents stop, and their files move to `.runtime/legacy-agents`. The data stays in place, so bots and paired iPhones keep working. Then the app registers its login items and updates the source.

The update still needs the Command Line Tools, because `push.py` compiles its notification helpers.

The menu shows the state of the service, the relay and Hermes. **Pair iPhone…** shows a one-time setup link and its QR code.
A Hermes agent can print the same link. This command does not need the Command Line Tools:

```sh
/Applications/Coconut.app/Contents/Helpers/coconut pair
```

## Build and test

```sh
swift test --package-path mac
python3 scripts/mac_app.py build
```

The build writes `build/mac/Coconut.app` with an ad-hoc signature.

## Release

The **Mac release** workflow runs on `main` when a person starts it. It runs in the GitHub environment `mac-release`.

1. It imports the Developer ID Application identity into a temporary keychain.
2. It builds the app and signs each binary with the hardened runtime and a timestamp.
3. It sends the app to the notary service with `notarytool` and an App Store Connect API key, and staples the ticket.
4. It keeps `Coconut-<version>-<build>.zip` as a workflow artifact for 30 days.

A missing secret stops the workflow. The secrets are `MAC_DEVELOPER_ID_P12`, `MAC_DEVELOPER_ID_P12_PASSWORD`,
`ASC_KEY_ID`, `ASC_ISSUER_ID` and `ASC_KEY_P8`.
