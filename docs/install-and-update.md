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

`--copy-model-access` transfers this Mac's model keys through SSH: the keys that Coconut saved, else the keys in the `.env` of the default Hermes profile. The receiver stores them with permissions `0600`, and its service moves them into Hermes at the restart.
Omit this flag to configure separate model access on the mini.

Model keys live in Hermes, as Hermes keeps them: each profile reads its provider keys from its own `.env`. At each start, update and setup, Coconut moves keys that it saved in `<data>/credentials.json` into the default profile's `.env`, then removes that file. It first claims the file with an atomic rename, so a file that arrives during the move waits for the next start. It also gives each named profile the provider keys of the default profile that the profile lacks, as `hermes_cli.profiles.backfill_profile_envs` does for a profile without a `.env`. A key that a profile already has stays. Coconut never prints a key. See `backend/coconut/model_keys.py`.
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
2. The first `hermes` command on the service's `PATH`. Coconut follows its launcher scripts to the Hermes source folder.
3. `~/.hermes/hermes-agent`, where the official installer puts Hermes.

The official installer keeps the Python packages of Hermes in a dependency environment in the Hermes home, under `installs/<id>`. Its `facts.json` names the active environment, and Coconut runs Hermes with that environment's Python.
The Hermes home is `HERMES_HOME`. A LaunchAgent does not get the variables of your shell, so when the service has no `HERMES_HOME`, Coconut reads it from your login shell, for example from `~/.zprofile`. Without either, it uses `~/.hermes`.
Coconut starts each Hermes process with `HERMES_DISABLE_LAZY_INSTALLS=1`. Each bot has its own Hermes home, and without this setting, Hermes would install a dependency environment for each of them.
A bot's worker also gets `HERMES_RUNTIME_DIR=<bot's Hermes home>/tools`. That is where the bot's terminal `hermes` installs tools such as the browser. Without this setting, the worker would read the tools of the Hermes home that owns the source folder. Bots do not share tools.
A bot's worker also gives the browser-harness daemons of `browser_exec` a private folder for their sockets: `BH_RUNTIME_DIR=/tmp/cbh-<random id>`, one for each worker process, removed when the worker stops. An AF_UNIX socket path has at most 103 bytes on macOS, which a folder in the bot's Hermes home exceeds. The daemon logs and the browser profiles stay where they were.
The login service notices a new Hermes by itself, for example after `hermes update`. Once a minute, it compares the Hermes that its next start would find with the Hermes that it runs on: the Python, the source folder, the checked-out commit, the installer's `facts.json`, and the installed packages. This comparison reads files only.
After a change, the service starts no new turn. It lets the running turns finish, for at most 10 minutes as during an update, then exits, and launchd starts it again. The new process runs the contract check against the new Hermes.
While the service waits, `GET /v1/setup` gives the reason "Coconut restarts for the new Hermes when the running turns finish." and `restarting: true`.

The service imports the Hermes modules that it uses, then runs its contract check against them.
It refuses a Hermes source folder that has a `.env` file, because every bot would get those keys. Hermes reads its keys from `~/.hermes/.env`. The check sends no model request and uses a temporary Hermes home.
The service then runs under the Python of that Hermes. It saves the result in `.runtime/host/hermes.json`.
A restart reuses a passed check while Hermes and the Coconut code stay the same. A failed check runs again at each start, so a restart is enough after you repair Hermes.

Without a usable Hermes, the service still starts, but it runs no bots.
The phone can pair and read `GET /v1/setup`, which says why. Every other route answers `503` with the code `hermes_unavailable`.
When you install or update Hermes, the service restarts by itself within a minute. After another repair, restart Coconut with `python3 scripts/host.py restart`.
A new Hermes that fails the check causes no second restart: each process compares against the Hermes that it started with.

An update changes Coconut only. It runs the contract check of the new code against the installed Hermes. It searches with the environment of the login service, so it checks the same Hermes as the service.
If that Hermes worked with the earlier version and fails with the new one, the update restores the earlier version.

The default model, approvals, and MCP servers in the phone's settings are those of your default Hermes profile. `hermes model` and `hermes config` change the same settings. Each other profile keeps its own settings.

Native Hermes owns login and credential refresh. Coconut does not copy OAuth tokens into bot profiles.

## The coconut skill for Hermes

Coconut ships a Hermes skill, `coconut`, in `skills/coconut` of the install folder. It tells Hermes how to update Coconut, pair an iPhone, and find the cause of a problem. Then the Hermes of the Telegram gateway or of the CLI can answer "update Coconut" or "why is Coconut offline".

Coconut adds `<install folder>/skills` to `skills.external_dirs` in the `config.yaml` of the default Hermes profile (`HERMES_HOME`, else `~/.hermes`), and of each profile in `<Hermes home>/profiles`. The bots are those profiles. `scripts/hermes_skills.py add` does this. It runs in `deploy.py` at each install, a first install and each update, and in `scripts/setup_coconut.py`. The service also runs the same edit at each start, in the background. A new bot profile gets the entry when Coconut creates it.
The edit uses the config reader and writer of Hermes, so comments and the other settings stay. A second run changes nothing. Coconut changes only its own entries: this skills folder and the `coconut` gateway platform below. Earlier versions changed the Hermes config only during setup. Now each update and each start set Coconut's own entries again, so an update keeps them current. A failed edit is a warning in the result of the update or in the service log. It never stops the update or the start.

An update replaces the skill files with the other source files. In an install, the skill files and folders are read-only, so `skill_manage` of Hermes cannot change them. The update makes the folders writable for its own changes, then read-only again.

The same step installs the `coconut` platform of the Hermes gateway in each of these profiles. It links `<Hermes home>/plugins/coconut` to `<install folder>/hermes_plugin/coconut`, adds `coconut` to `plugins.enabled`, and sets `gateway.platforms.coconut.enabled: true`. A plugin of another source with the name `coconut` stays, and the step reports it. The platform runs chat turns only when the chat runtime is `gateway`. See [the Hermes gateway](architecture.md#the-hermes-gateway). A gateway that already runs loads the platform after its next restart (`hermes gateway restart`).
Each update and each start set these entries again, as for the skills folder. Coconut owns the keys that it sets in `gateway.platforms.coconut`. The other keys of that block stay.
The uninstaller removes the link and both settings.

### Team commands of the coconut tool

A Mac with no person at it can join a team from a message, as `coconut pair <code>` pairs an iPhone. Hermes runs these commands of `Coconut.app/Contents/Helpers/coconut`:

| Command | What it does |
|---|---|
| `coconut team` | Prints the teams of this Mac: the name, the ID, the state, the admin, the members, the bots, and the sync error. `--json` prints the answer of the service. |
| `coconut team join [--name <text>]` | Joins a team with the invite link on standard input. `--name` is the name of this Mac in the member list. Without it, the service uses "Member". |
| `coconut team new-bot <name> [--home <path>]` | On a team-only Mac: declares a bot for the Hermes home that it stands for. See [A Mac without a person](#a-mac-without-a-person). |
| `coconut team add-bot <bot> [--team <name or ID>]` | Adds a bot of this Mac to the team. `<bot>` is the ID or the name of the bot. Case does not matter. |
| `coconut team remove-bot <bot> [--team <name or ID>]` | Removes a bot of this Mac from the team. |
| `coconut team leave --confirm <name of the team> [--team <name or ID>]` | Leaves the team, and deletes its copy and its keys on this Mac. Without `--confirm`, it prints what a leave does and the exact command. |

- The invite link is a secret. The tool reads it only from standard input, and no output of the tool has it. In a terminal, the tool asks for the link and does not show it.
- When this Mac is in more than one team, a command for one team needs `--team`.
- A join that is done already prints "This Mac is already in this team." and exits with 0.
- Each error prints the text of the service and exits with a status that is not 0. A service that does not answer, a service without the team routes, and a Coconut without its team helper each print one line that says what to do.
- `COCONUT_INSTALL_DIR` points the tool at another install folder, for example the folder of a test service.

The tool calls the team routes of the service with the token of `<data>/gateway/bridge.json`. See [the team log](team-log.md#mac). A person makes a team and an invite on the Team screen of the app.

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

1. It checks for macOS and Xcode Command Line Tools. Coconut.app needs macOS 15 or later. A Mac on macOS 14 gets the last build for macOS 14.
2. It finds and checks the Hermes that the person already uses, as the service does at each start. See [The Hermes that Coconut uses](#the-hermes-that-coconut-uses). It does not install its own copy. If Hermes is missing or does not pass the check, it stops and says why.
3. It adds the `coconut` skill to the Hermes profiles. See [The coconut skill for Hermes](#the-coconut-skill-for-hermes). A failure here does not stop the setup.
4. It prepares notification encryption with `push.py --rebuild`.
5. It installs Coconut.app when it is missing. It downloads the newest build from the update feed. It accepts the app only when `codesign` and Gatekeeper accept it. The app goes in `/Applications`, or in `~/Applications` when `/Applications` is not writable.
6. It makes the app run this checkout (the preference `InstallDirectory` of `com.gumroad.coconut.mac`). It keeps the Hermes settings `COCONUT_HERMES_PYTHON`, `COCONUT_HERMES_ROOT` and `HERMES_HOME` of the shell or of the earlier LaunchAgent in the preference `ServiceEnvironment`. If the app ran another folder, that service finishes its running turns, and the login items start again with this checkout.
7. It moves a service of the earlier LaunchAgents to the app with `host.py migrate-to-app`. The running bot turns finish first. The data stays in place.
8. It opens the app. The app registers its login items (the app itself, the service and the relay), and Coconut and its menu bar app start again after a restart. The command waits for the local health check.
9. It registers the relay route with the sidecar of the app, and restarts the relay login item.
10. It runs `python3 scripts/pairing.py --share`. It prints the setup link and the path of a QR image that holds the same link last.

Each failed check prints what to do. You can run the command again at any time. It skips the steps that are complete, and it prints a new setup link.
A later run uses the saved Hermes settings where the shell has none. `--reset-hermes-settings` forgets them. When the saved settings change, the service finishes its running turns and starts again with them.

Sparkle updates Coconut.app by itself. The source in this checkout stays yours: update it with `git pull`, then run `python3 scripts/host.py restart` while no bot works. The restart stops running bot turns at once.
The menu bar icon shows the state of the service, the relay and Hermes. **Pair iPhone…** shows a new setup link and its QR code.
A Hermes agent can print a link without the Command Line Tools: `/Applications/Coconut.app/Contents/Helpers/coconut pair`.

If the iPhone already has a pairing, Coconut asks before a link replaces it.

## A Mac without a person

A Mac that runs a Hermes agent with no person at it can be a member of a team, so that its agent publishes pages for the team. This Mac gets the normal Coconut.app, set up "team only". Its service keeps its teams in step and saves the pages of the bots that the Mac declares. See [team-only mode](team-log.md#team-only-mode). The Mac has the same release path, the same automatic updates, the same signed helper and the same `coconut` tool as a full install.

### What a team-only install never touches

- Hermes: no skills folder, no plugin link, no key in a Hermes `config.yaml`, no `.env`, no key move, no contract check, no search for Hermes. The setup and each update work on a Mac without Hermes, and on a Mac with a Hermes version that full Coconut does not support.
- The relay sidecar: the app registers only two login items, the app and the service. A relay login item from an earlier full install is removed.
- A phone: the Mac pairs no iPhone, and it sends no notification.

The service needs Python 3.11 or later from Homebrew (`/opt/homebrew/bin/python3` or `/usr/local/bin/python3`). It does not use the Python of Hermes. 3.11 is the oldest Python that the tests of Coconut run on. Without such a Python, the set-up stops, and `coconut status` shows this line as the last update error, or as `Service error:` on an installed Mac: "Coconut found no Python 3.11 or later in /opt/homebrew/bin or /usr/local/bin. Install it with Homebrew: brew install python". The Mac needs no Xcode Command Line Tools: nothing compiles, because the app carries its helpers.

### Install

Run these steps in the macOS account of the Hermes agent, for example over SSH. `coconut` is `/Applications/Coconut.app/Contents/Helpers/coconut`.

1. Download Coconut.app from `https://coconut-updates.gumroad.workers.dev/mac/download`, unzip it, and move it to `/Applications`.
2. Set up Coconut for a team only:

   ```sh
   open -g /Applications/Coconut.app --args --set-up --team-only
   ```

   The app keeps the mode in its preference `InstallMode` (`team`), registers its login items, installs the source that it carries in `~/Coconut`, and starts the service. Approve Coconut in **System Settings → General → Login Items** if macOS asks.
