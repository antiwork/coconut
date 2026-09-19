# Remaining request latency

These measurements do not establish a tenfold improvement in complete model replies or a physical lower bound.

## Remove unrelated context reads

PR context previously decoded all saved tasks and artifacts. The service now selects the relevant task IDs and PR records. It preserves current-head checks, original owners, callback links, and the latest observation.

A read-only comparison checks identical output for the twenty most recent tasks per bot:

| Bot | Previous median | New median | Samples |
| --- | ---: | ---: | ---: |
| Desk | 114.57 ms | 4.27 ms | 20 |
| Gumroad Dev | 115.59 ms | 4.29 ms | 20 |

These measurements cover the context function. They do not include model or phone latency. Sixty-nine focused tests pass under the pinned runtime.

## Locate the provider boundary

The arithmetic probe triggers native mandatory tool use. A fixed `READY` reply isolates a request without tools. The scratch profile retains Researchy's configuration. No live bot settings change.

| Probe | Warm service times | Provider request times |
| --- | --- | --- |
| Full bot context | 2,578 and 2,679 ms | 2,305 and 2,392 ms |
| Minimal native context | 2,056 and 3,012 ms | 1,771 and 2,749 ms |
| Full context with native priority requested | 2,396 and 1,967 ms | 2,116 and 1,675 ms |

The minimal control removes tool definitions, identity, memory, context files, and Coconut instructions. It is diagnostic only. A fresh-session provider-only control uses 499 input tokens and records 1,431, 1,707, and 1,366 ms provider calls. It still uses Coconut's transport adapter. It is not a bare HTTP benchmark.

The samples are small and sequential. Provider load, cache state, and session history vary. They cannot prove a small priority-mode benefit. The service does not enable priority mode or replace the selected model.

The evidence identifies provider request time as the main remaining cost for this short warm reply. It does not prove a physical limit or eliminate possible improvements in longer tool workflows. Request hooks now measure those workflows without recording their content.
