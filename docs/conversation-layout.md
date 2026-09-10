# Conversation layout

The transcript can turn blank when `LazyVStack` estimates heights for long Markdown messages.
The data remains present, but the scroll view displays an empty region.
A private copy of Desk's transcript reproduced this failure.
The same failure occurs with every letter and digit replaced by `x`.

Conversation and inbox threads now use `VStack` to measure message heights before applying scroll and keyboard offsets.
The change preserves message IDs, automatic scrolling after sending, and event handling.
It does not change Hermes, stored messages, or sessions.

Regular stacks load all their content, while lazy stacks load nearby views.
This fix therefore measures all messages that the client loads.
See [Apple's scroll view documentation](https://developer.apple.com/documentation/swiftui/scroll-views).

## Verification

`ConversationLayoutMessages.json` contains 38 messages with varied lengths and Markdown formatting.
It contains no original words, names, numbers, or URLs.
`test_conversation_layout.py` serves these messages through an isolated local API.
The test does not call Hermes or send messages to the Mac mini.

The UI test checks these actions:

- Open the conversation at the latest message.
- Scroll to earlier messages.
- Type with the software keyboard during streamed updates.
- Expand the composer to five lines.
- Send a message and receive a streamed reply.
- Dismiss the keyboard, continue typing, and send again.

The original layout fails the first visibility assertion and displays a blank transcript.
The fixed layout passes the full test without reopening the conversation.
XCTest records screenshots at each assertion.

Run the test on the development Mac:

```sh
xcodegen generate --spec ios/project.yml
xcodebuild build-for-testing -project ios/Coconut.xcodeproj -scheme Coconut \
  -destination 'platform=iOS Simulator,id=SIMULATOR_ID' \
  -derivedDataPath .build/layout CODE_SIGN_IDENTITY=-
python3 scripts/test_conversation_layout.py \
  --xctestrun .build/layout/Build/Products/Coconut_iphonesimulator26.5-arm64.xctestrun \
  --simulator SIMULATOR_ID --result .build/layout-check.xcresult
```

Use the generated `.xctestrun` filename for the installed runtime.
Keep simulator code signing enabled because pairing uses the Keychain.

Evidence:

- [Blank transcript before the fix](../evidence/conversation-layout-before.png)
- [Typing after the fix](../evidence/conversation-layout-typing.png)
- [Two replies after the fix](../evidence/conversation-layout-after-send.png)
- [Passing UI test](../evidence/conversation-layout-test.log)

## Build 24

The signed archive contains this fix.
On September 10, 2026, the command-line upload stops with `Failed to Use Accounts` before uploading.
Xcode Organizer uploads build 24 as TestFlight Internal Only at 11:36 UTC.
Apple shows build 24 as Testing in the existing one-person group.
Check Xcode access before asking the user to unlock a device; TestFlight uploads do not require the iPhone.
Keep distribution restricted to the existing one-person TestFlight group.
