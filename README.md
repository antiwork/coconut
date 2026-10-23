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
If you use Hermes, Coconut onboarding gives you a message for your Hermes agent. The agent runs one command on its Mac:

```sh
python3 ~/Coconut/scripts/setup_coconut.py
```

The command installs Hermes, configures this Mac's Tailscale address, starts the login service, and prints the iPhone setup link.
See [Pair through Hermes](docs/install-and-update.md#pair-through-hermes).
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
python3 -m pip install Pillow==12.3.0  # The image decoder also ships with Hermes.
PYTHONPATH=backend python3 -m unittest discover -s backend/tests
(cd relay && npm ci && npm test)
```

Run `CoconutTests` from Xcode for iOS checks.
See [architecture](docs/architecture.md), [Hermes integration](docs/hermes-integration.md), and [notifications](docs/notifications.md) for implementation details.

## Removal

On the execution Mac, run `python3 scripts/cleanup.py --yes` to remove Coconut's service, runtime, and stored work.
Delete the source folder separately. The uninstaller leaves Tailscale and Apple's tools installed.

### Concurrent work

Settings → Parallel work has two limits:

- **Concurrent work** sets how many independent work items Coconut runs across all bots. Automatic uses the Mac hardware.
- **Helpers per work item** sets the native Hermes delegation limit. Each bot can override this limit.

Each work item uses a separate native Hermes session. Follow-ups for the same work item run in order.
Coconut keeps one Hermes runtime per bot and routes each result to its source task. Cancelling one task preserves other tasks.
Reducing the limit lets active tasks finish before Coconut starts more work. Queue positions use the shared queue.

The adapter does not change Hermes source. To check concurrency against an installed runtime without spending model tokens, run:

```sh
python scripts/test_concurrent_hermes.py --source /path/to/hermes/source --python /path/to/hermes/venv/bin/python
```

The check uses a local model stub with native Hermes sessions, tools, bot relay, delegation results, and cancellation.
