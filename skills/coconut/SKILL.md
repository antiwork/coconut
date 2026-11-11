---
name: coconut
description: Update, pair, repair, and join teams with Coconut.
version: 1.2.0
author: Coconut
platforms: [macos]
metadata:
  hermes:
    tags: [coconut, iphone, mac, pairing, updates, troubleshooting, routines, cron, team]
    related_skills: [hermes-agent]
---

# Coconut Skill

Coconut is an iPhone app and a Mac service. The service runs on this Mac and runs bots, each with its own Hermes profile. The iPhone reaches the service through an end-to-end encrypted relay. This skill tells you how to update Coconut, pair an iPhone, find the cause of a problem, and make a routine that spends model tokens only when there is work. It does not change Hermes itself.

**This skill is a hub.** Before you answer a detail question, load the matching reference with `skill_view`.

| Question | Reference |
|---|---|
| Update Coconut, update Hermes, automatic updates, "is Coconut current?" | `references/updates.md` |
| Pair an iPhone, a new iPhone, an expired code, "the iPhone shows a message for Hermes" | `references/pairing.md` |
| "Coconut is offline", the relay, `hermes_unavailable`, logs, data | `references/troubleshooting.md` |
| Each `coconut` command and its output | `references/cli.md` |
| A routine, a cron job, a script gate (`wakeAgent`), a check that should cost fewer tokens, waiting for delegated work | `references/routines.md` |

## When to Use

- The person asks you to update Coconut or to check its version.
- The person sends a message from the Coconut iPhone app that holds a pairing code.
- A message has the invite link of a team, or asks about the team of this Mac.
- The person says that Coconut is offline, slow, or shows an error.
- The person asks how Coconut and Hermes work together on this Mac.
- You create or change a routine, or a routine runs often and finds nothing new.
- You want the result of a delegated task. Do not make a cron job for it: see `references/routines.md`.

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
| Show the teams of this Mac, their members and their bots | `coconut team` |
| Join a team with an invite link | pipe the link to `coconut team join` |
| Add a bot of this Mac to the team, or remove it | `coconut team add-bot <bot>`, `coconut team remove-bot <bot>` |

## Teams

A team is a set of Macs that share their bots and the pages of these bots. A person makes a team and its invite links in the Coconut app. This Mac can join from a message.

1. When a message has an invite link of a team (it starts with `coconut://team/`), pipe the link to the tool. Never give the link as an argument of `coconut`:

   ```
   printf '%s\n' '<the invite link>' | coconut team join --name '<name of this Mac>'
   ```

   The link is then not an argument of `coconut`: the tool reads it from standard input. Without `--name`, the member list shows "Member".
2. Never repeat the link in a reply, a log, a file, or a memory. It lets any Mac into the team.
3. To add a bot of this Mac, run `coconut team add-bot <bot>`. `<bot>` is the name or the ID of the bot. When this Mac is in more than one team, add `--team <name or ID>`.
4. Run `coconut team` to read the state: the members, the bots, and the sync error. Another Mac shows a new bot after its next sync, within a minute.
5. A leave cannot be undone. Run `coconut team leave` without `--confirm` first, show its text to the person, and run the command that it prints only after a clear yes.
6. `coconut remove` removes Coconut from a Mac that runs it for a team only. A removal cannot be undone. First, this Mac leaves each team, so the other Macs no longer show this Mac, its bots and their pages. Then Coconut.app removes its login items, the data of Coconut, and the key of this Mac in each team. To come back, this Mac needs a new invite link. First run `coconut remove` without `--confirm`: it changes nothing, and it prints what the removal does and the teams that this Mac leaves. Show that text to the person, and run `coconut remove --confirm` only after a clear yes. A leave that fails removes nothing: show its lines to the person. `coconut remove --confirm --no-leave` skips the leave, and the other Macs then keep this Mac, its bots and their pages in their lists until an admin can remove a member, so run it only after a second clear yes. A good result names the teams that this Mac left, then reads "Coconut is removed: the login items, the data and the key of this Mac in each team." Then `coconut status` shows `Service: not installed`. On a full install, the command changes nothing and prints one line that says so.

A join that is done already prints "This Mac is already in this team." That is a success. For each error, tell the person the line that the tool printed.

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
