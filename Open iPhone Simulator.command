#!/bin/zsh
set -e
cd -- "${0:A:h}"
if [[ -x .runtime/hermes/venv/bin/python ]]; then
  exec .runtime/hermes/venv/bin/python scripts/desktop.py simulator
fi
print "Install Hermes first: python3 scripts/hermes.py install"
exit 1
