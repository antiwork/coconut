# Contributing

## Principle: a thin client over Hermes

Coconut is a phone client for [Hermes](https://github.com/NousResearch/hermes-agent). Hermes owns the behavior. Coconut presents it.

- Before you add a feature, check if Hermes already has it. Use the native feature, setting, hook, or plugin API.
- When Hermes and Coconut disagree, change Coconut to match Hermes.
- Do not change the Hermes source. Do not install files into the Hermes checkout.
- Keep a Coconut copy of Hermes data only when the phone must show it. Native files own the data (see the table in [architecture](docs/architecture.md#ownership)).
- Prefer to delete code. A change that removes a Coconut layer is better than a change that adds one.
- Each import of a private Hermes module makes the next upgrade harder. Add one only when no public surface exists, and say why in the PR.
- If Hermes lacks a feature that Coconut needs, write down the gap in the PR. Prefer an upstream change to a local work-around.

A small client is easy to keep up to date with Hermes releases. This matters more than any single feature.

## Changes

- Make one focused change per PR.
- Write the PR title as one imperative sentence that describes the behavior, for example "Delete the recovery-question flow".
- In the PR body, state the problem, the fix, what stays the same, and how you verified it. Give real numbers for speed changes.
- `docs/` holds only setup, architecture and the relay protocol. Fix a page there in the same PR when your change makes it wrong. Do not add pages for reports, plans or one change; put that in the PR description.
- Write docs and comments in short, plain sentences in the present tense.

## Code

- The backend uses the Python standard library only. Do not add a dependency without a strong reason.
- Keep modules small and single-purpose. Do not grow `backend/coconut/service.py`.
- Do not hold the service lock during subprocess calls, network calls, or full table reads.
- The iOS app uses SwiftUI, XcodeGen (`ios/project.yml`), and strict concurrency. Generate the project with `xcodegen generate --spec ios/project.yml`.
- The relay is a Cloudflare Worker in plain JavaScript with exact dependency pins.

## Tests

Run each backend test file in its own process. Module names collide otherwise.

```sh
(for test in backend/tests/test_*.py; do
  PYTHONPATH=backend python3 -m unittest discover -s backend/tests -p "$(basename "$test")" || exit 1
done)
```

- Name each backend test file `test_*.py`. CI runs every such file.
- A test that needs Hermes also goes in the `native-kanban` job of `.github/workflows/hermes-compatibility.yml`.
- A test that needs Hermes uses `backend/tests/hermes_env.py`. It skips without a Hermes. To run it, see [Supported Hermes versions](docs/supported-hermes-versions.md#run-the-native-tests-locally).
- Run the relay tests with `(cd relay && npm ci && npm test)`.
- Run `CoconutTests` from Xcode for iOS changes.

## Hermes versions

Coconut runs on the Hermes that the person installed. `backend/coconut/hermes_versions.json` names the minimum and the tested Hermes releases, and CI checks the minimum and the latest release. To support a new Hermes release, follow [Supported Hermes versions](docs/supported-hermes-versions.md#support-a-new-hermes-release).
