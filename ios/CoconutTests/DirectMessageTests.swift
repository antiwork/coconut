import XCTest
import Network
import SwiftUI
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

    // MARK: A direct chat with an unsent message

    private func unsent(_ text: String, at: String) -> TeamPendingMessage {
        TeamPendingMessage(id: "p-" + text, text: text, replyTo: nil, reply: nil, createdAt: at, failed: true)
    }

    func testADirectChatWithOnlyAnUnsentMessageHasARow() throws {
        let lin = TeamChatKey(room: "r1", member: "m-lin")
        var chat = TeamChat()
        chat.pending = [unsent("First try", at: "2026-11-08T00:00:00Z"), unsent("Second try", at: "2026-11-09T00:00:00Z")]
        let local = [lin: try XCTUnwrap(chat.newestLocal)]
        let room = room(members: [member("m-lin", "Lin", direct: true)],
                        directs: [direct("m-grace", at: "2026-11-10T09:00:00Z")], last: "2026-11-05T00:00:00Z")
        let latest = ["planner": "2026-11-07T00:00:00Z".apiDate!]
        let items = ChatList.items(bots: [bot("planner")], latest: latest, rooms: [room], local: local)
        XCTAssertEqual(items.map(\.id), ["direct-r1-m-grace", "direct-r1-m-lin", "bot-planner", "team-r1"],
                       "The time of the newest unsent message places the row.")
        guard case .direct(let listed, let row) = items[1] else { return XCTFail("No row of Lin.") }
        XCTAssertEqual(listed.directPeer(row.member).name, "Lin")
        XCTAssertEqual(TeamChatPreview.text(row.lastMessage, direct: true), "You: Second try")
        XCTAssertEqual(row.lastMessage?.createdAt, "2026-11-09T00:00:00Z")
        XCTAssertNil(row.unread, "No unread count.")
        XCTAssertEqual(ChatFilter.people.items(items).map(\.id), ["direct-r1-m-grace", "direct-r1-m-lin", "team-r1"])
        XCTAssertEqual(ChatFilter.bots.items(items).map(\.id), ["bot-planner"])
        XCTAssertEqual(ChatSections.make(bots: [], messages: [], rooms: [room], local: local).people.map(\.id),
                       ["direct-r1-m-grace", "direct-r1-m-lin", "team-r1"], "The Mac section People reads the rule too.")
    }

    func testAChatOfTheMacAndOfTheAppShowsOneTimeWithTheRowOfTheMac() {
        let grace = TeamChatKey(room: "r1", member: "m-grace")
        let local = [grace: TeamLastMessage(thisMac: true, text: "Unsent", createdAt: "2026-11-11T00:00:00Z")]
        let room = room(directs: [direct("m-grace", at: "2026-11-10T09:00:00Z", unread: 2)])
        let directs = room.listedDirects(local: local)
        XCTAssertEqual(directs.map(\.member), ["m-grace"])
        XCTAssertEqual(directs.first?.lastMessage?.text, "See you at 3", "The row of the Mac keeps its line.")
        XCTAssertEqual(directs.first?.unread, 2)
        XCTAssertEqual(room.listedDirects(local: [TeamChatKey(room: "r2", member: "m-lin"): local[grace]!]).count, 1,
                       "A chat of another room is not a chat of this room.")
        XCTAssertEqual(room.listedDirects(local: ["r1": local[grace]!]).count, 1, "The Team chat is no direct chat.")
    }

    func testTheRowFollowsASendAndARemoval() throws {
        var chat = TeamChat()
        chat.pending = [unsent("Hello Lin", at: "2026-11-09T00:00:00Z")]
        XCTAssertEqual(chat.newestLocal?.text, "Hello Lin")
        // The Mac confirms the message: the row shows the saved message.
        chat.pending = []
        chat.put(TeamMessage(id: "e1", first: 1, thisMac: true, text: "Hello Lin", createdAt: "2026-11-09T00:00:01Z"))
        let saved = try XCTUnwrap(chat.newestLocal)
        XCTAssertEqual(TeamChatPreview.text(saved, direct: true), "You: Hello Lin")
        // The person removes an unsent message, and the chat has nothing left: the row goes.
        var removed = TeamChat()
        removed.pending = [unsent("Gone", at: "2026-11-09T00:00:00Z")]
        removed.pending = []
        XCTAssertNil(removed.newestLocal)
        let room = room(directs: [])
        XCTAssertTrue(room.listedDirects(local: [:]).isEmpty)
        // A removal on its way hides the message.
        chat.deleting = ["e1"]
        XCTAssertNil(chat.newestLocal)
    }

    func testAMemberThatLeftAndAnOlderService() {
        let gone = TeamChatKey(room: "r1", member: "m-gone")
        let local = [gone: TeamLastMessage(thisMac: true, text: "Are you there?", createdAt: "2026-11-09T00:00:00Z")]
        let room = room(directs: [])
        let directs = room.listedDirects(local: local)
        XCTAssertEqual(directs.map(\.member), ["m-gone"])
        XCTAssertEqual(room.directPeer("m-gone").name, TeamNames.formerMember)
        XCTAssertFalse(room.directPeer("m-gone").canReceive)
        // An older service sends no `directs`: no direct row, also with unsent messages in the cache.
        var older = room
        older.directs = nil
        XCTAssertTrue(older.listedDirects(local: local).isEmpty)
        XCTAssertEqual(ChatList.people([older], local: local).map(\.id), ["team-r1"])
    }

    /// The store gives the newest message of each direct chat, and none of a Team chat.
    @MainActor
    func testTheStoreGivesTheNewestMessageOfEachDirectChat() {
        let store = AppStore(demo: true)
        let lin = TeamChatKey(room: "r1", member: "m-lin")
        store.teamChats[lin] = TeamChat(pending: [unsent("Hello Lin", at: "2026-11-09T00:00:00Z")])
        store.teamChats["r1"] = TeamChat(pending: [unsent("Hello team", at: "2026-11-09T00:00:00Z")])
        store.teamChats[TeamChatKey(room: "r1", member: "m-empty")] = TeamChat(loaded: true)
        XCTAssertEqual(store.newestLocalMessages.mapValues(\.text), [lin: "Hello Lin"])
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
        XCTAssertEqual(sections.bots.map(\.id), ["bot-writer"])
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
                       .chat(TeamChatKey(room: roomId, member: graceId)))
    }

    func testAGroupOfOneDirectChatOpensThatChat() throws {
        let target = try XCTUnwrap(NotificationTarget(userInfo: ["coconut": [
            "version": 1, "device_id": device, "kind": "group", "count": 2,
            "direct": ["team": roomId, "member": graceId]]]))
        XCTAssertTrue(target.group)
        XCTAssertEqual(target.destination(pendingDecisionIds: [], snapshotReady: false),
                       .chat(TeamChatKey(room: roomId, member: graceId)))
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
        XCTAssertEqual(store.teamNotificationPlace(chat), .chat(chat),
                       "A list that is not loaded keeps each chat.")
        store.team = TeamRooms(rooms: [TeamRoom(room: roomId, name: "Example Team", state: "active")], helper: true)
        XCTAssertEqual(store.teamNotificationPlace(chat), .chat(chat),
                       "A chat with a member that left stays while the team stays.")
        store.team = TeamRooms(rooms: [], helper: true)
        XCTAssertEqual(store.teamNotificationPlace(chat), .chatList)
        XCTAssertEqual(NotificationDestination.chat(chat).teamChat, chat)
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
                       + "cards, agent replies, team messages and direct messages. Urgent cards notify you at once.")
        XCTAssertEqual(text(.every, .same, team: true), "At any time, Coconut notifies you about every card, every agent "
                       + "reply, every team message and every direct message.")
        XCTAssertEqual(text(.every, .urgent, team: true), "From 9:00 to 18:00, Coconut notifies you about every card, "
                       + "every agent reply, every team message and every direct message. Outside those hours, only urgent "
                       + "cards notify you. Other cards, agent replies, team messages and direct messages wait until 9:00, "
                       + "then one notification tells you about them.")
        // Without a team switch on, a direct message still takes "in Inbox" away.
        XCTAssertEqual(text(.every, .nothing, team: false), "From 9:00 to 18:00, Coconut notifies you about every card, "
                       + "every agent reply and every direct message. Outside those hours, nothing notifies you. Cards, agent "
                       + "replies and direct messages wait until 9:00, then one notification tells you about them.")
        // Only urgent does not change.
        XCTAssertEqual(text(.urgentOnly, .same, team: true),
                       "At any time, only urgent cards notify you. Other cards wait in Inbox without a notification.")
        // Without `direct` each text stays word for word.
        XCTAssertEqual(NotificationSentence.text(level: .every, outside: .same, replies: true, team: true,
                                                 schedule: schedule),
                       "At any time, Coconut notifies you about every card, every agent reply and every team message.")
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

    /// The chat with a bot uses the same store path with its own key: each request goes to the path of the chat with
    /// the bot, a read sets the count of its row in `bot_directs`, `team_bot_directs_changed` reads the chat and the
    /// list again, and a draft makes no typing call.
    @MainActor
    func testAChatWithABotLoadsSendsReadsAndFollowsTheUpdates() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        var state = try PreviewStateFixture.load()
        state.cursor = 10; state.syncVersion = 1
        let server = try DirectServer(state: APIJSON.encoder().encode(state), cursor: state.cursor, bot: true)
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            let chat = TeamChatKey(room: "r1", bot: "t-r1-e1")
            XCTAssertEqual(store.team?.rooms.first?.botDirectList.first?.unread, 1)
            XCTAssertEqual(store.team?.rooms.first?.directBot("t-r1-e1")?.name, "Reporter")
            await store.loadTeamChat(chat)
            XCTAssertEqual(store.teamChats[chat]?.messages.map(\.text), ["See you at 3"])
            XCTAssertNil(store.teamChats["r1"], "The Team chat does not load.")
            XCTAssertNil(store.teamChats[TeamChatKey(room: "r1", member: "t-r1-e1")])

            store.teamDraftChanged(chat, draft: "Sounds")
            XCTAssertTrue(store.sendTeamMessage(chat, text: "Sounds good", replyingTo: store.teamChats[chat]?.messages.first))
            try await waitUntil { store.teamChats[chat]?.messages.count == 2 }
            XCTAssertEqual(store.teamChats[chat]?.messages.count, 2)
            XCTAssertEqual(store.teamChats[chat]?.messages.last?.reply?.thisMac, false)

            await store.markTeamRead(chat, first: 2)
            XCTAssertEqual(store.team?.rooms.first?.botDirectList.first?.unread, 0)
            XCTAssertEqual(store.team?.rooms.first?.unread, 3, "The count of the team does not change.")

            server.addMessage("One more thing")
            store.setForeground(true)
            try await waitUntil { store.teamChats[chat]?.messages.count == 3 }
            XCTAssertEqual(store.teamChats[chat]?.messages.count, 3, "team_bot_directs_changed reads the chat again.")
            try await waitUntil { store.team?.rooms.first?.botDirectList.first?.lastMessage?.text == "One more thing" }
            XCTAssertEqual(store.team?.rooms.first?.botDirectList.first?.lastMessage?.text, "One more thing",
                           "team_bot_directs_changed reads the list again.")
            XCTAssertEqual(Set(server.routes), ["GET /v1/team/rooms", "GET /v1/team/rooms/r1/directs/t-r1-e1/messages",
                                                "POST /v1/team/rooms/r1/directs/t-r1-e1/messages",
                                                "POST /v1/team/rooms/r1/directs/t-r1-e1/read"],
                           "No typing call and no request of the Team chat.")
            XCTAssertEqual(server.sentBodies.first?["reply_to"] as? String, "e1")
            XCTAssertNil(server.sentBodies.first?["mentions"], "A direct message names no bot.")
        }
        #endif
    }

    /// A send with a page carries `about_document_id` and no `reply_to`. A refusal shows its sentence in the notice of
    /// a failed action, and the message waits with its page, also after a restart. The retry sends the page again, and
    /// the answer of the Mac gives the page with its title.
    @MainActor
    func testASendAboutAPageCarriesThePageAlsoInARetryAfterARestart() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        var state = try PreviewStateFixture.load()
        state.cursor = 10; state.syncVersion = 1
        let server = try DirectServer(state: APIJSON.encoder().encode(state), cursor: state.cursor, bot: true)
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            let chat = TeamChatKey(room: "r1", bot: "t-r1-e1")
            let sales = TeamAboutPage(id: "t-r1-p1", title: "Sales")
            await store.loadTeamChat(chat)
            let refusal = "Choose a page of this agent in the Library."
            server.refuse(refusal)
            XCTAssertTrue(store.sendTeamMessage(chat, text: "Why are the refunds higher?",
                                                replyingTo: store.teamChats[chat]?.messages.first, page: sales))
            try await waitUntil { store.teamChats[chat]?.pending.first?.failed == true }
            XCTAssertEqual(store.teamChats[chat]?.pending.first?.failed, true)
            XCTAssertEqual(store.teamChats[chat]?.pending.first?.page, sales)
            XCTAssertEqual(store.error, refusal, "The notice of a failed action shows the sentence of the Mac.")
            XCTAssertEqual(server.sentBodies.first?["about_document_id"] as? String, "t-r1-p1")
            XCTAssertNil(server.sentBodies.first?["reply_to"], "A page and a reply do not go together.")
            // A refused message waits for the person. It does not try again by itself, also not after a delay.
            try await Task.sleep(for: .seconds(1))
            XCTAssertEqual(server.sentBodies.count, 1, "A refused message does not send again by itself.")

            let restarted = AppStore(demo: false)
            XCTAssertEqual(restarted.teamChats[chat]?.pending.first?.page, sales, "A restart keeps the page.")

            server.refuse(nil)
            let id = try XCTUnwrap(store.teamChats[chat]?.pending.first?.id)
            store.retryTeamMessage(chat, id: id)
            try await waitUntil { store.teamChats[chat]?.pending.isEmpty == true }
            XCTAssertEqual(store.teamChats[chat]?.pending.isEmpty, true)
            XCTAssertEqual(server.sentBodies.count, 2)
            XCTAssertEqual(server.sentBodies.last?["about_document_id"] as? String, "t-r1-p1", "The retry sends the page.")
            XCTAssertEqual(store.teamChats[chat]?.messages.last?.page, sales, "The Mac gives the page with its title.")

            XCTAssertTrue(store.sendTeamMessage(chat, text: "Thanks", replyingTo: nil))
            try await waitUntil { server.sentBodies.count == 3 }
            XCTAssertEqual(server.sentBodies.count, 3)
            XCTAssertNil(server.sentBodies.last?["about_document_id"], "A send without a page has no field.")
        }
        #endif
    }

    /// An update that says that a document changed reads a chat that quotes a page again, also without a Library list:
    /// one read for one update. When the page went, the quote has no ID. When the page comes back, the next update
    /// reads the chat again, and the quote has the ID again.
    @MainActor
    func testAChangeOfTheDocumentsReadsAChatThatQuotesAPageWithoutALibraryList() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        var state = try PreviewStateFixture.load()
        state.cursor = 10; state.syncVersion = 1
        let server = try DirectServer(state: APIJSON.encoder().encode(state), cursor: state.cursor, bot: true)
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            let chat = TeamChatKey(room: "r1", bot: "t-r1-e1")
            let path = "GET /v1/team/rooms/r1/directs/t-r1-e1/messages"
            await store.loadTeamChat(chat)
            XCTAssertTrue(store.sendTeamMessage(chat, text: "Why are the refunds higher?", replyingTo: nil,
                                                page: TeamAboutPage(id: "t-r1-p1", title: "Sales")))
            try await waitUntil { store.teamChats[chat]?.messages.last?.page?.id == "t-r1-p1" }
            XCTAssertEqual(store.teamChats[chat]?.messages.last?.page?.id, "t-r1-p1")
            XCTAssertNil(store.documents, "The app has no Library list.")
            let reads = server.routes.filter { $0 == path }.count
            store.setForeground(true)

            // One update that names the documents and this chat, which quotes a page: one read.
            server.changeDocuments(andChat: true)
            try await waitUntil { server.pendingUpdate == false && server.routes.filter { $0 == path }.count > reads }
            try await Task.sleep(for: .seconds(1))
            XCTAssertEqual(server.routes.filter { $0 == path }.count, reads + 1,
                           "One update that names the documents and the chat reads the chat one time.")

            // The page goes: one read, and the quote says that the page is gone.
            server.removePages()
            try await waitUntil { store.teamChats[chat]?.messages.last?.page == TeamAboutPage() }
            XCTAssertEqual(store.teamChats[chat]?.messages.last?.page, TeamAboutPage(),
                           "The quote says that the page is gone.")
            try await Task.sleep(for: .seconds(1))
            XCTAssertEqual(server.routes.filter { $0 == path }.count, reads + 2, "One read for one update.")

            // The page comes back with the same ID: the chat whose pages went reads again, and the quote opens it.
            server.restorePages()
            try await waitUntil { store.teamChats[chat]?.messages.last?.page?.id == "t-r1-p1" }
            XCTAssertEqual(store.teamChats[chat]?.messages.last?.page, TeamAboutPage(id: "t-r1-p1", title: "Sales"),
                           "The quote shows the page that came back.")
            try await Task.sleep(for: .seconds(1))
            XCTAssertEqual(server.routes.filter { $0 == path }.count, reads + 3, "One read for one update.")
            store.setForeground(false)
        }
        #endif
    }

    /// A change of the documents during the first read of a chat reads the chat again, so the answer of the first
    /// read, made before the change, does not show a page that went.
    @MainActor
    func testAChangeOfTheDocumentsDuringTheFirstReadReadsTheChatAgain() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        var state = try PreviewStateFixture.load()
        state.cursor = 10; state.syncVersion = 1
        let server = try DirectServer(state: APIJSON.encoder().encode(state), cursor: state.cursor, bot: true)
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            let chat = TeamChatKey(room: "r1", bot: "t-r1-e1")
            let path = "GET /v1/team/rooms/r1/directs/t-r1-e1/messages"
            server.quotePage()
            server.delayReads(3)
            store.setForeground(true)
            let load = Task { await store.loadTeamChat(chat) }
            try await waitUntil { server.routes.contains(path) }

            // The page goes while the first read waits for its answer.
            server.removePages()
            try await waitUntil { server.pendingUpdate == false }
            XCTAssertNil(store.teamChats[chat], "The first read still runs when the update comes.")
            server.delayReads(0.05)
            _ = await load.value
            XCTAssertEqual(store.teamChats[chat]?.messages.first?.page, TeamAboutPage(),
                           "The chat reads again, and the quote says that the page is gone.")
            XCTAssertEqual(server.routes.filter { $0 == path }.count, 2, "The change reads the chat one more time.")
            store.setForeground(false)
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

