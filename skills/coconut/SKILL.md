---
name: coconut
description: Update, pair, repair, and join teams with Coconut.
version: 1.4.0
author: Coconut
platforms: [macos]
metadata:
  hermes:
    tags: [coconut, iphone, mac, pairing, updates, troubleshooting, routines, cron, team, library, pages]
    related_skills: [hermes-agent]
---

# Coconut Skill

Coconut is an iPhone app and a Mac service. The service runs on this Mac and runs agents: project chats that all run on the default Hermes profile. The iPhone reaches the service through an end-to-end encrypted relay. This skill tells you how to update Coconut, pair an iPhone, find the cause of a problem, make a routine that spends model tokens only when there is work, and write a good page for the Library. It does not change Hermes itself.

**This skill is a hub.** Before you answer a detail question, load the matching reference with `skill_view`.

| Question | Reference |
|---|---|
| Update Coconut, update Hermes, automatic updates, "is Coconut current?" | `references/updates.md` |
| Pair an iPhone, a new iPhone, an expired code, "the iPhone shows a message for Hermes" | `references/pairing.md` |
| "Coconut is offline", the relay, `hermes_unavailable`, logs, data | `references/troubleshooting.md` |
| Each `coconut` command and its output | `references/cli.md` |
| A routine, a cron job, a script gate (`wakeAgent`), a check that should cost fewer tokens, waiting for delegated work | `references/routines.md` |
| A page of the Library, a report page, the notes of a save of `coconut_save_document` | `references/pages.md` |
| Per-agent queues, parallel work limits, the kanban board, Coconut task data | `references/work-queues.md` |
| Change the Coconut app or service code: worktree, push to `main` without checks, check by hand, which build each device runs | `references/app-changes.md` |

## When to Use

- You change the code of Coconut: see "Change Coconut's code".
- The person asks you to update Coconut or to check its version.
- The person sends a message from the Coconut iPhone app that holds a pairing code.
- A message has the invite link of a team, or asks about the team of this Mac.
- The person says that Coconut is offline, slow, or shows an error.
- The person asks how Coconut and Hermes work together on this Mac.
- You create or change a routine, or a routine runs often and finds nothing new.
- You want the result of a delegated task. Do not make a cron job for it: see `references/routines.md`.
- You write a page of the Library, or a save of a page answers with notes: see `references/pages.md`.
- The person asks for a change to how the Coconut app behaves: see `references/app-changes.md`.

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
| Show the teams of this Mac, their members and their agents | `coconut team` |
| Join a team with an invite link | pipe the link to `coconut team join` |
| On a Mac for a team only: add an agent to the team, or remove it | `coconut team add-agent <agent>`, `coconut team remove-agent <agent>` |

## Change Coconut's code

The person wants speed. The repository `antiwork/coconut` has no tests, no commit hook and no CI.

1. Work on `main` in a separate worktree. Do not open a pull request unless the person asks.
2. Do not run tests, builds or other checks before a push. Commit, rebase on `origin/main` and push.
3. Never force-push `main`. Resolve conflicts and keep other people's changes.
4. Do not release. The release agent starts the TestFlight and Mac release workflows twice a day.

## Teams

A team is people who share pages and talk in one chat. A person makes a team and its invite links in the Coconut app. This Mac can join from a message.

The agents of a person stay that person's. The person shares a page with a team on the page in the Library of the Coconut app. An agent cannot share a page: a save names no audience. Only a Mac that runs Coconut for a team only gives its agents to a team.

1. When a message has an invite link of a team (it starts with `coconut://team/`), pipe the link to the tool. Never give the link as an argument of `coconut`:

   ```
   printf '%s\n' '<the invite link>' | coconut team join --name '<name of this Mac>'
   ```

   The link is then not an argument of `coconut`: the tool reads it from standard input. Without `--name`, the member list shows "Member".
2. Never repeat the link in a reply, a log, a file, or a memory. It lets any Mac into the team.
3. On a Mac that runs Coconut for a team only, run `coconut team add-agent <agent>` to give an agent to the team. `<agent>` is the name or the ID of the agent. When this Mac is in more than one team, add `--team <name or ID>`. On a person's Mac the command prints how to share a page.
4. Run `coconut team` to read the state: the members, the agents, and the sync error. Another Mac shows a new page after its next sync, within a minute.
5. A leave cannot be undone. Run `coconut team leave` without `--confirm` first, show its text to the person, and run the command that it prints for the team only after a clear yes. The name after `--confirm` chooses the team.

A join that is done already prints "This Mac is already in this team." That is a success. For each error, tell the person the line that the tool printed.

## Procedure

1. Run `coconut status` first. Read the lines `Service`, `Relay`, `Hermes`, and `Last update error`.
2. Load the reference for the question.
3. Before an action that stops work, tell the person and get a clear yes.
4. Run the action. Then run `coconut status` again, and tell the person the result.

## "/help" and settings without a screen

Coconut keeps options out of the app. The person changes them by asking an agent, and `/help` shows what is possible.

