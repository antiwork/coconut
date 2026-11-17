// Sign in with Apple: check an identity token from the iPhone and return its stable user ID (`sub`).
// Apple signs the token with RS256. Its public keys rotate, so the relay caches them for an hour and fetches them again
// for an unknown key ID.
const KEYS_URL = 'https://appleid.apple.com/auth/keys';
const ISSUER = 'https://appleid.apple.com';
let cache = { keys: [], at: 0 };

const decode = part => JSON.parse(Buffer.from(part, 'base64url').toString('utf8'));

async function appleKeys(kid, fetchImpl, now) {
  if (cache.keys.some(key => key.kid === kid) && now - cache.at < 3600000) return cache.keys;
  const response = await fetchImpl(KEYS_URL);
  if (!response.ok) throw new Error('Apple keys unavailable');
  const body = await response.json();
  cache = { keys: Array.isArray(body.keys) ? body.keys : [], at: now };
  return cache.keys;
}

export function resetAppleKeys() { cache = { keys: [], at: 0 }; }

export async function verifyAppleToken(token, env, fetchImpl = fetch, now = Date.now()) {
  if (typeof token !== 'string' || token.length > 4096) throw new Error('Invalid token');
  const parts = token.split('.');
  if (parts.length !== 3) throw new Error('Invalid token');
  const header = decode(parts[0]);
  const claims = decode(parts[1]);
  if (header.alg !== 'RS256' || typeof header.kid !== 'string') throw new Error('Invalid token');
  const jwk = (await appleKeys(header.kid, fetchImpl, now)).find(key => key.kid === header.kid);
  if (!jwk) throw new Error('Unknown key');
  const key = await crypto.subtle.importKey('jwk', { kty: jwk.kty, n: jwk.n, e: jwk.e, alg: 'RS256', ext: true },
    { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['verify']);
  const valid = await crypto.subtle.verify('RSASSA-PKCS1-v1_5', key, Buffer.from(parts[2], 'base64url'),
    new TextEncoder().encode(`${parts[0]}.${parts[1]}`));
  if (!valid) throw new Error('Invalid signature');
  const audiences = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (claims.iss !== ISSUER || !audiences.includes(env.APP_BUNDLE_ID)) throw new Error('Wrong audience');
  if (typeof claims.exp !== 'number' || claims.exp * 1000 <= now) throw new Error('Expired token');
  if (typeof claims.iat === 'number' && claims.iat * 1000 > now + 300000) throw new Error('Future token');
  if (typeof claims.sub !== 'string' || !claims.sub || claims.sub.length > 255) throw new Error('Invalid subject');
  return claims.sub;
}
