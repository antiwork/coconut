// The data module of Coconut on Exact. It answers the screens from the preview fixture of the Swift app
// (ios/Coconut/Fixtures/PreviewState.json, copied as preview.json) and the Gumroad team of its team preview
// (team-preview.json). The state lives in memory: a message, a stop, an answered card and a new project change it until
// the app launches again. No Mac and no relay sit behind it yet (docs/exact.md).
//
// Time. A data module reads no clock, so every time in the state is in milliseconds from the launch: the fixture's
// history is negative (the fixture moves its server_time to the launch, as PreviewStateFixture.decode does), and a
// message written now is performanceNow(). A source adds the launch (`zero`, the contract's `time.epochAtZero`) to each
// time it answers, so the screens hold times since 1970 and the state does not depend on when it was built.
import type { Answer, Result, Sources } from './app.contract.d.ts';
import preview from './preview.json';
import team from './team-preview.json';

export const grants = '';

type Home = Result<'loadHome'>;
type Chat = Result<'loadChat'>;
type Project = Home['projects'][number];
type Row = Home['chats'][number];
type Attention = Home['attention'][number];
type Message = Chat['messages'][number];
type Turn = Chat['turn'];

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
  return { chats, projects, notices, waits, stuck, teamId, counter: 0 };
};

// Built once, when the first source asks, and not at the top of the module: the top of a data module runs before the
// first frame.
let state: State | undefined;
const ensure = (): State => {
  if (!state) state = build();
  return state;
};

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

const none: Project = { id: '', name: '', letter: '', face: 0 };
const idle: Turn = { running: false, startedAt: 0, phase: '', detail: '', model: '', queued: 0 };
const emptyChat: Chat = {
  id: '', kind: 'agent', title: '', project: '', letter: '', face: 0, circle: false, messages: [], turn: idle, model: '', modelName: '', effort: '',
  efforts: [], models: [], toolbar: false,
};

const sources: Sources = {
  loadHome: ([zero, chosen]): Home => {
    const s = ensure();
    const list = ordered(s);
    return {
      target: list.find((p) => p.id === chosen) ?? list[0] ?? none,
      projects: list,
      attention: attention(s),
      chats: Array.from(s.chats.values()).map((c) => rowOf(c, zero)).sort((a, b) => b.at - a.at || (a.id < b.id ? -1 : 1)),
    };
  },

  loadChat: ([zero, id]): Chat => {
    const chat = ensure().chats.get(id);
    if (!chat) return emptyChat;
    const agent = chat.kind === 'agent';
    const model = modelOf(chat.model);
    const lines: Message[] = chat.messages.map((m) => ({ ...m, at: zero + m.at }));
    // The working line is the last row of a chat whose agent works.
    if (chat.run) lines.push(message('working', 'status', false, 0, ''));
    const turn: Turn = chat.run
      ? { running: true, startedAt: zero + chat.run.startedAt, phase: chat.run.phase, detail: chat.run.detail, model: chat.run.model, queued: chat.run.queued }
      : idle;
    return {
      id: chat.id, kind: chat.kind, title: chat.title, project: chat.projectName, letter: chat.letter, face: chat.face, circle: chat.circle,
      messages: lines, turn, model: chat.model, modelName: agent ? model.name : '', effort: chat.effort,
      efforts: agent ? model.reasoning_efforts : [], models: agent ? fx.model_catalog.models.map((m) => ({ id: m.id, name: m.name })) : [], toolbar: agent,
    };
  },

  // Opening a chat reads it: its unread count goes, and the notices of a team are seen.
  markRead: ([id]) => {
    const s = ensure();
    const chat = s.chats.get(id);
    if (!chat) return { ok: false };
    chat.unread = 0;
    if (chat.kind === 'team') for (const n of s.notices) n.seen = true;
    return { ok: true };
  },

  // A message of Home starts a chat with the project, titled by its words, and its agent starts a turn.
  startChat: ([projectId, text, at]) => {
    const s = ensure();
    const project = s.projects.find((p) => p.id === projectId) ?? s.projects[0];
    s.counter += 1;
    const id = `chat-${s.counter}`;
    const model = fx.model_catalog.default;
    s.chats.set(id, {
      id, kind: 'agent', project: project.id, projectName: project.name, title: text.length > 48 ? `${text.slice(0, 47).trimEnd()}…` : text,
      letter: project.letter, face: project.face, circle: false, messages: [message(`${id}-1`, 'text', true, at, text)],
      run: { startedAt: at, phase: PHASES.thinking, detail: '', model: modelOf(model.model).name, queued: 0 },
      model: model.model, effort: model.reasoning_effort, unread: 0, health: '',
    });
    return { id };
  },

  // A message of a chat. Names of files that were attached close it as a line of code. The agent of the chat starts a turn,
  // or, when one runs, the message waits for the next.
  sendMessage: ([chatId, text, files, at]) => {
    const s = ensure();
    const chat = s.chats.get(chatId);
    if (!chat) return { ok: false };
    s.counter += 1;
    const body = [text, files === '' ? '' : '`' + files + '`'].filter((part) => part !== '').join('\n\n');
    chat.messages = [...chat.messages, message(`${chat.id}-m${s.counter}`, 'text', true, at, body)];
    if (chat.kind === 'agent') {
      if (chat.run) chat.run = { ...chat.run, queued: chat.run.queued + 1 };
      else chat.run = { startedAt: at, phase: PHASES.thinking, detail: '', model: modelOf(chat.model).name, queued: 0 };
    }
    return { ok: true };
  },

  stopTurn: ([chatId]) => {
    const chat = ensure().chats.get(chatId);
    if (!chat) return { ok: false };
    chat.run = null;
    return { ok: true };
  },

  // The answer to a card: it keeps the label of the option, and the chat no longer waits on the person.
  answerCard: ([chatId, ref, optionId]) => {
    const s = ensure();
    const chat = s.chats.get(chatId);
    if (!chat) return { ok: false };
    chat.messages = chat.messages.map((m) => {
      if (m.kind !== 'decision' || m.ref !== ref) return m;
      const option = m.options.find((o) => o.id === optionId);
      return { ...m, status: 'replied', receipt: option ? option.label : '' };
    });
    s.waits = s.waits.filter((w) => w.ref !== ref);
    chat.unread = 0;
    return { ok: true };
  },

  // A model that lacks the effort of the chat gives it its own default.
  setModel: ([chatId, modelId]) => {
    const chat = ensure().chats.get(chatId);
    if (!chat) return { ok: false };
    const model = modelOf(modelId);
    chat.model = model.id;
    if (!model.reasoning_efforts.includes(chat.effort)) chat.effort = model.default_reasoning_effort;
    return { ok: true };
  },

  setEffort: ([chatId, effort]) => {
    const chat = ensure().chats.get(chatId);
    if (!chat) return { ok: false };
    chat.effort = effort;
    return { ok: true };
  },

  // AgentLetter.free: a new project takes the initial of its name when no agent holds it, else the first free letter.
  createProject: ([name]) => {
    const s = ensure();
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
