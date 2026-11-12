import XCTest
import Network
@testable import Coconut

/// The direct messages of the app (docs/team-log.md, "Direct messages"): the key of a chat, the rows of the Chats list,
/// the arrow of the Team screen, the notifications, the cache of unsent messages, and one store path for both chats.
final class DirectMessageTests: XCTestCase {
    private let roomId = String(repeating: "ab", count: 32)
    private let graceId = String(repeating: "cd", count: 32)
    private let device = "00000000-0000-4000-8000-0000000000d1"

    private func member(_ id: String, _ name: String, direct: Bool? = nil, thisMac: Bool = false,
                        botOnly: Bool? = nil) -> TeamMember {
        TeamMember(member: id, name: name, thisMac: thisMac, botOnly: botOnly, direct: direct)
    }

    private func room(_ id: String = "r1", members: [TeamMember] = [], directs: [TeamDirect]? = nil,
                      last: String? = nil) -> TeamRoom {
        TeamRoom(room: id, name: "Example Team", state: "active", members: members, createdAt: "2026-11-02T00:00:00Z",
                 unread: 0, lastMessage: last.map { TeamLastMessage(text: "Hi", createdAt: $0) }, directs: directs)
    }

    private func direct(_ member: String, name: String = "Grace", at: String?, own: Bool = false,
                        unread: Int = 0) -> TeamDirect {
        TeamDirect(member: member, memberName: name, unread: unread,
                   lastMessage: at.map { TeamLastMessage(first: 1, thisMac: own, text: "See you at 3", createdAt: $0) })
    }

    private func bot(_ id: String, pinned: Bool = false) -> Bot {
        Bot(id: id, sessionId: "session-\(id)", name: id, role: "", avatar: "sparkle", color: "777777",
            instructions: "", status: "idle", lastMessage: "", unreadCount: 0, archived: false,
            createdAt: "2026-11-01T00:00:00Z", pinned: pinned)
    }

    // MARK: The key and the paths

    func testTheKeyOfAChatNamesThePathOfEachRequest() {
        let team: TeamChatKey = "r1"
        XCTAssertEqual(team, TeamChatKey(room: "r1"))
        XCTAssertFalse(team.isDirect)
        XCTAssertEqual(team.path, "team/rooms/r1")
        let direct = TeamChatKey(room: "r1", member: "m-grace")
        XCTAssertTrue(direct.isDirect)
        XCTAssertEqual(direct.path, "team/rooms/r1/directs/m-grace")
        XCTAssertNotEqual(team, direct, "The Team chat and a direct chat of one room are two chats.")
        XCTAssertNotEqual(direct, TeamChatKey(room: "r1", member: "m-lin"))
    }

    func testTheListOfTheTeamsAsksForDirectMessages() throws {
        let client = APIClient.unconnected(token: "fixture-token")
        XCTAssertEqual(try client.makeTunnelRequest("team/rooms").headers["X-Coconut-Team"], "direct")
        XCTAssertNil(try client.makeTunnelRequest("team/rooms", method: "POST", body: Data("{}".utf8))
            .headers["X-Coconut-Team"], "A write does not ask.")
        XCTAssertNil(try client.makeTunnelRequest("team/rooms/r1/messages").headers["X-Coconut-Team"])
    }

    // MARK: Decode and an older service

    func testTheListDecodesTheMarkOfEachMemberAndTheDirectChats() throws {
        let answer = #"{"rooms": [{"room": "r1", "name": "Example Team", "state": "active", "unread": 0,"#
            + #" "members": [{"member": "m-ada", "name": "Ada", "this_mac": true, "direct": false},"#
            + #" {"member": "m-grace", "name": "Grace", "direct": true}],"#
            + #" "directs": [{"member": "m-grace", "member_name": "Grace", "unread": 2, "last_message":"#
            + #" {"first": 7, "this_mac": false, "text": "See you at 3", "created_at": "2026-11-10T12:00:00Z"}},"#
            + #" {"member": "m-old", "member_name": "Lin", "unread": 0, "last_message": null}]}]}"#
        let rooms = try APIJSON.decoder().decode(TeamRooms.self, from: Data(answer.utf8))
        let room = try XCTUnwrap(rooms.rooms.first)
        XCTAssertEqual(room.memberList.map(\.direct), [false, true])
        XCTAssertEqual(room.directList.map(\.member), ["m-grace", "m-old"])
        XCTAssertEqual(room.directList.first?.unread, 2)
        XCTAssertEqual(room.directList.first?.lastMessage?.text, "See you at 3")
        XCTAssertTrue(rooms.notifiesDirect)
        // A member that left keeps the last name of the Mac, and the chat cannot send.
        XCTAssertEqual(room.directPeer("m-old"), TeamDirectPeer(member: "m-old", entry: nil, name: "Lin", canReceive: false))
        XCTAssertEqual(room.directPeer("m-old").closedLine, "Lin is no longer in this team.")
        XCTAssertTrue(room.directPeer("m-grace").canReceive)
    }

