import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import { verifyRegistration } from './attest.js';
import { sendAPNs } from './apns.js';

const hash = value => createHash('sha256').update(value).digest('hex');
const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(value);
const reply = (value, status = 200) => Response.json(value, { status, headers: { 'Cache-Control': 'no-store' } });
const matches = (value, digest) => typeof value === 'string' && !!digest && timingSafeEqual(Buffer.from(hash(value)), Buffer.from(digest));

export class PushDevice {
  constructor(ctx, env, verify = verifyRegistration, send = sendAPNs) {
    this.ctx = ctx; this.env = env; this.verify = verify; this.send = send;
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
    if (keys.size) await this.ctx.storage.setAlarm(now + 3600000);
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
    if (request.method === 'DELETE') {
      if (!registration || !matches(token, registration.manageHash)) return reply({ error: 'Unauthorized' }, 401);
      registration.enabled = false;
      await store.put('registration', registration);
      await store.delete('registration-reply');
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
}
