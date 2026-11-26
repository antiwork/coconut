import { sign } from 'node:crypto';

const tokens = new Map();
// A Live Activity that hears nothing for this long says from when it is (stale-date). The Mac sends its glance again
// every few minutes while an agent works or a card waits (backend/coconut/home_screen.py), so only a quiet Mac lets it go
// stale.
export const LIVE_STALE_SECONDS = 600;

// One request to APNs. The provider token of each environment is kept for 45 minutes, as APNs asks.
async function post(env, environment, deviceToken, headers, payload) {
  const production = environment === 'production';
  const key = production ? env.APNS_PRODUCTION_KEY : env.APNS_DEVELOPMENT_KEY;
  const keyID = production ? env.APNS_PRODUCTION_KEY_ID : env.APNS_DEVELOPMENT_KEY_ID;
  if (!key || !keyID) return { status: 503, reason: 'PushNotConfigured' };
  const now = Math.floor(Date.now() / 1000);
  const cacheKey = `${environment}:${keyID}`;
  let entry = tokens.get(cacheKey);
  if (!entry || now - entry.issued >= 2700 || entry.key !== key) {
    const header = Buffer.from(JSON.stringify({ alg: 'ES256', kid: keyID })).toString('base64url');
    const claims = Buffer.from(JSON.stringify({ iss: env.APPLE_TEAM_ID, iat: now })).toString('base64url');
    const data = `${header}.${claims}`;
    const signature = sign('sha256', Buffer.from(data), { key, dsaEncoding: 'ieee-p1363' });
    entry = { token: `${data}.${signature.toString('base64url')}`, issued: now, key };
    tokens.set(cacheKey, entry);
  }
  const body = JSON.stringify(payload);
  if (Buffer.byteLength(body) > 4096) return { status: 413, reason: 'PayloadTooLarge' };
  try {
    const host = production ? 'api.push.apple.com' : 'api.sandbox.push.apple.com';
    const response = await fetch(`https://${host}/3/device/${deviceToken}`, {
      method: 'POST', body, signal: AbortSignal.timeout(12000), redirect: 'manual',
      headers: { authorization: `bearer ${entry.token}`, ...headers },
    });
    if (response.status >= 300 && response.status < 400) return { status: 503, reason: 'UnexpectedRedirect' };
    const result = response.status === 200 ? {} : await response.json();
    const reason = result.reason ?? (response.status === 200 ? 'Accepted' : 'Rejected');
    if (reason === 'ExpiredProviderToken') tokens.delete(cacheKey);
    return { status: response.status, reason };
  } catch { return { status: 503, reason: 'TransportUnavailable' }; }
}

export async function sendAPNs(env, registration, envelope, collapse, notificationID, expires) {
  const payload = { aps: { alert: { title: 'Coconut', body: 'Open Coconut to read this update.' },
    'mutable-content': 1, sound: 'default', 'thread-id': collapse }, coconut_encrypted: envelope };
  return post(env, registration.environment, registration.token, {
    'apns-topic': env.APP_BUNDLE_ID, 'apns-push-type': 'alert', 'apns-priority': '10', 'apns-id': notificationID,
    'apns-collapse-id': collapse, 'apns-expiration': String(expires) }, payload);
}

// An update or the end of the Live Activity of the work of the agents (ActivityKit). Its content state is the sealed
// glance, which the widget extension of the app opens, so APNs and this relay see no text. A new card is urgent
// (priority 10). Every other change goes at priority 5, which iOS does not count against the budget of the activity.
export async function sendLiveActivity(env, registration, activityToken, envelope, event, urgent, expires) {
  const now = Math.floor(Date.now() / 1000);
  const aps = { timestamp: now, event, 'content-state': { sealed: envelope } };
  if (event === 'end') aps['dismissal-date'] = now;
  else aps['stale-date'] = now + LIVE_STALE_SECONDS;
  return post(env, registration.environment, activityToken, {
    'apns-topic': `${env.APP_BUNDLE_ID}.push-type.liveactivity`, 'apns-push-type': 'liveactivity',
    'apns-priority': urgent ? '10' : '5', 'apns-expiration': String(expires) }, { aps });
}

// A reload of the widgets (WidgetKit push, iOS 26). It carries nothing: the widgets then read the sealed glance from
// this relay (GET /v1/routes/<route>/glance).
export async function sendWidgets(env, registration, widgetToken, expires) {
  return post(env, registration.environment, widgetToken, {
    'apns-topic': `${env.APP_BUNDLE_ID}.push-type.widgets`, 'apns-push-type': 'widgets',
    'apns-expiration': String(expires) }, { aps: { 'content-changed': true } });
}
