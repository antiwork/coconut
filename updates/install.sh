#!/bin/bash
# Install Coconut.app on this Mac, start Coconut, and pair the iPhone that shows the code.
#
#   curl -fsSL https://coconut-updates.gumroad.workers.dev/install | bash -s <code>
#
# The iPhone shows this command in a message for Hermes. The script:
#   1. downloads the newest notarized Coconut.app from the update feed,
#   2. checks the length and the SHA-256 that the feed names, the code signature, the team ID and Gatekeeper,
#   3. installs the app in /Applications, or in ~/Applications without write access to /Applications,
#   4. opens the app, which sets up Coconut and its login items, and waits for the service and the relay,
#   5. turns on automatic updates, so a Mac that nobody watches stays current,
#   6. runs `coconut pair <code>`, which pairs the iPhone through the relay,
#   7. prints a note for Hermes: how to turn automatic updates off, and how to update Coconut.
# It keeps an installed Coconut.app that can pair with a code (Sparkle updates it), so it is safe to run again. It
# prints one plain line for each step. The script holds no secret: the code works once, for 10 minutes, and only for
# the iPhone that made it.
#
# Tests set COCONUT_FEED_URL, and COCONUT_APP_FOLDERS as folders separated by colons.

set -euo pipefail

FEED_URL="${COCONUT_FEED_URL:-https://coconut-updates.gumroad.workers.dev/mac/appcast.xml}"
BUNDLE_ID="com.gumroad.coconut.mac"
TEAM_ID="66LJACJ2U7"
MINIMUM_MACOS=14
if [ -n "${COCONUT_APP_FOLDERS:-}" ]; then
  IFS=: read -r -a APP_FOLDERS <<<"$COCONUT_APP_FOLDERS"
else
  APP_FOLDERS=("/Applications" "$HOME/Applications")
fi

say() { printf 'Coconut: %s\n' "$1"; }
fail() { printf 'Coconut: %s\n' "$1" >&2; exit 1; }

# The XPath of the newest item. The feed lists the items from the newest build down.
ITEM_PATH="//item[1]"

# Print the installed Coconut.app, if any. An app that can do this run comes first: an earlier app can stay in a folder
# that this account cannot change.
installed_app() {
  local folder first=""
  for folder in "${APP_FOLDERS[@]}"; do
    [ -d "$folder/Coconut.app" ] || continue
    [ -n "$first" ] || first="$folder/Coconut.app"
    if can_run "$folder/Coconut.app" "$1"; then
      printf '%s\n' "$folder/Coconut.app"
      return
    fi
  done
  [ -z "$first" ] || printf '%s\n' "$first"
}

# The `coconut` tool in an app.
tool_of() { printf '%s\n' "$1/Contents/Helpers/coconut"; }

# True when the tool of the app pairs with a code from the iPhone. An earlier tool prints a setup link for any
# `coconut pair` and ignores the code.
pairs_with_code() {
  "$(tool_of "$1")" help </dev/null 2>/dev/null | grep -q 'pair <code>'
}

# True when the app sets up Coconut by itself when it opens with --set-up. An earlier app ignores the argument.
sets_up() {
  "$(tool_of "$1")" help </dev/null 2>/dev/null | grep -q -- '--set-up'
}

# True when the app can do this run: pair with the code, if there is one, and start a service that does not run.
can_run() {
  { [ -z "$2" ] || pairs_with_code "$1"; } && { sets_up "$1" || [ "$(TOOL="$(tool_of "$1")" status_field service)" = running ]; }
}

# Print one field of the service state, or nothing when the tool fails. An earlier tool prints JSON for `status` and
# ignores --json.
status_field() {
  "$TOOL" status --json </dev/null 2>/dev/null | plutil -extract "$1" raw -o - - 2>/dev/null || true
}

