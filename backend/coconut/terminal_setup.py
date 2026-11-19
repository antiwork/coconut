"""The PATH of the service and of the commands that it starts: package tools come before system tools."""
from pathlib import Path


def command_path(value: str) -> str:
    preferred = [p for p in ('/opt/homebrew/bin', '/opt/homebrew/sbin', '/usr/local/bin', '/usr/local/sbin',
                             str(Path.home() / '.orbstack/bin')) if Path(p).is_dir()]
    return ':'.join(dict.fromkeys([*preferred, *(p for p in value.split(':') if p), '/usr/bin', '/bin', '/usr/sbin', '/sbin']))