    func testAnOlderServiceShowsNothingOfDirectMessages() throws {
        let older = #"{"rooms": [{"room": "r1", "name": "Example Team", "state": "active", "unread": 0,"#
            + #" "members": [{"member": "m-ada", "name": "Ada", "this_mac": true},"#
            + #" {"member": "m-grace", "name": "Grace"}]}]}"#
        let rooms = try APIJSON.decoder().decode(TeamRooms.self, from: Data(older.utf8))
        let room = try XCTUnwrap(rooms.rooms.first)
        XCTAssertNil(room.directMember("m-grace"), "No arrow and no action on a name.")
        XCTAssertEqual(ChatList.people(rooms.rooms).map(\.id), ["team-r1"], "No row of a direct chat.")
        XCTAssertFalse(rooms.notifiesDirect)
    }

    // MARK: Who has an arrow

    func testOnlyAnotherPersonWithTheMarkCanGetADirectMessage() {
        let room = room(members: [member("m-ada", "Ada", direct: false, thisMac: true),
                                  member("m-grace", "Grace", direct: true),
                                  member("m-lin", "Lin", direct: true),
                                  member("m-noor", "Noor"),
                                  member("m-mac", "Reporter Mac", direct: true, botOnly: true),
                                  member("m-self", "Me again", direct: true, thisMac: true)])
        XCTAssertEqual(room.memberList.filter { room.directMember($0.member) != nil }.map(\.name), ["Grace", "Lin"])
        XCTAssertNil(room.directMember(nil), "A message without a member, such as one of a bot.")
        XCTAssertNil(room.directMember("m-gone"), "A member that left.")
    }

    // MARK: The Chats list

    func testDirectChatsSortWithTheTeamsAndTheBotsByTheirNewestMessage() {
        let rooms = [room(members: [member("m-grace", "Grace", direct: true)],
                          directs: [direct("m-grace", at: "2026-11-10T09:00:00Z"),
                                    direct("m-lin", name: "Lin", at: "2026-11-03T00:00:00Z")],
                          last: "2026-11-05T00:00:00Z")]
        let latest = ["planner": "2026-11-07T00:00:00Z".apiDate!]
        let items = ChatList.items(bots: [bot("planner"), bot("writer")], latest: latest, rooms: rooms)
        XCTAssertEqual(items.map(\.id), ["direct-r1-m-grace", "bot-planner", "team-r1", "direct-r1-m-lin", "bot-writer"])
        XCTAssertEqual(items.first?.chat, TeamChatKey(room: "r1", member: "m-grace"))
        XCTAssertEqual(items[2].chat, "r1")
        XCTAssertNil(items[1].chat)
    }

    func testPeopleHasTheTeamsAndTheDirectChatsAndBotsHasNone() {
        let room = room(directs: [direct("m-grace", at: "2026-11-10T09:00:00Z")])
        let items: [ChatListItem] = [.direct(room, room.directList[0]), .bot(bot("planner")), .team(room)]
        XCTAssertEqual(ChatFilter.people.items(items).map(\.id), ["direct-r1-m-grace", "team-r1"])
        XCTAssertEqual(ChatFilter.bots.items(items).map(\.id), ["bot-planner"])
        XCTAssertEqual(ChatFilter.all.items(items).map(\.id), items.map(\.id))
    }

