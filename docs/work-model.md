# Work outcomes

Work follows a requested result across bot turns and handoffs. A reply ending does not complete that result.

Coconut reuses four existing records:

- An artifact identifies the outcome and its completion condition.
- Tasks and decision replies record attempts.
- Native receipts record dependencies and their delivery.
- Decision cards record questions that require a human answer.

`Service._native_outcome_projection` derives the public state for both general work and pull requests from the native Hermes outcome.
A row from before native outcomes shows a saved terminal answer if it has one. Otherwise it shows "Status unavailable".
Fixture mode has no native outcomes and builds no Work rows, so it shows none.
The older reducer that read attempts and receipts (`LegacyWorkModel`) no longer runs in the product. It lives in `backend/tests/legacy_work_model.py` for the tests that still build such rows.

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
A check that ends without a recorded result waits twice as long after each failure (5, 10, 20 and 40 minutes), and it stops after five failures
against the same external evidence. The count ignores the receipts of the checks themselves, so a check that starts a background
process cannot restart it. New external evidence, for example a person who changes the scope, starts the count and the wait again.
The card stays open for a person meanwhile.
Hermes reads the source and keeps, revises, or withdraws the existing question. These checks cannot approve actions.
Coconut rejects results when the card revision or linked evidence changes during the check.
A check reads only human messages that can direct its card: replies on that card, and messages that a person sent in the card's own
outcome. A reply on another card, or a message whose chosen quote is from another conversation, is not input for this card.
The reply that the chat showed automatically when the person wrote does not count as a chosen quote, so a message sent in the card's
outcome stays input. The bot audience, the time, and the text alone never make a message relevant. Each message keeps its task,
thread, card, and chosen quote.

A withdrawal records the check that made it. When maintenance finds that a check withdrew a suggestion or message draft in error,
`coconut_correct_withdrawal` reopens the same card with its exact draft. Only a turn of the execution that owns the card, or a turn of
an execution that it genuinely delegated to in the same thread, sees and can use the tool. A genuine delegation is a service delivery
that set the delegate's reply link to the owner. A return, a progress reply, or a callback to the requester gives no authority.
Coconut reads that authority from service records, never from tool arguments or bot names.
The call names the card revision, the withdrawing check, the draft hash, and the source versions.
A withdrawal records the issue facts that its check saw and the receipt IDs of every check of the card. An older withdrawal uses
the source snapshot that its check recorded. The baseline must hold every issue fact (state, state reason, title, update time,
and comment count). Coconut reads each linked issue during the call and refuses it if any fact changed. A card that links a pull
request cannot be reopened, because the issue facts do not cover a new head, checks, or reviews. An `/issues/N` link can name a pull
request, so Coconut checks the kind on the GitHub record and refuses when it cannot read it.
Coconut also refuses a card that a person answered, dismissed, replied on, or replaced, a card with a later human message in its
outcome, a running check, and a tool result that is not confirmed. A recorded receipt that maintenance deleted counts as
unconfirmed. An older withdrawal without receipt IDs can be reopened only while its task is younger than the receipt retention
time (7 days), because maintenance cannot have deleted any of its receipts before then.
A blocking question cannot be reopened, because its withdrawal already resumed the task.
The later-message rule is deliberately strict. It also applies when a person asked for the repair in that outcome, because Coconut
cannot tell a maintenance request from direction about the card without reading prose. Such a card stays with the person.
A repair that the owner or its delegate requests, with no new human message in the outcome, can use the tool.
The correction is service maintenance, not human approval. It keeps the withdrawal in the card's history, and a replay changes nothing.
The person then chooses Send or Discard as usual.
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
