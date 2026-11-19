// The data module of Coconut on Exact. Paired with a Mac, it answers the screens from the state of the Mac's Coconut,
// read through the relay with the tunnel of the Swift app (tunnel.ts, docs/relay-transport.md), and sends messages,
// answers, stops and reads back to it. Before a pairing it shows a pairing code; "Look around with demo data" answers
// from the preview fixture of the Swift app (ios/Coconut/Fixtures/PreviewState.json, copied as preview.json) and the
// Gumroad team of its team preview (team-preview.json), in memory until the app launches again.
//
// The connection. The relay takes a socket without headers only with a socket ticket (`prepare` asks for it over
// HTTPS), so `socket` opens a stream resource with `?ticket=`. Its mapper only queues the bytes that come and wakes the
// answer that waits for them: the Noise handshake and the frames run in answers (`sync`, the writes), which pause
// between steps so each turn stays inside its time budget. A waiting answer goes on when another answer is asked, so
// `deliver` is asked for each event of the socket. Each connection is one round: a closed socket or an error starts the
// next round with a new ticket and a new handshake. The first read takes the Mac's whole state; each read after it
// waits on the Mac for the next change (`updates?wait=20`) and merges it, while writes go beside it on the same socket.
//
// Time. A data module reads no clock. A time of the fixture is in milliseconds from the launch (its history is
// negative: the fixture moves its server_time to the launch, as PreviewStateFixture.decode does), and a source adds the
// launch (`zero`, the contract's `time.epochAtZero`). A state of the Mac is in milliseconds from its server_time, which
// is its `zero`. So the screens hold times since 1970.
import type { Answer, Result, Sources, StreamEvent, StreamSocket, Store } from './app.contract.d.ts';
import { noPause, type Pause } from './crypto.ts';
import {
  clientToken, connect, fromBase64Url, newBox, newKeys, openBox, pairingPsk, relayTicket, text, toBase64Url, utf8,
  type Box, type Keys, type Link, type Reply, type Setup, type Tunnel,
} from './tunnel.ts';
import preview from './preview.json';
import team from './team-preview.json';

// The relay of Coconut. A pairing setup that names another relay is refused, because the grants name this one.
const RELAY = 'https://coconut-push.gumroad.workers.dev';
const SAVED = 'coconut-pairing';
export const grants = `net.fetch ${RELAY}\nnet.websocket wss://coconut-push.gumroad.workers.dev\nsecret.keep ${SAVED}`;
// How long a read of updates waits for each frame of its answer: the Mac holds it up to 25 seconds (the Swift app
// gives a long poll 45).
const LONG_POLL = 45000;

type Home = Result<'loadHome'>;
type Chat = Result<'loadChat'>;
type Project = Home['projects'][number];
type Row = Home['chats'][number];
type Attention = Home['attention'][number];
type Message = Chat['messages'][number];
type Turn = Chat['turn'];
type Live = Result<'sync'>;
type Sock = Result<'socket'>;

// The parts of the fixture that are read here.
interface FxOption { id: string; label: string; explanation: string }
interface FxDecision { id: string; bot_id: string; task_id: string; title: string; explanation: string; consequences: string; proposal_preview: string; status: string; created_at: string; options: FxOption[] }
interface FxArtifact { id: string; task_id: string; title: string; preview: string }
interface FxMessage { id: string; bot_id: string; task_id: string | null; kind: string; role: string; text: string; created_at: string }
interface FxTask { id: string; bot_id: string; status: string; updated_at: string }
interface FxBot { id: string; name: string; avatar: string; archived: boolean; unread_count: number; chat_of: string | null; chat_title: string | null; effective_model: { model: string; reasoning_effort: string } }
interface FxModel { id: string; name: string; reasoning_efforts: string[]; default_reasoning_effort: string }
interface Fixture {
  server_time: string;
  bots: FxBot[];
  messages: FxMessage[];
  tasks: FxTask[];
  decisions: FxDecision[];
  artifacts: FxArtifact[];
  model_catalog: { default: { model: string; reasoning_effort: string }; models: FxModel[] };
}
const fx = preview as unknown as Fixture;

const SECOND = 1000, MINUTE = 60000, HOUR = 3600000, DAY = 86400000;

// An ISO 8601 time in milliseconds since 1970, by the days of the civil calendar: no Date, which a data module may not
// read for the time.
const daysFromCivil = (year: number, month: number, day: number): number => {
  const y = month <= 2 ? year - 1 : year;
  const era = Math.floor(y / 400);
  const yoe = y - era * 400;
  const doy = Math.floor((153 * (month + (month > 2 ? -3 : 9)) + 2) / 5) + day - 1;
  return era * 146097 + yoe * 365 + Math.floor(yoe / 4) - Math.floor(yoe / 100) + doy - 719468;
};
const parse = (iso: string): number => {
  const m = /^(\d+)-(\d+)-(\d+)T(\d+):(\d+):(\d+)/.exec(iso);
  return m ? daysFromCivil(Number(m[1]), Number(m[2]), Number(m[3])) * DAY + Number(m[4]) * HOUR + Number(m[5]) * MINUTE + Number(m[6]) * SECOND : 0;
};

const LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';
// AgentLetter.colors: the face of a letter is its place in the alphabet, modulo the eight faces.
const faceOf = (letter: string): number => Math.max(0, LETTERS.indexOf(letter)) % 8;
// AgentLetter.letter: the letter of an avatar (`letter.R`), else the initial of the name.
const letterOf = (avatar: string, name: string): string => {
  const m = /^letter\.([A-Z])$/.exec(avatar);
  return m ? m[1] : (name.trim().slice(0, 1).toUpperCase() || 'A');
};

// BotPhase.label.
const PHASES: Record<string, string> = {
  thinking: 'Thinking it through', working: 'Working on it', reading: 'Reading files', editing: 'Editing files',
  command: 'Running a command', testing: 'Running tests', searching: 'Searching files', browsing: 'Searching the web',
  memory: 'Checking memory', github: 'Working in GitHub', helpers: 'Working with helpers', writing: 'Writing…',
};

