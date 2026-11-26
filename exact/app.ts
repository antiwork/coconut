// The data module of Coconut on Exact. Paired with a Mac, it answers the screens from the state of the Mac's Coconut,
// read through the relay with the tunnel of the Swift app (tunnel.ts, docs/relay-transport.md), and sends messages
// with their files, answers, stops and reads back to it. Before a pairing it shows a pairing code; "Look around with
// demo data" answers from the preview fixture of the Swift app (ios/Coconut/Fixtures/PreviewState.json, copied as
// preview.json) and the Gumroad team of its team preview (team-preview.json), in memory until the app launches again.
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
import type { Answer, Database, Result, Sources, Storage, StreamEvent, StreamSocket, Store } from './app.contract.d.ts';
import { noPause, type Pause } from './crypto.ts';
import {
  MAX_PAYLOAD, clientToken, connect, fromBase64Url, newBox, newKeys, openBox, pairingPsk, relayTicket, text, toBase64Url, utf8,
  type Box, type Keys, type Link, type Reply, type Setup, type Tunnel,
} from './tunnel.ts';
import preview from './preview.json';
import team from './team-preview.json';

// The relay of Coconut. A pairing setup that names another relay is refused, because the grants name this one.
const RELAY = 'https://coconut-push.gumroad.workers.dev';
const SAVED = 'coconut-pairing';
// The picked files of a message (app:/tmp/picked), the smaller copies of photos that go to the Mac (UPLOADS), and the
// last state of the Mac (CACHE).
const UPLOADS = 'app:/tmp/upload';
const CACHE = 'app:/data/coconut.db';
export const grants = [
  `net.fetch ${RELAY}`, 'net.websocket wss://coconut-push.gumroad.workers.dev', `secret.keep ${SAVED}`,
  'fs.read app:/tmp/picked', `fs.read ${UPLOADS}`, `fs.write ${UPLOADS}`, `sqlite.open ${CACHE}`,
].join('\n');
// How long a read of updates waits for each frame of its answer: the Mac holds it up to 25 seconds (the Swift app
// gives a long poll 45).
const LONG_POLL = 45000;

type Home = Result<'loadHome'>;
type Chat = Result<'loadChat'>;
type Project = Home['projects'][number];
type Row = Home['chats'][number];
type Attention = Home['attention'][number];
type Message = Chat['messages'][number];
type Card = Message['card'][number];
type Photo = Message['photos'][number];
type Draft = Card['drafts'][number];
type Turn = Chat['turn'];
type Live = Result<'sync'>;
type Sock = Result<'socket'>;

// The parts of the fixture that are read here.
interface FxOption { id: string; label: string; explanation: string; answer?: string; risk?: string | null; risk_level?: string | null }
interface FxDecision {
  id: string; bot_id: string; task_id: string; title: string; explanation: string; consequences: string; proposal_preview: string;
  status: string; created_at: string; options: FxOption[]; action_type?: string; resource?: string; context?: string; risk?: string;
  risk_level?: string; uncertainty?: string; recommended_option_id?: string; sources?: Source[]; draft?: RawDraft; previews?: RawPreview[];
}
interface FxArtifact { id: string; task_id: string; title: string; preview: string }
interface FxMessage { id: string; bot_id: string; task_id: string | null; kind: string; role: string; text: string; created_at: string }
interface FxTask { id: string; bot_id: string; status: string; updated_at: string; phase?: string; phase_detail?: string; phase_started_at?: string }
interface FxBot { id: string; name: string; avatar: string; archived: boolean; unread_count: number; chat_of: string | null; chat_title: string | null }
// What a chat runs with: a model of a provider and its reasoning effort (null: the model's own default).
interface Selection { model: string; provider: string; reasoning_effort: string | null }
// A model of a catalog (`model_catalog` of GET /v1/state/settings, and of the fixture), with the efforts it takes.
interface CatalogModel { id: string; name: string; provider: string; reasoning_efforts: string[]; default_reasoning_effort: string | null }
// The models and the default of every chat: one Hermes profile runs the chats of a project, so `/model` and `/effort`
// change the default, as in the Swift app (ModelChoice).
interface Catalog { default: Selection; models: CatalogModel[] }
interface Fixture {
  server_time: string;
  bots: FxBot[];
  messages: FxMessage[];
  tasks: FxTask[];
  decisions: FxDecision[];
  artifacts: FxArtifact[];
  model_catalog: Catalog;
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
// AgentChats.untitled: a further chat of an agent that has no title yet.
const UNTITLED = 'New agent';
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

// A photo as the data module keeps it: a file of the Mac by its artifact id (`file`), or an address that the view loads
// itself (`url`: a web address, or a picture of the app's assets in the demo).
interface PhotoRef { key: string; file: string; url: string; caption: string; width: number; height: number }
// The parts of a card that the Mac's state and the fixture both give.
interface Source { kind: string; title: string; url?: string | null }
interface RawDraft { channel: string; recipient: string; body: string; sender?: string | null; subject?: string | null; reply_to?: string | null }
interface RawPreview { artifact_id: string; caption: string; width: number; height: number }
interface Opt { id: string; label: string; explanation: string; recommended: boolean; risk: string; reason: string; answer: string }
// A card as a chat shows it, before the answer that the phone holds for it (`answers`).
interface CardData {
  ref: string; title: string; sources: string[]; diff: string; context: string[]; risk: string; reason: string; stale: boolean;
  previews: PhotoRef[]; draft: Draft | null; options: Opt[]; status: string; refreshing: boolean; decidedBy: string; selected: string;
  outcome: string; earlier: string; revised: boolean; revision: number; draftHash: string; previewsHash: string;
}
// A row of a chat. `from` joins the photos that one agent sent one after another into one row (Message.photoSender).
interface Msg {
  id: string; kind: string; mine: boolean; author: string; at: number; title: string; body: string; detail: string; failed: boolean;
  sender: string; quote: string; quoteAuthor: string; photos: PhotoRef[]; card: CardData | null; from: string;
}
// A turn that runs: `startedAt` is the start of its newest step (the Mac's `phase_started_at`), which the working line
// and the row of the chat time.
interface Run { startedAt: number; phase: string; detail: string; queued: number }
interface ChatState {
  id: string;
  kind: 'agent' | 'team' | 'direct';
  project: string;
  projectName: string;
  title: string;
  // The title of the chat itself (Bot.chatLabel), which its header shows; the list names the project before it.
  label: string;
  letter: string;
  face: number;
  circle: boolean;
  messages: Msg[];
  run: Run | null;
  unread: number;
  // The newest message that the person read (`last_read_message_id`): the New line goes under it.
  readMark: string;
  // BotHealth.label of an agent that cannot run, or an empty string.
  health: string;
  // An archived chat of the Mac. It shows in Idle with the others, but gives its project no place in the picker.
  archived?: boolean;
  muted?: boolean;
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
  id, kind, mine, author: '', at, title: '', body, detail: '', failed: false, sender: '', quote: '', quoteAuthor: '', photos: [],
  card: null, from: '', ...extra,
});

// MARK: Cards

// A decision of the Mac's state or of the fixture, as far as a card reads it.
interface RawDecision {
  id: string; title: string; status: string; options: FxOption[]; revision?: number; explanation?: string | null;
  context?: string | null; uncertainty?: string | null; risk?: string | null; risk_level?: string | null; assessment_stale?: boolean | null;
  action_type?: string; resource?: string | null; recommended_option_id?: string | null; sources?: Source[] | null;
  merge_pr_url?: string | null; merge_pr_diff?: { additions: number; deletions: number; changed_files: number } | null;
  draft?: RawDraft | null; draft_hash?: string | null; previews?: RawPreview[] | null; previews_hash?: string | null;
  selected_option_id?: string | null; selected_option_label?: string | null; outcome?: string | null; decided_by?: string | null;
  previous_decision_id?: string | null; replaced_by_decision_id?: string | null; source_refreshing?: boolean | null;
}

// DecisionRisk.label.
const riskLabel = (level: string | null | undefined): string => (level === 'low' ? 'Low risk' : level === 'medium' ? 'Medium risk'
  : level === 'high' ? 'High risk' : level === 'not_assessed' ? 'Risk not assessed' : 'Risk');
const SOURCE_LABELS: Record<string, string> = { email: 'Email', issue: 'Issue', pull_request: 'PR' };
const thousands = (n: number): string => String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
const said = (text: string | null | undefined): string => (text ?? '').trim();

// Decision.cardSources: each source once, with a title, and the pull request of a merge card when no source names it.
// A line names its kind and title (DecisionSources); the app opens no link yet.
const sourceLines = (d: RawDecision): string[] => {
  const sources = [...(d.sources ?? [])];
  const pr = /^https:\/\/github\.com\/[^/]+\/([^/]+)\/pull\/(\d+)$/.exec(d.merge_pr_url ?? '');
  if (pr && !sources.some((s) => s.url === d.merge_pr_url)) sources.push({ kind: 'pull_request', title: `${pr[1]} #${pr[2]}`, url: d.merge_pr_url });
  const lines: string[] = [];
  for (const s of sources) {
    const line = `${SOURCE_LABELS[s.kind] ?? 'Source'} · ${s.title.trim()}`;
    if (s.title.trim() !== '' && !lines.includes(line)) lines.push(line);
  }
  return lines;
};

// Decision.cardContext in paragraphs: the context, else the explanation unless it repeats the title, then an unknown
// that the context does not name, and the resource of a native approval.
const contextOf = (d: RawDecision): string[] => {
  const recap = said(d.context);
  const text = recap === '' ? said(d.explanation) : recap;
  let summary = text === d.title ? '' : text;
  const unknown = said(d.uncertainty);
  if (d.assessment_stale !== true && unknown !== '' && !summary.toLowerCase().includes(unknown.toLowerCase())) {
    summary = [summary, unknown].filter((part) => part !== '').join('\n\n');
  }
  const resource = said(d.resource);
  if (d.action_type === 'native_approval' && resource !== '' && !summary.includes(resource)) {
    summary = [summary, resource].filter((part) => part !== '').join('\n');
  }
  return summary === '' ? [] : summary.split('\n\n').map((part) => part.trim()).filter((part) => part !== '');
};

// Decision.cardOptions: the recommended option first. A file card without options takes Save file and Do not save, and
// a draft card takes Send and Discard (MessageDraftActions), whatever options it names.
const optionsOf = (d: RawDecision): Opt[] => {
  const stale = d.assessment_stale === true;
  if (d.draft) {
    return [
      { id: 'send', label: 'Send', explanation: '', recommended: false, risk: '', reason: '', answer: 'approve' },
      { id: 'discard', label: 'Discard', explanation: '', recommended: false, risk: '', reason: '', answer: 'reject' },
    ];
  }
  const raw: FxOption[] = d.options.length === 0 && d.action_type === 'create_file'
    ? [{ id: 'save-file', label: 'Save file', explanation: `Create ${said(d.resource)} on the Mac.`, answer: 'approve' },
      { id: 'do-not-save', label: 'Do not save', explanation: 'Create no file.', answer: 'reject' }]
    : d.options;
  const options = raw.map((o): Opt => ({
    id: o.id, label: o.label, explanation: o.explanation ?? '', recommended: o.id === d.recommended_option_id,
    risk: !stale && said(o.risk) !== '' ? riskLabel(o.risk_level) : '', reason: stale ? '' : said(o.risk), answer: o.answer ?? 'approve',
  }));
  const first = options.findIndex((o) => o.recommended);
  return first > 0 ? [options[first], ...options.slice(0, first), ...options.slice(first + 1)] : options;
};

