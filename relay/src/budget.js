// The Personal budget: Antiwork pays for the model use of each person's Personal project, up to a monthly limit.
//
// Antiwork trusts people, so the budget has no sign-in (docs/personal-budget.md). A Mac asks for an account, and the
// relay opens a new one with its own budget and gives the Mac its token. One BudgetAccount Durable Object holds each
// account: the hashes of its tokens and the cost of each calendar month (UTC). The Mac points only the Personal profile
// of Hermes at /v1/budget/anthropic with the token, so other projects never reach this proxy.
//
// The proxy forwards Anthropic Messages calls with Antiwork's key and adds the cost of each call from the `usage` that
// Anthropic returns. When the month's cost reaches the limit, it refuses calls until the 1st of the next month. A call
// that starts below the limit finishes, and at most MAX_INFLIGHT calls run at once, so the overshoot stays small.
import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';

const UPSTREAM = 'https://api.anthropic.com';
const MAX_TOKENS = 10;
const MAX_INFLIGHT = 4;
const PATHS = new Set(['/v1/messages', '/v1/messages/count_tokens']);
const TOKEN = /^cb1\.([a-f0-9]{64})\.([a-f0-9]{64})$/;
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October',
  'November', 'December'];

const hash = value => createHash('sha256').update(value).digest('hex');
const reply = (value, status = 200, headers = {}) =>
  Response.json(value, { status, headers: { 'Cache-Control': 'no-store', ...headers } });
// Anthropic's error shape, so Hermes shows the message.
const refuse = (status, type, message, headers = {}) =>
  reply({ type: 'error', error: { type, message } }, status, headers);

export function monthOf(now = Date.now()) { return new Date(now).toISOString().slice(0, 7); }

export function resetsAt(now = Date.now()) {
  const date = new Date(now);
  return new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 1)).toISOString();
}

export function limitMicros(env) {
  const usd = Number(env.BUDGET_LIMIT_USD);
  return Number.isFinite(usd) && usd > 0 ? Math.round(usd * 1e6) : 0;
}

export function prices(env) {
  const value = JSON.parse(env.BUDGET_PRICES || '{}');
  for (const name of ['input', 'output', 'cache_read', 'cache_write_5m', 'cache_write_1h', 'web_search']) {
    if (typeof value[name] !== 'number' || value[name] < 0) throw new Error(`BUDGET_PRICES lacks ${name}`);
  }
  return value;
}

// The cost of one call in millionths of a dollar. A price per million tokens is a price in millionths per token.
export function costMicros(usage, price) {
  if (!usage) return 0;
  const n = value => (Number.isFinite(value) && value > 0 ? value : 0);
  const creation = usage.cache_creation || {};
  const split = n(creation.ephemeral_5m_input_tokens) + n(creation.ephemeral_1h_input_tokens);
  const write5m = split ? n(creation.ephemeral_5m_input_tokens) : n(usage.cache_creation_input_tokens);
  const write1h = split ? n(creation.ephemeral_1h_input_tokens) : 0;
  return Math.ceil(n(usage.input_tokens) * price.input + n(usage.output_tokens) * price.output
    + n(usage.cache_read_input_tokens) * price.cache_read + write5m * price.cache_write_5m
    + write1h * price.cache_write_1h + n(usage.server_tool_use?.web_search_requests) * price.web_search);
}

// Keep the largest value that any event reported for each count. message_start carries the input counts and
// message_delta the final output count.
function mergeUsage(total, usage) {
  if (!usage || typeof usage !== 'object') return total;
  for (const [key, value] of Object.entries(usage)) {
    if (typeof value === 'number') total[key] = Math.max(total[key] || 0, value);
    else if (value && typeof value === 'object') total[key] = mergeUsage(total[key] || {}, value);
  }
  return total;
}

export function streamUsage(text) {
  let total = {};
  for (const line of text.split('\n')) {
    if (!line.startsWith('data:')) continue;
    try {
      const event = JSON.parse(line.slice(5));
      total = mergeUsage(total, event.usage);
      total = mergeUsage(total, event.message?.usage);
    } catch { /* A partial line at the end of a cut stream. */ }
  }
  return total;
}

