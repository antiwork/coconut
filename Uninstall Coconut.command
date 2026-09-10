#!/bin/zsh
set -e
cd -- "${0:A:h}"
if [[ -x .runtime/hermes/venv/bin/python ]]; then
  exec .runtime/hermes/venv/bin/python scripts/desktop.py uninstall
fi
exec /usr/bin/env python3 scripts/desktop.py uninstall