    func testTheSectionPeopleOfTheMacListsTheDirectChats() {
        let rooms = [room("r1", directs: [direct("m-grace", at: "2026-11-10T09:00:00Z")], last: "2026-11-05T00:00:00Z"),
                     room("r2", last: "2026-11-09T00:00:00Z")]
        let sections = ChatSections.make(bots: [bot("planner", pinned: true), bot("writer")], messages: [], rooms: rooms)
        XCTAssertEqual(sections.people.map(\.id), ["direct-r1-m-grace", "team-r2", "team-r1"])
        XCTAssertEqual(sections.pinned.map(\.id), ["planner"])
        XCTAssertEqual(sections.bots.map(\.id), ["writer"])
    }

    func testThePreviewOfADirectChatNamesOnlyYou() {
        let theirs = TeamLastMessage(first: 1, thisMac: false, text: "See you at 3", createdAt: "2026-11-10T09:00:00Z")
        let own = TeamLastMessage(first: 2, thisMac: true, text: "Sounds good", createdAt: "2026-11-10T09:01:00Z")
        XCTAssertEqual(TeamChatPreview.text(theirs, direct: true), "See you at 3")
        XCTAssertEqual(TeamChatPreview.text(own, direct: true), "You: Sounds good")
        XCTAssertEqual(TeamChatPreview.text(nil, direct: true), "No messages yet")
    }

    // MARK: Names in the chat

    func testTheWriterOfAMessageAndOfAQuoteInADirectChat() {
        let theirs = TeamMessage(id: "e1", first: 1, thisMac: false, text: "See you at 3", createdAt: "2026-11-10T09:00:00Z")
        let own = TeamMessage(id: "e2", first: 2, thisMac: true, text: "OK", createdAt: "2026-11-10T09:01:00Z")
        XCTAssertEqual(TeamNames.author(of: theirs, peer: "Grace"), "Grace")
        XCTAssertEqual(TeamNames.author(of: own, peer: "Grace"), "You")
        XCTAssertEqual(TeamNames.author(of: TeamMessageQuote(id: "e1", thisMac: false, text: "Hi"), ownMember: "m-ada",
                                        peer: "Grace"), "Grace")
        XCTAssertEqual(TeamNames.author(of: TeamMessageQuote(id: "e2", thisMac: true, text: "Hi"), ownMember: "m-ada",
                                        peer: "Grace"), "You")
        XCTAssertEqual(TeamChatEmptyLine.text(for: room(), peer: room().directPeer("m-x")),
                       "Messages here go only to Former member.")
    }

    // MARK: Updates

    func testUpdatesNameTheDirectChatsThatChanged() throws {
        let page = #"{"after": 4, "cursor": 5, "server_time": "2026-11-09T00:00:00Z", "team_messages_changed": ["r1"],"#
            + #" "team_directs_changed": [{"team": "r1", "member": "m-grace"}]}"#
        let updates = try APIJSON.decoder().decode(StateUpdates.self, from: Data(page.utf8))
        XCTAssertEqual(updates.changedTeamChats, ["r1", TeamChatKey(room: "r1", member: "m-grace")])
    }

    // MARK: Notifications

    func testADirectMessageOpensItsChat() throws {
        let target = try XCTUnwrap(NotificationTarget(userInfo: ["coconut": [
            "version": 1, "device_id": device, "kind": "direct", "direct": ["team": roomId, "member": graceId]]]))
        XCTAssertEqual(target.chat, TeamChatKey(room: roomId, member: graceId))
        XCTAssertFalse(target.group)
        XCTAssertEqual(target.destination(pendingDecisionIds: [], snapshotReady: false),
                       .direct(TeamChatKey(room: roomId, member: graceId)))
    }

    func testAGroupOfOneDirectChatOpensThatChat() throws {
        let target = try XCTUnwrap(NotificationTarget(userInfo: ["coconut": [
            "version": 1, "device_id": device, "kind": "group", "count": 2,
            "direct": ["team": roomId, "member": graceId]]]))
        XCTAssertTrue(target.group)
        XCTAssertEqual(target.destination(pendingDecisionIds: [], snapshotReady: false),
                       .direct(TeamChatKey(room: roomId, member: graceId)))
    }

