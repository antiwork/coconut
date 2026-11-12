"""Call the Swift helper of the team log (scripts/team-helper.swift).

The backend uses the Python standard library only, which has no ChaChaPoly, no X25519 and no Ed25519. The helper seals
and opens entries, the boxes of direct messages and welcomes, signs, and reads and makes invite links, with the format code of ios/Tunnel/TeamLog.swift.
docs/team-log.md is the contract.

Each call starts the helper once, sends one JSON object to its standard input and reads one JSON object from its
standard output. A key, a token or a secret passes only through these pipes. No error of this module carries a value
of a call, so a caller can log each error. The watch mode of the helper, which holds the socket of each room, does not
end: team_socket.py runs it.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess

# Coconut.app carries the helper, and its service login item names it here. A source install compiles the helper
# into <data>/team (scripts/team.py build).
HELPER_ENVIRONMENT = 'COCONUT_TEAM_HELPER'
TIMEOUT_SECONDS = 30
# One answer of the relay holds at most this many entries, so one call opens one answer.
OPEN_BATCH = 256


class TeamHelperError(Exception):
    """The helper is missing, or it did not run to its end."""


class TeamHelperMissing(TeamHelperError):
    """This Mac has no usable helper. An update of Coconut brings one, so the app tells the person to update."""


class TeamFormatError(Exception):
    """The helper refused a value. `code` is malformed, unknown_version, unknown_epoch, seal or signature."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def helper_path(root) -> Path | None:
    """The helper of Coconut.app, else the helper that this install compiled. None without a usable helper."""
    named = os.environ.get(HELPER_ENVIRONMENT)
    if named and os.path.isfile(named) and os.access(named, os.X_OK):
        return Path(named)
    directory = Path(root) / 'team'
    path = directory / 'team-helper'
    # The folder also holds the keys of the rooms. Only this account can write there, so the helper is its own.
    if directory.is_symlink() or path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        return None
    return path


class TeamHelper:
    def __init__(self, root):
        self.root = Path(root)

    @property
    def available(self) -> bool:
        return helper_path(self.root) is not None

    def call(self, operation: str, **fields) -> dict:
        path = helper_path(self.root)
        if path is None:
            raise TeamHelperMissing('This Mac has no team helper. Run python3 scripts/team.py build.')
        answer = None
        try:
            result = subprocess.run([str(path)], input=json.dumps(dict(fields, operation=operation)).encode(),
                                    capture_output=True, timeout=TIMEOUT_SECONDS)
            if result.returncode == 0:
                answer = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        if not isinstance(answer, dict):
            # The input and the output of the helper hold keys, so the error says nothing about them.
            raise TeamHelperError('The team helper did not finish its work.')
        if 'error' in answer:
            raise TeamFormatError(str(answer['error']))
        return answer

    def member(self) -> dict:
        """The keys and the token of a new member: member, member_key, agreement, agreement_key, token."""
        return self.call('member')

    def proof(self, room: str, token: str, member_key: str) -> dict:
        """The proof of a member for the relay, and the hash of its token: proof, hash."""
        return self.call('proof', room=room, token=token, member_key=member_key)

    def seal(self, room: str, entry: str, epoch: int, key: str, member_key: str, payload: bytes) -> str:
        """The sealed body of an entry, as the base64 text that the relay takes. The payload is JSON in UTF-8."""
        return self.call('seal', room=room, entry=entry, epoch=epoch, key=key, member_key=member_key,
                         payload=base64.b64encode(payload).decode())['body']

    def open(self, room: str, keys: dict, entries: list[dict]) -> list[dict]:
        """Open the bodies of entries of the relay. Each result has `epoch` and `payload` (bytes), or `error`."""
        results = []
        for start in range(0, len(entries), OPEN_BATCH):
            batch = [{'id': entry['id'], 'member': entry['member'], 'body': entry['body']}
                     for entry in entries[start:start + OPEN_BATCH]]
            opened = self.call('open', room=room, keys=keys, entries=batch).get('entries')
            if not isinstance(opened, list) or len(opened) != len(batch):
                raise TeamHelperError('The team helper did not finish its work.')
            for result in opened:
                try:
                    if 'error' not in result:
                        result = {'epoch': int(result['epoch']), 'payload': base64.b64decode(result['payload'], validate=True)}
                except (TypeError, KeyError, ValueError):
                    raise TeamHelperError('The team helper did not finish its work.') from None
                results.append(result)
        return results

    def seal_direct(self, room: str, entry: str, agreement_key: str, member: str, other: str, agreement: str,
                    plaintext: bytes) -> str:
        """The box of a direct message of `member` (this Mac) to `other`, as the unpadded base64url text of a payload.
        `agreement` is the public agreement key of `other`."""
        return self.call('seal_direct', room=room, entry=entry, agreement_key=agreement_key, member=member, other=other,
                         agreement=agreement, plaintext=base64.b64encode(plaintext).decode())['box']

    def open_direct(self, room: str, agreement_key: str, member: str, boxes: list[dict]) -> list[dict]:
        """Open the boxes of direct messages with the pair key of this Mac (`member`) and the `other` of each box.
        Each box has `entry`, `writer`, `other`, `agreement` and `box`. Each result has `plaintext` (bytes), or
        `error`."""
        results = []
        for start in range(0, len(boxes), OPEN_BATCH):
            batch = boxes[start:start + OPEN_BATCH]
            opened = self.call('open_direct', room=room, agreement_key=agreement_key, member=member,
                               boxes=batch).get('boxes')
            if not isinstance(opened, list) or len(opened) != len(batch):
                raise TeamHelperError('The team helper did not finish its work.')
            for result in opened:
                try:
                    if 'error' not in result:
                        result = {'plaintext': base64.b64decode(result['plaintext'], validate=True)}
                except (TypeError, KeyError, ValueError):
                    raise TeamHelperError('The team helper did not finish its work.') from None
                results.append(result)
        return results

    def ticket(self, secret: str) -> dict:
        """The relay ticket of an invite secret, and its hash: ticket, hash."""
        return self.call('ticket', secret=secret)

    def seal_welcome(self, room: str, secret: str, epoch: int, key: str, admin: str, name: str) -> str:
        return self.call('seal_welcome', room=room, secret=secret, epoch=epoch, key=key, admin=admin, name=name)['welcome']

    def open_welcome(self, room: str, secret: str, welcome: str) -> dict:
        """What the admin sealed for a new member: epoch, key, admin, name."""
        return self.call('open_welcome', room=room, secret=secret, welcome=welcome)

    def link(self, room: str, secret: str, expires: int, relay: str, name: str) -> str:
        return self.call('link', room=room, secret=secret, expires=expires, relay=relay, name=name)['link']

    def invite(self, link: str) -> dict:
        """What an invite link carries: room, secret, expires, relay, name."""
        return self.call('invite', link=link)
