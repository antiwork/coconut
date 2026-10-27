# coconut-updates

The Cloudflare Worker `coconut-updates` serves the Sparkle feed of Coconut.app at
`https://coconut-updates.gumroad.workers.dev/mac/appcast.xml`. It serves static assets only: the appcast and the archives that it names.

The **Mac release** workflow builds `public/` with `scripts/mac_feed.py`, then runs `wrangler deploy` with this folder's `wrangler.toml`.
The Worker keeps no files between deployments, so each release downloads the archives that it keeps from the live feed.
Each channel keeps its newest five archives. Cloudflare accepts at most 25 MiB for each file.

The Worker also serves two files for one-screen onboarding:

- `/install` is `updates/install.sh`. The iPhone's message for Hermes runs it: `curl -fsSL https://coconut-updates.gumroad.workers.dev/install | bash -s <code>`. It installs the newest notarized Coconut.app and pairs the iPhone. It holds no secret.
- `/mac/download` (`_redirects`) points to the newest stable archive, or to the newest nightly archive before the first stable release. The iPhone shows this link for a manual install.

A change to `install.sh` goes live with the next Mac release, because the release deploys this Worker.

`_headers` gives the appcast a cache time of five minutes, and each archive one day. A path that does not exist answers 404.
The relay Worker `coconut-push` is a different Worker. A release never deploys it.
