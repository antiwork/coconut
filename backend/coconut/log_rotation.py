"""Rotate the log files that launchd opens for the service."""
import os
import stat
import sys

# launchd opens StandardOutPath and StandardErrorPath once, before the service starts, and never rotates them.
# Above this size, the service moves a log to `<name>.1` and continues in a new file. Two files stay at most.
LOG_LIMIT_BYTES = 50 * 1024 * 1024


def rotate_log(fd, path, limit=LOG_LIMIT_BYTES):
    """Rotate `path` when the descriptor `fd` writes to it and the file is larger than `limit`.

    The same inode proves that the service owns the file. In a terminal or a test, the descriptor writes elsewhere,
    and nothing changes. A child process that inherited the old descriptor continues to write to `<name>.1`.
    """
    path = str(path)
    try:
        opened, current = os.fstat(fd), os.stat(path)
    except OSError:
        return False
    if (not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or opened.st_size <= limit):
        return False
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.fileno() == fd:
                stream.flush()
        except (AttributeError, OSError, ValueError):
            pass
    os.replace(path, path + '.1')
    replacement = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, stat.S_IMODE(opened.st_mode))
    try:
        os.dup2(replacement, fd)
    finally:
        os.close(replacement)
    return True
