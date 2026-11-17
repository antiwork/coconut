# Routines that spend model tokens only when there is work

A Coconut routine is a Hermes cron job of the agent's profile. The Hermes gateway of that profile runs it on its schedule, and each run that wakes the agent is a turn in the agent's chat in Coconut. A run that wakes the agent costs model tokens, also when it finds nothing to do. This reference tells you how to make a routine cheap and predictable.

## Choose the kind of routine

| The work | Kind | How |
|---|---|---|
| A fixed check or a report that needs no judgment, for example a disk alert or a nightly export | Script only | `hermes cron create ... --script <file> --no-agent`. The script is the job. Hermes delivers its stdout. Empty stdout is a silent run. |
| A check that needs judgment only when something changed, for example new mail, a new issue, or a failed build | Script gate | `hermes cron create ... --script <file>` with an agent prompt. The script runs first. Its stdout goes into the prompt as context. A last stdout line `{"wakeAgent": false}` skips the agent run. |
| Work that needs judgment each time, for example a weekly summary | Agent routine | A prompt and a schedule, without a script. |

Prefer a script gate when most runs find nothing new. Coconut shows such a routine with a "Script check" line. The person can edit its name, instructions, schedule and state in Coconut. An edit in Coconut keeps the script.

## Write a gate

1. Exit fast. Read only what tells you whether there is work: an updated time, a revision, a count, or a list of IDs.
2. Compare it with the state that the last run saved, in a file beside the script. Save the new state only after the comparison.
3. When nothing changed, print one short line about the check, and then the last line `{"wakeAgent": false}`.
4. When something changed, print the facts that the agent needs, for example the new IDs and their links. Do not print `wakeAgent`. The agent then runs with this text as context.
5. Wake the agent when in doubt: a read that fails, a state file that is missing or broken, or an answer that you cannot parse. Print the reason.
6. Log each decision with its time, its inputs and its result, so a person can see why a run woke the agent or not.
7. Never print secrets. Read credentials from the profile's environment, not from the script file.

A script ending in `.sh` or `.bash` runs with bash. Hermes runs any other script with Python.

## Attach a gate to a routine

1. Put the script in the `scripts` folder of the agent's Hermes profile.
2. Find the job ID with `hermes cron list`.
3. Run `hermes cron edit <id> --script <file>` with `terminal`. Name the file inside the `scripts` folder.
4. Run the routine once with "Run now" in Coconut, or with `hermes cron run <id>`. Check that a quiet run ends without an agent turn, and that a run with work starts one.

To remove the gate, run `hermes cron edit <id> --script ""`.

`hermes cron run <id>` only triggers the job: the gateway runs it at its next tick, so wait about a minute, then read the run in `~/.hermes/profiles/<profile>/cron/output/<id>/` (newest `.md`) and the gate's own log. A run whose output shows `## Script Error` with "dependency environment interpreter is missing" means Hermes' Python environment for scripts is gone; wrap the gate in a `.sh` file that sets `PATH` and runs `exec /usr/bin/python3 "$(dirname "$0")/<gate>.py"`, then point the job at the `.sh`.

A gate that calls `gh` from cron needs `PATH` with `/opt/homebrew/bin`; set it in the wrapper.

## Wait for delegated work

Do not create a cron job to poll for the result of a delegated task or of another agent, for example a one-time job "result pickup in 10 minutes". Each such run is a model turn, and it often finds no result yet.

Call `coconut_wait` with the task ID, and finish your turn. Coconut continues your turn when the task finishes or fails, with its result. For an answer of the person, the card does the same: its answer continues the work.