# Check that the app is Coconut, signed by Gumroad's team and accepted by Gatekeeper (notarized).
verify_app() {
  local app="$1" details
  codesign --verify --deep --strict "$app" 2>/dev/null || fail "The downloaded app has an invalid code signature. Nothing was installed."
  details="$(codesign --display --verbose=2 "$app" 2>&1)" || fail "The downloaded app has no code signature. Nothing was installed."
  grep -qx "Identifier=$BUNDLE_ID" <<<"$details" || fail "The downloaded app is not Coconut. Nothing was installed."
  grep -qx "TeamIdentifier=$TEAM_ID" <<<"$details" || fail "The downloaded app is not signed by Gumroad. Nothing was installed."
  spctl --assess --type execute "$app" 2>/dev/null || fail "Gatekeeper refused the downloaded app. Nothing was installed."
}

# A running app ignores new arguments and keeps its old files open, so it quits first. It loses nothing: the service
# runs apart from it.
quit_app() {
  if pgrep -qx Coconut; then
    osascript -e "tell application id \"$BUNDLE_ID\" to quit" >/dev/null 2>&1 || true
    sleep 2
  fi
}

# Download, check and install the newest app, in the folder or the first writable Applications folder.
# Print the path of the installed app.
install_app() {
  local folder="$1" work feed url length name digest
  # Global, so the trap can still read it when the script exits.
  WORK="$(mktemp -d)"
  trap 'rm -rf "$WORK"' EXIT
  work="$WORK"
  curl -fsSL --retry 3 -o "$work/appcast.xml" "$FEED_URL" || fail "Could not download the update feed $FEED_URL."
  feed="$work/appcast.xml"
  url="$(xmllint --xpath "string($ITEM_PATH/enclosure/@url)" "$feed" 2>/dev/null || true)"
  [ -n "$url" ] || fail "The update feed has no build."
  length="$(xmllint --xpath "string($ITEM_PATH/enclosure/@length)" "$feed")"
  name="${url##*/}"
  # Each archive name ends with the first 16 hex digits of its SHA-256.
  [[ "$name" =~ -([0-9a-f]{16})\.zip$ ]] || fail "The update feed names an unexpected archive: $name."
  digest="${BASH_REMATCH[1]}"
  say "Downloading $name." >&2
  curl -fsSL --retry 3 -o "$work/Coconut.zip" "$url" || fail "Could not download $url."
  [ "$(wc -c <"$work/Coconut.zip" | tr -d ' ')" = "$length" ] || fail "The download has the wrong size. Nothing was installed."
  [ "$(shasum -a 256 "$work/Coconut.zip" | cut -c1-16)" = "$digest" ] || fail "The download has the wrong SHA-256. Nothing was installed."
  ditto -x -k "$work/Coconut.zip" "$work/unpacked" || fail "Could not unpack the download."
  [ -d "$work/unpacked/Coconut.app" ] || fail "The download holds no Coconut.app."
  say "Checking the code signature and the notarization." >&2
  verify_app "$work/unpacked/Coconut.app"
  [ -n "$folder" ] || folder="$(first_writable_folder)"
  if [ -d "$folder/Coconut.app" ]; then
    quit_app
    rm -rf "$folder/Coconut.app" || fail "Could not replace $folder/Coconut.app."
  fi
  ditto "$work/unpacked/Coconut.app" "$folder/Coconut.app" || fail "Could not copy Coconut.app to $folder."
  say "Installed Coconut.app in $folder." >&2
  printf '%s\n' "$folder/Coconut.app"
}

# /Applications needs admin rights. Without them, ~/Applications works the same for one account.
first_writable_folder() {
  local folder
  for folder in "${APP_FOLDERS[@]}"; do
    mkdir -p "$folder" 2>/dev/null || continue
    if [ -w "$folder" ]; then
      printf '%s\n' "$folder"
      return
    fi
  done
  fail "No Applications folder is writable."
}

