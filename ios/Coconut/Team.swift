import SwiftUI
#if canImport(UIKit)
import UIKit
#else
import AppKit
#endif

/// GET /v1/team/rooms (backend/coconut/team.py). A room is a team. `helper` is false when the Mac cannot do the
/// cryptography of a team. Each field that an older service can leave out is optional.
struct TeamRooms: Decodable, Equatable, Sendable {
    var rooms: [TeamRoom]
    var helper: Bool? = nil
}

struct TeamRoom: Decodable, Identifiable, Equatable, Sendable {
    enum Status: Equatable, Sendable {
        case active, joining, removed, gone
    }

    var room: String
    var name: String
    /// "joining", "active", "removed" or "gone".
    var state: String
    var isAdmin: Bool? = nil
    var canInvite: Bool? = nil
    var members: [TeamMember]? = nil
    var bots: [TeamRoomBot]? = nil
    var sync: TeamSync? = nil
    /// The time when this Mac made or joined the team.
    var createdAt: String? = nil
    /// The unread count of the Team chat, 0 to 99. An older service has no chat and sends none.
    var unread: Int? = nil
    var lastMessage: TeamLastMessage? = nil

    var id: String { room }

    /// The Mac serves the Team chat of this team. An older service has no `unread`.
    var hasChat: Bool { unread != nil }
    /// The member of this Mac.
    var ownMember: TeamMember? { memberList.first { $0.thisMac == true } }
    /// The newest message, or the time when this Mac made or joined the team. The Chats list sorts by it.
    var lastActivity: Date { lastMessage?.createdAt?.apiDate ?? createdAt?.apiDate ?? .distantPast }

    /// A state that this app does not know shows as an active team, without the actions that need one.
    var status: Status {
        switch state {
        case "joining": .joining
        case "removed": .removed
        case "gone": .gone
        default: .active
        }
    }

    /// Only the admin of an active team can make an invite link. The service says so in `can_invite`.
    var showsInvite: Bool { status == .active && canInvite == true }
    var memberList: [TeamMember] { members ?? [] }
    var botList: [TeamRoomBot] { bots ?? [] }
    /// The IDs of the bots of this Mac that are in the team.
    var botsOfThisMac: Set<String> { Set(botList.compactMap { $0.thisMac == true ? $0.botId : nil }) }
}

struct TeamMember: Decodable, Identifiable, Equatable, Sendable {
    var member: String
    var name: String
    /// The face that the member chose: a symbol of a bot face and its color. Empty when the member chose none.
    var avatar: String? = nil
    var color: String? = nil
    var admin: Bool? = nil
    var thisMac: Bool? = nil
    /// True for a Mac that runs Coconut for a team only: no person is at it. An older service sends none, and none is
    /// false.
    var botOnly: Bool? = nil

    var id: String { member }
    var isBotOnly: Bool { botOnly == true }
}

/// A bot that is in a team. `botId` is the ID of a bot of this Mac. A bot of another Mac has none.
struct TeamRoomBot: Decodable, Identifiable, Equatable, Sendable {
    var id: String
    var name: String
    var avatar: String? = nil
    var color: String? = nil
    var memberName: String? = nil
    var thisMac: Bool? = nil
    var botId: String? = nil
    /// True for a bot that answers each member of the team: a bot of a Mac without a person. A bot of a person
    /// answers only that person. An older service sends none, and none is false.
    var open: Bool? = nil

    var isOpen: Bool { open == true }
}

struct TeamSync: Decodable, Equatable, Sendable {
    /// One sentence for a person, or nil.
    var error: String? = nil
}

/// The answer of each change of one room.
struct TeamRoomAnswer: Decodable, Sendable {
    var room: TeamRoom
}

struct TeamInvite: Decodable, Sendable {
    var link: String
}

struct TeamLeaveAnswer: Decodable, Sendable {
    var room: String
}

/// What the Team screen shows, from the state of the store.
enum TeamScreen: Equatable {
    case unpaired
    case loading
    case offline
    /// The first load failed, with the text of the Mac or of the app.
    case failed(String)
    /// The Mac cannot do the cryptography of a team.
    case updateMac
    case empty
    case team(TeamRoom)
    /// The app is for one team. A Mac that a tool put into more teams shows a list first.
    case teams([TeamRoom])

    init(paired: Bool, connection: ConnectionState, rooms: TeamRooms?, error: String?) {
        guard paired else { self = .unpaired; return }
        if let rooms {
            if rooms.helper == false { self = .updateMac }
            else if rooms.rooms.isEmpty { self = .empty }
            else if rooms.rooms.count == 1 { self = .team(rooms.rooms[0]) }
            else { self = .teams(rooms.rooms) }
            return
        }
        // The banner of the connection says why. The failure of a load without a connection says nothing more.
        switch connection {
        case .disconnected, .macOffline, .relayUnreachable, .needsRepair: self = .offline
        default: self = error.map(TeamScreen.failed) ?? .loading
        }
    }
}

