# Coconut in the cloud

Coconut can run on a Linux host without Hermes. Nothing uses this by default: every Mac runs its turns in the Hermes
gateway. A check on AWS on 2026-11-23 ran it end to end with the iPhone app (What the check showed, below).

## The parts

- **`--provider agent-sdk`** (`backend/coconut/agent_sdk_provider.py`). Claude's Agent SDK runs each turn, with Claude on
  Amazon Bedrock. Each chat keeps one SDK session (`<data>/agent-sdk/sessions.json`). The SDK keeps its transcripts and
  settings in `<data>/agent-sdk/claude`, never in a person's `~/.claude`. The card tool `coconut_ask` is an in-process
  MCP tool with the schema of the gateway's tool, without sources, previews and merge fields. The provider records a
  card through `native_question`, the path of a card of the gateway, and an answer continues the same SDK session. The
  agent has read-only tools: Read, Glob, Grep and WebFetch.
- **`cloud/sidecar.py`**. The end of the relay tunnel on Linux, a Python port of `scripts/tunnel-sidecar.swift`. It keeps
  the same files in `<data>/tunnel`, speaks the same relay protocol ([relay transport](relay-transport.md)) and runs the
  same Noise handshakes and frames, so the iPhone app pairs with a Linux host as it pairs with a Mac. `setup` registers a
  route with the relay, `run` connects, `link` prints a one-time pairing link, and `selftest <cacophony.txt>` checks the
  Noise code against the official test vectors in both roles.
- **`cloud/Dockerfile` and `cloud/entrypoint.sh`**. One container with the service, the sidecar and Litestream.
  Litestream runs the service and writes each commit of `coconut.sqlite3` to S3 within a second. On a volume without a
  database, the entrypoint restores the newest copy from S3 first.

## Run it

1. Let the AWS account use the model on Bedrock: accept its offer once (`ListFoundationModelAgreementOffers`, then
   `CreateFoundationModelAgreement`; pay per use). In the check, calls worked once the account had the agreement and the
   role that calls the model had `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream` and
   `aws-marketplace:ViewSubscriptions`, the last with the condition `aws:CalledViaLast` = `bedrock.amazonaws.com`. A
   role without `aws-marketplace:Subscribe` that calls a model without an agreement starts a subscription that fails a
   few minutes later.
2. Make an S3 bucket for the backups. The role needs `s3:ListBucket` and `s3:GetBucketLocation` on the bucket, and
   `s3:GetObject`, `s3:PutObject` and `s3:DeleteObject` on its objects.
3. On the host, build the image from the repository: `docker build -t coconut -f cloud/Dockerfile .`
4. Run it with AWS credentials for Bedrock and the bucket, for example an instance role:
   ```
   docker run -d --name coconut --network host --restart unless-stopped -v /srv/coconut:/data \
     -e AWS_REGION=us-east-1 -e LITESTREAM_REPLICA=s3://<bucket>/coconut coconut
   ```
   `COCONUT_SDK_MODEL` chooses the model (default `us.anthropic.claude-opus-5-5`), `COCONUT_BEDROCK_REGION` its region
   (default `us-east-1`), and `COCONUT_SDK_SMALL_MODEL` the model of side tasks (default: the same model). The host
   needs no open port: the sidecar connects out to the relay.
5. Pair the iPhone: `docker exec coconut python /app/cloud/sidecar.py link --data-dir /data --backend-dir /app/backend
   --name '<name>'`, then open the link on the iPhone. The link works once, for ten minutes.
6. Check a backup: `docker exec coconut sh -c 'litestream restore -o /tmp/check.db "$LITESTREAM_REPLICA"'`, then compare
   the rows of `/tmp/check.db` with `/data/coconut.sqlite3` and run `PRAGMA integrity_check` on the copy.

## What the check showed

- The iPhone app in the simulator, a build of `main`, paired with a Linux host on AWS through the production relay and
  used it as it uses a Mac.
- A message started a turn on Claude Opus 5.5 through Bedrock. The agent answered and asked with a card. The answer
  continued the same session, which asked a linked follow-up card. Three turns took seven model calls.
- With its database removed, the container restored it from S3 at start: the same rows, the app connected again, and
  the next card answer ran a full turn. A separate restore matched the live database and passed `PRAGMA integrity_check`.

## Gaps

- Litestream copies only the database. The SDK's sessions, the identity of the tunnel and the keys of the paired phones
  are files beside it, so a new host loses the context of each chat and the phone must pair again. The SDK's
  `session_store` and a secret store would close this.
- Push notifications and team rooms use Swift tools of the Mac (`push-encrypt` in `push_relay.py`, the team helper in
  `team_helper.py`), which a Linux host lacks.
- The provider checks a card only after the turn ends, so a card that breaks a rule of `coconut_ask`, such as a
  recommended choice that is not first, fails the turn. In the gateway, the agent gets the error and fixes the card.
- The agent has no tool that writes files or runs commands, no tool approvals, and no other Coconut tool
  (`coconut_suggest`, pages, Work). Routines do not run. A question about a card (`answer_context`) gets a fixed reply.
- The outcome of an answered card still says "Hermes continued with your answer." (`service.py`).
