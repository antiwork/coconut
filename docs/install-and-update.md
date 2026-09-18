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

Bots use `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, and `GH_CONFIG_DIR` inside their existing Hermes profile directory.
Keep these variables unchanged during login and later commands. Do not replace `HOME`.
For interactive login, the bot starts a native Hermes terminal with `pty=true` and `background=true`.
The process remains available while you authorize from your phone.
The bot checks a fresh authenticated request before it reports a successful connection.
Claude Code scopes its Keychain entry through `CLAUDE_CONFIG_DIR`; see [native credential storage](https://code.claude.com/docs/en/authentication#credential-management).

## Pair the iPhone

Install the signed iPhone app before pairing. See [the iPhone setup](iphone-install.md).

1. Enable Tailscale on the iPhone.
2. Open **Pair iPhone.command** on this Mac.
3. Open Coconut Settings on the iPhone.
4. Tap **Scan setup code**.
5. Scan the QR displayed on this Mac.
6. Check the Mac address, then tap **Pair iPhone**.

The QR contains the Mac address, certificate fingerprint, and a one-time code. It does not contain a model key or device token.
The code expires after five minutes. Open the launcher again if it expires.
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
