# links

The web form of an invite link, `https://coconut.gumroad.com/i/<token>`, and the file that tells iOS and macOS that the
app owns that path.

```
accepts https://coconut.gumroad.com/i/<token>   → page: the team name, the expiry, a button that opens the app
accepts https://coconut.gumroad.com/i/<token>/  → the same page: a browser adds one slash at the end of a path
accepts https://coconut.gumroad.com/.well-known/apple-app-site-association
                                               → the associated domain file, as Apple reads it
```

A person shares the web form, because a mail or a chat keeps an https link. A Mac without the app, and a person before
they install it, read the page instead: it names the team, says when the invite ends, and links the download. The token
is the same bytes as in `coconut://team/<token>`, so the app opens the same invite from either form
(docs/team-log.md, "Invite and join").

## The secret of a link

The page is about one invite, and the invite is a secret. So:

- The Worker decodes the token and keeps no copy. It never asks the relay for the room, so it never has a room key.
- It writes no log: `[observability] enabled = false` in `wrangler.toml`, and no `console` call.
- The answer has `cache-control: no-store`, `referrer-policy: no-referrer`, `x-robots-tag: noindex, nofollow, noarchive`
  and a content security policy of `default-src 'none'`. So no preview card of a chat caches the link, and the page
  loads nothing from another host.
- An expired invite gets 410 and a token that is not an invite gets 400, as the app refuses them.

## The associated domain file

`/.well-known/apple-app-site-association` names the apps that Apple sends a link to: the iPhone app and the Mac app,
with the team ID and the bundle IDs of `wrangler.toml`. Apple reads it over HTTPS, without a redirect, from the exact
path, so the route of the Worker covers it.

The app declares the same domain in `ios/Coconut/Coconut.entitlements`:

```xml
<key>com.apple.developer.associated-domains</key>
<array>
    <string>applinks:coconut.gumroad.com</string>
</array>
```

The profile of the app needs the Associated Domains capability for that declaration, so the App ID
`com.gumroad.coconut` has it in the developer account. A link then opens the app instead of Safari, and the app joins
from the same token (`TeamLinkCheck`, `onContinueUserActivity` in `ios/Coconut/CoconutApp.swift`).

## Deploy

```
(cd links && npm ci && npx wrangler deploy)
```

The route of `wrangler.toml` puts the Worker on `coconut.gumroad.com`, a proxied DNS record of the zone of the site
(`AAAA 100::`), so the host has no server of its own. `workers_dev` also gives the Worker an address of its own for a
check before the route changes.

## Check

```
curl -sI https://coconut.gumroad.com/i/<token>                          # 200, no-store, noindex
curl -s  https://coconut.gumroad.com/.well-known/apple-app-site-association   # the apps of the team
curl -sI https://coconut.gumroad.com/i/<token of an old invite>        # 410
```
