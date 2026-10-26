# Coconut.app

Coconut.app runs Coconut on the person's Mac. It is a menu bar app with two login items.

- `com.gumroad.coconut.service` runs `coconut service`. The tool finds a Python 3.11 or later and starts `host.py serve` from the install folder. host.py then finds the person's Hermes and runs the service under its Python.
- `com.gumroad.coconut.relay` runs `coconut sidecar`. The tool starts the bundled relay sidecar when the Mac has a relay route.

The install folder is `~/Coconut`. The app and its login items read another folder from the app's preferences:

```sh
defaults write com.gumroad.coconut.mac InstallDirectory /path/to/folder
```

`COCONUT_INSTALL_DIR` changes the folder for one `coconut` command in Terminal.
The app does not register its login items while the LaunchAgent of `host.py install-service` or `tunnel.py install` exists.

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

Without the secrets `ASC_KEY_ID`, `ASC_ISSUER_ID` and `ASC_KEY_P8`, the workflow skips step 3 and says so.
The archive then has the suffix `-not-notarized`, and Gatekeeper refuses it on other Macs.
