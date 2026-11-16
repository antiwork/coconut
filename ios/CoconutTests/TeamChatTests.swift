import XCTest
@testable import Coconut

/// The pure rules of the Team chat: the decode, the merge of a page, the runs, the preview of the row, the order of
/// the Chats list, the read position, and the key of a retry.
final class TeamChatTests: XCTestCase {
    private static let utc: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC")!
        return calendar
    }()

    private func message(_ id: String, _ first: Int, member: String = "m-grace", name: String? = "Grace",
                         own: Bool = false, bot: String? = nil, text: String? = nil,
                         at time: String = "2026-11-10T12:00:00Z") -> TeamMessage {
        TeamMessage(id: id, first: first, member: own ? "m-ada" : member, memberName: own ? "Ada" : name,
                    thisMac: own, bot: bot, botName: bot.map { _ in "Reporter" }, text: text ?? "Message \(id)",
                    createdAt: time)
    }

    private func page(_ messages: [TeamMessage], before: Int?) -> TeamMessagePage {
        TeamMessagePage(messages: messages.reversed(), before: before)
    }

    // MARK: Delete

    /// The admin of an active team deletes a message of another person or of a bot in the Team chat. Each person deletes
    /// an own message, also without the field.
    func testTheAdminHasDeleteOnAMessageOfAnotherPersonOrOfABot() {
        let admin = TeamRoom(room: "r1", name: "Example Team", state: "active", canDeleteMessages: true)
        let grace = message("e1", 1)
        let reporter = message("e2", 2, bot: "t-r1-b1")
        let mine = message("e3", 3, own: true)
        for item in [grace, reporter, mine] {
            XCTAssertTrue(TeamMessageDeletion.allows(item, in: admin, direct: false), item.id)
        }
        // A direct chat keeps the rule of an own message only.
        XCTAssertFalse(TeamMessageDeletion.allows(grace, in: admin, direct: true))
        XCTAssertTrue(TeamMessageDeletion.allows(mine, in: admin, direct: true))
    }

    func testAMemberThatIsNotTheAdminAndAnOlderServiceHaveDeleteOnlyOnAnOwnMessage() throws {
        let rooms = #"{"rooms": [{"room": "r1", "name": "Example Team", "state": "active", "can_delete_messages": false},"#
            + #" {"room": "r2", "name": "Example Team", "state": "active"},"#
            + #" {"room": "r3", "name": "Example Team", "state": "active", "can_delete_messages": true}]}"#
        let decoded = try APIJSON.decoder().decode(TeamRooms.self, from: Data(rooms.utf8)).rooms
        XCTAssertEqual(decoded.map(\.canDeleteMessages), [false, nil, true])
        for room in decoded.prefix(2) {
            XCTAssertFalse(TeamMessageDeletion.allows(message("e1", 1), in: room, direct: false), room.room)
            XCTAssertFalse(TeamMessageDeletion.allows(message("e2", 2, bot: "t-r1-b1"), in: room, direct: false))
            XCTAssertTrue(TeamMessageDeletion.allows(message("e3", 3, own: true), in: room, direct: false))
        }
        // A team that is not active has no Delete for a message of another person.
        for state in ["joining", "removed", "gone"] {
            let room = TeamRoom(room: "r1", name: "Example Team", state: state, canDeleteMessages: true)
            XCTAssertFalse(TeamMessageDeletion.allows(message("e1", 1), in: room, direct: false), state)
        }
    }

    /// One confirmation for each message that has the action. Its words are true for a message of another person too.
    func testTheConfirmationHasTheWordsOfAnOwnDelete() {
        XCTAssertEqual(TeamMessageDeletion.title, "Delete this message for everyone?")
        XCTAssertEqual(TeamMessageDeletion.confirm, "Delete")
    }

    // MARK: Decode

    /// The answer of GET /v1/team/rooms/<room>/messages (backend/coconut/team_messages.py), with nulls and a message
    /// that the app cannot read. The page keeps each other message.
    func testAPageDecodesAndDropsOnlyAMessageThatItCannotRead() throws {
        let answer = #"""
        {"room": "r1", "before": 7, "messages": [
          {"id": "e9", "first": 9, "member": "m-grace", "member_name": null, "this_mac": false, "bot": null,
           "bot_name": null, "text": "Hello", "reply_to": "e8", "reply": null, "mentions": [],
           "created_at": "2026-11-10T12:00:00.123456Z", "edited_at": null, "later_field": {"a": 1}},
          {"id": "e8", "first": "eight", "text": "Not a number"},
          {"id": "e7", "first": 7, "member": "m-ada", "member_name": "Ada", "this_mac": true, "bot": "t-r1-b1",
           "bot_name": "Reporter", "text": "Done", "reply_to": null, "reply": null, "mentions": ["m-grace"],
           "created_at": "2026-11-10T11:00:00Z", "edited_at": "2026-11-10T11:05:00Z"}
        ]}
        """#
        let page = try APIJSON.decoder().decode(TeamMessagePage.self, from: Data(answer.utf8))
        XCTAssertEqual(page.messages.map(\.id), ["e9", "e7"])
        XCTAssertEqual(page.before, 7)
        XCTAssertNil(page.messages[0].memberName)
        XCTAssertEqual(page.messages[0].replyTo, "e8")
        XCTAssertNil(page.messages[0].reply)
        XCTAssertFalse(page.messages[0].isOwn)
        // A bot of this Mac writes as itself, not as the person.
        XCTAssertFalse(page.messages[1].isOwn)
        XCTAssertTrue(page.messages[1].isEdited)
        XCTAssertNotNil(page.messages[0].createdAt.apiDate)
    }

    func testTheRoomsOfAnOlderServiceHaveNoChat() throws {
        let newer = #"{"rooms": [{"room": "r1", "name": "Example Team", "state": "active", "unread": 3,"#
            + #" "created_at": "2026-11-01T00:00:00Z", "last_message": {"first": 4, "member_name": "Grace","#
            + #" "bot_name": null, "text": "Hi", "created_at": "2026-11-10T12:00:00Z"}}]}"#
        let room = try XCTUnwrap(try APIJSON.decoder().decode(TeamRooms.self, from: Data(newer.utf8)).rooms.first)
        XCTAssertTrue(room.hasChat)
        XCTAssertEqual(room.unread, 3)
        XCTAssertEqual(room.lastMessage?.text, "Hi")
        let older = #"{"rooms": [{"room": "r1", "name": "Example Team", "state": "active"}]}"#
        let old = try XCTUnwrap(try APIJSON.decoder().decode(TeamRooms.self, from: Data(older.utf8)).rooms.first)
        XCTAssertFalse(old.hasChat)
        XCTAssertEqual(old.chatClosedReason, ServiceError.olderMac)
        XCTAssertNil(room.chatClosedReason)
        var removed = room
        removed.state = "removed"
        XCTAssertEqual(removed.chatClosedReason, "You are no longer in this team.")
    }

    func testUpdatesNameTheChatsThatChanged() throws {
        let page = #"{"after": 4, "cursor": 5, "server_time": "2026-11-09T00:00:00Z", "team_messages_changed": ["r1"]}"#
        XCTAssertEqual(try APIJSON.decoder().decode(StateUpdates.self, from: Data(page.utf8)).teamMessagesChanged, ["r1"])
    }

    // MARK: Merge

    func testTheFirstPageFillsTheChatOldestFirst() {
        var chat = TeamChat()
        chat.merge(page([message("a", 1), message("b", 2)], before: nil), requested: nil)
        XCTAssertTrue(chat.loaded)
        XCTAssertEqual(chat.messages.map(\.id), ["a", "b"])
        XCTAssertNil(chat.before)
    }

    /// The newest page again: a new message appears, an edit replaces its message, and a message of the range of the
    /// page that is no longer in it goes. An older message outside the range stays.
    func testTheNewestPageAddsReplacesAndRemovesInItsRange() {
        var chat = TeamChat()
        chat.merge(page([message("c", 30), message("d", 40), message("e", 50)], before: 30), requested: nil)
        chat.merge(page([message("a", 10), message("b", 20)], before: nil), requested: 30)
        XCTAssertEqual(chat.messages.map(\.id), ["a", "b", "c", "d", "e"])
        XCTAssertNil(chat.before)
        var edited = message("d", 40, text: "New text")
        edited.editedAt = "2026-11-10T12:05:00Z"
        // The page covers 40 and up. e went, and f is new.
        chat.merge(page([edited, message("f", 60)], before: 40), requested: nil)
        XCTAssertEqual(chat.messages.map(\.id), ["a", "b", "c", "d", "f"])
        XCTAssertEqual(chat.messages.first { $0.id == "d" }?.text, "New text")
        XCTAssertTrue(chat.messages.first { $0.id == "d" }?.isEdited == true)
    }

    func testAMessageUnderTheRangeOfTheNewestPageStays() {
        var chat = TeamChat()
        chat.merge(page([message("c", 30), message("d", 40), message("e", 50)], before: 30), requested: nil)
        chat.merge(page([message("d", 40), message("e", 50), message("f", 60)], before: 40), requested: nil)
        XCTAssertEqual(chat.messages.map(\.id), ["c", "d", "e", "f"])
        XCTAssertEqual(chat.before, 30, "The chat keeps the before of its older page.")
        // A removal in the range: e goes.
        chat.merge(page([message("d", 40), message("f", 60)], before: 40), requested: nil)
        XCTAssertEqual(chat.messages.map(\.id), ["c", "d", "f"])
    }

    func testAnOlderPageRemovesAMessageOfItsRangeThatWent() {
        var chat = TeamChat()
        chat.merge(page([message("c", 30), message("d", 40)], before: 30), requested: nil)
        chat.messages.insert(message("x", 25), at: 0)
        chat.merge(page([message("a", 10), message("b", 20)], before: 10), requested: 30)
        XCTAssertEqual(chat.messages.map(\.id), ["a", "b", "c", "d"], "x is in the range 10..<30 and the page lacks it")
        XCTAssertEqual(chat.before, 10)
    }

    /// More messages came than one page holds. The page starts the chat again, so no gap stays between the two parts.
    func testANewestPageAfterAGapStartsTheChatAgain() {
        var chat = TeamChat()
        chat.merge(page([message("a", 1), message("b", 2)], before: nil), requested: nil)
        chat.merge(page([message("y", 90), message("z", 91)], before: 90), requested: nil)
        XCTAssertEqual(chat.messages.map(\.id), ["y", "z"])
        XCTAssertEqual(chat.before, 90, "Load earlier messages must read the messages between.")
        var empty = TeamChat()
        empty.merge(page([], before: nil), requested: nil)
        empty.merge(page([message("y", 90)], before: 90), requested: nil)
        XCTAssertEqual(empty.before, 90)
    }

    /// A page that reads a loaded older range again: an edit replaces, a removal goes, and nothing under the oldest
    /// loaded message comes in. The chat keeps its `before`.
    func testAReadOfALoadedRangeReplacesAndRemovesOnlyInThatRange() {
        var chat = TeamChat()
        chat.merge(page([message("e", 50), message("f", 60)], before: 50), requested: nil)
        chat.merge(page([message("b", 20), message("c", 30), message("d", 40)], before: 20), requested: 50)
        XCTAssertEqual(chat.newestFloor, 50)
        XCTAssertTrue(chat.hasOlderPages)
        XCTAssertEqual(chat.before, 20)
        // The Mac has an older message "a" under the loaded range, an edit of "c", and no more "d".
        chat.merge(page([message("a", 10), message("b", 20), message("c", 30, text: "Edited")], before: nil),
                   requested: 50, floor: 20)
        XCTAssertEqual(chat.messages.map(\.id), ["b", "c", "e", "f"])
        XCTAssertEqual(chat.messages[1].text, "Edited")
        XCTAssertEqual(chat.before, 20, "The reread loads nothing new.")
    }

    func testAClosedChatKeepsItsNewestPageOnly() {
        var chat = TeamChat()
        chat.merge(page([message("e", 50), message("f", 60)], before: 50), requested: nil)
        chat.merge(page([message("b", 20), message("c", 30)], before: nil), requested: 50)
        XCTAssertNil(chat.before)
        chat.keepNewestPage()
        XCTAssertEqual(chat.messages.map(\.id), ["e", "f"])
        XCTAssertEqual(chat.before, 50)
        XCTAssertFalse(chat.hasOlderPages)
        // A chat whose newest page has the whole history keeps it.
        var small = TeamChat()
        small.merge(page([message("a", 1)], before: nil), requested: nil)
        small.keepNewestPage()
        XCTAssertEqual(small.messages.map(\.id), ["a"])
    }

    /// A removed message stays removed for a late answer. A page of the Mac that has it again shows it.
    func testARemovedMessageComesBackOnlyWithAPage() {
        var chat = TeamChat()
        chat.merge(page([message("a", 1), message("b", 2)], before: nil), requested: nil)
        chat.remove("b")
        chat.put(message("b", 2, text: "Late edit"))
        XCTAssertEqual(chat.messages.map(\.id), ["a"])
        chat.merge(page([message("a", 1), message("b", 2)], before: nil), requested: nil)
        XCTAssertEqual(chat.messages.map(\.id), ["a", "b"])
        XCTAssertTrue(chat.removed.isEmpty)
    }

    /// A newest page after a gap starts the range again and says so, so a read of the old range stops.
    func testAResetSaysSo() {
        var chat = TeamChat()
        XCTAssertFalse(chat.merge(page([message("a", 1)], before: nil), requested: nil))
        XCTAssertTrue(chat.merge(page([message("y", 90)], before: 90), requested: nil))
        XCTAssertFalse(chat.merge(page([message("y", 90), message("z", 91)], before: 90), requested: nil))
    }

    func testAnEmptyNewestPageEmptiesTheChat() {
        var chat = TeamChat()
        chat.merge(page([message("a", 1)], before: nil), requested: nil)
        chat.merge(page([], before: nil), requested: nil)
        XCTAssertTrue(chat.messages.isEmpty)
        XCTAssertTrue(chat.loaded)
    }

    func testAnAnsweredMessageGoesInByItsPlace() {
        var chat = TeamChat()
        chat.merge(page([message("a", 1), message("c", 3)], before: nil), requested: nil)
        chat.put(message("b", 2))
        chat.put(message("d", 4))
        XCTAssertEqual(chat.messages.map(\.id), ["a", "b", "c", "d"])
        chat.put(message("b", 2, text: "Edited"))
        XCTAssertEqual(chat.messages.map(\.id), ["a", "b", "c", "d"])
        XCTAssertEqual(chat.messages[1].text, "Edited")
        chat.remove("c")
        XCTAssertEqual(chat.messages.map(\.id), ["a", "b", "d"])
    }

    // MARK: Runs

    func testARunIsOneWriterOnOneDayWithNoOtherWriterBetween() {
        var chat = TeamChat()
        chat.merge(page([
            message("1", 1, at: "2026-11-09T09:00:00Z"),
            message("2", 2, at: "2026-11-09T09:01:00Z"),
            message("3", 3, at: "2026-11-10T09:00:00Z"),
            message("4", 4, at: "2026-11-10T09:01:00Z"),
            message("5", 5, at: "2026-11-10T09:02:00Z"),
            message("6", 6, own: true, at: "2026-11-10T09:03:00Z"),
            message("7", 7, bot: "t-r1-b1", at: "2026-11-10T09:04:00Z"),
            message("8", 8, member: "m-edsger", name: nil, at: "2026-11-10T09:05:00Z"),
            message("9", 9, member: "m-edsger", name: nil, at: "2026-11-10T09:06:00Z"),
        ], before: nil), requested: nil)
        chat.pending = [TeamPendingMessage(id: "p", text: "On its way", replyTo: nil, reply: nil,
                                           createdAt: "2026-11-10T09:07:00Z")]
        let rows = TeamChatRow.rows(chat, calendar: Self.utc)
        XCTAssertEqual(rows.map(\.id), ["1", "2", "3", "4", "5", "6", "7", "8", "9", "pending-p"])
        XCTAssertEqual(rows.map(\.startsDay), [true, false, true, false, false, false, false, false, false, false])
        XCTAssertEqual(rows.map(\.startsRun), [true, false, true, false, false, true, true, true, false, true])
        XCTAssertEqual(rows[5].writer, .you)
        XCTAssertEqual(rows[6].writer, .bot("t-r1-b1"))
        XCTAssertEqual(rows[9].writer, .you)
        // A message of a member without a name shows "Former member".
        XCTAssertEqual(rows[7].message.map(TeamNames.author(of:)), "Former member")
        // A removal on its way hides its row.
        chat.deleting = ["4"]
        XCTAssertEqual(TeamChatRow.rows(chat, calendar: Self.utc).map(\.id).contains("4"), false)
    }

    func testASentMessageKeepsItsRowWhenTheMacAnswers() {
        var chat = TeamChat()
        chat.merge(page([message("1", 1, at: "2026-11-10T09:00:00Z")], before: nil), requested: nil)
        chat.pending = [TeamPendingMessage(id: "p", text: "On its way", replyTo: nil, reply: nil,
                                           createdAt: "2026-11-10T09:07:00Z")]
        let before = TeamChatRow.rows(chat, calendar: Self.utc)
        XCTAssertEqual(before.map(\.id), ["1", "pending-p"])
        XCTAssertEqual(before.map(\.listID), ["1", "pending-p"])
        // The Mac answers with a message of its own ID. The row of the message keeps the identity of the row on the
        // way in the list of a Mac. Its ID, which a quote and a scroll use, is the ID of the message.
        chat.confirm("p", as: message("srv", 2, own: true, at: "2026-11-10T09:07:01Z"))
        let after = TeamChatRow.rows(chat, calendar: Self.utc)
        XCTAssertEqual(after.map(\.id), ["1", "srv"])
        XCTAssertEqual(after.map(\.listID), ["1", "pending-p"])
        XCTAssertTrue(chat.pending.isEmpty)
        // A page of the Mac keeps the row of the sent message.
        chat.merge(page([message("1", 1, at: "2026-11-10T09:00:00Z"), message("srv", 2, own: true, at: "2026-11-10T09:07:01Z")],
                        before: nil), requested: nil)
        XCTAssertEqual(TeamChatRow.rows(chat, calendar: Self.utc).map(\.listID), ["1", "pending-p"])
    }

    func testTheAuthorOfAQuote() {
        let grace = TeamMessageQuote(id: "e1", member: "m-grace", memberName: "Grace", text: "Hi")
        XCTAssertEqual(TeamNames.author(of: grace, ownMember: "m-ada"), "Grace")
        XCTAssertEqual(TeamNames.author(of: grace, ownMember: "m-grace"), "You")
        XCTAssertEqual(TeamNames.author(of: TeamMessageQuote(id: "e2", member: "m-x"), ownMember: "m-ada"),
                       "Former member")
        let bot = TeamMessageQuote(id: "e3", member: "m-ada", bot: "t-r1-b1", botName: "Reporter", text: "Done")
        XCTAssertEqual(TeamNames.author(of: bot, ownMember: "m-ada"), "Reporter", "A bot of this Mac is not you.")
    }

    // MARK: The row of the Chats list

    func testThePreviewOfTheRowOfATeam() {
        XCTAssertEqual(TeamChatPreview.text(nil), "No messages yet")
        XCTAssertEqual(TeamChatPreview.text(TeamLastMessage(memberName: "Grace", thisMac: false,
                                                            text: "**Lunch** at [noon](https://x.example)")),
                       "Grace: Lunch at noon")
        XCTAssertEqual(TeamChatPreview.text(TeamLastMessage(memberName: "Ada", thisMac: true, text: "On my way")),
                       "You: On my way")
        // A bot of this Mac writes as itself.
        XCTAssertEqual(TeamChatPreview.text(TeamLastMessage(memberName: "Ada", thisMac: true, bot: "t-r1-b1",
                                                            botName: "Reporter", text: "Done")), "Reporter: Done")
        XCTAssertEqual(TeamChatPreview.text(TeamLastMessage(thisMac: false, text: "Old")), "Former member: Old")
        // An older service does not say whose message it is: the name, never "You".
        XCTAssertEqual(TeamChatPreview.text(TeamLastMessage(memberName: "Ada", text: "On my way")), "Ada: On my way")
    }

    func testTheChatsListPutsTeamsAndBotsInOneOrderOfActivity() {
        func bot(_ id: String, created: String) -> Bot {
            Bot(id: id, sessionId: "session-\(id)", name: id, role: "", avatar: "sparkle", color: "777777",
                instructions: "", status: "idle", lastMessage: "", unreadCount: 0, archived: false, createdAt: created,
                pinned: false)
        }
        func team(_ id: String, created: String, last: String?) -> TeamRoom {
            TeamRoom(room: id, name: id, state: "active", createdAt: created, unread: 0,
                     lastMessage: last.map { TeamLastMessage(text: "Hi", createdAt: $0) })
        }
        let bots = [bot("news", created: "2026-11-01T00:00:00Z"), bot("scout", created: "2026-11-01T00:00:00Z")]
        let latest = ["news": "2026-11-10T12:00:00Z".apiDate!, "scout": "2026-11-08T12:00:00Z".apiDate!]
        let rooms = [
            team("quiet", created: "2026-11-02T00:00:00Z", last: nil),
            team("busy", created: "2026-11-02T00:00:00Z", last: "2026-11-09T12:00:00Z"),
            team("newest", created: "2026-11-11T00:00:00Z", last: nil),
            team("tie", created: "2026-11-01T00:00:00Z", last: "2026-11-10T12:00:00Z"),
        ]
        let items = ChatList.items(bots: bots, latest: latest, rooms: rooms)
        XCTAssertEqual(items.map(\.id), ["team-newest", "bot-news", "team-tie", "team-busy", "bot-scout", "team-quiet"])
        XCTAssertEqual(ChatList.items(bots: [], latest: [:], rooms: []).count, 0)
    }

    func testAReactionIsNoActivityOfABot() {
        let messages = [
            Message(id: "1", botId: "b", role: "user", kind: "text", text: "Hi", createdAt: "2026-11-09T00:00:00Z",
                    streaming: false),
            Message(id: "2", botId: "b", role: "user", kind: "text", text: "", createdAt: "2026-11-10T00:00:00Z",
                    streaming: false, reactionEvent: MessageReactionEvent(emoji: "👍", action: "add")),
        ]
        XCTAssertEqual(ChatList.lastMessageDates(messages)["b"], "2026-11-09T00:00:00Z".apiDate)
    }

    // MARK: Read position and retry

    func testTheAppPostsTheNewestMessageOnceWhileTheChatShowsIt() {
        XCTAssertEqual(TeamReadPosition.toPost(newest: 9, posted: nil, visible: true, atBottom: true), 9)
        XCTAssertEqual(TeamReadPosition.toPost(newest: 9, posted: 4, visible: true, atBottom: true), 9)
        XCTAssertNil(TeamReadPosition.toPost(newest: 9, posted: 9, visible: true, atBottom: true), "Each position once")
        XCTAssertNil(TeamReadPosition.toPost(newest: 9, posted: 12, visible: true, atBottom: true))
        XCTAssertNil(TeamReadPosition.toPost(newest: 9, posted: nil, visible: false, atBottom: true),
                     "A chat that is not on screen sends nothing.")
        XCTAssertNil(TeamReadPosition.toPost(newest: 9, posted: nil, visible: true, atBottom: false),
                     "The reader does not see the newest message.")
        XCTAssertNil(TeamReadPosition.toPost(newest: nil, posted: nil, visible: true, atBottom: true))
    }

    func testARetryKeepsTheKeyOfTheMessage() {
        var pending = TeamPendingMessage(id: UUID().uuidString, text: "Hello", replyTo: nil, reply: nil,
                                         createdAt: "2026-11-10T12:00:00Z")
        let key = pending.idempotencyKey
        XCTAssertNotNil(UUID(uuidString: key), "The Mac takes only a UUID as a key.")
        pending.failed = true
        let retried = pending.retried
        XCTAssertFalse(retried.failed)
        XCTAssertEqual(retried.idempotencyKey, key, "Each try of one message has the same key.")
        XCTAssertEqual(retried.text, "Hello")
    }

    // MARK: Filters, sections, faces, and who is in the chat

    private func bot(_ id: String, pinned: Bool = false) -> Bot {
        Bot(id: id, sessionId: "session-\(id)", name: id, role: "", avatar: "sparkle", color: "777777",
            instructions: "", status: "idle", lastMessage: "", unreadCount: 0, archived: false,
            createdAt: "2026-11-01T00:00:00Z", pinned: pinned)
    }

    private func room(_ id: String, name: String = "Example Team", last: String? = nil) -> TeamRoom {
        TeamRoom(room: id, name: name, state: "active", createdAt: "2026-11-02T00:00:00Z", unread: 0,
                 lastMessage: last.map { TeamLastMessage(text: "Hi", createdAt: $0) })
    }

    func testEachFilterShowsItsRowsInTheOrderOfTheList() {
        let items: [ChatListItem] = [.team(room("r1")), .bot(bot("planner")), .team(room("r2")), .bot(bot("writer"))]
        XCTAssertEqual(ChatFilter.all.items(items).map(\.id), ["team-r1", "bot-planner", "team-r2", "bot-writer"])
        XCTAssertEqual(ChatFilter.people.items(items).map(\.id), ["team-r1", "team-r2"])
        XCTAssertEqual(ChatFilter.bots.items(items).map(\.id), ["bot-planner", "bot-writer"])
        XCTAssertTrue(ChatFilter.all.showsPinned)
        XCTAssertTrue(ChatFilter.bots.showsPinned)
        XCTAssertFalse(ChatFilter.people.showsPinned, "The pinned tiles are bots.")
    }

    /// The chips show only for a Mac in a team. Without a team the list is "All", also for a choice from before.
    func testTheChipsShowForATeamAndTheChoiceFallsBackWithout() {
        XCTAssertFalse(ChatFilter.showsChips(rooms: []))
        XCTAssertTrue(ChatFilter.showsChips(rooms: [room("r1")]))
        XCTAssertEqual(ChatFilter.effective(.bots, rooms: [room("r1")]), .bots)
        XCTAssertEqual(ChatFilter.effective(.people, rooms: []), .all, "A Mac that left its last team is back at All.")
        XCTAssertEqual(ChatFilter.allCases.map(\.title), ["All", "People", "Agents"])
    }

    /// A team that the Mac left keeps its row and its history, but brings no chips. A team that the Mac joins does.
    func testTheChipsShowOnlyForATeamThatTheMacIsIn() {
        func room(_ state: String) -> TeamRoom { TeamRoom(room: state, name: "Example Team", state: state) }
        XCTAssertFalse(ChatFilter.showsChips(rooms: [room("removed")]))
        XCTAssertFalse(ChatFilter.showsChips(rooms: [room("gone")]))
        XCTAssertFalse(ChatFilter.showsChips(rooms: [room("removed"), room("gone")]))
        XCTAssertTrue(ChatFilter.showsChips(rooms: [room("joining")]), "A join on its way: the team shows in People.")
        XCTAssertTrue(ChatFilter.showsChips(rooms: [room("removed"), room("active")]))
        XCTAssertEqual(ChatFilter.effective(.people, rooms: [room("removed")]), .all,
                       "People must not hide the bots of a Mac that left its last team.")
        let items: [ChatListItem] = [.team(room("removed")), .bot(bot("planner"))]
        XCTAssertEqual(ChatFilter.effective(.people, rooms: [room("removed")]).items(items).map(\.id),
                       ["team-removed", "bot-planner"], "The left team stays a row of All.")
    }

    /// The face of a writer comes from the message: the same before and after the writer leaves the team. Only a
    /// writer without a name has the neutral face.
    func testAMessageOfAMemberThatLeftKeepsItsFace() {
        let before = LetterFace.person(memberId: "m-grace", name: "Grace")
        // After the leave, the member list no longer has Grace, but the message keeps her member ID and her name.
        let after = LetterFace.person(memberId: "m-grace", name: "Grace")
        XCTAssertEqual(before.letters, "G")
        XCTAssertEqual(after.letters, before.letters)
        XCTAssertEqual(after.colorIndex, before.colorIndex)
        XCTAssertEqual(after.colorIndex, LetterFace.colorIndex("m-grace"))
        XCTAssertNil(LetterFace.person(memberId: "m-x", name: nil).letters, "Former member: the neutral face.")
    }

    /// A new pairing and an unpair set the choice to All.
    @MainActor
    func testAnUnpairSetsTheFilterToAll() {
        let store = AppStore(demo: true)
        store.chatFilter = .people
        store.disconnect()
        XCTAssertEqual(store.chatFilter, .all)
    }

    func testTheSectionsOfTheMacSidebar() {
        let bots = [bot("planner", pinned: true), bot("writer")]
        let rooms = [room("r1", last: "2026-11-05T00:00:00Z"), room("r2", last: "2026-11-09T00:00:00Z"), room("r3")]
        let sections = ChatSections.make(bots: bots, messages: [], rooms: rooms)
        XCTAssertEqual(sections.pinned.map(\.id), ["planner"])
        XCTAssertEqual(sections.people.map(\.room), ["r2", "r1", "r3"], "Newest activity first.")
        XCTAssertEqual(sections.bots.map(\.id), ["bot-writer"])
    }

    func testTheLettersOfAFace() {
        XCTAssertEqual(LetterFace.letters("Example Team"), "ET")
        XCTAssertEqual(LetterFace.letters("design crew"), "DC")
        XCTAssertEqual(LetterFace.letters("Planner"), "PL")
        XCTAssertEqual(LetterFace.letters("Ada", count: 1), "A")
        XCTAssertEqual(LetterFace.letters("Ada Lovelace", count: 1), "A")
        XCTAssertEqual(LetterFace.letters("  the  big   team "), "TB")
        XCTAssertEqual(LetterFace.letters("2026 Launch"), "LA", "A word without a letter does not count.")
        XCTAssertEqual(LetterFace.letters("Équipe Été"), "ÉÉ")
        XCTAssertNil(LetterFace.letters("🚀 2026"), "A name without a letter shows the symbol.")
        XCTAssertNil(LetterFace.letters(""))
    }

    /// The fill comes from the room ID, so it is the same on each launch and after a new name.
    func testTheFillOfAFaceIsStableForAnID() {
        let index = LetterFace.colorIndex("3f7708d5f5cc2bc633b59d2b3a2ed92e")
        XCTAssertEqual(LetterFace.colorIndex("3f7708d5f5cc2bc633b59d2b3a2ed92e"), index)
        XCTAssertTrue(BoardStyle.faceColors.indices.contains(index))
        // The seed of characterSeed does not change between launches, so the value is fixed.
        XCTAssertEqual(index, Int(characterSeed("3f7708d5f5cc2bc633b59d2b3a2ed92e") % UInt64(BoardStyle.faceColors.count)))
        let ids = (0..<60).map { "room-\($0)" }
        XCTAssertGreaterThan(Set(ids.map(LetterFace.colorIndex)).count, 3, "The rooms spread over the fills.")
        var renamed = room("r1", name: "Example Team")
        let before = LetterFace.colorIndex(renamed.room)
        renamed.name = "Design Crew"
        XCTAssertEqual(LetterFace.colorIndex(renamed.room), before, "A new name keeps the fill.")
    }

    /// Golden values: the fill of a face is FNV-1a (64 bits) over the UTF-8 bytes of the ID, modulo the count of fills.
    /// It does not use `hashValue` or `Hasher`, which Swift seeds anew in each process, so each launch gives the same
    /// fill. The values below come from the same function in Python.
    func testTheFillsOfFixedIDsAreGolden() {
        let teams = ["3f7708d5f5cc2bc633b59d2b3a2ed92e": 6, "r1": 4, "r2": 5, "8b1c9e3a0d4f4a6b9c2e7f1a5b3d6c80": 1]
        let people = ["m-ada": 1, "m-grace": 5, "member-1": 1]
        for (id, index) in teams.merging(people, uniquingKeysWith: { first, _ in first }) {
            XCTAssertEqual(LetterFace.colorIndex(id), index, id)
        }
        XCTAssertEqual(BoardStyle.faceColors.map(\.fill),
                       ["7E99D3", "70A779", "AA79C2", "F2A088", "EAA052", "D76C91", "B6C493", "EBC96C"],
                       "A new fill or a new order changes the face of each team and person.")
        XCTAssertEqual(BoardStyle.faceColors[LetterFace.colorIndex("r1")].fill, "EAA052", "A team")
        XCTAssertEqual(BoardStyle.faceColors[LetterFace.colorIndex("m-grace")].fill, "D76C91", "A person")
    }

    /// A row never has an empty line: a bot without a message says so, as a team does.
    func testARowWithoutAMessageSaysNoMessagesYet() {
        XCTAssertEqual(ChatRowText.line(""), "No messages yet")
        XCTAssertEqual(ChatRowText.line("  \n"), "No messages yet")
        XCTAssertEqual(ChatRowText.line("You: Hi"), "You: Hi")
        XCTAssertEqual(TeamChatPreview.text(nil), ChatRowText.line(""), "One rule for each kind of row.")
        XCTAssertEqual(UnreadBadge.spoken(1), "1 unread message")
        XCTAssertEqual(UnreadBadge.spoken(140), "140 unread messages")
        XCTAssertEqual([5, 42, 99, 100, 140].map(UnreadBadge.text), ["5", "42", "99", "99+", "99+"])
    }

    /// One character gives a circle: no side space. Two and three characters give a capsule with the small space of the
    /// scale at each side.
    func testTheUnreadBadgeIsACircleForOneCharacterAndACapsuleForMore() {
        XCTAssertEqual(UnreadBadge.sidePadding(UnreadBadge.text(3)), 0)
        XCTAssertEqual(UnreadBadge.sidePadding(UnreadBadge.text(66)), BoardStyle.Space.s)
        XCTAssertEqual(UnreadBadge.sidePadding(UnreadBadge.text(140)), BoardStyle.Space.s)
    }

    func testTheLineOfWhoIsInTheChat() {
        func room(people: Int, bots: Int) -> TeamRoom {
            TeamRoom(room: "r", name: "Example Team", state: "active",
                     members: (0..<people).map { TeamMember(member: "m\($0)", name: "P\($0)") },
                     bots: (0..<bots).map { TeamRoomBot(id: "t-r-\($0)", name: "B\($0)") })
        }
        XCTAssertEqual(room(people: 1, bots: 0).whoLine, "1 person")
        XCTAssertEqual(room(people: 3, bots: 0).whoLine, "3 people")
        // The author entries of the bots of people are not in the chat: they have no `open` mark.
        XCTAssertEqual(room(people: 3, bots: 2).whoLine, "3 people")
        XCTAssertNil(room(people: 0, bots: 0).whoLine, "A team that is joining has no members yet.")
    }

    func testAMacWithoutAPersonIsNoPersonInTheLine() throws {
        let json = #"{"room": "r", "name": "Example Team", "state": "active", "members": ["#
            + #"{"member": "m0", "name": "Ada", "bot_only": false}, {"member": "m1", "name": "Grace"}, "#
            + #"{"member": "m2", "name": "Team Mac", "bot_only": true}], "bots": ["#
            + #"{"id": "t-r-1", "name": "Planner", "open": true}, {"id": "t-r-2", "name": "Scribe", "open": true}]}"#
        let room = try APIJSON.decoder().decode(TeamRoom.self, from: Data(json.utf8))
        XCTAssertEqual(room.memberList.map(\.isBotOnly), [false, false, true], "A missing mark is false.")
        XCTAssertEqual(room.whoLine, "2 people, 2 agents")
    }

    func testTheEmptyChatTellsOfTheAtOnlyWhenABotCanAnswer() {
        let open = TeamRoomBot(id: "t-r-1", name: "Planner", open: true)
        let own = TeamRoomBot(id: "t-r-2", name: "Writer", thisMac: true, botId: "bot-writer")
        func room(_ bots: [TeamRoomBot]) -> TeamRoom {
            TeamRoom(room: "r", name: "Example Team", state: "active", bots: bots)
        }
        XCTAssertEqual(TeamChatEmptyLine.text(for: room([open, own])),
                       "Messages here go to each person in Example Team. Type @ to ask an agent.")
        XCTAssertEqual(TeamChatEmptyLine.text(for: room([own])), "Messages here go to each person in Example Team.",
                       "A bot of a person answers no teammate, so the line tells of no @.")
        XCTAssertEqual(TeamChatEmptyLine.text(for: room([])), "Messages here go to each person in Example Team.")
    }

    // MARK: Names of team bots with @

    /// Planner and Reporter answer each member (bots of a Mac without a person). Writer is a bot of this person, and
    /// Helper a bot of Grace: each answers only its own person.
    private var mentionRoom: TeamRoom {
        let planner = TeamRoomBot(id: "t-r1-b1", name: "Planner", avatar: "fruit.blueberry", color: "7E99D3",
                                  memberName: "Team Mac", thisMac: false, open: true)
        let reporter = TeamRoomBot(id: "t-r1-b2", name: "Reporter", avatar: "fruit.kiwi", color: "A68456",
                                   memberName: "Team Mac", thisMac: false, open: true)
        let writer = TeamRoomBot(id: "t-r1-b3", name: "Writer", avatar: "fruit.peach", color: "F2A088",
                                 memberName: "Ada", thisMac: true, botId: "bot-writer")
        let helper = TeamRoomBot(id: "t-r1-b4", name: "Helper", avatar: "fruit.lemon", color: "F1D46B",
                                 memberName: "Grace", thisMac: false, open: false)
        return TeamRoom(room: "r1", name: "Example Team", state: "active",
                        bots: [planner, writer, reporter, helper, planner])
    }

    func testTheListOffersOnlyTheOpenBotsOfTheTeamEachOnce() {
        let bots = TeamMentions.bots(of: mentionRoom)
        XCTAssertEqual(bots.map(\.id), ["t-r1-b1", "t-r1-b2"],
                       "A bot of a person is not in the list, also not a bot of this person.")
        XCTAssertEqual(BotMentions.candidates(in: bots, text: "@", selection: nil, selected: []).map(\.name),
                       ["Planner", "Reporter"])
        XCTAssertEqual(BotMentions.candidates(in: bots, text: "Ask @pl", selection: nil, selected: []).map(\.name),
                       ["Planner"], "Letters filter by the start of the name.")
        XCTAssertEqual(BotMentions.candidates(in: bots, text: "a@", selection: nil, selected: []).count, 0,
                       "The @ must start a word.")
        let alone = TeamRoom(room: "r2", name: "Quiet Team", state: "active")
        XCTAssertTrue(BotMentions.candidates(in: TeamMentions.bots(of: alone), text: "@", selection: nil,
                                             selected: []).isEmpty, "A team without a bot shows no list.")
    }

    func testASendCarriesTheBotsThatTheTextStillNames() {
        let room = mentionRoom
        let text = "@Planner the plan, please"
        let picked = [SelectedBotMention(id: "t-r1-b1", name: "Planner", location: 0, snapshot: text)]
        XCTAssertEqual(TeamMentions.ids(in: text, selected: picked, room: room), ["t-r1-b1"], "A name that stays")

        let changed = "@Planer the plan, please"
        XCTAssertEqual(TeamMentions.ids(in: changed, selected: picked, room: room), [],
                       "A name that the person changed is no mention.")

        let two = "@Planner and @Reporter, the week"
        let both = [SelectedBotMention(id: "t-r1-b1", name: "Planner", location: 0, snapshot: two),
                    SelectedBotMention(id: "t-r1-b2", name: "Reporter", location: 13, snapshot: two)]
        XCTAssertEqual(TeamMentions.ids(in: two, selected: both, room: room), ["t-r1-b1", "t-r1-b2"])

        let twice = "@Planner one, @Planner two"
        XCTAssertEqual(TeamMentions.ids(in: twice, selected: [], room: room), ["t-r1-b1"], "The same bot one time")

        XCTAssertEqual(TeamMentions.ids(in: "No bot here", selected: [], room: room), [])
        XCTAssertEqual(TeamMentions.ids(in: "Text before @Reporter", selected: [], room: room), ["t-r1-b2"])
        XCTAssertEqual(TeamMentions.ids(in: "@Writer and @Helper", selected: [], room: room), [],
                       "A bot that is not open is no mention, also when the text names it.")
    }

    func testASendNamesAtMostSixteenBots() {
        let bots = (1...17).map { TeamRoomBot(id: "t-r1-\($0)", name: "Bot\($0)", open: true) }
        let room = TeamRoom(room: "r1", name: "Example Team", state: "active", bots: bots)
        func ids(_ count: Int) -> [String] {
            TeamMentions.ids(in: (1...count).map { "@Bot\($0)" }.joined(separator: " "), selected: [], room: room)
        }
        XCTAssertEqual(TeamMentions.limit, 16, "The limit of the Mac (MAX_MENTIONS).")
        XCTAssertEqual(ids(16).count, 16)
        XCTAssertTrue(TeamMentions.fits(ids(16)))
        XCTAssertEqual(ids(17).count, 17, "A name typed without a pick counts too.")
        XCTAssertFalse(TeamMentions.fits(ids(17)))
        let twice = TeamMentions.ids(in: "@Bot1 and again @Bot1", selected: [], room: room)
        XCTAssertEqual(twice, ["t-r1-1"], "The same bot named twice counts once.")
        XCTAssertFalse(TeamMentions.fits(ids(16) + ["t-r1-1"]), "The Mac counts each item, also a second copy of an ID.")
        XCTAssertEqual(TeamMentions.overLimit, "Name at most 16 people and agents in one message.")
    }

    @MainActor
    func testASendOverTheLimitSavesNothingAndSaysWhy() {
        let store = AppStore(demo: true)
        let room = TeamRoom(room: "r1", name: "Example Team", state: "active")
        store.team = TeamRooms(rooms: [room], helper: true)
        let mentions = (1...17).map { "t-r1-\($0)" }
        XCTAssertFalse(store.sendTeamMessage("r1", text: "Hello", replyingTo: nil, mentions: mentions))
        XCTAssertEqual(store.teamChats["r1"]?.pending ?? [], [], "Nothing is saved, so nothing waits for a retry.")
        XCTAssertEqual(store.error, "Name at most 16 people and agents in one message.")
        store.error = nil
        XCTAssertTrue(store.sendTeamMessage("r1", text: "Hello", replyingTo: nil, mentions: Array(mentions.prefix(16))))
        XCTAssertNil(store.error)
    }

    func testAnUnsentMessageKeepsItsMentionsInTheSavedFile() throws {
        let pending = TeamPendingMessage(id: "p1", text: "@Planner the plan", replyTo: nil, reply: nil,
                                         createdAt: "2026-11-10T12:00:00Z", mentions: ["t-r1-b1"])
        let saved = try JSONEncoder().encode(pending)
        let restored = try JSONDecoder().decode(TeamPendingMessage.self, from: saved)
        XCTAssertEqual(restored.mentions, ["t-r1-b1"])
        XCTAssertEqual(restored.afterRestart.mentions, ["t-r1-b1"])
        XCTAssertEqual(restored.retried.mentions, ["t-r1-b1"], "The retry keeps the mentions.")

        // A file of an older app has no mentions.
        let older = #"{"id": "p2", "text": "Hello", "createdAt": "2026-11-10T12:00:00Z", "failed": true}"#
        XCTAssertNil(try JSONDecoder().decode(TeamPendingMessage.self, from: Data(older.utf8)).mentions)
    }

    func testABotOfTheListReadsTheMarkOpen() throws {
        let json = #"{"room": "r1", "name": "Example Team", "state": "active", "bots": ["#
            + #"{"id": "t-r1-b1", "name": "Planner", "open": true}, {"id": "t-r1-b2", "name": "Writer", "open": false}, "#
            + #"{"id": "t-r1-b3", "name": "Older"}]}"#
        let room = try APIJSON.decoder().decode(TeamRoom.self, from: Data(json.utf8))
        XCTAssertEqual(room.botList.map(\.isOpen), [true, false, false], "A missing mark is false.")
        XCTAssertEqual(TeamMentions.bots(of: room).map(\.name), ["Planner"])
    }

    // MARK: Typing

    private func typing(_ name: String, bot: String? = nil, seconds: Double = 10) -> TeamTyping {
        TeamTyping(member: "m-" + name.lowercased(), name: bot == nil ? name : "Reporter Mac", bot: bot,
                   botName: bot == nil ? nil : name, seconds: seconds)
    }

    /// The answer of GET /v1/team/rooms with `typing` (backend/coconut/team_typing.py). An item that the app cannot read
    /// drops only that item, a value that is no list gives an empty list, and an older service sends none.
    func testTheListOfWhoTypesDecodes() throws {
        let json = #"{"rooms": [{"room": "r1", "name": "Example Team", "state": "active", "unread": 0, "typing": ["#
            + #"{"member": "m-grace", "name": "Grace", "bot": null, "bot_name": null, "seconds": 10},"#
            + #" {"member": "m-mac", "name": "Reporter Mac", "bot": "t-r1-b1", "bot_name": "Reporter", "seconds": 30},"#
            + #" {"member": 7}]}, {"room": "r2", "name": "Old", "state": "active", "typing": "x"},"#
            + #" {"room": "r3", "name": "Older", "state": "active"}]}"#
        let rooms = try APIJSON.decoder().decode(TeamRooms.self, from: Data(json.utf8)).rooms
        XCTAssertEqual(rooms[0].typing?.items.map(\.shownName), ["Grace", "Reporter"])
        XCTAssertEqual(rooms[0].typing?.items.map(\.seconds), [10, 30])
        XCTAssertEqual(rooms[1].typing?.items, [])
        XCTAssertNil(rooms[2].typing)
        XCTAssertEqual(rooms[2].typingNow(at: .now), [])
        let page = #"{"after": 4, "cursor": 5, "server_time": "2026-11-09T00:00:00Z", "team_typing_changed": ["r1"]}"#
        XCTAssertEqual(try APIJSON.decoder().decode(StateUpdates.self, from: Data(page.utf8)).teamTypingChanged, ["r1"])
    }

    func testTheTextOfWhoTypes() {
        XCTAssertNil(TeamTypingText.text([]))
        XCTAssertEqual(TeamTypingText.text([typing("Grace")]), "Grace is typing")
        XCTAssertEqual(TeamTypingText.text([typing("Grace"), typing("Lin")]), "Grace and Lin are typing")
        XCTAssertEqual(TeamTypingText.text([typing("Grace"), typing("Lin"), typing("Noor")]), "3 people are typing")
        // A bot is one subject with its name.
        XCTAssertEqual(TeamTypingText.text([typing("Reporter", bot: "t-r1-b1")]), "Reporter is typing")
        XCTAssertEqual(TeamTypingText.text([typing("Grace"), typing("Reporter", bot: "t-r1-b1")]),
                       "Grace and Reporter are typing")
        XCTAssertEqual(TeamTypingText.text([typing("Grace"), typing("Lin"), typing("Reporter", bot: "t-r1-b1"),
                                            typing("Noor")]), "4 people are typing")
        // A subject without a name.
        XCTAssertEqual(TeamTypingText.text([TeamTyping(member: "m-x", seconds: 10)]), "Former member is typing")
        XCTAssertEqual(TeamTypingText.text([TeamTyping(member: "m-x", bot: "t-r1-b9", seconds: 30)]), "Agent is typing")
    }

    /// Each subject ends `seconds` after the list came, without a request. A team that is not active shows nobody.
    func testWhoTypesEndsByTheSecondsOfEachSubject() {
        let received = Date(timeIntervalSince1970: 1_000)
        let list = TeamTypingList(items: [typing("Grace", seconds: 3), typing("Reporter", bot: "t-r1-b1", seconds: 30)],
                                  received: received)
        XCTAssertEqual(list.active(at: received).map(\.shownName), ["Grace", "Reporter"])
        XCTAssertEqual(list.active(at: received.addingTimeInterval(2.9)).count, 2)
        XCTAssertEqual(list.active(at: received.addingTimeInterval(3)).map(\.shownName), ["Reporter"])
        XCTAssertEqual(list.active(at: received.addingTimeInterval(30)), [])
        XCTAssertEqual(list.ends, [received.addingTimeInterval(3), received.addingTimeInterval(30)])
        var room = TeamRoom(room: "r1", name: "Example Team", state: "active", unread: 0, typing: list)
        XCTAssertEqual(room.typingNow(at: received).count, 2)
        room.state = "removed"
        XCTAssertEqual(room.typingNow(at: received), [])
        // Two empty lists are the same, so a list without a subject does not draw the Chats list again. A list with a
        // subject has its own ends.
        XCTAssertEqual(TeamTypingList(items: [], received: received), TeamTypingList(items: [], received: .now))
        XCTAssertNotEqual(TeamTypingList(items: [typing("Grace")], received: received),
                          TeamTypingList(items: [typing("Grace")], received: received.addingTimeInterval(4)))
    }

    /// The composer calls the Mac while the draft is not empty, at most one time in 5 seconds.
    func testThePaceOfTheTypingCalls() {
        var pace = TeamTypingPace()
        let start = Date(timeIntervalSince1970: 1_000)
        XCTAssertFalse(pace.take(draft: "", at: start))
        XCTAssertFalse(pace.take(draft: " \n", at: start), "An empty draft calls nothing.")
        XCTAssertTrue(pace.take(draft: "H", at: start))
        XCTAssertFalse(pace.take(draft: "He", at: start.addingTimeInterval(1)))
        XCTAssertFalse(pace.take(draft: "Hello", at: start.addingTimeInterval(4.9)))
        XCTAssertTrue(pace.take(draft: "Hello t", at: start.addingTimeInterval(5)))
        XCTAssertFalse(pace.take(draft: "Hello te", at: start.addingTimeInterval(9)))
        XCTAssertTrue(pace.take(draft: "Hello team", at: start.addingTimeInterval(10.5)))
    }

    /// The second line of the row of a team: who types in place of the newest message, until the seconds end.
    func testTheSecondLineOfTheRowOfATeam() {
        let received = Date(timeIntervalSince1970: 1_000)
        let last = TeamLastMessage(first: 4, member: "m-lin", memberName: "Lin", thisMac: false, text: "Hi",
                                   createdAt: "2026-11-10T12:00:00Z")
        var room = TeamRoom(room: "r1", name: "Example Team", state: "active", unread: 1, lastMessage: last)
        XCTAssertEqual(TeamChatPreview.line(room, at: received), .init(text: "Lin: Hi", typing: false))
        room.typing = TeamTypingList(items: [typing("Grace")], received: received)
        XCTAssertEqual(TeamChatPreview.line(room, at: received), .init(text: "Grace is typing", typing: true))
        XCTAssertEqual(TeamChatPreview.line(room, at: received.addingTimeInterval(10)),
                       .init(text: "Lin: Hi", typing: false))
        room.typing = TeamTypingList(items: [typing("Grace"), typing("Lin")], received: received)
        XCTAssertEqual(TeamChatPreview.line(room, at: received)?.text, "Grace and Lin are typing")
        // A Mac with an older service has no chat, so the row has no second line.
        XCTAssertNil(TeamChatPreview.line(TeamRoom(room: "r2", name: "Old", state: "active"), at: received))
    }

    // MARK: Names of people with @

    /// Example Team of a service that knows a mention of a person: Ada (this person), the people Lin and Grace, the
    /// Mac for a team only Team Mac, and the open bots Reporter and Planner.
    private func peopleRoom(mentioned: Bool? = false, unread: Int = 0, last: TeamLastMessage? = nil) -> TeamRoom {
        TeamRoom(room: "r1", name: "Example Team", state: "active",
                 members: [TeamMember(member: "m-ada", name: "Ada", thisMac: true),
                           TeamMember(member: "m-lin", name: "Lin", avatar: "fruit.kiwi", color: "A68456"),
                           TeamMember(member: "m-mac", name: "Team Mac", botOnly: true),
                           TeamMember(member: "m-grace", name: "Grace")],
                 bots: mentionRoom.botList, unread: unread, lastMessage: last, mentioned: mentioned)
    }

    func testTheListHasTheBotsThenThePeopleEachInTheOrderOfTheNames() {
        let rows = TeamMentions.targets(of: peopleRoom())
        XCTAssertEqual(rows.map(\.name), ["Planner", "Reporter", "Grace", "Lin"],
                       "No Mac for a team only, not this person, no bot that answers no teammate.")
        XCTAssertEqual(rows.map(\.id), ["t-r1-b1", "t-r1-b2", "m-grace", "m-lin"], "A person gives its member ID.")
        XCTAssertEqual(rows.map { $0.mentionedPerson?.member }, [nil, nil, "m-grace", "m-lin"], "A person row has a face.")
        XCTAssertEqual(rows[3].faceSymbol, "fruit.kiwi")
        XCTAssertEqual(TeamMentions.bots(of: peopleRoom()).map(\.name), ["Planner", "Reporter"],
                       "The rule of a bot that a teammate can ask stays.")
    }

    func testAnOlderServiceListsTheBotsOnly() {
        XCTAssertFalse(peopleRoom(mentioned: nil).knowsPersonMentions)
        XCTAssertTrue(peopleRoom(mentioned: false).knowsPersonMentions)
        XCTAssertEqual(TeamMentions.targets(of: peopleRoom(mentioned: nil)).map(\.name), ["Planner", "Reporter"])
        XCTAssertEqual(TeamMentions.ids(in: "@Grace hello", selected: [], room: peopleRoom(mentioned: nil)), [])
    }

    func testTheLettersFilterAPersonAsABot() {
        let rows = TeamMentions.targets(of: peopleRoom())
        XCTAssertEqual(BotMentions.candidates(in: rows, text: "@g", selection: nil, selected: []).map(\.name), ["Grace"])
        XCTAssertEqual(BotMentions.candidates(in: rows, text: "@", selection: nil, selected: []).count, 4)
        XCTAssertEqual(BotMentions.candidates(in: rows, text: "@Ada", selection: nil, selected: []).count, 0,
                       "This person is not in the list.")
    }

    func testASendCarriesTheMemberIDOfANamedPerson() {
        let room = peopleRoom()
        let text = "@Grace and @Planner, the week"
        let picked = [SelectedBotMention(id: "m-grace", name: "Grace", location: 0, snapshot: text),
                      SelectedBotMention(id: "t-r1-b1", name: "Planner", location: 11, snapshot: text)]
        XCTAssertEqual(TeamMentions.ids(in: text, selected: picked, room: room), ["m-grace", "t-r1-b1"])
        XCTAssertEqual(TeamMentions.ids(in: "@Lin, see this. @Lin", selected: [], room: room), ["m-lin"],
                       "A typed name counts, one time.")
        XCTAssertEqual(TeamMentions.ids(in: "@Ada and @Team Mac", selected: [], room: room), [],
                       "Neither this person nor a Mac for a team only is a mention.")
    }

    func testATypedNameGivesTheBotWhenAPersonHasTheSameName() {
        // An older service shows a Mac for a team only as a person with the name of its bot.
        let room = TeamRoom(room: "r1", name: "Example Team", state: "active",
                            members: [TeamMember(member: "m-ada", name: "Ada", thisMac: true),
                                      TeamMember(member: "m-mac", name: "Reporter")],
                            bots: mentionRoom.botList, mentioned: false)
        XCTAssertEqual(TeamMentions.targets(of: room).map(\.id), ["t-r1-b1", "t-r1-b2", "m-mac"])
        XCTAssertEqual(TeamMentions.ids(in: "@Reporter hello", selected: [], room: room), ["t-r1-b2"],
                       "A typed name gives the bot.")
        XCTAssertEqual(TeamMentions.ids(in: "@reporter hello", selected: [], room: room), ["t-r1-b2"])
        let text = "@Reporter hello"
        let person = [SelectedBotMention(id: "m-mac", name: "Reporter", location: 0, snapshot: text)]
        let bot = [SelectedBotMention(id: "t-r1-b2", name: "Reporter", location: 0, snapshot: text)]
        XCTAssertEqual(TeamMentions.ids(in: text, selected: person, room: room), ["m-mac"], "A tap gives its row.")
        XCTAssertEqual(TeamMentions.ids(in: text, selected: bot, room: room), ["t-r1-b2"])
        // A bot and two people with the name: the bot.
        let three = TeamRoom(room: "r1", name: "Example Team", state: "active",
                             members: room.memberList + [TeamMember(member: "m-rep", name: "Reporter")],
                             bots: mentionRoom.botList, mentioned: false)
        XCTAssertEqual(TeamMentions.ids(in: "@Reporter hello", selected: [], room: three), ["t-r1-b2"])
        // A name in another Unicode form (decomposed accents) is the same name, as on the Mac of the bot.
        let accented = TeamRoom(room: "r1", name: "Example Team", state: "active",
                                members: [TeamMember(member: "m-ada", name: "Ada", thisMac: true),
                                          TeamMember(member: "m-mac", name: "Ren\u{E9}e")],
                                bots: [TeamRoomBot(id: "t-r1-b9", name: "Ren\u{E9}e", memberName: "Ren\u{E9}e",
                                                   thisMac: false, open: true)], mentioned: false)
        XCTAssertEqual(TeamMentions.ids(in: "@RENE\u{301}E hello", selected: [], room: accented), ["t-r1-b9"])
        // Two people with the same name stay unclear, as before.
        let twins = TeamRoom(room: "r1", name: "Example Team", state: "active",
                             members: [TeamMember(member: "m-ada", name: "Ada", thisMac: true),
                                       TeamMember(member: "m-lin", name: "Lin"), TeamMember(member: "m-lin2", name: "Lin")],
                             bots: [], mentioned: false)
        XCTAssertEqual(TeamMentions.ids(in: "@Lin hello", selected: [], room: twins), [])
    }

    func testABubbleShowsTheNameOfThisPersonInBold() {
        let room = peopleRoom()
        func message(_ text: String, mentions: [String]?, own: Bool = false) -> TeamMessage {
            TeamMessage(id: "e1", first: 1, member: own ? "m-ada" : "m-grace", memberName: own ? "Ada" : "Grace",
                        thisMac: own, text: text, mentions: mentions, createdAt: "2026-11-12T10:00:00Z")
        }
        let named = message("@Ada, the report is yours. Thanks @ada!", mentions: ["m-ada"])
        XCTAssertTrue(TeamMentionEmphasis.namesMe(named, room: room))
        XCTAssertEqual(TeamMentionEmphasis.text(named.text, message: named, room: room),
                       "**@Ada**, the report is yours. Thanks **@ada**!")
        // Another name, a name inside a word, a message that does not name this person, an own message and code stay.
        let lin = message("@Lin and mail@Ada.com", mentions: ["m-lin", "m-ada"])
        XCTAssertEqual(TeamMentionEmphasis.text(lin.text, message: lin, room: room), "@Lin and mail@Ada.com")
        let other = message("@Ada hello", mentions: ["m-lin"])
        XCTAssertFalse(TeamMentionEmphasis.namesMe(other, room: room))
        XCTAssertEqual(TeamMentionEmphasis.text(other.text, message: other, room: room), "@Ada hello")
        let own = message("@Ada note to self", mentions: ["m-ada"], own: true)
        XCTAssertEqual(TeamMentionEmphasis.text(own.text, message: own, room: room), "@Ada note to self")
        let code = message("@Ada see `x`", mentions: ["m-ada"])
        XCTAssertEqual(TeamMentionEmphasis.text(code.text, message: code, room: room), "@Ada see `x`")
        XCTAssertEqual(TeamMentionEmphasis.spoken, "Names you")
    }

    func testTheRowOfATeamShowsTheMarkWhileAMessageNamesThisPerson() throws {
        let json = #"{"room": "r1", "name": "Example Team", "state": "active", "unread": 2, "mentioned": true, "#
            + #""mention_first": 8, "last_message": {"first": 9, "member": "m-grace", "member_name": "Grace", "text": "Hi"}}"#
        let room = try APIJSON.decoder().decode(TeamRoom.self, from: Data(json.utf8))
        XCTAssertTrue(room.showsMentionMark)
        XCTAssertEqual(room.mentionFirst, 8)
        XCTAssertEqual(TeamRowSpeech.label(room, preview: "Grace: Hi"),
                       "Example Team, Grace: Hi, 2 unread messages, You are named")
        XCTAssertFalse(peopleRoom(mentioned: false, unread: 2).showsMentionMark)
        XCTAssertEqual(TeamRowSpeech.label(peopleRoom(mentioned: false, unread: 2), preview: "Grace: Hi"),
                       "Example Team, Grace: Hi, 2 unread messages")
        let older = #"{"room": "r1", "name": "Example Team", "state": "active", "unread": 2}"#
        let old = try APIJSON.decoder().decode(TeamRoom.self, from: Data(older.utf8))
        XCTAssertFalse(old.showsMentionMark, "An older service sends no mark.")
        XCTAssertNil(old.mentioned)
        XCTAssertNil(old.mentionFirst)
    }

    func testTheFooterOfTheSwitchSaysWhatOffMeans() {
        XCTAssertEqual(TeamNotificationFooter.text(enabled: true, room: peopleRoom()),
                       "Get notified when someone writes in this team.")
        XCTAssertEqual(TeamNotificationFooter.text(enabled: false, room: peopleRoom()),
                       "You get a notification only when someone names you with @.")
        XCTAssertEqual(TeamNotificationFooter.text(enabled: false, room: peopleRoom(mentioned: nil)),
                       "Get notified when someone writes in this team.",
                       "An older service sends nothing for a mention in a quiet team.")
    }

    // MARK: Reactions

    /// Ada (this person), Grace and Lin in "North team".
    private func northTeam() -> TeamRoom {
        TeamRoom(room: "r1", name: "North team", state: "active",
                 members: [TeamMember(member: "m-ada", name: "Ada", thisMac: true),
                           TeamMember(member: "m-grace", name: "Grace"), TeamMember(member: "m-lin", name: "Lin")],
                 unread: 0)
    }

    private func reacted(_ reactions: [TeamReaction]?) -> TeamMessage {
        var message = message("e1", 1)
        message.reactions = reactions
        return message
    }

    /// A message of a Mac with reactions has the list, in the order of the service. A Mac with an older Coconut sends
    /// no key, so the message has none: the app then offers no reaction.
    func testAMessageDecodesWithAndWithoutReactions() throws {
        let answer = #"""
        {"room": "r1", "before": null, "messages": [
          {"id": "e2", "first": 2, "member": "m-grace", "member_name": "Grace", "this_mac": false, "text": "Lunch?",
           "created_at": "2026-11-10T12:00:00Z", "reactions": [
             {"emoji": "👍", "members": ["m-ada"], "mine": true},
             {"emoji": "❤️", "members": ["m-lin", "m-grace"], "mine": false}]},
          {"id": "e1", "first": 1, "member": "m-grace", "member_name": "Grace", "this_mac": false, "text": "Hello",
           "created_at": "2026-11-10T11:00:00Z"}
        ]}
        """#
        let page = try APIJSON.decoder().decode(TeamMessagePage.self, from: Data(answer.utf8))
        XCTAssertEqual(page.messages.map(\.id), ["e2", "e1"])
        XCTAssertEqual(page.messages[0].reactions, [TeamReaction(emoji: "👍", members: ["m-ada"], mine: true),
                                                    TeamReaction(emoji: "❤️", members: ["m-lin", "m-grace"], mine: false)])
        XCTAssertEqual(page.messages[0].ownReaction, "👍")
        XCTAssertNil(page.messages[1].reactions, "No key: a Mac without reactions.")
        XCTAssertNil(page.messages[1].ownReaction)
    }

    /// A tap adds the reaction of this person, a new emoji replaces it, and the same emoji takes it back.
    func testATapAddsReplacesAndRemovesTheOwnReaction() {
        let start = reacted([TeamReaction(emoji: "❤️", members: ["m-grace"], mine: false)])
        XCTAssertEqual(TeamReactions.value(tapping: "👍", on: start), "👍")
        let added = start.reacting("👍", as: "m-ada")
        XCTAssertEqual(added.reactions, [TeamReaction(emoji: "👍", members: ["m-ada"], mine: true),
                                         TeamReaction(emoji: "❤️", members: ["m-grace"], mine: false)],
                       "The list keeps the order of the six emoji.")
        let replaced = added.reacting(TeamReactions.value(tapping: "❤️", on: added), as: "m-ada")
        XCTAssertEqual(replaced.reactions, [TeamReaction(emoji: "❤️", members: ["m-grace", "m-ada"], mine: true)])
        XCTAssertEqual(replaced.ownReaction, "❤️")
        XCTAssertEqual(TeamReactions.value(tapping: "❤️", on: replaced), "", "The same emoji again takes it back.")
        let removed = replaced.reacting("", as: "m-ada")
        XCTAssertEqual(removed.reactions, [TeamReaction(emoji: "❤️", members: ["m-grace"], mine: false)])
        XCTAssertEqual(reacted([]).reacting("👍", as: "m-ada").reacting("", as: "m-ada").reactions, [],
                       "An emoji without a member is not in the list.")
    }

    /// The chat shows a reaction on its way in place of the reaction of the Mac. A page that the app reads meanwhile
    /// does not take it off.
    func testAReactionOnItsWayShowsAlsoAfterAPage() {
        var chat = TeamChat()
        chat.merge(page([reacted([])], before: nil), requested: nil)
        chat.reacting["e1"] = TeamOwnReaction(emoji: "😂", member: "m-ada")
        XCTAssertEqual(TeamChatRow.rows(chat).first?.message?.ownReaction, "😂")
        chat.merge(page([reacted([TeamReaction(emoji: "👍", members: ["m-lin"], mine: false)])], before: nil),
                   requested: nil)
        XCTAssertEqual(TeamChatRow.rows(chat).first?.message?.reactions,
                       [TeamReaction(emoji: "👍", members: ["m-lin"], mine: false),
                        TeamReaction(emoji: "😂", members: ["m-ada"], mine: true)])
        chat.reacting["e1"] = nil
        XCTAssertEqual(TeamChatRow.rows(chat).first?.message?.ownReaction, nil)
    }

    /// The chat offers reactions only on a saved message, where the person can write, from a Mac with reactions, and
    /// not in a direct chat with a bot.
    func testTheChatOffersReactionsOnlyWhereTheyWork() {
        var chat = TeamChat()
        chat.merge(page([reacted([]), { var old = message("e2", 2); old.reactions = nil; return old }()], before: nil),
                   requested: nil)
        chat.pending = [TeamPendingMessage(id: "k1", text: "On its way", replyTo: nil, reply: nil,
                                           createdAt: "2026-11-10T13:00:00Z")]
        let rows = TeamChatRow.rows(chat)
        let saved = rows[0], older = rows[1], pending = rows[2]
        XCTAssertTrue(TeamReactions.offered(on: saved, in: "r1", canWrite: true))
        XCTAssertTrue(TeamReactions.offered(on: saved, in: TeamChatKey(room: "r1", chat: "c1"), canWrite: true))
        XCTAssertTrue(TeamReactions.offered(on: saved, in: TeamChatKey(room: "r1", member: "m-grace"), canWrite: true))
        XCTAssertFalse(TeamReactions.offered(on: saved, in: TeamChatKey(room: "r1", bot: "t-r1-b1"), canWrite: true),
                       "A direct chat with a bot.")
        XCTAssertFalse(TeamReactions.offered(on: saved, in: "r1", canWrite: false), "A chat where the person cannot write.")
        XCTAssertFalse(TeamReactions.offered(on: older, in: "r1", canWrite: true), "A Mac without reactions.")
        XCTAssertFalse(TeamReactions.offered(on: pending, in: "r1", canWrite: true), "A message on its way.")
        chat.deleting.insert("e1")
        XCTAssertFalse(TeamChatRow.rows(chat).contains { $0.id == "e1" }, "A message whose removal runs has no row.")
    }

    /// The answer of a send adds its message in its place when the chat does not have it. A copy that a page brought
    /// stays.
    func testTheAnswerOfASendAddsOnlyAMissingMessage() {
        var chat = TeamChat()
        chat.merge(page([message("e1", 1), message("e3", 3)], before: nil), requested: nil)
        chat.add(message("e2", 2, own: true, text: "Sent"))
        XCTAssertEqual(chat.messages.map(\.id), ["e1", "e2", "e3"])
        var newer = message("e2", 2, own: true, text: "Sent")
        newer.reactions = [TeamReaction(emoji: "👍", members: ["m-grace"], mine: false)]
        chat.merge(page([message("e1", 1), newer, message("e3", 3)], before: nil), requested: nil)
        chat.add(message("e2", 2, own: true, text: "Sent"))
        XCTAssertEqual(chat.messages.first { $0.id == "e2" }?.reactions, newer.reactions)
    }

    /// The pill: each emoji with a member in the fixed order, and one count after them for more than one person.
    func testTheTextOfThePill() throws {
        XCTAssertNil(TeamReactionSummary(nil))
        XCTAssertNil(TeamReactionSummary([]))
        let one = try XCTUnwrap(TeamReactionSummary([TeamReaction(emoji: "😮", members: ["m-lin"])]))
        XCTAssertEqual(one.text, "😮")
        XCTAssertEqual(one.spoken, "Reactions: 1 surprise")
        let many = try XCTUnwrap(TeamReactionSummary([TeamReaction(emoji: "🙏", members: ["m-lin"]),
                                                      TeamReaction(emoji: "❤️", members: ["m-ada"]),
                                                      TeamReaction(emoji: "👍", members: ["m-grace", "m-lin"])]))
        XCTAssertEqual(many.text, "👍❤️🙏 4")
        XCTAssertEqual(many.spoken, "Reactions: 2 thumbs up, 1 heart, 1 thanks")
        XCTAssertEqual(TeamReactionSummary([TeamReaction(emoji: "🎉", members: ["m-lin"])]), nil,
                       "An emoji of a later version shows nothing.")
    }

    /// The list of the people: this person first as "You", then the others in the order of the emoji and of the log.
    /// A member that left reads "Former member".
    func testTheNamesOfTheListOfThePeople() {
        let reactions = [TeamReaction(emoji: "👍", members: ["m-lin", "m-old"]),
                         TeamReaction(emoji: "😢", members: ["m-grace", "m-ada"], mine: true)]
        let people = TeamReactionPerson.list(reactions, room: northTeam())
        XCTAssertEqual(people.map(\.name), ["You", "Lin", TeamNames.formerMember, "Grace"])
        XCTAssertEqual(people.map(\.emoji), ["😢", "👍", "👍", "😢"])
        XCTAssertEqual(people.map(\.own), [true, false, false, false])
        let peer = TeamDirectPeer(member: "m-old", entry: nil, name: "Bea", canReceive: false)
        XCTAssertEqual(TeamReactionPerson.list(reactions, room: northTeam(), peer: peer).map(\.name),
                       ["You", "Lin", "Bea", "Grace"], "A direct chat names its other person.")
    }

    /// VoiceOver and the Mac menu name the action of each emoji.
    func testTheWordsOfTheActions() {
        let mine = reacted([TeamReaction(emoji: "👍", members: ["m-ada"], mine: true)])
        XCTAssertEqual(TeamReactions.action("👍", on: mine), "Remove thumbs up")
        XCTAssertEqual(TeamReactions.action("❤️", on: mine), "React with heart")
        XCTAssertEqual(TeamReactions.all, ["👍", "\u{2764}\u{FE0F}", "😂", "😮", "😢", "🙏"])
    }

    // MARK: Link previews

    private let example = TeamLinkPreview(url: "https://example.com/a", title: "Example page", site: "example.com")

    /// A Mac with previews sends the key: an object, or null. A Mac without previews sends no key. A preview of a wrong
    /// shape, or one that does not fit, is left out, and the message stays.
    func testAMessageDecodesWithNullAndWithoutAPreview() throws {
        let answer = #"""
        {"room": "r1", "before": null, "messages": [
          {"id": "e4", "first": 4, "member": "m-grace", "member_name": "Grace", "this_mac": false,
           "text": "Read https://example.com/a", "created_at": "2026-11-10T12:00:00Z",
           "preview": {"url": "https://example.com/a", "title": "Example page", "site": "example.com"}},
          {"id": "e3", "first": 3, "member": "m-grace", "member_name": "Grace", "this_mac": false, "text": "Hello",
           "created_at": "2026-11-10T11:00:00Z", "preview": null},
          {"id": "e2", "first": 2, "member": "m-grace", "member_name": "Grace", "this_mac": false, "text": "Hi",
           "created_at": "2026-11-10T10:00:00Z"},
          {"id": "e1", "first": 1, "member": "m-grace", "member_name": "Grace", "this_mac": false, "text": "No link",
           "created_at": "2026-11-10T09:00:00Z",
           "preview": {"url": "https://example.com/a", "title": "Example page", "site": "example.com"}},
          {"id": "s1", "first": 0, "member": "m-grace", "text": "Read https://example.com/a",
           "created_at": "2026-11-10T08:00:00Z", "preview": "https://example.com/a"},
          {"id": "s2", "first": 0, "member": "m-grace", "text": "Read https://example.com/a",
           "created_at": "2026-11-10T08:00:00Z", "preview": 7},
          {"id": "s3", "first": 0, "member": "m-grace", "text": "Read https://example.com/a",
           "created_at": "2026-11-10T08:00:00Z", "preview": {"url": "https://example.com/a", "title": "Example page"}}
        ]}
        """#
        let page = try APIJSON.decoder().decode(TeamMessagePage.self, from: Data(answer.utf8))
        XCTAssertEqual(page.messages.map(\.id), ["e4", "e3", "e2", "e1", "s1", "s2", "s3"], "Each message stays.")
        XCTAssertEqual(page.messages[0].preview, example)
        XCTAssertEqual(page.messages.map(\.previewKey), [true, true, false, true, true, true, true])
        XCTAssertNil(page.messages[1].preview, "Null: a Mac with previews, a message without one.")
        XCTAssertNil(page.messages[2].preview, "No key: a Mac without previews.")
        XCTAssertNil(page.messages[3].preview, "The text does not have the address: the preview is left out.")
        XCTAssertEqual(page.messages[4...].map(\.preview), [nil, nil, nil],
                       "A string, a number and an object without a site are left out.")
    }

    /// The composer offers a preview only while the newest message of the chat has the key. A chat without a message
    /// is not known, so it shows no bar.
    func testOnlyAMacWithPreviewsGetsThem() {
        var chat = TeamChat()
        XCTAssertFalse(chat.offersPreviews, "A chat without a message.")
        var newer = message("e2", 2)
        newer.previewKey = true
        chat.merge(page([message("e1", 1), newer], before: nil), requested: nil)
        XCTAssertTrue(chat.offersPreviews)
        chat.merge(page([message("e1", 1), message("e2", 2)], before: nil), requested: nil)
        XCTAssertFalse(chat.offersPreviews, "A Mac without previews.")
    }

    /// The first link of a draft, exactly as the draft has it. A mail address is no link. A first link that is not a
    /// public https page gives no preview, also when a later link is one.
    func testTheFirstLinkOfADraft() throws {
        XCTAssertNil(TeamDraftLink.first(in: "No link here"))
        let link = try XCTUnwrap(TeamDraftLink.first(in: "Mail ada@example.com, read https://example.com/a?b=1. And https://example.org"))
        XCTAssertEqual(link.text, "https://example.com/a?b=1")
        XCTAssertEqual(link.url.absoluteString, "https://example.com/a?b=1")
        XCTAssertNil(TeamDraftLink.first(in: "http://192.168.1.4/a then https://example.com/a"))
        XCTAssertNil(TeamDraftLink.first(in: "example.com without a scheme"))
    }

    /// The page loads only after the pause, and a link that changes in the pause loads nothing.
    @MainActor
    func testThePageLoadsAfterThePauseOnly() async throws {
        let link = try XCTUnwrap(TeamDraftLink.first(in: "Read https://example.com/a"))
        let asked = AskedPages()
        let stub: @MainActor @Sendable (URL) async -> LinkPreview? = { url in
            asked.urls.append(url)
            return LinkPreview(title: "Example\n  page ", site: "example.com", image: nil)
        }
        let cancelled = Task { @MainActor in await TeamComposerPreview.load(link, pause: .seconds(5), loader: stub) }
        try await Task.sleep(for: .milliseconds(50))
        cancelled.cancel()
        let none = await cancelled.value
        XCTAssertNil(none)
        XCTAssertEqual(asked.urls, [], "A link that changes in the pause loads nothing.")
        XCTAssertEqual(TeamComposerPreview.pause, .milliseconds(600))
        let loaded = await TeamComposerPreview.load(link, pause: .milliseconds(10), loader: stub)
        XCTAssertEqual(asked.urls.map(\.absoluteString), ["https://example.com/a"])
        XCTAssertEqual(loaded, TeamLinkPreview(url: "https://example.com/a", title: "Example page", site: "example.com"),
                       "The title has one line.")
    }

    /// A new first link drops the old preview at once. A late page of the old link shows nothing. A removed preview
    /// stays removed for its link, and comes back after the link changed. A send clears the preview.
    func testTheComposerKeepsThePreviewOfTheFirstLinkOnly() {
        var composer = TeamComposerState()
        composer.draft = "Read https://example.com/a"
        composer.linkChanged(to: "https://example.com/a")
        composer.previewLoaded(example, for: "https://example.com/a")
        XCTAssertEqual(composer.linkPreview, example)

        composer.draft = "Read https://example.org/b"
        composer.linkChanged(to: "https://example.org/b")
        XCTAssertNil(composer.linkPreview, "A new link drops the old bar at once.")
        composer.previewLoaded(example, for: "https://example.com/a")
        XCTAssertNil(composer.linkPreview, "A late page of the old link shows nothing.")

        composer.draft = "Read https://example.com/a"
        composer.linkChanged(to: "https://example.com/a")
        composer.previewLoaded(example, for: "https://example.com/a")
        composer.removePreview()
        XCTAssertNil(composer.linkPreview)
        composer.draft = "Read https://example.com/a again"
        composer.linkChanged(to: "https://example.com/a")
        composer.previewLoaded(example, for: "https://example.com/a")
        XCTAssertNil(composer.linkPreview, "A removed preview stays removed for that link.")

        composer.draft = "Read"
        composer.linkChanged(to: nil)
        composer.draft = "Read https://example.com/a"
        composer.linkChanged(to: "https://example.com/a")
        composer.previewLoaded(example, for: "https://example.com/a")
        XCTAssertEqual(composer.linkPreview, example, "After the link changed, the preview comes back.")

        composer.sent()
        XCTAssertNil(composer.linkPreview)
        XCTAssertNil(composer.removedLink)
        composer.draft = "Read https://example.com/a"
        composer.edit(message("e1", 1, own: true, text: "Read https://example.com/a"))
        composer.previewLoaded(example, for: "https://example.com/a")
        XCTAssertNil(composer.linkPreview, "An edit shows no preview.")
    }

    /// The limits of the Mac, for each value as sent: the title and the site are trimmed, then cut to 200 and 100
    /// Unicode scalars. The address must be in the text exactly and have at most 2,000. A line break or an empty value
    /// sends no preview.
    func testThePreviewFitsTheLimitsOfTheMac() {
        let text = "Read https://example.com/a"
        XCTAssertEqual(TeamLinkPreview(url: "https://example.com/a", title: "  Example page ", site: " example.com")
            .fitting(in: text), example)
        func with(url: String = "https://example.com/a", title: String = "Example page", site: String = "example.com",
                  in body: String = text) -> TeamLinkPreview? {
            TeamLinkPreview(url: url, title: title, site: site).fitting(in: body)
        }
        XCTAssertEqual(with(title: String(repeating: "é", count: 200))?.title, String(repeating: "é", count: 200))
        XCTAssertEqual(with(title: "  " + String(repeating: "a", count: 201) + " ")?.title, String(repeating: "a", count: 200),
                       "A long title is trimmed, then cut to 200.")
        XCTAssertEqual(with(title: String(repeating: "e\u{301}", count: 101))?.title,
                       String(repeating: "e\u{301}", count: 100), "The Mac counts Unicode scalars, and a cut keeps whole characters.")
        XCTAssertEqual(with(title: String(repeating: "a", count: 199) + " b")?.title, String(repeating: "a", count: 199),
                       "A space at the cut goes.")
        XCTAssertEqual(with(site: String(repeating: "s", count: 101))?.site, String(repeating: "s", count: 100))
        XCTAssertNil(with(title: " "))
        XCTAssertNil(with(site: ""))
        XCTAssertNil(with(title: "Example\npage"))
        XCTAssertNil(with(url: "https://example.com/b"), "The text does not have the address.")
        XCTAssertNil(with(url: "ftp://example.com/a", in: "ftp://example.com/a"))
        let path = String(repeating: "p", count: 2000 - "https://example.com/".count)
        XCTAssertNotNil(with(url: "https://example.com/" + path, in: "https://example.com/" + path))
        XCTAssertNil(with(url: "https://example.com/" + path + "p", in: "https://example.com/" + path + "p"))
    }

    /// The bubble shows the preview of a message of a person and of a message that waits. A message of a team agent
    /// shows none.
    func testAnAgentMessageShowsNoPreview() {
        var chat = TeamChat()
        var grace = message("e1", 1, text: "Read https://example.com/a")
        grace.preview = example
        var agent = message("e2", 2, bot: "t-r1-b1", text: "Read https://example.com/a")
        agent.preview = example
        chat.merge(page([grace, agent], before: nil), requested: nil)
        chat.pending = [TeamPendingMessage(id: "k1", text: "Read https://example.com/a", replyTo: nil, reply: nil,
                                           createdAt: "2026-11-10T13:00:00Z", preview: example)]
        XCTAssertEqual(TeamChatRow.rows(chat).map(\.linkPreview), [example, nil, example])
    }

    /// The answer of an edit sets the preview of the Mac: an edit can drop it.
    func testTheAnswerOfAnEditSetsThePreview() {
        var chat = TeamChat()
        var sent = message("e1", 1, own: true, text: "Read https://example.com/a")
        sent.preview = example
        chat.merge(page([sent], before: nil), requested: nil)
        var edited = message("e1", 1, own: true, text: "Read it later")
        edited.editedAt = "2026-11-10T13:00:00Z"
        chat.putEdit(edited)
        XCTAssertNil(chat.messages.first?.preview)
        XCTAssertEqual(chat.messages.first?.text, "Read it later")
    }

    /// VoiceOver reads the card as one element, and only a web address opens.
    func testTheCardSaysWhatItIs() {
        XCTAssertEqual(example.spoken, "Link preview: Example page, example.com")
        XCTAssertEqual(example.address?.absoluteString, "https://example.com/a")
        XCTAssertNil(TeamLinkPreview(url: "javascript:alert(1)", title: "t", site: "s").address)
    }
}

/// The pages that a stub loader was asked for.
@MainActor
private final class AskedPages {
    var urls: [URL] = []
}