export function parseToken(request) {
  const header = request.headers.get('x-api-key') || (request.headers.get('authorization') || '').replace(/^Bearer /, '');
  const match = TOKEN.exec(header);
  return match ? { account: match[1], secret: match[2] } : null;
}

export async function handleBudget(request, env) {
  const path = new URL(request.url).pathname;
  if (!path.startsWith('/v1/budget/')) return null;
  if (!env.ANTHROPIC_API_KEY || !limitMicros(env)) {
    return refuse(503, 'api_error', 'The Personal budget is not available yet.');
  }
  if (path === '/v1/budget/accounts' && request.method === 'POST') {
    // Anyone may open an account: Antiwork chose to trust people over a sign-in. Each request opens a new account with
    // its own budget, and this limit of sign-ups per address is the only brake. An older Mac still sends an identity
    // token of Apple, which the relay no longer reads.
    const rate = await env.REGISTRATION_LIMIT.limit({ key: request.headers.get('cf-connecting-ip') || 'unknown' });
    if (!rate.success) return new Response(null, { status: 429, headers: { 'Retry-After': '60' } });
    const account = randomBytes(32).toString('hex');
    return env.BUDGETS.get(env.BUDGETS.idFromName(account)).fetch(new Request('https://budget/issue', {
      method: 'POST', headers: { 'x-account': account }, body: '{}',
    }));
  }
  const token = parseToken(request);
  if (!token) return refuse(401, 'authentication_error', 'Use the Antiwork budget again in a Personal chat.');
  const stub = env.BUDGETS.get(env.BUDGETS.idFromName(token.account));
  const headers = new Headers(request.headers);
  headers.set('x-token-hash', hash(token.secret));
  headers.set('x-account', token.account);
  headers.delete('x-api-key');
  headers.delete('authorization');
  if (path === '/v1/budget/usage' && request.method === 'GET') {
    return stub.fetch(new Request('https://budget/usage', { headers }));
  }
  if (path === '/v1/budget/token' && request.method === 'DELETE') {
    return stub.fetch(new Request('https://budget/token', { method: 'DELETE', headers }));
  }
  const upstream = path.replace(/^\/v1\/budget\/anthropic/, '');
  if (path.startsWith('/v1/budget/anthropic/') && request.method === 'POST' && PATHS.has(upstream)) {
    return stub.fetch(new Request(`https://budget/proxy${upstream}`, { method: 'POST', headers, body: request.body, duplex: 'half' }));
  }
  return new Response(null, { status: 404 });
}

export class BudgetAccount {
  constructor(ctx, env, fetchImpl = (...args) => fetch(...args)) {
    this.ctx = ctx; this.env = env; this.fetch_ = fetchImpl;
    this.inflight = 0;
  }

  async fetch(request) {
    try {
      return await this.handle(request);
    } catch (error) {
      return error instanceof SyntaxError
        ? refuse(400, 'invalid_request_error', 'Invalid request')
        : refuse(503, 'api_error', 'The Personal budget is unavailable. Try again.');
    }
  }

  async usage(now = Date.now()) {
    const limit = limitMicros(this.env);
    const spent = (await this.ctx.storage.get(`spent:${monthOf(now)}`)) || 0;
    return { month: monthOf(now), spent_usd: spent / 1e6, limit_usd: limit / 1e6, paused: spent >= limit,
      resets_at: resetsAt(now), model: this.env.BUDGET_MODEL };
  }

  async record(micros, now = Date.now()) {
    if (micros <= 0) return;
    const key = `spent:${monthOf(now)}`;
    // No await sits between the read and the write, so a concurrent call cannot lose this cost.
    await this.ctx.storage.put(key, ((await this.ctx.storage.get(key)) || 0) + micros);
  }

