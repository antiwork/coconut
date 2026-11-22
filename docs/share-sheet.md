# Share sheet

Coconut is a recipient of the system share sheet on the iPhone and on the Mac. A share holds text, links, images, PDFs or other files. The person picks a place, adds an optional note, and sends. The share becomes one message, with its files as attachments.

The places, in the sections of the list:

- **Projects** (Coconut, Gumroad, Personal), each with the line New agent: a share starts a new agent of the project (a new chat with its agent), as the first message of the message box does, so a link or a screenshot with feedback becomes its own task.
- **Agents**: the agents that the lists of the app show, newest activity first, with their titles and their projects. A share goes into that conversation.
- **Teams** and **Direct messages**: the chats of the active teams, and the people and the bots that this person can write to.

The sheet opens on the place that the person tapped in the row of people of the share sheet, else on the place of the last share. Before the first share it opens on a new agent of the project that the person wrote to last, else of the Coconut project.

## Parts

| Part | Where |
| --- | --- |
| The message of a share, and the limits | `ios/ShareShared/ShareItems.swift` |
| The places that take a share | `ios/ShareShared/ShareDirectory.swift`, written by `ios/Coconut/ShareDirectoryPublisher.swift` |
| The sheet | `ios/ShareShared/ShareComposeView.swift`, `ShareModel.swift` |
| The row of people of the iPhone share sheet | `ios/ShareShared/ShareSuggestions.swift` |
| The iPhone extension `com.gumroad.coconut.share` | `ios/CoconutShare`, sends with `ios/ShareShared/ShareSender.swift` |
| The Mac extension `com.gumroad.coconut.mac.share` | `ios/CoconutMacShare`, hands off with `ios/ShareShared/ShareHandoff.swift` |
| The Mac app sends a share | `mac/Sources/CoconutMac/MacShareInbox.swift` |

## Rules of a message

- The note comes first, then an empty line, then each shared text and link on its own line.
- A text that only repeats a link of the share goes once. A browser gives the address of a page as both.
- A message takes at most 4 files of at most 4 MB each. An image becomes smaller first (`FileFit`). An image for a team leaves without its GPS position.
- A direct chat takes text and links only.

## iPhone: the row of people

The top row of the iPhone share sheet suggests people and chats, Coconut's beside those of Messages. A tap on one opens the extension with that place chosen, so a share to the Coconut project is two taps: the suggestion, then Send.

- iOS suggests the places that the person sends to often. The app tells iOS about each message of the person (`INInteraction.donate` of an `INSendMessageIntent`): its project for a message in a chat of a project, the chat for a message in a chat of a team or to a person. A chat of a project counts for its project, because the chat goes to Archived after two days and the project stays. The extension tells iOS about each share the same way.
- The intent carries the ID of the place (`conversationIdentifier`, for example `project-<bot id>`), its title, and its picture: the face of a team or a person, the first letter of a project in white on the brown of the app icon, else the first letter on a fill of the faces.
- The app declares `INSendMessageIntent` in `NSUserActivityTypes`; without it iOS refuses each donation (error 1901). The extension declares it in `IntentsSupported`, and reads it from `extensionContext.intent`.
- A place that leaves the list of the app leaves the row too (`INInteraction.delete` by the ID of the place), and an app without a pairing clears the row.
- A tap on a suggested project outside the share sheet, for example in Search, opens the message box on that project.
- The simulator shows no suggestions. Check the row on a phone, after a few messages in the app. A donation from the simulator works only from a build whose code identity is the bundle ID: an unsigned build is `Coconut`, and Spotlight refuses it (error 1901 with CSIndex -1003, "Invalid bundle ID" in the log of searchd). Sign a copy with `codesign --force --sign - --identifier com.gumroad.coconut Coconut.app` to check a donation there.

## iPhone: the extension sends

The extension sends through the relay tunnel itself, so the app does not open. It uses the pairing of the app:

- The app writes the list of the places, their faces and the relay route into one Keychain item of the group `com.gumroad.coconut.push`. This is the group that the notification extension uses. There is no app group.
- The extension reads the device token and the tunnel key from the Keychain items of the app. Its entitlements name the group `com.gumroad.coconut` for that. It never writes them.
- The extension opens its own tunnel socket. The relay allows several sockets of one phone.

The extension sends the same requests as the composers of the app: `POST /v1/attachments` then `POST /v1/bots/<id>/messages` for an agent, and `POST /v1/team/rooms/<room>/files` then `.../messages` for a chat of a team. A share to a project asks for a new chat first (`POST /v1/bots/<project>/chats`), with one Idempotency-Key for each try of the share, so a try whose answer was lost gets the same chat back.

The app writes the list when a place comes, goes or changes its name, and when it leaves the front, so the order of the chats is fresh for a share from another app. After an install, the person opens Coconut once before the first share.

## Mac: the extension hands off to the app

macOS loads an app extension only with a sandbox. Coconut.app has no sandbox and keeps its pairing in the login Keychain, which a sandboxed extension cannot read. So the extension does not send.

- Both sides use the folder `~/Library/Application Support/Coconut/Share`. The sandbox of the extension has an exception for this one folder (`CoconutMacShare.entitlements`).
- The app writes `directory.json` there: the places and their faces.
- The extension writes a share as one folder under `inbox/`, then opens `coconut://share`. That starts the app or brings it to the front.
- The app sends each waiting share when it is connected to the paired Mac, and shows the chat: for a project, the new chat (`chatForFirstMessage`). A share that waited for 24 hours is dropped.

`scripts/mac_app.py` copies the extension into `Contents/PlugIns` and signs it with its sandbox, also in an ad-hoc build.

## Signing

- iPhone: the extension has the new App ID `com.gumroad.coconut.share`. It needs no capability beyond Keychain sharing, which needs no registration. The export with `-allowProvisioningUpdates` makes the App ID and its profile. `scripts/testflight.py` signs the extension with its entitlements. The suggestions need no capability: a donation of `INSendMessageIntent` needs no Siri entitlement.
- Mac: no new identity and no profile. The extension is signed with the Developer ID of the app.
