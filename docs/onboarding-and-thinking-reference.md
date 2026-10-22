# Onboarding and thinking references

## Source and method

- Claude Duet: `Chris_Flores_-_Claude_Duet_onboarding_in_case_you_re_curious_0ofQZr.mp4`. 88.8 seconds, 1206 × 2622 pixels, 60 fps, light theme.
- Grok Bot: `ScreenRecording_10-21-2026 13-48-03_1.MP4`. 67.4 seconds, 1206 × 2622 pixels, 60 fps, dark theme.

FFmpeg extracts an overview at 1 fps for each recording.
Crops at 6–60 fps show the status rows, the header avatar, the typing dots, and the page transitions.
All times are approximate to ±0.1 s, unless the table gives a frame count.
The frames stay in the session scratchpad. They are not in the repository.

## Claude Duet onboarding

### Page structure

Every page uses one layout: a segmented progress bar, a serif title, a gray subtitle, one live illustration, and a black full-width button.
A back chevron appears from page 2.
A small disclaimer sits directly above the button when a page needs one.

| Time | Page | Illustration |
| --- | --- | --- |
| 00:00–00:09 | 24/7 proactive help that keeps tasks moving | Clock dial. One hand sweeps with a red gradient trail. Red markers sit on the rim. A chip appears when the hand passes a marker: "Finished your report", "Prepped your 2 PM", "Drafted a reply", "Reviewed a PR". |
| 00:09–00:16 | Already knows your setup | Claude mark on the left, Duet character on the right, joined by a line. Tool icons travel along the line and land as chips in a list that grows downward: Gmail, Calendar, Drive, then projects, then skills. A green check badge lands on the character at about 00:13. |
| 00:16–00:28 | It gets things done on the web, too | A request chip ("Sign my team up for the conference") above a browser window labeled "Claude's own computer". A cursor with a "Claude" tag moves field to field and fills Name, Team, and Dates. Then a result bubble fades in: "The form's filled in. Take a look before I send it." |
| 00:28–00:37 | Safe, private, and in your control | A faded phone outline behind three rows: "Pauses when something looks unsafe", "You set the permissions", "Private and secure". |
| 00:37–00:45 | Connect your apps | One connector row (Slack, plus button). Continue and Not now. |
| 00:45–00:52 | Get notified | A phone home screen. A notification banner drops in from the top. The button opens the system prompt. After Allow, the button turns into a green "✓ Notifications on" pill for about 1 s. |
| 00:52–00:56 | Handoff | The page fades to blank. The character appears at the center. Its ink stroke redraws into a check mark. Eight short ticks burst outward and fade. The character then becomes the chat header avatar. |
| 00:56–00:82 | First chat | The bot sends a welcome, says "I'm going to check your apps to determine who you are and how I can help", works for about 25 s, and then sends a profile summary with "How I'll help" bullets. |

### Motion details

- Page change (00:09.0–00:09.8): the old title, subtitle, and illustration fade out together in about 0.3 s. The new title fades in first. The subtitle follows about 0.1 s later. Illustration parts enter in a stagger after that.
- Clock hand: about 30° per second. Each chip fades in at the hand and fades out when the next chip appears.
- Setup import: each icon takes about 0.5 s to travel the line. The list grows one row at a time. The order is apps, then projects, then skills.
- Notification banner: it slides down with a small overshoot and settles in about 0.4 s.
- Check mark burst (00:54.3–00:55.4, 15 fps crop): the stroke draws in 5 frames. Ticks appear on frame 8, reach full length on frame 10, and are gone by frame 14.

### Thinking in Duet

Duet shows two signals at the same time.

1. The header avatar animates and shows a label pill under it.
2. A three-dot bubble sits at the end of the transcript.

The header avatar is an orange dot with one black ink stroke.
The stroke shape changes with the current tool:

| Label | Stroke |
| --- | --- |
| Thinking it through | A loop that grows and shrinks on a horizontal line |
| Checking memory | A wave that oscillates |
| Searching your email | A magnifier |
| Checking recent files | A swoosh that sweeps around the dot |

Between two states, the stroke retracts into a short stub at the edge of the dot in about 0.3 s.
The next stroke then draws out from that stub.
The label pill crossfades at the same moment.
The dot itself never moves, so the change reads as one character changing its gesture.

The three dots use a wave: one dot darkens at a time, from left to right.
The wave crosses all three dots in about 0.6 s, holds for about 0.4 s, and repeats.

## Grok Bot thinking

Grok Bot shows one status row directly below the last message.
The row has a small white face with two black eyes and a gray label.
The header does not change.

| Time | Label |
| --- | --- |
| 00:35 | Working on it |
| 00:36–00:37 | Mulling it over |
| 00:37.6–00:38 | Typing… |
| 00:38 | Interim message appears: "Sure. First I'll look up what coconut is and how you use it, then I'll draft." |
| 00:38–00:56 | Putting it together, Working in GitHub, and Mulling it over alternate. Each label lasts 1–5 s. |
| 00:56–00:64 | Typing… |
| 00:64.9 | The full reply appears in one frame. |

### Motion details

