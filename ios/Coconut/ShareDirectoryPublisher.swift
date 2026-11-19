import ImageIO
import SwiftUI
import UniformTypeIdentifiers

// The app writes the chats that a share can go to, with their faces, for its share extension (ShareDirectory).

enum ShareDirectoryBuilder {
    /// The chats that take a share, without their faces: first the agents, the Coconut chat at the top, then the
    /// chats of the active teams, then the people and the bots of those teams that this person can write to. An
    /// archived agent takes none. `bots` keeps its order.
    static func destinations(bots: [Bot], rooms: [TeamRoom]) -> [ShareDestination] {
        let agents = bots.filter { !$0.archived }
        var list = (agents.filter(\.coconut) + agents.filter { !$0.coconut }).map {
            ShareDestination(kind: .agent, bot: $0.id, title: $0.name)
        }
        let active = rooms.filter { $0.status == .active && $0.hasChat }
        for room in active {
            list.append(ShareDestination(kind: .team, room: room.room, title: room.name))
            list += room.namedChats.map {
                ShareDestination(kind: .team, room: room.room, chat: $0.id, title: $0.name, detail: room.name)
            }
        }
        for room in active {
            list += room.memberList.filter { room.directMember($0.member) != nil }.map {
                ShareDestination(kind: .direct, room: room.room, peer: $0.member, title: $0.name, detail: room.name)
            }
            list += room.botList.filter { room.directBot($0.id) != nil }.map {
                ShareDestination(kind: .direct, room: room.room, peer: $0.id, peerIsBot: true, title: $0.name,
                                 detail: room.name)
            }
        }
        return list
    }

    /// The chat that a share starts with: the agent that the person wrote to last, else the Coconut chat. Nil when
    /// neither is in the list.
    static func defaultId(bots: [Bot], messages: [Message], among destinations: [ShareDestination]) -> String? {
        let offered = Set(destinations.map(\.id))
        var newest: (date: Date, bot: String)?
        for message in messages where message.role == "user" {
            guard let date = message.createdAt.apiDate, offered.contains("agent-" + message.botId) else { continue }
            if newest.map({ date > $0.date }) ?? true { newest = (date, message.botId) }
        }
        if let newest { return "agent-" + newest.bot }
        return bots.first { $0.coconut && offered.contains("agent-" + $0.id) }.map { "agent-" + $0.id }
    }

    /// The key of a chat of a team from a chat of the list, or nil for an agent chat.
    static func chatKey(_ destination: ShareDestination) -> TeamChatKey? {
        guard destination.kind != .agent, let room = destination.room else { return nil }
        guard destination.kind == .direct else { return TeamChatKey(room: room, chat: destination.chat) }
        return destination.peerIsBot == true ? TeamChatKey(room: room, bot: destination.peer)
            : TeamChatKey(room: room, member: destination.peer)
    }
}

/// Draws the face of each chat as the lists of the app show it, once for each look of a face.
@MainActor
enum ShareFaces {
    static let size: CGFloat = 40
    private static var cache: [String: Data] = [:]

    static func face(of destination: ShareDestination, rooms: [TeamRoom]) -> Data? {
        let room = rooms.first { $0.room == destination.room }
        switch destination.kind {
        case .agent:
            // A project has no face.
            return nil
        case .team:
            guard let room else { return nil }
            return png("team \(room.room) \(room.name)") { TeamFace(room: room, size: size) }
        case .direct:
            guard let room, let peer = destination.peer else { return nil }
            // A bot has no face.
            if destination.peerIsBot == true { return nil }
            let member = room.memberList.first { $0.member == peer }
            return png("person \(peer) \(member?.name ?? "") \(member?.avatar ?? "") \(member?.color ?? "")") {
                PersonFace(member: member, memberId: peer, name: member?.name, size: size)
            }
        }
    }

    private static func png(_ key: String, @ViewBuilder _ view: () -> some View) -> Data? {
        if let data = cache[key] { return data }
        let renderer = ImageRenderer(content: view().environment(\.colorScheme, .light))
        renderer.scale = 2
        guard let image = renderer.cgImage else { return nil }
        let buffer = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(buffer, UTType.png.identifier as CFString, 1, nil) else { return nil }
        CGImageDestinationAddImage(destination, image, nil)
        guard CGImageDestinationFinalize(destination) else { return nil }
        cache[key] = buffer as Data
        return buffer as Data
    }
}

extension AppStore {
    /// The list for the share extension as the app has it now, with the faces.
    func shareDirectory() -> ShareDirectory? {
        let rooms = team?.rooms ?? []
        var destinations = ShareDirectoryBuilder.destinations(bots: bots, rooms: rooms)
        guard hasPairing || isDemo, !destinations.isEmpty else { return nil }
        let defaultId = ShareDirectoryBuilder.defaultId(bots: bots, messages: messages, among: destinations)
        for index in destinations.indices {
            destinations[index].face = ShareFaces.face(of: destinations[index], rooms: rooms)
        }
        return ShareDirectory(destinations: destinations, defaultId: defaultId, endpoint: shareEndpoint,
                              macName: hasPairing ? pairedMacDescription : nil)
    }
}

/// Keeps the list of the share extension in step with the chats of the app. The list changes when a chat comes, goes
/// or gets a new name or face, and when the pairing changes. The app also writes it when it leaves the front, so the
/// chat of the newest message of the person is the first choice of the next share.
struct ShareDirectoryPublisher: ViewModifier {
    @Environment(AppStore.self) private var store
    @Environment(\.scenePhase) private var scenePhase

    private struct Outline: Hashable {
        let destinations: [ShareDestination]
        let faces: [String]
        let paired: Bool
        let endpoint: String?
    }

    private var outline: Outline {
        Outline(destinations: ShareDirectoryBuilder.destinations(bots: store.bots, rooms: store.team?.rooms ?? []),
                faces: [],
                paired: store.hasPairing, endpoint: store.shareEndpoint?.route)
    }

    func body(content: Content) -> some View {
        content
            .task(id: outline) { publish() }
            .onChange(of: scenePhase) { _, phase in if phase != .active { publish() } }
    }

    private func publish() {
        // A test run and the preview data write nothing: the list is for the chats of the paired Mac.
        guard !store.isDemo, !TestIsolation.active else { return }
        if let directory = store.shareDirectory() {
            ShareDirectoryStore.save(directory)
        } else if !store.hasPairing {
            // A paired app without a chat has not read its chats yet, so the last list stays.
            ShareDirectoryStore.save(nil)
        }
    }
}
