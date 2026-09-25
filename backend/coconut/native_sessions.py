"""End the native sessions of idle outcomes, then let Hermes prune its own session store."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
if __package__:
    from .native_conversation import IDLE_END_REASON, resume_session, session_root
else:
    from native_conversation import IDLE_END_REASON, resume_session, session_root


def maintain(request):
    """End the newest session of each idle outcome, and run Hermes session maintenance.

    Hermes deletes only ended sessions that were inactive for `retention_days`, and it runs at most once a day.
    Coconut never ends a bot's chat, so its conversation stays open. A turn reopens an ended outcome session.
    """
    from hermes_state import SessionDB

    db = SessionDB()
    ended = 0
    try:
        for item in request['outcomes']:
            root = session_root(request['bot'], execution=item['execution_session_id'], epoch=item.get('epoch'))
            session_id = resume_session(db, root)
            session = db.get_session(session_id)
            if session and not session.get('ended_at'):
                db.end_session(session_id, IDLE_END_REASON)
                ended += 1
        result = db.maybe_auto_prune_and_vacuum(retention_days=int(request['retention_days']),
                                                sessions_dir=Path(os.environ['HERMES_HOME']) / 'sessions')
    finally:
        db.close()
    return dict(ended=ended, pruned=result.get('pruned', 0), closed=result.get('closed', 0),
                vacuumed=bool(result.get('vacuumed')), skipped=bool(result.get('skipped')), error=result.get('error'))


if __name__ == '__main__':
    request = json.loads(sys.stdin.read())
    output = sys.stdout
    sys.stdout = sys.stderr
    output.write(json.dumps(maintain(request)) + '\n')
