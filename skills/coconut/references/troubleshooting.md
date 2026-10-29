# Find why Coconut does not work

Start with `coconut status` in `terminal`. Read the `Service`, `Relay`, and `Hermes` lines. Then use the section for the first line that is not healthy. The menu bar icon of Coconut.app shows the same state.

## The service

The `Service` line shows the state and the install folder, for example `Service: running (/Users/name/Coconut)`.

| State | Meaning | What to do |
|---|---|---|
| `running` | The service answers its health check. | Nothing. |
| `starting` | The process runs, but the health check does not answer yet. | Wait. A start can take minutes on a large database. Then read `error.log`. |
| `stopped` | No service process runs. | See "The service is stopped". |
| `not installed` | The install folder has no Coconut. | Run the setup: send the install command without a code, or choose **Set Up Coconut** in the menu. |

### The service is stopped

The service runs as a login item of Coconut.app.

1. Make sure that Coconut.app runs: `open -g /Applications/Coconut.app`.
2. If the menu shows **Allow Coconut in Login Items…**, the person must allow Coconut in **System Settings → General → Login Items**. You cannot do this for the person.
3. Make sure that **Start at Login** is on in the menu. When it is off, the service does not run.
4. Read `error.log` and `service.log`.

## The relay

The iPhone reaches the Mac only through the relay. The relay sidecar keeps an outgoing connection, so the Mac needs no VPN and no open port.

| State | Meaning |
|---|---|
| `connected` | The iPhone can reach the Mac. |
| `connecting`, `starting` | The sidecar connects now. Wait a minute. |
| `offline` | The sidecar cannot reach the relay. Check the internet connection of the Mac. |
| `stopped` | The sidecar does not run. Make sure that Coconut.app runs and that **Start at Login** is on. |
| `not installed` | This Mac has no relay route. The setup is not complete. Run the setup again. |

When the iPhone showed "Relay unreachable", find out why the connection ended:

1. Run `coconut status`. The `Last relay disconnect` line shows the time and the cause of the last end of a connected relay socket, and the count of reconnects since the sidecar started.
2. Read `.runtime/host/tunnel/events.log`, and `events.log.1` for older events. Each line is one JSON event with a UTC time. Find the `disconnected` lines near the time of the problem. `cause` is `relay_closed`, `network_error`, `ping_timeout`, `wake`, `network_changed`, or `invalid_url`. `channel_close` lines with `by: mac` show a failed phone session, and `backend_unreachable` lines show that the sidecar could not reach the service.

Read `tunnel.log` and `tunnel-error.log` for more details.

## Hermes is unavailable

The `Hermes` line shows the Hermes version when Hermes works. Otherwise it shows the reason.

Without a usable Hermes, the service still runs, but it runs no bots. The iPhone can pair and read the reason. Every other request gets the error `503` with the code `hermes_unavailable`.

Common reasons:

- Coconut found no Hermes. Coconut looks at `COCONUT_HERMES_PYTHON` and `COCONUT_HERMES_ROOT`, then at the first `hermes` on the service's `PATH`, then at `~/.hermes/hermes-agent`.
- The installed Hermes cannot load a module that Coconut uses, or it fails the contract check. Repair or update Hermes.
- The Hermes source folder has a `.env` file. Every bot would get those keys, so Coconut refuses it. The keys belong in `~/.hermes/.env`. Tell the person. Do not move keys yourself.

After Hermes changes, the service restarts by itself. If Hermes did not change, but the cause is gone, restart the login item. In this state no bot runs, so a restart stops no work:

```
launchctl kickstart -k gui/$(id -u)/com.gumroad.coconut.service
```

## A gateway that the person started

Coconut runs one Hermes gateway for the Hermes home. A `hermes chat` or `hermes gateway run` command can also start a gateway. When a gateway already runs for the Hermes home, Coconut uses it and does not start its own.

A gateway loads the `coconut` plugin when it starts. After a Coconut update, a gateway that the person started keeps the earlier plugin until it restarts. Coconut restarts only the gateway that it started.

To check, run `pgrep -fl "gateway run"`. In `~/.hermes/logs/gateway.log`, each served profile shows a `✓ coconut connected` line at the start of the gateway. For a profile other than `default`, the line ends with `(profile: <name>)`.

After a Coconut update, ask the person before you stop a gateway that they started. When they agree, stop it. Coconut then starts its own gateway within a minute, with the new plugin.

## An update failed

`coconut status` shows the error on the `Last update error` line. A failed update restores the earlier source, so Coconut keeps running.

- "A managed source file changed": somebody edited a file in the install folder. Tell the person. Do not edit or delete files there.
- "The new version does not work with the installed Hermes": the new Coconut fails the check against this Hermes. Wait for a newer Coconut, or update Hermes.
- For other errors, run `coconut update` once more. If it fails again, report the error to the person.

## Logs and data

All Coconut data is in the install folder (default `~/Coconut`). Read the files with `read_file`.

| Path in the install folder | Content |
|---|---|
| `.runtime/host/logs/service.log` | The output of the service. |
| `.runtime/host/logs/error.log` | The errors of the service. |
| `.runtime/host/logs/install-source.log`, `install-source.log.1` | The full output of each source install, with the traceback of a failure. |
| `.runtime/host/logs/tunnel.log`, `tunnel-error.log` | The relay sidecar. |
| `.runtime/host/tunnel/events.log`, `events.log.1` | The connection events of the relay sidecar, as JSON lines. |
| `.runtime/host/tunnel/status.json` | The state of the relay sidecar, with the last disconnect and its cause. |
| `.runtime/host/hermes.json` | The result of the last Hermes check. |
| `.runtime/host/coconut.sqlite3` | The Coconut database: bots, conversations, and tasks. |
| `.runtime/host/bots/<bot id>/home` | The Hermes profile of one bot: `SOUL.md`, `config.yaml`, `.env`, and its memory. |
| `skills/coconut` | This skill. |

The menu item **Open Logs** opens the logs folder.

## Never do these

- Do not run `scripts/host.py restart` while bots work. It stops the running turns at once, and their tasks fail. Use `coconut update`, which waits for running turns.
- Do not edit, move, or delete files in the install folder. Updates own the source files.
- Do not change the database with `sqlite3`.
- Do not copy model keys, OAuth tokens, or `.env` files between Hermes profiles, into a bot profile, or into chat.
- Do not delete `.runtime`. It holds all conversations and the pairing of the iPhone.
