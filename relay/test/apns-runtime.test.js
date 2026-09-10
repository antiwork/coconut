import test from 'node:test';
import assert from 'node:assert/strict';
import { generateKeyPairSync, verify } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';

test('Cloudflare signs APNs requests and sends only the encrypted envelope', async () => {
  const { privateKey, publicKey } = generateKeyPairSync('ec', { namedCurve: 'prime256v1' });
  const pem = privateKey.export({ format: 'pem', type: 'pkcs8' });
  let deliveries = 0;
  let simulateRedirect = false;
  let outboundFailure;
  const runtime = new Miniflare(convertV4MiniflareOptions({
    compatibilityDate: '2026-09-10', compatibilityFlags: ['nodejs_compat'],
    modules: [
      { type: 'ESModule', path: 'entry.js', contents: `
        import { sendAPNs } from './apns.js';
        export default { async fetch(request, env) {
          const input = await request.json();
          return Response.json(await sendAPNs(env, input.registration, input.envelope,
            input.collapse, input.id, input.expires));
        }};` },
      { type: 'ESModule', path: 'apns.js', contents: readFileSync(new URL('../src/apns.js', import.meta.url), 'utf8') },
    ],
    bindings: { APP_BUNDLE_ID: 'com.gumroad.coconut', APPLE_TEAM_ID: 'TESTTEAM01',
      APNS_DEVELOPMENT_KEY: pem, APNS_DEVELOPMENT_KEY_ID: 'TESTDEV001',
      APNS_PRODUCTION_KEY: pem, APNS_PRODUCTION_KEY_ID: 'TESTPROD01' },
    outboundService: async request => {
      try {
        const token = request.headers.get('authorization').replace('bearer ', '');
        const [header, claims, signature] = token.split('.');
        assert.ok(verify('sha256', Buffer.from(`${header}.${claims}`),
          { key: publicKey, dsaEncoding: 'ieee-p1363' }, Buffer.from(signature, 'base64url')));
        assert.equal(JSON.parse(Buffer.from(claims, 'base64url')).iss, 'TESTTEAM01');
        const development = JSON.parse(Buffer.from(header, 'base64url')).kid === 'TESTDEV001';
        assert.equal(new URL(request.url).hostname, development ? 'api.sandbox.push.apple.com' : 'api.push.apple.com');
        assert.equal(request.headers.get('apns-topic'), 'com.gumroad.coconut');
        const payload = await request.json();
        assert.equal(payload.aps['mutable-content'], 1);
        assert.equal(payload.coconut_encrypted.ciphertext, 'opaque-encrypted-preview');
        assert.equal(payload.coconut, undefined);
        deliveries++;
        return simulateRedirect
          ? new Response(null, { status: 307, headers: { location: 'https://untrusted.test/' } })
          : new Response(null, { status: 200 });
      } catch (error) { outboundFailure = error; return new Response(null, { status: 500 }); }
    },
  }));
  try {
    for (const environment of ['development', 'production', 'development']) {
      simulateRedirect = deliveries === 2;
      const response = await runtime.dispatchFetch('https://test.local', { method: 'POST',
        body: JSON.stringify({ registration: { environment, token: 'a'.repeat(64) },
          envelope: { ciphertext: 'opaque-encrypted-preview' }, collapse: 'b'.repeat(64),
          id: crypto.randomUUID(), expires: Math.floor(Date.now() / 1000) + 60 }) });
      assert.equal(response.status, 200);
      if (outboundFailure) throw outboundFailure;
      assert.deepEqual(await response.json(), simulateRedirect
        ? { status: 503, reason: 'UnexpectedRedirect' } : { status: 200, reason: 'Accepted' });
    }
    assert.equal(deliveries, 3);
  } finally { await runtime.dispose(); }
});
