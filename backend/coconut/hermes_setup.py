"""Read an existing Hermes installation without changing it, and copy chosen parts into Coconut.

The probe reports names and counts only. It never returns file paths or secret values.
Coconut copies API keys but not OAuth sign-ins: many providers rotate refresh tokens, so two homes
that share one sign-in log each other out. The person signs in again with Coconut's own Hermes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import threading
from .profiles import MAX_PROMPT_FILE_BYTES, ProfileError, _safe, _write, read_regular

PROFILE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
# Model provider keys that Hermes reads. Other keys, such as tool and MCP secrets, stay behind.
MODEL_KEYS = {
    'ANTHROPIC_API_KEY': 'Anthropic', 'ANTHROPIC_TOKEN': 'Anthropic', 'OPENAI_API_KEY': 'OpenAI',
    'OPENROUTER_API_KEY': 'OpenRouter', 'GOOGLE_API_KEY': 'Google', 'GEMINI_API_KEY': 'Gemini',
    'NOUS_API_KEY': 'Nous Portal', 'XAI_API_KEY': 'xAI', 'DEEPSEEK_API_KEY': 'DeepSeek', 'MISTRAL_API_KEY': 'Mistral',
    'GROQ_API_KEY': 'Groq', 'TOGETHER_API_KEY': 'Together', 'FIREWORKS_API_KEY': 'Fireworks', 'KIMI_API_KEY': 'Kimi',
    'MOONSHOT_API_KEY': 'Moonshot', 'GLM_API_KEY': 'GLM', 'ZAI_API_KEY': 'Z.ai', 'MINIMAX_API_KEY': 'MiniMax',
    'DASHSCOPE_API_KEY': 'Qwen', 'NVIDIA_API_KEY': 'NVIDIA', 'CEREBRAS_API_KEY': 'Cerebras',
    'AI_GATEWAY_API_KEY': 'Vercel AI Gateway', 'HF_TOKEN': 'Hugging Face',
}
SIGN_INS = {'anthropic': 'Claude', 'nous': 'Nous Portal', 'openai-codex': 'OpenAI Codex', 'xai-oauth': 'xAI',
            'qwen-oauth': 'Qwen', 'google-gemini-cli': 'Gemini', 'copilot': 'GitHub Copilot'}
MAX_SKILL_FILE_BYTES = 5 * 1024 * 1024
MAX_SKILL_BYTES = 50 * 1024 * 1024
# The probe runs on each GET /v1/setup. Deep or huge folders are not skill libraries.
MAX_SKILL_DEPTH = 4
MAX_SKILL_ENTRIES = 5000
# One skill with more files, or deeper folders, is a dependency tree. Coconut leaves it out.
MAX_SKILL_FILES = 500


def source_home(environ=os.environ) -> Path | None:
    """The account's own Hermes home. A symbolic link or a missing folder means none."""
    candidate = Path(environ.get('COCONUT_HERMES_SOURCE') or Path.home() / '.hermes').expanduser()
    try:
        if candidate.is_symlink() or not candidate.is_dir():
            return None
    except OSError:
        return None
    return candidate.resolve()


def _directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def _text(path: Path, limit: int = MAX_PROMPT_FILE_BYTES) -> str | None:
    try:
        return read_regular(path, path.name, limit, 'The Hermes file').decode('utf-8')
    except (OSError, ProfileError, UnicodeDecodeError):
        return None


def _config(home: Path) -> dict:
    text = _text(home / 'config.yaml')
    if not text:
        return {}
    try:
        # The service runs in the Hermes environment, which provides ruamel.yaml. Tests may use JSON, which is valid YAML.
        from ruamel.yaml import YAML
        value = YAML(typ='safe', pure=True).load(text)
    except ImportError:
        try:
            value = json.loads(text)
        except ValueError:
            return {}
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def profiles(root: Path) -> dict[str, tuple[str, Path]]:
    """The default home and each named profile: {key: (display name, folder)}."""
    found = {}
    if any((root / name).exists() for name in ('SOUL.md', 'memories', 'skills', 'config.yaml')):
        found['default'] = ('Hermes', root)
    folder = root / 'profiles'
    if _directory(folder):
        for child in sorted(folder.iterdir()):
            if PROFILE_NAME.match(child.name) and _directory(child):
                found['profile:' + child.name] = (child.name.replace('-', ' ').replace('_', ' ').title(), child)
    return found


def _skills(home: Path) -> list[Path]:
    folder = home / 'skills'
    if not _directory(folder):
        return []
    found, visited = [], 0
    for path, directories, files in os.walk(folder):
        current = Path(path)
        visited += len(directories) + len(files)
        if visited > MAX_SKILL_ENTRIES:
            break
        if 'SKILL.md' in files and (current / 'SKILL.md').is_file() and not (current / 'SKILL.md').is_symlink():
            found.append(current)
            directories[:] = []
            continue
        depth = len(current.relative_to(folder).parts)
        directories[:] = [name for name in directories
                          if depth < MAX_SKILL_DEPTH and not name.startswith('.') and _directory(current / name)]
    return found


