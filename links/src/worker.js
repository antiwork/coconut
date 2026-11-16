// The web form of an invite link: https://coconut.gumroad.com/i/<token>. The page opens the app, and the associated
// domain file tells iOS that the app owns this domain, so a tap on the link goes to the app and not to Safari
// (docs/team-log.md, "Invite and join"). The secret of the invite is in the token: this Worker decodes it and keeps no
// copy, so it never logs a link and never asks the relay for one.

const ALPHABET = 'abcdefghijklmnopqrstuvwxyz234567';
const DOWNLOAD = 'https://coconut-updates.gumroad.workers.dev/mac/download';

/// Lowercase base32 without padding, as TeamLog spells a token (ios/Tunnel/TeamLog.swift).
function base32(text) {
  let bits = 0;
  let value = 0;
  const bytes = [];
  for (const letter of text) {
    const index = ALPHABET.indexOf(letter);
    if (index < 0) return null;
    value = (value << 5) | index;
    bits += 5;
    if (bits >= 8) {
      bytes.push((value >>> (bits - 8)) & 0xff);
      bits -= 8;
    }
  }
  return Uint8Array.from(bytes);
}

/// An invite as the token of a link holds it: version 1, room, secret, expiry time, relay and team name. The app reads
/// the same bytes with `TeamLog.invite(link:)`, so a link that the app refuses gets no page here either.
function invite(token) {
  if (!/^[a-z2-7]{40,2048}$/.test(token)) return null;
  const bytes = base32(token);
  if (!bytes || bytes.length < 1 + 32 + 32 + 4 + 2 || bytes[0] !== 1) return null;
  let offset = 1 + 32 + 32;
  const expires = ((bytes[offset] << 24) | (bytes[offset + 1] << 16) | (bytes[offset + 2] << 8) | bytes[offset + 3]) >>> 0;
  offset += 4;
  const relayLength = (bytes[offset] << 8) | bytes[offset + 1];
  offset += 2;
  if (bytes.length < offset + relayLength) return null;
  const name = new TextDecoder().decode(bytes.slice(offset + relayLength));
  if (!name) return null;
  return { token, name, expires };
}

const escape = (text) =>
  text.replace(/[&<>"']/g, (letter) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[letter]);

function document({ title, body }) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>${escape(title)}</title>
<style>
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
padding:24px;font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:#fafafa;color:#111}
@media (prefers-color-scheme:dark){body{background:#0d0d0d;color:#f2f2f2}}
main{max-width:420px;width:100%}
h1{font-size:24px;line-height:1.25;margin:0 0 8px}
p{margin:0 0 20px;opacity:.75}
a.btn{display:block;text-align:center;text-decoration:none;padding:14px 20px;border-radius:999px;
font-weight:600;background:#111;color:#fff}
@media (prefers-color-scheme:dark){a.btn{background:#fff;color:#111}}
.small{font-size:14px;opacity:.6;margin-top:24px}
.small a{color:inherit}
</style></head><body><main>${body}</main></body></html>`;
}

// A page about an invite must leave no copy: no cache, no referrer, and no card in a chat that shows the link again.
const pageHeaders = {
  'content-type': 'text/html; charset=utf-8',
  'cache-control': 'no-store, max-age=0',
  'referrer-policy': 'no-referrer',
  'x-robots-tag': 'noindex, nofollow, noarchive',
  'content-security-policy': "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'",
  'x-content-type-options': 'nosniff',
};

function page({ title, body }, status = 200) {
  return new Response(document({ title, body }), { status, headers: pageHeaders });
}

/// The file that Apple reads before it sends a link to the app: it names the apps of this team and the paths they own
/// (ios/Coconut/Coconut.entitlements).
function associatedDomains(env) {
  const appIDs = [env.APP_BUNDLE_ID, env.MAC_BUNDLE_ID].filter(Boolean).map((id) => `${env.APPLE_TEAM_ID}.${id}`);
  return { applinks: { details: [{ appIDs, components: [{ '/': '/i/*' }] }] } };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return new Response('Method not allowed', { status: 405, headers: { allow: 'GET, HEAD' } });
    }
    if (url.pathname === '/.well-known/apple-app-site-association') {
      return new Response(JSON.stringify(associatedDomains(env)), {
        headers: { 'content-type': 'application/json', 'cache-control': 'public, max-age=3600' },
      });
    }

    const match = url.pathname.match(/^\/i\/(.+)$/);
    if (!match) {
      const body = `<h1>Coconut</h1><p>An invite link looks like <code>coconut.gumroad.com/i/&lt;token&gt;</code>.</p>
<p class="small"><a href="${DOWNLOAD}">Download Coconut for Mac</a></p>`;
      return page({ title: 'Coconut', body }, url.pathname === '/' ? 200 : 404);
    }

    // A browser adds one slash at the end of a path, and the app takes that form too. A path with a broken percent
    // escape is not an invite either.
    let token;
    try {
      token = decodeURIComponent(match[1]);
    } catch {
      token = '';
    }
    const found = invite(token.replace(/\/+$/, ''));
    if (!found) {
      return page(
        { title: 'Invite link not valid', body: '<h1>This invite link is not valid.</h1><p>Ask an admin of the team for a new link.</p>' },
        400,
      );
    }
    if (found.expires * 1000 < Date.now()) {
      return page({
        title: `Invite to ${found.name} expired`,
        body: `<h1>This invite has expired</h1><p>The invite to <strong>${escape(found.name)}</strong> is past its end.
Ask an admin of the team for a new link.</p>`,
      }, 410);
    }
    return page({
      title: `Join ${found.name} on Coconut`,
      body: `<h1>Join ${escape(found.name)} on Coconut</h1>
<p>This invite works once, and expires ${escape(new Date(found.expires * 1000).toUTCString())}.</p>
<a class="btn" href="coconut://team/${escape(found.token)}">Open Coconut</a>
<p class="small">Nothing happened? Make sure Coconut is installed, then tap the button again.
<a href="${DOWNLOAD}">Download Coconut for Mac</a></p>`,
    });
  },
};
