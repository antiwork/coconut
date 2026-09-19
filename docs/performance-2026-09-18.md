# Performance measurements, September 18, 2026

These measurements separate local acceptance, dispatch, GitHub observation, and complete model replies. They do not establish a tenfold improvement in complete replies.

| Measurement | Before | After | Method |
| --- | ---: | ---: | --- |
| Direct conversation dispatch | 2,014 ms | 2.12 ms | Median of five native-service runs with a fixed two-second maintenance delay |
| Save a card response locally | 2,035 ms | 0.6–2.0 ms | Simulator tests with a fixed two-second server response delay |
| Update a fast PR behind a slow request | 509 ms | 1.86 ms | Median of five service runs with a fixed 500 ms GitHub delay |
| Complete a short live reply | 5,651 ms | 5,464 ms | Median of three Researchy-clone turns per version |

The dispatch test uses the real service and native Hermes task adapter with a fixture model. It measures admission under contention.

The card tests use the real AppStore and a local HTTP fixture. They also check restart recovery, rejection, transient retries, stable request identifiers, and independent conversations. Local acceptance means the phone saved the response. It does not mean the server accepted the decision. The server still checks the original revision and approval scope.

The PR test uses the real service with a fixture GitHub tracker. Fast results no longer wait for unrelated requests. Registration wakes the existing monitor. External GitHub changes still use the periodic refresh when no local event occurs.

The live probe clones Researchy's configuration into a temporary profile. It asks for a short arithmetic result and requests no tools. Native Hermes instructions require a tool for arithmetic, so this measures a tool workflow. All replies are correct. It leaves the live profile unchanged. The provider-only control retains Coconut's Hermes adapter, so it is not a bare Hermes API benchmark. Session history and runtime warmth differ between paths. Do not subtract their medians to estimate Coconut overhead.

A separate arithmetic trial with low reasoning records a 6,377 ms median across three service turns. This sample does not show a speed benefit. The current model settings remain unchanged. These short tests cannot establish coding quality.

Existing Hermes workers already reuse processes and native session history. Coconut retains native ownership and tool permissions. This change adds no model-based tracker and does not restore visible token streaming.

Production traces contain mixed workloads. They do not provide matched before-and-after cohorts. The user was away during deployment, so no new direct-chat production cohort was available for comparison.

## Request profiling and input selection

A second pass uses native Hermes request hooks. It records request counts, durations, retries, token counts, and cache hits. It does not record prompts, answers, tool arguments, credentials, or model configuration. Each event retains the Coconut task and turn identifiers.

The arithmetic probe makes two or three provider requests. Hermes requires a tool for arithmetic. Those results cannot measure the no-tool lower bound.

A fixed-text probe asks for `READY`. All six service/control replies match, and all use one provider request without tools. The three service times are 4,730 ms, 2,578 ms, and 2,679 ms. The first includes cold setup. The two warm requests spend 2,305 ms and 2,392 ms in the provider call. Warm worker preparation takes about 50 ms. This is an observed boundary for this model, prompt, and route, not a physical lower bound.

The service previously decoded each bot's full visible history to select the current task input. It now uses the existing task index. The full history remains available for native conversation recovery. Visibility, shared messages, message order, and reply boundaries remain unchanged.

A read-only benchmark uses the live database and checks identical results across nine runs:

| Bot | Visible history rows | Selected rows | Old selection median | Indexed selection median |
| --- | ---: | ---: | ---: | ---: |
| Desk | 5,322 | 1 | 59.94 ms | 0.080 ms |
| Gumroad Dev | 4,378 | 0 | 40.56 ms | 0.054 ms |
| Researchy | 14 | 2 | 0.042 ms | 0.011 ms |

These gains apply to input selection and assignment context. They do not establish a tenfold improvement in complete replies.