def _memories(home: Path) -> list[tuple[str, str]]:
    folder = home / 'memories'
    if not _directory(folder):
        return []
    found = []
    for path in sorted(folder.glob('*.md')):
        text = _text(path)
        if text and text.strip():
            found.append((path.name, text))
    return found


def _model(config: dict) -> dict | None:
    model = config.get('model')
    if isinstance(model, str) and model.strip():
        return {'model': model.strip()[:200], 'provider': None}
    if isinstance(model, dict) and isinstance(model.get('default'), str) and model['default'].strip():
        provider = model.get('provider')
        return {'model': model['default'].strip()[:200], 'provider': provider[:80] if isinstance(provider, str) else None}
    return None


def summary(home: Path) -> dict:
    soul = _text(home / 'SOUL.md')
    return {'soul': bool(soul and soul.strip()), 'memories': len(_memories(home)), 'skills': len(_skills(home))}


def _env(home: Path) -> tuple[str, dict[str, str]]:
    """The .env text and its model keys as {name: resolved value}. Empty values do not count."""
    text = _text(home / '.env') or ''
    try:
        # Hermes reads .env with python-dotenv. Use the same parser, which also resolves ${NAME} references.
        from dotenv import dotenv_values
        from io import StringIO
        values = dotenv_values(stream=StringIO(text))
    except ImportError:
        values = {}
        for line in text.splitlines():
            name, separator, value = line.strip().removeprefix('export ').partition('=')
            if separator and not name.strip().startswith('#'):
                values[name.strip()] = value.strip().strip('"\'')
    return text, {name: value for name, value in values.items() if name in MODEL_KEYS and value}


def _env_line(name: str, value: str) -> str:
    # Single quotes keep the value literal. A value with a single quote uses escaped double quotes.
    if "'" not in value and '\n' not in value:
        return f"{name}='{value}'"
    escaped = value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
    return f'{name}="{escaped}"'


def _sign_ins(home: Path) -> list[str]:
    names = []
    try:
        data = json.loads(_text(home / 'auth.json') or '{}')
        providers = data.get('providers') if isinstance(data, dict) else None
        if isinstance(providers, dict):
            names += [name for name, value in providers.items() if isinstance(name, str) and value]
    except ValueError:
        pass
    if (home / '.anthropic_oauth.json').is_file():
        names.append('anthropic')
    return sorted(set(name[:80] for name in names))


def _label(name: str) -> str:
    return MODEL_KEYS[name] + (' API key' if name.endswith('_API_KEY') else ' token')


def _sign_in_label(name: str) -> str:
    return SIGN_INS.get(name) or name.removesuffix('-oauth').replace('-', ' ').replace('_', ' ').title()


def probe(root: Path | None) -> dict:
    if root is None:
        return {'found': False, 'profiles': [], 'logins': [], 'sign_ins': [], 'model': None}
    available = profiles(root)
    found = [dict(id=key, name=name, **summary(home)) for key, (name, home) in available.items()]
    _, keys = _env(root)
    return {'found': bool(found), 'profiles': found,
            'logins': [{'id': name, 'label': _label(name)} for name in sorted(keys)],
            'sign_ins': [{'id': name, 'label': _sign_in_label(name)} for name in _sign_ins(root)],
            'model': _model(_config(root)) if 'default' in available else None}


def _copy_file(origin: Path, destination: Path, boundary: Path) -> None:
    content = read_regular(origin, origin.name, MAX_SKILL_FILE_BYTES, 'A Hermes skill file')
    _safe(destination, boundary)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o700 if origin.stat(follow_symlinks=False).st_mode & stat.S_IXUSR else 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(content)


def copy_profile(source: Path, target: Path, boundary: Path) -> list[str]:
    """Copy the personality, memory, and skills of one profile. Settings, sessions, and secrets stay behind.

    Returns the names of skills that Coconut left out: a file is too large or not a regular file,
    or the skill has too many files or folders that are too deep.
    """
    _safe(target, boundary)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    soul = _text(source / 'SOUL.md')
    if soul is not None:
        _write(target / 'SOUL.md', soul, boundary)
    memories = _memories(source)
    if memories:
        (target / 'memories').mkdir(exist_ok=True, mode=0o700)
        for name, text in memories:
            _write(target / 'memories' / name, text, boundary)
    total, skipped = 0, []
    for skill in _skills(source):
        relative = skill.relative_to(source / 'skills')
        files, deep = [], False
        for path, directories, names in os.walk(skill):
            depth = len(Path(path).relative_to(skill).parts)
            deep = deep or (depth >= MAX_SKILL_DEPTH and bool(directories))
            directories[:] = [name for name in directories if depth < MAX_SKILL_DEPTH
                              and not name.startswith('.') and _directory(Path(path) / name)]
            files += [Path(path) / name for name in names if not name.startswith('.')]
            if len(files) > MAX_SKILL_FILES:
                break
        if deep or len(files) > MAX_SKILL_FILES:
            skipped.append(relative.as_posix())
            continue
        sizes = []
        for origin in files:
            info = origin.stat(follow_symlinks=False)
            sizes.append(info.st_size if stat.S_ISREG(info.st_mode) and info.st_size <= MAX_SKILL_FILE_BYTES else None)
        if None in sizes or total + sum(sizes) > MAX_SKILL_BYTES:
            # A partial skill can fail in ways a missing skill cannot.
            skipped.append(relative.as_posix())
            continue
        total += sum(sizes)
        for origin in files:
            _copy_file(origin, target / 'skills' / relative / origin.relative_to(skill), boundary)
    return skipped


