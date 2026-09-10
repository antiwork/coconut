#!/bin/zsh
set -e
cd -- "${0:A:h}"
python3 scripts/remote.py update
read -r '?Press Return to close.'