interface Msg { id: string; kind: string; mine: boolean; author: string; at: number; title: string; body: string; detail: string; ref: string; status: string; receipt: string; options: FxOption[] }
interface Run { startedAt: number; phase: string; detail: string; model: string; queued: number }
interface ChatState {
  id: string;
  kind: 'agent' | 'team' | 'direct';
  project: string;
  projectName: string;
  title: string;
  letter: string;
  face: number;
  circle: boolean;
  messages: Msg[];
  run: Run | null;
  model: string;
  effort: string;
  unread: number;
  // BotHealth.label of an agent that cannot run, or an empty string.
  health: string;
}
interface Notice { id: string; type: string; text: string; at: number; author: string; seen: boolean; active: boolean }
// A card that waits for the person, with the chat that holds it and the time it was made.
interface Wait { ref: string; chat: string; at: number }
// A To-do that got blocked, with the chat of its agent.
interface Stuck { title: string; chat: string; at: number }
interface State {
  // The epoch of time 0: null for the fixture, whose 0 is the launch; the server_time of a state of the Mac.
  zero: number | null;
  chats: Map<string, ChatState>;
  projects: Project[];
  notices: Notice[];
  waits: Wait[];
  stuck: Stuck[];
  teamId: string;
  counter: number;
}

const message = (id: string, kind: string, mine: boolean, at: number, body: string, extra: Partial<Msg> = {}): Msg => ({
  id, kind, mine, author: '', at, title: '', body, detail: '', ref: '', status: '', receipt: '', options: [], ...extra,
});

const kindName = (kind: string): string => kind.slice(0, 1).toUpperCase() + kind.slice(1);
const modelOf = (id: string): FxModel => fx.model_catalog.models.find((m) => m.id === id) ?? fx.model_catalog.models[0];

// A short age, as AttentionRules.age gives it: 12m, 5h, 3d.
const shortAge = (ms: number): string => {
  const minutes = Math.max(0, Math.floor(ms / MINUTE));
  return minutes < 60 ? `${minutes}m` : minutes < 48 * 60 ? `${Math.floor(minutes / 60)}h` : `${Math.floor(minutes / (24 * 60))}d`;
};

const build = (): State => {
  const server = parse(fx.server_time);
  const rel = (iso: string): number => parse(iso) - server;
  const chats = new Map<string, ChatState>();
  const projects: Project[] = [];
  const waits: Wait[] = [];
  const bySent = (a: FxMessage, b: FxMessage): number => (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0);

  for (const bot of fx.bots) {
    const letter = letterOf(bot.avatar, bot.name);
    const messages = fx.messages.filter((m) => m.bot_id === bot.id).sort(bySent).map((m): Msg => {
      const at = rel(m.created_at);
      // A card and a file are messages with no words: the task they belong to names them.
      const decision = m.kind === 'decision' ? fx.decisions.find((d) => d.task_id === m.task_id) : undefined;
      const artifact = m.kind === 'artifact' ? fx.artifacts.find((a) => a.task_id === m.task_id) : undefined;
      if (decision) {
        return message(m.id, 'decision', false, at, decision.explanation, {
          title: decision.title,
          detail: '```diff\n' + decision.proposal_preview.trimEnd() + '\n```\n\n' + decision.consequences,
          ref: decision.id,
          status: decision.status === 'pending' ? 'pending' : 'replied',
          options: decision.options.map((o) => ({ id: o.id, label: o.label, explanation: o.explanation })),
        });
      }
      if (artifact) return message(m.id, 'artifact', false, at, artifact.preview, { title: artifact.title });
      return message(m.id, m.kind === 'activity' ? 'activity' : 'text', m.role === 'user', at, m.text);
    });
    if (!bot.chat_of) projects.push({ id: bot.id, name: bot.name, letter, face: faceOf(letter) });
    chats.set(bot.id, {
      id: bot.id, kind: 'agent', project: bot.chat_of ?? bot.id, projectName: bot.name, title: bot.chat_title ?? bot.name,
      letter, face: faceOf(letter), circle: false, messages, run: null, model: bot.effective_model.model,
      effort: bot.effective_model.reasoning_effort, unread: bot.unread_count, health: '',
    });
    for (const d of fx.decisions) {
      if (d.bot_id === bot.id && d.status === 'pending') waits.push({ ref: d.id, chat: bot.id, at: rel(d.created_at) });
    }
  }

  // A task that works is the running turn of its agent. The fixture has no turn_started_at and no phase, so the turn
  // starts at the last update of the task and the phase is the one the app shows first, as AppStore.runningPhase does.
  for (const task of fx.tasks) {
    const chat = chats.get(task.bot_id);
    if (!chat || task.status !== 'working') continue;
    chat.run = { startedAt: rel(task.updated_at), phase: PHASES.thinking, detail: '', model: modelOf(chat.model).name, queued: 0 };
  }

  // What AppStore.applyAttentionPreview does for the UI test of Home: a card that waits for 5 hours, an agent with a
  // provider limit and a blocked To-do, all of the first agent. Without it a clear day shows no Needs attention.
  const stuck: Stuck[] = [];
  const first = fx.bots.find((b) => !b.archived && !b.chat_of);
  if (first) {
    for (const w of waits) if (w.chat === first.id) w.at = -5 * HOUR;
    const chat = chats.get(first.id);
    if (chat) chat.health = 'Rate limited · 2 waiting';
    stuck.push({ title: 'Ship the retry fix', chat: first.id, at: -40 * MINUTE });
  }

  // The team: its room, the notices on its board, and a direct chat. The notices are lines of the room here.
  const teamId = `team-${team.room}`;
  const notices: Notice[] = team.notices.map((n) => ({
    id: n.id, type: n.type, text: n.text, at: -n.minutes_ago * MINUTE, author: n.author, seen: n.seen, active: n.active,
  }));
  chats.set(teamId, {
    id: teamId, kind: 'team', project: '', projectName: team.name, title: team.name, letter: team.name.slice(0, 1).toUpperCase(),
    face: faceOf(team.name.slice(0, 1).toUpperCase()), circle: true, run: null, model: '', effort: '', unread: 0, health: '',
    messages: [
      message(`${teamId}-last`, 'text', false, -team.last.minutes_ago * MINUTE, team.last.text, { author: team.last.name }),
      ...notices.map((n) => message(`notice-${n.id}`, 'notice', false, n.at, n.text, { author: n.author, title: kindName(n.type) })),
    ].sort((a, b) => a.at - b.at),
  });
  for (const direct of team.directs) {
    const letter = direct.name.slice(0, 1).toUpperCase();
    chats.set(`direct-${direct.member}`, {
      id: `direct-${direct.member}`, kind: 'direct', project: '', projectName: direct.name, title: direct.name, letter, face: faceOf(letter),
      circle: true, run: null, model: '', effort: '', unread: direct.unread, health: '',
      messages: [message(`direct-${direct.member}-1`, 'text', false, -direct.minutes_ago * MINUTE, direct.text, { author: direct.name })],
    });
  }
  return { zero: null, chats, projects, notices, waits, stuck, teamId, counter: 0 };
};

