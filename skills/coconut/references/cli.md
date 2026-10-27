# The `coconut` command

The tool is in the app bundle: `/Applications/Coconut.app/Contents/Helpers/coconut`, or the same path in `~/Applications`. Run it with `terminal`. `coconut help` prints the list below.

| Command | What it does |
|---|---|
| `coconut status` | Prints the app version, the update settings, the last update check and error, and the state of the service, the relay, and Hermes. |
| `coconut status --json` | Prints the state of the service, the relay, and Hermes as JSON. |
| `coconut update` | Checks for a newer Coconut.app now and installs it. Returns when the install starts. See `references/updates.md`. |
| `coconut update --wait` | Also waits up to 30 minutes for the end. Do not use it inside a bot turn. |
| `coconut settings` | Prints the update settings. |
| `coconut settings automatic-updates on` or `off` | Turns automatic updates on or off. |
| `coconut settings channel stable` or `nightly` | Chooses the update channel. |
| `coconut pair` | Prints a one-time iPhone setup link. |
| `coconut pair <code>` | Pairs the iPhone that shows this code. See `references/pairing.md`. |
| `coconut version` | Prints the version of Coconut.app. |

The app and its login items use these commands. Do not run them yourself:

- `coconut install-source` installs the source that the app carries. The app runs it after each update.
- `coconut migrate` moves a service of the earlier LaunchAgents to the app's login items.
- `coconut service` and `coconut sidecar` run the service and the relay sidecar.

## JSON status

`coconut status --json` prints these fields:

| Field | Values |
|---|---|
| `install` | The install folder. |
| `service` | `running`, `starting`, `stopped`, or `not_installed`. |
| `relay` | `connected`, `connecting`, `starting`, `offline`, `stopped`, or `not_installed`. |
| `hermes` | `ok`, or the reason why Hermes cannot run, such as `missing` or `incompatible`. |
| `hermes_version` | The Hermes version, when known. |
| `hermes_reason` | The reason text when Hermes cannot run. |

## Setup and removal

- `Coconut.app --set-up` sets up Coconut without a click, as **Set Up Coconut** in the menu does. The install script opens the app with it.
- Setup adds `<install folder>/skills` to `skills.external_dirs` in each Hermes profile's `config.yaml`, so Hermes loads this skill. Coconut changes the Hermes config only during setup.
- If a Hermes profile does not list this skill, and the person asks you to add it, run `coconut install-source --set-up`. It reinstalls the source like an update, so the running turns finish first. Do not run it inside a bot turn.
- `Uninstall Coconut.command` in the install folder removes Coconut's data and that `skills.external_dirs` entry. Hermes stays. Run it only when the person asks.