# Wait until the service state shows the value for the field. Return 1 after the seconds.
wait_for() {
  local field="$1" value="$2" seconds="$3" start=$SECONDS
  while [ $((SECONDS - start)) -lt "$seconds" ]; do
    [ "$(status_field "$field")" = "$value" ] && return 0
    sleep 3
  done
  return 1
}

main() {
  local code="" app="" installed="" automatic=no tool
  while [ $# -gt 0 ]; do
    case "$1" in
      -*) fail "Unknown option $1." ;;
      *) code="$1"; shift ;;
    esac
  done
  if [ -n "$code" ] && ! [[ "$code" =~ ^[a-zA-Z2-7]{26}$ ]]; then
    fail "This is not a Coconut pairing code. Copy the message from the iPhone again."
  fi

  [ "$(uname -s)" = Darwin ] || fail "Coconut runs on macOS only."
  local major
  major="$(sw_vers -productVersion | cut -d. -f1)"
  [ "$major" -ge "$MINIMUM_MACOS" ] || fail "Coconut needs macOS $MINIMUM_MACOS or later."

  app="$(installed_app "$code")"
  if [ -n "$app" ] && ! can_run "$app" "$code"; then
    say "The Coconut.app in ${app%/Coconut.app} is too old for this setup, so Coconut installs the newest version."
    # A folder that this account cannot change, for example /Applications without admin rights, keeps the earlier app.
    # The new app then goes in the first writable folder.
    if [ -w "${app%/Coconut.app}" ]; then
      installed="$(install_app "${app%/Coconut.app}")"
    else
      installed="$(install_app "")"
    fi
  elif [ -n "$app" ]; then
    say "Coconut.app is already in ${app%/Coconut.app}. It updates itself."
  else
    installed="$(install_app "")"
  fi
  [ -z "$installed" ] || app="$installed"
  TOOL="$(tool_of "$app")"
  [ -x "$TOOL" ] || fail "$app has no coconut tool. Delete it, then run this command again."
  if ! can_run "$app" "$code"; then
    fail "This Coconut.app is too old for this setup. A newer build adds what it needs. Try again after the next Coconut release."
  fi

  if [ "$(status_field service)" != running ]; then
    say "Opening Coconut.app. It sets up Coconut, which can take a few minutes."
    quit_app
    open -g "$app" --args --set-up
    wait_for service running 900 || fail "The Coconut service did not start. Open Coconut in the menu bar to see why."
  elif ! pgrep -qx Coconut; then
    # The service runs apart from the app, but the automatic updates run in the app.
    open -g "$app"
  fi
  say "The Coconut service runs."
  if [ "$(status_field relay)" != connected ]; then
    say "Waiting for the relay."
    wait_for relay connected 120 || fail "The relay did not connect. Open Coconut in the menu bar to see why."
  fi
  say "The relay is connected."

  # A Mac that runs Coconut for Hermes often has nobody who watches it, so it updates by itself.
  if "$TOOL" settings automatic-updates on </dev/null >/dev/null 2>&1; then
    automatic=yes
    say "Automatic updates are on."
  fi

  local done_text="Coconut is installed."
  if [ -n "$code" ]; then
    "$TOOL" pair "$code" </dev/null
    done_text="Coconut is installed and paired."
  fi

  tool="$(printf '%q' "$TOOL")"
  say "Note for Hermes:"
  if [ "$automatic" = yes ]; then
    say "$done_text Automatic updates are on. Ask the person whether to keep them on. To turn them off, run: $tool settings automatic-updates off. To update Coconut at any time, run: $tool update."
  else
    say "$done_text This Coconut.app cannot change its update settings yet. It still checks for updates by itself."
  fi
  if [ -z "$code" ]; then
    say "To pair an iPhone, open Coconut on the iPhone and send its message to Hermes."
  fi
}

# The whole script is read before it runs, so a command that reads standard input cannot take the rest of the script.
main "$@"