    func testADirectTargetNeedsTheIDsOfARoomAndAMember() {
        let bad: [Any] = [["team": roomId], ["member": graceId], ["team": "r1", "member": graceId],
                          ["team": roomId, "member": graceId.uppercased()], "text"]
        for value in bad {
            for kind in ["direct", "group"] {
                XCTAssertNil(NotificationTarget(userInfo: ["coconut": [
                    "version": 1, "device_id": device, "kind": kind, "direct": value]]), "\(kind) \(value)")
            }
        }
        XCTAssertNil(NotificationTarget(userInfo: ["coconut": ["version": 1, "device_id": device, "kind": "direct"]]))
    }

    @MainActor
    func testADirectChatOfATeamThatTheAppNoLongerHasOpensTheChatsList() {
        let store = AppStore(demo: true)
        let chat = TeamChatKey(room: roomId, member: graceId)
        XCTAssertEqual(store.teamNotificationPlace(roomId, member: graceId), .direct(chat),
                       "A list that is not loaded keeps each chat.")
        store.team = TeamRooms(rooms: [TeamRoom(room: roomId, name: "Example Team", state: "active")], helper: true)
        XCTAssertEqual(store.teamNotificationPlace(roomId, member: graceId), .direct(chat),
                       "A chat with a member that left stays while the team stays.")
        store.team = TeamRooms(rooms: [], helper: true)
        XCTAssertEqual(store.teamNotificationPlace(roomId, member: graceId), .chatList)
        XCTAssertEqual(NotificationDestination.direct(chat).teamChat, chat)
        XCTAssertEqual(NotificationDestination.team(roomId).teamChat, TeamChatKey(room: roomId))
        XCTAssertNil(NotificationDestination.chatList.teamChat)
    }

    // MARK: The sentence of the notification settings

    func testTheSentenceNamesDirectMessagesAfterTeamMessages() {
        let schedule = NotificationSchedule()
        func text(_ level: NotificationLevel, _ outside: OutsideWorkHours, team: Bool) -> String {
            NotificationSentence.text(level: level, outside: outside, replies: true, team: team, direct: true,
                                      schedule: schedule)
        }
        XCTAssertEqual(text(.grouped, .same, team: true), "At any time, Coconut sends one summary at a time about new "
                       + "cards, bot replies, team messages and direct messages. Urgent cards notify you at once.")
        XCTAssertEqual(text(.every, .same, team: true), "At any time, Coconut notifies you about every card, every bot "
                       + "reply, every team message and every direct message.")
        XCTAssertEqual(text(.every, .urgent, team: true), "From 9:00 to 18:00, Coconut notifies you about every card, "
                       + "every bot reply, every team message and every direct message. Outside those hours, only urgent "
                       + "cards notify you. Other cards, bot replies, team messages and direct messages wait until 9:00, "
                       + "then one notification tells you about them.")
        // Without a team switch on, a direct message still takes "in Inbox" away.
        XCTAssertEqual(text(.every, .nothing, team: false), "From 9:00 to 18:00, Coconut notifies you about every card, "
                       + "every bot reply and every direct message. Outside those hours, nothing notifies you. Cards, bot "
                       + "replies and direct messages wait until 9:00, then one notification tells you about them.")
        // Only urgent does not change.
        XCTAssertEqual(text(.urgentOnly, .same, team: true),
                       "At any time, only urgent cards notify you. Other cards wait in Inbox without a notification.")
        // Without `direct` each text stays word for word.
        XCTAssertEqual(NotificationSentence.text(level: .every, outside: .same, replies: true, team: true,
                                                 schedule: schedule),
                       "At any time, Coconut notifies you about every card, every bot reply and every team message.")
    }

    // MARK: The cache of unsent messages