3. Wait until `coconut status` shows `Mode: team only` and `Service: running`.
4. Join the team. An admin of the team makes an invite on the Team screen of the app. The link is a secret, so the command reads it only from standard input. In a terminal, the command asks for the link and does not show it. A Hermes agent pipes the link to the command.

   ```sh
   coconut team join --name "Reporter Mac"
   ```

5. Declare a bot for the Hermes home of the agent, then add it to the team:

   ```sh
   coconut team new-bot Reporter --home ~/.hermes
   coconut team add-bot Reporter --team "Example Team"
   ```

6. Let the agent save a page with `"audience": "team"`. It sends `POST /v1/gateway/document` to the address in `~/Coconut/.runtime/host/gateway/bridge.json`, with the token of that file and the Hermes home of the bot:

   ```python
   import json, pathlib, urllib.request
   bridge = json.loads(pathlib.Path('~/Coconut/.runtime/host/gateway/bridge.json').expanduser().read_text())
   page = {'home': str(pathlib.Path('~/.hermes').expanduser()), 'audience': 'team', 'key': 'weekly-report',
           'title': 'Weekly report', 'format': 'markdown', 'content': '3 tasks are open.'}
   request = urllib.request.Request(bridge['url'] + '/v1/gateway/document', data=json.dumps(page).encode(),
                                    headers={'Authorization': 'Bearer ' + bridge['token'],
                                             'Content-Type': 'application/json'})
   print(urllib.request.urlopen(request).read().decode())
   ```

   The other Macs of the team see the page in their Library after their next sync.

