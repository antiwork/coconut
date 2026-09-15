# Parallel work

Coconut uses Hermes's native delegation limit. Hermes owns worker sessions, execution, and completion delivery.

Choose **Settings → Parallel work** for the default. Choose a bot under **Settings → Bots** for an override.

Automatic reserves half the logical processors and 4 GiB of memory. It then budgets 2 GiB per worker, with a ceiling of 10. If memory is unavailable, it uses at most two workers. This heuristic sets a starting limit; it does not predict model throughput or measure provider allowances.

The limit applies to each parent session. It does not cap workers across the Mac or start several foreground turns for one bot. Hermes decides when to delegate independent steps. Raising the limit does not split every queued request.

Coconut stores custom limits in native `delegation.max_concurrent_children` settings. An inherited bot setting contains YAML `null`. This selects Hermes's `DELEGATION_MAX_CONCURRENT_CHILDREN` fallback instead of its merged default. Coconut refreshes that environment value before each turn. Explicit bot limits remain independent of the default.

Changes do not stop active workers. The next bot turn reads the current default. The native compatibility check verifies inheritance, an override, and return to inheritance without a model request.

Hermes retains its native settings for worktree isolation and model routing. This setting does not change them.
