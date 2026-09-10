# Coconut

A native iOS app for messaging [Hermes](https://github.com/NousResearch/hermes-agent) bots.
Each bot is a Hermes profile with its own instructions, memory, conversation, and routines.
Coconut keeps the Hermes source unchanged.

- Chat with bots, attach text files, and follow their progress.
- Tag other bots with `@` and inspect their exchanges.
- Answer pending decisions from one inbox.
- Receive encrypted notifications with message previews and task links.

Your Mac runs the agents and stores their work. Your iPhone connects through HTTPS over Tailscale.
Model requests can use remote providers. The shared Cloudflare relay delivers notifications without giving users Apple push keys.

## Setup

Follow the [Mac setup and pairing guide](docs/install-and-update.md), then [install the iPhone app](docs/iphone-install.md).
The installer keeps its managed Hermes runtime inside `.runtime/`.

## Development

Use Xcode, XcodeGen, Python 3.11 or newer, and Node.js for the relay.
The app supports iOS 18 or newer.

```sh
xcodegen generate --spec ios/project.yml
open ios/Coconut.xcodeproj
```

Run tests without installing Hermes:

```sh
PYTHONPATH=backend python3 -m unittest discover -s backend/tests
(cd relay && npm ci && npm test)
```

Run `CoconutTests` from Xcode for iOS checks.
See [architecture](docs/architecture.md), [Hermes integration](docs/hermes-integration.md), and [notifications](docs/notifications.md) for implementation details.

## Removal

On the execution Mac, run `python3 scripts/cleanup.py --yes` to remove Coconut's service, runtime, and stored work.
Delete the source folder separately. The uninstaller leaves Tailscale and Apple's tools installed.
