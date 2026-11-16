import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';

const ALPHABET = 'abcdefghijklmnopqrstuvwxyz234567';

/// Lowercase base32 without padding, as TeamLog spells a token.
function base32(bytes) {
  let bits = 0;
  let value = 0;
  let text = '';
  for (const byte of bytes) {
    value = (value << 8) | byte;
    bits += 8;
    while (bits >= 5) {
      text += ALPHABET[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
  }
  if (bits > 0) text += ALPHABET[(value << (5 - bits)) & 31];
  return text;
}

/// A token in the form of the contract: version 1, room, secret, expiry time, relay and team name.
function token({ expires = Math.floor(Date.now() / 1000) + 600, name = 'Example Team', version = 1 } = {}) {
  const relay = new TextEncoder().encode('https://relay.example');
  const bytes = [version, ...new Array(64).fill(7), (expires >>> 24) & 0xff, (expires >>> 16) & 0xff,
    (expires >>> 8) & 0xff, expires & 0xff, relay.length >> 8, relay.length & 0xff, ...relay,
    ...new TextEncoder().encode(name)];
  return base32(bytes);
}

const runtime = new Miniflare(convertV4MiniflareOptions({
  compatibilityDate: '2026-09-10',
  modules: [{ type: 'ESModule', path: 'worker.js', contents: readFileSync(new URL('../src/worker.js', import.meta.url), 'utf8') }],
  bindings: { APP_BUNDLE_ID: 'com.gumroad.coconut', MAC_BUNDLE_ID: 'com.gumroad.coconut.mac', APPLE_TEAM_ID: 'TESTTEAM01' },
}));

const get = (path) => runtime.dispatchFetch('https://coconut.gumroad.com' + path);
after(() => runtime.dispose());

test('The page of an invite names the team and opens the app without keeping a copy', async () => {
  const link = token();
  const response = await get('/i/' + link);
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /<h1>Join Example Team on Coconut<\/h1>/);
  assert.match(html, new RegExp(`href="coconut://team/${link}"`));
  // A browser adds one slash at the end of a path, and the app takes that form too.
  assert.equal((await get('/i/' + link + '/')).status, 200);
  // The page holds a secret: no cache, no referrer, no card in a chat, and no image or script from another host.
  assert.equal(response.headers.get('cache-control'), 'no-store, max-age=0');
  assert.equal(response.headers.get('referrer-policy'), 'no-referrer');
  assert.equal(response.headers.get('x-robots-tag'), 'noindex, nofollow, noarchive');
  assert.match(response.headers.get('content-security-policy'), /default-src 'none'/);
  // A name can hold markup, and the page shows it as text.
  const markup = await (await get('/i/' + token({ name: '<b>Team</b> & "co"' }))).text();
  assert.match(markup, /<h1>Join &lt;b&gt;Team&lt;\/b&gt; &amp; &quot;co&quot; on Coconut<\/h1>/);
});

test('An invite that is past its end says so, and a link that is not an invite says that', async () => {
  const past = await get('/i/' + token({ expires: 1_600_000_000 }));
  assert.equal(past.status, 410);
  assert.match(await past.text(), /This invite has expired/);
  // The app refuses these forms too, so the page does not promise more than the app does.
  for (const bad of [token().toUpperCase(), 'x', token({ version: 2 }), token().slice(0, 60)]) {
    const response = await get('/i/' + bad);
    assert.equal(response.status, 400, bad.slice(0, 12));
  }
  assert.equal((await get('/i/')).status, 404);
  assert.equal((await get('/team/' + token())).status, 404);
});

test('The associated domain file names the apps of the team and the path of an invite', async () => {
  const response = await get('/.well-known/apple-app-site-association');
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('content-type'), 'application/json');
  const file = await response.json();
  assert.deepEqual(file, { applinks: { details: [{
    appIDs: ['TESTTEAM01.com.gumroad.coconut', 'TESTTEAM01.com.gumroad.coconut.mac'],
    components: [{ '/': '/i/*' }],
  }] } });
});

test('The root names the form of an invite link, and a Worker takes no write', async () => {
  assert.match(await (await get('/')).text(), /coconut\.gumroad\.com\/i\//);
  const response = await runtime.dispatchFetch('https://coconut.gumroad.com/i/' + token(), { method: 'POST' });
  assert.equal(response.status, 405);
  assert.equal(response.headers.get('allow'), 'GET, HEAD');
});
