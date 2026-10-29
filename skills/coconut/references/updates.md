# Coconut and Hermes updates

Coconut and Hermes update separately. A Coconut update never changes Hermes. A Hermes update never changes Coconut.

## How Coconut updates

Coconut.app updates itself with Sparkle from the Coconut update feed. The app carries the Coconut source. An update has two parts:

1. Sparkle replaces Coconut.app and opens the new app. The service keeps running during this part.
2. The new app installs its source in the install folder (default `~/Coconut`) with `coconut install-source --if-needed`.

The second part is safe for running work:

1. The service starts no new turn. The running bot turns finish. The update waits up to ten minutes for them, then it stops the service, its Hermes gateway, and its workers.
2. The update replaces the source files. It checks each hash first. It refuses to replace a file that somebody changed.
3. It runs the Coconut contract check against the installed Hermes.
4. It starts the service and waits for its health check.
5. If a step fails, it restores the earlier source and the earlier service mode. It does not restore an older database.

If the installed Hermes worked with the earlier Coconut and fails the check of the new one, the update restores the earlier Coconut.

To find why an update failed, read `.runtime/host/logs/install-source.log` in the install folder. It has the output of each step and the traceback of the failure.

## Update Coconut now

Run `coconut update` with `terminal`. The command asks the app to check the update feed. It returns when the install starts, and prints one of these results:

- `Coconut.app <version> is up to date.`
- `Installing Coconut <version> now. ...` The service updates after its running bot work finishes, also the turn that ran the command.
- `The update check failed: <reason>`

Later, run `coconut status`. The update is complete when `Last update result` says `installed`.

Do not add `--wait` inside a bot turn or your own turn. The service update waits for all running turns, so the command would wait for itself. A person in Terminal can use `coconut update --wait`. It waits up to 30 minutes for the end.

## Automatic updates

When automatic updates are on, Coconut.app checks every six hours and installs a new version without a question. The install script turns them on, because often nobody watches the Mac.

- To turn them off: `coconut settings automatic-updates off`.
- To turn them on: `coconut settings automatic-updates on`.
- To show the settings: `coconut settings`.

Ask the person before you change this setting.

Every Mac gets the same builds. Coconut.app has no update channel.

## Read the update state

`coconut status` prints these update lines:

| Line | Meaning |
|---|---|
| `Coconut.app` | The version and the build of the app. |
| `Automatic updates` | `on` or `off`. |
| `Last update check` | The time of the last check, or `never`. |
| `Last update result` | `up to date`, `installing <version>`, `installed <version>`, or `failed`, with a time. |
| `Pending install` | Shows only while the app waits for the service update. The service updates after its running bot work. |
| `Last update error` | The error of a failed update, or `none`. |

## How Hermes updates

The person or Hermes itself updates Hermes, for example with `hermes update`. Coconut does not run `hermes update`.

After a Hermes change, the Coconut service restarts by itself:

1. Once a minute, the service checks whether the Hermes that it would find at start changed.
2. After a change, the service starts no new turn. The running turns finish, for at most ten minutes.
3. The service exits, and its login item starts it again on the new Hermes.
4. The new service runs the contract check against the new Hermes.

While the service waits, the iPhone shows that Coconut restarts for the new Hermes. You do not need to restart Coconut.

If the new Hermes fails the check, Coconut runs without bots and tells the iPhone why. See `references/troubleshooting.md`, "Hermes is unavailable".
