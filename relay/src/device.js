import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import { verifyRegistration } from './attest.js';
import { sendAPNs, sendLiveActivity, sendWidgets } from './apns.js';

const hash = value => createHash('sha256').update(value).digest('hex');
const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(value);
const reply = (value, status = 200) => Response.json(value, { status, headers: { 'Cache-Control': 'no-store' } });
const matches = (value, digest) => typeof value === 'string' && !!digest && timingSafeEqual(Buffer.from(hash(value)), Buffer.from(digest));
// An APNs token of a Live Activity or of the widgets: hex, as the one of the app.
const pushToken = value => typeof value === 'string' && /^[a-f0-9]{32,512}$/.test(value) && value.length % 2 === 0;
// APNs no longer knows the token.
const gone = result => result.status === 410 || (result.status === 400 && ['BadDeviceToken', 'DeviceTokenNotForTopic'].includes(result.reason));

// The envelope that the Mac seals for this iPhone (push_relay.py): the same shape for a notification and a glance.
function sealed(envelope, input, registration, now) {
  return !!envelope && envelope.version === 1 && envelope.id === input.id && envelope.keyId === registration.keyId
    && Number.isSafeInteger(input.expires) && input.expires === envelope.expires
    && input.expires * 1000 > now && input.expires * 1000 <= now + 86400000
    && typeof envelope.encapsulatedKey === 'string' && Buffer.from(envelope.encapsulatedKey, 'base64').length === 32
    && typeof envelope.ciphertext === 'string' && Buffer.from(envelope.ciphertext, 'base64').length <= 2216
    && Buffer.from(envelope.ciphertext, 'base64').length >= 16
    && Object.keys(envelope).every(key => ['version', 'id', 'keyId', 'expires', 'encapsulatedKey', 'ciphertext'].includes(key));
}

// One count of requests for each minute and each day. It answers true while both stay under their limits.
function counted(quota, now, perMinute, perDay) {
  const minute = Math.floor(now / 60000), day = Math.floor(now / 86400000);
  if (quota.minute !== minute) { quota.minute = minute; quota.minuteCount = 0; }
  if (quota.day !== day) { quota.day = day; quota.dayCount = 0; }
  if (quota.minuteCount >= perMinute || quota.dayCount >= perDay) return false;
  quota.minuteCount++; quota.dayCount++;
  return true;
}

export class PushDevice {
  constructor(ctx, env, verify = verifyRegistration, send = sendAPNs,
              push = { live: sendLiveActivity, widgets: sendWidgets }) {
    this.ctx = ctx; this.env = env; this.verify = verify; this.send = send; this.push = push;
    this.pending = Promise.resolve();
  }

  fetch(request) {
    const response = this.pending.then(() => this.handle(request));
    this.pending = response.catch(() => {});
    return response.catch(error => error instanceof SyntaxError
      ? reply({ error: 'Invalid request' }, 400)
      : reply({ error: 'Service unavailable' }, 503));
  }

  async alarm() {
    const keys = await this.ctx.storage.list({ prefix: 'receipt:' });
    const now = Date.now();
    for (const key of ['challenge', 'registration-reply']) {
      const value = await this.ctx.storage.get(key);
      if (value?.expires <= now) await this.ctx.storage.delete(key);
    }
    for (const [key, receipt] of keys) if (receipt.expires <= now) await this.ctx.storage.delete(key);
    const glance = await this.ctx.storage.get('glance');
    if (glance?.expires <= now) await this.ctx.storage.delete('glance');
    if (keys.size || glance?.expires > now) await this.ctx.storage.setAlarm(now + 3600000);
  }