    func testTheCacheOfAnOlderAppStillLoadsAndBothChatsRoundTrip() throws {
        let older = #"{"deviceId": "d1", "messages": [], "teamMessages": {"r1": [{"id": "p1", "text": "Hi","#
            + #" "replyTo": null, "reply": null, "createdAt": "2026-11-10T09:00:00Z", "failed": false}]}}"#
        let cache = try JSONDecoder().decode(OutgoingCache.self, from: Data(older.utf8))
        XCTAssertNil(cache.directMessages)
        XCTAssertEqual(cache.pendingTeamMessages.mapValues { $0.map(\.text) }, ["r1": ["Hi"]])

        let pending = TeamPendingMessage(id: "p2", text: "Only for Grace", replyTo: nil,
                                         reply: TeamMessageQuote(id: "e1", thisMac: false, text: "See you"),
                                         createdAt: "2026-11-10T09:00:00Z")
        var both = cache
        both.pendingTeamMessages = cache.pendingTeamMessages.merging([TeamChatKey(room: "r1", member: "m-grace"): [pending]]) { $1 }
        let decoded = try JSONDecoder().decode(OutgoingCache.self, from: JSONEncoder().encode(both))
        XCTAssertEqual(decoded.teamMessages?["r1"]?.map(\.text), ["Hi"], "The Team chat keeps its field.")
        XCTAssertEqual(decoded.directMessages?["r1"]?["m-grace"], [pending])
        both.pendingTeamMessages = [:]
        XCTAssertNil(both.teamMessages)
        XCTAssertNil(both.directMessages)
    }

    // MARK: One store path

    /// The direct chat uses the store path of the Team chat with its own key: each request goes to the path of the
    /// direct chat, the Team chat stays as it is, a read sets the count of the row, and `team_directs_changed` reads
    /// the chat and the list again.
    @MainActor
    func testADirectChatLoadsSendsReadsAndFollowsTheUpdates() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        var state = try PreviewStateFixture.load()
        state.cursor = 10; state.syncVersion = 1
        let server = try DirectServer(state: APIJSON.encoder().encode(state), cursor: state.cursor)
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            XCTAssertEqual(server.listHeaders.last, "direct")
            let chat = TeamChatKey(room: "r1", member: "m1")
            XCTAssertEqual(store.team?.rooms.first?.directList.first?.unread, 1)
            await store.loadTeamChat(chat)
            XCTAssertEqual(store.teamChats[chat]?.messages.map(\.text), ["See you at 3"])
            XCTAssertNil(store.teamChats["r1"], "The Team chat does not load.")

            XCTAssertTrue(store.sendTeamMessage(chat, text: "Sounds good", replyingTo: store.teamChats[chat]?.messages.first))
            try await waitUntil { store.teamChats[chat]?.messages.count == 2 }
            XCTAssertEqual(store.teamChats[chat]?.messages.last?.reply?.thisMac, false)

            await store.markTeamRead(chat, first: 2)
            XCTAssertEqual(store.team?.rooms.first?.directList.first?.unread, 0)
            XCTAssertEqual(store.team?.rooms.first?.unread, 3, "The count of the team does not change.")

            server.addMessage("One more thing")
            store.setForeground(true)
            try await waitUntil { store.teamChats[chat]?.messages.count == 3 }
            try await waitUntil { store.team?.rooms.first?.directList.first?.lastMessage?.text == "One more thing" }
            XCTAssertEqual(Set(server.routes), ["GET /v1/team/rooms", "GET /v1/team/rooms/r1/directs/m1/messages",
                                                "POST /v1/team/rooms/r1/directs/m1/messages",
                                                "POST /v1/team/rooms/r1/directs/m1/read"])
            XCTAssertEqual(server.sentBodies.first?["reply_to"] as? String, "e1")
            XCTAssertNil(server.sentBodies.first?["mentions"], "A direct message names no bot.")
        }
        #endif
    }

    #if DEBUG && targetEnvironment(simulator)
    @MainActor
    private func withPairedStore(address: String, _ body: (AppStore) async throws -> Void) async throws {
        let savedToken = PairingToken.read()
        let folder = URL.applicationSupportDirectory.appendingPathComponent(TestIsolation.name("Coconut"))
        let paths = ["pairing.json", "snapshot.json", "outgoing.json"].map { folder.appendingPathComponent($0) }
        let saved = paths.map { try? Data(contentsOf: $0) }
        let store = AppStore(demo: false)
        store.disconnect()
        defer {
            store.setForeground(false); store.disconnect()
            if let savedToken { try? PairingToken.save(savedToken) } else { PairingToken.delete() }
            for (path, data) in zip(paths, saved) {
                if let data { try? data.write(to: path, options: .atomic) }
                else { try? FileManager.default.removeItem(at: path) }
            }
        }
        await store.pairOverLoopback(address: address, code: "fixture")
        XCTAssertEqual(store.connection, .connected, store.error ?? "No error")
        try await waitUntil(timeout: .seconds(5)) { store.team != nil || store.teamLoadError != nil }
        try await body(store)
    }
    #endif
}

