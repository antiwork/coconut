# OpenAI API fallback

In Settings, open Model and enable **Use OpenAI API fallback**.
The Mac must have an OpenAI API key and an OpenAI model selected.
API usage has separate billing.

Coconut writes the fallback to Hermes's `fallback_providers` in `config.yaml`.
The primary model remains unchanged.
The fallback uses the selected model when you first enable the switch.
Hermes owns error handling, provider switching, and retries.
If the bot has a native credential pool, Hermes can retry with its configured credentials and endpoints.

The native provider ID is `openai-api`.
Hermes uses `openai` as a legacy alias for OpenRouter.
Do not substitute that alias when configuring an OpenAI API fallback.

Changes apply when a new turn starts.
Bots inherit the default OpenAI API fallback.
Other providers remain in their own profiles.
A bot with an explicit native fallback chain uses that chain instead of the default.
An explicit empty chain disables fallback for that bot.
Unpinned delegated workers inherit the parent fallback through Hermes.

The switch removes only the default OpenAI API routes when disabled.
Other native fallback routes remain unchanged.
Coconut sends only the public setting to the phone.
The API key stays on the Mac.
Save the key in the owning profile's `.env` or native `secrets:` configuration.
Fallback credentials do not use shell exports, which can remain stale in persistent workers.