- When the person sends `/help`, answer in short lines: what you can do in this chat, what they can ask you to change, and that the Coconut chat changes the settings of Coconut (the default model and access, each agent's model, access, name and notifications, work sessions per agent, helpers per task). Name the `coconut` commands that fit (`references/cli.md`).
- Do not send the person to a settings screen for these. In the Coconut chat, change them with `coconut_settings`. In another chat, tell the person to ask the Coconut chat.
- Parallel work has no total limit: every agent works at once.

## Queues, parallel work, and Coconut features

The person prefers native Hermes functionality over custom Coconut or agent-built versions: reuse Hermes (kanban board, cron, delegation settings) and propose deleting Coconut's duplicate code where Hermes covers it.

1. Pin down the unit first. "Project" or "queue" usually means a Coconut agent (a project chat on the default profile), not a GitHub repo. When the word is ambiguous, ask with `coconut_ask` before building; a wrong guess wastes a whole build-and-remove cycle.
2. Check what exists before designing: `hermes kanban --help`, then Coconut's board with `HERMES_HOME=~/Coconut/.runtime/host/kanban hermes kanban stats` (plain `hermes kanban` reads an empty default board), and the Coconut service code in `~/Coconut/backend/coconut` (`work_capacity.py`, `parallel_work.py`). Coconut already queues each agent's work on the Hermes kanban board. Depth: `references/work-queues.md`.
3. A change to how Coconut queues, shows, or limits work is a Coconut app change in `antiwork/coconut` (committed to `main` per its CONTRIBUTING, `references/app-changes.md`), not a cron job, GitHub labels, or a routine on a business repo. From an agent's chat, hand the build to a work session with `coconut_kanban_create`, and confirm the session runs on the model the person named (`references/work-queues.md`).
4. Write the design as a Library page, then ask with `coconut_ask` naming the page. Touch GitHub topics, labels, routines, or settings only after approval.
5. Be careful on shared repos: before adding labels or a runner to a repo, check whether another agent already dispatches its issues (its labels such as `queued-build`, `awaiting-*`, and that profile's `hermes -p <name> cron list`), and reuse the repo's existing priority and blocked labels instead of adding parallel ones.

A project for business functions is still a GitHub repo with the `coconut-project` topic (`gh search repos --owner <owner> --topic coconut-project`); org repos such as `antiwork/*` do not show under the user's own login, so search the org or by name.

## Model and standing rules

Every chat runs on the default Hermes profile, so the model, `SOUL.md`, memory and access are shared by all chats. An agent of an earlier release can still run on a named profile until the person merges it (`python3 -m coconut.profile_merge`, see `docs/architecture.md`).

1. Map the spoken model name to a real ID from the catalog (e.g. "fable 5.2" → `claude-fable-5-2`, provider `anthropic`); say the mapping in one line. The model changes for every chat: from the Coconut chat with `coconut_settings` `default_model`, or `hermes config set model.default <id>`.
2. A standing rule for one project goes into the default `SOUL.md` as a section that names the chat it applies to ("Only in the \"Gumroad\" chat"). Use `read_file` then `write_file` (the guard refuses an unread overwrite). Keep the hard limits: no outbound messages, spending, deletion, merge, or deploy on a best guess, and no overriding an explicit hold.
3. Do each change as its own small tool call, not one long terminal command with backups and heredocs: a long command raises a native approval prompt, and an unanswered prompt blocks the whole step.
4. Native Hermes approval prompts are not Coconut cards. A "continue if I don't respond" rule cannot answer them. Access is one setting for every chat, which only the main Coconut chat changes (`coconut_settings` `default_access`).
5. When a chat asks to remove approval prompts, `coconut_settings` refuses there. Do not switch them off through `hermes config set` instead: that is an agent disabling its own safety check by a side route after the proper one refused. Reply with the exact sentence to send in the Coconut chat ("let chats run commands without asking") and one line on what turning prompts off means.

The person values autonomy and short replies. Treat a terse "Go" as approval of the plan just described, and act at once without restating it.

## Pitfalls

- Do not run `scripts/host.py restart` while agents work. It stops their running turns at once. `coconut update` waits for running turns.
- Do not use `coconut update --wait` inside your own turn. The update waits for running turns, also yours.
- Do not edit files in the install folder (default `~/Coconut`). An update refuses to replace a changed file.
- Do not copy model keys, OAuth tokens, or `.env` files between Hermes profiles or into Coconut. The merge of old profiles lists the credentials to connect again in default; it never copies them.
- Set a profile's Hermes config with `hermes -p <name> config set <key> <value>` (or `HERMES_HOME=~/.hermes/profiles/<name> hermes config set ...`). Without the profile, the write goes to the default profile's `~/.hermes/config.yaml`. The `patch`/`write_file` tools refuse `config.yaml` as security-sensitive, and a hand-rolled script edit in the terminal triggers an approval prompt, so `hermes config set` is the one path that lands without a prompt.
- Never tell the person the app already behaves some way without checking `origin/main`. A message that describes app behavior is often a feature request; answering "that's how it works" makes them ask twice.
- A bug report ("counts too high") usually reflects the installed build, not `main`: compare the installed service code and test the fix on a backup copy of the live database before changing code (`references/app-changes.md`, "A bug report about the person's own data").
- The reverse also happens: a feature request for something already on `main`. Do not rebuild it and do not stop at "it exists"; check which build each device runs (`references/app-changes.md`, "The requested feature is already on main").
- A pairing code works once, for ten minutes. Do not save it or send it to another person.

## Verification

`coconut status` shows `Service: running`, `Relay: connected`, and a Hermes version on the `Hermes` line.
