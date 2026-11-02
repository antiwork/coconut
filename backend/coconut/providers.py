"""Execution adapters. The service owns decisions, permissions, and file writes.

With a Hermes home, the Hermes gateway runs each turn (gateway_chat). HermesProvider reads and writes the settings,
models, profiles and routines of Hermes. FixtureProvider runs each turn of fixture mode and the tests."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from copy import deepcopy
from pathlib import Path
from uuid import UUID

class FixtureProvider:
    """Deterministic service fixture. This class never runs Hermes or a model."""

    name = 'fixture'
    ready = True
    model = 'Deterministic fixture'

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).expanduser().resolve()

    def configure(self, *, model: str, base_url: str | None = None):
        pass

    def start_task(self, task, bot, history, emit):
        prompt = next((m.get('text', '') for m in reversed(history) if m.get('role') == 'user' and m.get('task_id') == task['id']), task.get('title', ''))
        if self._team_milestone(task, bot, prompt, emit):
            return
        if re.search(r'^\s*(create|write|draft|prepare)\b', prompt, re.I):
            match = re.search(r'\b([\w-]+\.(?:md|txt|json|csv))\b', prompt)
            filename = match.group(1) if match else 'morning-note.md'
            emit('decision', self.proposal(filename, '# Morning review\n\nThe independent check is complete. Review this proposed file before saving it.\n'))
            return
        text = 'Fixture: 17 × 19 = 323. The check is complete.'
        emit('text_delta', {'text': text})
        emit('completed', {'summary': 'Complete.'})

    @staticmethod
    def _team_milestone(task, bot, prompt, emit):
        from .native_conversation import explicit_bot_messages
        incoming = explicit_bot_messages(task)
        message = incoming[-1] if incoming else None
        text = message['text'] if message else prompt
        if text.startswith('TEAM_MILESTONE_RESULT:'):
            emit('text_delta', {'text': 'Fixture: Lin reports that 17 × 19 = 323.'})
            emit('completed', {'summary': 'Fixture: the team result is ready.'})
            return True
        if not text.startswith(('TEAM_MILESTONE_HANDOFF:', 'TEAM_MILESTONE_REQUEST:')):
            return False
        directory = [peer for peer in task.get('_available_bots', [])
                     if isinstance(peer, dict) and peer.get('id') != bot.get('id')]
        if text.startswith('TEAM_MILESTONE_REQUEST:') and message:
            target = message['sender_bot_id']
            shared = 'TEAM_MILESTONE_RESULT: 17 multiplied by 19 equals 323.'
        else:
            target = next((peer['id'] for peer in directory if peer['name'] == 'Lin'), None)
            if target is None:
                raise ValueError('The fixture milestone needs an available bot named Lin.')
            shared = 'TEAM_MILESTONE_REQUEST: What is 17 multiplied by 19? Send the result to this message sender once. Do not acknowledge the result.'
        receipt = emit('bot_message', {'target_bot_id': target, 'text': shared})
        status = receipt.get('status', 'unconfirmed') if isinstance(receipt, dict) else 'unconfirmed'
        emit('completed', {'summary': 'Fixture: message delivery is ' + status + '.'})
        return True

    @staticmethod
    def proposal(filename, content):
        """A file card with three choices, in the shape that the service records."""
        options = [
            {'id': 'full', 'label': 'Full note', 'explanation': 'Keep all proposed details.', 'answer': 'approve',
             'action': {'type': 'create_file', 'filename': filename, 'content': content}},
            {'id': 'brief', 'label': 'Brief note', 'explanation': 'Keep one short review note.', 'answer': 'approve',
             'action': {'type': 'create_file', 'filename': filename, 'content': 'Morning review is ready for human review.\n'}},
            {'id': 'skip', 'label': 'Do not save', 'explanation': 'Finish without creating this file.', 'answer': 'reject'},
        ]
        return {
            'title': 'Review the morning note',
            'explanation': 'Choose the full note or a brief version before the service saves it.',
            'action_type': 'create_file', 'resource': filename,
            'consequences': 'An approved choice creates its exact file in Coconut storage. Reject saves nothing. '
                            'Modify requests a new proposal.',
            'recommended': 'approve', 'predicted_choice': 'approve',
            'recommended_option_id': 'full', 'predicted_option_id': 'full',
            'options': options, 'action': options[0]['action'],
        }

    def resume_task(self, task, bot, history, decision, emit):
        if decision['status'] == 'replied':
            emit('text_delta', {'text': 'Fixture reply: I received your message. No action ran.'})
            emit('completed', {'summary': 'Reply handled.'})
            return
        if decision['status'] == 'modified':
            action = decision['action']
            content = action['content'] + '\nRequested change: ' + (decision.get('correction') or '') + '\n'
            emit('decision', self.proposal(action['filename'], content))
            return
        if decision['status'] != 'executed':
            raise RuntimeError('The service has not confirmed execution.')
        emit('text_delta', {'text': 'The file is ready. Its execution receipt prevents a repeated write.'})
        emit('completed', {'summary': 'Ready to review.'})

    def answer_context(self, task, bot, history, question, emit):
        proposal = task.get('pending_decision', {})
        resource = proposal.get('resource', 'the proposed file')
        emit('text_delta', {'text': f'Fixture context: approval saves {resource} in Coconut storage. It does not send or publish it. The decision remains pending.'})

    def cancel_task(self, task_id):
        pass



# The Hermes gateway runs each turn of a service with a Hermes home (gateway_chat). Coconut has no worker of its own.
GATEWAY_ONLY = 'The Hermes gateway runs each turn. Start Coconut with the Hermes home of its bots.'


class HermesProvider(FixtureProvider):
    """The settings, models, profiles and routines of the person's Hermes. The Hermes gateway runs each turn."""

    name = 'hermes'
    supports_native_outcomes = True
    # Coconut's Kanban board runs in a Kanban runtime of this Hermes (kanban_scheduler.KanbanScheduler).
    native_board = True

    def __init__(self, data_dir, *, python=None, hermes_root=None, home=None, model=None, base_url=None, catalog=None):
        super().__init__(data_dir)
        from .hermes_runtime import DEFAULT_HOME, configured
        from .model_catalog import MISSING_SOURCE, HermesModelCatalog
        # An explicit source keeps the shared home beside it, as the tests lay it out. See HermesModelCatalog.
        if home is None:
            home = Path(hermes_root).parent / 'terminal' if hermes_root is not None else DEFAULT_HOME
        python, hermes_root = configured(python, hermes_root)
        self.python = str(python or '')
        self.hermes_root = Path(hermes_root or MISSING_SOURCE).resolve()
        self.catalog = catalog or HermesModelCatalog(self.data_dir, python=self.python, source=self.hermes_root, home=home)
        if model:
            self.catalog.migrate_legacy_default(model, base_url)
        self._profile_settings_lock = threading.Lock()
        self._profile_settings: dict[str, tuple] = {}
        self._routines_lock = threading.Lock()
        self._routines: dict[str, tuple] = {}

    @property
    def ready(self):
        return bool(self.model_catalog().get('default_configured'))

    @property
    def model(self):
        return self.model_catalog()['default'].get('model') or ''

    @property
    def base_url(self):
        return self.model_catalog()['default'].get('base_url')

    def model_catalog(self):
        try:
            return self.catalog.get()
        except ValueError as error:
            return {'default': {'model': '', 'provider': '', 'base_url': None, 'reasoning_effort': None},
                    'models': [], 'available': False, 'default_configured': False,
                    'model_source': 'Hermes', 'default_source': 'Hermes config.yaml', 'error': str(error)}

    def refresh_model_catalog(self, after):
        """Ask for the live model lists of the providers, at most once in `after` seconds."""
        self.catalog.refresh_if_old(after)

    def validation_model_catalog(self):
        return self.catalog.get_for_validation()

    def resolve_model(self, bot, catalog=None):
        if bot.get('_model_error'):
            raise ValueError(bot['_model_error'])
        if bot.get('model_override') is not None and bot.get('_native_effective_model'):
            return dict(bot['_native_effective_model'])
        catalog = self.model_catalog() if catalog is None else catalog
        if catalog.get('error'):
            raise ValueError(catalog['error'])
        return self.catalog.resolve(bot.get('model_override'), catalog=catalog)

    def native_settings(self, operation='read', bots=(), **payload):
        if operation == 'routine_create':
            bot = next(bot for bot in bots if bot['id'] == payload['bot_id'])
            selection = self.resolve_model(self.observe_bot(bot))
            payload = dict(payload, changes=dict(payload['changes'],
                           model=selection['model'], provider=selection['provider'],
                           reasoning_effort=selection.get('reasoning_effort')))
        # A change needs the profile folder of each bot. A read also lists a bot whose folder is missing.
        homes = {bot['id']: str(self.catalog._profile_home(bot['id'])) for bot in bots}
        if operation not in ('read', 'routines_read', 'snapshot_read'):
            for home in homes.values():
                if not Path(home).is_dir():
                    raise RuntimeError(f'The Hermes profile folder {home} is missing. Restore it, or remove the bot from Coconut.')
        return self.catalog._call('native', native_operation=operation, homes=homes, **payload)

    def maintain_sessions(self, bot, conversations, outcomes, retention_days, *, allow_vacuum=False):
        """Record session tips, end the native sessions of idle outcomes, and prune Coconut sessions for one bot."""
        env, cwd = self.environment(bot['id'])
        from .session_maintenance import SESSION_HISTORY_DAYS
        request = dict(bot=bot, conversations=conversations, outcomes=outcomes, retention_days=retention_days,
                       history_days=SESSION_HISTORY_DAYS, allow_vacuum=allow_vacuum)
        # A first VACUUM of a large session store can take minutes. Hermes throttles later runs.
        result = subprocess.run([self.python, str(Path(__file__).with_name('native_sessions.py'))],
                                input=json.dumps(request), text=True, capture_output=True,
                                cwd=cwd, env=env, timeout=1800, check=True)
        return json.loads(result.stdout)

    def set_bot_settings(self, bot, changes):
        return self.native_settings('update_profile', [bot], bot_id=bot['id'], changes=changes)

    def observe_bot(self, bot):
        return self.observe_bots([bot])[bot['id']]

    def forget_profile_settings(self, bots):
        """Drop the saved settings of every bot that is not in this complete list, such as a deleted bot."""
        keep = {bot['id'] for bot in bots}
        with self._profile_settings_lock:
            for identity in [identity for identity in self._profile_settings if identity not in keep]:
                del self._profile_settings[identity]

    def observe_state(self, bots):
        self.forget_profile_settings(bots)
        settings = self.native_settings('snapshot_read', bots)
        return self.observe_bots(bots, settings=settings['profiles']), settings

    def observe_bots(self, bots, *, settings=None):
        native = self.catalog.read_profiles([bot['id'] for bot in bots])
        if settings is None:
            settings = self.profile_settings(bots)
        result = {}
        for bot in bots:
            observed = dict(bot)
            values = native.get(bot['id'], {})
            observed['model_override'] = values.get('model_override')
            if bot.get('model_follows_default'):
                # The profile holds a copy of the default model. The app shows the bot on the default model.
                observed['model_override'] = None
            elif values.get('effective_model'):
                observed['_native_effective_model'] = values['effective_model']
            if values.get('error'):
                observed['_model_error'] = values['error']
            observed.update(settings.get(bot['id'], {}))
            result[bot['id']] = observed
        return result

    def profile_settings(self, bots):
        """Read the native profile settings of these bots. Each turn reads them, and each read starts a Hermes process.

        A saved result stays valid while the files that profile_state reads are unchanged (see profile_signature).
        """
        signature = getattr(self.catalog, 'profile_signature', None)
        if signature is None:
            return self.native_settings(bots=bots)['profiles']
        try:
            signatures = {bot['id']: signature(bot['id']) for bot in bots}
        except (OSError, ValueError):
            return self.native_settings(bots=bots)['profiles']
        with self._profile_settings_lock:
            result = {}
            for identity, current in signatures.items():
                cached = self._profile_settings.get(identity)
                if cached and cached[0] == current:
                    result[identity] = deepcopy(cached[1])
        missing = [bot for bot in bots if bot['id'] not in result]
        if missing:
            values = self.native_settings(bots=missing)['profiles']
            with self._profile_settings_lock:
                for bot in missing:
                    if bot['id'] in values:
                        self._profile_settings[bot['id']] = (signatures[bot['id']], deepcopy(values[bot['id']]))
            result.update(values)
        return result

    def native_routines(self, bots):
        """Read the native routines of these bots. The routine tick reads them every 15 seconds.

        Each read starts a Hermes process. A saved result of a bot stays valid while the files that the read uses are
        unchanged (see routines_signature).
        """
        signature = getattr(self.catalog, 'routines_signature', None)
        if signature is None:
            return self.native_settings('routines_read', bots)
        try:
            # Take the signatures before the read. A file that changes during the read changes the next signature.
            signatures = {bot['id']: signature(bot['id']) for bot in bots}
        except (OSError, ValueError):
            return self.native_settings('routines_read', bots)
        with self._routines_lock:
            for identity in self._routines.keys() - signatures.keys():
                # A deleted bot is not read again.
                del self._routines[identity]
            saved = {identity: deepcopy(self._routines[identity][1]) for identity, current in signatures.items()
                     if identity in self._routines and self._routines[identity][0] == current}
        missing = [bot for bot in bots if bot['id'] not in saved]
        if missing:
            snapshot = self.native_settings('routines_read', missing)
            with self._routines_lock:
                for bot in missing:
                    value = dict(routines=[r for r in snapshot['routines'] if r['bot_id'] == bot['id']])
                    self._routines[bot['id']] = (signatures[bot['id']], deepcopy(value))
                    saved[bot['id']] = value
        # The read returns the routines of each bot in the order of the bots.
        return {'routines': [routine for bot in bots for routine in saved[bot['id']]['routines']]}

    def set_bot_model(self, bot, selection):
        return self.catalog.set_profile(bot['id'], selection)

    def follow_default_model(self, bot_ids, classify=()):
        return self.catalog.follow_default(bot_ids, classify)

    def set_model_default(self, selection):
        return self.catalog.set_default(selection)

    def subscribe_model_catalog(self, callback):
        self.catalog.on_change = callback

    def migrate_model_default(self, model, base_url):
        self.catalog.migrate_legacy_default(model, base_url)

    def configure(self, *, model, base_url=None):
        catalog = self.model_catalog()
        matches = [row for row in catalog['models'] if row['id'] == model and (base_url is None or row.get('base_url') == base_url)]
        if len(matches) != 1:
            raise ValueError('Choose one model and provider from the Hermes catalog.')
        self.set_model_default({'model': model, 'provider': matches[0]['provider'], 'reasoning_effort': None})

    def environment(self, bot_id):
        UUID(bot_id)
        root = self.data_dir / 'bots' / bot_id
        from .profiles import HermesProfiles
        # A process of the bot runs in the bot's Hermes profile, so Coconut, `hermes -p <name>` and the gateway share its
        # skills, memories, config and cron jobs. Coconut's own files stay in <data>/bots/<uuid>.
        home = HermesProfiles(self.data_dir, getattr(self, 'profile_rows', None)).existing_home({'id': bot_id})
        for name in ('workspace', 'browser', 'tmp'):
            (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        # Homebrew manages one Mac installation, so its trust must have one scope.
        shared_config = self.data_dir / 'shared'
        shared_config.mkdir(exist_ok=True, mode=0o700)
        # Native tool configuration stays in the bot profile. HOME remains the OS account home.
        env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY') if k in os.environ}
        from .runtime_environment import login_folders, prepare_profile
        env.update(prepare_profile(home))
        # A bot that keeps its own logins sets their folders in its .env. Hermes loads that file, and every other
        # process for this bot (work sources and session checks) must use the same logins.
        env.update(login_folders(home))
        env['COCONUT_GOOGLE_HOME'] = str(self.data_dir / 'connections' / 'google' / 'home')
        env.update(COCONUT_DATA_DIR=str(self.data_dir), COCONUT_BOT_ID=bot_id, COCONUT_BOT_DIR=str(root))
        env.update(HERMES_HOME=str(home), TMPDIR=str(root / 'tmp'), PYTHONPATH=str(self.hermes_root), PYTHONUNBUFFERED='1', COCONUT_BROWSER_PROFILE=str(root / 'browser'), HERMES_DISABLE_TELEMETRY='1', HOMEBREW_XDG_CONFIG_HOME=str(shared_config))
        from .hermes_runtime import NO_LAZY_INSTALLS
        env.update(NO_LAZY_INSTALLS)
        # Hermes finds its tool store from the source folder, which the Hermes home of the official installer owns.
        # The bot's terminal `hermes` installs tools into <HERMES_HOME>/tools, so a process of the bot reads that store too.
        env['HERMES_RUNTIME_DIR'] = str(home / 'tools')
        env.update(HERMES_KANBAN_DB=str(self.data_dir / 'kanban/kanban.db'),
                   HERMES_KANBAN_BOARD='default', HERMES_TENANT=bot_id,
                   HERMES_KANBAN_WORKSPACES_ROOT=str(self.data_dir / 'kanban/kanban/workspaces'))
        return env, root / 'workspace'

    def start_task(self, task, bot, history, emit):
        raise RuntimeError(GATEWAY_ONLY)

    def resume_task(self, task, bot, history, decision, emit):
        raise RuntimeError(GATEWAY_ONLY)

    def answer_context(self, task, bot, history, question, emit):
        raise RuntimeError(GATEWAY_ONLY)
