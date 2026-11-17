# Development

Work directly on `main`. Do not open pull requests unless the user asks.
Fetch before pushing, preserve other changes, and never force-push `main`.
Resolve all merge conflicts. Promptly revert a change that causes a confirmed fault.

Push fast. The repository has no tests, no commit hook and no regression CI. Do not run checks before a push.

GitHub runs only release automation. A successful app build can publish from `main` at most once per hour per app.
Pushes within that hour coalesce into the next hourly release. No change to app inputs means no new upload.
An explicit urgent manual Mac release can skip the hourly cooldown.
