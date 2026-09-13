"""Install Coconut's Google CLI without changing system packages."""
import fcntl
import hashlib
import io
import os
from pathlib import Path
import platform
import tarfile
import tempfile
from urllib.request import urlopen

try:
    from .profiles import _safe
except ImportError:
    from profiles import _safe

VERSION = '0.39.1'
BASE = 'https://github.com/openclaw/gogcli/releases/download/v' + VERSION + '/'


def download(name, maximum):
    with urlopen(BASE + name, timeout=15) as response:
        content = response.read(maximum + 1)
    if len(content) > maximum:
        raise ValueError('The Google CLI download is too large.')
    return content


def install(root):
    root = Path(root)
    _safe(root, root.parent)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = root / 'gog'
    _safe(target, root)
    fd = os.open(root / 'install.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if target.is_file():
            return str(target)
        system = {'Darwin': 'darwin', 'Linux': 'linux'}.get(platform.system())
        arch = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'amd64'}.get(platform.machine())
        if not system or not arch:
            raise ValueError('Google tools do not support this computer.')
        name = f'gogcli_{VERSION}_{system}_{arch}.tar.gz'
        checksums = download('checksums.txt', 32768).decode().splitlines()
        expected = next((line.split()[0] for line in checksums if len(line.split()) == 2 and line.split()[1].lstrip('*') == name), None)
        archive = download(name, 80 * 1024 * 1024)
        if not expected or hashlib.sha256(archive).hexdigest() != expected:
            raise ValueError('The Google CLI download did not pass its integrity check.')
        with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as package:
            matches = [entry for entry in package.getmembers() if entry.isfile() and Path(entry.name).name == 'gog']
            if len(matches) != 1 or matches[0].size > 160 * 1024 * 1024:
                raise ValueError('The Google CLI archive is invalid.')
            source = package.extractfile(matches[0])
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=root, delete=False) as file:
                    temporary = Path(file.name)
                    while block := source.read(1024 * 1024):
                        file.write(block)
                    file.flush(); os.fsync(file.fileno())
                temporary.chmod(0o700)
                os.replace(temporary, target)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
        return str(target)
    finally:
        os.close(fd)
