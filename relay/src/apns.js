import { sign } from 'node:crypto';

const tokens = new Map();

export async function sendAPNs(env, registration, envelope, collapse, notificationID, expires) {
  const production = registration.environment === 'production';
  const key = production ? env.APNS_PRODUCTION_KEY : env.APNS_DEVELOPMENT_KEY;
  const keyID = production ? env.APNS_PRODUCTION_KEY_ID : env.APNS_DEVELOPMENT_KEY_ID;
  if (!key || !keyID) return { status: 503, reason: 'PushNotConfigured' };
  const now = Math.floor(Date.now() / 1000);
  const cacheKey = `${registration.environment}:${keyID}`;
  let entry = tokens.get(cacheKey);
  if (!entry || now - entry.issued >= 2700 || entry.key !== key) {
    const header = Buffer.from(JSON.stringify({ alg: 'ES256', kid: keyID })).toString('base64url');
    const claims = Buffer.from(JSON.stringify({ iss: env.APPLE_TEAM_ID, iat: now })).toString('base64url');
    const data = `${header}.${claims}`;
    const signature = sign('sha256', Buffer.from(data), { key, dsaEncoding: 'ieee-p1363' });
    entry = { token: `${data}.${signature.toString('base64url')}`, issued: now, key };
    tokens.set(cacheKey, entry);
  }
  const payload = { aps: { alert: { title: 'Coconut', body: 'Open Coconut to read this update.' },
    'mutable-content': 1, sound: 'default', 'thread-id': collapse }, coconut_encrypted: envelope };
  const body = JSON.stringify(payload);
  if (Buffer.byteLength(body) > 4096) return { status: 413, reason: 'PayloadTooLarge' };
  try {
    const host = production ? 'api.push.apple.com' : 'api.sandbox.push.apple.com';
    const response = await fetch(`https://${host}/3/device/${registration.token}`, {
      method: 'POST', body, signal: AbortSignal.timeout(12000), redirect: 'manual',
      headers: { authorization: `bearer ${entry.token}`, 'apns-topic': env.APP_BUNDLE_ID,
        'apns-push-type': 'alert', 'apns-priority': '10', 'apns-id': notificationID,
        'apns-collapse-id': collapse, 'apns-expiration': String(expires) },
    });
    if (response.status >= 300 && response.status < 400) return { status: 503, reason: 'UnexpectedRedirect' };
    const result = response.status === 200 ? {} : await response.json();
    const reason = result.reason ?? (response.status === 200 ? 'Accepted' : 'Rejected');
    if (reason === 'ExpiredProviderToken') tokens.delete(cacheKey);
    return { status: response.status, reason };
  } catch { return { status: 503, reason: 'TransportUnavailable' }; }
}
