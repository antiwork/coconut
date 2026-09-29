"""Keep each conversation's newest session recorded, end idle outcome sessions, and prune Coconut's old sessions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
if __package__:
    from .native_conversation import IDLE_END_REASON, TIP_KEY, resume_session, session_root
else:
    from native_conversation import IDLE_END_REASON, TIP_KEY, resume_session, session_root

VACUUM_KEY = 'coconut.last-vacuum'
VACUUM_INTERVAL_SECONDS = 7 * 86400
# Hermes vacuums only above this free-page fraction. Below it, a rewrite costs more than it returns.
VACUUM_MIN_FREELIST_RATIO = 0.25
# The search indexes of a store fragment as the store grows and loses old sessions, and they keep their pages. On the mini,
# one store held 4.5 GB with 16% of its pages free. Hermes merged the indexes and vacuumed it to 0.95 GB in 150 seconds.
# So a store above this size also gets a VACUUM when it is 1.5 times as large as it was after the last one.
VACUUM_SIZE_KEY = 'coconut.vacuum-size'
VACUUM_MIN_BYTES = 512 * 1024 ** 2
VACUUM_REGROWTH = 1.5
# A VACUUM writes a new copy of the store. Without this much free disk it could fill the disk.
VACUUM_DISK_RESERVE_BYTES = 2 * 1024 ** 3


def freelist_ratio(path):
    from contextlib import closing
    import sqlite3
    # Close this reader before the holder checks and the VACUUM. as_uri() escapes the path.
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as connection:
        pages = connection.execute('PRAGMA page_count').fetchone()[0]
        free = connection.execute('PRAGMA freelist_count').fetchone()[0]
    return free / pages if pages else 0.0


def store_bytes(path):
    from contextlib import closing
    import sqlite3
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as connection:
        return connection.execute('PRAGMA page_count').fetchone()[0] * connection.execute('PRAGMA page_size').fetchone()[0]


def vacuum_pays(db):
    """Return True when a VACUUM of this store frees enough to be worth its exclusive lock, and the disk can hold it."""
    import shutil
    size = store_bytes(db.db_path)
    if shutil.disk_usage(Path(db.db_path).parent).free < size + VACUUM_DISK_RESERVE_BYTES:
        return False
    if freelist_ratio(db.db_path) > VACUUM_MIN_FREELIST_RATIO:
        return True
    after_last = db.get_meta(VACUUM_SIZE_KEY)
    return size >= VACUUM_MIN_BYTES and (not after_last or size >= VACUUM_REGROWTH * float(after_last))


def maintain(request):
    """Run the session maintenance of one bot with Hermes SessionDB methods only.

    1. Record the newest session of every conversation root. A prune of ended compression ancestors cuts the
       chain from the root, so this must happen before any prune. A tip row is about 100 bytes, and Hermes has no
       public call to delete one, so a row stays after Hermes prunes its conversation. resume_session() ignores it.
    2. End the newest session of each idle outcome. Coconut never ends a chat.
    3. Prune ended idle work sessions inactive for `retention_days`, and other ended Coconut sessions, such as
       compressed chat sessions, inactive for `history_days`. Other sources keep the profile's own settings.
    4. VACUUM at most once a week when it pays (see `vacuum_pays`: more than a quarter of the file is free, or the
       store is large and has regrown), the service reports the bot quiet, and no other process holds the store.
       That test, not this run's prune count, decides, so a VACUUM that a holder deferred runs on a later hour.
    """
    from hermes_state import SessionDB
    from hermes_state_holders import foreign_state_db_holders, in_process_state_db_holders

    db = SessionDB()
    result = dict(recorded=0, ended=0, pruned=0, vacuumed=False)
    try:
        for item in request['conversations']:
            root = session_root(request['bot'], execution=item.get('execution_session_id'), epoch=item.get('epoch'))
            tip = resume_session(db, root)
            if db.get_session(tip) and db.get_meta(TIP_KEY + root) != tip:
                db.set_meta(TIP_KEY + root, tip)
                result['recorded'] += 1
        for item in request['outcomes']:
            root = session_root(request['bot'], execution=item['execution_session_id'], epoch=item.get('epoch'))
            session_id = resume_session(db, root)
            session = db.get_session(session_id)
            if session and db.get_meta(TIP_KEY + root) != session_id:
                # An idle outcome can be older than the tip window. Record its tip before the prune too.
                db.set_meta(TIP_KEY + root, session_id)
                result['recorded'] += 1
            if session and not session.get('ended_at'):
                db.end_session(session_id, IDLE_END_REASON)
                result['ended'] += 1
        sessions_dir = Path(os.environ['HERMES_HOME']) / 'sessions'
        # Idle work goes first. Chat history, including compressed chat sessions, stays searchable longer.
        result['pruned'] = db.prune_sessions(older_than_days=float(request['retention_days']), source='coconut',
                                             end_reason=IDLE_END_REASON, sessions_dir=sessions_dir,
                                             exclude_active_write_guards=True)
        result['pruned'] += db.prune_sessions(older_than_days=float(request.get('history_days', request['retention_days'])),
                                              source='coconut', sessions_dir=sessions_dir, exclude_active_write_guards=True)
        last = db.get_meta(VACUUM_KEY)
        due = not last or time.time() - float(last) >= VACUUM_INTERVAL_SECONDS
        # VACUUM rewrites the whole file under an exclusive lock. Hermes skips it while another process
        # holds the store or a WAL sidecar, and so does Coconut. The next hour tries again.
        if (request.get('allow_vacuum') and due
                and vacuum_pays(db)
                and not (foreign_state_db_holders(db.db_path) + in_process_state_db_holders(db.db_path, exclude=db))):
            try:
                db.vacuum()
            except Exception as error:
                # The VACUUM is optional. Keep the tips, ends, and prune of this run, and retry next hour.
                result['vacuum_error'] = str(error)[:500]
            else:
                db.set_meta(VACUUM_KEY, str(time.time()))
                db.set_meta(VACUUM_SIZE_KEY, str(store_bytes(db.db_path)))
                result['vacuumed'] = True
    finally:
        db.close()
    return result


if __name__ == '__main__':
    request = json.loads(sys.stdin.read())
    output = sys.stdout
    sys.stdout = sys.stderr
    output.write(json.dumps(maintain(request)) + '\n')