/// A Mac in one team with one direct chat: Ada (this Mac) and Grace. It records the route of each team request.
private final class DirectServer: @unchecked Sendable {
    private let queue = DispatchQueue(label: "coconut.direct-test")
    private let lock = NSLock()
    private let listener: NWListener
    private let state: Data
    private var cursor: Int
    private struct ChatMessage { let first: Int; let text: String; let own: Bool; let replyTo: Int? }
    private var messages = [ChatMessage(first: 1, text: "See you at 3", own: false, replyTo: nil)]
    private var read = 0
    private var pending = false
    private var recorded: [String] = []
    private var headers: [String] = []
    private var bodies: [[String: Any]] = []
    /// The route of each team request, without its query.
    var routes: [String] { lock.withLock { recorded } }
    /// The value of `X-Coconut-Team` of each list request.
    var listHeaders: [String] { lock.withLock { headers } }
    /// The body of each send.
    var sentBodies: [[String: Any]] { lock.withLock { bodies } }

    init(state: Data, cursor: Int) throws {
        self.state = state; self.cursor = cursor
        listener = try NWListener(using: .tcp, on: .any)
    }

    /// Grace writes a message. The next updates answer names the direct chat.
    func addMessage(_ text: String) {
        lock.withLock {
            messages.append(ChatMessage(first: messages.count + 1, text: text, own: false, replyTo: nil))
            pending = true
        }
    }