  async handle(request) {
    const path = new URL(request.url).pathname;
    const store = this.ctx.storage;
    const tokens = (await store.get('tokens')) || {};
    if (path === '/issue') {
      const secret = randomBytes(32).toString('hex');
      tokens[hash(secret)] = { created: Date.now() };
      // An account gets its token when it opens. The oldest token goes when there are too many.
      const kept = Object.entries(tokens).sort((a, b) => b[1].created - a[1].created).slice(0, MAX_TOKENS);
      await store.put('tokens', Object.fromEntries(kept));
      return reply({ token: `cb1.${request.headers.get('x-account')}.${secret}`, ...(await this.usage()) });
    }
    const presented = request.headers.get('x-token-hash') || '';
    const known = Object.keys(tokens).some(item => item.length === presented.length
      && timingSafeEqual(Buffer.from(item), Buffer.from(presented)));
    if (!known) return refuse(401, 'authentication_error', 'Use the Antiwork budget again in a Personal chat.');
    if (path === '/usage') return reply(await this.usage());
    if (path === '/token' && request.method === 'DELETE') {
      delete tokens[presented];
      await store.put('tokens', tokens);
      return new Response(null, { status: 204 });
    }
    if (path.startsWith('/proxy/')) return this.proxy(request, path.slice('/proxy'.length));
    return new Response(null, { status: 404 });
  }

  async proxy(request, path) {
    const state = await this.usage();
    if (state.paused) {
      const next = new Date(state.resets_at);
      return refuse(403, 'permission_error', `Personal used its $${state.limit_usd} of model use for `
        + `${MONTHS[(next.getUTCMonth() + 11) % 12]}. It starts again on ${MONTHS[next.getUTCMonth()]} 1.`,
        { 'x-coconut-budget': 'paused' });
    }
    const body = await request.json();
    if (body?.model !== this.env.BUDGET_MODEL) {
      return refuse(400, 'invalid_request_error', `The Personal budget covers ${this.env.BUDGET_MODEL} only.`);
    }
    // Fast mode and regional inference cost more than the prices of the ledger.
    if ((body.speed && body.speed !== 'standard') || body.inference_geo) {
      return refuse(400, 'invalid_request_error', 'The Personal budget covers standard requests only.');
    }
    if (path === '/v1/messages' && this.inflight >= MAX_INFLIGHT) {
      return refuse(429, 'rate_limit_error', 'Personal runs a few model calls at a time. Try again shortly.',
        { 'retry-after': '5' });
    }
    const headers = { 'content-type': 'application/json', 'x-api-key': this.env.ANTHROPIC_API_KEY,
      'anthropic-version': request.headers.get('anthropic-version') || '2023-06-01' };
    if (request.headers.get('anthropic-beta')) headers['anthropic-beta'] = request.headers.get('anthropic-beta');
    if (path !== '/v1/messages') {
      // Counting tokens costs nothing.
      const counted = await this.fetch_(UPSTREAM + path, { method: 'POST', headers, body: JSON.stringify(body) });
      return new Response(counted.body, { status: counted.status, headers: { 'content-type': 'application/json' } });
    }
    const price = prices(this.env);
    this.inflight += 1;
    let upstream;
    try {
      upstream = await this.fetch_(UPSTREAM + path, { method: 'POST', headers, body: JSON.stringify(body) });
    } catch (error) {
      this.inflight -= 1;
      throw error;
    }
    const type = upstream.headers.get('content-type') || 'application/json';
    const passed = { 'content-type': type };
    for (const name of ['request-id', 'retry-after']) if (upstream.headers.get(name)) passed[name] = upstream.headers.get(name);
    if (!upstream.ok || !upstream.body) {
      this.inflight -= 1;
      return new Response(upstream.body, { status: upstream.status, headers: passed });
    }
    // One branch goes to the Mac and the meter reads the other to the end, also when the Mac stops reading.
    const [client, meter] = upstream.body.tee();
    const metered = new Response(meter).text()
      .then(text => this.record(costMicros(type.includes('event-stream') ? streamUsage(text)
        : JSON.parse(text).usage, price)))
      .finally(() => { this.inflight -= 1; });
    this.ctx.waitUntil?.(metered);
    this.metering = metered;
    return new Response(client, { status: upstream.status, headers: passed });
  }
}
