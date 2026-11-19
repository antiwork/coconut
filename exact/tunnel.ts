// The phone's end of the tunnel to the Mac, as docs/relay-transport.md defines it and ios/Coconut/TunnelConnection.swift
// speaks it: a pairing setup, the rendezvous box of a pairing code, Noise IK (IKpsk2 with a pairing secret) as the
// initiator, the frames of a session, and HTTP requests as streams. It reads no clock and opens no socket: the data
// module (app.ts) gives it a `Link` to one socket of the relay.
import { concat, derive, noPause, noiseHkdf, open, publicKeyOf, random, seal, sha256, x25519, type Pause } from './crypto.ts';

const encoder = new TextEncoder();
const decoder = new TextDecoder();
export const utf8 = (text: string): Uint8Array => encoder.encode(text);
export const text = (bytes: Uint8Array): string => decoder.decode(bytes);

export const toHex = (bytes: Uint8Array): string => Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
export const fromHex = (hex: string): Uint8Array | null => {
  if (!/^(?:[0-9a-f]{2})*$/.test(hex)) return null;
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
};

const BASE32 = 'abcdefghijklmnopqrstuvwxyz234567';
// Unpadded lowercase base32 (RFC 4648).
export const toBase32 = (bytes: Uint8Array): string => {
  let out = '', buffer = 0, bits = 0;
  for (const b of bytes) {
    buffer = (buffer << 8) | b;
    bits += 8;
    while (bits >= 5) {
      out += BASE32[(buffer >>> (bits - 5)) & 31];
      bits -= 5;
    }
    buffer &= 0xff;
  }
  if (bits > 0) out += BASE32[(buffer << (5 - bits)) & 31];
  return out;
};
export const fromBase32 = (input: string): Uint8Array | null => {
  const out: number[] = [];
  let buffer = 0, bits = 0;
  for (const ch of input.toLowerCase().replace(/=+$/, '')) {
    const v = BASE32.indexOf(ch);
    if (v < 0) return null;
    buffer = ((buffer << 5) | v) & 0xffff;
    bits += 5;
    if (bits >= 8) {
      out.push((buffer >>> (bits - 8)) & 0xff);
      bits -= 8;
    }
  }
  return Uint8Array.from(out);
};

export const toBase64Url = (bytes: Uint8Array): string => {
  let binary = '';
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
};
export const fromBase64Url = (input: string): Uint8Array | null => {
  if (!/^[A-Za-z0-9_-]*$/.test(input)) return null;
  try {
    const binary = atob(input.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (input.length % 4)) % 4));
    return Uint8Array.from(binary, (c) => c.charCodeAt(0));
  } catch {
    return null;
  }
};

// A pairing setup (PairingSetup.swift): the relay, the route of the Mac, its static key, and the one-time secret.
export interface Setup {
  relay: string;
  route: string;
  macKey: Uint8Array;
  secret: Uint8Array;
  // What `POST /v1/pair` takes: the secret in unpadded base64url.
  code: string;
  // Seconds since 1970.
  expires: number;
  name: string;
}

const validRelay = (relay: string): boolean => /^https:\/\/[A-Za-z0-9.-]+(?::\d{1,5})?$/.test(relay);
const make = (relay: string, route: string, macKey: Uint8Array | null, secret: Uint8Array | null, code: string, expires: number, name: string): Setup | null => {
  if (!validRelay(relay) || !/^[0-9a-f]{64}$/.test(route) || macKey?.length !== 32 || secret?.length !== 32) return null;
  const trimmed = name.trim();
  if (trimmed === '' || trimmed.length > 80) return null;
  return { relay, route, macKey, secret, code, expires, name: trimmed };
};

