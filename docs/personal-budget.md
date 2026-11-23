# The Personal budget

Antiwork pays for the model use of each person's Personal project, up to $200 a month. Coconut, Gumroad and the
person's own keys are not part of it.

## How it works

1. In a Personal chat, the iPhone app shows **Sign in with Apple** under the last turn (`PersonalBudget.swift`), once the
   relay offers the budget (Turning it on).
2. The app sends Apple's identity token to the Mac: `POST /v1/settings/personal-budget {identity_token}`.
3. The Mac sends it to the relay: `POST /v1/budget/accounts` (`relay/src/budget.js`). The relay checks the token
   against Apple's keys, with the app's bundle ID as audience (`relay/src/apple.js`). The account is the SHA-256 of
   the team ID and Apple's stable user ID, so a reinstall or a second Mac reaches the same account and the same ledger.
   The relay answers with a budget token `cb1.<account>.<secret>` and keeps only the hash of the secret, at most ten
   per account.
4. The Mac keeps the token in `<data>/personal-budget.json` (0600) and points only the Hermes profile of Personal at
   the proxy (`budget_native.py`): a `providers.coconut-budget` entry with `api_mode: anthropic_messages` and
   `key_env: COCONUT_BUDGET_TOKEN`, the token in the profile's `.env`, and `model.provider: coconut-budget`. The
   profile's earlier `model` section is saved in `<profile>/.coconut-budget.json`.
5. Hermes calls `https://coconut-push.gumroad.workers.dev/v1/budget/anthropic/v1/messages` with the token as
   `x-api-key`. The relay forwards the call to Anthropic with Antiwork's key and adds its cost to the month.
6. Every five minutes the Mac reads `GET /v1/budget/usage` and puts it in the state as `personal_budget`. The chats
   of Personal show "$41 of $200 this month".

Signing out (`DELETE /v1/settings/personal-budget`, from the line's menu) puts the saved `model` section back, removes
the provider and the `.env` key, and revokes the token at the relay.

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

- Personal runs on the Hermes home on a Mac without a `personal` profile. There, the home's model belongs to the
  budget while it is on, and the Coconut chat refuses another model until the person signs out. The person's own
  `hermes` in Terminal then uses the budget too.
- Hermes copies the default `.env` into a new profile that has none, so a profile made while the budget is on can get
  the token. It still uses its own model provider.
- The Mac app shows the line only. Sign in with Apple is in the iPhone app.

## Turning it on

The budget routes answer 503 until the relay has the secret `ANTHROPIC_API_KEY` (Antiwork's key). Until then the apps
show no line and no button. Every five minutes the Mac asks the relay `GET /v1/budget/usage` without a token (503
means off, 401 on), keeps the answer in the setting `personal_budget_offered`, and leaves the Personal project out of
`personal_budget` for a person who is not signed in. Every iPhone build hides the budget when that project is missing,
so no phone needs a new build. When the answer changes, the Mac adds the event `personal_budget.updated`, which the
updates of the apps do not know, so their next update is a whole state and the line shows or hides at once.

Sign in with Apple needs the capability on the App ID `com.gumroad.coconut`. The TestFlight workflow signs automatically
with `-allowProvisioningUpdates`, which adds it when the App Store Connect key may change identifiers.

## Checks

`cd relay && node --test test/budget.test.js` covers the cost, the ledger, the limit, the tokens and the Apple token.
