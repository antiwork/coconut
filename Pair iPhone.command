#!/bin/zsh
set -e
cd -- "${0:A:h}"
if [[ -f .runtime/remote.json ]]; then
  python3 scripts/remote.py pair
else
  python3 scripts/pairing.py
fi
