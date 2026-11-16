"""The icon of a new agent: an image that the image backend of Hermes makes from the agent's name and role.

A new agent shows its fruit face at once. A thread then asks agent_icon_worker.py, in the agent's Hermes profile, for
an icon. The service keeps the image as an attachment of the agent and sets `avatar_image` to its ID, so the apps
show it in place of the fruit (`coconut-file://<id>`). Without an image backend, or after an error, the fruit stays.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from pathlib import Path

LOG = logging.getLogger(__name__)
ICON_LIMIT = 4 * 1024 * 1024
ICON_SECONDS = 180


def wanted(service) -> bool:
    """Only a service whose provider runs Hermes makes icons. Tests and a Mac without Hermes do not."""
    provider = getattr(service, 'provider', None)
    return (os.environ.get('COCONUT_AGENT_ICONS', '1') != '0' and callable(getattr(provider, 'environment', None))
            and isinstance(getattr(provider, 'python', None), (str, Path)))


def start(service, bot_id: str) -> None:
    """Make the icon of a new agent in the background. The agent works with its fruit face meanwhile."""
    if wanted(service):
        threading.Thread(target=make, args=(service, bot_id), name='coconut-agent-icon', daemon=True).start()


def make(service, bot_id: str, run=subprocess.run) -> str | None:
    """Ask Hermes for the icon and keep it. Return the artifact ID of the icon, or None."""
    try:
        with service.lock:
            bot = service._get('bots', bot_id)
        if bot.get('avatar_image') or bot.get('_deleted'):
            return None
        env, cwd = service.provider.environment(bot_id)
        result = run([str(service.provider.python), str(Path(__file__).with_name('agent_icon_worker.py'))],
                     input=json.dumps({'name': bot['name'], 'role': bot.get('role', '')}), text=True,
                     capture_output=True, cwd=cwd, env=env, timeout=ICON_SECONDS)
        lines = (result.stdout or '').strip().splitlines()
        answer = json.loads(lines[-1]) if lines else {}
        if not answer.get('path'):
            LOG.info('No icon for a new agent (%s).', answer.get('error') or 'no answer')
            return None
        path = Path(answer['path'])
        content = path.read_bytes() if path.is_file() and path.stat().st_size <= ICON_LIMIT else b''
        return keep(service, bot_id, content, path.suffix or '.png')
    except Exception as error:
        LOG.warning('No icon for a new agent (%s).', type(error).__name__)
        return None


def keep(service, bot_id: str, content: bytes, suffix: str = '.png') -> str | None:
    """Keep the bytes as the icon of the agent and tell the apps."""
    from .gateway_chat import is_photo_data
    from .service import now, uid
    if not content or not is_photo_data(content):
        return None
    with service.transaction():
        bot = service._get('bots', bot_id)
        if bot.get('_deleted') or bot.get('avatar_image'):
            return None
        artifact = dict(id=uid(), bot_id=bot_id, task_id=None, title='icon' + suffix, kind='file', status='attached',
                        preview=f'{len(content):,} bytes', url=None, created_at=now(), _attachment=True,
                        _bot_photo=True, _icon=True)
        service._put('artifacts', artifact)
        service.store_artifact_content(artifact['id'], content)
        bot['avatar_image'] = artifact['id']
        service._put('bots', bot)
        service._event('bot.updated', service._public(bot), bot_id)
    return artifact['id']
