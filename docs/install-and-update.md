# Install Coconut and update it

The iPhone runs the native app. The Mac mini runs Coconut, Hermes, and the database.
The app connects through HTTPS over Tailscale. This Mac uses SSH to install and update the mini.

Source files stay in this local folder. These commands do not publish a GitHub repository.

## Prepare the Mac mini once

1. Sign into the Mac mini's macOS account.
2. Connect Tailscale to the same private network as the iPhone.
3. Enable **Remote Login** in **System Settings → General → Sharing**.
4. Allow only the macOS account that will run Coconut.
5. Install Xcode Command Line Tools with `xcode-select --install` if they are absent.
6. Enable **Prevent automatic sleeping when the display is off** in Energy settings.

SSH needs working key access from this development Mac. The installer uses SSH through the mini's Tailscale address.
After saving the mini configuration, open **Connect Mac mini.command** if SSH key access is not ready.
Enter the mini's password directly in Terminal. The command adds one Coconut-specific public key to the mini's `~/.ssh/authorized_keys`.
The private key stays in this project's `.runtime/ssh` folder. The command does not store the password.
The installer records the SSH host key inside this project's `.runtime/ssh` folder.
It refuses a changed host key. It does not change the global SSH configuration.

Apple documents [Remote Login](https://support.apple.com/guide/mac-help/allow-a-remote-computer-to-access-your-mac-mchlp1066/mac).

## Install from this Mac

Run these commands from the Coconut folder. Replace the example login and address with the mini's values.

```sh
python3 scripts/remote.py configure \
  --ssh username@mini.example.ts.net \
  --address https://mini.example.ts.net:8960
python3 scripts/remote.py authorize
python3 scripts/remote.py doctor
python3 scripts/remote.py install --copy-model-access
```

The default destination is `~/Coconut` on the mini. Configuration accepts a different folder below the remote account's home.
The installer refuses an unrelated folder. It installs the verified Hermes version and its Python environment inside `.runtime`.
If `uv` is absent, the Hermes installer downloads a verified copy into `.runtime`.

`--copy-model-access` transfers this Mac's saved model keys through SSH. The receiver stores them with permissions `0600`.
Omit this flag to configure separate model access on the mini.
The source package never includes keys, bot data, certificates, logs, or builds.

The first installation starts a user LaunchAgent. It restarts Coconut after a process failure and starts after macOS account login.
FileVault or a restart can require login before this service becomes available.

This command starts with a new database. It does not copy the development Mac's conversations or profiles.

Coconut automatically gives every bot the same Homebrew configuration path, including bots created after setup.
No shell changes or onboarding step are required. Hermes profiles and other tool settings remain separate.
New installations start with Homebrew’s default trust. Your approved package list is not bundled or copied to another person’s installation.
Existing trust lists require an explicit import; Coconut never merges private profile settings automatically.

## Hermes in Terminal

The runtime stays inside `~/Coconut`. To expose its terminal command, run this once on the mini:

```sh
python3 ~/Coconut/scripts/terminal.py install-command
```

The command installs `~/.local/bin/hermes`. Add `~/.local/bin` to your shell PATH if needed.
Run `hermes` to use the managed native Terminal profile. Its model configuration supplies Coconut's shared default.
Native Hermes owns login and credential refresh. Coconut does not copy OAuth tokens into bot profiles.

## An existing Hermes setup

Coconut reads the account's own Hermes home, `~/.hermes`, and its `profiles` folder. It never changes them.
After pairing, the iPhone shows what it found. Settings → Hermes setup shows the same step later.
Each chosen profile becomes a bot with a copy of its `SOUL.md`, memory files, and skills. Sessions, settings, and secrets stay behind.
The person can also copy the model API keys from `.env`. Coconut adds them to its default Hermes home and keeps the keys that it already has.
Coconut does not copy OAuth sign-ins from `auth.json`. Many providers rotate refresh tokens, so two homes that share one sign-in log each other out. Sign in again in Coconut's Hermes Terminal.
If Coconut has no default model, the iPhone offers the model of the setup. It offers the model only when Coconut's model list has the same provider route. Otherwise the person chooses a model from the list.
The person can also add the MCP servers of the setup. Coconut adds a server only when its default Hermes home has no server with the same name. It never changes an existing server.
Coconut copies the variables that a new server refers to, when the setup's `.env` defines them. It keeps the values that it already has.
A server that refers to a missing variable, or signs in with OAuth, is added with `enabled: false` and needs setup.
To finish such a server, add the variable to Coconut's Hermes `.env`, or run `hermes mcp login <name>` in Coconut's Hermes Terminal. Then remove `enabled: false` from the server in Coconut's Hermes `config.yaml`, and run `hermes mcp test <name>`.
Set `COCONUT_HERMES_SOURCE` to read another Hermes home. Coconut ignores a linked folder and its own Hermes homes.
Terminal chats do not appear in Coconut.
Run `hermes --coconut-bots` to list the app's bot profiles.
To open a bot directly, stop Coconut first, then run `hermes --coconut-bot Ada`.
The launcher holds the service lock while Terminal uses that bot. Restart Coconut after closing that Terminal session.

## GitHub access for a bot

Each bot uses its own GitHub CLI configuration. A login in your normal Mac Terminal does not configure the bot.
Coconut preserves the Mac account home for Keychain access and selects the bot through `GH_CONFIG_DIR`.
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

To run these commands from the Mac's normal Terminal, first run `hermes --coconut-bots` to find the bot's profile ID.
From the Coconut folder, prefix each `gh` command with `GH_CONFIG_DIR="$PWD/.runtime/host/bots/PROFILE_ID/home/.config/gh"`.
Replace `PROFILE_ID` with that bot's ID. Do not change the shell's `HOME` or paste a token into chat.
If the Keychain is locked or unavailable, GitHub CLI supports `--insecure-storage`.
This stores the token unencrypted with owner-only permissions. Use it only if you accept that storage method.
Reauthorize if GitHub expires or revokes the token.
See [GitHub CLI authentication](https://cli.github.com/manual/gh_auth_login).

## CLI login from chat

Bots use `CODEX_HOME` and `GH_CONFIG_DIR` inside their existing Hermes profile directory.
Keep these variables unchanged during login and later commands. Do not replace `HOME`.
Claude Code uses one Mac account login for all bots and managed Hermes Terminal.
Keep `CLAUDE_CONFIG_DIR` and `CLAUDE_SECURESTORAGE_CONFIG_DIR` unset so Claude Code and Hermes use the same native credential store.
Claude Code settings and session history also share this account scope. Hermes instructions, memory, and workspaces remain separate.
An update preserves old bot Claude directories. Coconut does not copy their tokens or settings into the account directory.
For interactive login, the bot starts a native Hermes terminal with `pty=true` and `background=true`.
The process remains available while you authorize from your phone.
The bot checks a fresh authenticated request before it reports a successful connection.
The Mac account's Keychain must permit access. See [native credential storage](https://code.claude.com/docs/en/authentication#credential-management).

## Pair the iPhone

Install the signed iPhone app before pairing. See [the iPhone setup](iphone-install.md).

1. Enable Tailscale on the iPhone.
2. Open **Pair iPhone.command** on this Mac.
3. Open Coconut on the iPhone. A new installation opens onboarding. Otherwise, open Settings.
4. Tap **Scan the code**, or **Scan setup code** in Settings.
5. Scan the QR displayed on this Mac.
6. Check the Mac address, then tap **Pair**.

The QR contains the Mac address, certificate fingerprint, and a one-time code. It does not contain a model key or device token.

### Pair through Hermes

A person who already uses Hermes can pair without this repository. Onboarding shows a message to send to the Hermes agent.
The message tells the agent to clone this repository to `~/Coconut` if it is missing, then to run one command on its Mac:

```sh
python3 ~/Coconut/scripts/setup_coconut.py
```

The command does these steps:

1. It checks for macOS and Xcode Command Line Tools.
2. It finds the Hermes that the person already uses: the `hermes` command, or `~/.hermes/hermes-agent`. It does not install its own copy. If Hermes is missing, it stops and asks you to install Hermes first.
3. It checks that Tailscale is connected, then configures the host with this Mac's Tailscale name and port 8960. It keeps a port that an earlier setup chose for the same name.
4. It installs or starts the login service, so Coconut starts again after a restart. Then it waits for the local HTTPS health check.
5. It runs `python3 scripts/pairing.py --share`. It prints the `coconut://pair` setup link and the path of a QR image that holds the same link last.

Each failed check prints what to do. You can run the command again at any time. It skips the steps that are complete, and it prints a new setup link.
If the Tailscale name changes, the command stops Coconut before it saves the new address. Every paired iPhone must then pair again.
If the certificate does not cover the new name, the command stops and prints how to replace the certificate.
The iPhone needs Tailscale, signed in to the same tailnet as the Mac.

The agent sends the link and the QR image to the person. On the same iPhone, the person taps the link. On another screen, the person scans the QR image in Coconut.
The link and the image contain the one-time code. Send them only to the person who pairs.
If the iPhone already has a pairing, Coconut asks before it replaces it.
The code expires after five minutes. If it expires, open the launcher or run the command again.
The app validates HTTPS and the certificate before it sends the code.

If no mini configuration exists, the launcher pairs with this development Mac instead.
The QR image stays in `.runtime/pairing`. Its expired code cannot create another pairing.

For automatic Tailscale connection, enable VPN On Demand for Wi-Fi and cellular on the iPhone.
See [Tailscale's instructions](https://tailscale.com/docs/features/client/ios-vpn-on-demand).

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
It retains bot profiles, decisions, events, receipts, artifacts, credentials, and the certificate.
Normal updates do not require another iPhone pairing.

The updater checks Hermes imports and local HTTPS health. This development Mac then checks the mini's API through Tailscale.
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
2. Confirm that Tailscale connects through cellular.
3. Open Coconut and check for a fresh synchronization time.
4. Send a short task, then close the app.
5. Reopen the app and inspect the result.
6. Check that a pending decision survives a backend restart.

Physical iPhone and Mac mini checks remain separate from local tests.
Tailscale does not provide push notifications. The mini sends encrypted alerts through Coconut's shared relay and APNs; see [notification setup](notifications.md).

## Remove Coconut

On the mini, open `~/Coconut/Uninstall Coconut.command`.
The uninstaller stops the owned service and removes the runtime, keys, bots, and stored work.
It also removes this installation's exact `hermes` launcher. Close active Hermes Terminal sessions before removal.
Delete `~/Coconut` to remove its source files afterward.

On this development Mac, the same launcher removes its separate runtime.
Before deleting its runtime, run `python3 scripts/remote.py revoke-access` to remove this installation's SSH authorization from the mini.
This command removes only the exact public key added by **Connect Mac mini.command**. Other SSH keys remain unchanged.
Delete the iPhone app to remove the client. Coconut does not uninstall Tailscale or Command Line Tools.
