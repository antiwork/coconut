import ImageIO
import SwiftUI
import UniformTypeIdentifiers

// The app writes the chats that a share can go to, with their faces, for its share extension (ShareDirectory).

enum ShareDirectoryBuilder {
    /// The places that take a share, without their faces: first the projects, the Coconut project at the top, where a
    /// share starts a new chat as the message box does; then the chats that the lists show, newest activity first,
    /// each with its title and its project; then the chats of the active teams; then the people and the bots of those
    /// teams that this person can write to. An archived agent or chat takes none. `latest` is the date of the newest
    /// message of each chat (ChatSummaries.newestActivity).
    static func destinations(bots: [Bot], rooms: [TeamRoom], latest: [String: Date] = [:]) -> [ShareDestination] {
        let projects = bots.filter { $0.isProject && !$0.archived }
        var list = (projects.filter(\.coconut) + projects.filter { !$0.coconut }).map {
            ShareDestination(kind: .project, bot: $0.id, title: $0.name, detail: AgentChats.untitled)
        }
        func activity(_ bot: Bot) -> Date { latest[bot.id] ?? bot.createdAt.apiDate ?? .distantPast }
        list += bots.filter { AgentChats.isListed($0, latest: latest) }
            .sorted { (activity($0), $0.id) > (activity($1), $1.id) }
            .map { bot in
                ShareDestination(kind: .agent, bot: bot.id, project: bot.agentId, title: bot.isChat ? bot.chatLabel : bot.name,
                                 detail: bot.isChat ? bot.name : nil)
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

    /// The place of a share for a chat of an agent: its project, where a share starts a new chat, else the first chat
    /// of an agent of an earlier release, which takes no new chat. Nil when neither is in the list.
    static func placeId(of bot: String, bots: [Bot], among offered: Set<String>) -> String? {
        let agent = AgentChats.agentId(of: bot, in: bots)
        return ["project-" + agent, "agent-" + agent].first { offered.contains($0) }
    }

    /// The place that a share starts with: a new chat of the project that the person wrote to last, else of the
    /// Coconut project. Nil when neither is in the list.
    static func defaultId(bots: [Bot], messages: [Message], among destinations: [ShareDestination]) -> String? {
        let offered = Set(destinations.map(\.id))
        var newest: (date: Date, bot: String)?
        for message in messages where message.role == "user" {
            guard let date = message.createdAt.apiDate else { continue }
            if newest.map({ date > $0.date }) ?? true { newest = (date, message.botId) }
        }
        if let newest, let id = placeId(of: newest.bot, bots: bots, among: offered) { return id }
        return bots.first(where: \.coconut).flatMap { placeId(of: $0.id, bots: bots, among: offered) }
    }

    /// The key of a chat of a team from a place of the list, or nil for a project or an agent chat.
    static func chatKey(_ destination: ShareDestination) -> TeamChatKey? {
        guard destination.kind == .team || destination.kind == .direct, let room = destination.room else { return nil }
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
        case .project, .agent:
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
        var destinations = ShareDirectoryBuilder.destinations(bots: bots, rooms: rooms, latest: chatSummaries.newestActivity)
        guard hasPairing || isDemo, !destinations.isEmpty else { return nil }
        let defaultId = ShareDirectoryBuilder.defaultId(bots: bots, messages: messages, among: destinations)
        for index in destinations.indices {
            destinations[index].face = ShareFaces.face(of: destinations[index], rooms: rooms)
        }
        return ShareDirectory(destinations: destinations, defaultId: defaultId, endpoint: shareEndpoint,
                              macName: hasPairing ? pairedMacDescription : nil)
    }

    #if os(iOS)
    /// The person sent a message to a chat of an agent: its project shows in the row of people of the share sheet
    /// (ShareSuggestions). A chat of a project counts for the project, because a chat goes to Archived after two days
    /// and a share to the project starts a new chat.
    func suggestInShareSheet(_ bot: Bot) {
        suggestInShareSheet { destinations in
            ShareDirectoryBuilder.placeId(of: bot.id, bots: bots, among: Set(destinations.map(\.id)))
        }
    }

    /// The person sent a message to a chat of a team or to a person: that chat shows in the row of people of the share
    /// sheet.
    func suggestInShareSheet(_ chat: TeamChatKey) {
        let id = ShareDestination(kind: chat.isDirect ? .direct : .team, room: chat.room, chat: chat.chat,
                                  peer: chat.member ?? chat.bot, title: "").id
        suggestInShareSheet { _ in id }
    }

    private func suggestInShareSheet(_ place: ([ShareDestination]) -> String?) {
        // The preview data suggests nothing: its chats are not on a Mac.
        guard !isDemo, hasPairing, !TestIsolation.active else { return }
        let rooms = team?.rooms ?? []
        let destinations = ShareDirectoryBuilder.destinations(bots: bots, rooms: rooms, latest: chatSummaries.newestActivity)
        guard let id = place(destinations), var destination = destinations.first(where: { $0.id == id }) else { return }
        destination.face = ShareFaces.face(of: destination, rooms: rooms)
        ShareSuggestions.donate(destination)
    }
    #endif
}

/// Keeps the list of the share extension in step with the chats of the app. The list changes when a chat comes, goes
/// or gets a new name or face, and when the pairing changes. The app also writes it when it leaves the front, so the
/// order of the chats and the project of the newest message of the person are fresh for the next share, which comes
/// from another app; a new message alone writes nothing.
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
        let destinations = ShareDirectoryBuilder.destinations(bots: store.bots, rooms: store.team?.rooms ?? [],
                                                              latest: store.chatSummaries.newestActivity)
        return Outline(destinations: destinations.sorted { $0.id < $1.id }, faces: [],
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
        #if os(iOS)
        let previous = Set(ShareDirectoryStore.load()?.destinations.map(\.id) ?? [])
        #endif
        if let directory = store.shareDirectory() {
            ShareDirectoryStore.save(directory)
            #if os(iOS)
            // A place that left the list leaves the row of people of the share sheet too.
            ShareSuggestions.forget(previous.subtracting(directory.destinations.map(\.id)))
            #endif
        } else if !store.hasPairing {
            // A paired app without a chat has not read its chats yet, so the last list stays.
            ShareDirectoryStore.save(nil)
            #if os(iOS)
            ShareSuggestions.forgetAll()
            #endif
        }
    }
}