A set-up does not change the mode of an install. On a Mac with a full install, `--set-up --team-only` stops with one line that says to remove Coconut first. `coconut status` shows that line as the last update error. On a team-only Mac, `scripts/setup_coconut.py` stops in the same way.

### What the app and the tool show

The window of the app shows one page: what this Mac does, each team with its state, its members and bots, and the commands above. It has no chat, no Inbox, no Library and no bot settings, and it never shows the pairing page. The menu shows `Mode: team only`, the state of the service, and the login items.

`coconut status` prints the version and the update lines, then `Mode: team only`, the service, the team helper, the count of the bots of this Mac, and one line for each team with its state, its members and bots, and whether it is connected, with its sync error. `--json` has `"mode": "team"`. These commands are for a phone or for Hermes, so each one prints one line and exits with 1: `coconut pair`, `coconut migrate` and `coconut install-source --set-up`. `coconut team` works as on a full Mac.

### When the service does not start

A service that refuses its data folder writes the cause to `<data>/start-error.json`, next to `process.json`, in each mode. `coconut status` prints it as `Service error:`, and the menu shows it. On a team-only Mac, the window shows it too. A start that succeeds removes the file. An example is a data folder of the other mode (code `data_folder_mode`): a restart cannot heal it, so the login item waits five minutes before each new try, and not five seconds. When a person moves the folder away, the service starts within five minutes, without a click.

