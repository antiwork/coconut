"""The text of a Hermes sign-in notice in place of its raw name.

Hermes writes its notices with a lookup in its message catalog (agent/i18n.py, `t`). When the catalog does not load,
the lookup returns the bare name of the notice, for example `gateway.errors.no_credentials`, and the chat would show
that name. The lookup keeps a failed load until the process ends or its language cache is reset (agent/i18n.py,
`_load_catalog`).

Coconut carries a text for the two sign-in notices only, because a person cannot answer the raw
name of these notices. A wider rule would change real replies: a config key, a web address or a file name can have the
same form as the name of a notice.
"""

from __future__ import annotations

import logging

LOG = logging.getLogger(__name__)

# The text for each name. The first sentence is the English text of Hermes (locales/en.yaml, the same in Hermes 0.22.3
# to 0.23.1). The rest differs from Hermes, because a Coconut chat does not accept `/login` and `/model`: the adapter
# sends chat text with `allow_gateway_control=False` (hermes_plugin/coconut/adapter.py), and gateway_bridge.COMMANDS
# does not have them.
NOTICES = {
    'gateway.errors.no_credentials': (
        "⚠️ I couldn't connect to the AI model service, so this message wasn't processed. On the Mac of this agent, "
        "run `hermes doctor` to see the cause. The agent can need a new sign-in for its model, or a different model."),
    'gateway.background.no_credentials': (
        "❌ The background task couldn't start because no AI model sign-in is configured. On the Mac of this agent, "
        "run `hermes doctor` to see the cause."),
}


def readable_text(text: str) -> str:
    """Return the text of a gateway message for the chat.

    When the first paragraph is only the name of a sign-in notice, the text of the notice replaces that paragraph. The
    spaces around the name go with it. Each character after the first paragraph stays. Each other text comes back as
    the same object.
    """
    if not isinstance(text, str):
        return text
    first, gap, rest = text.partition('\n\n')
    name = first.strip()
    if name not in NOTICES:
        return text
    LOG.warning('Hermes sent the raw name of the notice %s in place of its text. A restart of the Hermes gateway '
                'can load its message catalog again.', name)
    return NOTICES[name] + gap + rest