// A card from a decision. `all` names the cards of the state, for the earlier choice of a follow-up.
const cardOf = (d: RawDecision, all: Map<string, RawDecision>): CardData => {
  const stale = d.assessment_stale === true;
  const diff = d.merge_pr_url && d.merge_pr_diff
    ? `+${thousands(d.merge_pr_diff.additions)} −${thousands(d.merge_pr_diff.deletions)} · ${thousands(d.merge_pr_diff.changed_files)} ${d.merge_pr_diff.changed_files === 1 ? 'file' : 'files'}`
    : '';
  const previous = d.previous_decision_id ? all.get(d.previous_decision_id) : undefined;
  const draft = d.draft ? {
    via: d.draft.channel ?? '', from: said(d.draft.sender), to: d.draft.recipient ?? '', replyTo: said(d.draft.reply_to),
    subject: said(d.draft.subject), body: d.draft.body ?? '',
  } : null;
  return {
    ref: d.id, title: d.title, sources: sourceLines(d), diff, context: contextOf(d), stale,
    risk: !stale && said(d.risk) !== '' ? riskLabel(d.risk_level) : '', reason: stale ? '' : said(d.risk),
    previews: (d.previews ?? []).map((p) => ({ key: `file:${p.artifact_id}`, file: p.artifact_id, url: '', caption: p.caption ?? '', width: p.width ?? 0, height: p.height ?? 0 })),
    draft, options: optionsOf(d), status: d.status, refreshing: d.status === 'refreshing' || (d.status === 'pending' && d.source_refreshing === true),
    decidedBy: d.decided_by ?? '', selected: d.selected_option_label ?? '', outcome: said(d.outcome),
    earlier: previous && previous.status !== 'replied' && previous.selected_option_label ? `Earlier choice: ${previous.selected_option_label}` : '',
    revised: !!d.replaced_by_decision_id, revision: d.revision ?? 0, draftHash: d.draft_hash ?? '', previewsHash: d.previews_hash ?? '',
  };
};

// The answers that the phone holds for cards, by card (OutgoingAnswer): the option, its label, and whether it still
// waits for its Undo or goes to the Mac. `failures` say why the last answer to a card did not go, and `dismissed` are
// the cards that the person dismissed until the Mac says so.
interface Answering { id: string; option: string; label: string; sending: boolean; undone: boolean }
const answers = new Map<string, Answering>();
let choices = 0;
const failures = new Map<string, string>();
const dismissed = new Set<string>();
// How long an answer waits for its Undo before it goes (AppStore.answerUndoSeconds).
const UNDO_WAIT = 10000;

// DecisionSelection: the receipt of a card that is not pending, its mark, and whether it is dimmed.
const SERVICE_DECIDERS = new Set(['cancellation', 'archive', 'workflow_permission']);
const CHOICES = new Set(['approved', 'executed', 'rejected', 'modified', 'replied']);
const receiptOf = (c: CardData, gone: boolean, choosing: string): { label: string; mark: string; dim: boolean } => {
  const byService = SERVICE_DECIDERS.has(c.decidedBy);
  const pending = c.status === 'pending' && !c.refreshing;
  const check = CHOICES.has(c.status) && !byService;
  const told = c.outcome === '' ? '' : c.outcome.length <= 80 && !c.outcome.includes('\n') ? c.outcome : 'Closed';
  let label: string;
  if (gone) label = 'Dismissed';
  else if (byService) label = told || 'Closed';
  else if (c.draft) {
    label = ['pending', 'approved', 'executed'].includes(c.status) ? 'Approved for sending' : c.status === 'rejected' ? 'Discarded'
      : c.status === 'failed' ? 'Could not finish' : told || 'Closed';
  } else if (choosing !== '') label = choosing;
  else if (c.selected !== '') label = c.selected;
  else if (c.status === 'resolved' && told !== '') label = told;
  else label = c.status.slice(0, 1).toUpperCase() + c.status.slice(1);
  const mark = gone ? 'symbol:close' : c.status === 'failed' ? 'symbol:alert' : pending ? 'symbol:sf/arrow.up' : check ? 'symbol:checkmark' : '';
  return { label, mark, dim: !gone && c.status !== 'pending' && c.status !== 'failed' && !check };
};

// MARK: Photos

// The files of photos that the phone keeps (a tile and a larger copy of each), by artifact id, and the ones that did not
// load this launch. The copies live in the scratch folder of uploads, which the grants of build 19 name.
const PHOTOS = 'app:/tmp/upload/photos';
interface KeptPhoto { tile: string; full: string }
const kept = new Map<string, KeptPhoto>();
const unloadable = new Set<string>();
// How large a tile and the viewer's copy are: a tile of 280 points on a phone of 3 pixels a point, and a photo that
// fills a screen.
const TILE_SIDE = 840;
const FULL_SIDE = 2400;

