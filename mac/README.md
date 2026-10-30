# Coconut.app

Coconut.app runs Coconut on the person's Mac. It is a menu bar app with three login items: the app itself and two agents.

- The app (`SMAppService.mainApp`) opens at login. Then the menu bar icon shows, and Sparkle, which runs in the app, checks for updates after a restart of the Mac.
- `com.gumroad.coconut.service` runs `coconut service`. The tool finds a Python 3.11 or later and starts `host.py serve` from the install folder. host.py then finds the person's Hermes and runs the service under its Python.
- `com.gumroad.coconut.relay` runs `coconut sidecar`. The tool starts the bundled relay sidecar when the Mac has a relay route.

The install folder is `~/Coconut`. The app and its login items read another folder from the app's preferences:

```sh
defaults write com.gumroad.coconut.mac InstallDirectory /path/to/folder
```

`COCONUT_INSTALL_DIR` changes the folder for one `coconut` command in Terminal. The folder must be inside the home folder.
The app does not register its login items while the LaunchAgent of `host.py install-service` or `tunnel.py install` exists.
Each launch registers a login item that is missing. **Start at Login** off removes all three. The menu shows each login item that is not on, and **Allow Coconut in Login Items…** when one waits for approval in System Settings.

## Install and update

The app carries the Coconut source: `Contents/Resources/coconut-source.tar.gz`, its package ID, and `scripts/deploy.py`.
`coconut install-source` installs that source with `deploy.py receive`, the same installer that `remote.py` and the Mac mini updater use.

- **Set Up Coconut** in the menu (and `Coconut.app --set-up`) registers the three login items, then installs the source. The service tool waits until the source exists, and the install starts it.
- At each start, the app runs `coconut install-source --if-needed`. After an update of the app, the new source goes in at once.
- An update lets the running turns finish first, for up to ten minutes (`host.drain_for_update`). Then `host.py` writes `.runtime/host/update-hold` and stops the service. The service gets two minutes to stop its Hermes gateway and its workers. Then it gets SIGKILL, and `host.py` stops the child processes that remain. `deploy.py` uses the `host.py` of the new source for this stop, so a fix to the stop also stops the service of an older source. launchd starts the tool again, and the tool waits while the hold lasts. After the source changes, the hold goes away, the service starts, and `deploy.py` checks Hermes and health. A failed check restores the earlier source. `deploy.py` adds the output of each run, with the traceback of a failure, to `.runtime/host/logs/install-source.log`.
- The hold ends after 30 minutes, also when an update stops halfway.
- `scripts/tunnel.py` uses the sidecar of the app (`COCONUT_SIDECAR`). It registers a relay route once, then restarts the relay login item. It compiles nothing.
- **Move Coconut to This App** in the menu runs `coconut migrate` (`host.py migrate-to-app`). The running turns finish first, then the earlier LaunchAgents stop, and their files move to `.runtime/legacy-agents`. The data stays in place, so bots and paired iPhones keep working. Then the app registers its login items and updates the source.

The update still needs the Command Line Tools, because `push.py` compiles its notification helpers.

The menu shows the state of the service, the relay and Hermes. **Pair iPhone…** shows a one-time setup link and its QR code.
A Hermes agent can print the same link. This command does not need the Command Line Tools:

```sh
/Applications/Coconut.app/Contents/Helpers/coconut pair
```

The iPhone can also start pairing: it shows a one-time code in a message for Hermes. With that code, the command seals a new setup for the iPhone, sends it through the relay, and waits until the iPhone pairs. It prints one plain line for each step. `docs/relay-transport.md`, "Pairing code from the iPhone", describes the protocol.

```sh
/Applications/Coconut.app/Contents/Helpers/coconut pair <code>
```

## Build and test

```sh
swift test --package-path mac
python3 scripts/mac_app.py build
```

The build writes `build/mac/Coconut.app` with an ad-hoc signature.

## Updates

Coconut.app updates itself with Sparkle 2.10.0. The feed is `https://coconut-updates.gumroad.workers.dev/mac/appcast.xml`.
`FEED_URL` in `scripts/mac_app.py` is the only place that names it. `SUPublicEDKey` names the key that signs each archive.

- **Automatic updates** is on by default. Then the app checks the feed every six hours and installs an update without a question. The setting is Sparkle's `SUEnableAutomaticChecks` and `SUAutomaticallyUpdate`. Info.plist sets both, so Sparkle never asks at the first start, and a Mac that nobody watches never waits on a dialog.
- The feed has one channel. Its items have no `sparkle:channel`, so every app sees every build, and Sparkle takes the newest one.
- Sparkle replaces only the app. The service runs from `~/Coconut`, so it keeps running. After the relaunch, the new app runs `coconut install-source --if-needed`, which lets the running turns finish before it changes the service.

A Hermes agent manages the updates with the `coconut` tool (`/Applications/Coconut.app/Contents/Helpers/coconut`). The output is plain text:

```sh
coconut status                              # version, automatic updates, last check, last error, service
coconut update                              # check now, start the install of a newer build (--wait: wait for the end)
coconut settings automatic-updates on|off
```

`coconut update` installs a newer build also when automatic updates are off. It asks the menu bar app, and opens the app when it does not run.
It returns when the install starts, because the service update waits for all running bot turns, also the one that ran the command. `coconut status` then shows "installing", and "installed" when the service runs the new version. `--wait` waits for the end; use it only outside a bot turn.
The menu shows the same settings, the version and the last update error.

The Worker `coconut-updates` (`updates/`) serves the feed. See `updates/README.md`.

## Release

The **Mac release** workflow runs in the GitHub environment `mac-release`, which accepts only `main`.

It runs on each push to `main` and publishes one build that every Mac gets. A person can start it to publish the head of `main` again, for example after a failed run.
A run publishes nothing when the feed already has its build or a newer one.

The version is in `mac/VERSION`. The build number is the commit count of `main`. The release job does these steps:

1. It checks that `SPARKLE_ED_PRIVATE_KEY` belongs to `SUPublicEDKey`.
2. It imports the Developer ID Application identity into a temporary keychain.
3. It builds the app and signs each part with the hardened runtime and a timestamp.
4. It sends the app to the notary service with `notarytool` and an App Store Connect API key, and staples the ticket.
5. It makes `Coconut-<version>-<build>.zip` and signs it with Sparkle's `sign_update`. An archive over 25 MiB stops the release. The feed publishes it with the start of its SHA-256 in the name, so each file has its own URL.
6. `scripts/mac_feed.py publish` builds the feed: the new archive, plus the newest five archives from the live feed. The archives that the earlier feed named stay for one more release, for clients with a cached appcast.
7. It deploys the Worker `coconut-updates` with Wrangler and checks that the live feed has the new build.

A missing secret stops the workflow. The secrets are `MAC_DEVELOPER_ID_P12`, `MAC_DEVELOPER_ID_P12_PASSWORD`,
`ASC_KEY_ID`, `ASC_ISSUER_ID`, `ASC_KEY_P8`, `SPARKLE_ED_PRIVATE_KEY` and `CLOUDFLARE_API_TOKEN`.

A local build has an ad-hoc signature without the hardened runtime: library validation refuses an ad-hoc Sparkle.framework.