// The fixture's state is built once, when the first source asks, and not at the top of the module: the top of a data
// module runs before the first frame. The Mac's state is the newest that `sync` read. `useLive` follows the screens.
let demoState: State | undefined;
let liveState: State | undefined;
let useLive = false;
const ensure = (): State => {
  if (useLive) return liveState ?? emptyState();
  if (!demoState) demoState = build();
  return demoState;
};
const emptyState = (): State => ({ zero: null, chats: new Map(), projects: [], notices: [], waits: [], stuck: [], teamId: '', counter: 0 });
// The epoch to add to the times of a state.
const zeroOf = (s: State, launch: number): number => s.zero ?? launch;

// The newest message that is no notice: the age and the place of the row of a chat.
const newest = (chat: ChatState): number => chat.messages.reduce((t, m) => (m.kind === 'notice' ? t : Math.max(t, m.at)), -Infinity);
const waitingCard = (chat: ChatState): boolean => chat.messages.some((m) => m.kind === 'decision' && m.status === 'pending');

// The line under the name of a chat: the newest text, with who wrote it (ChatRowText).
const lastLine = (chat: ChatState): string => {
  for (let i = chat.messages.length - 1; i >= 0; i--) {
    const m = chat.messages[i];
    if (m.kind !== 'text' || m.body.trim() === '') continue;
    const who = m.mine ? 'You: ' : chat.kind === 'team' && m.author !== '' ? `${m.author}: ` : '';
    return who + m.body.replace(/\s+/g, ' ').trim();
  }
  return 'No messages yet';
};

// BotRowSubtitle: a card that waits first, then the phase of the work, then a health, then the last message.
const rowOf = (chat: ChatState, zero: number): Row => {
  const status = waitingCard(chat) ? 'needs_you' : chat.run ? 'working' : chat.health !== '' ? 'health' : 'idle';
  const line = status === 'needs_you' ? 'Needs you' : status === 'working' && chat.run ? chat.run.phase : status === 'health' ? chat.health : lastLine(chat);
  const at = newest(chat);
  return {
    id: chat.id, kind: chat.kind, title: chat.title, letter: chat.letter, face: chat.face, circle: chat.circle, line, status,
    active: chat.run !== null, unread: chat.unread, at: Number.isFinite(at) ? zero + at : zero,
  };
};

// AttentionRules.items: the rows in a fixed order, each one only while something breaches its rule.
const attention = (s: State): Attention[] => {
  const items: Attention[] = [];
  const row = (id: string, chat: string, label: string, detail: string, count: number, symbol: string): void => {
    items.push({ id, chat, label, detail, value: String(count), count, symbol: `symbol:sf/${symbol}` });
  };
  // A card that waits on the person for more than four hours.
  const overdue = s.waits.filter((w) => -w.at > 4 * HOUR).sort((a, b) => a.at - b.at);
  if (overdue.length > 0) {
    row('waiting', overdue[0].chat, overdue.length === 1 ? 'Card waiting on you' : 'Cards waiting on you', `Past 4h · oldest ${shortAge(-overdue[0].at)}`, overdue.length, 'hourglass');
  }
  // An agent that cannot run: a provider limit or an error.
  const halted = Array.from(s.chats.values()).filter((c) => c.kind === 'agent' && c.health !== '' && c.project === c.id);
  if (halted.length > 0) {
    const names = halted.slice(0, 2).map((c) => c.projectName).join(', ') + (halted.length > 2 ? ` and ${halted.length - 2} more` : '');
    row('agents', halted[0].id, halted.length === 1 ? "Agent can't run" : "Agents can't run", halted.length === 1 ? `${halted[0].projectName} · ${halted[0].health}` : names, halted.length, 'bolt.slash.fill');
  }
  // Work that got blocked or failed in the last day.
  const blocked = s.stuck.filter((w) => -w.at <= DAY).sort((a, b) => b.at - a.at);
  if (blocked.length > 0) {
    row('work', blocked[0].chat, 'Blocked or failed', blocked.length === 1 ? blocked[0].title : `Last 24h · newest: ${blocked[0].title}`, blocked.length, 'exclamationmark.octagon.fill');
  }
  // A notice of the team that holds its work: an active Status or Deadline that the person has not seen.
  const urgent = s.notices.filter((n) => n.active && !n.seen && (n.type === 'status' || n.type === 'deadline')).sort((a, b) => b.at - a.at);
  if (urgent.length > 0) {
    row('notices', s.teamId, urgent.length === 1 ? 'Team notice' : 'Team notices', `${kindName(urgent[0].type)} · ${urgent[0].text.trim()}`, urgent.length, 'megaphone.fill');
  }
  return items;
};

// The projects with the newest chat first: the picker lists them so, and the first is where a message goes by default.
const ordered = (s: State): Project[] => {
  const latest = (p: Project): number => Array.from(s.chats.values()).filter((c) => c.project === p.id).reduce((t, c) => Math.max(t, newest(c)), -Infinity);
  return [...s.projects].sort((a, b) => latest(b) - latest(a) || (a.name < b.name ? -1 : 1));
};

// MARK: The Mac's state

