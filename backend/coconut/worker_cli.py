"""Forward CLI results while saving progress outside redirected stdout."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

from worker_activity import event_phase, progress_path


def run(identity, engine, output_format, argv):
    path = progress_path(identity)

    def save(phase):
        # Persist only a fixed phase and time. Prompts and tool output remain in native logs.
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'phase': phase, 'at': time.time()}))
            temporary.chmod(0o600)
            os.replace(temporary, path)
        except OSError:
            # A failed progress write must not interrupt approved work.
            pass

    save('starting')
    try:
        child = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
    except OSError as error:
        save('failed')
        print(str(error), file=sys.stderr)
        return 127
    def stop(signum, frame):
        child.send_signal(signum)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    final_message = None
    try:
        for line in child.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                # Preserve diagnostics even if the CLI does not follow its stream contract.
                sys.stdout.write(line)
                sys.stdout.flush()
                continue
            phase = event_phase(engine, event)
            if phase:
                save(phase)
            if output_format == 'stream-json':
                sys.stdout.write(line)
            elif engine == 'claude' and isinstance(event, dict) and event.get('type') == 'result':
                if output_format == 'json':
                    sys.stdout.write(json.dumps(event) + '\n')
                else:
                    if event.get('is_error'):
                        print(json.dumps(event), file=sys.stderr)
                    if 'result' in event:
                        sys.stdout.write(str(event['result']) + '\n')
            elif engine == 'codex' and isinstance(event, dict) and event.get('type') == 'item.completed':
                item = event.get('item') or {}
                if item.get('type') == 'agent_message':
                    if final_message is not None:
                        print(final_message, file=sys.stderr)
                    final_message = item.get('text', '')
            elif engine == 'codex' and isinstance(event, dict) and event.get('type') == 'turn.completed':
                if final_message is not None:
                    print(final_message)
                    final_message = None
            elif engine == 'codex' and isinstance(event, dict) and event.get('type') in ('error', 'turn.failed', 'thread.started'):
                print(json.dumps(event), file=sys.stderr)
            sys.stdout.flush()
        if final_message is not None:
            print(final_message, file=sys.stderr)
        code = child.wait()
        save('finished' if code == 0 else 'failed')
        return code if code >= 0 else 128 - code
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait()


if __name__ == '__main__':
    sys.exit(run(*sys.argv[1:4], sys.argv[4:]))