/// An invite link that the person tapped or pasted. It is a secret for one day, so it stays in memory only.
struct TeamJoinRequest: Identifiable, Equatable {
    let id = UUID()
    let link: String
}

/// The invite of a link, for the join sheet: the name of the team, or the reason why the link does not work. The
/// texts are the ones of the service for the same link (backend/coconut/team.py, `team_join`).
enum TeamLinkCheck: Equatable {
    case team(String)
    case invalid(String)

    init(link: String, now: Date = .now) {
        do {
            let invite = try TeamLog.invite(link: link.trimmingCharacters(in: .whitespacesAndNewlines))
            if TimeInterval(invite.expires) <= now.timeIntervalSince1970 {
                self = .invalid("The invite link is past its end. Ask for a new link.")
            } else {
                self = .team(invite.name)
            }
        } catch TeamLog.Failure.unknownVersion {
            self = .invalid("A newer Coconut made this invite link. Update Coconut, then join again.")
        } catch {
            self = .invalid("This is not the invite link of a team. Check the link, then try again.")
        }
    }

    static func isLink(_ url: URL) -> Bool { url.scheme?.lowercased() == "coconut" && url.host?.lowercased() == "team" }
}

/// The pairing pages on screen: the first run, "Pair with your Mac" in Settings, and the Mac window. Each page counts
/// itself, so a view can wait until the last page closes, whichever view opened it.
@MainActor @Observable
final class PairingPages {
    static let shared = PairingPages()
    private(set) var count = 0
    var isOpen: Bool { count > 0 }

    func opened() { count += 1 }
    func closed() { count = max(0, count - 1) }
}

/// When the join sheet of a tapped invite link can show: a link waits, its sheet does not show yet, and no pairing
/// page covers the screen. A pairing page that closes, also one that Settings opened, lets the sheet show.
enum TeamJoinGate {
    static func canShow(_ request: TeamJoinRequest?, showing: UUID?, pairingOpen: Bool) -> Bool {
        guard let request else { return false }
        return request.id != showing && !pairingOpen
    }
}

/// The name that the team sees for this person. The app fills it in for the next team, so the person types it once.
enum TeamMemberName {
    static let key = "teamMemberName"

    static func remembered(_ defaults: UserDefaults = .standard) -> String { defaults.string(forKey: key) ?? "" }

    static func remember(_ name: String, _ defaults: UserDefaults = .standard) {
        let name = name.trimmingCharacters(in: .whitespacesAndNewlines)
        if !name.isEmpty { defaults.set(name, forKey: key) }
    }
}

/// The share sheet of the system for a value that the app gets only after a tap, such as an invite link. ShareLink
/// needs its item before the tap.
enum SystemShare {
    @MainActor static func present(_ url: URL) {
        #if canImport(UIKit)
        let scene = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
            .first { $0.activationState == .foregroundActive }
        guard var top = scene?.keyWindow?.rootViewController else { return }
        while let presented = top.presentedViewController, !presented.isBeingDismissed { top = presented }
        let sheet = UIActivityViewController(activityItems: [ShareItem(url: url)], applicationActivities: nil)
        // An iPad shows the sheet as a popover, which needs a place.
        sheet.popoverPresentationController?.sourceView = top.view
        sheet.popoverPresentationController?.sourceRect = CGRect(x: top.view.bounds.midX, y: top.view.bounds.midY,
                                                                 width: 0, height: 0)
        top.present(sheet, animated: true)
        #else
        guard let view = NSApp.keyWindow?.contentView else { return }
        // The picker opens at the pointer, which is on the row that the person clicked.
        let point = view.convert(view.window?.mouseLocationOutsideOfEventStream ?? .zero, from: nil)
        NSSharingServicePicker(items: [url]).show(relativeTo: NSRect(origin: point, size: .zero), of: view,
                                                  preferredEdge: .minY)
        #endif
    }

    #if canImport(UIKit)
    /// A link for each activity. Copy puts it as text, so a paste works in each text field and in the paste control of
    /// the join sheet.
    private final class ShareItem: NSObject, UIActivityItemSource {
        let url: URL

        init(url: URL) { self.url = url }

        func activityViewControllerPlaceholderItem(_ controller: UIActivityViewController) -> Any { url }

        func activityViewController(_ controller: UIActivityViewController,
                                    itemForActivityType type: UIActivity.ActivityType?) -> Any? {
            type == .copyToPasteboard ? url.absoluteString : url
        }
    }
    #endif
}