// The parts of the Mac's state (`GET /v1/state` with recent content) that are read here.
interface LvOption { id: string; label: string; explanation: string; answer: string }
interface LvBot { id: string; name: string; avatar: string; archived: boolean; pinned: boolean; project?: boolean; profile: string; status: string; unread_count: number; model_override: { model: string; reasoning_effort: string } | null }
interface LvMessage { id: string; bot_id: string; task_id: string | null; kind: string; role: string; text: string; created_at: string; decision_id: string | null; artifact_id: string | null }
interface LvTask { id: string; bot_id: string; status: string; title: string | null; created_at: string; updated_at: string }
interface LvDecision { id: string; bot_id: string; title: string; context: string | null; explanation: string | null; consequences: string | null; risk: string | null; uncertainty: string | null; status: string; created_at: string; revision: number; options: LvOption[]; selected_option_label: string | null }
interface LvArtifact { id: string; title: string; preview: string }
interface Snapshot { server_time: string; cursor: number; model: string | null; bots: LvBot[]; messages: LvMessage[]; tasks: LvTask[]; decisions: LvDecision[]; artifacts: LvArtifact[] }
// An answer of `GET /v1/updates` (X-Coconut-Updates 4, StateUpdates.swift): the changes after `after` up to `cursor`, or
// a whole state in `reset` when the Mac cannot list them.
interface Page {
  after: number;
  cursor: number;
  server_time: string;
  reset?: Snapshot;
  bots?: LvBot[];
  tasks?: LvTask[];
  messages?: LvMessage[];
  removed_messages?: string[];
  decisions?: LvDecision[];
  artifacts?: LvArtifact[];
  collection_deltas?: boolean;
  removed_decisions?: string[];
  removed_artifacts?: string[];
}

// The values of `updates` over those of `values`, by id: a known one in its place, a new one at the end.
const merge = <T extends { id: string }>(updates: T[], values: T[]): T[] => {
  const places = new Map(values.map((v, i) => [v.id, i]));
  const result = [...values];
  for (const value of updates) {
    const at = places.get(value.id);
    if (at === undefined) {
      places.set(value.id, result.length);
      result.push(value);
    } else {
      result[at] = value;
    }
  }
  return result;
};

