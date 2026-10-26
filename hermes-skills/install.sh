#!/bin/sh
# Install the skills of one bot folder into that bot's Hermes home on this Mac.
# Usage: hermes-skills/install.sh <bot folder> [<bots root>]
# Example: hermes-skills/install.sh desk
# Before it replaces a SKILL.md, it copies the old file to ~/Coconut-backups/hermes-skills-<time>/.
set -eu
folder=$1
root=${2:-$HOME/Coconut/.runtime/host/bots}
here=$(cd "$(dirname "$0")" && pwd)
bot=$(sed -n 's/^bot_id: *//p' "$here/$folder/BOT")
home=$root/$bot/home
[ -d "$home" ] || { echo "No Hermes home at $home" >&2; exit 1; }
backup=$HOME/Coconut-backups/hermes-skills-$(date -u +%Y%m%dT%H%M%SZ)/$bot
for source in "$here/$folder"/*/SKILL.md; do
  name=$(basename "$(dirname "$source")")
  target=$home/skills/$name/SKILL.md
  mkdir -p "$(dirname "$target")"
  if [ -f "$target" ]; then
    cmp -s "$source" "$target" && { echo "unchanged $name"; continue; }
    mkdir -p "$backup"
    cp -p "$target" "$backup/$name.SKILL.md"
  fi
  cp "$source" "$target.new"
  chmod 600 "$target.new"
  mv "$target.new" "$target"
  echo "installed $name"
done
