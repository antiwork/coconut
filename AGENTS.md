# Development

Ship hyper fast, straight to `main`. Small changes, many times a day. Do not open pull requests or long-lived branches
unless the user asks.

The only testing is manual QA. The repository has no automated tests, no commit hook and no regression CI. Do not
write tests or run test suites before a push.

1. Make the change.
2. QA it by hand: build and use the change the way a person would, in the iPhone simulator, the Mac app or the
   running service. Check the exact thing you changed and the screen or flow around it.
3. If it works, merge it in: fetch, rebase onto `origin/main`, resolve every conflict, and push to `main` at once.

Preserve other changes and never force-push `main`. Promptly revert a change that causes a confirmed fault, then fix
forward. In your report, say what you checked by hand and what you did not.

GitHub runs only release automation. A successful app build can publish from `main` at most once per hour per app.
Pushes within that hour coalesce into the next hourly release. No change to app inputs means no new upload.
An explicit urgent manual Mac release can skip the hourly cooldown.
