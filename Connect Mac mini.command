#!/bin/zsh
set -e
cd -- "${0:A:h}"
python3 scripts/remote.py authorize
python3 scripts/remote.py doctor
read -r '?Press Return to close.'