def copy_logins(source: Path, target: Path, boundary: Path) -> list[str]:
    """Add API keys that Coconut does not have yet. Existing Coconut keys always win."""
    _, incoming = _env(source)
    existing_text, existing = _env(target)
    added = [name for name in sorted(incoming) if name not in existing]
    if added:
        # A blank placeholder for the same key gives way to the real value.
        kept = [line for line in existing_text.splitlines()
                if line.strip().removeprefix('export ').partition('=')[0].strip() not in added]
        _write(target / '.env', '\n'.join(kept + [_env_line(name, incoming[name]) for name in added]).lstrip('\n') + '\n', boundary)
        (target / '.env').chmod(0o600)
    return added


class HermesSetup:
    """Service routes for the phone's Hermes setup step."""
    MAX_IMPORT_PROFILES = 20
    # One import at a time in this process. Each import also takes the model settings lock for logins.
    setup_lock = threading.Lock()

    def _setup_source(self) -> Path | None:
        root = source_home()
        catalog = getattr(getattr(self, 'provider', None), 'catalog', None)
        own = [Path(self.root).resolve()] + ([Path(catalog.root).resolve()] if catalog is not None else [])
        # Coconut's own Hermes homes are not an outside setup to import.
        if root is None or any(root == path or path in root.parents for path in own):
            return None
        return root

    def _source_profiles(self) -> dict:
        with self.lock:
            return {bot['source_profile']: bot['id'] for bot in self._all('bots')
                    if bot.get('source_profile') and not bot.get('_deleted')}

    def hermes_setup(self) -> dict:
        result = probe(self._setup_source())
        imported = self._source_profiles()
        for item in result['profiles']:
            item['bot_id'] = imported.get(item['id'])
        return result

    def import_hermes_setup(self, data: dict, key: str, fingerprint: str) -> dict:
        from .service import ServiceError, uid
        keys = data.get('profile_ids', []) if isinstance(data, dict) else None
        logins = data.get('logins', False) if isinstance(data, dict) else None
        if (not isinstance(keys, list) or len(keys) > self.MAX_IMPORT_PROFILES
                or not all(isinstance(item, str) for item in keys) or not isinstance(logins, bool)):
            raise ServiceError(400, 'Send profile_ids as a list of profile IDs and logins as true or false.')
        with self.setup_lock:
            previous = self._previous_request(key, fingerprint)
            if previous is not None:
                return previous
            root = self._setup_source()
            if root is None:
                raise ServiceError(404, 'Coconut found no Hermes setup on this Mac.')
            available = profiles(root)
            imported = self._source_profiles()
            bots, skipped = [], []
            try:
                for profile_key in dict.fromkeys(keys):
                    if profile_key in imported:
                        with self.lock:
                            bots.append(self._public(self._get('bots', imported[profile_key])))
                        continue
                    if profile_key not in available:
                        raise ProfileError('That Hermes profile is no longer on the Mac.')
                    name, home = available[profile_key]
                    # Copy before the database lock, so a large skill library does not stall other requests.
                    staging = self.profiles.root / 'imports' / uid()
                    _safe(staging, self.profiles.root)
                    try:
                        skipped += copy_profile(home, staging, self.profiles.root)
                        with self.transaction():
                            bot = self._create_bot({'name': name, 'role': 'Imported from Hermes'}, source_profile=profile_key)
                            target = self.profiles.home(bot)
                            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                            # The deferred profile sync runs after commit and keeps the copied files.
                            os.replace(staging, target)
                    finally:
                        shutil.rmtree(staging, ignore_errors=True)
                    bots.append(self._public(bot))
                copied = self._copy_logins(root) if logins else []
            except (ProfileError, OSError) as error:
                # A retry with a new key resumes: imported profiles return their existing bots.
                raise ServiceError(409, str(error) if isinstance(error, ProfileError) else
                                   'Coconut could not copy the Hermes setup.') from None
            result = {'bots': bots, 'logins': copied, 'skipped_skills': sorted(set(skipped))}
            self._remember_request(key, fingerprint, result)
            return result

    def _copy_logins(self, root: Path) -> list[str]:
        from .service import ServiceError
        catalog = getattr(self.provider, 'catalog', None)
        if catalog is None:
            raise ServiceError(503, 'This Mac has no Coconut model settings.')
        from .model_catalog import native_home
        with self.model_settings_lock:
            Path(catalog.root).mkdir(parents=True, exist_ok=True, mode=0o700)
            copied = copy_logins(root, native_home(catalog.root), Path(catalog.root))
            if copied and hasattr(catalog, '_cache'):
                # The model step reads providers again with the new keys.
                catalog._cache = None
        return copied
