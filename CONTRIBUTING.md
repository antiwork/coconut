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

- Work directly on `main` and push. Do not open a pull request unless asked.
- Make each commit one focused change and describe the resulting behavior in its message.
- `docs/` holds only setup, architecture and the relay protocol. Fix a page there when your change makes it wrong. Do not add pages for reports, plans or one change.
- Write docs and comments in short, plain sentences in the present tense.

## Code

- The backend uses the Python standard library only. Do not add a dependency without a strong reason.
- Keep modules small and single-purpose. Do not grow `backend/coconut/service.py`.
- Do not hold the service lock during subprocess calls, network calls, or full table reads.
- The iOS app uses SwiftUI, XcodeGen (`ios/project.yml`), and strict concurrency. Generate the project with `xcodegen generate --spec ios/project.yml`.
- The relay is a Cloudflare Worker in plain JavaScript with exact dependency pins.

## Tests

The repository has no tests, no commit hook and no GitHub workflows. Push without running checks. Releases are manual.

## Hermes versions

Coconut runs on the Hermes that the person installed. `backend/coconut/hermes_versions.json` names the minimum and
the tested Hermes releases. Check compatibility locally when the Hermes contract changes. To support a new release,
follow [Supported Hermes versions](docs/supported-hermes-versions.md#support-a-new-hermes-release).
