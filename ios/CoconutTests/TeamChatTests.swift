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
        XCTAssertEqual(ChatFilter.allCases.map(\.title), ["All", "People", "Bots"])
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
        XCTAssertEqual(sections.bots.map(\.id), ["writer"])
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

    func testTheLineOfWhoIsInTheChat() {
        func room(people: Int, bots: Int) -> TeamRoom {
            TeamRoom(room: "r", name: "Example Team", state: "active",
                     members: (0..<people).map { TeamMember(member: "m\($0)", name: "P\($0)") },
                     bots: (0..<bots).map { TeamRoomBot(id: "t-r-\($0)", name: "B\($0)") })
        }
        XCTAssertEqual(room(people: 1, bots: 0).whoLine, "1 person")
        XCTAssertEqual(room(people: 3, bots: 0).whoLine, "3 people")
        XCTAssertEqual(room(people: 3, bots: 1).whoLine, "3 people, 1 bot")
        XCTAssertEqual(room(people: 3, bots: 2).whoLine, "3 people, 2 bots")
        XCTAssertNil(room(people: 0, bots: 0).whoLine, "A team that is joining has no members yet.")
    }
}
