---
name: coconut
description: Update, pair, and repair the iPhone app's Mac service.
version: 1.0.0
author: Coconut
platforms: [macos]
metadata:
  hermes:
    tags: [coconut, iphone, mac, pairing, updates, troubleshooting]
    related_skills: [hermes-agent]
---

# Coconut Skill

Coconut is an iPhone app and a Mac service. The service runs on this Mac and runs bots, each with its own Hermes profile. The iPhone reaches the service through an end-to-end encrypted relay. This skill tells you how to update Coconut, pair an iPhone, and find the cause of a problem. It does not change Hermes itself.

**This skill is a hub.** Before you answer a detail question, load the matching reference with `skill_view`.

| Question | Reference |
|---|---|
| Update Coconut, update Hermes, automatic updates, "is Coconut current?" | `references/updates.md` |
| Pair an iPhone, a new iPhone, an expired code, "the iPhone shows a message for Hermes" | `references/pairing.md` |
| "Coconut is offline", the relay, `hermes_unavailable`, logs, data | `references/troubleshooting.md` |
| Each `coconut` command and its output | `references/cli.md` |

## When to Use

- The person asks you to update Coconut or to check its version.
- The person sends a message from the Coconut iPhone app that holds a pairing code.
- The person says that Coconut is offline, slow, or shows an error.
- The person asks how Coconut and Hermes work together on this Mac.

## Prerequisites

- macOS 14 or later.
- Coconut.app in `/Applications` or `~/Applications`. Its command line tool is `Coconut.app/Contents/Helpers/coconut`.
- Hermes, installed in the same Mac account. Coconut uses that Hermes. It does not install its own copy.

## How to Run

Run the `coconut` tool with `terminal`:

```
/Applications/Coconut.app/Contents/Helpers/coconut status
```

If the app is in `~/Applications`, use `~/Applications/Coconut.app/Contents/Helpers/coconut`.

## Quick Reference

| Task | Command |
|---|---|
| Show the version, the update state, the service, the relay, and Hermes | `coconut status` |
| Install a newer Coconut.app now | `coconut update` |
| Pair the iPhone that shows a code | `coconut pair <code>` |
| Print a one-time setup link | `coconut pair` |
| Turn automatic updates on or off | `coconut settings automatic-updates on` or `off` |

## Procedure

1. Run `coconut status` first. Read the lines `Service`, `Relay`, `Hermes`, and `Last update error`.
2. Load the reference for the question.
3. Before an action that stops work, tell the person and get a clear yes.
4. Run the action. Then run `coconut status` again, and tell the person the result.

## Pitfalls

- Do not run `scripts/host.py restart` while bots work. It stops their running turns at once. `coconut update` waits for running turns.
- Do not use `coconut update --wait` inside your own turn. The update waits for running turns, also yours.
- Do not edit files in the install folder (default `~/Coconut`). An update refuses to replace a changed file.
- Do not copy model keys, OAuth tokens, or `.env` files between Hermes profiles or into Coconut. Each profile keeps its own credentials.
- A pairing code works once, for ten minutes. Do not save it or send it to another person.

## Verification

`coconut status` shows `Service: running`, `Relay: connected`, and a Hermes version on the `Hermes` line.