// The state after a page of updates, as StateUpdates.applying makes it, or null when the page does not follow it.
const applying = (page: Page, snap: Snapshot): Snapshot | null => {
  if (page.after !== snap.cursor) return null;
  if (page.reset) return page.reset.cursor === page.cursor ? page.reset : null;
  if (page.cursor < page.after) return null;
  const next: Snapshot = { ...snap, cursor: page.cursor, server_time: page.server_time };
  if (page.bots) next.bots = page.bots;
  if (page.tasks) next.tasks = merge(page.tasks, snap.tasks);
  if (page.messages || page.removed_messages?.length) {
    const gone = new Set(page.removed_messages ?? []);
    next.messages = merge(page.messages ?? [], snap.messages).filter((m) => !gone.has(m.id))
      .sort((a, b) => (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  }
  if (page.collection_deltas) {
    const cards = new Set(page.removed_decisions ?? []);
    const files = new Set(page.removed_artifacts ?? []);
    next.decisions = merge(page.decisions ?? [], snap.decisions).filter((d) => !cards.has(d.id));
    next.artifacts = merge(page.artifacts ?? [], snap.artifacts).filter((a) => !files.has(a.id));
  } else {
    if (page.decisions) next.decisions = page.decisions;
    if (page.artifacts) next.artifacts = page.artifacts;
  }
  return next;
};

// The decisions and tasks of the Mac's state, for the writes: a card's options and revision, a chat's running task.
const decisions = new Map<string, LvDecision>();
const running = new Map<string, string>();

// A model id as a name: claude-opus-5-5 is Claude Opus 5.5.
const modelName = (id: string): string => id.split('-').reduce((name, part) => {
  if (/^\d+$/.test(part) && /\d$/.test(name)) return `${name}.${part}`;
  return `${name}${name === '' ? '' : ' '}${/^\d/.test(part) ? part : part.slice(0, 1).toUpperCase() + part.slice(1)}`;
}, '');

const fromSnapshot = (snap: Snapshot): State => {
  const server = parse(snap.server_time);
  const rel = (iso: string): number => parse(iso) - server;
  const bots = snap.bots.filter((b) => !b.archived);
  // A project is a bot with `project`; its chats are the bots of its Hermes profile.
  const projectOf = new Map(bots.filter((b) => b.project === true).map((b) => [b.profile, b]));
  const artifacts = new Map(snap.artifacts.map((a) => [a.id, a]));
  decisions.clear();
  for (const d of snap.decisions) decisions.set(d.id, d);
  const byBot = new Map<string, LvMessage[]>();
  for (const m of snap.messages) byBot.set(m.bot_id, [...(byBot.get(m.bot_id) ?? []), m]);
  const chats = new Map<string, ChatState>();
  const projects: Project[] = [];
  const waits: Wait[] = [];
  for (const bot of bots) {
    const project = projectOf.get(bot.profile) ?? bot;
    const letter = letterOf(bot.avatar || project.avatar, project.name);
    if (bot.project === true) projects.push({ id: bot.id, name: bot.name, letter, face: faceOf(letter) });
    const messages = (byBot.get(bot.id) ?? []).sort((a, b) => (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0)).map((m): Msg => {
      const at = rel(m.created_at);
      const decision = m.decision_id ? decisions.get(m.decision_id) : undefined;
      if (decision) {
        return message(m.id, 'decision', false, at, decision.context || decision.explanation || '', {
          title: decision.title,
          detail: [decision.consequences, decision.risk, decision.uncertainty].filter((part): part is string => !!part && part.trim() !== '').join('\n\n'),
          ref: decision.id,
          status: decision.status === 'pending' ? 'pending' : 'replied',
          receipt: decision.selected_option_label ?? '',
          options: decision.options.map((o) => ({ id: o.id, label: o.label, explanation: o.explanation })),
        });
      }
      const artifact = m.artifact_id ? artifacts.get(m.artifact_id) : undefined;
      if (artifact) return message(m.id, 'artifact', false, at, artifact.preview, { title: artifact.title });
      return message(m.id, m.kind === 'text' ? 'text' : 'activity', m.role === 'user', at, m.text);
    });
    // A chat that nobody wrote in shows nowhere (AgentChats.isBlank), but a project and a starred chat always show.
    if (messages.length === 0 && bot.unread_count === 0 && bot.status === 'idle' && !bot.pinned && bot.project !== true) continue;
    chats.set(bot.id, {
      id: bot.id, kind: 'agent', project: project.id, projectName: project.name, title: bot.name, letter, face: faceOf(letter),
      circle: false, messages, run: null, model: bot.model_override?.model ?? snap.model ?? '', effort: bot.model_override?.reasoning_effort ?? '',
      unread: bot.unread_count, health: '',
    });
  }
  for (const d of snap.decisions) {
    if (d.status === 'pending' && chats.has(d.bot_id)) waits.push({ ref: d.id, chat: d.bot_id, at: rel(d.created_at) });
  }
  running.clear();
  const stuck: Stuck[] = [];
  for (const task of snap.tasks) {
    const chat = chats.get(task.bot_id);
    if (!chat) continue;
    if (task.status === 'working') {
      running.set(chat.id, task.id);
      chat.run = { startedAt: rel(task.created_at), phase: PHASES.working, detail: '', model: modelName(chat.model), queued: 0 };
    }
    if (task.status === 'failed' && -rel(task.updated_at) <= DAY) stuck.push({ title: task.title || 'A task failed', chat: chat.id, at: rel(task.updated_at) });
  }
  return { zero: server, chats, projects, notices: [], waits, stuck, teamId: '', counter: 0 };
};

// MARK: The pairing

// What this phone keeps of its pairing, in the Keychain (secret.keep): the Mac's route and key, the phone's key, the
// device token of the Mac's Coconut and the relay's client token.
interface Pairing {
  route: string;
  macKey: string;
  macName: string;
  deviceId: string;
  token: string;
  client: string;
  privateKey: string;
  publicKey: string;
}

let pairing: Pairing | null | undefined;
const loadPairing = (store: Store): Pairing | null => {
  if (pairing === undefined) {
    try {
      const value = store.get(SAVED);
      pairing = value ? (JSON.parse(value) as Pairing) : null;
    } catch {
      pairing = null;
    }
  }
  return pairing;
};
const forget = (store: Store): void => {
  pairing = null;
  liveState = undefined;
  latest = null;
  pairs += 1;
  try {
    store.forget(SAVED);
  } catch {
    // Nothing kept.
  }
};

// The code that this phone shows, and the setup that the Mac sealed into its box.
let box: Box | null = null;
let setup: Setup | null = null;
let pairKeys: Keys | null = null;
// Pairings made since launch: the screens read the Keychain again after each.
let pairs = 0;

const boxRequest = (method: string, path = ''): Promise<Response> => fetch(`${RELAY}/v1/rendezvous/${box?.id ?? ''}${path}`, {
  method, headers: { Authorization: `Bearer ${box?.token ?? ''}` },
});

// MARK: The connection

// One socket of the relay, and the bytes it brought that no answer took yet.
interface Conn {
  key: string;
  socket: StreamSocket | null;
  open: boolean;
  ended: string;
  queue: Uint8Array[];
  waiters: Array<{ resolve: (bytes: Uint8Array) => void; reject: (error: Error) => void }>;
  n: number;
}

// The plan of a round: what its socket ticket authorizes, and the handshake that follows.
interface Plan {
  kind: 'pair' | 'token';
  route: string;
  macKey: Uint8Array;
  keys: Keys;
  psk: Uint8Array | null;
}

const tickets = new Map<string, { ticket: string; plan: Plan }>();
let conn: Conn | null = null;
let plan: Plan | null = null;
let tunnel: Tunnel | null = null;
let tunnelKey = '';
let version = 0;
let lastError = '';
// The Mac's state as it came, with every update since merged in: what the screens show, and where the next read of
// updates starts (its cursor). It outlives a round, so a new socket asks only for what changed meanwhile.
let latest: Snapshot | null = null;
// The round that read the Mac once: its next reads wait on the Mac for a change.
let polled = '';

// The error of the last try. A change of it moves the version, so the screens show the banner of it, or drop it.
const settle = (error: string): void => {
  if (error === lastError) return;
  lastError = error;
  version += 1;
};

// A turn of its own for the next step, so long work spreads over turns that each stay inside the budget.
const pause: Pause = () => {
  const scheduler = (globalThis as { scheduler?: { yield(): Promise<void> } }).scheduler;
  return scheduler ? scheduler.yield() : noPause();
};

const RELAY_ERRORS: Record<string, string> = {
  mac_offline: 'Your Mac does not answer. Check that it is awake.',
  host_reconnected: 'Your Mac connected again.',
  revoked: 'This phone is no longer paired with your Mac.',
  closed: 'Your Mac closed the connection.',
};

const end = (c: Conn, reason: string): void => {
  if (c.ended !== '') return;
  c.ended = reason;
  for (const w of c.waiters.splice(0)) w.reject(new Error(reason));
};

const linkOf = (c: Conn): Link => ({
  send(bytes: Uint8Array): void {
    if (!c.socket || c.ended !== '') throw new Error(c.ended || 'The connection to your Mac is not open.');
    c.socket.send(bytes);
  },
  async next(patience: number): Promise<Uint8Array> {
    const queued = c.queue.shift();
    if (queued) return queued;
    if (c.ended !== '') throw new Error(c.ended);
    const bytes = await new Promise<Uint8Array>((resolve, reject) => {
      const timer = setTimeout(() => {
        c.waiters = c.waiters.filter((w) => w.reject !== fail);
        reject(new Error('Your Mac did not answer in time.'));
      }, patience);
      const fail = (error: Error): void => {
        clearTimeout(timer);
        reject(error);
      };
      c.waiters.push({ resolve: (b) => { clearTimeout(timer); resolve(b); }, reject: fail });
    });
    // The mapper woke this answer inside another answer's turn: go on in a turn of its own.
    await pause();
    return bytes;
  },
  open: () => c.ended === '',
  pause,
});

const headers = (method: string, extra: Record<string, string> = {}): Record<string, string> => {
  const out: Record<string, string> = { Accept: 'application/json', ...extra };
  if (pairing) out.Authorization = `Bearer ${pairing.token}`;
  if (method !== 'GET') {
    out['Content-Type'] = 'application/json';
    out['Idempotency-Key'] = crypto.randomUUID();
  }
  return out;
};

const failureOf = (reply: Reply): string => {
  try {
    const body = JSON.parse(text(reply.body)) as { error?: string };
    if (body.error) return body.error;
  } catch {
    // Not JSON.
  }
  return `Your Mac answered ${reply.status}.`;
};

// A write to the Mac on the open connection. It returns the answer's JSON, or throws the Mac's reason.
const write = async (method: string, path: string, body: unknown): Promise<unknown> => {
  if (!tunnel || tunnel.session.broken || !conn || conn.ended !== '') throw new Error('Your Mac is not connected.');
  const reply = await tunnel.request(method, path, headers(method), utf8(JSON.stringify(body ?? {})));
  if (reply.status < 200 || reply.status >= 300) throw new Error(failureOf(reply));
  return reply.body.length > 0 ? JSON.parse(text(reply.body)) : {};
};

const gunzip = async (bytes: Uint8Array): Promise<Uint8Array> => {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(bytes);
      controller.close();
    },
  }).pipeThrough(new DecompressionStream('gzip') as unknown as ReadableWritablePair<Uint8Array, Uint8Array>);
  const reader = stream.getReader();
  const parts: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    parts.push(value);
    size += value.length;
  }
  const out = new Uint8Array(size);
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return out;
};

