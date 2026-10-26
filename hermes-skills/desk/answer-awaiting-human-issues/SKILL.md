---
name: answer-awaiting-human-issues
description: Ask Gianfranco about a GitHub issue or pull request that waits for him, post his answer to the source, and decide whether to close the issue.
---

# Answer sources that wait for Gianfranco

Use this skill when a Coconut work-source audit says that a source waits for the person. Use it again when a card about such a source gets an answer.

A source waits for Gianfranco when it is assigned to him, it has the `awaiting-human` label, and the latest comment is not his.

## Ask

1. Read the source body and all comments. Find the question that the latest request asks.
2. If the latest comments already settle the choice, do not make a card.
3. A delegated ruling, a scheduled recheck, or a wait for another party does not answer the source. Ask him.
4. Ask with one `coconut_ask` card, also when you know the likely answer.
5. Use the question that the latest request asks. Offer its own choices with their exact letters or names. Mark the recommended choice.
6. Put the source link in `sources`.
7. Reuse a pending card about the source. Do not ask again a question that he answered or dismissed.

## Post the answer

1. Post his answer to the source as a comment. Use his words or the chosen choice.
2. Read the comment back to verify it.
3. If an answered card has an answer that is not on the source yet, post that recorded answer now. Do not ask again.
4. A rejection with no text, or a dismissed card, means that he chose not to act. Post nothing and ask nothing.

## Close or keep open

Close only an issue. Never close a pull request, or a source of unknown type.

After you post his answer and verify the comment, close the issue with `gh issue close <url>` only when his answer settles everything that the issue asks. Examples: a ruling, an approval, a refund call. Then verify that the issue is closed.

Keep the issue open when:

- Work continues after the answer: a feature, bug or plan issue (labels `enhancement`, `bug`, `planning`, `working`).
- The request says that the answer unblocks work, a draft, a PR or a next phase.
- The answer says to keep the issue open.
- The answer asks a question back instead of deciding.
- You are not sure.

He said so. gumroad-private#3931 was closed wrongly.