// A version 3 link (`coconut://pair/<base32 token>`) or the JSON of a version 2 setup.
export function parseSetup(input: string): Setup | null {
  const value = input.trim();
  if (value.length > 4096) return null;
  if (value.toLowerCase().startsWith('coconut://pair/')) {
    const data = fromBase32(value.slice('coconut://pair/'.length).split(/[?#]/)[0]);
    if (!data || data.length < 103 || data[0] !== 3) return null;
    const relayEnd = 103 + ((data[101] << 8) | data[102]);
    if (data.length < relayEnd) return null;
    const expires = ((data[97] << 24) >>> 0) + (data[98] << 16) + (data[99] << 8) + data[100];
    const secret = data.slice(65, 97);
    return make(text(data.slice(103, relayEnd)), toHex(data.slice(1, 33)), data.slice(33, 65), secret, toBase64Url(secret), expires,
      text(data.slice(relayEnd)));
  }
  try {
    const fields = JSON.parse(value) as Record<string, unknown>;
    if (fields.version !== 2) return null;
    const { relay, route, mac_key: macKey, secret, expires, name } = fields;
    if (typeof relay !== 'string' || typeof route !== 'string' || typeof macKey !== 'string' || typeof secret !== 'string'
      || typeof expires !== 'number' || typeof name !== 'string') return null;
    return make(relay, route, fromBase64Url(macKey), fromBase64Url(secret), secret, expires, name);
  } catch {
    return null;
  }
}

// A pairing code from the phone (docs/relay-transport.md, "Pairing code from the iPhone"): 16 random bytes as 26
// characters of base32. The Mac seals a setup into the box that the code names.
export interface Box {
  code: string;
  id: string;
  key: Uint8Array;
  // The phone token of the box. It never leaves the phone.
  token: string;
}
export const boxOf = (codeBytes: Uint8Array, token: string): Box => {
  const id = derive(codeBytes, 'coconut rendezvous id');
  return { code: toBase32(codeBytes), id: toHex(id), key: derive(codeBytes, 'coconut rendezvous key'), token };
};
export const newBox = (): Box => boxOf(random(16), toHex(random(32)));

// The setup in a sealed message of the box: nonce (12 bytes) | ciphertext | tag, with the box ID as additional data.
export async function openBox(box: Box, sealed: Uint8Array, pause: Pause = noPause): Promise<Setup | null> {
  if (sealed.length < 29) return null;
  const id = fromHex(box.id);
  if (!id) return null;
  const plain = await open(box.key, sealed.subarray(0, 12), sealed.subarray(12), id, pause);
  return plain ? parseSetup(text(plain)) : null;
}

// The values a pairing derives from its secret, and the client token that its handshake gives.
export const relayTicket = (secret: Uint8Array): string => toHex(derive(secret, 'coconut pairing ticket'));
export const pairingPsk = (secret: Uint8Array): Uint8Array => derive(secret, 'coconut pairing psk');
export const clientToken = (handshakeHash: Uint8Array): string => toHex(derive(handshakeHash, 'coconut relay client'));

// MARK: Noise

// The 12-byte nonce of a counter: 4 zero bytes, then the counter as 64 bits, little-endian. No BigInt: a counter stays
// far below 2^53.
const nonceBytes = (n: number): Uint8Array => {
  const out = new Uint8Array(12);
  const view = new DataView(out.buffer);
  view.setUint32(4, n >>> 0, true);
  view.setUint32(8, Math.floor(n / 0x100000000), true);
  return out;
};
// The last nonce, 2^64 - 1, which only a rekey uses.
const LAST_NONCE = Uint8Array.of(0, 0, 0, 0, 255, 255, 255, 255, 255, 255, 255, 255);

// A cipher with its key and nonce (Noise section 5.1). One owner: two copies would reuse nonces.
export class Cipher {
  key: Uint8Array | null;
  nonce = 0;

  constructor(key: Uint8Array | null = null) {
    this.key = key;
  }

  async encrypt(plaintext: Uint8Array, ad: Uint8Array, pause: Pause = noPause): Promise<Uint8Array> {
    if (!this.key) return plaintext;
    const out = await seal(this.key, nonceBytes(this.nonce), plaintext, ad, pause);
    this.nonce += 1;
    return out;
  }

  async decrypt(ciphertext: Uint8Array, ad: Uint8Array, pause: Pause = noPause): Promise<Uint8Array> {
    if (!this.key) return ciphertext;
    const out = await open(this.key, nonceBytes(this.nonce), ciphertext, ad, pause);
    if (!out) throw new Error('A message from the Mac did not decrypt.');
    this.nonce += 1;
    return out;
  }

  // Noise section 11.3: the first 32 bytes of the encryption of 32 zero bytes at the last nonce. The nonce stays.
  async rekey(): Promise<void> {
    if (!this.key) return;
    this.key = (await seal(this.key, LAST_NONCE, new Uint8Array(32), new Uint8Array(0))).slice(0, 32);
  }
}

class SymmetricState {
  hash: Uint8Array;
  chainingKey: Uint8Array;
  cipher = new Cipher();

  constructor(protocolName: string) {
    const name = utf8(protocolName);
    this.hash = name.length <= 32 ? concat(name, new Uint8Array(32 - name.length)) : sha256(name);
    this.chainingKey = this.hash;
  }

  mixHash(data: Uint8Array): void {
    this.hash = sha256(concat(this.hash, data));
  }

  mixKey(input: Uint8Array): void {
    const [chainingKey, key] = noiseHkdf(this.chainingKey, input, 2);
    this.chainingKey = chainingKey;
    this.cipher = new Cipher(key);
  }

  mixKeyAndHash(input: Uint8Array): void {
    const [chainingKey, hash, key] = noiseHkdf(this.chainingKey, input, 3);
    this.chainingKey = chainingKey;
    this.mixHash(hash);
    this.cipher = new Cipher(key);
  }

  async encryptAndHash(plaintext: Uint8Array): Promise<Uint8Array> {
    const ciphertext = await this.cipher.encrypt(plaintext, this.hash);
    this.mixHash(ciphertext);
    return ciphertext;
  }

  async decryptAndHash(ciphertext: Uint8Array): Promise<Uint8Array> {
    const plaintext = await this.cipher.decrypt(ciphertext, this.hash);
    this.mixHash(ciphertext);
    return plaintext;
  }
}

// The keys of the phone: its static X25519 key, kept with the pairing.
export interface Keys {
  privateKey: Uint8Array;
  publicKey: Uint8Array;
}
export const newKeys = async (pause: Pause = noPause): Promise<Keys> => {
  const privateKey = random(32);
  return { privateKey, publicKey: await publicKeyOf(privateKey, pause) };
};

// The initiator's handshake (NoiseHandshake.swift): message 1 is `-> e, es, s, ss`, message 2 `<- e, ee, se[, psk]`,
// with an empty prologue and empty payloads. A pairing session adds the pre-shared key of its secret.
export class Handshake {
  private state: SymmetricState;
  private ephemeral: Keys | null = null;
  private keys: Keys;
  private macKey: Uint8Array;
  private psk: Uint8Array | null;

  constructor(keys: Keys, macKey: Uint8Array, psk: Uint8Array | null) {
    this.keys = keys;
    this.macKey = macKey;
    this.psk = psk;
    this.state = new SymmetricState(psk ? 'Noise_IKpsk2_25519_ChaChaPoly_SHA256' : 'Noise_IK_25519_ChaChaPoly_SHA256');
    this.state.mixHash(new Uint8Array(0));
    this.state.mixHash(macKey);
  }

  async first(pause: Pause = noPause): Promise<Uint8Array> {
    const ephemeral = await newKeys(pause);
    this.ephemeral = ephemeral;
    this.state.mixHash(ephemeral.publicKey);
    if (this.psk) this.state.mixKey(ephemeral.publicKey);
    this.state.mixKey(await x25519(ephemeral.privateKey, this.macKey, pause));
    const sealedStatic = await this.state.encryptAndHash(this.keys.publicKey);
    this.state.mixKey(await x25519(this.keys.privateKey, this.macKey, pause));
    return concat(ephemeral.publicKey, sealedStatic, await this.state.encryptAndHash(new Uint8Array(0)));
  }

  async finish(reply: Uint8Array, pause: Pause = noPause): Promise<Session> {
    if (!this.ephemeral || reply.length < 48) throw new Error('The Mac did not answer the handshake.');
    const remote = reply.slice(0, 32);
    this.state.mixHash(remote);
    if (this.psk) this.state.mixKey(remote);
    this.state.mixKey(await x25519(this.ephemeral.privateKey, remote, pause));
    this.state.mixKey(await x25519(this.keys.privateKey, remote, pause));
    if (this.psk) this.state.mixKeyAndHash(this.psk);
    await this.state.decryptAndHash(reply.slice(32));
    const [send, receive] = noiseHkdf(this.state.chainingKey, new Uint8Array(0), 2);
    this.ephemeral = null;
    return new Session(new Cipher(send), new Cipher(receive), this.state.hash);
  }
}

// MARK: Frames

export const FRAME = { requestHead: 1, body: 2, end: 3, responseHead: 4, cancel: 5, ping: 6, pong: 7, rekey: 8, window: 9, error: 10 } as const;
export const MAX_PAYLOAD = 60 * 1024;
const INITIAL_WINDOW = 1024 * 1024;
const WINDOW_STEP = 256 * 1024;
const REKEY_AFTER = 1 << 16;

export interface Frame {
  type: number;
  stream: number;
  flags: number;
  payload: Uint8Array;
}

export const encodeFrame = (frame: Frame): Uint8Array => {
  if (frame.payload.length > MAX_PAYLOAD) throw new Error('A frame holds at most 60 KB.');
  const out = new Uint8Array(6 + frame.payload.length);
  out[0] = frame.type;
  new DataView(out.buffer).setUint32(1, frame.stream);
  out[5] = frame.flags;
  out.set(frame.payload, 6);
  return out;
};

export const decodeFrame = (data: Uint8Array): Frame => {
  if (data.length < 6 || data.length - 6 > MAX_PAYLOAD) throw new Error('The Mac sent a frame that is not valid.');
  return { type: data[0], stream: new DataView(data.buffer, data.byteOffset, data.length).getUint32(1), flags: data[5], payload: data.slice(6) };
};

const frame = (type: number, stream: number, payload: Uint8Array = new Uint8Array(0)): Frame => ({ type, stream, flags: 0, payload });

// The ciphers of one connection (TunnelSession). The phone rekeys its send key after 65,536 frames.
export class Session {
  private sent = 0;
  broken = false;
  private sender: Cipher;
  private receiver: Cipher;
  readonly handshakeHash: Uint8Array;

  constructor(sender: Cipher, receiver: Cipher, handshakeHash: Uint8Array) {
    this.sender = sender;
    this.receiver = receiver;
    this.handshakeHash = handshakeHash;
  }

  async seal(f: Frame, pause: Pause = noPause): Promise<Uint8Array[]> {
    const out: Uint8Array[] = [];
    if (this.sent >= REKEY_AFTER) {
      out.push(await this.sender.encrypt(encodeFrame(frame(FRAME.rekey, 0)), new Uint8Array(0)));
      await this.sender.rekey();
      this.sent = 0;
    }
    out.push(await this.sender.encrypt(encodeFrame(f), new Uint8Array(0), pause));
    this.sent += 1;
    return out;
  }

  // The frame of one message from the Mac, or null for a rekey, which only changes the receive key.
  async open(ciphertext: Uint8Array, pause: Pause = noPause): Promise<Frame | null> {
    try {
      const f = decodeFrame(await this.receiver.decrypt(ciphertext, new Uint8Array(0), pause));
      if (f.type !== FRAME.rekey) return f;
      if (f.stream !== 0 || f.flags !== 0 || f.payload.length > 0) throw new Error('The Mac sent a rekey that is not valid.');
      await this.receiver.rekey();
      return null;
    } catch (error) {
      this.broken = true;
      throw error;
    }
  }
}

// MARK: Requests

// One socket of the relay. `next` gives the next binary message, and fails when the socket ends or when none comes
// within `patience` milliseconds. `open` says whether the socket still runs: a timeout leaves it open.
export interface Link {
  send(bytes: Uint8Array): void;
  next(patience: number): Promise<Uint8Array>;
  open(): boolean;
  pause: Pause;
}

export interface Reply {
  status: number;
  headers: Record<string, string>;
  body: Uint8Array;
}

// The patience of a request: how long it waits for each frame of its answer. A long poll (`updates?wait=20`) waits up
// to 25 seconds on the Mac before it answers, so it waits longer.
export const PATIENCE = 20000;

// A request that waits for its answer: the frames that came for its stream, and the wake of the answer that waits.
interface Waiting {
  frames: Frame[];
  wake: (() => void) | null;
}

// HTTP requests as streams of one session, side by side, as the Swift app sends them: a long poll waits on the Mac
// while a message goes. Both keys number their messages, so one answer at a time seals (`sending`) and one at a time
// reads (`reading`): the reader opens the next message of the socket and hands its frame to the request of its
// stream, and the other requests wait for that, each with its own patience. Frames of a stream that no request waits
// for any more are dropped. Only `await` in async functions chains them: Exact lets an answer send on a socket in its
// own turns, which it restores after an `await`, and a plain `.then` callback has no answer.
export class Tunnel {
  private nextStream = 1;
  private sending: Promise<void> = Promise.resolve();
  private reading = false;
  private streams = new Map<number, Waiting>();
  // Why every request fails: the socket ended, or a message did not open.
  private failure: Error | null = null;
  readonly session: Session;
  private link: Link;

  constructor(session: Session, link: Link) {
    this.session = session;
    this.link = link;
  }

  async request(method: string, path: string, headers: Record<string, string>, body?: Uint8Array, patience = PATIENCE): Promise<Reply> {
    if (this.failure) throw this.failure;
    if (this.session.broken) throw new Error('The connection to the Mac ended.');
    const id = this.nextStream;
    this.nextStream = this.nextStream >= 0xfffffffd ? 1 : this.nextStream + 2;
    const waiting: Waiting = { frames: [], wake: null };
    this.streams.set(id, waiting);
    try {
      return await this.exchange(id, waiting, method, path, headers, body ?? new Uint8Array(0), patience);
    } catch (error) {
      // A request that stops before its answer tells the Mac, which then stops it too. The socket stays.
      if (!this.failure && !this.session.broken && this.link.open()) {
        try {
          await this.send(frame(FRAME.cancel, id));
        } catch {
          // The socket ended meanwhile.
        }
      }
      throw error;
    } finally {
      this.streams.delete(id);
    }
  }

  private async send(f: Frame): Promise<void> {
    const previous = this.sending;
    let release = (): void => undefined;
    this.sending = new Promise<void>((resolve) => {
      release = resolve;
    });
    try {
      await previous;
      for (const message of await this.session.seal(f, this.link.pause)) this.link.send(message);
    } finally {
      release();
    }
  }

  // Every request fails with `error`, and the requests that wait learn it now.
  private fail(error: Error): void {
    this.failure = this.failure ?? error;
    for (const w of this.streams.values()) w.wake?.();
  }

  // The next frame of a stream. The request that finds no reader reads one message and hands its frame on; the others
  // wait until a reader hands them one or stops reading, and then look again.
  private async frameOf(waiting: Waiting, patience: number): Promise<Frame> {
    for (;;) {
      const queued = waiting.frames.shift();
      if (queued) return queued;
      if (this.failure) throw this.failure;
      if (!this.reading) {
        this.reading = true;
        try {
          const f = await this.session.open(await this.link.next(patience), this.link.pause);
          if (f && f.type === FRAME.ping) await this.send(frame(FRAME.pong, 0));
          else if (f) this.streams.get(f.stream)?.frames.push(f);
        } catch (error) {
          // A timeout fails this request only. An ended socket or a message that did not open fails them all.
          if (this.session.broken || !this.link.open()) this.fail(error instanceof Error ? error : new Error(String(error)));
          throw error;
        } finally {
          this.reading = false;
          for (const w of this.streams.values()) w.wake?.();
        }
      } else {
        await new Promise<void>((resolve, reject) => {
          const timer = setTimeout(() => {
            waiting.wake = null;
            reject(new Error('Your Mac did not answer in time.'));
          }, patience);
          waiting.wake = () => {
            clearTimeout(timer);
            waiting.wake = null;
            resolve();
          };
        });
        // The reader woke this request inside its own turn: go on in a turn of this request.
        await this.link.pause();
      }
    }
  }

  private async exchange(id: number, waiting: Waiting, method: string, path: string, headers: Record<string, string>, body: Uint8Array,
    patience: number): Promise<Reply> {
    await this.send(frame(FRAME.requestHead, id, utf8(JSON.stringify({ method, path, headers }))));
    let window = INITIAL_WINDOW, offset = 0, status = 0, unacknowledged = 0, finished = false;
    let replyHeaders: Record<string, string> = {};
    const chunks: Uint8Array[] = [];
    const take = async (): Promise<void> => {
      const f = await this.frameOf(waiting, patience);
      switch (f.type) {
        case FRAME.responseHead: {
          const head = JSON.parse(text(f.payload)) as { status?: unknown; headers?: unknown };
          if (typeof head.status !== 'number') throw new Error('The Mac sent an answer that is not valid.');
          status = head.status;
          replyHeaders = (head.headers ?? {}) as Record<string, string>;
          return;
        }
        case FRAME.body:
          chunks.push(f.payload);
          unacknowledged += f.payload.length;
          if (unacknowledged >= WINDOW_STEP) {
            const grant = new Uint8Array(4);
            new DataView(grant.buffer).setUint32(0, unacknowledged);
            unacknowledged = 0;
            await this.send(frame(FRAME.window, id, grant));
          }
          return;
        case FRAME.end:
          if (status === 0) throw new Error('The Mac ended an answer without a status.');
          finished = true;
          return;
        case FRAME.window:
          window += f.payload.length === 4 ? new DataView(f.payload.buffer, f.payload.byteOffset, 4).getUint32(0) : 0;
          return;
        case FRAME.error: {
          const code = (JSON.parse(text(f.payload)) as { code?: string }).code ?? 'error';
          throw new Error(code === 'backend_down' ? 'Coconut on the Mac does not answer.' : `The Mac refused the request (${code}).`);
        }
        case FRAME.cancel:
          throw new Error('The Mac stopped the answer.');
      }
    };
    while (offset < body.length && !finished) {
      while (window <= 0 && !finished) await take();
      const count = Math.min(body.length - offset, window, MAX_PAYLOAD);
      await this.send(frame(FRAME.body, id, body.slice(offset, offset + count)));
      offset += count;
      window -= count;
    }
    if (!finished) await this.send(frame(FRAME.end, id));
    while (!finished) await take();
    return { status, headers: replyHeaders, body: concat(...chunks) };
  }
}

// The first message of a handshake, then the session from the Mac's answer.
export async function connect(link: Link, keys: Keys, macKey: Uint8Array, psk: Uint8Array | null): Promise<Tunnel> {
  const handshake = new Handshake(keys, macKey, psk);
  link.send(await handshake.first(link.pause));
  return new Tunnel(await handshake.finish(await link.next(PATIENCE), link.pause), link);
}