### Updates

Updates work as on a full Mac: Sparkle installs each new Coconut.app, and the app installs the source that it carries. The mode stays, and the service starts again in team-only mode. The update does not wait for running turns, because this service runs none. It skips each Hermes step and the relay sidecar, and it checks the health of the service on its main listener.

### Remove

No click is necessary. A removal cannot be undone: it deletes the teams of this Mac, their keys and the member key of this Mac. To come back, the Mac needs a new invite. A bot follows the rule of a leave: it shows the person what the command removes, and runs it only after a clear yes.

1. Leave each team: `coconut team leave --confirm "Example Team"`.
2. Quit the app, so that it gets the next argument: `osascript -e 'tell application id "com.gumroad.coconut.mac" to quit'`.
3. Remove Coconut:

   ```sh
   open -g /Applications/Coconut.app --args --remove
   ```

   The app removes its login items. That needs no approval. Then `coconut remove` removes the data of Coconut with `Uninstall Coconut.command` and moves `~/Coconut` to the Trash. The app removes its preference `InstallMode` and quits. Nothing in Hermes changes, because a team-only install added nothing there.
4. Check that `coconut status` shows `Service: not installed`. A failure shows as `Last update error:`.
5. Delete `/Applications/Coconut.app`.

On a full install, `--remove` and `coconut remove` change nothing. Each one gives one line that says to use `Uninstall Coconut.command`: `coconut remove` prints it, and `coconut status` shows the line of `--remove` as the last update error.

## Releases from CI

GitHub Actions makes every release. No Mac deploys or uploads builds by itself.

- `.github/workflows/mac-release.yml` publishes one Coconut.app build on each push to `main`. The feed has one channel, so every Mac gets each build.
- Sparkle in Coconut.app installs the new build on each Mac.
- `.github/workflows/testflight.yml` uploads the iPhone app to TestFlight on each push to `main` that changes the app. See [the iPhone setup](iphone-install.md#testflight-releases-from-ci).

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
It removes the `coconut` gateway platform from the same profiles: the plugin link and its two settings.
Delete `~/Coconut` to remove its source files afterward.

On this development Mac, the same launcher removes its separate runtime.
Before deleting its runtime, run `python3 scripts/remote.py revoke-access` to remove this installation's SSH authorization from the mini.
This command removes only the exact public key added by **Connect Mac mini.command**. Other SSH keys remain unchanged.
Delete the iPhone app to remove the client. Coconut does not uninstall Command Line Tools.
