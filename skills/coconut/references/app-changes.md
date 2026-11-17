# Changing the Coconut app (iPhone, Mac, service)

The source is `antiwork/coconut`, checked out at `~/Projects/coconut`. The install folder `~/Coconut` is not the place to edit.

## Procedure

1. Confirm the request against `origin/main` before saying anything about current behavior. A person who describes how the app works ("+ opens a new chat with a random letter") may be describing the change they want. `git fetch origin main` and `git grep ... origin/main -- ios mac backend` show what the app does today.
2. Check that no open PR or branch already does it: `gh pr list -R antiwork/coconut --state open`.
3. Work in a fresh worktree: `git worktree add -b <topic> ~/Projects/coconut-wt-<topic> origin/main`. The main checkout often holds another session's uncommitted work on another branch; never stash or reset it.
4. Follow `AGENTS.md`: commit straight to `main` and push at once, without tests, builds or other checks; no PR unless the person asks. Commit messages describe the resulting behavior. Prefer deleting code that the change makes unused.
5. Most UI flows exist twice: iPhone (`ios/Coconut/*.swift`, e.g. `CoconutApp.swift` root toolbar) and Mac (`mac/Sources/CoconutMac/MacMainWindow.swift`). Put shared logic in an `AppStore` extension and call it from both.
6. Rebase onto the latest `origin/main`, `git push origin HEAD:main`, and confirm `git rev-parse HEAD origin/main` match. Then check the change by hand (see below) and fix forward; revert a change that is confirmed broken.
7. Remove the worktree and its branch. Report: the commit, what changed, what was verified (demo mode vs real Mac service), and whether it has reached the person's devices (see "Releases" below). Never say a push to `main` ships by itself without checking.

## Releases

How builds reach devices changes: read `CONTRIBUTING.md` and `git ls-tree origin/main .github/workflows/` before you promise or trigger a release. Your local checkout can still have workflow files that `main` deleted, and `gh workflow run` then fails with a 422. Do not release: an agent publishes the Mac app and TestFlight builds on a loop a couple of times a day. Say plainly in the reply that the fix is on `main` but not on the devices yet.

## A bug report about the person's own data (counts, read state, lists)

The person sees the installed service, not `main`. Before you write a fix:

1. Grep `origin/main` and `git log origin/main -- <module>` for a recent fix; a rewrite often landed hours ago, and only parts of the request (here a colour) remain to do.
2. Compare with the code that runs: `~/Coconut/backend/coconut/` is the installed service. When it lacks the module or still has the old logic, the cause is the old build. When `coconut status` shows a `Pending install` for hours, check that `~/Coconut/.coconut-install.json` exists: without that marker the app treats the folder as a developer checkout, and `deploy.py` refuses it, so the service never updates.
3. Read the live data read-only: `sqlite3 -readonly ~/Coconut/.runtime/host/coconut.sqlite3` (tables `bots`, `messages`, JSON in `data`). Request results are in `.runtime/host/logs/service.log` as `coconut.request_timing` lines.
4. Prove the fix on real data without touching it: `sqlite3 <live db> ".backup $TMPDIR/copy.sqlite3"`, load the module from the worktree with `importlib.util.spec_from_file_location`, run it against the copy with `/opt/homebrew/bin/python3.14`, print before and after per row, then delete the copy. Put that table in the report.

## The requested feature is already on main

A short request ("Coconut needs to be X") is often for something that landed hours ago but has not reached the person's devices. Step 1 finds it: grep `origin/main` for the feature words and list `docs/` and the top folders of `ios/` (a feature usually has its own doc and target folder). Then build nothing, and answer what the person needs: is it on their Mac and their iPhone?

1. Find the commit and its build number. A build number is the commit count: `git log origin/main --format='%h %s' -- <feature paths>`, then `git rev-list --count <sha>`.
2. Mac: compare with the `Coconut.app` line of `coconut status` (build in parentheses) and with the newest `<sparkle:version>` of `https://coconut-updates.gumroad.workers.dev/mac/appcast.xml`. An installed build at or above the feature's has it. When the feed is ahead of the app, automatic updates install it within the hour (or `coconut update`); check again before replying, since the app often updates while you look.
3. iPhone: a green TestFlight run does not mean an upload. Releases coalesce, so most runs end with the `upload` job `skipped`. Walk `gh run list -R antiwork/coconut -w TestFlight` newest first and read `gh run view <id> --json jobs` until a run has `upload` = `success`; that run's head SHA is what the phone can get. `git merge-base --is-ancestor <feature sha> <run sha>` says whether it holds the feature.
4. Confirm the installed part with evidence, not from the doc: for an app extension, `ls Coconut.app/Contents/PlugIns` and `pluginkit -m -v -i <extension bundle id>`.
5. Report per device: on it now, or which build brings it and whether that upload is running. Add any first-use step that the feature's doc names (e.g. open the app once, enable the extension in System Settings). Say what you did not see with your own eyes (a menu you could not open).

## Check by hand

The repository has no tests. After the push, build the iPhone app into your own simulator and look at the change:

```sh
xcodegen generate --spec ios/project.yml            # after adding a Swift file
cd ios
xcodebuild build -project Coconut.xcodeproj -scheme Coconut \
  -destination 'platform=iOS Simulator,id=<udid>' -derivedDataPath $TMPDIR/dd CODE_SIGNING_ALLOWED=NO
xcrun simctl install <udid> $TMPDIR/dd/Build/Products/Debug-iphonesimulator/Coconut.app
xcrun simctl launch <udid> com.gumroad.coconut --demo
xcrun simctl io <udid> screenshot $TMPDIR/shot.png
```

- Pipe xcodebuild through `grep -E " error:|BUILD (SUCCEEDED|FAILED)"`; full output floods the context.
- Run one xcodebuild at a time, and shut your simulator down when you finish: many agents share this Mac.
- Never launch the debug Mac app: it has the installed app's bundle ID. Never drive the desktop (mouse or keyboard): the person uses this Mac.
- Demo mode skips the Mac service; say so when reporting, with what you checked by hand and what you did not.
