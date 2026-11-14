# The `coconut` command

The tool is in the app bundle: `/Applications/Coconut.app/Contents/Helpers/coconut`, or the same path in `~/Applications`. Run it with `terminal`. `coconut help` prints the list below.

| Command | What it does |
|---|---|
| `coconut status` | Prints the app version, the update settings, the last update check and error, and the state of the service, the relay, and Hermes. |
| `coconut status --json` | Prints the state of the service, the relay, and Hermes as JSON. |
| `coconut update` | Checks for a newer Coconut.app now and installs it. Returns when the install starts. See `references/updates.md`. |
| `coconut update --wait` | Also waits up to 30 minutes for the end. Do not use it inside an agent turn. |
| `coconut settings` | Prints the update settings. |
| `coconut settings automatic-updates on` or `off` | Turns automatic updates on or off. |
| `coconut pair` | Prints a one-time iPhone setup link. |
| `coconut pair <code>` | Pairs the iPhone that shows this code. See `references/pairing.md`. |
| `coconut team` | Prints the teams of this Mac: the name, the ID, the state, the admin, the members, the agents, and the sync error. `--json` prints the answer of the service. |
| `coconut team join [--name <text>]` | Joins a team with the invite link on standard input. Never put the link in the command. |
| `coconut team add-agent <agent> [--team <name or ID>]` | On a Mac for a team only: adds an agent of this Mac to the team, by its name or its ID. On a person's Mac it prints how to share a page in the Library. |
| `coconut team remove-agent <agent> [--team <name or ID>]` | On a Mac for a team only: removes an agent of this Mac from the team. On a person's Mac it prints how to share a page. |
| `coconut team leave --confirm <name of the team> [--team <name or ID>]` | Leaves the team. The name after `--confirm` chooses the team; `--team <ID>` is necessary only when two teams have the same name. Without `--confirm`, it prints what a leave does and the exact command for each team. |
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
- Setup adds `<install folder>/skills` to `skills.external_dirs` in each Hermes profile's `config.yaml`, so Hermes loads this skill. It also installs the `coconut` gateway platform. Coconut changes only these entries of the Hermes config. Each update and each start of the service set them again.
- If a Hermes profile does not list this skill, a restart of the Coconut service adds it. Do not restart the service inside an agent turn.
- `Uninstall Coconut.command` in the install folder removes Coconut's data and that `skills.external_dirs` entry. Hermes stays. Run it only when the person asks.
