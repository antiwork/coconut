"""The secrets of this Mac in its team rooms: one private file for each room, below <data>/team.

`<data>/team/<room ID>.json` holds what only this Mac may know about a room (docs/team-log.md, "Keys"):

- `member_key`: the private member key (Ed25519), which signs each entry of this Mac.
- `agreement_key`: the private agreement key (X25519), which receives the room key after a key change.
- `token`: the member token, which authorizes this Mac at the relay.
- `keys`: the room key of each epoch that this Mac has, by the number of the epoch. A key change adds one (add_key).

The keys are standard base64, and the token is lowercase hex, as the helper takes them (team_helper.py). The folder
has mode 0700 and each file mode 0600, as <data>/tunnel. The store (team_store.py) holds everything else about a room.
No function here puts a value of a file into an error.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile

ROOM_ID = re.compile(r'[a-f0-9]{64}')


class TeamKeysError(Exception):
    """The key file of a room is missing, is not private, or does not have the form above."""


def directory(root) -> Path:
    """<data>/team with mode 0700."""
    path = Path(root) / 'team'
    if path.is_symlink():
        raise TeamKeysError('The team folder must not be a symbolic link.')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def key_file(root, room: str) -> Path:
    if not isinstance(room, str) or not ROOM_ID.fullmatch(room):
        raise TeamKeysError('This is not a room ID.')
    return directory(root) / f'{room}.json'


def read(root, room: str) -> dict:
    path = key_file(root, room)
    try:
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise TeamKeysError('The key file of the room must be private to this Mac account.')
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise TeamKeysError('This Mac has no usable key file for the room.') from None
    keys = value.get('keys') if isinstance(value, dict) else None
    if (not isinstance(keys, dict)
            or not all(isinstance(value.get(name), str) for name in ('member_key', 'agreement_key', 'token'))
            or not all(epoch.isdecimal() and isinstance(key, str) for epoch, key in keys.items())):
        raise TeamKeysError('This Mac has no usable key file for the room.')
    return value


def write(root, room: str, *, member_key: str, agreement_key: str, token: str, keys: dict) -> None:
    """Replace the key file of the room in one step, so a stop in the middle leaves the earlier file."""
    path = key_file(root, room)
    value = dict(version=1, room=room, member_key=member_key, agreement_key=agreement_key, token=token,
                 keys={str(epoch): key for epoch, key in keys.items()})
    # mkstemp makes the file with mode 0600.
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.room-')
    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def add_key(root, room: str, epoch: int, key: str) -> dict:
    """Add the room key of an epoch to the key file of the room, in one step, and return the new content. The caller
    holds the lock of the room, so no other write of the file comes between the read and the write."""
    value = read(root, room)
    write(root, room, member_key=value['member_key'], agreement_key=value['agreement_key'], token=value['token'],
          keys=dict(value['keys'], **{str(epoch): key}))
    return read(root, room)


def remove(root, room: str) -> None:
    key_file(root, room).unlink(missing_ok=True)