// FileRules.isImageName: the files that the chat shows as photos.
const IMAGE_NAME = /\.(png|jpe?g|gif|webp|heic)$/i;
// TranscriptPresentation.photo: a message that is one Markdown image line, `![caption](address)`, with a web address or
// a file of the Mac (`coconut-file://<id>`); or the older form, an image address with an optional caption line above.
const photoIn = (text: string): { address: string; caption: string } | null => {
  if (text.length > 2048) return null;
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter((line) => line !== '');
  if (lines.length === 1) {
    const m = /^!\[((?:\\.|[^\]\\])*)\]\((?:<([^<>]+)>|([^\s<>()]+))\)$/.exec(lines[0]);
    const address = m ? (m[2] ?? m[3]).replace(/ /g, '%20') : '';
    if (m && /^(https|coconut-file):\/\/[^/\s]+/i.test(address)) return { address, caption: m[1].replace(/\\(.)/g, '$1') };
  }
  if (lines.length < 1 || lines.length > 2) return null;
  const address = lines[lines.length - 1];
  if (address.includes(' ') || !/^https:\/\/[^/\s]+/i.test(address) || !IMAGE_NAME.test(address.split(/[?#]/)[0])) return null;
  return { address, caption: lines.length === 2 ? lines[0] : '' };
};
const photoRef = (address: string, caption: string): PhotoRef => {
  const file = /^coconut-file:\/\/([0-9a-f-]{36})/i.exec(address);
  return file
    ? { key: `file:${file[1].toLowerCase()}`, file: file[1].toLowerCase(), url: '', caption, width: 0, height: 0 }
    : { key: `url:${address}`, file: '', url: address, caption, width: 0, height: 0 };
};
// The photo as a tile shows it: a file of the Mac once the phone keeps it, an address at once.
const photoOf = (p: PhotoRef): Photo => {
  if (p.file === '') return { key: p.key, src: p.url, full: p.url, caption: p.caption, state: 'ready', width: p.width, height: p.height };
  const have = kept.get(p.file);
  const state = have ? 'ready' : unloadable.has(p.file) ? 'failed' : 'loading';
  return { key: p.key, src: have?.tile ?? '', full: have?.full ?? '', caption: p.caption, state, width: p.width, height: p.height };
};

// MARK: Messages

// TranscriptPresentation.visibleMessages: what a chat leaves out. A reply still streaming without words, a card that the
// person answered in words (the answer shows in its place), reactions, the silence marker, settings notices, a generic
// "Working" or "Done" beside a real result, empty texts, the marker of a message of files alone, and the second copy of
// an answer that older workers kept as a file.
const GENERIC_PROGRESS = new Set(['working', 'working on your request', 'preparing an answer']);
const GENERIC_DONE = new Set(['ready to review', 'done', 'complete', 'completed', 'task complete', 'task completed', 'work complete', 'work completed']);
const isLabel = (text: string, labels: Set<string>): boolean => text.length <= 40 && labels.has(text.trim().replace(/^[.!…]+|[.!…]+$/g, '').toLowerCase());
const LEGACY_SETTINGS = 'New actions use this permission mode. An action already executing may finish.';

const kindName = (kind: string): string => kind.slice(0, 1).toUpperCase() + kind.slice(1);

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
  const all = new Map<string, RawDecision>(fx.decisions.map((d) => [d.id, d]));

  for (const bot of fx.bots) {
    const letter = letterOf(bot.avatar, bot.name);
    const messages = fx.messages.filter((m) => m.bot_id === bot.id).sort(bySent).map((m): Msg => {
      const at = rel(m.created_at);
      // A card and a file are messages with no words: the task they belong to names them.
      const decision = m.kind === 'decision' ? fx.decisions.find((d) => d.task_id === m.task_id) : undefined;
      const artifact = m.kind === 'artifact' ? fx.artifacts.find((a) => a.task_id === m.task_id) : undefined;
      if (decision) return message(m.id, 'decision', false, at, '', { card: cardOf(decision, all) });
      if (artifact) return message(m.id, 'artifact', false, at, artifact.preview, { title: artifact.title });
      return message(m.id, m.kind === 'activity' ? 'activity' : 'text', m.role === 'user', at, m.text);
    });
    if (!bot.chat_of) projects.push({ id: bot.id, name: bot.name, letter, face: faceOf(letter) });
    const label = bot.chat_of ? said(bot.chat_title) || UNTITLED : bot.name;
    // The New line goes under the newest message that the person read: the fixture counts the unread ones.
    const read = bot.unread_count > 0 ? messages[messages.length - 1 - bot.unread_count] : undefined;
    chats.set(bot.id, {
      id: bot.id, kind: 'agent', project: bot.chat_of ?? bot.id, projectName: bot.name, title: bot.chat_of ? `${bot.name} · ${label}` : bot.name,
      label, letter, face: faceOf(letter), circle: false, messages, run: null, unread: bot.unread_count, readMark: read?.id ?? '', health: '',
    });
    for (const d of fx.decisions) {
      if (d.bot_id === bot.id && d.status === 'pending') waits.push({ ref: d.id, chat: bot.id, at: rel(d.created_at) });
    }
  }
  demoExtras(chats, rel, waits);

  // A task that works is the running turn of its agent, with its newest step: the phase, its detail and its start, as
  // AppStore.runningPhase reads them. A task without a step yet starts at its last update and thinks.
  for (const task of fx.tasks) {
    const chat = chats.get(task.bot_id);
    if (!chat || task.status !== 'working') continue;
    chat.run = {
      startedAt: rel(task.phase_started_at ?? task.updated_at), phase: (task.phase && PHASES[task.phase]) || PHASES.thinking,
      detail: task.phase_detail ?? '', queued: 0,
    };
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
    id: teamId, kind: 'team', project: '', projectName: team.name, title: team.name, label: team.name, letter: team.name.slice(0, 1).toUpperCase(),
    face: faceOf(team.name.slice(0, 1).toUpperCase()), circle: true, run: null, unread: 0, readMark: '', health: '',
    messages: [
      message(`${teamId}-last`, 'text', false, -team.last.minutes_ago * MINUTE, team.last.text, { author: team.last.name }),
      ...notices.map((n) => message(`notice-${n.id}`, 'notice', false, n.at, n.text, { author: n.author, title: kindName(n.type) })),
    ].sort((a, b) => a.at - b.at),
  });
  for (const direct of team.directs) {
    const letter = direct.name.slice(0, 1).toUpperCase();
    chats.set(`direct-${direct.member}`, {
      id: `direct-${direct.member}`, kind: 'direct', project: '', projectName: direct.name, title: direct.name, label: direct.name, letter,
      face: faceOf(letter), circle: true, run: null, unread: direct.unread, readMark: '', health: '',
      messages: [message(`direct-${direct.member}-1`, 'text', false, -direct.minutes_ago * MINUTE, direct.text, { author: direct.name })],
    });
  }
  return { zero: null, chats, projects, notices, waits, stuck, teamId, counter: 0 };
};

// What the demo shows besides the fixture, so its chats have the parts that the fixture lacks: a photo and a draft card
// in Payouts, and a background process that finished and a card with a source, a risk, a recommended option and a
// preview in a chat of Releases. The pictures are in the app's assets (assets/demo), charts of the numbers that the
// fixture's messages give.
const demoExtras = (chats: Map<string, ChatState>, rel: (iso: string) => number, waits: Wait[]): void => {
  const add = (chatId: string, row: Msg): void => {
    const chat = chats.get(chatId);
    if (!chat) return;
    chat.messages = [...chat.messages, row].sort((a, b) => a.at - b.at);
    if (row.card?.status === 'pending') waits.push({ ref: row.card.ref, chat: chatId, at: row.at });
  };
  const card = (ref: string, extra: Partial<CardData>): CardData => ({
    ref, title: '', sources: [], diff: '', context: [], risk: '', reason: '', stale: false, previews: [], draft: null, options: [],
    status: 'pending', refreshing: false, decidedBy: '', selected: '', outcome: '', earlier: '', revised: false, revision: 1, draftHash: '',
    previewsHash: '', ...extra,
  });
  const payouts = '00000000-0000-4000-8000-000000000003';
  add(payouts, message('demo-photo-payouts', 'photos', false, rel('2026-09-08T11:21:00Z'), '', {
    photos: [{ key: 'url:assets/demo/failed-payouts.png', file: '', url: 'assets/demo/failed-payouts.png', caption: 'Failed payouts by reason', width: 1260, height: 810 }],
    from: `assistant:${payouts}`,
  }));
  add(payouts, message('demo-card-payouts', 'decision', false, rel('2026-09-08T11:26:00Z'), '', {
    card: card('demo-draft-payouts', {
      title: 'Reply to Jordan about the missing payout?',
      context: ['Jordan Lee asked Support why today\'s payout did not arrive. Their bank rejected the account number on file, so the reply says how to fix it before the next run.'],
      draft: {
        via: 'Gmail', from: 'payouts@gumroad.com', to: 'jordan@leestudio.co', replyTo: '', subject: 'Re: Where is my payout?',
        body: 'Hi Jordan,\n\nYour bank rejected the account number on file, so today\'s payout did not reach you. Update it in Settings → Payments, and the next payout run will send the money.\n\nThe Gumroad team',
      },
      options: [
        { id: 'send', label: 'Send', explanation: 'Send this exact message.', recommended: false, risk: '', reason: '', answer: 'approve' },
        { id: 'discard', label: 'Discard', explanation: 'Send nothing.', recommended: false, risk: '', reason: '', answer: 'reject' },
      ],
    }),
  }));
  const sidekiq = '00000000-0000-4000-8000-000000000004';
  add(sidekiq, message('demo-process-queues', 'process', false, rel('2026-09-08T09:00:00Z'), 'Finished in gumroad · 4s', {
    title: 'bin/rails runner Sidekiq::Queue.all', detail: 'cd ~/gumroad && bin/rails runner \'Sidekiq::Queue.all.each { |q| puts "#{q.name} #{q.size}" }\'',
    from: `assistant:${sidekiq}`,
  }));
  add(sidekiq, message('demo-card-webhooks', 'decision', false, rel('2026-09-08T09:07:00Z'), '', {
    card: card('demo-webhooks', {
      title: 'Turn off webhooks for the seller\'s endpoint?',
      sources: ['Issue · Webhook retries fill the low queue'],
      context: ['The paused retries go to one seller\'s endpoint, which answers 502. Turning off its webhooks drops the 41 retries for good, and Gumroad emails the seller how to turn them back on.'],
      risk: 'Medium risk', reason: 'The seller gets no sale webhooks until they turn them back on.',
      previews: [{ key: 'url:assets/demo/webhook-answers.png', file: '', url: 'assets/demo/webhook-answers.png', caption: 'Answers of the endpoint, last 24 hours', width: 1260, height: 810 }],
      options: [
        { id: 'off', label: 'Turn off webhooks', explanation: 'Drops the 41 retries and emails the seller.', recommended: true, risk: '', reason: '', answer: 'approve' },
        { id: 'keep', label: 'Keep them paused', explanation: 'The retries start again once the endpoint answers 200.', recommended: false, risk: 'Low risk', reason: 'The low queue keeps the 41 jobs until then.', answer: 'approve' },
      ],
    }),
  }));
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
// The time of a row made here, `at` milliseconds after the launch, in the times of a state.
const localAt = (s: State, at: number): number => (s.zero === null ? at : launchAt + at - s.zero);
// The rows of `outgoing` in the state that the screens read now, in place of the ones it showed before.
const showOutgoing = (chatId: string): void => {
  const s = ensure();
  const chat = s.chats.get(chatId);
  if (!chat || s.zero === null) return;
  const zero = s.zero;
  const rows = (outgoing.get(chatId) ?? []).flatMap((send) => send.rows.map((m) => ({ ...m, at: m.at - zero })));
  chat.messages = [...chat.messages.filter((m) => !m.id.startsWith('out-')), ...rows];
};

// The newest message that is no notice: the age and the place of the row of a chat.
const newest = (chat: ChatState): number => chat.messages.reduce((t, m) => (m.kind === 'notice' ? t : Math.max(t, m.at)), -Infinity);
const waitingCard = (chat: ChatState): boolean => chat.messages.some((m) => m.card !== null && m.card.status === 'pending' && !m.card.refreshing && !dismissed.has(m.card.ref));

// The line under the name of a chat: the newest text, with who wrote it (ChatRowText). A photo reads as Photo, with its
// caption (MessagePhoto.preview).
const lastLine = (chat: ChatState): string => {
  for (let i = chat.messages.length - 1; i >= 0; i--) {
    const m = chat.messages[i];
    const photo = m.kind === 'photos' ? m.photos[m.photos.length - 1] : undefined;
    const text = photo ? (photo.caption === '' ? 'Photo' : `Photo: ${photo.caption}`) : m.kind === 'text' ? m.body.trim() : '';
    if (text === '') continue;
    const who = m.mine ? 'You: ' : chat.kind === 'team' && m.author !== '' ? `${m.author}: ` : '';
    return who + text.replace(/\s+/g, ' ');
  }
  return 'No messages yet';
};

// BotRowSubtitle: a card that waits first, then the phase of the work, then a health, then the last message. A row
// that shows the phase times its newest step (`step`), as BotContactRow does.
const rowOf = (chat: ChatState, zero: number): Row => {
  const status = waitingCard(chat) ? 'needs_you' : chat.run ? 'working' : chat.health !== '' ? 'health' : 'idle';
  const line = status === 'needs_you' ? 'Needs you' : status === 'working' && chat.run ? chat.run.phase : status === 'health' ? chat.health : lastLine(chat);
  const at = newest(chat);
  return {
    id: chat.id, kind: chat.kind, title: chat.title, letter: chat.letter, face: chat.face, circle: chat.circle, line, status,
    // A muted chat counts no unread, as in the Swift app (BotChatMute.unread): no badge, and it does not move up.
    active: chat.run !== null, unread: chat.muted === true ? 0 : chat.unread, at: Number.isFinite(at) ? zero + at : zero,
    step: status === 'working' && chat.run ? zero + chat.run.startedAt : 0,
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
// An archived chat does not count, as in AgentChats.activity.
const ordered = (s: State): Project[] => {
  const latest = (p: Project): number => Array.from(s.chats.values()).filter((c) => c.project === p.id && !c.archived).reduce((t, c) => Math.max(t, newest(c)), -Infinity);
  return [...s.projects].sort((a, b) => latest(b) - latest(a) || (a.name < b.name ? -1 : 1));
};

// MARK: The Mac's state

// The parts of the Mac's state (`GET /v1/state` with recent content) that are read here.
interface LvOption { id: string; label: string; explanation: string; answer: string; risk?: string | null; risk_level?: string | null }
interface LvBot {
  id: string; name: string; avatar: string; archived: boolean; pinned: boolean; project?: boolean; profile: string; status: string;
  unread_count: number; chat_of?: string | null; chat_title?: string | null; last_read_message_id?: string | null; notifications?: boolean;
}
interface LvReference { message_id: string; text: string; author: string; role: string; document_id?: string | null }
interface LvNotice { ok: boolean; command?: string | null; label?: string | null; place?: string | null; duration?: string | null; exit_code?: number | null; output?: string | null }
interface LvMessage {
  id: string; bot_id: string; task_id: string | null; kind: string; role: string; text: string; created_at: string; decision_id: string | null;
  artifact_id: string | null; streaming?: boolean; sender_bot_id?: string | null; recipient_bot_ids?: string[] | null; thread_id?: string | null;
  reply_to?: LvReference | null; work_context?: { id: string; title: string } | null; reaction_event?: unknown; process_notice?: LvNotice | null;
  document_id?: string | null;
}
interface LvTask { id: string; bot_id: string; status: string; title: string | null; created_at: string; updated_at: string; phase?: string | null; phase_detail?: string | null; phase_started_at?: string | null }
interface LvDecision extends RawDecision { bot_id: string; created_at: string; revision: number; options: LvOption[] }
interface LvArtifact { id: string; title: string; preview: string; kind?: string; status?: string; url?: string | null }
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
  settings_changed?: boolean;
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
    // The messages stay in the order they came: `fromSnapshot` orders the messages of each chat, and a sort of every
    // message of the Mac on each page cost the phone's time.
    const gone = new Set(page.removed_messages ?? []);
    next.messages = merge(page.messages ?? [], snap.messages).filter((m) => !gone.has(m.id));
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

// The models of the Mac (`model_catalog` of GET /v1/state/settings) and the model of each chat that has one of its own,
// read with the state. The demo's are the fixture's, which `/model` and `/effort` change in memory.
let macCatalog: Catalog | null = null;
const macModels = new Map<string, Selection>();
// The time on the Mac of the last read of its settings: as in the Swift app, they are read again at most once a
// minute, and at once when an update says that they changed.
let settingsAt = -Infinity;
let demoCatalog: Catalog | null = null;
const catalogNow = (): Catalog | null => {
  if (useLive) return macCatalog;
  if (!demoCatalog) demoCatalog = { default: { ...fx.model_catalog.default }, models: fx.model_catalog.models };
  return demoCatalog;
};
const entryOf = (catalog: Catalog, s: Selection): CatalogModel | undefined => catalog.models.find((m) => m.id === s.model && m.provider === s.provider);
// A model in the slash menu: its provider and its id, as ModelCatalogEntry.selectionKey.
const keyOf = (m: { provider: string; id: string }): string => `${m.provider}\u001f${m.id}`;
// The name of the model of a chat, as the working line shows it: its own model on the Mac, else the default.
const modelOfChat = (id: string): string => {
  const catalog = catalogNow();
  const selection = (useLive ? macModels.get(id) : undefined) ?? catalog?.default;
  if (!selection) return '';
  return (catalog ? entryOf(catalog, selection)?.name : undefined) ?? modelName(selection.model);
};
// ModelPresentation.effortName: none is Off, low is Low.
const effortName = (effort: string): string => (effort === 'none' ? 'Off' : effort.slice(0, 1).toUpperCase() + effort.slice(1));
// The commands of the slash menu that the app knows. The Swift app has more (ComposerSlash.commands), which come with
// the rest of its composer.
const COMMANDS = [
  { name: 'model', detail: 'Change the model', symbol: 'symbol:sf/cpu' },
  { name: 'effort', detail: 'Change the reasoning effort', symbol: 'symbol:sf/brain' },
];

// The text of a message of files alone: the Mac needs a text for each message, and the chat does not show this one
// (AttachmentOnly).
const ATTACHMENT_ONLY = '[attachment]';
// The epoch of the launch, which the screens give with each read: a time of the contract is from the launch.
let launchAt = 0;
// A message that this phone sends to a chat of the Mac, from Send until a state of the Mac shows it: its rows, at times
// since 1970 (the words and a row for each file, and why it failed), its text as the Mac keeps it, and the ids of the
// chat's messages with that text when it was sent. The Mac answers a message with its task, not the message, so a
// state shows the message when it has a message of the person with that text and a new id. Each state shows the rows
// of the sends that it does not show yet after its own rows. A failure stays until the next send of the chat, and a
// send that went and that two states do not show goes with the third (`states`).
interface Outgoing { rows: Msg[]; text: string; before: Set<string>; went: boolean; states: number; failed: boolean }
const outgoing = new Map<string, Outgoing[]>();
// The number of the last send, for the ids of its rows (`out-<n>`). Each state of the Mac counts its own rows from 0
// again, and the rows of a send outlive states.
let sends = 0;
const shows = (snap: Snapshot, chatId: string, send: Outgoing): boolean => snap.messages.some((m) =>
  m.bot_id === chatId && m.role === 'user' && m.kind === 'text' && m.text.trim() === send.text && !send.before.has(m.id));
// A message that shows and has yet to go, by its chat: what `queueMessage` took for `sendMessage`, and its rows in a
// chat of the Mac. `at` is from the launch.
const unsent = new Map<string, { words: string; files: PickedFile[]; photos: string[]; at: number; send: Outgoing | null }>();

// The messages of a chat that it shows (TranscriptPresentation.visibleMessages, see GENERIC_PROGRESS).
const visible = (messages: LvMessage[], artifacts: Map<string, LvArtifact>): LvMessage[] => {
  // The tasks whose agent gave a real result: a generic "Done" of theirs says nothing more.
  const answered = new Set<string>();
  for (const o of messages) {
    if (o.task_id && o.role === 'assistant' && ['text', 'artifact', 'decision'].includes(o.kind) && o.text !== '' && !isLabel(o.text, GENERIC_DONE)) {
      answered.add(`${o.task_id} ${o.sender_bot_id ?? o.bot_id}`);
    }
  }
  return messages.filter((m) => {
    const text = m.text.trim();
    if (m.streaming && (text === '' || '[SILENT]'.startsWith(text))) return false;
    if (m.kind === 'decision' && m.decision_id && decisions.get(m.decision_id)?.status === 'replied') return false;
    if (m.reaction_event) return false;
    if (['text', 'bot_exchange'].includes(m.kind) && (m.role === 'assistant' || !!m.sender_bot_id) && !m.decision_id && !m.artifact_id && text === '[SILENT]') return false;
    if (m.role === 'system' && !m.decision_id && !m.artifact_id && !(m.recipient_bot_ids ?? []).length && !m.streaming
      && (['policy.updated', 'settings.updated'].includes(m.kind) || (m.kind === 'activity' && !m.task_id && !m.thread_id && text === LEGACY_SETTINGS))) return false;
    const completion = m.kind === 'activity' || (m.kind === 'text' && m.role === 'assistant' && !m.decision_id && !(m.recipient_bot_ids ?? []).length && !m.streaming);
    if (completion && m.task_id && isLabel(m.text, GENERIC_DONE) && answered.has(`${m.task_id} ${m.sender_bot_id ?? m.bot_id}`)) return false;
    if (m.kind === 'activity') return !isLabel(m.text, GENERIC_PROGRESS);
    if (m.kind === 'text' && text === '') return false;
    if (m.kind === 'text' && m.role === 'user' && text === ATTACHMENT_ONLY) return false;
    // Older workers saved the answer of a task twice: as its result and as a message.
    const artifact = m.kind === 'artifact' && m.artifact_id ? artifacts.get(m.artifact_id) : undefined;
    if (!artifact || artifact.kind !== 'task' || artifact.status !== 'completed' || artifact.url || !artifact.preview || !m.task_id) return true;
    return !messages.some((a) => a.task_id === m.task_id && a.kind === 'text' && a.role === 'assistant' && !a.streaming
      && !(a.recipient_bot_ids ?? []).length && (a.sender_bot_id ?? a.bot_id) === (m.sender_bot_id ?? m.bot_id) && a.text.startsWith(artifact.preview));
  });
};

// A message of the Mac as a row of its chat (TranscriptMessageRow): a photo, a card, a file, a page of the Library, a
// background process that ended, an activity line or a bubble. `names` are the names of the Mac's agents.
const rowOfMessage = (m: LvMessage, at: number, artifacts: Map<string, LvArtifact>, names: Map<string, string>): Msg => {
  const mine = m.role === 'user';
  // Who wrote a message to another agent, and to whom: the sender, an arrow and the others.
  const recipients = m.recipient_bot_ids ?? [];
  const to = recipients.length === 1 && recipients[0] === m.bot_id ? [] : recipients;
  const toNames = to.map((id) => names.get(id) ?? 'Deleted agent').join(', ');
  const from = m.sender_bot_id ?? '';
  const sender = from !== '' && names.has(from) && (from !== m.bot_id || recipients.length > 0)
    ? `${names.get(from)}${toNames === '' ? '' : ` → ${toNames}`}`
    : from !== '' && from !== m.bot_id ? 'Deleted agent' : mine && toNames !== '' ? `To ${toNames}` : '';
  // The message that this one answers: a card that the person answered in words, or a quoted message or page.
  let quote = '';
  let quoteAuthor = '';
  const card = m.decision_id ? decisions.get(m.decision_id) : undefined;
  if (mine && m.decision_id && m.reply_to?.message_id === m.decision_id) {
    quote = m.reply_to.text;
    quoteAuthor = m.reply_to.author;
  } else if (mine && card?.status === 'replied') {
    quote = card.title;
    quoteAuthor = names.get(card.bot_id) ?? 'Agent';
  } else if (m.reply_to) {
    quote = m.reply_to.text;
    quoteAuthor = m.reply_to.role === 'document' ? 'Library page' : m.reply_to.author;
  }
  const base = { sender, quote, quoteAuthor, from: `${m.role} ${[m.sender_bot_id ?? m.bot_id, ...recipients].join(' ')}` };
  // An image that the person attached shows as the image, and so does a reply of an agent that is one image.
  const artifact = m.artifact_id ? artifacts.get(m.artifact_id) : undefined;
  if (mine && m.kind === 'artifact' && m.artifact_id && IMAGE_NAME.test(m.text)) {
    return message(m.id, 'photos', true, at, '', { ...base, photos: [photoRef(`coconut-file://${m.artifact_id}`, '')] });
  }
  const photo = !mine && m.role === 'assistant' && m.kind === 'text' && !m.streaming && !m.decision_id && !m.artifact_id && !m.reply_to && !m.work_context
    ? photoIn(m.text) : null;
  if (photo) return message(m.id, 'photos', false, at, '', { ...base, photos: [photoRef(photo.address, photo.caption)] });
  if (m.kind === 'decision' && card) return message(m.id, 'decision', false, at, '', { ...base, card: cardOf(card, decisions) });
  if (artifact) return message(m.id, 'artifact', mine, at, artifact.preview, { ...base, title: artifact.title });
  if (m.kind === 'document') return message(m.id, 'document', false, at, '', { ...base, title: m.text.trim() || 'Library page' });
  const notice = m.role === 'assistant' ? m.process_notice : null;
  if (notice) {
    // ProcessNoticeRow: "Finished in coconut" or "Failed · exit 2", with the run time. A failure shows the last lines
    // of its output at once; the command shows on a tap.
    const status = (notice.ok ? 'Finished' : 'Failed') + (notice.place ? ` in ${notice.place}` : '');
    const caption = [status, !notice.ok && notice.exit_code != null ? `exit ${notice.exit_code}` : '', notice.duration ?? ''].filter((part) => part !== '').join(' · ');
    return message(m.id, 'process', false, at, caption, {
      ...base, title: notice.label || notice.command || 'Background task', detail: notice.ok ? said(notice.command) : said(notice.output) || said(notice.command),
      failed: !notice.ok,
    });
  }
  if (m.kind === 'activity') return message(m.id, 'activity', false, at, m.text, base);
  return message(m.id, 'text', mine, at, m.text, base);
};

const fromSnapshot = (snap: Snapshot): State => {
  const server = parse(snap.server_time);
  const rel = (iso: string): number => parse(iso) - server;
  // An archived chat shows too: Idle has it among the others, as ChatList.all of the Swift app has it since the person
  // merged Idle and Archived. A message of the person brings it back on the Mac.
  const bots = snap.bots;
  // A project is a bot with `project`; its chats are the bots of its Hermes profile. The Mac never archives a project.
  const projectOf = new Map(bots.filter((b) => b.project === true && !b.archived).map((b) => [b.profile, b]));
  const artifacts = new Map(snap.artifacts.map((a) => [a.id, a]));
  decisions.clear();
  for (const d of snap.decisions) decisions.set(d.id, d);
  // An answer that went, a dismissal and a failure end once the Mac no longer has the card pending.
  for (const [ref, holding] of answers) if (holding.sending && decisions.get(ref)?.status !== 'pending') answers.delete(ref);
  for (const ref of dismissed) if (decisions.get(ref)?.status !== 'pending') dismissed.delete(ref);
  for (const ref of failures.keys()) if (decisions.get(ref)?.status !== 'pending') failures.delete(ref);
  const byBot = new Map<string, LvMessage[]>();
  for (const m of snap.messages) {
    const list = byBot.get(m.bot_id);
    if (list) list.push(m);
    else byBot.set(m.bot_id, [m]);
  }
  const bySent = (a: LvMessage, b: LvMessage): number =>
    (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
  const chats = new Map<string, ChatState>();
  const projects: Project[] = [];
  const waits: Wait[] = [];
  const names = new Map(bots.map((b) => [b.id, b.name]));
  for (const bot of bots) {
    const project = projectOf.get(bot.profile) ?? bot;
    const letter = letterOf(bot.avatar || project.avatar, project.name);
    if (bot.project === true && !bot.archived) projects.push({ id: bot.id, name: bot.name, letter, face: faceOf(letter) });
    const messages = visible((byBot.get(bot.id) ?? []).sort(bySent), artifacts).map((m) => rowOfMessage(m, rel(m.created_at), artifacts, names));
    // What this phone sends to the chat and the Mac does not show yet.
    const sends = (outgoing.get(bot.id) ?? []).filter((send) => !shows(snap, bot.id, send) && !(send.went && ++send.states > 2));
    if (sends.length > 0) outgoing.set(bot.id, sends);
    else outgoing.delete(bot.id);
    for (const send of sends) for (const row of send.rows) messages.push({ ...row, at: row.at - server });
    // A chat that nobody wrote in shows nowhere (AgentChats.isBlank), but a project and a starred chat always show.
    if (messages.length === 0 && bot.unread_count === 0 && bot.status === 'idle' && !bot.pinned && bot.project !== true) continue;
    // A further chat of a project has a title of its own, which the list names after the project (Bot.listName).
    const label = bot.chat_of ? said(bot.chat_title) || UNTITLED : bot.name;
    chats.set(bot.id, {
      id: bot.id, kind: 'agent', project: project.id, projectName: project.name, title: bot.chat_of ? `${bot.name} · ${label}` : bot.name, label,
      letter, face: faceOf(letter), circle: false, messages, run: null, unread: bot.unread_count, readMark: bot.last_read_message_id ?? '',
      health: '', archived: bot.archived, muted: bot.notifications === false,
    });
  }
  for (const d of snap.decisions) {
    if (d.status === 'pending' && !dismissed.has(d.id) && chats.has(d.bot_id)) waits.push({ ref: d.id, chat: d.bot_id, at: rel(d.created_at) });
  }
  running.clear();
  const stuck: Stuck[] = [];
  for (const task of snap.tasks) {
    const chat = chats.get(task.bot_id);
    if (!chat) continue;
    if (task.status === 'working') {
      running.set(chat.id, task.id);
      // The newest step that the Mac reported: what the agent does now, and since when (AppStore.runningPhase).
      chat.run = {
        startedAt: rel(task.phase_started_at ?? task.created_at), phase: (task.phase && PHASES[task.phase]) || PHASES.working,
        detail: task.phase_detail ?? '', queued: 0,
      };
    }
    // The work of an archived chat does not count, as in AttentionRules.stuckWork.
    if (task.status === 'failed' && !chat.archived && -rel(task.updated_at) <= DAY) stuck.push({ title: task.title || 'A task failed', chat: chat.id, at: rel(task.updated_at) });
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
// A phone that is no longer paired keeps nothing of the Mac: not its keys, its state, its cache or its models.
const forget = (store: Store, storage: Storage): void => {
  pairing = null;
  liveState = undefined;
  latest = null;
  cached = false;
  macCatalog = null;
  macModels.clear();
  outgoing.clear();
  unsent.clear();
  pairs += 1;
  try {
    store.forget(SAVED);
  } catch {
    // Nothing kept.
  }
  void withCache(storage, (db) => db.execute('DELETE FROM snapshot')).catch(() => undefined);
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
    // The mapper woke this answer inside another answer's turn: go on in a turn of its own. An answer that was let go
    // meanwhile fails there, and the message stays for the next reader.
    try {
      await pause();
    } catch (error) {
      c.queue.unshift(bytes);
      throw error;
    }
    return bytes;
  },
  unread(bytes: Uint8Array): void {
    c.queue.unshift(bytes);
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

// Whether the session of the open socket takes requests.
const connected = (): boolean => tunnel !== null && !tunnel.session.broken && conn !== null && conn.ended === '' && tunnelKey === conn.key;
// How long a write waits for the connection, and how often it looks.
const CONNECT_WAIT = 25000;
const CONNECT_LOOK = 250;

// A write to the Mac on the open connection: a value as JSON, or the bytes of a JSON body made already. It returns the
// answer's JSON, or throws the Mac's reason. A write that comes while there is no connection (a round ended, and the
// next one connects) waits for the next one, as the Swift app sends a request again on a new socket (APIClient), and
// says why the Mac cannot be reached only when none comes.
const write = async (method: string, path: string, body: unknown): Promise<unknown> => {
  for (let waited = 0; !connected(); waited += CONNECT_LOOK) {
    if (waited >= CONNECT_WAIT) throw new Error(lastError || 'Your Mac is not connected.');
    await new Promise<void>((resolve) => setTimeout(resolve, CONNECT_LOOK));
  }
  const t = tunnel as Tunnel;
  const bytes = body instanceof Uint8Array ? body : utf8(JSON.stringify(body ?? {}));
  const reply = await t.request(method, path, headers(method), bytes);
  if (reply.status < 200 || reply.status >= 300) throw new Error(failureOf(reply));
  return reply.body.length > 0 ? JSON.parse(text(reply.body)) : {};
};

// MARK: Attachments

// The largest file that the Mac takes (`_attachment` in backend/coconut/service.py, FileRules.largest), and the longest
// side of a photo that goes as it is (FileFit.longestSide).
const LARGEST = 4 * 1024 * 1024;
const LONGEST_SIDE = 2400;
const SIZE_TEXT = 'Choose a file between 1 byte and 4 MB.';
const CANNOT_SEND = 'This file cannot be sent.';
// `_validate_attachments`: a message takes at most 20 files.
const MOST_FILES = 20;
// What one answer may send on a socket in all its turns: 1 MiB (Exact, LLP 1016.000 D7, `socket answer exceeds 1 MiB`).
// A message and its files go in one answer (`sendMessage`), so they share it: the files of a message can take about
// 750 KB, as base64 in frames of the tunnel, and a photo becomes a JPEG that fits what is left. Sending files in parts
// over more answers would lift this.
const ANSWER_BYTES = 1024 * 1024;
// What a request costs besides its body: the head with its headers, the end, and room to spare; and what the answer
// keeps aside besides.
const REQUEST_BYTES = 2048;
const SPARE_BYTES = 16 * 1024;
// What a body of `size` bytes costs in frames of the tunnel (a header of 6 bytes and a tag of 16 for each).
const bodyBytes = (size: number): number => size + Math.ceil(size / MAX_PAYLOAD) * 22;
// What the file of `size` bytes named `name` costs as an upload, and the largest file that `room` takes.
const uploadBytes = (name: string, size: number): number => REQUEST_BYTES + bodyBytes(utf8(name).length * 2 + 128 + Math.ceil(size / 3) * 4);
const roomFor = (name: string, room: number): number => Math.floor(((room - uploadBytes(name, 0)) * 3) / 4 * (MAX_PAYLOAD / (MAX_PAYLOAD + 22)));
const TOO_MUCH = 'The files of one message can take about 750 KB in this app.';

// A file of the pickers, as `change` gives it (`Picked`): `width` and `height` are null when the picker did not read them.
interface PickedFile { path: string; name: string; type: string; size: number; width: number | null; height: number | null }

// FileFit.jpegName: a photo that becomes a JPEG takes the extension .jpg.
const jpegName = (name: string): string => `${name.replace(/\.[^./]*$/, '') || 'Photo'}.jpg`;

// The file as it goes to the Mac (FileFit.outcome of an agent chat, which keeps the metadata of a photo), within the
// `room` that the message leaves it: a file of the file picker as it is, from 1 byte to 4 MB. A photo of the library goes
// as it is when it fits 2400 pixels and the room and is no HEIC or HEIF image, and a GIF always does, because a JPEG
// loses its frames; any other photo becomes a JPEG of at most 2400 pixels that fits the room.
const fitted = async (storage: Storage, file: PickedFile, photo: boolean, n: number, room: number): Promise<{ path: string; name: string; size: number }> => {
  const type = file.type.toLowerCase();
  const heif = /^image\/hei[cf]/.test(type) || /\.hei[cf]$/i.test(file.name);
  const longest = Math.max(file.width ?? 0, file.height ?? 0);
  const fits = Math.min(LARGEST, roomFor(file.name, room));
  const keep = !photo || !type.startsWith('image/') || type === 'image/gif' || (!heif && longest <= LONGEST_SIDE && file.size <= fits);
  if (keep) {
    if (file.size < 1 || file.size > LARGEST) throw new Error(SIZE_TEXT);
    if (file.size > fits) throw new Error(TOO_MUCH);
    return { path: file.path, name: file.name, size: file.size };
  }
  const name = jpegName(file.name);
  const most = Math.min(LARGEST, roomFor(name, room));
  if (most < 16 * 1024) throw new Error(TOO_MUCH);
  try {
    await storage.fs.mkdir(UPLOADS);
  } catch (error) {
    if ((error as { code?: string }).code !== 'EEXIST') throw error;
  }
  try {
    const out = await storage.fs.compressImage(file.path, `${UPLOADS}/${n}.jpg`, { maxDimension: LONGEST_SIDE, maxBytes: most, quality: 0.8 });
    return { path: out.path, name, size: out.size };
  } catch (error) {
    const code = (error as { code?: string }).code;
    throw new Error(code === 'unfit' || code === 'too-large' ? (most < LARGEST ? TOO_MUCH : SIZE_TEXT) : CANNOT_SEND);
  }
};

const BASE64 = Uint8Array.from('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/', (c) => c.charCodeAt(0));
// What POST /v1/attachments takes (AttachmentRequest): the agent, the file name and the bytes in base64. The base64 goes
// straight into the bytes of the body, a part of the file in each turn, so a file of 4 MB makes no string of 5.6 MB.
const attachmentBody = async (botId: string, filename: string, bytes: Uint8Array): Promise<Uint8Array> => {
  const head = utf8(`${JSON.stringify({ bot_id: botId, filename }).slice(0, -1)},"content_base64":"`);
  const tail = utf8('"}');
  const out = new Uint8Array(head.length + Math.ceil(bytes.length / 3) * 4 + tail.length);
  out.set(head, 0);
  let o = head.length;
  const STEP = 3 * 32768;
  for (let start = 0; start < bytes.length; start += STEP) {
    const end = Math.min(start + STEP, bytes.length);
    let i = start;
    for (; i + 2 < end; i += 3) {
      const n = (bytes[i] << 16) | (bytes[i + 1] << 8) | bytes[i + 2];
      out[o++] = BASE64[n >>> 18];
      out[o++] = BASE64[(n >>> 12) & 63];
      out[o++] = BASE64[(n >>> 6) & 63];
      out[o++] = BASE64[n & 63];
    }
    // The last one or two bytes of the file, with the padding of base64.
    if (i < end) {
      const two = end - i === 2;
      const n = (bytes[i] << 16) | (two ? bytes[i + 1] << 8 : 0);
      out[o++] = BASE64[n >>> 18];
      out[o++] = BASE64[(n >>> 12) & 63];
      out[o++] = two ? BASE64[(n >>> 6) & 63] : 61;
      out[o++] = 61;
    }
    await pause();
  }
  out.set(tail, o);
  return out;
};

// The files of a message on the Mac, one upload each, in the order of the picks (AppStore.uploadAttachment): the ids of
// their attachments. Each file is fitted into what the message and the files before it leave of the answer's bytes
// before any goes, so a message whose files do not fit sends none. A copy that a photo needed goes away after the uploads.
const upload = async (storage: Storage, botId: string, words: string, files: PickedFile[], photos: string[]): Promise<string[]> => {
  const fits: Array<{ path: string; name: string; size: number }> = [];
  try {
    let room = ANSWER_BYTES - SPARE_BYTES - REQUEST_BYTES - bodyBytes(utf8(words).length * 2 + 512);
    for (const [n, file] of files.entries()) {
      const fit = await fitted(storage, file, photos.includes(file.path), n, room);
      fits.push(fit);
      room -= uploadBytes(fit.name, fit.size);
      if (room < 0) throw new Error(TOO_MUCH);
    }
    const ids: string[] = [];
    for (const fit of fits) {
      const bytes = new Uint8Array(await storage.fs.readFile(fit.path));
      if (bytes.length < 1 || bytes.length > LARGEST) throw new Error(SIZE_TEXT);
      const made = (await write('POST', '/v1/attachments', await attachmentBody(botId, fit.name, bytes))) as { id?: string };
      if (!made.id) throw new Error('Your Mac did not keep the file.');
      ids.push(made.id);
      // A photo that goes keeps its copies for the Mac's file it becomes, so the chat does not load it back.
      if (IMAGE_NAME.test(fit.name)) await keepPhoto(storage, made.id.toLowerCase(), fit.path).catch(() => undefined);
    }
    return ids;
  } finally {
    for (const [n, fit] of fits.entries()) {
      if (fit.path !== files[n].path) await storage.fs.rm(fit.path).catch(() => undefined);
    }
  }
};

// The size of a file as the Mac writes it under the file (`preview` of an attachment).
const bytesText = (size: number): string => `${String(size).replace(/\B(?=(\d{3})+(?!\d))/g, ',')} bytes`;

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

// The JSON of an answer, which the Mac gzips when the request takes it.
const jsonOf = async (reply: Reply): Promise<unknown> => {
  const encoding = Object.entries(reply.headers).find(([name]) => name.toLowerCase() === 'content-encoding')?.[1] ?? '';
  const body = encoding.toLowerCase() === 'gzip' ? await gunzip(reply.body) : reply.body;
  await pause();
  const value = JSON.parse(text(body)) as unknown;
  await pause();
  return value;
};

// The models of the Mac and the model of each chat that has its own (GET /v1/state/settings, as NativeSnapshot). A read
// that fails keeps what the app had, and a later one tries again.
const readSettings = async (t: Tunnel, at: number): Promise<void> => {
  settingsAt = at;
  try {
    const reply = await t.request('GET', '/v1/state/settings', headers('GET', { 'Accept-Encoding': 'gzip' }));
    if (reply.status !== 200) return;
    const settings = (await jsonOf(reply)) as { bots: Array<{ id: string; model_override: Selection | null; effective_model: Selection | null }>; model_catalog: Catalog | null };
    if (settings.model_catalog) macCatalog = settings.model_catalog;
    macModels.clear();
    for (const bot of settings.bots) if (bot.model_override) macModels.set(bot.id, bot.effective_model ?? bot.model_override);
    version += 1;
  } catch {
    // Kept as it was.
  }
};

// MARK: The cache

// The last state of the Mac on the phone (SQLite, as the Swift app keeps snapshot.json), so a launch shows the chats
// before the Mac answers, or without it: one row, for the Mac of the pairing. It is only shown: the first read of a
// launch still takes the whole state, so a state that the phone kept for days never grows with their updates.
const CACHE_EVERY = 10_000;
// The time on the Mac of the last write, whether the state changed since, whether the screens show the cache because
// the Mac has not answered yet, and the reads of it that this launch tried.
let cachedAt = -Infinity;
let unsaved = false;
let cached = false;
let cacheTries = 0;
// An open database locks its file, so one use of the cache at a time, in order.
let cacheTail: Promise<void> = Promise.resolve();
const withCache = async <T>(storage: Storage, work: (db: Database) => Promise<T>): Promise<T> => {
  const previous = cacheTail;
  let release = (): void => undefined;
  cacheTail = new Promise<void>((resolve) => {
    release = resolve;
  });
  try {
    await previous;
    const db = await storage.sqlite.open(CACHE);
    try {
      const table = await db.query("SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'snapshot'");
      if (table.rows.length === 0) await db.execute('CREATE TABLE IF NOT EXISTS snapshot (route TEXT PRIMARY KEY, state TEXT NOT NULL)');
      return await work(db);
    } finally {
      await db.close();
    }
  } finally {
    release();
  }
};

// The kept state of the Mac of `route`, or null: none, another Mac's, or one that cannot be read. It throws when the
// storage does not answer.
const readCache = async (storage: Storage, route: string): Promise<Snapshot | null> => {
  const found = await withCache(storage, (db) => db.query('SELECT state FROM snapshot WHERE route = ?', [route]));
  const value = found.rows[0]?.[0];
  if (typeof value !== 'string') return null;
  await pause();
  try {
    return JSON.parse(value) as Snapshot;
  } catch {
    return null;
  } finally {
    await pause();
  }
};

// The state replaces the kept one, and a state of another Mac goes. A write that fails leaves the last one.
const saveCache = async (storage: Storage, route: string, snap: Snapshot): Promise<void> => {
  try {
    await pause();
    const value = JSON.stringify(snap);
    await pause();
    await withCache(storage, (db) => db.transaction([
      { sql: 'DELETE FROM snapshot WHERE route != ?', params: [route] },
      { sql: 'INSERT INTO snapshot (route, state) VALUES (?, ?) ON CONFLICT(route) DO UPDATE SET state = excluded.state', params: [route, value] },
    ]));
  } catch {
    // Kept as it was.
  }
};

// The answer of `sync` for the round of `key`. A round whose session read the Mac and then ended gives way to the next
// one soon (`quick`), so a connection that drops comes back within seconds; a round that never reached the Mac waits
// the longer time of `retrying`.
const status = (key = ''): Live => ({ version, pairs, error: lastError, quick: key !== '' && polled === key });

const none: Project = { id: '', name: '', letter: '', face: 0 };
const idle: Turn = { running: false, startedAt: 0, phase: '', detail: '', model: '', queued: 0 };
const emptyChat: Chat = { id: '', kind: 'agent', title: '', project: '', letter: '', face: 0, circle: false, messages: [], turn: idle, toolbar: false, live: false, missing: 0, muted: false };
const rowDefaults: Message = {
  id: '', kind: 'text', mine: false, author: '', at: 0, title: '', body: '', detail: '', failed: false, startsDay: false, newLine: false, sender: '',
  quote: '', quoteAuthor: '', photos: [], card: [],
};

const failed = (error: unknown): string => (error instanceof Error ? error.message : String(error));

// The model and the effort of every chat. A Mac that refuses says why in the chat: for example a Personal budget that
// holds the model.
const choose = async (catalog: Catalog, chatId: string, selection: Selection): Promise<{ ok: boolean }> => {
  const resolved: Selection = { ...selection, reasoning_effort: selection.reasoning_effort ?? entryOf(catalog, selection)?.default_reasoning_effort ?? null };
  if (!useLive) {
    catalog.default = resolved;
    return { ok: true };
  }
  try {
    const answer = (await write('PUT', '/v1/settings/model', selection)) as { model_catalog?: Catalog };
    macCatalog = answer.model_catalog ?? { ...catalog, default: resolved };
    return { ok: true };
  } catch (error) {
    const s = ensure();
    const chat = s.chats.get(chatId);
    if (chat) {
      s.counter += 1;
      const at = newest(chat);
      chat.messages = [...chat.messages, message(`${chat.id}-f${s.counter}`, 'activity', false, Number.isFinite(at) ? at : 0, `The model did not change: ${failed(error)}`)];
    }
    return { ok: false };
  }
};

// MARK: The chat's cards and photos

// The read mark that each chat opened with (UnreadMark): the view counts its openings, and the first source that a new
// opening asks (the chat or its read) takes the mark before the read clears the count.
const newLines = new Map<string, string>();
const opening = new Map<string, number>();
const markOf = (chat: ChatState, opened: number): void => {
  if (opening.get(chat.id) === opened) return;
  opening.set(chat.id, opened);
  newLines.set(chat.id, chat.unread > 0 ? chat.readMark : '');
};

// A card as the chat shows it, with the answer that the phone holds for it.
const cardView = (c: CardData): Card => {
  const holding = answers.get(c.ref);
  const gone = dismissed.has(c.ref) || c.status === 'dismissed';
  const open = c.status === 'pending' && !c.refreshing && !gone;
  const receipt = receiptOf(c, gone, holding?.sending ? holding.label : '');
  const previews = c.previews.map(photoOf);
  return {
    ref: c.ref, title: c.title, sources: c.sources, diff: c.diff, context: c.context, risk: c.risk, reason: c.reason, stale: c.stale, previews,
    previewsReady: previews.every((p) => p.state === 'ready'), drafts: c.draft ? [c.draft] : [],
    options: c.options.map((o) => ({ id: o.id, label: o.label, explanation: o.explanation, recommended: o.recommended, risk: o.risk, reason: o.reason, approve: o.answer === 'approve' })),
    pending: open, refreshing: c.refreshing && !gone, dismissible: open && !holding, chosen: holding?.option ?? '', chosenLabel: holding?.label ?? '',
    waiting: !!holding && !holding.sending, sending: !!holding?.sending, receipt: receipt.label, mark: receipt.mark, dim: receipt.dim,
    outcome: c.status === 'failed' ? c.outcome : '', earlier: c.earlier, revised: c.revised, error: failures.get(c.ref) ?? '',
  };
};

// The card of `ref` in the state that the screens read, and its chat.
const cardIn = (s: State, ref: string): { chat: ChatState; card: CardData } | null => {
  for (const chat of s.chats.values()) {
    for (const m of chat.messages) if (m.card?.ref === ref) return { chat, card: m.card };
  }
  return null;
};

// The files of the Mac that the photos of a chat and the previews of its cards show, the newest first.
const filesOf = (chat: ChatState): string[] => {
  const files: string[] = [];
  for (let i = chat.messages.length - 1; i >= 0; i--) {
    const m = chat.messages[i];
    for (const p of [...m.photos, ...(m.card?.previews ?? [])]) if (p.file !== '' && !files.includes(p.file)) files.push(p.file);
  }
  return files;
};
// The files whose copies this launch looked for, and the photos that it loaded, which the view counts.
const looked = new Set<string>();
let mediaCount = 0;
let tidied = false;

const makeFolder = async (storage: Storage, path: string): Promise<void> => {
  try {
    await storage.fs.mkdir(path);
  } catch (error) {
    if ((error as { code?: string }).code !== 'EEXIST') throw error;
  }
};

// The tile and the viewer's copy of a picture, kept for the file of the Mac that it is.
const keepPhoto = async (storage: Storage, file: string, source: string): Promise<void> => {
  await makeFolder(storage, UPLOADS);
  await makeFolder(storage, PHOTOS);
  const tile = await storage.fs.compressImage(source, `${PHOTOS}/${file}-tile.jpg`, { maxDimension: TILE_SIDE, quality: 0.8 });
  const full = await storage.fs.compressImage(source, `${PHOTOS}/${file}.jpg`, { maxDimension: FULL_SIDE, quality: 0.85 });
  kept.set(file, { tile: tile.path, full: full.path });
};

// The bytes of a file of the Mac (GET /v1/artifacts/<id>/content), which come as they are, not as JSON.
const download = async (path: string): Promise<Uint8Array> => {
  if (!connected()) throw new Error(lastError || 'Your Mac is not connected.');
  const reply = await (tunnel as Tunnel).request('GET', path, headers('GET'));
  if (reply.status !== 200) throw new Error(failureOf(reply));
  return reply.body;
};

// One photo from the Mac into the phone's copies. A connection that ends leaves it missing, so a later answer tries
// again; a file that the Mac refuses or that is no image the phone reads shows that it did not load.
const fetchPhoto = async (storage: Storage, file: string): Promise<void> => {
  let bytes: Uint8Array;
  try {
    bytes = await download(`/v1/artifacts/${file}/content`);
  } catch {
    if (connected()) unloadable.add(file);
    return;
  }
  const source = `${PHOTOS}/${file}.download`;
  try {
    await makeFolder(storage, UPLOADS);
    await makeFolder(storage, PHOTOS);
    await storage.fs.atomicWriteFile(source, bytes);
    await keepPhoto(storage, file, source);
  } catch {
    unloadable.add(file);
  } finally {
    await storage.fs.rm(source).catch(() => undefined);
  }
};

// The copies of photos stay in the scratch folder, which iOS may empty; past 400 files, the first look of a launch
// keeps only the ones of the open chat.
const tidyPhotos = async (storage: Storage, chat: ChatState): Promise<void> => {
  if (tidied) return;
  tidied = true;
  try {
    const names = await storage.fs.readdir(PHOTOS);
    if (names.length <= 400) return;
    const keep = new Set(filesOf(chat));
    for (const name of names) {
      if (!keep.has(name.replace(/(-tile)?\.jpg$/, ''))) await storage.fs.rm(`${PHOTOS}/${name}`).catch(() => undefined);
    }
  } catch {
    // No folder yet.
  }
};

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
  prepare: async ([key], store, storage) => {
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
        if (next.kind === 'token') forget(store, storage);
        throw new Error(next.kind === 'token' ? 'This phone is no longer paired with your Mac.' : 'This pairing code no longer works. Ask for a new one.');
      }
      if (!minted.ok) throw new Error(minted.status === 429 ? 'The relay is busy. Coconut tries again soon.' : `The relay answered ${minted.status}.`);
      const { ticket } = (await minted.json()) as { ticket: string };
      tickets.clear();
      tickets.set(key, { ticket, plan: next });
      return key;
    } catch (error) {
      // The next answer of `sync` carries the reason to the screens, which show it over the chats that the phone kept.
      settle(failed(error));
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
  sync: async ([key, open], store, storage) => {
    // A launch shows the last state that the phone kept until the Mac answers, also without the Mac.
    const paired = loadPairing(store);
    if (!liveState && !latest && cacheTries < 3 && paired) {
      cacheTries += 1;
      try {
        const snap = await readCache(storage, paired.route);
        cacheTries = 3;
        if (snap && !liveState && !latest) {
          liveState = fromSnapshot(snap);
          cached = true;
          version += 1;
        }
      } catch {
        // The app's storage did not answer: the next read tries again, three times in all.
      }
    }
    if (!open || !conn || conn.key !== key || !plan) return status(key);
    try {
      if (!tunnel || tunnelKey !== key || tunnel.session.broken) {
        tunnel = null;
        const c = conn;
        const made = await connect(linkOf(c), plan.keys, plan.macKey, plan.psk);
        // A socket of a newer round took the place of this one meanwhile.
        if (conn !== c) throw new Error('A new connection replaced this one.');
        tunnel = made;
        tunnelKey = key;
      }
      // The tunnel of this round, also when a newer round replaces the module's meanwhile.
      const mac = tunnel;
      if (plan.kind === 'pair') {
        if (!setup) throw new Error('Ask for a new code.');
        const reply = await mac.request('POST', '/v1/pair', { Accept: 'application/json', 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID() },
          utf8(JSON.stringify({ code: setup.code, device_name: 'Coconut on Exact' })));
        if (reply.status !== 200) throw new Error(reply.status === 401 ? 'This pairing code no longer works. Ask for a new one.' : failureOf(reply));
        const made = JSON.parse(text(reply.body)) as { token: string; device_id: string };
        const kept: Pairing = {
          route: plan.route, macKey: toBase64Url(plan.macKey), macName: setup.name, deviceId: made.device_id, token: made.token,
          client: clientToken(mac.session.handshakeHash), privateKey: toBase64Url(plan.keys.privateKey), publicKey: toBase64Url(plan.keys.publicKey),
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
          forget(store, storage);
          throw new Error('This phone is no longer paired with your Mac.');
        }
        if (reply.status !== 200) throw new Error(failureOf(reply));
        return jsonOf(reply);
      };
      const read = { 'Accept-Encoding': 'gzip', 'X-Coconut-State': 'recent', 'X-Coconut-Work': 'recent', 'X-Coconut-Updates': '4' };
      const base = latest;
      let next: Snapshot | null;
      let page: Page | null = null;
      if (!base) {
        next = (await decoded(await mac.request('GET', '/v1/state', headers('GET', read)))) as Snapshot;
      } else {
        const wait = polled === key ? 20 : 0;
        page = (await decoded(await mac.request('GET', `/v1/updates?after=${base.cursor}&wait=${wait}`, headers('GET', read), undefined, LONG_POLL))) as Page;
        // A page that does not follow the state drops it, and the next read takes the whole state again.
        next = latest === base ? applying(page, base) : null;
      }
      latest = next;
      polled = key;
      if (next && (!page || page.reset || page.cursor !== page.after)) {
        liveState = fromSnapshot(next);
        cached = false;
        unsaved = true;
        version += 1;
      }
      // The phone keeps the state at most every 10 seconds of the Mac's clock while changes stream, and what is left once
      // a read brings no change, as the Swift app writes snapshot.json.
      const at = next ? parse(next.server_time) : settingsAt;
      if (next && paired && unsaved && (at - cachedAt >= CACHE_EVERY || (page !== null && page.cursor === page.after))) {
        unsaved = false;
        cachedAt = at;
        await saveCache(storage, paired.route, next);
      }
      // The models of the Mac come with the first state, again when they changed, and at most once a minute.
      if (next && (!macCatalog || page?.settings_changed || page?.reset || at - settingsAt >= MINUTE)) await readSettings(mac, at);
      settle('');
      return status(key);
    } catch (error) {
      settle(failed(error));
      return status(key);
    }
  },

  loadHome: ([zero, chosen, live]): Home => {
    useLive = live;
    launchAt = zero;
    const s = ensure();
    const list = ordered(s);
    const at = zeroOf(s, zero);
    // A state from the cache says so until the Mac answers.
    const banner = !live ? '' : lastError !== '' ? lastError : liveState && !cached ? '' : `Connecting to ${pairing?.macName ?? 'your Mac'}…`;
    return {
      target: list.find((p) => p.id === chosen) ?? list[0] ?? none,
      projects: list,
      attention: attention(s),
      // The chats with an unread message first, then the newest (the person, 2026-11-25: "Put unread chats at top").
      chats: Array.from(s.chats.values()).map((c) => rowOf(c, at))
        .sort((a, b) => Number(b.unread > 0) - Number(a.unread > 0) || b.at - a.at || (a.id < b.id ? -1 : 1)),
      live,
      banner,
    };
  },

  loadChat: ([zero, id, live, _version, offset, opened, _media]): Chat => {
    useLive = live;
    launchAt = zero;
    const s = ensure();
    const chat = s.chats.get(id);
    if (!chat) return emptyChat;
    markOf(chat, opened);
    const at = zeroOf(s, zero);
    const agent = chat.kind === 'agent';
    // The day of a time in the zone of the person: a row of a new day shows the day above it (DaySeparatorRow).
    const dayOf = (ms: number): number => Math.floor((ms + offset * MINUTE) / DAY);
    const mark = newLines.get(chat.id) ?? '';
    const lines: Message[] = [];
    let missing = 0;
    let lastDay = Number.NaN;
    let read = false;
    let previous: Msg | null = null;
    for (const m of chat.messages) {
      const time = at + m.at;
      const day = dayOf(time);
      const photos = m.photos.map(photoOf);
      const card = m.card ? [cardView(m.card)] : [];
      missing += photos.filter((p) => p.state === 'loading').length + card.reduce((n, c) => n + c.previews.filter((p) => p.state === 'loading').length, 0);
      // Photos that one agent sent one after another on one day are one row (TranscriptEntry.grouped).
      const last = lines[lines.length - 1];
      if (m.kind === 'photos' && previous?.kind === 'photos' && previous.from === m.from && last && dayOf(last.at) === day) {
        lines[lines.length - 1] = { ...last, photos: [...last.photos, ...photos] };
      } else {
        lines.push({
          id: m.id, kind: m.kind, mine: m.mine, author: m.author, at: time, title: m.title, body: m.body, detail: m.detail, failed: m.failed,
          startsDay: day !== lastDay, newLine: read, sender: m.sender, quote: m.quote, quoteAuthor: m.quoteAuthor, photos, card,
        });
        read = false;
      }
      // The New line goes above the first row after the message that the person read last (UnreadMark.firstNewRow).
      if (m.id === mark) read = true;
      lastDay = day;
      previous = m;
    }
    // The working line is the last row of a chat whose agent works, with the model the chat runs with.
    if (chat.run) lines.push({ ...rowDefaults, id: 'working', kind: 'status' });
    const turn: Turn = chat.run
      ? { running: true, startedAt: at + chat.run.startedAt, phase: chat.run.phase, detail: chat.run.detail, model: modelOfChat(chat.id), queued: chat.run.queued }
      : idle;
    return {
      id: chat.id, kind: chat.kind, title: chat.label, project: chat.projectName, letter: chat.letter, face: chat.face, circle: chat.circle,
      messages: lines, turn, toolbar: agent, live, missing, muted: chat.muted === true,
    };
  },

  // The photos of the open chat that the phone has yet to keep, one from the Mac in each answer, the newest first, as
  // the chat opens at its newest row. A copy that the phone kept on an earlier launch counts without a download. A file
  // that is no image the phone can read, or that the Mac does not send, shows that it did not load. The view asks again
  // while photos are missing and the Mac is connected.
  loadMedia: async ([id, live], _store, storage) => {
    useLive = live;
    const chat = ensure().chats.get(id);
    if (!chat || !useLive || !connected()) return { n: mediaCount, missing: 0 };
    await tidyPhotos(storage, chat);
    const wanted = filesOf(chat).filter((file) => !kept.has(file) && !unloadable.has(file));
    for (const file of wanted) {
      if (looked.has(file)) continue;
      looked.add(file);
      const tile = `${PHOTOS}/${file}-tile.jpg`;
      const full = `${PHOTOS}/${file}.jpg`;
      try {
        await storage.fs.stat(tile);
        await storage.fs.stat(full);
        kept.set(file, { tile, full });
      } catch {
        // Not kept yet.
      }
    }
    const next = wanted.find((file) => !kept.has(file));
    if (next) await fetchPhoto(storage, next);
    mediaCount += 1;
    return { n: mediaCount, missing: filesOf(chat).filter((file) => !kept.has(file) && !unloadable.has(file)).length };
  },

  // The slash menu over the message box (ComposerSlash.suggestions): the commands that start with what follows the
  // slash, then the models or the efforts of the command. Without a catalog of the Mac yet it has no models.
  suggest: ([id, draft, live]) => {
    useLive = live;
    const chat = ensure().chats.get(id);
    if (!chat || chat.kind !== 'agent' || !draft.startsWith('/') || draft.includes('\n')) return [];
    const body = draft.slice(1);
    const space = body.indexOf(' ');
    if (space < 0) {
      const head = body.toLowerCase();
      return COMMANDS.filter((c) => c.name.startsWith(head)).map((c) => ({
        id: c.name, kind: 'complete', value: `/${c.name} `, title: `/${c.name}`, detail: c.detail, symbol: c.symbol, chosen: false,
      }));
    }
    const catalog = catalogNow();
    if (!catalog) return [];
    const head = body.slice(0, space).toLowerCase();
    const argument = body.slice(space + 1).trim().toLowerCase();
    const current = catalog.default;
    if (head === 'model') {
      return catalog.models.filter((m) => argument === '' || m.name.toLowerCase().includes(argument)).slice(0, 6).map((m, i) => ({
        id: `model-${i}`, kind: 'model', value: keyOf(m), title: m.name, detail: m.provider, symbol: 'symbol:sf/cpu',
        chosen: m.id === current.model && m.provider === current.provider,
      }));
    }
    if (head === 'effort') {
      const entry = entryOf(catalog, current);
      const chosen = current.reasoning_effort ?? entry?.default_reasoning_effort ?? '';
      return (entry?.reasoning_efforts ?? []).filter((e) => argument === '' || e.toLowerCase().startsWith(argument)).map((e) => ({
        id: `effort-${e}`, kind: 'effort', value: e, title: effortName(e), detail: 'Reasoning effort', symbol: 'symbol:sf/brain', chosen: e === chosen,
      }));
    }
    return [];
  },

  // Opening a chat reads it: its unread count goes, and the notices of a team are seen. The New line keeps the read mark
  // that the chat opened with.
  markRead: async ([id, opened]) => {
    const s = ensure();
    const chat = s.chats.get(id);
    if (!chat) return { ok: false };
    markOf(chat, opened);
    // A photo that did not load gets another try.
    unloadable.clear();
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
    const firstAt = localAt(s, at);
    const title = words.length > 48 ? `${words.slice(0, 47).trimEnd()}…` : words;
    s.chats.set(id, {
      id, kind: 'agent', project: project.id, projectName: project.name, title: `${project.name} · ${title}`, label: title,
      letter: project.letter, face: project.face, circle: false, messages: [message(`${id}-1`, 'text', true, firstAt, words)],
      run: { startedAt: firstAt, phase: PHASES.thinking, detail: '', queued: 0 }, unread: 0, readMark: '', health: '',
    });
    return { id };
  },

  // A message of a chat shows at once, with the files that were attached, and `sendMessage` then sends it: the words, a
  // row of the photos, which show from the phone's copies, and a row for each other file (in the demo their names close
  // the words as a line of code). A chat of the Mac keeps its rows in `outgoing` until the Mac's state has them.
  queueMessage: ([chatId, words, files, photos, at]) => {
    const s = ensure();
    const chat = s.chats.get(chatId);
    if (!chat || (words === '' && files.length === 0) || unsent.has(chatId)) return { id: '' };
    const pictures = files.filter((f) => f.type.toLowerCase().startsWith('image/') || IMAGE_NAME.test(f.name));
    const others = files.filter((f) => !pictures.includes(f));
    const photoRow = (id: string, time: number): Msg[] => (pictures.length === 0 ? [] : [message(id, 'photos', true, time, '', {
      photos: pictures.map((f) => ({ key: `url:${f.path}`, file: '', url: f.path, caption: '', width: f.width ?? 0, height: f.height ?? 0 })),
      from: 'user',
    })]);
    if (!useLive) {
      s.counter += 1;
      unsent.set(chatId, { words, files, photos, at, send: null });
      const names = others.map((f) => f.name).join(', ');
      const body = [words, names === '' ? '' : '`' + names + '`'].filter((part) => part !== '').join('\n\n');
      chat.messages = [...chat.messages, ...(body === '' ? [] : [message(`${chat.id}-m${s.counter}`, 'text', true, at, body)]), ...photoRow(`${chat.id}-p${s.counter}`, at)];
      return { id: chatId };
    }
    sends += 1;
    const n = sends;
    const sentAt = launchAt + at;
    const textOf = words || ATTACHMENT_ONLY;
    const send: Outgoing = {
      rows: [
        ...(words === '' ? [] : [message(`out-${n}`, 'text', true, sentAt, words)]),
        ...photoRow(`out-${n}-p`, sentAt),
        ...others.map((f, i) => message(`out-${n}-${i}`, 'artifact', true, sentAt, bytesText(f.size), { title: f.name })),
      ],
      text: textOf,
      before: new Set((latest?.messages ?? []).filter((m) => m.bot_id === chatId && m.role === 'user' && m.text.trim() === textOf).map((m) => m.id)),
      went: false, states: 0, failed: false,
    };
    unsent.set(chatId, { words, files, photos, at, send });
    // The next send of a chat takes the place of a failure.
    outgoing.set(chatId, [...(outgoing.get(chatId) ?? []).filter((o) => !o.failed), send]);
    showOutgoing(chatId);
    return { id: chatId };
  },

  // The message that `queueMessage` showed goes. On the Mac its files go up first, and the message names them; files
  // alone go with ATTACHMENT_ONLY. A failure stays under the rows of the message. The agent of the chat starts a turn,
  // or, when one runs, the message waits for the next.
  sendMessage: async ([chatId], _store, storage) => {
    const send = unsent.get(chatId);
    unsent.delete(chatId);
    if (!send) return { ok: false };
    if (useLive) {
      const mine = send.send;
      try {
        if (send.files.length > MOST_FILES) throw new Error(`A message takes at most ${MOST_FILES} files.`);
        const ids = await upload(storage, chatId, send.words, send.files, send.photos);
        await write('POST', `/v1/bots/${chatId}/messages`, { text: send.words || ATTACHMENT_ONLY, mode: 'auto', attachment_ids: ids, recipient_bot_ids: [] });
      } catch (error) {
        if (mine) {
          mine.failed = true;
          mine.rows = [...mine.rows, message(`${mine.rows[0]?.id ?? 'out'}-f`, 'activity', false, launchAt + send.at, `Not sent: ${failed(error)}`)];
        }
        showOutgoing(chatId);
        return { ok: false };
      }
      // The Mac has the message. A state that came while it went can show it already: then its rows go now, and
      // otherwise with the next state that shows it.
      if (mine) {
        mine.went = true;
        if (latest && shows(latest, chatId, mine)) outgoing.set(chatId, (outgoing.get(chatId) ?? []).filter((o) => o !== mine));
        showOutgoing(chatId);
      }
    }
    const s = ensure();
    const chat = s.chats.get(chatId);
    if (chat && chat.kind === 'agent') {
      if (chat.run) chat.run = { ...chat.run, queued: chat.run.queued + 1 };
      else chat.run = { startedAt: localAt(s, send.at), phase: PHASES.thinking, detail: '', queued: 0 };
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

  // A choice on a card shows at once, with Undo, and `answerCard` sends it after the wait (AnswerUndoRow): the person
  // can take back a choice made by mistake, and nothing reaches the Mac before then. The reply names this one choice, so
  // a choice made again after an Undo goes once. The demo answers at once, as AppStore.answer does in the preview.
  chooseOption: ([chatId, ref, optionId]) => {
    const s = ensure();
    const found = cardIn(s, ref);
    const option = found?.card.options.find((o) => o.id === optionId);
    if (!found || !option || found.chat.id !== chatId || answers.has(ref) || found.card.status !== 'pending') return { id: '' };
    failures.delete(ref);
    if (!useLive) {
      found.card.status = option.answer === 'reject' ? 'rejected' : found.card.draft ? 'approved' : 'executed';
      found.card.selected = option.label;
      s.waits = s.waits.filter((w) => w.ref !== ref);
      return { id: '' };
    }
    choices += 1;
    const id = `${choices} ${ref}`;
    answers.set(ref, { id, option: option.id, label: option.label, sending: false, undone: false });
    return { id };
  },

  undoAnswer: ([ref]) => {
    const holding = answers.get(ref);
    if (!holding || holding.sending) return { ok: false };
    holding.undone = true;
    answers.delete(ref);
    return { ok: true };
  },

  // The answer that `chooseOption` holds goes once its Undo has passed: on the Mac with the card's revision, and for a
  // card with a draft or previews the hash of what the person saw (AnswerRequest), which the Mac compares before it
  // approves. The chat says why an answer did not go.
  answerCard: async ([_chat, choice]) => {
    const ref = choice.slice(choice.indexOf(' ') + 1);
    const holding = answers.get(ref);
    if (!holding || holding.id !== choice || holding.sending) return { ok: false };
    for (let waited = 0; waited < UNDO_WAIT; waited += 250) {
      await new Promise<void>((resolve) => setTimeout(resolve, 250));
      if (holding.undone || answers.get(ref) !== holding) return { ok: false };
    }
    holding.sending = true;
    const found = cardIn(ensure(), ref);
    const option = found?.card.options.find((o) => o.id === holding.option);
    const decision = decisions.get(ref);
    if (!useLive || !found || !option || !decision) {
      answers.delete(ref);
      return { ok: false };
    }
    const body: Record<string, unknown> = { choice: option.answer, revision: decision.revision, option_id: decision.options.length > 0 ? option.id : null };
    if (decision.draft_hash) body.draft_hash = decision.draft_hash;
    if (decision.previews_hash) body.previews_hash = decision.previews_hash;
    try {
      await write('POST', `/v1/decisions/${ref}/answer`, body);
    } catch (error) {
      answers.delete(ref);
      failures.set(ref, failed(error));
      return { ok: false };
    }
    // The answer stays until a state of the Mac shows the card answered.
    return { ok: true };
  },

  // A dismissal is no answer: the card closes, and the agent goes on without one (the X of DecisionCard).
  dismissCard: async ([chatId, ref]) => {
    const s = ensure();
    const found = cardIn(s, ref);
    if (!found || found.chat.id !== chatId || answers.has(ref)) return { ok: false };
    failures.delete(ref);
    if (useLive) {
      const decision = decisions.get(ref);
      if (!decision) return { ok: false };
      try {
        await write('POST', `/v1/decisions/${ref}/inbox`, { action: 'dismiss', revision: decision.revision });
      } catch {
        failures.set(ref, 'Dismissal not confirmed. Try again.');
        return { ok: false };
      }
      dismissed.add(ref);
      return { ok: true };
    }
    found.card.status = 'dismissed';
    s.waits = s.waits.filter((w) => w.ref !== ref);
    return { ok: true };
  },

  // `/model` and `/effort` change the model and the effort of every chat, as ModelChoice of the Swift app does: on the
  // Mac with PUT /v1/settings/model, in the demo in the fixture's catalog. A new model takes its own default effort.
  setModel: async ([chatId, key]) => {
    const catalog = catalogNow();
    const model = catalog?.models.find((m) => keyOf(m) === key);
    if (!catalog || !model) return { ok: false };
    return choose(catalog, chatId, { model: model.id, provider: model.provider, reasoning_effort: null });
  },

  setEffort: async ([chatId, effort]) => {
    const catalog = catalogNow();
    if (!catalog || !(entryOf(catalog, catalog.default)?.reasoning_efforts ?? []).includes(effort)) return { ok: false };
    return choose(catalog, chatId, { model: catalog.default.model, provider: catalog.default.provider, reasoning_effort: effort });
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
