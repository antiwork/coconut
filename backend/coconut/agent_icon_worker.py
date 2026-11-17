"""Make the icon of a new agent with the image backend of Hermes. The service runs this file with the Python of Hermes
in the profile of the agent (agent_icons.py), so it uses the keys of that profile and never prints one.

Input on stdin: {"name", "role"}. Output on stdout: {"path"} of a PNG or JPEG, or {"error"}.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# The fruit faces of Coconut, as one sprite sheet: the style of every agent. They are inspiration, never the answer.
SPRITES = Path(__file__).with_name('assets') / 'agent-sprites.png'
PROMPT = ('A new character for an AI agent called "{name}"{role}, in exactly the style of the attached sprite sheet: a '
          'flat, friendly character with the same small dot eyes, simple smile, soft muted colors, gentle shapes and '
          'no outlines. Do not copy any character of the sheet. Invent a new one whose shape or props show what this '
          'agent does. One character, centered, on a plain white background, square. No text, no letters, no logos.')


def provider():
    """The image backend that Hermes has: the one that its settings choose, else OpenAI when the profile has a key.
    The person asked Coconut to make an icon for each new agent, so a key alone is enough here."""
    from agent.image_gen_registry import get_active_provider
    active = get_active_provider()
    if active is not None:
        return active
    try:
        from plugins.image_gen.openai import OpenAIImageGenProvider
        candidate = OpenAIImageGenProvider()
        return candidate if candidate.is_available() else None
    except Exception:
        return None


def main() -> None:
    request = json.loads(sys.stdin.read() or '{}')
    try:
        from hermes_cli.env_loader import load_hermes_dotenv
        load_hermes_dotenv()
    except Exception:
        pass
    backend = provider()
    if backend is None:
        print(json.dumps({'error': 'no_backend'}))
        return
    role = str(request.get('role') or '').strip()
    prompt = PROMPT.format(name=str(request.get('name') or 'Agent')[:80], role=f', which {role[:160]}' if role else '')
    references = [str(SPRITES)] if SPRITES.is_file() else []
    try:
        result = backend.generate(prompt, aspect_ratio='square', reference_image_urls=references)
    except TypeError:
        # A backend without reference images still gets the style in words.
        result = backend.generate(prompt, aspect_ratio='square')
    image = result.get('image') if isinstance(result, dict) else None
    if not result.get('success') or not isinstance(image, str) or image.startswith(('http://', 'https://')):
        print(json.dumps({'error': str(result.get('error_type') or 'failed')[:80]}))
        return
    print(json.dumps({'path': image}))


if __name__ == '__main__':
    main()
