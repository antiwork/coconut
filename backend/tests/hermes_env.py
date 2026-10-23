"""The installed Hermes that the native tests run against.

COCONUT_HERMES_ROOT and COCONUT_HERMES_PYTHON name the Hermes source folder and its Python. Without them, the tests use
the Hermes that hermes_runtime finds, but only when this test process runs under the Python of that Hermes. The native
tests import Hermes modules, and another Python does not have their dependencies.

Without a Hermes, the native tests skip. Native CI sets COCONUT_REQUIRE_NATIVE. Then a missing Hermes stops the test
module at import, so a native test cannot skip there.
"""
import os
from pathlib import Path
import sys
import unittest

from coconut import hermes_runtime

REASON = ('Set COCONUT_HERMES_ROOT and COCONUT_HERMES_PYTHON, or run the tests with the Python of an installed Hermes, '
          'for the native integration checks.')


def _find() -> tuple[str | None, str | None]:
    root, python = os.environ.get('COCONUT_HERMES_ROOT'), os.environ.get('COCONUT_HERMES_PYTHON')
    if root or python:
        return root, python
    found = hermes_runtime.locate()
    if found is None or Path(found[0]).absolute() != Path(sys.executable).absolute():
        return None, None
    python, root = hermes_runtime.configured(*found)
    return str(root), str(python)


SOURCE, PYTHON = _find()
AVAILABLE = bool(SOURCE and PYTHON)
if os.environ.get('COCONUT_REQUIRE_NATIVE') and not AVAILABLE:
    raise RuntimeError('Native CI must install Hermes and set COCONUT_HERMES_ROOT and COCONUT_HERMES_PYTHON. '
                       'The native tests must not skip.')

# Decorate a test class or method that needs the installed Hermes.
needs_hermes = unittest.skipUnless(AVAILABLE, REASON)


def require(test: unittest.TestCase) -> None:
    """Skip the test when no Hermes is available. Call it in setUp of a test that also has work without Hermes."""
    if not AVAILABLE:
        test.skipTest(REASON)
