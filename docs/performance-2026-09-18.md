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

The live probe clones Researchy's configuration into a temporary profile. It asks for a short arithmetic result without tools. All replies are correct. It leaves the live profile unchanged. The provider-only control retains Coconut's Hermes adapter, so it is not a bare Hermes API benchmark. Session history and runtime warmth differ between paths. Do not subtract their medians to estimate Coconut overhead.

A separate low-reasoning trial records a 6,377 ms median across three service turns. This sample does not show a speed benefit. The current model settings remain unchanged. These short tests cannot establish coding quality.

Existing Hermes workers already reuse processes and native session history. Coconut retains native ownership and tool permissions. This change adds no model-based tracker and does not restore visible token streaming.

Production traces contain mixed workloads. They do not provide matched before-and-after cohorts. The user was away during deployment, so no new direct-chat production cohort was available for comparison.