  async handle(request) {
    const path = new URL(request.url).pathname;
    const now = Date.now();
    const store = this.ctx.storage;
    if (path === '/v1/challenge') {
      const challenge = randomBytes(32).toString('base64');
      await store.put('challenge', { value: challenge, expires: now + 300000 });
      await store.setAlarm(now + 300000);
      const identity = await store.get('identity');
      return reply({ challenge, registered: !!identity });
    }
    if (path === '/v1/register') {
      const input = await request.json();
      if (typeof input?.payload !== 'string') return reply({ error: 'Invalid registration' }, 400);
      const fingerprint = hash(JSON.stringify(input));
      const previousReply = await store.get('registration-reply');
      if (previousReply?.fingerprint === fingerprint && previousReply.expires > now) return reply(previousReply.value);
      const payload = Buffer.from(input.payload, 'base64');
      const data = JSON.parse(payload.toString('utf8'));
      const challenge = await store.get('challenge');
      if (!challenge || challenge.expires <= now || data.challenge !== challenge.value) return reply({ error: 'Challenge expired' }, 401);
      if (!uuid(data.device_id) || !/^[a-f0-9]{32,512}$/.test(data.token) || data.token.length % 2
          || !['development', 'production'].includes(data.environment)
          || typeof data.encryption_key !== 'string' || Buffer.from(data.encryption_key, 'base64').length !== 32) {
        return reply({ error: 'Invalid registration' }, 400);
      }
      const identity = await store.get('identity');
      let verified;
      try { verified = this.verify(input, payload, identity, this.env); }
      catch { return reply({ error: 'App verification failed' }, 401); }
      if (data.environment === 'production' && verified.environment !== 'production') return reply({ error: 'Environment mismatch' }, 401);
      const sendToken = randomBytes(32).toString('hex');
      const manageToken = randomBytes(32).toString('hex');
      const registration = { token: data.token, environment: data.environment, deviceId: data.device_id,
        keyId: hash(Buffer.from(data.encryption_key, 'base64')), sendHash: hash(sendToken), manageHash: hash(manageToken), enabled: true };
      const result = { route_id: request.headers.get('x-route-id'), send_token: sendToken, manage_token: manageToken };
      await store.put({ identity: verified, registration,
        'registration-reply': { fingerprint, value: result, expires: now + 300000 } });
      await store.delete('challenge');
      return reply(result);
    }
    const registration = await store.get('registration');
    const token = request.headers.get('authorization')?.replace(/^Bearer /, '');
    if (path.endsWith('/live')) return this.live(request, registration, token);
    if (path.endsWith('/glance')) {
      return request.method === 'GET' ? this.readGlance(registration, token, now) : this.glance(request, registration, token, now);
    }
    if (request.method === 'DELETE') {
      if (!registration || !matches(token, registration.manageHash)) return reply({ error: 'Unauthorized' }, 401);
      registration.enabled = false;
      await store.put('registration', registration);
      await store.delete(['registration-reply', 'activity-token', 'widget-token', 'glance']);
      return reply({ revoked: true });
    }
    if (!registration || !matches(token, registration.sendHash)) return reply({ error: 'Unauthorized' }, 401);
    if (!registration.enabled) return reply({ status: 410, reason: 'Unregistered' });
    const input = await request.json();
    if (!input || typeof input !== 'object') return reply({ error: 'Invalid envelope' }, 400);
    const envelope = input.envelope;
    if (!uuid(input.id) || !envelope || envelope.version !== 1 || envelope.id !== input.id
        || Object.keys(input).some(key => !['id', 'expires', 'collapse_id', 'envelope'].includes(key))
        || envelope.keyId !== registration.keyId || !Number.isSafeInteger(input.expires)
        || input.expires !== envelope.expires || input.expires * 1000 <= now || input.expires * 1000 > now + 86400000
        || !/^[a-f0-9]{64}$/.test(input.collapse_id)
        || typeof envelope.encapsulatedKey !== 'string' || Buffer.from(envelope.encapsulatedKey, 'base64').length !== 32
        || typeof envelope.ciphertext !== 'string' || Buffer.from(envelope.ciphertext, 'base64').length > 2216
        || Buffer.from(envelope.ciphertext, 'base64').length < 16
        || Object.keys(envelope).some(key => !['version', 'id', 'keyId', 'expires', 'encapsulatedKey', 'ciphertext'].includes(key))) {
      return reply({ error: 'Invalid envelope' }, 400);
    }
    const receiptKey = `receipt:${input.id}`;
    const fingerprint = hash(JSON.stringify(input));
    const receipt = await store.get(receiptKey);
    if (receipt && receipt.fingerprint !== fingerprint) return reply({ error: 'Notification ID conflict' }, 409);
    if (receipt?.result?.status === 200) return reply(receipt.result);
    if (receipt && receipt.retryAt > now) return reply({ status: 429, reason: 'RetryLater' });
    const quota = await store.get('quota') || { minute: 0, minuteCount: 0, day: 0, dayCount: 0 };
    const minute = Math.floor(now / 60000), day = Math.floor(now / 86400000);
    if (quota.minute !== minute) { quota.minute = minute; quota.minuteCount = 0; }
    if (quota.day !== day) { quota.day = day; quota.dayCount = 0; }
    if (quota.minuteCount >= 30 || quota.dayCount >= 1000) return reply({ status: 429, reason: 'RateLimited' });
    quota.minuteCount++; quota.dayCount++;
    await store.put({ quota, [receiptKey]: { fingerprint, retryAt: now + 30000, expires: now + 172800000 } });
    await store.setAlarm(now + 3600000);
    const result = await this.send(this.env, registration, envelope, input.collapse_id, input.id, input.expires);
    await store.put(receiptKey, { fingerprint, result, retryAt: now + 30000, expires: now + 172800000 });
    if (result.status === 410 || (result.status === 400 && ['BadDeviceToken', 'DeviceTokenNotForTopic'].includes(result.reason))) {
      registration.enabled = false;
      await store.put('registration', registration);
    }
    return reply(result);
  }