    /// The caller holds the lock.
    private func json(_ message: ChatMessage) -> String {
        let reply = message.replyTo.flatMap { first in messages.first { $0.first == first } }
            .map { #"{"id": "e\#($0.first)", "this_mac": \#($0.own), "text": "\#($0.text)"}"# } ?? "null"
        return #"{"id": "e\#(message.first)", "first": \#(message.first), "this_mac": \#(message.own), "#
            + #""text": "\#(message.text)", "reply_to": \#(message.replyTo.map { #""e\#($0)""# } ?? "null"), "#
            + #""reply": \#(reply), "created_at": "2026-11-10T12:0\#(message.first):00Z", "edited_at": null}"#
    }

    /// The caller holds the lock.
    private func room() -> String {
        let last = messages.last.map {
            #"{"first": \#($0.first), "this_mac": \#($0.own), "text": "\#($0.text)", "#
                + #""created_at": "2026-11-10T12:0\#($0.first):00Z"}"#
        } ?? "null"
        let unread = messages.filter { !$0.own && $0.first > read }.count
        return #"{"room": "r1", "name": "Example Team", "state": "active", "unread": 3, "last_message": null, "#
            + #""members": [{"member": "m0", "name": "Ada", "this_mac": true, "direct": false}, "#
            + #"{"member": "m1", "name": "Grace", "this_mac": false, "direct": true}], "#
            + #""directs": [{"member": "m1", "member_name": "Grace", "unread": \#(unread), "last_message": \#(last)}]}"#
    }

    private func receive(_ connection: NWConnection, _ buffer: Data, then handle: @escaping @Sendable (Data) -> Void) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [self] data, _, done, _ in
            var buffer = buffer
            if let data { buffer.append(data) }
            let text = String(decoding: buffer, as: UTF8.self)
            if let split = text.range(of: "\r\n\r\n") {
                let length = text[..<split.lowerBound].components(separatedBy: "\r\n")
                    .first { $0.lowercased().hasPrefix("content-length:") }
                    .flatMap { Int($0.split(separator: ":").last?.trimmingCharacters(in: .whitespaces) ?? "") } ?? 0
                if text[split.upperBound...].utf8.count >= length { handle(buffer); return }
            }
            if done || data == nil { handle(buffer); return }
            receive(connection, buffer, then: handle)
        }
    }

    // swiftlint:disable:next function_body_length
    func start() async throws -> String {
        listener.newConnectionHandler = { [self] connection in
            connection.start(queue: queue)
            receive(connection, Data()) { [self] data in
                guard let request = String(data: data, encoding: .utf8),
                      let first = request.components(separatedBy: "\r\n").first else { connection.cancel(); return }
                let words = first.split(separator: " ").map(String.init)
                guard words.count >= 2 else { connection.cancel(); return }
                let route = words[0] + " " + words[1].components(separatedBy: "?")[0]
                let sent = request.components(separatedBy: "\r\n\r\n").last
                    .flatMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
                let body: String? = lock.withLock {
                    if route.hasPrefix("GET /v1/team/") || route.hasPrefix("POST /v1/team/") { recorded.append(route) }
                    switch route {
                    case "GET /v1/state": return nil
                    case "POST /v1/pair": return #"{"device_id": "00000000-0000-4000-8000-0000000000d1", "token": "fixture-only"}"#
                    case "GET /v1/team/rooms":
                        headers.append(request.components(separatedBy: "\r\n")
                            .first { $0.lowercased().hasPrefix("x-coconut-team:") }?
                            .split(separator: ":", maxSplits: 1).last?.trimmingCharacters(in: .whitespaces) ?? "")
                        return #"{"rooms": [\#(room())], "helper": true}"#
                    case "GET /v1/team/rooms/r1/directs/m1/messages":
                        let shown = messages.reversed().map(json).joined(separator: ", ")
                        return #"{"room": "r1", "member": "m1", "messages": [\#(shown)], "before": null}"#
                    case "POST /v1/team/rooms/r1/directs/m1/messages":
                        bodies.append(sent ?? [:])
                        let reply = (sent?["reply_to"] as? String).flatMap { Int($0.dropFirst()) }
                        messages.append(ChatMessage(first: messages.count + 1, text: sent?["text"] as? String ?? "",
                                                    own: true, replyTo: reply))
                        return #"{"room": "r1", "member": "m1", "message": \#(json(messages[messages.count - 1]))}"#
                    case "POST /v1/team/rooms/r1/directs/m1/read":
                        read = max(read, sent?["first"] as? Int ?? 0)
                        let unread = messages.filter { !$0.own && $0.first > read }.count
                        return #"{"room": "r1", "member": "m1", "read": \#(read), "unread": \#(unread)}"#
                    case "GET /v1/updates":
                        let after = Int(words[1].components(separatedBy: "after=").dropFirst().first?
                            .prefix { $0.isNumber } ?? "") ?? 0
                        guard pending, after == cursor else {
                            return #"{"after": \#(after), "cursor": \#(after), "server_time": "2026-11-09T00:00:00Z"}"#
                        }
                        pending = false; cursor += 1
                        return #"{"after": \#(after), "cursor": \#(cursor), "server_time": "2026-11-09T00:00:00Z", "#
                            + #""team_directs_changed": [{"team": "r1", "member": "m1"}]}"#
                    default: return "{}"
                    }
                }
                let content = body.map { Data($0.utf8) } ?? state
                var reply = Data(("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                                  + "\(content.count)\r\nConnection: close\r\n\r\n").utf8)
                reply.append(content)
                let answer = reply
                queue.asyncAfter(deadline: .now() + (route == "GET /v1/updates" ? 0.25 : 0.05)) {
                    connection.send(content: answer, completion: .contentProcessed { _ in connection.cancel() })
                }
            }
        }
        return try await withCheckedThrowingContinuation { continuation in
            listener.stateUpdateHandler = { [self] state in
                switch state {
                case .ready:
                    listener.stateUpdateHandler = nil
                    continuation.resume(returning: "http://127.0.0.1:\(listener.port!.rawValue)")
                case .failed(let error):
                    listener.stateUpdateHandler = nil
                    continuation.resume(throwing: error)
                default: break
                }
            }
            listener.start(queue: queue)
        }
    }

    func stop() { listener.cancel() }
}
