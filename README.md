# Coconut

A native iOS app for your [Hermes](https://github.com/NousResearch/hermes-agent) agents.
Each agent is one of your Hermes profiles, with its own instructions, memory, skills, and routines.
Coconut keeps the Hermes source unchanged.

- Chat with agents, attach files, and follow their progress.
- Tag other agents with `@` and inspect their exchanges.
- Answer pending decisions, approvals, and questions from one inbox.
- Receive encrypted notifications with message previews and task links.

Your Mac runs Hermes and Coconut and stores the work. Your iPhone reaches the Mac through a shared Cloudflare relay, end-to-end encrypted, so the Mac needs no VPN and no open port. The same relay delivers notifications, so you need no Apple push keys.

## How it works

- **Agents are Hermes profiles.** `default` is your Hermes home, usually `~/.hermes`. Each other agent is `~/.hermes/profiles/<name>`. An agent in Coconut and `hermes -p <name>` in Terminal are the same agent.
- **Turns run in your Hermes gateway.** Coconut runs `hermes gateway` for your Hermes home, and its `coconut` gateway plugin connects the gateway to the iPhone. Routines run in the gateway as Coconut turns. Some turn types still run in Coconut's own workers; [architecture](docs/architecture.md) lists them.
- **Settings are native.** The app's default model is the default profile's model. An agent follows it, unless you give that agent its own model.
- **Hermes knows Coconut.** Coconut ships a `coconut` skill, so your Hermes, for example in Telegram, can update, pair, and repair Coconut.

## Setup

If you use Hermes, Coconut onboarding is one screen on the iPhone. It shows one message for your Hermes agent, for example in Telegram:

```
curl -fsSL https://coconut-updates.gumroad.workers.dev/install | bash -s <code>
```

The command installs the notarized Coconut.app on the agent's Mac, sets up Coconut, and pairs the iPhone by itself. You do not touch the Mac.
See [Pair through Hermes](docs/install-and-update.md#pair-through-hermes), the [Mac setup and pairing guide](docs/install-and-update.md), and [install the iPhone app](docs/iphone-install.md).

Coconut runs on the Hermes that you already use. It does not install its own copy. Without a usable Hermes, the service starts but runs no agents, and the phone shows why.

## Updates

- **Coconut.app** is a menu bar app. Its login items run the service and the relay sidecar. It updates itself with Sparkle. Each merge to `main` publishes a build that every Mac gets. Run `coconut update` to install it at once, and `coconut status` to see the version, the last update, the service, the relay, and Hermes. See [mac/README.md](mac/README.md).
- **Each update** sets Coconut's own entries in your Hermes profiles again: the skill folder and the gateway plugin. It changes no other setting.
- **Hermes** updates with `hermes update`. Coconut sees the new Hermes and restarts itself after the running turns finish.
- **The iPhone app** ships through TestFlight. CI uploads a build when the app changes.

## Development

Use Xcode, XcodeGen, Python 3.11 or newer, and Node.js for the relay.
The app supports iOS 18 or newer. The Mac app supports macOS 14 or newer.

```sh
xcodegen generate --spec ios/project.yml
open ios/Coconut.xcodeproj
```

Run tests without installing Hermes:

```sh
python3 -m pip install Pillow==12.3.0  # The image decoder also ships with Hermes.
PYTHONPATH=backend python3 -m unittest discover -s backend/tests
swift test --package-path mac
(cd relay && npm ci && npm test)
python3 scripts/test_tunnel_loopback.py  # iPhone, relay, and sidecar end to end
```

Tests that need Hermes skip without it. CI runs them against the minimum and the latest supported Hermes. Run `CoconutTests` from Xcode for iOS checks.
See [architecture](docs/architecture.md) for implementation details, [relay transport](docs/relay-transport.md) for the relay, and [relay](relay/README.md) to deploy it.

## Parallel work

Settings → Parallel work sets how many agents work at once. Automatic lets the Mac choose from its processors and memory. Lowering the limit lets running work finish before Coconut starts more.

## Removal

On the Mac, open `~/Coconut/Uninstall Coconut.command`. It stops the service and removes Coconut's data folder and stored work. It removes Coconut's skill and plugin entries from your Hermes profiles. Your Hermes, its profiles, their memories, and their keys stay.
Delete `~/Coconut` to remove the source files afterward, and delete the iPhone app. See [Remove Coconut](docs/install-and-update.md#remove-coconut).