const status = (): Live => ({ version, pairs, error: lastError });

const none: Project = { id: '', name: '', letter: '', face: 0 };
const idle: Turn = { running: false, startedAt: 0, phase: '', detail: '', model: '', queued: 0 };
const emptyChat: Chat = {
  id: '', kind: 'agent', title: '', project: '', letter: '', face: 0, circle: false, messages: [], turn: idle, model: '', modelName: '', effort: '',
  efforts: [], models: [], toolbar: false, live: false,
};

const failed = (error: unknown): string => (error instanceof Error ? error.message : String(error));

const sources: Sources = {
  loadSaved: (_args, store) => {
    const saved = loadPairing(store);
    return { paired: saved !== null, macName: saved?.macName ?? '' };
  },

  // A new pairing code and its box on the relay, which keeps it for 10 minutes.
  pairCode: async () => {
    box = newBox();
    setup = null;
    try {
      const made = await boxRequest('PUT');
      if (made.status === 201 || made.status === 200) return { code: box.code, id: box.id, error: '' };
      return { code: '', id: '', error: made.status === 429 ? 'The relay is busy. Wait one minute, then ask for a new code.' : `The relay answered ${made.status}.` };
    } catch {
      return { code: '', id: '', error: 'The relay cannot be reached. Check the connection, then ask for a new code.' };
    }
  },

  // Waits up to 25 seconds for the setup that the Mac seals into the box, then opens it.
  pairWait: async ([id]) => {
    const state = (status: string, macName = '', error = '') => ({ status, id, macName, error });
    if (!box || box.id !== id) return state('error', '', 'Ask for a new code.');
    if (setup) return state('ready', setup.name);
    try {
      const got = await boxRequest('GET', '?wait=25');
      if (got.status === 204) return state('waiting');
      if (got.status === 404) return state('expired');
      if (got.status !== 200) return state('error', '', `The relay answered ${got.status}.`);
      const opened = await openBox(box, new Uint8Array(await got.arrayBuffer()), pause);
      await boxRequest('DELETE').catch(() => undefined);
      if (!opened) return state('error', '', 'The setup from the Mac did not open. Ask for a new code.');
      if (opened.relay !== RELAY) return state('error', '', 'This Mac uses another relay, which this app cannot reach.');
      setup = opened;
      return state('ready', opened.name);
    } catch {
      // A long poll that the network dropped: the next poll asks again.
      return state('waiting');
    }
  },

  // The socket ticket of a round: with the pairing ticket of the setup, or with the client token of the saved pairing.
  prepare: async ([key], store) => {
    try {
      let next: Plan;
      let authorization: string;
      if (key.startsWith('pair:')) {
        if (!setup) throw new Error('Ask for a new code.');
        pairKeys = pairKeys ?? (await newKeys(pause));
        next = { kind: 'pair', route: setup.route, macKey: setup.macKey, keys: pairKeys, psk: pairingPsk(setup.secret) };
        authorization = `Ticket ${relayTicket(setup.secret)}`;
      } else {
        const saved = loadPairing(store);
        const macKey = saved ? fromBase64Url(saved.macKey) : null;
        const privateKey = saved ? fromBase64Url(saved.privateKey) : null;
        const publicKey = saved ? fromBase64Url(saved.publicKey) : null;
        if (!saved || !macKey || !privateKey || !publicKey) throw new Error('Pair this phone with your Mac.');
        next = { kind: 'token', route: saved.route, macKey, keys: { privateKey, publicKey }, psk: null };
        authorization = `Bearer ${saved.client}`;
      }
      const minted = await fetch(`${RELAY}/v1/tunnels/${next.route}/client-ticket`, { method: 'POST', headers: { Authorization: authorization } });
      if (minted.status === 401 || minted.status === 404) {
        if (next.kind === 'token') forget(store);
        throw new Error(next.kind === 'token' ? 'This phone is no longer paired with your Mac.' : 'This pairing code no longer works. Ask for a new one.');
      }
      if (!minted.ok) throw new Error(minted.status === 429 ? 'The relay is busy. Coconut tries again soon.' : `The relay answered ${minted.status}.`);
      const { ticket } = (await minted.json()) as { ticket: string };
      tickets.clear();
      tickets.set(key, { ticket, plan: next });
      return key;
    } catch (error) {
      lastError = failed(error);
      return '';
    }
  },

  // The socket of a round. It opens synchronously, with the ticket that `prepare` kept, and queues what comes.
  socket: ([key]) => {
    const entry = tickets.get(key);
    tickets.delete(key);
    if (!entry) throw new Error('Connect again for a new ticket.');
    if (conn) end(conn, 'A new connection replaced this one.');
    const c: Conn = { key, socket: null, open: false, ended: '', queue: [], waiters: [], n: 0 };
    conn = c;
    plan = entry.plan;
    tunnel = null;
    tunnelKey = '';
    return fetch(`wss://coconut-push.gumroad.workers.dev/v1/tunnels/${entry.plan.route}/client?ticket=${entry.ticket}`, {
      exactStream(event: StreamEvent, socket?: StreamSocket): Sock {
        if (socket) c.socket = socket;
        if (event.type === 'open') {
          c.open = true;
        } else if (event.type === 'message') {
          if (typeof event.data === 'string') {
            // The relay's own words: an error before it closes the socket, or a pong.
            try {
              const said = JSON.parse(event.data) as { type?: string; code?: string };
              if (said.type === 'error') end(c, RELAY_ERRORS[said.code ?? ''] ?? 'The relay closed the connection.');
            } catch {
              // A pong.
            }
          } else {
            const bytes = new Uint8Array(event.data);
            const waiter = c.waiters.shift();
            if (waiter) waiter.resolve(bytes);
            else c.queue.push(bytes);
          }
        } else if (event.type === 'close' || event.type === 'error') {
          end(c, c.open ? 'The connection to your Mac ended.' : 'The relay refused the connection.');
        }
        c.n += 1;
        return { key, status: c.ended !== '' ? 'closed' : c.open ? 'open' : 'connecting', n: c.n };
      },
    });
  },

  // Asked for each event of the socket, so an answer that waits for the socket goes on (see the top of the module).
  deliver: ([n]) => n,

  // The handshake of a round, then its work: a pairing pairs and keeps the keys; a session with the Mac reads its state.
  sync: async ([key, open], store) => {
    if (!open || !conn || conn.key !== key || !plan) return status();
    try {
      if (!tunnel || tunnelKey !== key || tunnel.session.broken) {
        tunnel = null;
        tunnel = await connect(linkOf(conn), plan.keys, plan.macKey, plan.psk);
        tunnelKey = key;
      }
      if (plan.kind === 'pair') {
        if (!setup) throw new Error('Ask for a new code.');
        const reply = await tunnel.request('POST', '/v1/pair', { Accept: 'application/json', 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID() },
          utf8(JSON.stringify({ code: setup.code, device_name: 'Coconut on Exact' })));
        if (reply.status !== 200) throw new Error(reply.status === 401 ? 'This pairing code no longer works. Ask for a new one.' : failureOf(reply));
        const made = JSON.parse(text(reply.body)) as { token: string; device_id: string };
        const kept: Pairing = {
          route: plan.route, macKey: toBase64Url(plan.macKey), macName: setup.name, deviceId: made.device_id, token: made.token,
          client: clientToken(tunnel.session.handshakeHash), privateKey: toBase64Url(plan.keys.privateKey), publicKey: toBase64Url(plan.keys.publicKey),
        };
        store.set(SAVED, JSON.stringify(kept));
        pairing = kept;
        setup = null;
        box = null;
        pairKeys = null;
        pairs += 1;
        lastError = '';
        return status();
      }
      // The first read of the Mac is its whole state. Each read after it asks for what changed after the cursor of the
      // last one (`updates`) and merges it, also on a new socket. In a round it waits up to 20 seconds on the Mac for a
      // change, so the next read starts as soon as it ends; the first read of a round waits for nothing, so a round
      // that follows a failure clears it at once.
      const decoded = async (reply: Reply): Promise<unknown> => {
        if (reply.status === 401) {
          forget(store);
          throw new Error('This phone is no longer paired with your Mac.');
        }
        if (reply.status !== 200) throw new Error(failureOf(reply));
        const encoding = Object.entries(reply.headers).find(([name]) => name.toLowerCase() === 'content-encoding')?.[1] ?? '';
        const body = encoding.toLowerCase() === 'gzip' ? await gunzip(reply.body) : reply.body;
        await pause();
        const value = JSON.parse(text(body)) as unknown;
        await pause();
        return value;
      };
      const read = { 'Accept-Encoding': 'gzip', 'X-Coconut-State': 'recent', 'X-Coconut-Work': 'recent', 'X-Coconut-Updates': '4' };
      const base = latest;
      let next: Snapshot | null;
      if (!base) {
        next = (await decoded(await tunnel.request('GET', '/v1/state', headers('GET', read)))) as Snapshot;
      } else {
        const wait = polled === key ? 20 : 0;
        const page = (await decoded(await tunnel.request('GET', `/v1/updates?after=${base.cursor}&wait=${wait}`, headers('GET', read), undefined, LONG_POLL))) as Page;
        // A page that does not follow the state drops it, and the next read takes the whole state again.
        next = latest === base ? applying(page, base) : null;
        if (next && !page.reset && page.cursor === page.after) {
          latest = next;
          polled = key;
          settle('');
          return status();
        }
      }
      latest = next;
      polled = key;
      if (next) {
        liveState = fromSnapshot(next);
        version += 1;
      }
      settle('');
      return status();
    } catch (error) {
      settle(failed(error));
      return status();
    }
  },

  loadHome: ([zero, chosen, live]): Home => {
    useLive = live;
    const s = ensure();
    const list = ordered(s);
    const at = zeroOf(s, zero);
    const banner = !live ? '' : lastError !== '' ? lastError : liveState ? '' : `Connecting to ${pairing?.macName ?? 'your Mac'}…`;
    return {
      target: list.find((p) => p.id === chosen) ?? list[0] ?? none,
      projects: list,
      attention: attention(s),
      chats: Array.from(s.chats.values()).map((c) => rowOf(c, at)).sort((a, b) => b.at - a.at || (a.id < b.id ? -1 : 1)),
      live,
      banner,
    };
  },

  loadChat: ([zero, id, live]): Chat => {
    useLive = live;
    const s = ensure();
    const chat = s.chats.get(id);
    if (!chat) return emptyChat;
    const at = zeroOf(s, zero);
    const agent = chat.kind === 'agent';
    const model = modelOf(chat.model);
    const lines: Message[] = chat.messages.map((m) => ({ ...m, at: at + m.at }));
    // The working line is the last row of a chat whose agent works.
    if (chat.run) lines.push(message('working', 'status', false, 0, ''));
    const turn: Turn = chat.run
      ? { running: true, startedAt: at + chat.run.startedAt, phase: chat.run.phase, detail: chat.run.detail, model: chat.run.model, queued: chat.run.queued }
      : idle;
    // The Mac's chats choose no model or effort here yet: the Swift app does.
    return {
      id: chat.id, kind: chat.kind, title: chat.title, project: chat.projectName, letter: chat.letter, face: chat.face, circle: chat.circle,
      messages: lines, turn, model: chat.model, modelName: !agent ? '' : live ? modelName(chat.model) : model.name, effort: chat.effort,
      efforts: agent && !live ? model.reasoning_efforts : [], models: agent && !live ? fx.model_catalog.models.map((m) => ({ id: m.id, name: m.name })) : [],
      toolbar: agent && !live, live,
    };
  },

  // Opening a chat reads it: its unread count goes, and the notices of a team are seen.
  markRead: async ([id]) => {
    const s = ensure();
    const chat = s.chats.get(id);
    if (!chat) return { ok: false };
    chat.unread = 0;
    if (chat.kind === 'team') for (const n of s.notices) n.seen = true;
    if (!useLive) return { ok: true };
    try {
      await write('POST', `/v1/bots/${id}/read`, {});
      return { ok: true };
    } catch {
      return { ok: false };
    }
  },

  // A message of Home starts a chat with the project, titled by its words, and its agent starts a turn.
  startChat: async ([projectId, words, at]) => {
    const s = ensure();
    const project = s.projects.find((p) => p.id === projectId) ?? s.projects[0];
    if (!project) return { id: '' };
    s.counter += 1;
    let id = `chat-${s.counter}`;
    if (useLive) {
      try {
        const made = (await write('POST', `/v1/bots/${project.id}/chats`, { todo: false })) as { id?: string };
        if (!made.id) return { id: '' };
        id = made.id;
        await write('POST', `/v1/bots/${id}/messages`, { text: words, mode: 'auto', attachment_ids: [], recipient_bot_ids: [] });
      } catch (error) {
        lastError = failed(error);
        return { id: '' };
      }
    }
    const model = useLive ? { model: '', reasoning_effort: '' } : fx.model_catalog.default;
    const firstAt = useLive && s.zero !== null ? at - s.zero : at;
    s.chats.set(id, {
      id, kind: 'agent', project: project.id, projectName: project.name, title: words.length > 48 ? `${words.slice(0, 47).trimEnd()}…` : words,
      letter: project.letter, face: project.face, circle: false, messages: [message(`${id}-1`, 'text', true, firstAt, words)],
      run: { startedAt: firstAt, phase: PHASES.thinking, detail: '', model: useLive ? '' : modelOf(model.model).name, queued: 0 },
      model: model.model, effort: model.reasoning_effort, unread: 0, health: '',
    });
    return { id };
  },

  // A message of a chat. In the demo, names of files that were attached close it as a line of code; the Mac's chats
  // take words only here. The agent of the chat starts a turn, or, when one runs, the message waits for the next.
  sendMessage: async ([chatId, words, files, at]) => {
    const s = ensure();
    const chat = s.chats.get(chatId);
    if (!chat) return { ok: false };
    if (useLive && words === '') return { ok: false };
    s.counter += 1;
    const body = useLive ? words : [words, files === '' ? '' : '`' + files + '`'].filter((part) => part !== '').join('\n\n');
    const sentAt = useLive && s.zero !== null ? at - s.zero : at;
    chat.messages = [...chat.messages, message(`${chat.id}-m${s.counter}`, 'text', true, sentAt, body)];
    if (useLive) {
      try {
        await write('POST', `/v1/bots/${chatId}/messages`, { text: words, mode: 'auto', attachment_ids: [], recipient_bot_ids: [] });
      } catch (error) {
        chat.messages = [...chat.messages, message(`${chat.id}-f${s.counter}`, 'activity', false, sentAt, `Not sent: ${failed(error)}`)];
        return { ok: false };
      }
    }
    if (chat.kind === 'agent') {
      if (chat.run) chat.run = { ...chat.run, queued: chat.run.queued + 1 };
      else chat.run = { startedAt: sentAt, phase: PHASES.thinking, detail: '', model: useLive ? modelName(chat.model) : modelOf(chat.model).name, queued: 0 };
    }
    return { ok: true };
  },

  stopTurn: async ([chatId]) => {
    const chat = ensure().chats.get(chatId);
    if (!chat) return { ok: false };
    if (useLive) {
      const task = running.get(chatId);
      if (!task) return { ok: false };
      try {
        await write('POST', `/v1/tasks/${task}/cancel`, {});
      } catch {
        return { ok: false };
      }
    }
    chat.run = null;
    return { ok: true };
  },

  // The answer to a card: it keeps the label of the option, and the chat no longer waits on the person.
  answerCard: async ([chatId, ref, optionId]) => {
    const s = ensure();
    const chat = s.chats.get(chatId);
    if (!chat) return { ok: false };
    if (useLive) {
      const decision = decisions.get(ref);
      const option = decision?.options.find((o) => o.id === optionId);
      if (!decision || !option) return { ok: false };
      try {
        await write('POST', `/v1/decisions/${ref}/answer`, { choice: option.answer, revision: decision.revision, option_id: option.id });
      } catch (error) {
        lastError = failed(error);
        return { ok: false };
      }
    }
    chat.messages = chat.messages.map((m) => {
      if (m.kind !== 'decision' || m.ref !== ref) return m;
      const option = m.options.find((o) => o.id === optionId);
      return { ...m, status: 'replied', receipt: option ? option.label : '' };
    });
    s.waits = s.waits.filter((w) => w.ref !== ref);
    chat.unread = 0;
    return { ok: true };
  },

  // A model that lacks the effort of the chat gives it its own default. Only the demo changes models here.
  setModel: ([chatId, modelId]) => {
    const chat = ensure().chats.get(chatId);
    if (!chat || useLive) return { ok: false };
    const model = modelOf(modelId);
    chat.model = model.id;
    if (!model.reasoning_efforts.includes(chat.effort)) chat.effort = model.default_reasoning_effort;
    return { ok: true };
  },

  setEffort: ([chatId, effort]) => {
    const chat = ensure().chats.get(chatId);
    if (!chat || useLive) return { ok: false };
    chat.effort = effort;
    return { ok: true };
  },

  // AgentLetter.free: a new project takes the initial of its name when no agent holds it, else the first free letter.
  // The Mac's projects are fixed, so only the demo makes one.
  createProject: ([name]) => {
    const s = ensure();
    if (useLive) return { id: '' };
    const held = new Set(s.projects.map((p) => p.letter));
    const initial = name.trim().slice(0, 1).toUpperCase();
    const letter = LETTERS.includes(initial) && !held.has(initial) ? initial : (LETTERS.split('').find((l) => !held.has(l)) ?? 'A');
    s.counter += 1;
    const id = `project-${s.counter}`;
    s.projects.push({ id, name: name.trim(), letter, face: faceOf(letter) });
    return { id };
  },
};
export const answer: Answer = (source, args, store, storage, native) => sources[source](args, store, storage, native);
