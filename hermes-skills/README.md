# Hermes skills of the owner's bots

Coconut is a Hermes client. It connects the phone to the person's Hermes profiles. It does not decide how a bot does its work.

The workflow of a bot is the person's choice. It lives in the bot's Hermes profile: its skills, `SOUL.md` and memory. This folder keeps the skills of the owner's bots on the Mac mini under version control. Coconut does not read this folder. Hermes reads the installed copies in `<bot home>/skills/<name>/SKILL.md`.

`docs/hermes-client-audit.md` lists the workflow policy that moves out of Coconut's backend, and the skill that receives each part.

## Layout

- `<bot>/BOT` names the bot and its Coconut bot ID.
- `<bot>/<skill>/SKILL.md` is one skill: one workflow, with Hermes frontmatter (`name`, `description`).
- `shared/<skill>/SKILL.md` is a skill that more than one bot uses. Each of those bots links it: `<bot>/<skill>` is a symbolic link to `../shared/<skill>`.

The folders of a bot are the list of its skills. For example, `ls hermes-skills/desk` shows Desk's skills.

## Install a change

Before a PR removes policy text from Coconut, install the skill that replaces it. Then no behavior is lost while the PR waits or deploys.

1. Copy the folder to the Mac mini, for example with `scp -r hermes-skills coconut@<mini>:/tmp/`.
2. On the Mac mini, run `/tmp/hermes-skills/install.sh desk`.
3. The script backs up each changed `SKILL.md` to `~/Coconut-backups/hermes-skills-<time>/<bot id>/`.

Hermes reads a changed skill at the next turn of the bot. No restart is necessary.
