# Share sheet

Coconut is a recipient of the system share sheet on the iPhone and on the Mac. A share holds text, links, images, PDFs or other files. The person picks a chat, adds an optional note, and sends. The share becomes one message of that chat, with its files as attachments.

The sheet opens on the chat of the last share. Before the first share it opens on the agent that the person wrote to last, else on the Coconut chat.

## Parts

| Part | Where |
| --- | --- |
| The message of a share, and the limits | `ios/ShareShared/ShareItems.swift` |
| The chats that take a share | `ios/ShareShared/ShareDirectory.swift`, written by `ios/Coconut/ShareDirectoryPublisher.swift` |
| The sheet | `ios/ShareShared/ShareComposeView.swift`, `ShareModel.swift` |
| The iPhone extension `com.gumroad.coconut.share` | `ios/CoconutShare`, sends with `ios/ShareShared/ShareSender.swift` |
| The Mac extension `com.gumroad.coconut.mac.share` | `ios/CoconutMacShare`, hands off with `ios/ShareShared/ShareHandoff.swift` |
| The Mac app sends a share | `mac/Sources/CoconutMac/MacShareInbox.swift` |

## Rules of a message

- The note comes first, then an empty line, then each shared text and link on its own line.
- A text that only repeats a link of the share goes once. A browser gives the address of a page as both.
- A message takes at most 4 files of at most 4 MB each. An image becomes smaller first (`FileFit`). An image for a team leaves without its GPS position.
- A direct chat takes text and links only.

## iPhone: the extension sends

The extension sends through the relay tunnel itself, so the app does not open. It uses the pairing of the app:

- The app writes the list of the chats, their faces and the relay route into one Keychain item of the group `com.gumroad.coconut.push`. This is the group that the notification extension uses. There is no app group.
- The extension reads the device token and the tunnel key from the Keychain items of the app. Its entitlements name the group `com.gumroad.coconut` for that. It never writes them.
- The extension opens its own tunnel socket. The relay allows several sockets of one phone.

The extension sends the same requests as the composers of the app: `POST /v1/attachments` then `POST /v1/bots/<id>/messages` for an agent, and `POST /v1/team/rooms/<room>/files` then `.../messages` for a chat of a team.

The app writes the list while it runs. After an install, the person opens Coconut once before the first share.

## Mac: the extension hands off to the app

macOS loads an app extension only with a sandbox. Coconut.app has no sandbox and keeps its pairing in the login Keychain, which a sandboxed extension cannot read. So the extension does not send.

- Both sides use the folder `~/Library/Application Support/Coconut/Share`. The sandbox of the extension has an exception for this one folder (`CoconutMacShare.entitlements`).
- The app writes `directory.json` there: the chats and their faces.
- The extension writes a share as one folder under `inbox/`, then opens `coconut://share`. That starts the app or brings it to the front.
- The app sends each waiting share when it is connected to the paired Mac, and shows the chat. A share that waited for 24 hours is dropped.

`scripts/mac_app.py` copies the extension into `Contents/PlugIns` and signs it with its sandbox, also in an ad-hoc build.

## Signing

- iPhone: the extension has the new App ID `com.gumroad.coconut.share`. It needs no capability beyond Keychain sharing, which needs no registration. The export with `-allowProvisioningUpdates` makes the App ID and its profile. `scripts/testflight.py` signs the extension with its entitlements.
- Mac: no new identity and no profile. The extension is signed with the Developer ID of the app.
