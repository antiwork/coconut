# Work outcomes

Work follows a requested result across bot turns and handoffs. A reply ending does not complete that result.

Coconut reuses four existing records:

- An artifact identifies the outcome and its completion condition.
- Tasks and decision replies record attempts.
- Native receipts record dependencies and their delivery.
- Decision cards record questions that require a human answer.

`WorkModel._work_outcome` derives the public state for both general work and pull requests.

| State | Evidence | Action |
| --- | --- | --- |
| Working | A live worker or a recent observation of a running process | Show the current progress sentence |
| Queued | A queued attempt or a saved result awaiting continuation | Keep the same outcome visible |
| Waiting | A recorded dependency without a result | Resume through the existing dispatcher |
| Needs decision | A linked pending card | Open that card |
| Needs recovery | No current execution, dependency, decision, or completion explains the pause | Request progress with the outcome attached |
| Completed | Recorded completion evidence, a completed assignment root, or a terminal pull request | Move into Recently completed |

A failed step remains visible when an unrelated watcher continues. A new attempt can replace an earlier failed attempt.
Coconut retains earlier attempts in the discussion context. Earlier failures do not determine a resumed outcome's state.

Hermes supplies progress text and completion evidence. Coconut checks record relationships and execution state; it does not independently prove every reported result.
New `coconut_track_work` completion reports require `completion_evidence`. Existing explicit completion reports remain compatible.
A pull request closing completes the pull request outcome. It does not prove deployment or complete separate rollout work.

Decision research can continue while its card remains pending. A saved research result queues a continuation of the current card.
The dispatcher records delivery once. Research cannot approve the card or repeat an external action.

The reducer does not call a model, start work, change permissions, or write records. Existing dispatch and recovery code owns those actions.
No supervisor bot or additional polling model is required.

The iPhone shows one progress sentence. A compact action opens a decision when needed. The row menu provides progress requests and PR links. Completed work retains native swipe dismissal.
Client fields are optional so an older host can run during a rolling update.
