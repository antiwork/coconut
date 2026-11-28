"""The unread count of an agent chat, from a read mark. Callers hold the service transaction.

A chat keeps the ID of the newest message that the person read (`last_read_message_id`; the app draws its "New" line
below that message). The unread messages are the replies after the mark that the chat shows as replies: no interim
message of a running turn, no hidden or deleted message, no reaction. A count from the messages stays true when a
streaming reply becomes an interim message, an interim message becomes the final reply, or a reply is withdrawn,
which a counter that only went up and down did not: each interim message of a turn counted as an unread reply.

A chat goes unread only once its turn is fully done (HOLD): while a run of the chat works, the count stops at the
messages from before the run, and the replies of the run count when it ends.
"""

from .service_base import now

UNREAD = ("json_extract(data,'$.bot_id')=? AND json_extract(data,'$.role')='assistant' "
          "AND COALESCE(json_extract(data,'$._hidden'),0)=0 AND COALESCE(json_extract(data,'$._deleted'),0)=0 "
          "AND COALESCE(json_extract(data,'$._interim'),0)=0 AND json_extract(data,'$.reaction_event') IS NULL")
MARK = 'last_read_message_id'
# The time when the mark last moved. A chat goes to Archived a time after its newest activity (14 hours unless the person
# changed the setting), and a read of a new reply is activity (agent_chats.archive_idle_chats), so a chat that the person
# reads late stays that long again.
READ_AT = '_read_at'
# The person, 2026-11-28: "Only mark unread once a turn is fully done." While a run of the chat works, its count stops at
# this rowid, the newest message when the run started, so the chat does not go unread with the first words of a
# streaming reply and again with each artifact. When no run works, the hold goes and the replies of the turn count at
# once. A reply that was unread before the run, for example the one above a card that the person approved, still
# counts. Service._refresh_bot sets and clears the hold.
HOLD = '_unread_hold'


def is_read(method: str, path: str) -> bool:
    """True for a read: POST /v1/read (every chat) and POST /v1/bots/<id>/read (one chat)."""
    parts = path.strip('/').split('/')
    return method == 'POST' and (parts == ['v1', 'read'] or (len(parts) == 4 and parts[:2] == ['v1', 'bots']
                                                             and parts[3] == 'read'))


def _count(db, bot_id: str, after: int, upto: int | None = None) -> int:
    if upto is None:
        return db.execute(f'SELECT COUNT(*) FROM messages WHERE {UNREAD} AND rowid>?', (bot_id, after)).fetchone()[0]
    return db.execute(f'SELECT COUNT(*) FROM messages WHERE {UNREAD} AND rowid>? AND rowid<=?',
                      (bot_id, after, upto)).fetchone()[0]


def _newest(db, bot_id: str):
    # A visible message, so the app finds the mark in its rows and can draw the line below it.
    return db.execute("SELECT rowid, id FROM messages WHERE json_extract(data,'$.bot_id')=? "
                      "AND COALESCE(json_extract(data,'$._hidden'),0)=0 AND COALESCE(json_extract(data,'$._deleted'),0)=0 "
                      "ORDER BY rowid DESC LIMIT 1", (bot_id,)).fetchone()


def ensure_mark(db, bot: dict):
    """Give a chat from before read marks its mark: after the last message of the person, and never more unread
    replies than the old counter showed. A chat that showed none is read through its newest message.

    Service._message calls this before it stores a new message, so a new reply never lands under the first mark."""
    if MARK in bot:
        return
    stored = max(0, int(bot.get('unread_count') or 0))
    if stored == 0:
        row = _newest(db, bot['id'])
    else:
        row = db.execute("SELECT rowid, id FROM messages WHERE json_extract(data,'$.bot_id')=? "
                         "AND json_extract(data,'$.role')='user' ORDER BY rowid DESC LIMIT 1", (bot['id'],)).fetchone()
        if _count(db, bot['id'], row[0] if row else 0) > stored:
            row = db.execute(f'SELECT rowid, id FROM messages WHERE {UNREAD} ORDER BY rowid DESC LIMIT 1 OFFSET ?',
                             (bot['id'], stored)).fetchone()
    bot[MARK] = row[1] if row else None


def _mark_row(db, bot: dict) -> int:
    if not bot.get(MARK):
        return 0
    row = db.execute('SELECT rowid FROM messages WHERE id=?', (bot[MARK],)).fetchone()
    return row[0] if row else 0


def hold(db, bot: dict, running: bool) -> bool:
    """Hold the count of the chat from the start of a run until no run works (HOLD). Returns whether the stored row
    changed. Call recount after it."""
    if running == (HOLD in bot):
        return False
    if running:
        bot[HOLD] = db.execute('SELECT COALESCE(MAX(rowid), 0) FROM messages').fetchone()[0]
    else:
        del bot[HOLD]
    return True


def recount(db, bot: dict) -> bool:
    """Set the unread count of the chat from its read mark, up to its hold while a run works. Returns whether the
    stored row changed."""
    before = (bot.get('unread_count'), bot.get(MARK, ...))
    ensure_mark(db, bot)
    bot['unread_count'] = _count(db, bot['id'], _mark_row(db, bot), bot.get(HOLD))
    return before != (bot['unread_count'], bot[MARK])


def read_through(db, bot: dict, message_id: str) -> bool:
    """True when the person read the chat through this message: its read mark is at the message or after it."""
    row = db.execute('SELECT rowid FROM messages WHERE id=?', (message_id,)).fetchone()
    return row is not None and bool(bot.get(MARK)) and _mark_row(db, bot) >= row[0]


def named(db, bot: dict, data) -> str | None:
    """The message that a read from the app names (`message_id`), when it is a visible message of this chat. The app
    names the newest message that it shows whole during a run, so a reply that the agent still wrote when the person
    left stays unread. None reads through the newest message: a read without a name, as from an app before it, or a
    name that the chat does not show."""
    message_id = data.get('message_id') if isinstance(data, dict) else None
    if not isinstance(message_id, str) or not message_id:
        return None
    row = db.execute("SELECT id FROM messages WHERE id=? AND json_extract(data,'$.bot_id')=? "
                     "AND COALESCE(json_extract(data,'$._hidden'),0)=0 AND COALESCE(json_extract(data,'$._deleted'),0)=0",
                     (message_id, bot['id'])).fetchone()
    return row[0] if row else None


def mark_read(db, bot: dict, message_id: str | None = None):
    """Read the chat through one message, or through its newest message. A mark never moves back."""
    ensure_mark(db, bot)
    row = db.execute('SELECT rowid, id FROM messages WHERE id=?', (message_id,)).fetchone() if message_id \
        else _newest(db, bot['id'])
    if row and row[0] > _mark_row(db, bot):
        bot[MARK] = row[1]
        bot[READ_AT] = now()
    recount(db, bot)