  // The iPhone names the APNs token of its Live Activity and of its widgets (iOS 26), with its manage token. A key
  // that the body leaves out keeps its token, and null removes it. The tokens are routing IDs, as the one of the app.
  async live(request, registration, token) {
    if (request.method !== 'PUT') return reply({ error: 'Not found' }, 404);
    if (!registration || !matches(token, registration.manageHash)) return reply({ error: 'Unauthorized' }, 401);
    if (!registration.enabled) return reply({ status: 410, reason: 'Unregistered' });
    const input = await request.json();
    const names = { activity: 'activity-token', widgets: 'widget-token' };
    if (!input || typeof input !== 'object' || Array.isArray(input) || !Object.keys(input).length
        || Object.entries(input).some(([key, value]) => !Object.hasOwn(names, key) || (value !== null && !pushToken(value)))) {
      return reply({ error: 'Invalid tokens' }, 400);
    }
    for (const [key, value] of Object.entries(input)) {
      if (value === null) await this.ctx.storage.delete(names[key]);
      else await this.ctx.storage.put(names[key], value);
    }
    return reply({ ok: true });
  }

  // The newest glance that the Mac sealed for this iPhone, for its widgets. Only the iPhone reads it, with its manage
  // token. 204 when the Mac sent none or the one that it sent expired.
  async readGlance(registration, token, now) {
    if (!registration || !matches(token, registration.manageHash)) return reply({ error: 'Unauthorized' }, 401);
    if (!registration.enabled) return reply({ status: 410, reason: 'Unregistered' });
    const glance = await this.ctx.storage.get('glance');
    if (!glance || glance.expires <= now) return new Response(null, { status: 204, headers: { 'Cache-Control': 'no-store' } });
    return reply({ envelope: glance.envelope });
  }

  // The Mac sends a sealed glance at each change of what the Home Screen shows (backend/coconut/home_screen.py). The
  // relay keeps the newest one for the widgets, gives it to the Live Activity as its content state when `live` says
  // `update` or `end`, and asks the widgets to read it again when `widgets` is true. The relay sees neither its text nor
  // its rows, and keeps no older glance.
  async glance(request, registration, token, now) {
    if (request.method !== 'POST') return reply({ error: 'Not found' }, 404);
    if (!registration || !matches(token, registration.sendHash)) return reply({ error: 'Unauthorized' }, 401);
    if (!registration.enabled) return reply({ status: 410, reason: 'Unregistered' });
    const input = await request.json();
    if (!input || typeof input !== 'object' || !uuid(input.id)
        || Object.keys(input).some(key => !['id', 'expires', 'envelope', 'live', 'urgent', 'widgets'].includes(key))
        || !sealed(input.envelope, input, registration, now)
        || ![undefined, null, 'update', 'end'].includes(input.live)
        || ![undefined, true, false].includes(input.urgent) || ![undefined, true, false].includes(input.widgets)) {
      return reply({ error: 'Invalid glance' }, 400);
    }
    const store = this.ctx.storage;
    // Glances have a count of their own, so a busy day of agents never holds back a notification.
    const quota = await store.get('glance-quota') || { minute: 0, minuteCount: 0, day: 0, dayCount: 0 };
    if (!counted(quota, now, 30, 3000)) return reply({ status: 429, reason: 'RateLimited' });
    await store.put({ 'glance-quota': quota, glance: { envelope: input.envelope, expires: input.expires * 1000 } });
    await store.setAlarm(now + 3600000);
    const result = { status: 200, reason: 'Stored', live: null, widgets: null };
    const activity = input.live ? await store.get('activity-token') : null;
    if (activity) {
      const sent = await this.push.live(this.env, registration, activity, input.envelope, input.live, !!input.urgent, input.expires);
      result.live = sent.reason;
      // An activity that ended or that APNs no longer knows takes no more updates. The app names the next one.
      if (input.live === 'end' || gone(sent)) await store.delete('activity-token');
    }
    const widgets = input.widgets ? await store.get('widget-token') : null;
    if (widgets) {
      const sent = await this.push.widgets(this.env, registration, widgets, input.expires);
      result.widgets = sent.reason;
      if (gone(sent)) await store.delete('widget-token');
    }
    return reply(result);
  }
}
