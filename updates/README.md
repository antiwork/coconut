# coconut-updates

The Cloudflare Worker `coconut-updates` serves the Sparkle feed of Coconut.app at
`https://coconut-updates.gumroad.workers.dev/mac/appcast.xml`. It serves static assets only: the appcast and the archives that it names.

The **Mac release** workflow builds `public/` with `scripts/mac_feed.py`, then runs `wrangler deploy` with this folder's `wrangler.toml`.
The Worker keeps no files between deployments, so each release downloads the archives that it keeps from the live feed.
Each channel keeps its newest five archives. Cloudflare accepts at most 25 MiB for each file.

`_headers` gives the appcast a cache time of five minutes, and each archive one day. A path that does not exist answers 404.
The relay Worker `coconut-push` is a different Worker. A release never deploys it.
