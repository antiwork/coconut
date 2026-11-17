"""Each agent of a person has a letter from A to Z: its face in the apps, and its name until the person gives another.

The avatar of an agent is `letter.<A-Z>`. No two agents share a letter, so the 26 letters are the 26 agents a person
can have. A chat of an agent (agent_chats.py) shows the letter of its agent and takes no letter of its own. A deleted
agent frees its letter.

A new agent takes the letter that the app asks for when it is free, else the first letter of its name when that is
free, else the first free letter. The color of an agent comes from its letter, so the apps and a team see one color
for one letter. At start, `migrate` gives a letter to each agent without one, for example an agent from a release with
fruit faces: an agent named with one letter keeps that letter first.
"""
from __future__ import annotations

import string

LETTERS = string.ascii_uppercase
PREFIX = 'letter.'
# The fills of the faces with letters (BoardStyle.faceColors in the app). Neighboring letters get different fills.
COLORS = ('7E99D3', '70A779', 'AA79C2', 'F2A088', 'EAA052', 'D76C91', 'B6C493', 'EBC96C')
FULL = 'All 26 letters have an agent. Delete an agent to free its letter.'


def symbol(letter: str) -> str:
    return PREFIX + letter


def letter_of(avatar) -> str | None:
    """The letter of an avatar, or None for another avatar (a fruit, for example)."""
    if isinstance(avatar, str) and avatar.startswith(PREFIX) and avatar[len(PREFIX):] in tuple(LETTERS):
        return avatar[len(PREFIX):]
    return None


def color(letter: str) -> str:
    return COLORS[LETTERS.index(letter) % len(COLORS)]


def initial(name) -> str | None:
    """The first letter of a name, as A to Z, or None. `Émile` gives E."""
    import unicodedata
    for character in unicodedata.normalize('NFKD', str(name or '')).upper():
        if character in LETTERS:
            return character
        if character.isalnum():
            return None
    return None


def agents(bots: list[dict]) -> list[dict]:
    """The bots that hold a letter: each agent, without its further chats and without a deleted agent."""
    return [bot for bot in bots if not bot.get('_deleted') and not bot.get('chat_of')]


def taken(bots: list[dict], *, besides: str | None = None) -> dict[str, dict]:
    """Each letter that an agent holds, with that agent. `besides` leaves out one agent."""
    held = {}
    for bot in agents(bots):
        letter = letter_of(bot.get('avatar'))
        if letter and bot['id'] != besides and letter not in held:
            held[letter] = bot
    return held


def choose(held, wanted: str | None = None, name=None) -> str | None:
    """The letter for a new agent: `wanted` when it is free, else the initial of `name`, else the first free one."""
    for letter in (wanted, initial(name)):
        if letter and letter not in held:
            return letter
    return next((letter for letter in LETTERS if letter not in held), None)


def assign(bot: dict, letter: str) -> None:
    bot['avatar'] = symbol(letter)
    bot['color'] = color(letter)


def migrate(bots: list[dict], name_of=lambda bot: bot.get('name')) -> list[dict]:
    """Give a letter to each agent without its own: an avatar of another kind, or a letter that an older agent holds
    already. Returns the changed bots. An agent named with one letter (\"C\") gets that letter first, then the others in
    the order of creation. With more than 26 agents, the agents after the 26th keep their avatar. `name_of` gives the
    name that the apps show, for example Coconut for the default profile."""
    group = sorted(agents(bots), key=lambda bot: (bot.get('created_at') or '', bot['id']))
    held, changed = {}, []
    for bot in group:
        letter = letter_of(bot.get('avatar'))
        if letter and letter not in held:
            held[letter] = bot
    waiting = [bot for bot in group if not any(holder is bot for holder in held.values())]
    waiting.sort(key=lambda bot: (str(name_of(bot) or '').strip().upper() not in tuple(LETTERS),
                                  bot.get('created_at') or '', bot['id']))
    for bot in waiting:
        letter = choose(held, name=name_of(bot))
        if letter is None:
            break
        held[letter] = bot
        assign(bot, letter)
        changed.append(bot)
    for bot in group:
        letter = letter_of(bot.get('avatar'))
        if letter and bot.get('color') != color(letter):
            bot['color'] = color(letter)
            if not any(other is bot for other in changed):
                changed.append(bot)
    return changed
