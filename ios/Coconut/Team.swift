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

    /// True when a message of at least one team notifies this phone. The sentence of the notification settings reads it.
    var notifiesPhone: Bool { rooms.contains(where: \.notifiesPhone) }

    /// True when an active team has a Mac that serves direct messages. A direct message then notifies this phone. The
    /// sentence of the notification settings reads it.
    var notifiesDirect: Bool { rooms.contains { $0.status == .active && $0.directs != nil } }
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
    /// The unread count of the Team chat, 0 to `maxUnread`. An older service has no chat and sends none.
    var unread: Int? = nil
    /// The highest unread count of a chat of a team, also of a direct chat: the service stops its count there
    /// (`MAX_UNREAD` in backend/coconut/team_store.py). A count at this value can hide a new message.
    static let maxUnread = 99
    var lastMessage: TeamLastMessage? = nil
    /// True while an unread message of the Team chat names this person. An older service sends none: it does not
    /// know a mention of a person (TeamRoom.knowsPersonMentions).
    var mentioned: Bool? = nil
    /// The `first` of the newest unread message that names this person. Nil when none does, and for an older service.
    /// The Mac banner of a quiet team reads it (MessageBanners).
    var mentionFirst: Int? = nil
    /// Whether a new message of the team notifies this phone. It is a choice of the person on this Mac. A Mac for a
    /// team only and an older service send none, and the Team screen then has no switch.
    var notifications: Bool? = nil
    /// The direct chats of this person in the team, newest first. Only a service that serves direct messages sends it,
    /// for an app that asks with the header `X-Coconut-Team: direct`.
    var directs: [TeamDirect]? = nil
    /// True when this person can delete a message of another person or of a bot in the Team chat: the Mac is the
    /// admin of an active team (TeamMessageDeletion). An older service sends none, and none is false.
    var canDeleteMessages: Bool? = nil
    /// Who types in the Team chat now (docs/team-log.md, "Typing"). An older service sends none.
    var typing: TeamTypingList? = nil

    var id: String { room }

    /// A message of this team notifies this phone: the team is active and its switch is on. A team of an older
    /// service has no switch and does not notify.
    var notifiesPhone: Bool { status == .active && notifications == true }

    /// The Mac serves the Team chat of this team. An older service has no `unread`.
    var hasChat: Bool { unread != nil }
    /// The Mac knows a mention of a person: it notifies a named person also in a quiet team. Its sign is `mentioned`,
    /// which an older service does not send. The `@` list and the footer of the switch read it.
    var knowsPersonMentions: Bool { mentioned != nil }
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
    /// The people of the team: each member that is not a Mac for a team only. This is the one place of that rule.
    /// The section "Members", the count in the list of teams and the line of who is in the chat read it.
    var people: [TeamMember] { memberList.filter { !$0.isBotOnly } }
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
    /// True when this person can send the member a direct message now (TeamRoom.directMember). An older service sends
    /// none, and none is false.
    var direct: Bool? = nil
    /// True when this person can remove the member: the Mac is the admin of an active team, and the member is another
    /// Mac (TeamRemoval). An older service sends none, and none is false.
    var canRemove: Bool? = nil

    var id: String { member }
    var isBotOnly: Bool { botOnly == true }
}

/// The admin takes a member out of the team (backend/coconut/team.py, `team_remove`): which rows of the Team screen
/// have the action, and the texts of the one confirmation. A person and a Mac for a team only are both members.
struct TeamRemoval: Identifiable, Equatable {
    let member: TeamMember
    let team: String
    /// The names of the bots of a Mac for a team only, in the order of the Team screen. A person has none here: the
    /// bots of a person are only the authors of the pages that the person shares.
    let bots: [String]

    var id: String { member.member }

    /// The removal of a member of the room, or nil when this person cannot remove it. `can_remove` of the service is
    /// the one rule, so an older service shows no action.
    init?(member id: String?, in room: TeamRoom) {
        guard room.status == .active, let member = room.memberList.first(where: { $0.member == id }),
              member.canRemove == true, member.thisMac != true else { return nil }
        self.member = member
        team = room.name
        bots = member.isBotOnly
            ? room.botList.filter { $0.member == member.member }.map(\.name)
                .sorted { $0.localizedStandardCompare($1) == .orderedAscending }
            : []
    }

    var title: String { "Remove \(member.name) from \(team)?" }

    var message: String {
        let first = "\(member.name) can no longer read or write in this team."
        if !member.isBotOnly { return first + " The pages that \(member.name) shared leave the Library." }
        switch bots.count {
        case 0: return first
        case 1: return first + " Its bot \(bots[0]) leaves the team with it."
        default: return first + " Its bots \(bots.formatted(.list(type: .and))) leave the team with it."
        }
    }
}

/// A bot that is in a team. `botId` is the ID of a bot of this Mac. A bot of another Mac has none.
struct TeamRoomBot: Decodable, Identifiable, Equatable, Sendable {
    var id: String
    var name: String
    var avatar: String? = nil
    var color: String? = nil
    /// The member ID of the Mac of the bot. An older service sends none.
    var member: String? = nil
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