- Send (00:34.4): the draft becomes a bubble in the transcript. The status row appears about 0.3 s later.
- Label change: the text swaps in one frame. It does not crossfade.
- Shimmer: a bright band about one word wide moves from left to right over the gray label. It crosses "Putting it together" in about 1.2 s and then repeats.
- Face: each label has its own gaze. "Mulling it over" looks up and to the left. "Putting it together" uses two slanted ovals, and the face rolls as if the eyes move over a ball.
- Face to dots (00:37.7, 60 fps): the label changes first. Over the next 15 frames (0.25 s) the face shrinks into a middle dot and two side dots grow out of it. The reverse morph happens when work resumes after the interim message.
- Reply: Grok Bot does not stream the final text. "Typing…" covers the whole generation, and then the reply appears at once.

## Coconut today

- Onboarding is the Mac scripts plus pairing in Settings (`MacPairingFields` in `ios/Coconut/SettingsViews.swift`). The app has no first-run flow.
- `scripts/hermes.py` installs a pinned runtime in `.runtime/hermes`. Coconut never reads `~/.hermes`.
- An existing Hermes user loses their profiles, `config.yaml`, provider logins, memory, skills, and MCP servers. Only `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and `OPENROUTER_API_KEY` transfer.
- Thinking is `BotWorkingIndicator` (`ios/Coconut/ConversationView.swift`): a 38 pt animated avatar below the last row. It shows no text.
- The phone hides all `activity` messages, although `worker_activity.py` already computes phases such as "Reading files" and "Running tests".
- The indicator disappears when the connection is not `.connected`, even when work continues.
- The final reply is buffered and appears at once. This matches Grok Bot, but Coconut shows nothing that says a reply is being written.

The 38 pt avatar with no label is the awkward part.
It is large for a status, it says nothing, and the same motion runs for 2 s or for 10 min.

## Proposal: thinking

Use the Grok Bot row with Duet's per-tool gesture.

1. Replace `BotWorkingIndicator` with a status row: a 20 pt live avatar and a one-line label in secondary text.
2. Add a left-to-right shimmer to the label. Use one pass of about 1.2 s and a 0.6 s rest.
3. Derive the label from real phases. Map the `worker_activity.py` phases to short phrases: Thinking it through, Reading files, Editing files, Running a command, Running tests, Searching, Checking GitHub.
4. Give each phase its own expression on the existing avatar: gaze, eyelids, and mouth. Between two phases, pass through the neutral idle face in about 0.3 s, as Duet passes through the stub.
5. Hold each label for at least 1.2 s, so that fast tool calls do not flicker.
6. When the final text starts, morph the face into three dots in 0.25 s and show "Writing…". Morph back if tools run again.
7. Keep interim messages as normal bubbles. The status row always follows the newest message.
8. After one minute, append the elapsed time: "Running tests · 1 min".
9. When the phone loses the connection during work, keep the row and show "Reconnecting…" without shimmer.
10. With Reduce Motion, keep the label, remove the shimmer and the morph, and keep the green activity dot.

This replaces the rule in `product-design.md` that keeps tool activity out of the conversation.
The new rule: show one live phase label, never a tool log.

## Proposal: onboarding for any Hermes setup

The Mac detects the setup. The phone shows it and asks for choices.

### Mac

1. Add a read-only setup probe. It checks, in order: `$HERMES_HOME`, `~/.hermes`, and the managed `.runtime/hermes/terminal/hermes`.
2. The probe reports the Hermes version, the default provider and model, whether a model call can authenticate, the profiles (name, skill count, memory size, SOUL present), MCP servers, and logins for `gh` and Codex.
3. Expose the result as `GET /v1/setup` after pairing.
4. Coconut keeps its pinned runtime for execution. It reads the user's configuration but never writes to it.
5. Importing a profile copies its `SOUL.md`, memories, skills, and `config.yaml` into a new bot home. The original profile stays unchanged. Symlinks stay rejected.

### Phone

1. Welcome: what Coconut does. Show a bot that works around the clock, with the Duet clock and real Coconut events (a tracked PR, a decision, a routine).
2. Pair: the current scan or paste flow, moved into the first-run flow.
3. Your Hermes setup: the Duet import animation with the Hermes mark, the coconut, and chips for the provider, profiles, skills, and MCP servers. If the probe finds no setup, say "Coconut set up its own Hermes" and show the model access step.
4. Choose your bots: one toggle for each found profile. Each becomes a bot with a generated character. With no profiles, create one default bot.
5. Model access: show it only when the probe cannot authenticate. Give the exact command to run on the Mac.
6. Notifications: the existing relay permission, with the Duet banner preview and the green confirmation pill.
7. Handoff: the coconut character shows a check mark burst and becomes the header of the first bot's chat. The first bot sends an introduction that uses what the probe found.

## Open decisions

- Import by copy, or run existing profiles in place? Copy is safe and matches the current ownership checks. In-place use keeps one source of truth but conflicts with `coconut-profile.json` ownership and the pinned Hermes version.
- Stream the final reply? Grok Bot does not stream. Streaming needs `hermes_worker.py` to emit `text_delta` before the turn completes. The "Writing…" state makes buffering acceptable, so streaming can wait.
