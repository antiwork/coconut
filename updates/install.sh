#!/bin/bash
# Install Coconut.app on this Mac, start Coconut, and pair the iPhone that shows the code.
#
#   curl -fsSL https://coconut-updates.gumroad.workers.dev/install | bash -s <code>
#   curl -fsSL https://coconut-updates.gumroad.workers.dev/install | bash -s -- --channel nightly <code>
#
# The iPhone shows this command in a message for Hermes. The script:
#   1. downloads the newest notarized Coconut.app from the update feed (the stable channel, or the nightly channel
#      before the first stable release, or the channel of --channel),
#   2. checks the length and the SHA-256 that the feed names, the code signature, the team ID and Gatekeeper,
#   3. installs the app in /Applications, or in ~/Applications without write access to /Applications,
#   4. opens the app, which sets up Coconut and its login items, and waits for the service and the relay,
#   5. runs `coconut pair <code>`, which pairs the iPhone through the relay.
# It keeps an installed Coconut.app (Sparkle updates it), so it is safe to run again. It prints one plain line for each
# step. The script holds no secret: the code works once, for 10 minutes, and only for the iPhone that made it.
#
# Tests set COCONUT_FEED_URL and COCONUT_APP_FOLDERS.

set -euo pipefail

FEED_URL="${COCONUT_FEED_URL:-https://coconut-updates.gumroad.workers.dev/mac/appcast.xml}"
BUNDLE_ID="com.gumroad.coconut.mac"
TEAM_ID="66LJACJ2U7"
MINIMUM_MACOS=14

say() { printf 'Coconut: %s\n' "$1"; }
fail() { printf 'Coconut: %s\n' "$1" >&2; exit 1; }

# The XPath of the newest item of a channel. The feed lists the items from the newest build down. A stable item has no
# <sparkle:channel>.
item_path() {
  if [ "$1" = stable ]; then
    printf '%s' "//item[not(*[local-name()='channel'])][1]"
  else
    printf '%s' "//item[*[local-name()='channel']='$1'][1]"
  fi
}

# Print the installed Coconut.app, if any.
installed_app() {
  local folder
  for folder in ${COCONUT_APP_FOLDERS:-/Applications $HOME/Applications}; do
    if [ -d "$folder/Coconut.app" ]; then
      printf '%s\n' "$folder/Coconut.app"
      return
    fi
  done
}

# Print one field of `coconut status`, or nothing when the tool fails.
status_field() {
  "$TOOL" status </dev/null 2>/dev/null | plutil -extract "$1" raw -o - - 2>/dev/null || true
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

# Download, check and install the newest app of the channel. Print the path of the installed app.
install_app() {
  local channel="$1" work feed path url length name digest folder
  # Global, so the trap can still read it when the script exits.
  WORK="$(mktemp -d)"
  trap 'rm -rf "$WORK"' EXIT
  work="$WORK"
  curl -fsSL --retry 3 -o "$work/appcast.xml" "$FEED_URL" || fail "Could not download the update feed $FEED_URL."
  feed="$work/appcast.xml"
  path="$(item_path "$channel")"
  url="$(xmllint --xpath "string($path/enclosure/@url)" "$feed" 2>/dev/null || true)"
  if [ -z "$url" ] && [ "$channel" = stable ] && [ -z "$CHANNEL_OPTION" ]; then
    # Before the first stable release, the nightly build is the only build.
    channel=nightly
    path="$(item_path nightly)"
    url="$(xmllint --xpath "string($path/enclosure/@url)" "$feed" 2>/dev/null || true)"
    say "There is no stable release yet, so Coconut uses the nightly channel." >&2
  fi
  [ -n "$url" ] || fail "The update feed has no build in the $channel channel."
  length="$(xmllint --xpath "string($path/enclosure/@length)" "$feed")"
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
  folder="$(first_writable_folder)"
  ditto "$work/unpacked/Coconut.app" "$folder/Coconut.app" || fail "Could not copy Coconut.app to $folder."
  if [ "$channel" != stable ]; then
    defaults write "$BUNDLE_ID" updateChannel "$channel" >/dev/null
  fi
  say "Installed Coconut.app in $folder." >&2
  printf '%s\n' "$folder/Coconut.app"
}

# /Applications needs admin rights. Without them, ~/Applications works the same for one account.
first_writable_folder() {
  local folder
  for folder in ${COCONUT_APP_FOLDERS:-/Applications $HOME/Applications}; do
    mkdir -p "$folder" 2>/dev/null || continue
    if [ -w "$folder" ]; then
      printf '%s\n' "$folder"
      return
    fi
  done
  fail "No Applications folder is writable."
}

# Wait until `coconut status` shows the value for the field. Return 1 after the seconds.
wait_for() {
  local field="$1" value="$2" seconds="$3" start=$SECONDS
  while [ $((SECONDS - start)) -lt "$seconds" ]; do
    [ "$(status_field "$field")" = "$value" ] && return 0
    sleep 3
  done
  return 1
}

main() {
  CHANNEL_OPTION=""
  local code="" app
  while [ $# -gt 0 ]; do
    case "$1" in
      --channel) CHANNEL_OPTION="${2:-}"; shift 2 || fail "--channel needs stable or nightly." ;;
      --channel=*) CHANNEL_OPTION="${1#--channel=}"; shift ;;
      -*) fail "Unknown option $1." ;;
      *) code="$1"; shift ;;
    esac
  done
  case "${CHANNEL_OPTION:-stable}" in stable|nightly) ;; *) fail "--channel needs stable or nightly." ;; esac
  if [ -n "$code" ] && ! [[ "$code" =~ ^[a-zA-Z2-7]{26}$ ]]; then
    fail "This is not a Coconut pairing code. Copy the message from the iPhone again."
  fi

  [ "$(uname -s)" = Darwin ] || fail "Coconut runs on macOS only."
  local major
  major="$(sw_vers -productVersion | cut -d. -f1)"
  [ "$major" -ge "$MINIMUM_MACOS" ] || fail "Coconut needs macOS $MINIMUM_MACOS or later."

  app="$(installed_app)"
  if [ -n "$app" ]; then
    say "Coconut.app is already in ${app%/Coconut.app}. It updates itself."
  else
    app="$(install_app "${CHANNEL_OPTION:-stable}")"
  fi
  TOOL="$app/Contents/Helpers/coconut"
  [ -x "$TOOL" ] || fail "$app has no coconut tool. Delete it, then run this command again."

  if [ "$(status_field service)" != running ]; then
    say "Opening Coconut.app. It sets up Coconut, which can take a few minutes."
    # A running app ignores new arguments, so it quits first. It loses nothing: the service runs apart from it.
    if pgrep -qx Coconut; then
      osascript -e "tell application id \"$BUNDLE_ID\" to quit" >/dev/null 2>&1 || true
      sleep 2
    fi
    open -g "$app" --args --set-up
    wait_for service running 900 || fail "The Coconut service did not start. Open Coconut in the menu bar to see why."
  fi
  say "The Coconut service runs."
  if [ "$(status_field relay)" != connected ]; then
    say "Waiting for the relay."
    wait_for relay connected 120 || fail "The relay did not connect. Open Coconut in the menu bar to see why."
  fi
  say "The relay is connected."

  if [ -z "$code" ]; then
    say "Coconut is ready. To pair an iPhone, open Coconut on the iPhone and send its message to Hermes."
    return
  fi
  "$TOOL" pair "$code" </dev/null
}

# The whole script is read before it runs, so a command that reads standard input cannot take the rest of the script.
main "$@"
