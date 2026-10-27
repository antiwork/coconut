# Install Coconut and update it

The iPhone runs the native app. The Mac mini runs Coconut, Hermes, and the database.
The app reaches the Mac through Coconut's relay, end-to-end encrypted. The relay sees route IDs, timing, and sizes, never content. See [the relay transport](relay-transport.md).
The Mac needs no VPN and no open port: Coconut listens only on `127.0.0.1`, and a sidecar keeps an outgoing connection to the relay.
This Mac uses SSH to install and update the mini.

Source files stay in this local folder. These commands do not publish a GitHub repository.

## Prepare the Mac mini once

1. Sign into the Mac mini's macOS account.
2. Enable **Remote Login** in **System Settings → General → Sharing**.
3. Allow only the macOS account that will run Coconut.
4. Install Xcode Command Line Tools with `xcode-select --install` if they are absent.
5. Install Hermes with its [official installer](https://github.com/NousResearch/hermes-agent) in the same macOS account.
6. Enable **Prevent automatic sleeping when the display is off** in Energy settings.

SSH needs working key access from this development Mac, through any host name that reaches the mini.
After saving the mini configuration, open **Connect Mac mini.command** if SSH key access is not ready.
Enter the mini's password directly in Terminal. The command adds one Coconut-specific public key to the mini's `~/.ssh/authorized_keys`.
The private key stays in this project's `.runtime/ssh` folder. The command does not store the password.
The installer records the SSH host key inside this project's `.runtime/ssh` folder.
It refuses a changed host key. It does not change the global SSH configuration.

Apple documents [Remote Login](https://support.apple.com/guide/mac-help/allow-a-remote-computer-to-access-your-mac-mchlp1066/mac).

## Install from this Mac

Run these commands from the Coconut folder. Replace the example login with the mini's values.

```sh
python3 scripts/remote.py configure --ssh username@mini.local
python3 scripts/remote.py authorize
python3 scripts/remote.py doctor
python3 scripts/remote.py install --copy-model-access
```

The default destination is `~/Coconut` on the mini. Configuration accepts a different folder below the remote account's home.
The installer refuses an unrelated folder. It does not install Hermes. See [The Hermes that Coconut uses](#the-hermes-that-coconut-uses).

`--copy-model-access` transfers this Mac's saved model keys through SSH. The receiver stores them with permissions `0600`.
Omit this flag to configure separate model access on the mini.
The source package never includes keys, bot data, logs, or builds.

The first installation starts a user LaunchAgent. It restarts Coconut after a process failure and starts after macOS account login.
It also installs the relay sidecar, the LaunchAgent `com.gumroad.coconut.tunnel`.
FileVault or a restart can require login before this service becomes available.

This command starts with a new database. It does not copy the development Mac's conversations or profiles.

Coconut automatically gives every bot the same Homebrew configuration path, including bots created after setup.
No shell changes or onboarding step are required. Hermes profiles and other tool settings remain separate.
New installations start with Homebrew’s default trust. Your approved package list is not bundled or copied to another person’s installation.
Existing trust lists require an explicit import; Coconut never merges private profile settings automatically.

## The Hermes that Coconut uses

Coconut runs on the Hermes that is installed in the Mac account. It does not install its own copy.
Each time the service starts, it looks for Hermes in this order:

1. `COCONUT_HERMES_PYTHON` and `COCONUT_HERMES_ROOT`, when you set them. An explicit setting is final.
2. The first `hermes` command on the service's `PATH`, except Coconut's old launcher. Coconut follows its launcher scripts to the Hermes source folder.
3. `~/.hermes/hermes-agent`, where the official installer puts Hermes.

The official installer keeps the Python packages of Hermes in a dependency environment in the Hermes home, under `installs/<id>`. Its `facts.json` names the active environment, and Coconut runs Hermes with that environment's Python.
The Hermes home is `HERMES_HOME`. A LaunchAgent does not get the variables of your shell, so when the service has no `HERMES_HOME`, Coconut reads it from your login shell, for example from `~/.zprofile`. Without either, it uses `~/.hermes`.
Coconut starts each Hermes process with `HERMES_DISABLE_LAZY_INSTALLS=1`. Each bot has its own Hermes home, and without this setting, Hermes would install a dependency environment for each of them.
A bot's worker also gets `HERMES_RUNTIME_DIR=<bot's Hermes home>/tools`. That is where the bot's terminal `hermes` installs tools such as the browser. Without this setting, the worker would read the tools of the Hermes home that owns the source folder. Bots do not share tools.
A bot's worker also gives the browser-harness daemons of `browser_exec` a private folder for their sockets: `BH_RUNTIME_DIR=/tmp/cbh-<random id>`, one for each worker process, removed when the worker stops. An AF_UNIX socket path has at most 103 bytes on macOS, which a folder in the bot's Hermes home exceeds. The daemon logs and the browser profiles stay where they were.
After `hermes update`, restart Coconut, so that it uses the new dependency environment.

The service imports the Hermes modules that it uses, then runs its contract check against them.
It refuses a Hermes source folder that has a `.env` file, because every bot would get those keys. Hermes reads its keys from `~/.hermes/.env`. The check sends no model request and uses a temporary Hermes home.
The service then runs under the Python of that Hermes. It saves the result in `.runtime/host/hermes.json`.
A restart reuses a passed check while Hermes and the Coconut code stay the same. A failed check runs again at each start, so a restart is enough after you repair Hermes.

Without a usable Hermes, the service still starts, but it runs no bots.
The phone can pair and read `GET /v1/setup`, which says why. Every other route answers `503` with the code `hermes_unavailable`.
Install or update Hermes, then restart Coconut with `python3 scripts/host.py restart`.

An update changes Coconut only. It runs the contract check of the new code against the installed Hermes. It searches with the environment of the login service, so it checks the same Hermes as the service.
If that Hermes worked with the earlier version and fails with the new one, the update restores the earlier version.

Older versions installed their own Hermes in `.runtime/hermes` and their own `~/.local/bin/hermes` command.
The service deletes that command at its next start, but only when the file has Coconut's marker line.
Their LaunchAgent started the service with the Python of that runtime. When an update reloads the service, after the drain step, it changes only that Python in the LaunchAgent.
It uses the Python that runs the update when that Python is not in a virtual environment, else `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, or `/usr/bin/python3`, the first that can load Coconut.
Until bots become Hermes profiles, the shared model settings of the bots stay in `.runtime/hermes/terminal/hermes`.
To change them in Terminal, set that folder as the Hermes home:

```sh
HERMES_HOME="$HOME/Coconut/.runtime/hermes/terminal/hermes" hermes model
```

Native Hermes owns login and credential refresh. Coconut does not copy OAuth tokens into bot profiles.

## The coconut skill for Hermes

Coconut ships a Hermes skill, `coconut`, in `skills/coconut` of the install folder. It tells Hermes how to update Coconut, pair an iPhone, and find the cause of a problem. Then the Hermes of the Telegram gateway or of the CLI can answer "update Coconut" or "why is Coconut offline".

Setup adds `<install folder>/skills` to `skills.external_dirs` in the `config.yaml` of the default Hermes profile (`HERMES_HOME`, else `~/.hermes`), of each profile in `<Hermes home>/profiles`, and of each bot profile. `scripts/hermes_skills.py add` does this. It runs on a first install (`deploy.py`, which `Coconut.app --set-up` and **Set Up Coconut** run) and in `scripts/setup_coconut.py`.
The edit uses the config reader and writer of Hermes, so comments and the other settings stay. A second run changes nothing. Coconut changes the Hermes config only during setup. An install from before this version gets the entry when setup runs again.
A new bot profile gets the entry when Coconut creates it.

An update replaces the skill files with the other source files. In an install, the skill files and folders are read-only, so `skill_manage` of Hermes cannot change them. The update makes the folders writable for its own changes, then read-only again.

## GitHub access for a bot

Bots use the GitHub CLI login of the Mac account, as your normal Mac Terminal does.
A bot that had its own GitHub login before this version keeps it: the profile's `.env` sets `GH_CONFIG_DIR` to the bot's own folder.
Coconut does not copy tokens between bots.

From a bot's terminal, run:

```sh
gh auth login --hostname github.com --git-protocol https --web --skip-ssh-key
```

Open the device authorization URL on your phone and enter the displayed code. Keep the login process running until authorization finishes.
Then verify access from the same bot's terminal:

```sh
gh api user --jq .login
```

To run these commands from the Mac's normal Terminal, first find the bot's profile ID:

```sh
sqlite3 ~/Coconut/.runtime/host/coconut.sqlite3 "SELECT json_extract(data, '$.name'), id FROM bots"
```

For a bot with its own login, read `GH_CONFIG_DIR` from `.runtime/host/bots/PROFILE_ID/home/.env` in the Coconut folder. Prefix each `gh` command with that value.
Replace `PROFILE_ID` with that bot's ID. Do not change the shell's `HOME` or paste a token into chat.
If the Keychain is locked or unavailable, GitHub CLI supports `--insecure-storage`.
This stores the token unencrypted with owner-only permissions. Use it only if you accept that storage method.
Reauthorize if GitHub expires or revokes the token.
See [GitHub CLI authentication](https://cli.github.com/manual/gh_auth_login).

## CLI login from chat

Bots use the Mac account's `CODEX_HOME`, `GH_CONFIG_DIR` and `XDG_*` folders, unless the profile's `.env` sets them.
Keep these variables unchanged during login and later commands. Do not replace `HOME`.
Claude Code uses one Mac account login for all bots.
Keep `CLAUDE_CONFIG_DIR` and `CLAUDE_SECURESTORAGE_CONFIG_DIR` unset so Claude Code and Hermes use the same native credential store.
Claude Code settings and session history also share this account scope. Hermes instructions, memory, and workspaces remain separate.
An update preserves old bot Claude directories. Coconut does not copy their tokens or settings into the account directory.
For interactive login, the bot starts a native Hermes terminal with `pty=true` and `background=true`.
The process remains available while you authorize from your phone.
The bot checks a fresh authenticated request before it reports a successful connection.
The Mac account's Keychain must permit access. See [native credential storage](https://code.claude.com/docs/en/authentication#credential-management).

## Pair the iPhone

Install the signed iPhone app before pairing. See [the iPhone setup](iphone-install.md).

1. Open **Pair iPhone.command** on this Mac.
2. Open Coconut on the iPhone. A new installation opens onboarding. Otherwise, open Settings and tap **Pair a new Mac**.
3. Tap **Need help?**, then **Scan the QR code**.
4. Scan the QR displayed on this Mac.
5. Check the Mac name, then tap **Pair**.

The QR holds a `coconut://pair` link with the relay route, the Mac's public key, and a one-time secret. It does not contain a model key or device token.
The secret works once, for ten minutes. If it expires, open the launcher again.
If no mini configuration exists, the launcher pairs with this development Mac instead.

### Pair through Hermes

A person who already uses Hermes can pair without this repository and without touching the Mac. Onboarding on the iPhone is one screen. It shows one message to send to the Hermes agent, for example in a Telegram chat with the Hermes bot:

```
curl -fsSL https://coconut-updates.gumroad.workers.dev/install | bash -s <code>
```

1. The iPhone makes a one-time pairing code and a rendezvous box on the relay. The message holds the code.
2. The script (`updates/install.sh`) installs the newest notarized Coconut.app, after it checks the code signature, the team ID and Gatekeeper.
3. The script opens the app, which sets up Coconut, and waits for the service and the relay.
4. The script runs `coconut pair <code>`. The Mac seals a new setup for the iPhone in the rendezvous box.
5. The iPhone opens the setup and pairs by itself.

The code works once, for 10 minutes, and only for the iPhone that made it. Telegram does not encrypt bot chats end to end, so the code passes through Telegram's servers. After the iPhone pairs, the code is useless. `docs/relay-transport.md`, "Pairing code from the iPhone", describes the protocol.

**Need help?** on the screen shows the download link of Coconut.app, for a Hermes that cannot run commands. The person installs the app by hand, chooses **Pair iPhone** in its menu, and scans the QR code. Settings has **Pair a new Mac**, which opens the same screen.

A developer with a checkout of this repository runs `python3 ~/Coconut/scripts/setup_coconut.py` instead. The command does these steps:

1. It checks for macOS and Xcode Command Line Tools. Coconut.app needs macOS 14 or later.
2. It finds and checks the Hermes that the person already uses, as the service does at each start. See [The Hermes that Coconut uses](#the-hermes-that-coconut-uses). It does not install its own copy. If Hermes is missing or does not pass the check, it stops and says why.
3. It adds the `coconut` skill to the Hermes profiles. See [The coconut skill for Hermes](#the-coconut-skill-for-hermes). A failure here does not stop the setup.
4. It prepares notification encryption with `push.py --rebuild`.
5. It installs Coconut.app when it is missing. It downloads the newest stable build from the update feed, or the newest nightly build before the first stable release. It accepts the app only when `codesign` and Gatekeeper accept it. The app goes in `/Applications`, or in `~/Applications` when `/Applications` is not writable.
6. It makes the app run this checkout (the preference `InstallDirectory` of `com.gumroad.coconut.mac`). It keeps the Hermes settings `COCONUT_HERMES_PYTHON`, `COCONUT_HERMES_ROOT` and `HERMES_HOME` of the shell or of the earlier LaunchAgent in the preference `ServiceEnvironment`. If the app ran another folder, that service finishes its running turns, and the login items start again with this checkout.
7. It moves a service of the earlier LaunchAgents to the app with `host.py migrate-to-app`. The running bot turns finish first. The data stays in place.
8. It opens the app. The app registers its login items, and Coconut starts again after a restart. The command waits for the local health check.
9. It registers the relay route with the sidecar of the app, and restarts the relay login item.
10. It runs `python3 scripts/pairing.py --share`. It prints the setup link and the path of a QR image that holds the same link last.

Each failed check prints what to do. You can run the command again at any time. It skips the steps that are complete, and it prints a new setup link.
`--channel nightly` installs the nightly build of a new app.
A later run uses the saved Hermes settings where the shell has none. `--reset-hermes-settings` forgets them. When the saved settings change, the service finishes its running turns and starts again with them.

Sparkle updates Coconut.app by itself. The source in this checkout stays yours: update it with `git pull`, then run `python3 scripts/host.py restart` while no bot works. The restart stops running bot turns at once.
The menu bar icon shows the state of the service, the relay and Hermes. **Pair iPhone…** shows a new setup link and its QR code.
A Hermes agent can print a link without the Command Line Tools: `/Applications/Coconut.app/Contents/Helpers/coconut pair`.

If the iPhone already has a pairing, Coconut asks before a link replaces it.

An iPhone that paired before the relay used Tailscale. It shows "Coconut no longer needs Tailscale. Ask your Hermes for a new pairing link." Pair it again with a new link.

## Update the backend

After code changes, open **Update Mac mini.command**. The terminal command is:

```sh
python3 scripts/remote.py update
```

The updater validates file hashes before changing source files. It stops Coconut during replacement and preserves its previous service mode.
Before it stops Coconut, the updater asks a running service to drain. The service starts no new turn, and the running turns finish.
The updater waits up to ten minutes for that, and then it stops the service. A turn that a stop interrupts loses the results of its
tool calls, and Coconut fails its task at the next start, so that it never repeats an action that may have run. The request ends by
itself after the wait, so a failed update cannot leave the service paused. A service without this support is stopped at once.
It retains bot profiles, decisions, events, receipts, artifacts, and credentials.
Normal updates do not require another iPhone pairing.

The updater runs the Hermes contract check and checks the service's health on loopback. It rebuilds the relay sidecar, and installs it on a Mac that does not have it yet.
This development Mac then checks the health and the sidecar's state over SSH.
If the mini's Hermes worked with the earlier version and fails with the new one, the updater restores the earlier version.
An intentionally stopped service remains stopped.
If startup fails, the updater attempts to restore the previous source files. It never restores an older database automatically.
An update journal retains the source backup and previous service mode if the update stops midway.
A connection failure can hide a completed update. Before retrying, inspect the result:

```sh
python3 scripts/remote.py status
```

An iPhone interface change needs a separate signed app update. Backend changes do not install a new iPhone binary.

A bot can request a service restart after staging a reviewed update. Coconut pauses new work for at most 60 seconds.
If active turns remain, Coconut reopens the queue and defers the restart until the workers become idle.
Repeated requests do not extend the pause. Coconut does not interrupt active turns to meet this deadline.

## Check remote use

1. Turn off Wi-Fi on the iPhone.
2. Open Coconut and check for a fresh synchronization time.
3. Send a short task, then close the app.
4. Reopen the app and inspect the result.
5. Check that a pending decision survives a backend restart.

Physical iPhone and Mac mini checks remain separate from local tests.
The mini sends encrypted notifications through Coconut's shared relay and APNs.

## Remove Coconut

On the mini, open `~/Coconut/Uninstall Coconut.command`.
The uninstaller stops the owned service and removes the runtime, keys, bots, and stored work. Your Hermes installation stays.
It removes the `skills.external_dirs` entry of the `coconut` skill from the Hermes profiles, and makes the skill folders writable again.
It also removes this installation's exact `hermes` launcher. Close active Hermes Terminal sessions before removal.
Delete `~/Coconut` to remove its source files afterward.

On this development Mac, the same launcher removes its separate runtime.
Before deleting its runtime, run `python3 scripts/remote.py revoke-access` to remove this installation's SSH authorization from the mini.
This command removes only the exact public key added by **Connect Mac mini.command**. Other SSH keys remain unchanged.
Delete the iPhone app to remove the client. Coconut does not uninstall Command Line Tools.
