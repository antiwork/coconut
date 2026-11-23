# The Personal budget

Antiwork pays for the model use of each person's Personal project, up to $200 a month. Coconut, Gumroad and the
person's own keys are not part of it.

## How it works

Antiwork trusts people, so the budget has no sign-in. Until 2026-11-23 it asked for Sign in with Apple, which tied each
budget to one Apple ID. The person found that too much for a light feature and chose to remove the check.

1. In a Personal chat, the iPhone app and the Mac app show **Use the Antiwork budget** under the last turn
   (`PersonalBudget.swift`), once the relay offers the budget (Turning it on).
2. A tap asks the Mac to turn it on: `POST /v1/settings/personal-budget {}`. When it is on already, the Mac returns its
   state and opens no second account.
3. The Mac asks the relay for an account: `POST /v1/budget/accounts` (`relay/src/budget.js`). The relay asks for no
   proof: each request opens a new account with its own ledger, a random 64-character ID. It answers with a budget
   token `cb1.<account>.<secret>` and keeps only the hash of the secret.
4. The Mac keeps the token in `<data>/personal-budget.json` (0600) and points only the Hermes profile of Personal at
   the proxy (`budget_native.py`): a `providers.coconut-budget` entry with `api_mode: anthropic_messages` and
   `key_env: COCONUT_BUDGET_TOKEN`, the token in the profile's `.env`, and `model.provider: coconut-budget`. The
   profile's earlier `model` section is saved in `<profile>/.coconut-budget.json`.
5. Hermes calls `https://coconut-push.gumroad.workers.dev/v1/budget/anthropic/v1/messages` with the token as
   `x-api-key`. The relay forwards the call to Anthropic with Antiwork's key and adds its cost to the month.
6. Every five minutes the Mac reads `GET /v1/budget/usage` and puts it in the state as `personal_budget`. The chats
   of Personal show "$41 of $200 this month".

Turning it off (`DELETE /v1/settings/personal-budget`, Stop using the Antiwork budget in the line's menu) puts the saved
`model` section back, removes the provider and the `.env` key, and revokes the token at the relay.

## The ledger

- One `BudgetAccount` Durable Object per account holds the token hashes and `spent:<YYYY-MM>` in millionths of a
  dollar. Months are UTC calendar months.
- The cost comes from Anthropic's `usage` of each call at the list prices in `BUDGET_PRICES` (`wrangler.toml`):
  input, output, cache reads, 5-minute and 1-hour cache writes, and web searches. A stream is metered to its end,
  also when the Mac stops reading.
- Only `BUDGET_MODEL` (Claude Opus 5.5) at standard speed is covered. Fast mode and regional inference are refused.
- At `BUDGET_LIMIT_USD` the proxy refuses calls with 403 and "Personal used its $200 of model use for November. It
  starts again on December 1." A call that started below the limit finishes, and an account runs at most four calls
  at once, so a month can end a few dollars over.
- A routine of Personal on a paused budget gets the same refusal, so it costs nothing.

## Limits

- Anyone can open accounts, so turning the budget off and on again, a reinstall, a second Mac or a script each gets a
  new $200 a month. The only brake is the relay's limit of 30 new accounts a minute per address
  (`REGISTRATION_LIMIT`). Antiwork chose that over a sign-in.
- Personal runs on the Hermes home on a Mac without a `personal` profile. There, the home's model belongs to the
  budget while it is on, and the Coconut chat refuses another model until the person turns it off. The person's own
  `hermes` in Terminal then uses the budget too.
- Hermes copies the default `.env` into a new profile that has none, so a profile made while the budget is on can get
  the token. It still uses its own model provider.
- iPhone builds from before the change still show Sign in with Apple. The Mac and the relay ignore the identity token
  that they send, so the button still turns the budget on.

## Turning it on

The budget routes answer 503 until the relay has the secret `ANTHROPIC_API_KEY` (Antiwork's key). Until then the apps
show no line and no button. Every five minutes the Mac asks the relay `GET /v1/budget/usage` without a token (503
means off, 401 on), keeps the answer in the setting `personal_budget_offered`, and leaves the Personal project out of
`personal_budget` for a person who has not turned it on. Every iPhone build hides the budget when that project is
missing, so no phone needs a new build. When the answer changes, the Mac adds the event `personal_budget.updated`,
which the updates of the apps do not know, so their next update is a whole state and the line shows or hides at once.

## Checks

`cd relay && node --test test/budget.test.js` covers the cost, the ledger, the limit and the tokens.
