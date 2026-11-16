# Development

Work directly on `main` and push after the local checks pass. Do not open pull requests unless the user asks.
Fetch before pushing, preserve other changes, and never force-push `main`.

Install the local commit hook once per clone: `git config core.hooksPath .githooks`.
The hook runs three service smoke cases for backend changes and the tests next to changed modules or test files.
Each backend test file runs in its own process. Use Python 3.11 or newer; `COCONUT_TEST_PYTHON` selects a runtime.

Choose the smallest additional regression check that covers the change. Run app builds, simulator tests, relay tests,
or native Hermes tests locally when the change needs them. Do not run every suite on every commit.

GitHub runs only release automation. A successful app build can publish from `main` at most once per hour per app.
Pushes within that hour coalesce into the next hourly release. No change to app inputs means no new upload.
