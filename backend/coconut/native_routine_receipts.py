"""Reconcile the handoff from a native cron claim to a Coconut task."""
import fcntl
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path


def receipt_path(home, identity):
    if not identity or Path(identity).name != identity or identity in ('.', '..'):
        raise ValueError('Invalid native routine ID.')
    return Path(home) / 'coconut-runs' / (identity + '.json')


def read(home, identity):
    path = receipt_path(home, identity)
    return json.loads(path.read_text()) if path.exists() else {}


def save(home, identity, value):
    from profiles import _write
    path = receipt_path(home, identity)
    path.parent.mkdir(exist_ok=True, mode=0o700)
    _write(path, json.dumps(value), Path(home))


@contextmanager
def locked(home, identity):
    path = receipt_path(home, identity)
    path.parent.mkdir(exist_ok=True, mode=0o700)
    with path.with_suffix('.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def claim(home, identity, jobs, manual=False):
    with locked(home, identity):
        previous = read(home, identity)
        job = jobs.get_job(identity)
        if previous.get('phase') == 'claimed':
            owner = previous['job']['fire_claim']['by']
            if job and (job.get('fire_claim') or {}).get('by') == owner:
                return {'job': previous['job']}
            raise ValueError('This routine lost its native execution claim. Inspect its last run in Hermes.')
        if previous.get('phase') in ('claiming', 'dispatching'):
            raise ValueError('The routine dispatch result is unknown. Inspect its last run in Hermes before resetting its dispatch receipt.')
        save(home, identity, {'phase': 'claiming'})
        job = jobs.claim_job_for_fire(identity, force=manual, return_job=True)
        if not job:
            save(home, identity, {'phase': 'idle'})
            return {'job': None}
        save(home, identity, {'phase': 'dispatching', 'job': job})
        if not jobs.claim_dispatch(identity):
            save(home, identity, {'phase': 'idle'})
            return {'job': None}
        save(home, identity, {'phase': 'claimed', 'job': job})
        return {'job': job}


def issue(home, job):
    receipt = read(home, job['id'])
    if receipt.get('phase') in ('claiming', 'dispatching'):
        return 'The dispatch result is unknown. Inspect the last run in Hermes.'
    if receipt.get('phase') == 'claimed' and (job.get('fire_claim') or {}).get('by') != receipt['job']['fire_claim']['by']:
        return 'The routine lost its native execution claim. Inspect the last run in Hermes.'
    return None


def completion_id(identity, owner):
    return 'completed-' + hashlib.sha256(json.dumps([identity, owner]).encode()).hexdigest()


def completed(home, identity, owner, jobs, provenance=None):
    """Require native completion provenance, never a task's finished flag alone."""
    expected = dict(job_id=identity, owner=owner, success=True)
    if provenance == dict(expected, native_confirmed=True):
        return True
    if read(home, completion_id(identity, owner)) == expected:
        return True
    job = jobs.get_job(identity) or {}
    outcome = job.get('_coconut_completion') or {}
    if outcome.get('owner') == owner:
        return outcome == expected
    # Legacy native receipts have no outcome. Only the service's persisted
    # successful task history can supply it, bound to this exact occurrence.
    return (provenance == expected and job.get('_coconut_completion_owner') == owner
            and read(home, identity) == dict(phase='finished', owner=owner)
            and (job.get('fire_claim') or {}).get('by') != owner)


def finish(home, identity, jobs, *, owner, success, output='', failure=None):
    with locked(home, identity):
        def confirmed(job):
            if job.get('_coconut_completion') == dict(job_id=identity, owner=owner, success=True):
                # Keep proof for this occurrence before another native fire can replace it.
                save(home, completion_id(identity, owner), dict(job_id=identity, owner=owner, success=True))
            return True

        def complete(all_jobs, index, job):
            if job.get('_coconut_completion_owner') == owner:
                return confirmed(job)
            if (job.get('fire_claim') or {}).get('by') != owner:
                return False
            if output:
                jobs.save_job_output(identity, output)
            stamp = jobs._hermes_now().isoformat()
            # Hermes owns outcome and scheduling. The same native write records our receipt.
            jobs._record_run_outcome(job, success, failure, None, None, stamp)
            jobs._advance_after_run(job, stamp)
            job['_coconut_completion_owner'] = owner
            job['_coconut_completion'] = dict(job_id=identity, owner=owner, success=bool(success))
            jobs.save_jobs(all_jobs)
            return confirmed(job)

        result = jobs._under_fire_fence(identity, lambda: jobs._with_job(identity, complete, False))
        if result:
            save(home, identity, {'phase': 'finished', 'owner': owner})
        return {'ok': bool(result)}


def completion_receipts(home):
    """Read only our exact legacy occurrence format; leave other receipts alone."""
    result = {}
    for path in (Path(home) / 'coconut-runs').glob('completed-*.json'):
        try:
            value = json.loads(path.read_text())
            if (isinstance(value, dict) and isinstance(value.get('job_id'), str)
                    and isinstance(value.get('owner'), str)
                    and value == dict(job_id=value['job_id'], owner=value['owner'], success=True)
                    and path.stem == completion_id(value['job_id'], value['owner'])):
                result[path.stem] = value
                # Bound each provider transfer, not the lifetime of saved proof.
                if len(result) == 128:
                    break
        except (OSError, ValueError):
            continue
    return result


def prune_completions(home, candidates):
    # Candidates were captured before the service task snapshot and migrated in
    # its committed transaction. Never sweep receipts created after that snapshot.
    for identity, value in candidates.items():
        if identity != completion_id(value['job_id'], value['owner']):
            raise ValueError('Invalid completion receipt.')
        with locked(home, value['job_id']):
            if read(home, identity) == value:
                receipt_path(home, identity).unlink(missing_ok=True)
    return {'ok': True}
