# Routines and team milestone

Run this check from the Coconut folder:

```sh
python3 scripts/team_milestone.py --provider fixture
```

Use `--provider hermes` to run the same service checks with real Hermes agents. That mode sends real model requests. It reads existing model keys through `scripts/host.py`. It does not copy keys into the test directory or print them.

Each run starts a separate service on an unused loopback port. The run creates a fresh `.runtime/team-milestone-*` directory. It leaves `.runtime/host` unchanged. The service stops when the check finishes.

The check verifies these results:

- Two bots have different session IDs.
- A routine accepts creation, editing, pause, and resume requests.
- A scheduled occurrence runs automatically at an upcoming UTC minute.
- A hard restart preserves the occurrence without dispatching it twice.
- A message addressed to Lin from Ada's conversation receives Lin's attributed response in that conversation.
- Repeated recipient IDs and an identical request retry create one recipient task.
- Ada requests a check from Lin through a bot tool.
- Lin returns the result through the bot tool.
- Ada reports the result in the same conversation thread.
- The exchange records two deliveries within its reported limit of eight.
- A cancelled future task stays cancelled after the restart.
- Event replay retrieves the restart and subsequent changes.

The Hermes mode also checks separate bot home directories. Directory separation does not provide an operating system security boundary.

The fixture uses the same HTTP service, scheduling, routing, persistence, and restart paths. Its provider scripts the responses and tool requests. It does not prove real model behavior.

The run has one 170-second limit, followed by at most five seconds for process cleanup. The routine uses a daily schedule with at least two seconds before its first occurrence. It waits at most 62 seconds for that occurrence. This avoids a second legitimate occurrence during the restart check.

The check verifies that this exchange stays within its delivery budget. It does not exhaust that budget or prove cancellation during a model request. The report states those limits.

`team-milestone-report.json` contains IDs, counts, statuses, and check results. It excludes prompt text, pairing tokens, and model keys. The isolated directory retains service history for inspection. `scripts/cleanup.py --yes` removes these directories with the rest of `.runtime`.

The real trial also allows routine delegation. One occurrence can contain several bot tasks.

The script counts occurrence roots by `routine_id`. It verifies the related task IDs separately after restart.