/// A Mac in one team with one direct chat: Ada (this Mac) and Grace, or with `bot` the chat of Ada with the bot
/// Reporter of the Mac for a team only "Reporter Mac" (team bot ID `t-r1-e1`). It records the route of each team
/// request.
private final class DirectServer: @unchecked Sendable {
    private let queue = DispatchQueue(label: "coconut.direct-test")
    private let lock = NSLock()
    private let listener: NWListener
    private let state: Data
    private var cursor: Int
    private struct ChatMessage { let first: Int; let text: String; let own: Bool; let replyTo: Int?; var page: String? = nil }
    private var messages = [ChatMessage(first: 1, text: "See you at 3", own: false, replyTo: nil)]
    /// The sentence of a 409 that each send gets. Nil accepts each send.
    private var refusal: String?
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

    /// The chat is the chat with the bot Reporter, not the chat with Grace.
    private let bot: Bool
    /// The ID of the other side in the paths, and the field that names the chat in an answer.
    private var other: String { bot ? "t-r1-e1" : "m1" }
    private var key: String { bot ? #""bot": "t-r1-e1""# : #""member": "m1""# }

    init(state: Data, cursor: Int, bot: Bool = false) throws {
        self.state = state; self.cursor = cursor; self.bot = bot
        listener = try NWListener(using: .tcp, on: .any)
    }

    /// Each send gets a 409 with this sentence, or nil to accept each send again.
    func refuse(_ sentence: String?) { lock.withLock { refusal = sentence } }

    /// The pages of the messages went: each page has no ID and no title. The next updates answer says that a
    /// document changed.
    func removePages() {
        lock.withLock {
            pagesGone = true
            documentsChanged = true
            pending = true
        }
    }

    /// The pages of the messages came back with their IDs. The next updates answer says that a document changed.
    func restorePages() {
        lock.withLock { pagesGone = false; documentsChanged = true; pending = true }
    }

    /// The first message quotes the page `t-r1-p1`. The updates do not say it.
    func quotePage() {
        lock.withLock { messages[0].page = "t-r1-p1" }
    }

    /// Each read of the newest page answers after this many seconds, with the messages at the time of the request.
    func delayReads(_ seconds: Double) { lock.withLock { readDelay = seconds } }
    private var readDelay = 0.05

    /// The next updates answer says that a document changed, and with `andChat` also that the chat changed.
    func changeDocuments(andChat: Bool = false) {
        lock.withLock { documentsChanged = true; chatChangedToo = andChat; pending = true }
    }

    private var chatChangedToo = false

    /// An updates answer waits for the app.
    var pendingUpdate: Bool { lock.withLock { pending } }
    private var pagesGone = false
    private var documentsChanged = false

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
            + #""reply": \#(reply), "created_at": "2026-11-10T12:0\#(message.first):00Z", "edited_at": null"#
            + (bot ? #", "page": \#(pageJSON(message.page))}"# : "}")
    }

    /// The caller holds the lock.
    private func pageJSON(_ page: String?) -> String {
        guard let page else { return "null" }
        return pagesGone ? #"{"id": null, "title": null}"# : #"{"id": "\#(page)", "title": "Sales"}"#
    }

    /// The caller holds the lock.
    private func room() -> String {
        let last = messages.last.map {
            #"{"first": \#($0.first), "this_mac": \#($0.own), "text": "\#($0.text)", "#
                + #""created_at": "2026-11-10T12:0\#($0.first):00Z"}"#
        } ?? "null"
        let unread = messages.filter { !$0.own && $0.first > read }.count
        let head = #"{"room": "r1", "name": "Example Team", "state": "active", "unread": 3, "last_message": null, "#
            + #""members": [{"member": "m0", "name": "Ada", "this_mac": true, "direct": false}, "#
            + #"{"member": "m1", "name": "Grace", "this_mac": false, "direct": true}, "#
            + #"{"member": "m2", "name": "Reporter Mac", "bot_only": true}], "#
        guard bot else {
            return head + #""directs": [{"member": "m1", "member_name": "Grace", "unread": \#(unread), "#
                + #""last_message": \#(last)}]}"#
        }
        return head + #""bots": [{"id": "t-r1-e1", "name": "Reporter", "member": "m2", "open": true, "direct": true}], "#
            + #""directs": [], "bot_directs": [{"bot": "t-r1-e1", "bot_name": "Reporter", "member": "m2", "#
            + #""member_name": "Reporter Mac", "unread": \#(unread), "last_message": \#(last), "typing": null}]}"#
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
                var status = "200 OK"
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
                    case "GET /v1/team/rooms/r1/directs/\(other)/messages":
                        let shown = messages.reversed().map(json).joined(separator: ", ")
                        return #"{"room": "r1", \#(key), "messages": [\#(shown)], "before": null}"#
                    case "POST /v1/team/rooms/r1/directs/\(other)/messages":
                        bodies.append(sent ?? [:])
                        if let refusal { status = "409 Conflict"; return #"{"error": "\#(refusal)"}"# }
                        let reply = (sent?["reply_to"] as? String).flatMap { Int($0.dropFirst()) }
                        messages.append(ChatMessage(first: messages.count + 1, text: sent?["text"] as? String ?? "",
                                                    own: true, replyTo: reply, page: sent?["about_document_id"] as? String))
                        return #"{"room": "r1", \#(key), "message": \#(json(messages[messages.count - 1]))}"#
                    case "POST /v1/team/rooms/r1/directs/\(other)/read":
                        read = max(read, sent?["first"] as? Int ?? 0)
                        let unread = messages.filter { !$0.own && $0.first > read }.count
                        return #"{"room": "r1", \#(key), "read": \#(read), "unread": \#(unread)}"#
                    case "GET /v1/updates":
                        let after = Int(words[1].components(separatedBy: "after=").dropFirst().first?
                            .prefix { $0.isNumber } ?? "") ?? 0
                        guard pending, after == cursor else {
                            return #"{"after": \#(after), "cursor": \#(after), "server_time": "2026-11-09T00:00:00Z"}"#
                        }
                        pending = false; cursor += 1
                        let changed = bot ? #""team_bot_directs_changed": [{"team": "r1", "bot": "t-r1-e1"}]"#
                            : #""team_directs_changed": [{"team": "r1", "member": "m1"}]"#
                        if documentsChanged {
                            documentsChanged = false
                            let also = chatChangedToo ? ", " + changed : ""
                            chatChangedToo = false
                            return #"{"after": \#(after), "cursor": \#(cursor), "server_time": "2026-11-09T00:00:00Z", "#
                                + #""documents_changed": true"# + also + "}"
                        }
                        return #"{"after": \#(after), "cursor": \#(cursor), "server_time": "2026-11-09T00:00:00Z", "#
                            + changed + "}"
                    default: return "{}"
                    }
                }
                let content = body.map { Data($0.utf8) } ?? state
                var reply = Data(("HTTP/1.1 \(status)\r\nContent-Type: application/json\r\nContent-Length: "
                                  + "\(content.count)\r\nConnection: close\r\n\r\n").utf8)
                reply.append(content)
                let answer = reply
                let delay = route == "GET /v1/updates" ? 0.25
                    : route == "GET /v1/team/rooms/r1/directs/\(other)/messages" ? lock.withLock { readDelay } : 0.05
                queue.asyncAfter(deadline: .now() + delay) {
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

/// The direct chat of this person with a bot of the team (docs/team-log.md, "Direct chat with an agent"): the key and
/// its path, the new fields of the list with and without them, the arrow of the Team screen, the closed lines, the rows
/// of the Chats list and of the Mac sidebar, the updates, the notifications, the banners and the cache.
final class BotDirectChatTests: XCTestCase {
    private let roomId = String(repeating: "ab", count: 32)
    private let entry = String(repeating: "ef", count: 16)
    private let device = "00000000-0000-4000-8000-0000000000d1"
    private var botId: String { "t-\(roomId)-\(entry)" }

    private func reporter(direct: Bool? = true, open: Bool? = true, thisMac: Bool? = nil) -> TeamRoomBot {
        TeamRoomBot(id: "t-r1-e1", name: "Reporter", avatar: "sparkle", color: "777777", member: "m-mac",
                    thisMac: thisMac, open: open, direct: direct)
    }

    private func room(_ id: String = "r1", bots: [TeamRoomBot] = [], botDirects: [TeamBotDirect]? = nil,
                      directs: [TeamDirect]? = [], state: String = "active", last: String? = nil) -> TeamRoom {
        TeamRoom(room: id, name: "Example Team", state: state,
                 members: [TeamMember(member: "m-ada", name: "Ada", thisMac: true),
                           TeamMember(member: "m-mac", name: "Reporter Mac", botOnly: true)],
                 bots: bots, createdAt: "2026-11-02T00:00:00Z", unread: 0,
                 lastMessage: last.map { TeamLastMessage(text: "Hi", createdAt: $0) }, directs: directs,
                 botDirects: botDirects)
    }

    private func chat(_ bot: String = "t-r1-e1", name: String? = "Reporter", at: String?, own: Bool = false,
                      unread: Int = 0, typing: Double? = nil) -> TeamBotDirect {
        TeamBotDirect(bot: bot, botName: name, unread: unread,
                      lastMessage: at.map { TeamLastMessage(first: 1, thisMac: own, text: "4 pull requests wait.", createdAt: $0) },
                      typing: typing.map { TeamTypingList(items: [TeamTyping(member: "", bot: bot, botName: name, seconds: $0)]) })
    }

    private func bot(_ id: String, pinned: Bool = false) -> Bot {
        Bot(id: id, sessionId: "session-\(id)", name: id, role: "", avatar: "sparkle", color: "777777",
            instructions: "", status: "idle", lastMessage: "", unreadCount: 0, archived: false,
            createdAt: "2026-11-01T00:00:00Z", pinned: pinned)
    }

    // MARK: The key and the path

    func testTheKeyOfAChatWithABotNamesThePathOfADirectChat() {
        let key = TeamChatKey(room: "r1", bot: "t-r1-e1")
        XCTAssertTrue(key.isDirect)
        XCTAssertEqual(key.path, "team/rooms/r1/directs/t-r1-e1")
        XCTAssertEqual(key.query, "", "A direct chat names no named chat.")
        XCTAssertEqual(key.botEntry, "e1")
        XCTAssertNotEqual(key, TeamChatKey(room: "r1", member: "t-r1-e1"), "A chat with a bot is not a chat with a person.")
        XCTAssertNotEqual(key, "r1")
        XCTAssertNil(TeamChatKey(room: "r1", member: "m-grace").botEntry)
    }

    // MARK: Decode and an older service

    func testTheListDecodesTheMarkOfEachBotAndTheChatsWithABot() throws {
        let answer = #"{"rooms": [{"room": "r1", "name": "Example Team", "state": "active", "unread": 0,"#
            + #" "bots": [{"id": "t-r1-e1", "name": "Reporter", "open": true, "direct": true},"#
            + #" {"id": "t-r1-e2", "name": "Scribe", "direct": false}],"#
            + #" "directs": [],"#
            + #" "bot_directs": [{"bot": "t-r1-e1", "bot_name": "Reporter", "member": "m-mac", "member_name": "Reporter Mac","#
            + #" "unread": 2, "typing": 12, "last_message":"#
            + #" {"first": 7, "this_mac": false, "text": "4 pull requests wait.", "created_at": "2026-11-10T12:00:00Z"}},"#
            + #" {"bot": "t-r1-e3", "bot_name": "Former", "unread": 0, "typing": null, "last_message": null},"#
            + #" {"bot": "t-r1-e4", "typing": "soon"}]}]}"#
        let rooms = try APIJSON.decoder().decode(TeamRooms.self, from: Data(answer.utf8))
        let room = try XCTUnwrap(rooms.rooms.first)
        XCTAssertEqual(room.botList.map(\.direct), [true, false])
        XCTAssertEqual(room.botDirectList.map(\.bot), ["t-r1-e1", "t-r1-e3", "t-r1-e4"])
        XCTAssertEqual(room.botDirectList.first?.unread, 2)
        XCTAssertEqual(room.botDirectList.first?.member, "m-mac")
        XCTAssertEqual(room.botDirectList.first?.memberName, "Reporter Mac")
        XCTAssertNil(room.botDirectList[2].member, "Each new field is optional.")
        XCTAssertEqual(room.botDirectList.first?.lastMessage?.text, "4 pull requests wait.")
        XCTAssertEqual(room.botDirectList.first?.typing?.items.first?.seconds, 12)
        XCTAssertNil(room.botDirectList[1].typing)
        XCTAssertNil(room.botDirectList[2].typing, "A value that is not a number shows nobody.")
        XCTAssertEqual(room.directBot("t-r1-e1")?.name, "Reporter")
        XCTAssertNil(room.directBot("t-r1-e2"))
    }

    func testAnOlderServiceShowsNothingOfAChatWithABot() throws {
        let older = #"{"rooms": [{"room": "r1", "name": "Example Team", "state": "active", "unread": 0,"#
            + #" "bots": [{"id": "t-r1-e1", "name": "Reporter", "open": true}], "directs": []}]}"#
        let rooms = try APIJSON.decoder().decode(TeamRooms.self, from: Data(older.utf8))
        let room = try XCTUnwrap(rooms.rooms.first)
        XCTAssertNil(room.botDirects)
        XCTAssertNil(room.directBot("t-r1-e1"), "No arrow and no action on the name.")
        XCTAssertEqual(TeamBotSection.rows(of: room).compactMap { TeamBotSection.directBot(of: $0, in: room) }, [])
        let local = [TeamChatKey(room: "r1", bot: "t-r1-e1"): TeamLastMessage(thisMac: true, text: "Hi", createdAt: "2026-11-09T00:00:00Z")]
        XCTAssertEqual(ChatList.items(bots: [], latest: [:], rooms: [room], local: local).map(\.id), ["team-r1"],
                       "No row, also with an unsent message in the cache.")
    }

    func testAMessageKeepsItsPage() throws {
        let page = #"{"messages": [{"id": "e3", "first": 3, "this_mac": true, "text": "Why?", "created_at": "2026-11-10T12:00:00Z","#
            + #" "page": {"id": "t-r1-p1", "title": "Sales"}},"#
            + #" {"id": "e2", "first": 2, "this_mac": true, "text": "And this?", "created_at": "2026-11-10T11:00:00Z","#
            + #" "page": {"id": null, "title": null}},"#
            + #" {"id": "e1", "first": 1, "this_mac": false, "text": "Hello", "created_at": "2026-11-10T10:00:00Z", "page": null},"#
            + #" {"id": "e0", "first": 0, "this_mac": false, "text": "Older", "created_at": "2026-11-10T09:00:00Z"}],"#
            + #" "before": null}"#
        let decoded = try APIJSON.decoder().decode(TeamMessagePage.self, from: Data(page.utf8))
        XCTAssertEqual(decoded.messages.map(\.page), [TeamAboutPage(id: "t-r1-p1", title: "Sales"), TeamAboutPage(), nil, nil],
                       "A page with its title, a page that is gone, no page, and a message without the field.")
    }

    // MARK: The Team screen and the name in the Team chat

    func testOnlyABotThatAnswersAndHasTheMarkHasTheArrow() {
        let room = room(bots: [reporter(),
                               TeamRoomBot(id: "t-r1-e2", name: "Scribe", member: "m-mac", open: true),
                               TeamRoomBot(id: "t-r1-e3", name: "Pages", member: "m-mac", open: false, direct: true),
                               TeamRoomBot(id: "t-r1-e4", name: "Mine", member: "m-ada", thisMac: true, open: true,
                                           direct: true)])
        let rows = TeamBotSection.rows(of: room)
        XCTAssertEqual(rows.compactMap { TeamBotSection.directBot(of: $0, in: room)?.name }, ["Reporter"],
                       "A bot without the mark, a bot of pages only, and a bot of this Mac keep the plain row.")
        XCTAssertEqual(room.directBot("t-r1-e1")?.id, "t-r1-e1", "The name above a message of the bot opens the chat.")
        XCTAssertNil(room.directBot("t-r1-e4"))
        XCTAssertNil(room.directBot(nil))
        XCTAssertNil(room.directBot("t-r1-gone"))
        XCTAssertEqual(TeamDirectText.message("Reporter"), "Message Reporter")
        XCTAssertEqual(TeamDirectText.opens, "Opens a direct chat")
    }

    func testABotOfPagesOnlyWithTheMarkGetsNoEntry() {
        let pages = TeamRoomBot(id: "t-r1-e1", name: "Reporter", member: "m-mac", open: false, direct: true)
        let room = room(bots: [pages])
        XCTAssertNil(room.directBot("t-r1-e1"), "No name button and no open composer for a bot of pages only.")
        XCTAssertEqual(TeamBotSection.rows(of: room).compactMap { TeamBotSection.directBot(of: $0, in: room) }, [])
        XCTAssertEqual(room.peer(of: TeamChatKey(room: "r1", bot: "t-r1-e1"))?.canReceive, false)
        XCTAssertEqual(room.peer(of: TeamChatKey(room: "r1", bot: "t-r1-e1"))?.closedLine, "Reporter does not answer now.")
    }

    // MARK: The chat view

    func testTheChatNamesTheBotAndClosesWhenTheBotCannotAnswer() throws {
        let open = room(bots: [reporter()])
        let key = TeamChatKey(room: "r1", bot: "t-r1-e1")
        let peer = try XCTUnwrap(open.peer(of: key))
        XCTAssertEqual(peer.name, "Reporter")
        XCTAssertTrue(peer.isBot)
        XCTAssertTrue(peer.canReceive)
        XCTAssertEqual(peer.botEntry?.avatar, "sparkle", "The face comes from the bot.")
        XCTAssertEqual(TeamChatEmptyLine.text(for: open, peer: peer), "Messages here go only to Reporter.")

        let silent = room(bots: [reporter(direct: false)])
        XCTAssertEqual(silent.peer(of: key)?.canReceive, false)
        XCTAssertEqual(silent.peer(of: key)?.closedLine, "Reporter does not answer now.")

        let gone = room(bots: [], botDirects: [chat(at: "2026-11-10T09:00:00Z")])
        XCTAssertEqual(gone.peer(of: key)?.canReceive, false)
        XCTAssertEqual(gone.peer(of: key)?.closedLine, "Reporter is no longer in this team.",
                       "The last name of the list of the chats stays.")
        XCTAssertEqual(room().peer(of: key)?.name, "Former agent")
        XCTAssertNil(open.peer(of: "r1"), "The Team chat has no other side.")
        XCTAssertEqual(open.peer(of: TeamChatKey(room: "r1", member: "m-grace"))?.isBot, false)
    }

    func testTheChatShowsWhileTheBotTypes() {
        let now = Date()
        var room = room(bots: [reporter()], botDirects: [chat(at: "2026-11-10T09:00:00Z", typing: 20)])
        room.botDirects?[0].typing?.received = now
        let key = TeamChatKey(room: "r1", bot: "t-r1-e1")
        let typing = room.typingNow(in: key, at: now.addingTimeInterval(5))
        XCTAssertEqual(TeamTypingText.text(typing), "Reporter is typing")
        XCTAssertEqual(room.typingNow(in: key, at: now.addingTimeInterval(21)), [], "The seconds end.")
        XCTAssertEqual(room.typingNow(in: TeamChatKey(room: "r1", bot: "t-r1-e9"), at: now), [])
        XCTAssertEqual(room.typingNow(in: TeamChatKey(room: "r1", member: "m-grace"), at: now), [],
                       "A direct chat with a person shows nobody.")
        room.state = "removed"
        XCTAssertEqual(room.typingNow(in: key, at: now.addingTimeInterval(5)), [], "A team that is not active shows nobody.")
    }

    // MARK: The Chats list and the Mac sidebar

    func testAChatWithABotSortsWithTheOtherRowsAndShowsUnderAllAndBots() {
        let rooms = [room(bots: [reporter()], botDirects: [chat(at: "2026-11-10T09:00:00Z", unread: 1)],
                          directs: [TeamDirect(member: "m-grace", memberName: "Grace",
                                               lastMessage: TeamLastMessage(text: "Hi", createdAt: "2026-11-08T00:00:00Z"))],
                          last: "2026-11-05T00:00:00Z")]
        let latest = ["planner": "2026-11-07T00:00:00Z".apiDate!]
        let items = ChatList.items(bots: [bot("planner"), bot("writer")], latest: latest, rooms: rooms)
        XCTAssertEqual(items.map(\.id), ["bot-direct-r1-t-r1-e1", "direct-r1-m-grace", "bot-planner", "team-r1", "bot-writer"])
        XCTAssertEqual(items.first?.chat, TeamChatKey(room: "r1", bot: "t-r1-e1"))
        XCTAssertEqual(ChatFilter.all.items(items).map(\.id), items.map(\.id))
        XCTAssertEqual(ChatFilter.bots.items(items).map(\.id), ["bot-direct-r1-t-r1-e1", "bot-planner", "bot-writer"])
        XCTAssertEqual(ChatFilter.people.items(items).map(\.id), ["direct-r1-m-grace", "team-r1"], "A bot is not a person.")
        XCTAssertEqual(HomeLaunchView.identifier(TeamChatKey(room: "r1", bot: "t-r1-e1")), "bot-direct-chat-t-r1-e1")
    }

    func testAChatWithABotWithOnlyAnUnsentMessageHasARow() throws {
        let key = TeamChatKey(room: "r1", bot: "t-r1-e1")
        var draft = TeamChat()
        draft.pending = [TeamPendingMessage(id: "p1", text: "Which pull requests wait?", replyTo: nil, reply: nil,
                                            createdAt: "2026-11-09T00:00:00Z", failed: true)]
        let local = [key: try XCTUnwrap(draft.newestLocal)]
        let room = room(bots: [reporter()], botDirects: [])
        let listed = room.listedBotDirects(local: local)
        XCTAssertEqual(listed.map(\.bot), ["t-r1-e1"])
        XCTAssertNil(listed.first?.unread, "No unread count.")
        XCTAssertEqual(TeamChatPreview.text(listed.first?.lastMessage, direct: true), "You: Which pull requests wait?")
        XCTAssertEqual(room.listedDirects(local: local).count, 0, "A chat with a bot is no direct chat with a person.")
        let listedOnce = self.room(botDirects: [chat(at: "2026-11-10T09:00:00Z", unread: 2)]).listedBotDirects(local: local)
        XCTAssertEqual(listedOnce.map(\.unread), [2], "A chat of the Mac and of the app shows one time.")
        XCTAssertEqual(self.room("r2", botDirects: []).listedBotDirects(local: local).count, 0)
    }

    func testTheSectionBotsOfTheMacListsTheChatsWithABot() {
        let rooms = [room(bots: [reporter()], botDirects: [chat(at: "2026-11-10T09:00:00Z")], last: "2026-11-11T00:00:00Z")]
        let messages = [Message(id: "m1", botId: "writer", role: "assistant", kind: "text", text: "Hi",
                                createdAt: "2026-11-12T00:00:00Z", streaming: false)]
        let sections = ChatSections.make(bots: [bot("planner", pinned: true), bot("writer"), bot("quiet")],
                                         messages: messages, rooms: rooms)
        XCTAssertEqual(sections.pinned.map(\.id), ["planner"])
        XCTAssertEqual(sections.people.map(\.id), ["team-r1"], "The section People has no chat with a bot.")
        XCTAssertEqual(sections.bots.map(\.id), ["bot-writer", "bot-direct-r1-t-r1-e1", "bot-quiet"],
                       "Newest activity first, with the other bots.")
    }

    @MainActor
    func testTheStoreGivesTheNewestMessageOfAChatWithABot() {
        let store = AppStore(demo: true)
        let key = TeamChatKey(room: "r1", bot: "t-r1-e1")
        store.teamChats[key] = TeamChat(pending: [TeamPendingMessage(id: "p1", text: "Hello", replyTo: nil, reply: nil,
                                                                     createdAt: "2026-11-09T00:00:00Z")])
        XCTAssertEqual(store.newestLocalMessages.mapValues(\.text), [key: "Hello"])
    }

    // MARK: A chat that goes

    func testAChatWithABotStaysWhileItsTeamStays() {
        let key = TeamChatKey(room: "r1", bot: "t-r1-e1")
        XCTAssertTrue(TeamRooms(rooms: [room(bots: [])], helper: true).has(key), "The bot left, and the history stays.")
        XCTAssertFalse(TeamRooms(rooms: [room("r2")], helper: true).has(key), "The team left: the Mac selects the Inbox.")
    }

    // MARK: Updates

    func testUpdatesNameTheChatsWithABotThatChanged() throws {
        let page = #"{"after": 4, "cursor": 5, "server_time": "2026-11-09T00:00:00Z","#
            + #" "team_directs_changed": [{"team": "r1", "member": "m-grace"}],"#
            + #" "team_bot_directs_changed": [{"team": "r1", "bot": "t-r1-e1"}]}"#
        let updates = try APIJSON.decoder().decode(StateUpdates.self, from: Data(page.utf8))
        XCTAssertEqual(updates.changedTeamChats, [TeamChatKey(room: "r1", member: "m-grace"),
                                                  TeamChatKey(room: "r1", bot: "t-r1-e1")])
    }

    // MARK: Notifications

    func testADirectMessageOfABotOpensItsChat() throws {
        let target = try XCTUnwrap(NotificationTarget(userInfo: ["coconut": [
            "version": 1, "device_id": device, "kind": "direct", "direct": ["team": roomId, "bot": botId]]]))
        XCTAssertEqual(target.chat, TeamChatKey(room: roomId, bot: botId))
        XCTAssertEqual(target.destination(pendingDecisionIds: [], snapshotReady: false),
                       .chat(TeamChatKey(room: roomId, bot: botId)))
        let group = try XCTUnwrap(NotificationTarget(userInfo: ["coconut": [
            "version": 1, "device_id": device, "kind": "group", "count": 3, "direct": ["team": roomId, "bot": botId]]]))
        XCTAssertTrue(group.group)
        XCTAssertEqual(group.destination(pendingDecisionIds: [], snapshotReady: false),
                       .chat(TeamChatKey(room: roomId, bot: botId)))
    }

    func testADirectTargetOfABotNeedsATeamBotIDOfItsRoom() {
        let other = String(repeating: "12", count: 32)
        let bad: [Any] = [["team": roomId, "bot": "t-\(other)-\(entry)"], ["team": roomId, "bot": "t-\(roomId)-short"],
                          ["team": roomId, "bot": "\(roomId)-\(entry)"], ["team": roomId, "bot": botId.uppercased()],
                          ["team": roomId, "bot": 7], ["bot": botId]]
        for value in bad {
            for kind in ["direct", "group"] {
                XCTAssertNil(NotificationTarget(userInfo: ["coconut": [
                    "version": 1, "device_id": device, "kind": kind, "direct": value]]), "\(kind) \(value)")
            }
        }
    }

    @MainActor
    func testAChatWithABotOfATeamThatTheAppNoLongerHasOpensTheChatsList() {
        let store = AppStore(demo: true)
        let key = TeamChatKey(room: roomId, bot: botId)
        XCTAssertEqual(store.teamNotificationPlace(key), .chat(key))
        store.team = TeamRooms(rooms: [TeamRoom(room: roomId, name: "Example Team", state: "active")], helper: true)
        XCTAssertEqual(store.teamNotificationPlace(key), .chat(key))
        store.team = TeamRooms(rooms: [], helper: true)
        XCTAssertEqual(store.teamNotificationPlace(key), .chatList)
    }

    /// The place that a tapped notification of a chat with a bot shows at once is that chat, through the function
    /// that the app calls (CoconutApp.openNotification).
    @MainActor
    func testATappedNotificationOfAChatWithABotShowsThatChatAtOnce() throws {
        let store = AppStore(demo: true)
        let target = try XCTUnwrap(NotificationTarget(userInfo: ["coconut": [
            "version": 1, "device_id": device, "kind": "direct", "direct": ["team": roomId, "bot": botId]]]))
        XCTAssertEqual(store.notificationPreview(target), .chat(TeamChatKey(room: roomId, bot: botId)))
        store.team = TeamRooms(rooms: [TeamRoom(room: roomId, name: "Example Team", state: "active")], helper: true)
        XCTAssertEqual(store.notificationPreview(target), .chat(TeamChatKey(room: roomId, bot: botId)))
        let group = try XCTUnwrap(NotificationTarget(userInfo: ["coconut": [
            "version": 1, "device_id": device, "kind": "group", "count": 2, "direct": ["team": roomId, "bot": botId]]]))
        XCTAssertEqual(store.notificationPreview(group), .chat(TeamChatKey(room: roomId, bot: botId)))
        let person = try XCTUnwrap(NotificationTarget(userInfo: ["coconut": [
            "version": 1, "device_id": device, "kind": "direct",
            "direct": ["team": roomId, "member": String(repeating: "cd", count: 32)]]]))
        XCTAssertEqual(store.notificationPreview(person),
                       .chat(TeamChatKey(room: roomId, member: String(repeating: "cd", count: 32))))
    }

    // MARK: A chat that takes the place of another chat

    #if os(iOS)
    /// The Chats tab puts a new chat in the place of the open chat (CoconutApp.showTeamChat): the name above a message,
    /// and a tapped notification. The page of the new chat must be a new view: it loads its own messages, and the
    /// draft, the reply quote and the edit of the first chat stay behind.
    @MainActor
    func testAChatThatTakesThePlaceOfAnotherChatIsANewPage() async throws {
        let store = AppStore(demo: true)
        var team = room(bots: [reporter()], botDirects: [], directs: [])
        team.members?.append(TeamMember(member: "m-grace", name: "Grace", direct: true))
        store.team = TeamRooms(rooms: [team], helper: true)
        let place = ShownRoute(TeamChatRoute(chat: "r1"))
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.rootViewController = UIHostingController(rootView: ShownRouteHost(place: place).environment(store))
        window.makeKeyAndVisible()
        defer { window.isHidden = true }

        try await waitUntil(timeout: .seconds(5)) { store.teamChats["r1"] != nil }
        XCTAssertNotNil(store.teamChats["r1"], "The first chat loads.")
        for next in [TeamChatKey(room: "r1", bot: "t-r1-e1"), "r1", TeamChatKey(room: "r1", member: "m-grace")] {
            store.teamChats[next] = nil
            place.route = TeamChatRoute(chat: next)
            try await waitUntil(timeout: .seconds(5)) { store.teamChats[next] != nil }
            XCTAssertNotNil(store.teamChats[next], "The new page loads the messages of \(next).")
        }
    }

    /// The real chat view takes the page that waits for it: when it opens, when it shows again, and when the page
    /// comes while it shows. A chat under another page leaves the page.
    @MainActor
    func testTheChatOnTheScreenTakesThePageThatWaitsForIt() async throws {
        let store = AppStore(demo: true)
        store.team = TeamRooms(rooms: [room(bots: [reporter()], botDirects: [], directs: [])], helper: true)
        let chat = TeamChatKey(room: "r1", bot: "t-r1-e1")
        let sales = TeamAboutPage(id: "t-r1-p1", title: "Sales")
        store.teamPageQuotes[chat] = sales
        let stack = ChatStack()
        stack.path.append(TeamChatRoute(chat: chat))
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.rootViewController = UIHostingController(rootView: ChatStackHost(stack: stack).environment(store))
        window.makeKeyAndVisible()
        defer { window.isHidden = true }

        try await waitUntil(timeout: .seconds(5)) { store.teamPageQuotes[chat] == nil }
        XCTAssertNil(store.teamPageQuotes[chat], "The chat takes the page when it opens.")

        // The page comes only after the chat disappeared: a slow machine ends the push of the cover late.
        stack.path.append("Cover")
        try await waitUntil(timeout: .seconds(10)) { !store.isTeamChatOnScreen(chat) }
        XCTAssertFalse(store.isTeamChatOnScreen(chat), "The cover hides the chat.")
        store.teamPageQuotes[chat] = sales
        try await settle(window)
        XCTAssertEqual(store.teamPageQuotes[chat], sales, "A chat under another page leaves the page.")

        stack.path.removeLast()
        try await waitUntil(timeout: .seconds(5)) { store.teamPageQuotes[chat] == nil }
        XCTAssertNil(store.teamPageQuotes[chat], "The chat takes the page when it shows again.")

        store.teamPageQuotes[chat] = sales
        try await waitUntil(timeout: .seconds(5)) { store.teamPageQuotes[chat] == nil }
        XCTAssertNil(store.teamPageQuotes[chat], "The chat on the screen takes a page that comes while it shows.")
    }

    /// Gives the hosted views their turn after a change of the store: one layout pass of the window, which runs the
    /// pending updates of the views and their onChange, and then a short time for a task that a view starts. A view
    /// that takes a page takes it here, so the assert after it sees the result.
    @MainActor
    private func settle(_ window: UIWindow) async throws {
        await Task.yield()
        window.rootViewController?.view.setNeedsLayout()
        window.rootViewController?.view.layoutIfNeeded()
        try await Task.sleep(for: .milliseconds(300))
    }

    /// The chat on the Chats tab stays while the Library tab shows. It leaves a page that comes then, and takes it
    /// when its tab shows again.
    @MainActor
    func testAChatOnAnotherTabLeavesThePage() async throws {
        let store = AppStore(demo: true)
        store.team = TeamRooms(rooms: [room(bots: [reporter()], botDirects: [], directs: [])], helper: true)
        let chat = TeamChatKey(room: "r1", bot: "t-r1-e1")
        let sales = TeamAboutPage(id: "t-r1-p1", title: "Sales")
        let tabs = ChatTabs()
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.rootViewController = UIHostingController(rootView: ChatTabsHost(tabs: tabs, chat: chat).environment(store))
        window.makeKeyAndVisible()
        defer { window.isHidden = true }
        try await waitUntil(timeout: .seconds(10)) { store.isTeamChatOnScreen(chat) }
        XCTAssertTrue(store.isTeamChatOnScreen(chat), "The chat shows.")

        // The page comes only after the chat disappeared with its tab.
        tabs.selected = 1
        try await waitUntil(timeout: .seconds(10)) { !store.isTeamChatOnScreen(chat) }
        XCTAssertFalse(store.isTeamChatOnScreen(chat), "The other tab hides the chat.")
        store.teamPageQuotes[chat] = sales
        try await settle(window)
        XCTAssertEqual(store.teamPageQuotes[chat], sales, "A chat on another tab leaves the page.")

        tabs.selected = 0
        try await waitUntil(timeout: .seconds(5)) { store.teamPageQuotes[chat] == nil }
        XCTAssertNil(store.teamPageQuotes[chat], "The chat takes the page when its tab shows again.")
    }
    #endif

    // MARK: The banners of the Mac

    func testANewMessageOfABotInItsDirectChatMakesABannerWithItsName() {
        let key = TeamChatKey(room: "r1", bot: "t-r1-e1")
        let before = TeamRooms(rooms: [room(bots: [reporter()], botDirects: [chat(at: "2026-11-10T09:00:00Z")])], helper: true)
        var newer = chat(at: "2026-11-10T10:00:00Z", unread: 1)
        newer.lastMessage?.first = 2
        let after = TeamRooms(rooms: [room(bots: [reporter()], botDirects: [newer])], helper: true)
        let plan = MessageBanners.plan(before: before, after: after)
        XCTAssertEqual(plan.post, [MessageBanner(chat: key, title: "Reporter", subtitle: nil, body: "4 pull requests wait.")])
        XCTAssertEqual(MessageBanners.thread(key), "r1:e1", "The thread of the push of the iPhone.")
        XCTAssertEqual(MessageBanners.chat(userInfo: MessageBanners.userInfo(key)), key)
        XCTAssertEqual(MessageBanners.shownChat(key, rooms: after), key)
        XCTAssertNil(MessageBanners.shownChat(TeamChatKey(room: "r1", bot: "t-r1-e9"), rooms: after))
        var own = newer
        own.lastMessage?.thisMac = true
        XCTAssertEqual(MessageBanners.plan(before: before, after: TeamRooms(rooms: [room(botDirects: [own])], helper: true)).post, [])
    }

    // MARK: Chat about this page

    private func teamPage(_ bot: String, room: String? = "r1") -> BotDocument {
        BotDocument(id: "t-r1-p1", botId: bot, key: "sales", title: "Sales", format: "html", version: 1,
                    createdAt: "2026-11-10T09:00:00Z", updatedAt: "2026-11-10T09:00:00Z",
                    team: room.map { DocumentTeam(room: $0, name: "Example Team") })
    }

    private let sales = TeamAboutPage(id: "t-r1-p1", title: "Sales")

    func testOnlyAPageOfATeamBotThatCanGetADirectMessageHasTheChatItem() {
        let rooms = TeamRooms(rooms: [room(bots: [
            reporter(),
            TeamRoomBot(id: "t-r1-e2", name: "Scribe", member: "m-mac", open: true, direct: false),
            TeamRoomBot(id: "t-r1-e3", name: "Older", member: "m-mac", open: true),
            TeamRoomBot(id: "t-r1-e4", name: "Pages", member: "m-mac", open: false, direct: true),
            TeamRoomBot(id: "t-r1-e5", name: "Mine", member: "m-ada", thisMac: true, open: true, direct: true)])])
        XCTAssertEqual(rooms.directChat(about: teamPage("t-r1-e1")), TeamChatKey(room: "r1", bot: "t-r1-e1"))
        XCTAssertNil(rooms.directChat(about: teamPage("t-r1-e2")), "A bot without the mark has no item.")
        XCTAssertNil(rooms.directChat(about: teamPage("t-r1-e3")), "An older service sends no mark, so no item.")
        XCTAssertNil(rooms.directChat(about: teamPage("t-r1-e4")), "A bot of pages only has no item.")
        XCTAssertNil(rooms.directChat(about: teamPage("t-r1-e5")), "A bot of this Mac has no direct chat.")
        XCTAssertNil(rooms.directChat(about: teamPage("t-r1-gone")), "A bot that left the team has no item.")
        XCTAssertNil(rooms.directChat(about: teamPage("t-r1-e1", room: "r2")), "A team that the app does not have.")
        XCTAssertNil(rooms.directChat(about: teamPage("bot-1", room: nil)),
                     "A page of a bot of this Mac keeps the item of its own chat.")
    }

    func testThePageOfAMessageShowsAsAQuoteOfTheLibrary() {
        XCTAssertEqual(TeamAboutPage(teamPage("t-r1-e1")), sales)
        XCTAssertEqual(sales.quote, MessageReference.page(teamPage("t-r1-e1")),
                       "The quote of a page of a team bot is the quote of a page of an own bot.")
        let gone = TeamAboutPage().quote
        XCTAssertTrue(gone.isPage)
        XCTAssertNil(gone.documentId, "A page that is gone opens nothing.")
        XCTAssertEqual(gone.text, "This page is no longer in the Library.")
        XCTAssertEqual(PageQuoteText.remove, "Remove the page")
        XCTAssertEqual(PageQuoteText.open("Sales"), "Open the page Sales")
        XCTAssertFalse(MessageReference(messageId: "e1", text: "Hello", author: "Grace", role: "").isPage)
    }

    private let answered = TeamMessage(id: "e1", first: 1, text: "Hello", createdAt: "2026-11-10T10:00:00Z")

    /// The composer of a team chat keeps one quote: a reply, a page or an edit. The newer one takes the place of the
    /// other. A closed chat drops a page, and a removed message drops its reply or its edit.
    func testTheComposerKeepsOneQuoteAndAClosedChatDropsThePage() {
        var composer = TeamComposerState()
        composer.reply(to: answered)
        composer.ask(about: sales)
        XCTAssertEqual(composer.quote, .page(sales), "A page takes the place of a reply.")
        composer.reply(to: answered)
        XCTAssertEqual(composer.quote, .reply(answered), "A reply takes the place of a page.")

        composer.draft = "Why?"
        composer.edit(answered)
        XCTAssertNil(composer.quote, "An edit takes the place of a reply.")
        XCTAssertEqual(composer.draft, "Hello", "The draft of an edit is the text of the message.")
        composer.ask(about: sales)
        XCTAssertEqual(composer, TeamComposerState(draft: "", quote: .page(sales), editing: nil),
                       "A page takes the place of an edit, and the edit takes its draft with it.")
        composer.edit(answered)
        XCTAssertNil(composer.quote, "An edit takes the place of a page.")
        composer.reply(to: answered)
        XCTAssertNil(composer.editing, "A reply takes the place of an edit.")
        composer.edit(answered)
        composer.ask(about: sales)
        XCTAssertNil(composer.editing, "A page takes the place of an edit.")
        composer.edit(answered)
        composer.cancelEdit()
        XCTAssertEqual(composer, TeamComposerState())

        composer.ask(about: sales)
        composer.removeQuote()
        XCTAssertNil(composer.quote, "The cross removes the page.")

        composer.ask(about: sales)
        composer.close()
        XCTAssertNil(composer.quote, "A chat where the person can no longer write drops the page.")
        composer.reply(to: answered)
        composer.close()
        XCTAssertEqual(composer.quote, .reply(answered), "The closed line keeps a reply, as before.")
        composer.removed("e1")
        XCTAssertNil(composer.quote, "A removed message takes its reply with it.")
        composer.edit(answered)
        composer.removed("e1")
        XCTAssertEqual(composer, TeamComposerState(), "A removed message takes its edit with it.")

        composer.draft = "Why?"
        composer.ask(about: sales)
        composer.sent()
        XCTAssertEqual(composer, TeamComposerState(), "A send clears the draft and the page.")
    }

    /// An edit that the Mac did not take comes back, and a reply or a page that the person set in the meantime stays.
    /// A new draft or a new edit wins over the failed edit.
    func testAFailedEditComesBackAndKeepsAReplyOrAPage() {
        var composer = TeamComposerState()
        composer.edit(answered)
        composer.cancelEdit()
        composer.ask(about: sales)
        composer.restore(edit: answered, text: "Hello again")
        XCTAssertEqual(composer, TeamComposerState(draft: "Hello again", quote: .page(sales), editing: answered),
                       "The edit comes back with its text, and the page stays.")

        composer = TeamComposerState(quote: .reply(answered))
        composer.restore(edit: answered, text: "Hello again")
        XCTAssertEqual(composer.quote, .reply(answered), "The reply stays.")
        XCTAssertEqual(composer.editing, answered)

        composer = TeamComposerState(draft: "Something new")
        composer.restore(edit: answered, text: "Hello again")
        XCTAssertEqual(composer, TeamComposerState(draft: "Something new"), "A new draft wins.")
        var other = answered
        other.id = "e2"
        composer = TeamComposerState(draft: "Other", editing: other)
        composer.restore(edit: answered, text: "Hello again")
        XCTAssertEqual(composer.editing, other, "A new edit wins.")
        composer = TeamComposerState(draft: "", editing: other)
        composer.restore(edit: answered, text: "Hello again")
        XCTAssertEqual(composer, TeamComposerState(draft: "", editing: other), "A new edit wins, also with no text.")
    }

    @MainActor
    func testAPageWaitsForTheChatOfItsKeyOnly() {
        let store = AppStore(demo: true)
        let chat = TeamChatKey(room: "r1", bot: "t-r1-e1")
        store.teamPageQuotes[chat] = sales
        func take(_ key: TeamChatKey) -> TeamAboutPage? { store.takeTeamPageQuote(key, onScreen: true, canWrite: true) }
        XCTAssertNil(take(TeamChatKey(room: "r1", member: "t-r1-e1")), "Not the chat of a person.")
        XCTAssertNil(take("r1"), "Not the Team chat.")
        XCTAssertNil(take(TeamChatKey(room: "r1", bot: "t-r1-e2")), "Not the chat of another bot.")
        XCTAssertNil(take(TeamChatKey(room: "r2", bot: "t-r1-e1")), "Not a chat of another team.")
        XCTAssertEqual(take(chat), sales)
        XCTAssertNil(take(chat), "The chat takes the page one time.")
    }

    /// A chat that is not on the screen leaves the page for the chat that the person sees. A chat where the person
    /// cannot write takes the page and drops it, so it never shows later.
    @MainActor
    func testOnlyAChatOnTheScreenWhereThePersonCanWriteTakesThePage() {
        let store = AppStore(demo: true)
        let chat = TeamChatKey(room: "r1", bot: "t-r1-e1")
        store.teamPageQuotes[chat] = sales
        XCTAssertNil(store.takeTeamPageQuote(chat, onScreen: false, canWrite: true))
        XCTAssertEqual(store.teamPageQuotes[chat], sales, "The page still waits.")
        XCTAssertNil(store.takeTeamPageQuote(chat, onScreen: true, canWrite: false))
        XCTAssertNil(store.teamPageQuotes[chat], "A closed chat drops the page.")
        store.teamPageQuotes[chat] = sales
        XCTAssertEqual(store.takeTeamPageQuote(chat, onScreen: true, canWrite: true), sales)
    }

    /// The send of the composer gives the store the page and no reply, or the reply and no page.
    @MainActor
    func testTheSendOfTheComposerGivesThePageToTheStore() async throws {
        let store = AppStore(demo: true)
        store.team = TeamRooms(rooms: [room(bots: [reporter()])])
        let chat = TeamChatKey(room: "r1", bot: "t-r1-e1")
        var composer = TeamComposerState(draft: "Why are the refunds higher?")
        composer.reply(to: answered)
        composer.ask(about: sales)
        XCTAssertTrue(store.sendTeamMessage(chat, from: composer))
        try await waitUntil { store.teamChats[chat]?.messages.count == 1 }
        XCTAssertEqual(store.teamChats[chat]?.messages.last?.page, sales)
        XCTAssertNil(store.teamChats[chat]?.messages.last?.replyTo)

        composer = TeamComposerState(draft: "And this?")
        composer.reply(to: answered)
        XCTAssertTrue(store.sendTeamMessage(chat, from: composer))
        try await waitUntil { store.teamChats[chat]?.messages.count == 2 }
        XCTAssertEqual(store.teamChats[chat]?.messages.last?.replyTo, "e1")
        XCTAssertNil(store.teamChats[chat]?.messages.last?.page)
    }

    /// A row shows the page of its message, also of a message on its way, and else the message that it answers.
    func testTheRowShowsThePageElseTheReply() {
        var message = answered
        XCTAssertNil(TeamRowQuote(message: message, pending: nil))
        message.replyTo = "e0"
        XCTAssertEqual(TeamRowQuote(message: message, pending: nil), .reply("e0"))
        message.page = sales
        XCTAssertEqual(TeamRowQuote(message: message, pending: nil), .page(sales))
        message.page = TeamAboutPage()
        XCTAssertEqual(TeamRowQuote(message: message, pending: nil), .page(TeamAboutPage()), "A page that is gone.")
        let pending = TeamPendingMessage(id: "p1", text: "Why?", replyTo: nil, reply: nil,
                                         createdAt: "2026-11-10T09:00:00Z", page: sales)
        XCTAssertEqual(TeamRowQuote(message: nil, pending: pending), .page(sales))
    }

    /// A tap on the quote opens the page in the Library: in the window where it can, else with a push. A page that
    /// is gone opens nothing.
    func testATapOnTheQuoteOpensThePageAndAGonePageOpensNothing() throws {
        var opened: [String] = []
        var pushed: [AnyHashable] = []
        let window: (String) -> Void = { opened.append($0) }
        let push: (any Hashable) -> Void = { pushed.append(AnyHashable($0)) }
        try XCTUnwrap(sales.open(openDocument: window, pushRoute: push))()
        XCTAssertEqual(opened, ["t-r1-p1"])
        XCTAssertTrue(pushed.isEmpty)
        try XCTUnwrap(sales.open(openDocument: nil, pushRoute: push))()
        XCTAssertEqual(pushed, [AnyHashable(DocumentRoute(id: "t-r1-p1"))])
        XCTAssertNil(TeamAboutPage().open(openDocument: window, pushRoute: push), "A page that is gone is no button.")
    }

    /// The item of a page leads to the chat of a bot of this Mac, or to the direct chat with the team bot. The page
    /// waits for that chat.
    @MainActor
    func testTheItemLeadsThePageToItsChat() {
        let rooms = TeamRooms(rooms: [room(bots: [reporter()])])
        let own = bot("bot-1")
        XCTAssertEqual(DocumentChatTarget(teamPage("bot-1", room: nil), bots: [own], team: rooms), .bot("bot-1"))
        let chat = TeamChatKey(room: "r1", bot: "t-r1-e1")
        XCTAssertEqual(DocumentChatTarget(teamPage("t-r1-e1"), bots: [own], team: rooms), .team(chat))
        XCTAssertNil(DocumentChatTarget(teamPage("t-r1-e2"), bots: [own], team: rooms), "No item for another page.")
        XCTAssertNil(DocumentChatTarget(teamPage("t-r1-e1"), bots: [own], team: nil), "No item without the teams.")

        let store = AppStore(demo: true)
        store.ask(about: teamPage("t-r1-e1"), in: .team(chat))
        XCTAssertEqual(store.teamPageQuotes, [chat: sales])
        XCTAssertTrue(store.pageQuotes.isEmpty)
        store.ask(about: teamPage("bot-1", room: nil), in: .bot("bot-1"))
        XCTAssertEqual(store.pageQuotes["bot-1"]?.documentId, "t-r1-p1")
    }

    /// An edit of a message about a page keeps the page, also in the demo.
    @MainActor
    func testAnEditKeepsThePage() async throws {
        let store = AppStore(demo: true)
        let chat = TeamChatKey(room: "r1", bot: "t-r1-e1")
        var message = answered
        message.thisMac = true
        message.page = sales
        store.teamChats[chat] = TeamChat(messages: [message], loaded: true)
        let edited = await store.editTeamMessage(chat, message, text: "Hello again")
        XCTAssertTrue(edited)
        XCTAssertEqual(store.teamChats[chat]?.messages.first?.text, "Hello again")
        XCTAssertEqual(store.teamChats[chat]?.messages.first?.page, sales)
    }

    /// A fresh Library list without a page that a loaded chat with a bot quotes reads that chat again.
    /// After an update that says that a document changed, each loaded chat with a bot that quotes a page reads again,
    /// one time. A chat whose pages all went also reads again, because a page can come back with the same ID.
    func testAChatThatQuotesAPageReadsAgainAfterAChangeOfTheDocuments() {
        var message = answered
        message.page = sales
        var second = message
        second.id = "e2"
        var gone = message
        gone.page = TeamAboutPage()
        let bot = TeamChatKey(room: "r1", bot: "t-r1-e1")
        let chats: [TeamChatKey: TeamChat] = [
            bot: TeamChat(messages: [message, second, gone], loaded: true),
            TeamChatKey(room: "r1", bot: "t-r1-e2"): TeamChat(messages: [gone], loaded: true),
            TeamChatKey(room: "r1", bot: "t-r1-e3"): TeamChat(messages: [answered], loaded: true),
            TeamChatKey(room: "r1", bot: "t-r1-e4"): TeamChat(messages: [message], loaded: false),
            "r1": TeamChat(messages: [message], loaded: true)]
        XCTAssertEqual(AppStore.teamChatsQuotingPages(chats), [bot, TeamChatKey(room: "r1", bot: "t-r1-e2")],
                       "One time for a chat with two pages, and a chat whose pages went. Not a chat without a page, "
                       + "not a chat that did not load, and not the Team chat.")
    }

    /// One update reads each chat one time: a chat that the update names and that quotes a page reads one time after a
    /// change of the documents. A reset reads each chat.
    @MainActor
    func testOneUpdateReadsEachChatOneTime() throws {
        var message = answered
        message.page = sales
        let bot = TeamChatKey(room: "r1", bot: "t-r1-e1")
        let named = TeamChatKey(room: "r1", chat: "c1")
        let chats: [TeamChatKey: TeamChat] = [
            bot: TeamChat(messages: [message], loaded: true),
            TeamChatKey(room: "r1", bot: "t-r1-e2"): TeamChat(messages: [message], loaded: true),
            named: TeamChat(messages: [answered], loaded: true),
            TeamChatKey(room: "r1", member: "m-grace"): TeamChat(messages: [answered], loaded: true)]
        func page(_ fields: String) throws -> StateUpdates {
            try APIJSON.decoder().decode(StateUpdates.self, from: Data(
                (#"{"after": 1, "cursor": 2, "server_time": "2026-11-10T00:00:00Z""# + fields + "}").utf8))
        }
        let both = try page(#", "documents_changed": true, "team_bot_directs_changed": [{"team": "r1", "bot": "t-r1-e1"}]"#)
        XCTAssertEqual(AppStore.teamChatsToRead(after: both, chats: chats),
                       [bot, TeamChatKey(room: "r1", bot: "t-r1-e2")], "Each chat that quotes a page, one time.")
        let chat = try page(#", "team_bot_directs_changed": [{"team": "r1", "bot": "t-r1-e1"}]"#)
        XCTAssertEqual(AppStore.teamChatsToRead(after: chat, chats: chats), [bot])
        let room = try page(#", "team_messages_changed": ["r1"]"#)
        XCTAssertEqual(AppStore.teamChatsToRead(after: room, chats: chats), [named], "A room names its named chats.")
        XCTAssertEqual(AppStore.teamChatsToRead(after: try page(""), chats: chats), [])
    }

    /// A send with a page keeps the page and no reply. A page goes only into a direct chat with a bot.
    @MainActor
    func testASendWithAPageKeepsThePageAndNoReply() async throws {
        let store = AppStore(demo: true)
        store.team = TeamRooms(rooms: [room(bots: [reporter()])])
        let chat = TeamChatKey(room: "r1", bot: "t-r1-e1")
        let answered = TeamMessage(id: "e1", first: 1, text: "Hello", createdAt: "2026-11-10T10:00:00Z")
        XCTAssertTrue(store.sendTeamMessage(chat, text: "Why are the refunds higher?", replyingTo: answered, page: sales))
        try await waitUntil { store.teamChats[chat]?.messages.isEmpty == false }
        let sent = try XCTUnwrap(store.teamChats[chat]?.messages.last)
        XCTAssertEqual(sent.page, sales)
        XCTAssertNil(sent.replyTo, "A page and a reply do not go together.")

        let person = TeamChatKey(room: "r1", member: "m-mac")
        XCTAssertTrue(store.sendTeamMessage(person, text: "Hi", replyingTo: nil, page: sales))
        try await waitUntil { store.teamChats[person]?.messages.isEmpty == false }
        XCTAssertEqual(store.teamChats[person]?.messages.map(\.text), ["Hi"])
        XCTAssertNil(store.teamChats[person]?.messages.first?.page, "A chat of two people has no page.")
    }

    // MARK: The cache of unsent messages

    func testTheCacheKeepsTheUnsentMessagesOfAChatWithABot() throws {
        let key = TeamChatKey(room: "r1", bot: "t-r1-e1")
        let pending = TeamPendingMessage(id: "p1", text: "Only for Reporter", replyTo: nil, reply: nil,
                                         createdAt: "2026-11-10T09:00:00Z")
        var cache = OutgoingCache(deviceId: "d1", messages: [])
        cache.pendingTeamMessages = [key: [pending], TeamChatKey(room: "r1", member: "m-grace"): [pending]]
        let decoded = try JSONDecoder().decode(OutgoingCache.self, from: JSONEncoder().encode(cache))
        XCTAssertEqual(decoded.pendingTeamMessages[key], [pending])
        XCTAssertEqual(decoded.directMessages?["r1"]?.keys.sorted(), ["m-grace"], "A chat with a bot is not a chat with a person.")
        cache.pendingTeamMessages = [:]
        XCTAssertNil(cache.botDirectMessages)
    }

    func testAnUnsentMessageKeepsItsPage() throws {
        let key = TeamChatKey(room: "r1", bot: "t-r1-e1")
        let pending = TeamPendingMessage(id: "p1", text: "Why?", replyTo: nil, reply: nil,
                                         createdAt: "2026-11-10T09:00:00Z", page: sales)
        var cache = OutgoingCache(deviceId: "d1", messages: [])
        cache.pendingTeamMessages = [key: [pending]]
        let decoded = try JSONDecoder().decode(OutgoingCache.self, from: JSONEncoder().encode(cache))
        XCTAssertEqual(decoded.pendingTeamMessages[key]?.first?.page, sales)
        XCTAssertEqual(pending.afterRestart.page, sales, "A restart keeps the page.")
        XCTAssertEqual(pending.afterRestart.retried.page, sales, "A retry keeps the page.")

        let older = #"{"deviceId": "d1", "messages": [], "botDirectMessages": {"r1": {"t-r1-e1": [{"id": "p2","#
            + #" "text": "Hi", "replyTo": null, "reply": null, "createdAt": "2026-11-10T09:00:00Z", "failed": true}]}}}"#
        let earlier = try JSONDecoder().decode(OutgoingCache.self, from: Data(older.utf8))
        XCTAssertEqual(earlier.pendingTeamMessages[key]?.map(\.text), ["Hi"], "A message of an earlier app still loads.")
        XCTAssertNil(earlier.pendingTeamMessages[key]?.first?.page)
    }
}

#if os(iOS)
/// The path of the stack that a test shows. A new route takes the place of the open chat, as
/// CoconutApp.showTeamChat does: a new path with one chat page.
@MainActor @Observable
private final class ShownRoute {
    var path: [TeamChatRoute]
    init(_ route: TeamChatRoute) { path = [route] }
    var route: TeamChatRoute {
        get { path[0] }
        set { path = [newValue] }
    }
}

/// Two tabs, as the iPhone app has: the chat on the first, and a plain page on the second.
@MainActor @Observable
private final class ChatTabs {
    var selected = 0
}

private struct ChatTabsHost: View {
    @Bindable var tabs: ChatTabs
    let chat: TeamChatKey

    var body: some View {
        TabView(selection: $tabs.selected) {
            NavigationStack { TeamChatDestination(route: TeamChatRoute(chat: chat)) }
                .tabItem { Text("Chats") }.tag(0)
            Text("Library").tabItem { Text("Library") }.tag(1)
        }
    }
}

/// A stack with a chat page, and a plain page that can cover the chat.
@MainActor @Observable
private final class ChatStack {
    var path = NavigationPath()
}

private struct ChatStackHost: View {
    @Bindable var stack: ChatStack

    var body: some View {
        NavigationStack(path: $stack.path) {
            Color.clear
                .navigationDestination(for: TeamChatRoute.self) { TeamChatDestination(route: $0) }
                .navigationDestination(for: String.self) { Text($0) }
        }
    }
}

/// The stack of the Chats tab with one chat page, as CoconutApp shows it.
private struct ShownRouteHost: View {
    @Bindable var place: ShownRoute

    var body: some View {
        NavigationStack(path: $place.path) {
            Color.clear.navigationDestination(for: TeamChatRoute.self) { TeamChatDestination(route: $0) }
        }
    }
}
#endif
