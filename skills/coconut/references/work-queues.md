# Work queues and parallel work

Coconut's queue for each agent is the Hermes kanban board. Build on it; do not invent a second queue.

## What exists

| Piece | Where |
|---|---|
| Queue | Coconut's own Hermes kanban board at `~/Coconut/.runtime/host/kanban/kanban.db`, not `~/.hermes/kanban.db`. Read it with `HERMES_HOME=~/Coconut/.runtime/host/kanban hermes kanban show <task_id>` (also `list`, `stats`, `set-model`, `reclaim`, `comment`); without that `HERMES_HOME`, `hermes kanban` reads an empty default board and reports "no such task" |
| Dispatch | Coconut's service dispatches kanban tasks (`kanban_runtime.py`, `service._dispatch_pending_locked`). The person's turn always goes first and never waits for a slot (`work_capacity.person_turn`) |
| One turn per chat | An agent's chat runs one turn at a time; work sessions run in their own chats |
| Total limit | Coconut setting "Parallel work" (`work_capacity`, `PUT /v1/settings/work-capacity`). Automatic = `max(1, min(10, cpus//2, (GB-4)//2))`; 4 on an 8-core 16 GB Mac |
| Helpers per task | Hermes `delegation.max_concurrent_children` per profile (`parallel_work.py`). The Coconut global value does not reach profiles that inherit it |
| Per-agent cap | Hermes has `kanban.max_in_progress_per_profile` and board-wide `kanban.max_in_progress`; Coconut does not expose them yet |
| iPhone UI | `ios/Coconut/ProfileView.swift` (Parallel work row), `ParallelWorkSettingsView` |

Hermes kanban docs: https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban (concurrency config, workspaces, dispatcher). `hermes kanban diagnostics` shows the dispatch allowlist.

## Read Coconut's task data

The service database is `~/Coconut/.runtime/host/coconut.sqlite3`. Tables store JSON in a `data` column, so query with `json_extract`. Open it read-only:

```
sqlite3 -readonly ~/Coconut/.runtime/host/coconut.sqlite3 "select json_extract(b.data,'$.name'), json_extract(t.data,'$.status'), count(*) from tasks t join bots b on b.id=json_extract(t.data,'$.bot_id') group by 1,2"
```

Do not run broad `find ~` searches for it; they time out.

## Start a Coconut code change from an agent's chat

`coconut_workspace` and `coconut_track_work` work only inside a work session, so the agent's chat refuses them. From the chat:

1. Check open PRs in `antiwork/coconut` that touch the same code, and find a clean clone (`~/Developer/coconut`). Never work in `~/Coconut`, which is the live install.
2. Call `coconut_kanban_create` with the whole request in `body`: the user's words, scope, files to start from, a branch and worktree path off `origin/main`, limits (draft PRs only, no merge, deploy, service restart or live config change), and the done condition. Use `workspace_kind: dir` on the clone. The task is dispatched within seconds.
3. When the person names a model, check which model the session actually runs: `sqlite3 -readonly ~/.hermes/profiles/<profile>/state.db "select id, model from sessions order by started_at desc limit 3"`. A kanban `set-model` override may not reach a Coconut gateway turn. Pin the model on the profile's helpers instead (`delegation.model` and `delegation.provider`, see the pitfall below), and tell the worker with `hermes kanban comment` to do the heavy work through `delegate_task`. A `set-model` plus `reclaim` restart can leave the first run going, so check for a second session before you report.
4. Report the task, the model it actually runs on, and the limits. The PR links come back on the task.

## Change one profile's Hermes config

`hermes config set` writes `~/.hermes/config.yaml` (the default profile) unless `HERMES_HOME` points at the profile, and `hermes -p <name> config` may not find the profile. Use `HERMES_HOME=~/.hermes/profiles/<name> hermes config set <key> <value>`, then read that profile's `config.yaml` to confirm. When a write lands in the wrong file, remove it at once and tell the person.

## Realistic limits

Size from the Mac, not from wishes: total = Coconut automatic limit; per agent = 2 (chat plus one work session) so several agents always progress; helpers per task = 2 keeps the process count near total × (1 + helpers). Model rate limits, not the Mac, are the next ceiling. Cloud runners would not count against the Mac total.
