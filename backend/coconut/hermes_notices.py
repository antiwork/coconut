"""Clear words in place of the raw name of a Hermes notice.

Hermes writes its notices with a lookup in its message catalog (agent/i18n.py, `t`). When the catalog does not load,
the lookup returns the bare name of the notice, for example `gateway.errors.no_credentials`, and the chat would show
that name. The service gives each text of the gateway to readable_text before the text becomes a message of a chat.
"""

from __future__ import annotations

import logging
import re

LOG = logging.getLogger(__name__)

# A paragraph that is only a name of two to seven parts. Each part starts with a letter, so a version number is no name.
RAW_NAME = re.compile(r'[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,6}')
# A blank line between two paragraphs. The group keeps the break, so a text without a raw name stays the same.
PARAGRAPH_BREAK = re.compile(r'(\n[ \t]*\n\s*)')

NO_CREDENTIALS = frozenset({'gateway.errors.no_credentials', 'gateway.background.no_credentials'})
NO_CREDENTIALS_TEXT = (
    'I could not connect to the AI model, so this message was not run. The sign-in for the model on the Mac is '
    'missing or no longer valid. On the Mac, run `hermes setup` to sign in again, or `hermes doctor` to see the '
    'cause. Then send the message again.')
MISSING_TEXT = 'Hermes reported a problem, but the text of its notice is missing on the Mac (`{name}`). ' \
               'On the Mac, run `hermes doctor`.'
# The row of Hermes that closes a failed turn (agent/turn_failure_copy.py, FAILED_TURN_NOTICE).
FAILED_TURN_NOTICE = 'Your request was not processed. Send it again if you still want me to carry it out.'


def readable_text(text: str) -> str:
    """Return the text of a gateway message for the chat.

    A paragraph that is only the name of a Hermes notice becomes clear words. After the sign-in notice, the function
    removes the closing row of a failed turn, because the new words already tell when to send again. A text without a
    raw name comes back unchanged.
    """
    if not isinstance(text, str) or '.' not in text:
        return text
    pieces = PARAGRAPH_BREAK.split(text)
    paragraphs, breaks = pieces[0::2], [''] + pieces[1::2]
    names = [paragraph.strip() for paragraph in paragraphs if RAW_NAME.fullmatch(paragraph.strip())]
    if not names:
        return text
    LOG.warning('Hermes sent the raw name of a notice in place of its text: %s.', ', '.join(names))
    result, credentials = [], False
    for paragraph, gap in zip(paragraphs, breaks):
        name = paragraph.strip()
        if credentials and name == FAILED_TURN_NOTICE:
            continue
        if RAW_NAME.fullmatch(name):
            credentials = credentials or name in NO_CREDENTIALS
            paragraph = NO_CREDENTIALS_TEXT if name in NO_CREDENTIALS else MISSING_TEXT.format(name=name)
        result.append((gap if result else '') + paragraph)
    return ''.join(result)
