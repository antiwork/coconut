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
The reducer does not use tokens. Hermes uses normal context turns to check pending questions.

The iPhone shows one progress sentence. A compact action opens a decision when needed. The row menu provides progress requests and PR links. Completed work retains native swipe dismissal.
Client fields are optional so an older host can run during a rolling update.

Pending native questions receive a background check after one hour or when linked evidence changes.
The queue runs one automatic check at a time and waits at least five minutes between attempts for the same card.
A check that ends without a recorded result waits twice as long after each failure, up to an hour, and it stops after five failures
against the same evidence. New evidence lifts that stop, but the doubling wait still applies. A check can change the evidence
itself, for example through a background process, and that looks the same as a person who changes the scope. So after ten
failures in a row, whatever the evidence, the next check waits six hours after the last one. The card stays open for a person meanwhile.
Hermes reads the source and keeps, revises, or withdraws the existing question. These checks cannot approve actions.
Coconut rejects results when the card revision or linked evidence changes during the check.
Unchanged checks and failed checks do not add chat messages.
Native tool approvals and message drafts keep their separate approval rules.

An explicit handoff that uses the same Work key shows the parent's row.
The child keeps its task and progress records. Similar titles do not establish a duplicate.
Routine local setup belongs to the authorized code change. Bots use worktrees and isolated local services without creating setup approval cards.
Explicit human limits and native Hermes approval checks still apply.

A follow-up uses its original assignment's Work context when it lists or waits for dependencies.
This preserves the existing process owner without allowing waits across unrelated conversations.

A passing PR gets another readiness check when its previous check is over one hour old and no live work, decision, or dependency owns the next step.
This check reuses the existing task and cannot merge the PR.
