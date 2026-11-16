import XCTest
@testable import Coconut

/// The pure rules of the named chats of a team (docs/team-log.md, "Chats of a team"): the decode, the rows of the list
/// and their order, the title and the words for VoiceOver, who makes and changes a chat, the route of a notification
/// and the banner of the Mac.
final class TeamNamedChatTests: XCTestCase {
    private let room = String(repeating: "a", count: 64)
    private let design = String(repeating: "d", count: 32)
    private let launch = String(repeating: "e", count: 32)

    private func rooms(admin: Bool = true) throws -> TeamRooms {
        let json = """
        {"rooms": [{"room": "\(room)", "name": "Example Team", "state": "active", "created_at": "2026-11-01T09:00:00Z",
          "unread": 0, "mentioned": false, "typing": [], "notifications": true, "can_manage_chats": \(admin),
          "last_message": {"first": 4, "member": "m-lin", "member_name": "Lin", "text": "Thanks",
                           "created_at": "2026-11-12T08:00:00Z"},
          "chats": [
            {"id": "\(design)", "name": "Design", "created_at": "2026-11-10T09:00:00Z", "unread": 2, "mentioned": true,
             "mention_first": 9, "typing": [],
             "last_message": {"first": 9, "member": "m-grace", "member_name": "Grace", "text": "@Ada the logo",
                              "created_at": "2026-11-12T10:00:00Z"}},
            {"id": "\(launch)", "name": "Launch", "created_at": "2026-11-11T09:00:00Z", "unread": 0,
             "mentioned": false, "typing": [], "last_message": null}]}]}
        """
        return try APIJSON.decoder().decode(TeamRooms.self, from: Data(json.utf8))
    }

    func testARoomDecodesItsChatsAndAnOlderServiceHasNone() throws {
        let room = try XCTUnwrap(try rooms().rooms.first)
        XCTAssertEqual(room.namedChats.map(\.name), ["Design", "Launch"])
        XCTAssertEqual(room.canManageChats, true)
        let old = try APIJSON.decoder().decode(TeamRooms.self, from: Data(#"{"rooms": [{"room": "r", "name": "T", "state": "active"}]}"#.utf8))
        XCTAssertEqual(old.rooms[0].namedChats, [])
        XCTAssertFalse(TeamChatManagement.showsSection(old.rooms[0]))
        XCTAssertEqual(ChatList.people(old.rooms).count, 1)
    }

    func testAChatShowsTheFieldsOfItsChatAndKeepsTheTeam() throws {
        let room = try XCTUnwrap(try rooms().rooms.first)
        let shown = try XCTUnwrap(room.inChat(design))
        XCTAssertEqual(shown.title, "Design")
        XCTAssertEqual(shown.name, "Example Team")
        XCTAssertEqual(shown.chatAndTeam, "Design · Example Team")
        XCTAssertEqual(shown.unread, 2)
        XCTAssertEqual(shown.showsMentionMark, true)
        XCTAssertEqual(shown.chatKey, TeamChatKey(room: self.room, chat: design))
        XCTAssertEqual(room.inChat(nil), room)
        XCTAssertNil(room.inChat(String(repeating: "f", count: 32)), "A chat that went shows nothing.")
        XCTAssertEqual(room.chatAndTeam, "Example Team")
    }

    func testTheRowsOfTheListAndTheirOrder() throws {
        let items = ChatList.people(try rooms().rooms)
        // Design has the newest message, then the Team chat, then Launch without a message, by the time of the chat.
        XCTAssertEqual(items.map(\.id), ["team-\(room)-\(design)", "team-\(room)", "team-\(room)-\(launch)"])
        XCTAssertEqual(items.map(\.chat), [TeamChatKey(room: room, chat: design), TeamChatKey(room: room),
                                           TeamChatKey(room: room, chat: launch)])
        guard case .team(let empty) = items[2] else { return XCTFail("No row of Launch.") }
        XCTAssertEqual(TeamChatPreview.line(empty, at: .now)?.text, TeamChatPreview.empty)
    }

    func testTheWordsOfVoiceOverNameTheChatAndTheTeam() throws {
        let room = try XCTUnwrap(try rooms().rooms.first)
        let shown = try XCTUnwrap(room.inChat(design))
        XCTAssertEqual(TeamRowSpeech.label(shown, preview: "Grace: @Ada the logo"),
                       "Design, Example Team, Grace: @Ada the logo, \(UnreadBadge.spoken(2)), You are named")
        XCTAssertEqual(TeamTitle.spoken(shown), "Design, Example Team")
        XCTAssertTrue(TeamTitle.spoken(room).hasPrefix("Example Team team"))
    }

    func testOnlyTheAdminOfAnActiveTeamMakesAndChangesAChat() throws {
        let admin = try XCTUnwrap(try rooms().rooms.first)
        XCTAssertTrue(TeamChatManagement.allows(admin))
        let member = try XCTUnwrap(try rooms(admin: false).rooms.first)
        XCTAssertFalse(TeamChatManagement.allows(member))
        XCTAssertTrue(TeamChatManagement.showsSection(member), "A member sees the rows without the button.")
        var gone = admin
        gone.state = "gone"
        XCTAssertFalse(TeamChatManagement.allows(gone))
        var none = member
        none.chats = []
        XCTAssertFalse(TeamChatManagement.showsSection(none), "No section for a member of a team without a chat.")
        XCTAssertEqual(TeamChatManagement.deleteTitle, "Delete this chat for everyone?")
        XCTAssertEqual(TeamChatManagement.deleteMessage, "Its messages go from the app of each member.")
    }

    func testANotificationOfANamedChatOpensThatChat() throws {
        let device = UUID().uuidString
        let one = try XCTUnwrap(NotificationTarget(userInfo: ["coconut": [
            "version": 1, "device_id": device, "kind": "team", "team": room, "chat": design]]))
        XCTAssertEqual(one.chat, TeamChatKey(room: room, chat: design))
        XCTAssertEqual(NotificationDestination(chat: try XCTUnwrap(one.chat)), .chat(TeamChatKey(room: room, chat: design)))
        let group = try XCTUnwrap(NotificationTarget(userInfo: ["coconut": [
            "version": 1, "device_id": device, "kind": "group", "count": 2, "team": room, "chat": design]]))
        XCTAssertEqual(group.chat, TeamChatKey(room: room, chat: design))
        // A value that is not the ID of an entry opens the Team chat, as an older Mac does.
        let odd = try XCTUnwrap(NotificationTarget(userInfo: ["coconut": [
            "version": 1, "device_id": device, "kind": "team", "team": room, "chat": "Design"]]))
        XCTAssertEqual(odd.chat, TeamChatKey(room: room))
        XCTAssertEqual(NotificationDestination(chat: TeamChatKey(room: room)), .team(room))
    }

    /// The one rule of a chat that goes while it is open: the iPhone closes the chat view, and the Mac sidebar selects
    /// the Inbox. A direct chat stays while its team stays.
    func testTheListHasAChatWhileItsTeamAndItsNamedChatShow() throws {
        var list = try rooms()
        XCTAssertTrue(list.has(TeamChatKey(room: room, chat: design)))
        XCTAssertTrue(list.has(TeamChatKey(room: room)))
        XCTAssertTrue(list.has(TeamChatKey(room: room, member: "m-left")))
        list.rooms[0].chats = []
        XCTAssertFalse(list.has(TeamChatKey(room: room, chat: design)), "The admin deleted the chat.")
        XCTAssertTrue(list.has(TeamChatKey(room: room)))
        list.rooms = []
        XCTAssertFalse(list.has(TeamChatKey(room: room)), "The team goes.")
        XCTAssertFalse(list.has(TeamChatKey(room: room, member: "m-left")))
    }

    func testTheRequestsOfANamedChatNameIt() {
        XCTAssertEqual(TeamChatKey(room: room, chat: design).path, "team/rooms/\(room)")
        XCTAssertEqual(TeamChatKey(room: room, chat: design).query, "&chat=\(design)")
        XCTAssertEqual(TeamChatKey(room: room).query, "")
    }

    func testTheMacBannerOfANamedChat() throws {
        var before = try rooms()
        before.rooms[0].chats?[0].lastMessage?.first = 8
        before.rooms[0].chats?[0].unread = 1
        before.rooms[0].chats?[0].mentionFirst = 8
        let plan = MessageBanners.plan(before: before, after: try rooms())
        let key = TeamChatKey(room: room, chat: design)
        XCTAssertEqual(plan.post, [MessageBanner(chat: key, title: "Grace", subtitle: "Design · Example Team",
                                                 body: "@Ada the logo")])
        XCTAssertEqual(MessageBanners.thread(key), room + ":" + design)
        XCTAssertEqual(MessageBanners.chat(userInfo: MessageBanners.userInfo(key)), key)
        XCTAssertEqual(MessageBanners.shownChat(key, rooms: try rooms()), key)
        var without = try rooms()
        without.rooms[0].chats = []
        XCTAssertNil(MessageBanners.shownChat(key, rooms: without), "A deleted chat opens nothing.")
    }

    // MARK: Mute

    func testAChatDecodesItsChoiceAndAnOlderServiceSendsNone() throws {
        let json = """
        {"rooms": [{"room": "\(room)", "name": "Example Team", "state": "active", "notifications": true,
          "chats": [{"id": "\(design)", "name": "Design", "notifications": false},
                    {"id": "\(launch)", "name": "Launch", "notifications": true}]}]}
        """
        let decoded = try APIJSON.decoder().decode(TeamRooms.self, from: Data(json.utf8))
        XCTAssertEqual(decoded.rooms[0].namedChats.map(\.notifications), [false, true])
        // The fixture of the other tests is an older service: its chats have no `notifications`.
        XCTAssertEqual(try rooms().rooms[0].namedChats.map(\.notifications), [nil, nil])
    }

    func testEachMemberOfAnActiveTeamHasMuteOrUnmute() throws {
        for admin in [true, false] {
            var room = try XCTUnwrap(try rooms(admin: admin).rooms.first)
            XCTAssertNil(TeamChatMute.action(room.namedChats[0], in: room), "An older service shows no action.")
            room.chats?[0].notifications = true
            XCTAssertEqual(TeamChatMute.action(room.namedChats[0], in: room), "Mute")
            XCTAssertEqual(TeamChatMute.symbol(room.namedChats[0]), "bell.slash")
            room.chats?[0].notifications = false
            XCTAssertEqual(TeamChatMute.action(room.namedChats[0], in: room), "Unmute")
            XCTAssertEqual(TeamChatMute.symbol(room.namedChats[0]), "bell")
            room.state = "gone"
            XCTAssertNil(TeamChatMute.action(room.namedChats[0], in: room), "A team that went has no action.")
        }
    }

    func testOnlyAMutedChatHasTheMark() throws {
        var chat = try XCTUnwrap(try rooms().rooms.first?.namedChats.first)
        XCTAssertFalse(TeamChatMute.showsMark(chat))
        chat.notifications = true
        XCTAssertFalse(TeamChatMute.showsMark(chat))
        chat.notifications = false
        XCTAssertTrue(TeamChatMute.showsMark(chat))
        XCTAssertEqual(TeamChatMute.mark, "bell.slash")
    }

    /// Chat A waits while chat B starts and ends: the choice of A stays until its own request ends, and only the
    /// action of a chat that waits is off.
    func testEachChatKeepsItsOwnChoiceAndOnlyItsActionWaits() throws {
        var room = try XCTUnwrap(try rooms().rooms.first)
        room.chats?[0].notifications = true
        room.chats?[1].notifications = true
        var choices = [design: false, launch: false]
        XCTAssertEqual(TeamChatMute.applying(choices, to: room).namedChats.map(\.notifications), [false, false])
        XCTAssertTrue(TeamChatMute.waits(design, choices))
        XCTAssertTrue(TeamChatMute.waits(launch, choices))
        // The request of Launch ends, and its answer is in the list. Design still waits and still shows its choice.
        choices[launch] = nil
        room.chats?[1].notifications = false
        XCTAssertEqual(TeamChatMute.applying(choices, to: room).namedChats.map(\.notifications), [false, false])
        XCTAssertTrue(TeamChatMute.waits(design, choices))
        XCTAssertFalse(TeamChatMute.waits(launch, choices))
        // The answer of Design changes only Design in the list as it is now.
        let answered = TeamChatMute.applying([design: false], to: room)
        XCTAssertEqual(answered.namedChats.map(\.notifications), [false, false])
        XCTAssertEqual(answered.notifications, room.notifications, "The switch of the team does not change.")
    }

    func testOnlyTheRowOfANamedChatInTheListHasMuteInItsMenu() throws {
        var room = try XCTUnwrap(try rooms().rooms.first)
        let design = try XCTUnwrap(room.inChat(self.design))
        XCTAssertNil(TeamChatMute.listAction(.team(design)), "An older service shows no item.")
        room.chats?[0].notifications = true
        XCTAssertEqual(TeamChatMute.listAction(.team(try XCTUnwrap(room.inChat(self.design)))),
                       TeamChatMute.ListAction(room: self.room, chat: self.design, label: "Mute", symbol: "bell.slash",
                                               enabled: false))
        room.chats?[0].notifications = false
        XCTAssertEqual(TeamChatMute.listAction(.team(try XCTUnwrap(room.inChat(self.design)))),
                       TeamChatMute.ListAction(room: self.room, chat: self.design, label: "Unmute", symbol: "bell",
                                               enabled: true))
        XCTAssertNil(TeamChatMute.listAction(.team(room)), "The Team chat has no item.")
        XCTAssertNil(TeamChatMute.listAction(.direct(room, TeamDirect(member: "m-grace"))), "A direct chat has none.")
        XCTAssertNil(TeamChatMute.listAction(.botDirect(room, TeamBotDirect(bot: "t-r-e"))), "An agent has none.")
        room.state = "gone"
        XCTAssertNil(TeamChatMute.listAction(.team(try XCTUnwrap(room.inChat(self.design)))),
                     "A team that went has no item.")
    }

    func testTheFooterOfTheSwitchNamesAMutedChat() throws {
        var room = try XCTUnwrap(try rooms().rooms.first)
        XCTAssertEqual(TeamNotificationFooter.text(enabled: true, room: room), TeamNotificationFooter.on)
        room.chats?[1].notifications = true
        XCTAssertEqual(TeamNotificationFooter.text(enabled: true, room: room), TeamNotificationFooter.on)
        room.chats?[1].notifications = false
        XCTAssertEqual(TeamNotificationFooter.text(enabled: true, room: room),
                       "Get notified when someone writes in this team. A muted chat notifies only when someone names "
                       + "you with @.")
        // With the switch off the text stays as before.
        XCTAssertEqual(TeamNotificationFooter.text(enabled: false, room: room), TeamNotificationFooter.off)
        room.mentioned = nil
        XCTAssertEqual(TeamNotificationFooter.text(enabled: false, room: room), TeamNotificationFooter.on)
    }

    func testAChoiceShowsAtOnceAndTheRoomOfTheMacShowsWhenItEnds() throws {
        var room = try XCTUnwrap(try rooms().rooms.first)
        XCTAssertEqual(TeamChatMute.applying([design: false], to: room), room,
                       "An older service has no choice, so a choice changes nothing.")
        room.chats?[0].notifications = true
        room.chats?[1].notifications = true
        let shown = TeamChatMute.applying([design: false], to: room)
        XCTAssertEqual(shown.namedChats.map(\.notifications), [false, true])
        XCTAssertEqual(TeamChatMute.action(shown.namedChats[0], in: shown), "Unmute")
        XCTAssertTrue(TeamChatMute.showsMark(shown.namedChats[0]))
        XCTAssertEqual(TeamChatMute.applying([:], to: room), room, "After the request the room of the Mac shows.")
    }

    func testTheRowOfAMutedChatInTheListHasTheMarkAndSaysIt() throws {
        var room = try XCTUnwrap(try rooms().rooms.first)
        room.chats?[0].notifications = false
        let muted = try XCTUnwrap(room.inChat(design))
        XCTAssertTrue(muted.shownChatMuted)
        XCTAssertFalse(try XCTUnwrap(room.inChat(launch)).shownChatMuted)
        XCTAssertFalse(room.shownChatMuted, "The Team chat has no mute.")
        XCTAssertEqual(TeamRowSpeech.label(muted, preview: "Grace: @Ada the logo"),
                       "Design, Example Team, Grace: @Ada the logo, \(UnreadBadge.spoken(2)), You are named, Muted")
    }

    func testAMutedChatKeepsItsCountAndMakesABannerOnlyForAMention() throws {
        let before = try rooms()
        // A plain message after the mention: the newest message and the count grow, and `mention_first` stays.
        var after = try rooms()
        after.rooms[0].chats?[0].lastMessage?.first = 10
        after.rooms[0].chats?[0].lastMessage?.text = "A plain note"
        after.rooms[0].chats?[0].unread = 3
        after.rooms[0].chats?[0].notifications = true
        XCTAssertEqual(MessageBanners.plan(before: before, after: after).post.count, 1)
        after.rooms[0].chats?[0].notifications = false
        XCTAssertEqual(after.rooms[0].inChat(design)?.unread, 3, "A muted chat counts each unread message.")
        XCTAssertFalse(try XCTUnwrap(after.rooms[0].inChat(design)).chatNotifies)
        XCTAssertEqual(MessageBanners.plan(before: before, after: after).post, [])
        // A new mention of this person makes a banner also in a muted chat.
        after.rooms[0].chats?[0].mentionFirst = 10
        XCTAssertEqual(MessageBanners.plan(before: before, after: after).post.map(\.body), ["A plain note"])
        // The switch of the team off makes each chat quiet, also a chat that is not muted.
        after.rooms[0].chats?[0].mentionFirst = 9
        after.rooms[0].chats?[0].notifications = true
        after.rooms[0].notifications = false
        XCTAssertEqual(MessageBanners.plan(before: before, after: after).post, [])
    }

    // MARK: Pin

    /// The fixture of a person's Mac of today: the room and each chat have `pinned`.
    private func pinnedRooms(team: Bool, design: Bool, launch: Bool = false) throws -> TeamRooms {
        var list = try rooms()
        list.rooms[0].pinned = team
        list.rooms[0].chats?[0].pinned = design
        list.rooms[0].chats?[1].pinned = launch
        return list
    }

    /// The one rule of the choices that wait: a new pin and a new mute show at once in the list, and the list of the
    /// Mac shows again when the requests end. VoiceOver says "Starred" and "Muted".
    func testTheChoicesThatWaitShowAtOnceAndVoiceOverSaysThem() throws {
        var list = try pinnedRooms(team: false, design: false)
        list.rooms[0].chats?[0].notifications = true
        let shown = TeamChatChoices.applying(mutes: [design: false], pins: [design: true, room: true],
                                             to: list.rooms)
        XCTAssertEqual(shown[0].pinned, true)
        XCTAssertEqual(shown[0].namedChats[0].pinned, true)
        XCTAssertEqual(shown[0].namedChats[0].notifications, false)
        XCTAssertEqual(ChatList.pinnedChats(shown).map(\.id), ["team-\(room)", "team-\(room)-\(design)"])
        XCTAssertEqual(TeamChatChoices.applying(mutes: [:], pins: [:], to: list.rooms), list.rooms)
        // An older service has no choice, so a choice changes nothing.
        let old = try rooms().rooms
        XCTAssertEqual(TeamChatChoices.applying(mutes: [design: false], pins: [design: true], to: old), old)
        let row = try XCTUnwrap(shown[0].inChat(design))
        XCTAssertTrue(TeamRowSpeech.label(row, preview: nil).hasSuffix("You are named, Muted, Starred"))
        XCTAssertFalse(TeamRowSpeech.label(try XCTUnwrap(list.rooms[0].inChat(design)), preview: nil)
            .contains("Starred"))
    }

    func testARoomDecodesItsPinsAndAnOlderServiceSendsNone() throws {
        let json = """
        {"rooms": [{"room": "\(room)", "name": "Example Team", "state": "active", "pinned": true,
          "chats": [{"id": "\(design)", "name": "Design", "pinned": false, "notifications": true}]}]}
        """
        let decoded = try APIJSON.decoder().decode(TeamRooms.self, from: Data(json.utf8)).rooms[0]
        XCTAssertEqual(decoded.pinned, true)
        XCTAssertEqual(decoded.namedChats.map(\.pinned), [false])
        XCTAssertTrue(decoded.chatPinned)
        XCTAssertEqual(decoded.inChat(design)?.chatPinned, false, "A named chat has its own pin.")
        let old = try XCTUnwrap(try rooms().rooms.first)
        XCTAssertNil(old.chatPinChoice)
        XCTAssertNil(try XCTUnwrap(old.inChat(design)).chatPinChoice)
    }

    func testTheMenuOfAChatOfATeamHasPinAndItsLabel() throws {
        let old = try XCTUnwrap(try rooms().rooms.first)
        XCTAssertNil(TeamChatPin.listAction(.team(old)), "An older service shows no item.")
        let room = try pinnedRooms(team: false, design: true).rooms[0]
        XCTAssertEqual(TeamChatPin.listAction(.team(room)),
                       TeamChatPin.ListAction(room: self.room, chat: nil, label: "Star", symbol: "star", pinned: true))
        XCTAssertEqual(TeamChatPin.listAction(.team(try XCTUnwrap(room.inChat(design)))),
                       TeamChatPin.ListAction(room: self.room, chat: design, label: "Unstar", symbol: "star.slash",
                                              pinned: false))
        // The Team chat has no mute, so its menu has Pin only.
        XCTAssertNil(TeamChatMute.listAction(.team(room)))
        XCTAssertNil(TeamChatPin.listAction(.direct(room, TeamDirect(member: "m-grace"))), "A direct chat has none.")
        XCTAssertNil(TeamChatPin.listAction(.botDirect(room, TeamBotDirect(bot: "t-r-e"))), "An agent has none.")
        // A team that is not active: "Star" goes, "Unstar" stays, so a pinned chat can leave the top of the list.
        for state in ["gone", "removed"] {
            var left = room
            left.state = state
            XCTAssertNil(TeamChatPin.listAction(.team(left)), "\(state): no Pin")
            XCTAssertEqual(TeamChatPin.listAction(.team(try XCTUnwrap(left.inChat(design))))?.label, "Unstar")
            XCTAssertEqual(TeamChatMenuItem.items(.team(try XCTUnwrap(left.inChat(design)))).map(\.label), ["Unstar"],
                           "\(state): no Mute either")
            XCTAssertEqual(ChatList.pinnedChats([left]).map(\.id), ["team-\(self.room)-\(design)"])
        }
        // The menu has "Star" above "Mute" for a named chat, and "Star" only for the Team chat.
        var both = room
        both.chats?[1].notifications = true
        XCTAssertEqual(TeamChatMenuItem.items(.team(try XCTUnwrap(both.inChat(launch)))).map(\.label), ["Star", "Mute"])
        XCTAssertEqual(TeamChatMenuItem.items(.team(both)).map(\.label), ["Star"])
        XCTAssertEqual(TeamChatMenuItem.items(.direct(both, TeamDirect(member: "m-grace"))), [])
        XCTAssertEqual(TeamChatPin.key(room: self.room, chat: nil), self.room)
        XCTAssertEqual(TeamChatPin.key(room: self.room, chat: design), design)
        XCTAssertTrue(TeamChatPin.waits(design, [design: true]))
        XCTAssertFalse(TeamChatPin.waits(self.room, [design: true]))
    }

    func testAPinnedChatLeavesItsListAndShowsWithThePinnedInAFixedOrder() throws {
        let list = try pinnedRooms(team: true, design: false, launch: true)
        // The Team chat first, then the named chats in the order of the Mac, whatever their newest message.
        XCTAssertEqual(ChatList.pinnedChats(list.rooms).map(\.id), ["team-\(room)", "team-\(room)-\(launch)"])
        XCTAssertEqual(ChatList.people(list.rooms).map(\.id), ["team-\(room)-\(design)"])
        XCTAssertEqual(ChatList.items(bots: [], latest: [:], rooms: list.rooms).map(\.id), ["team-\(room)-\(design)"])
        let sections = ChatSections.make(bots: [], messages: [], rooms: list.rooms)
        XCTAssertEqual(sections.pinnedChats.map(\.id), ["team-\(room)", "team-\(room)-\(launch)"])
        XCTAssertEqual(sections.people, [])
        XCTAssertEqual(sections.unread.map(\.id), ["team-\(room)-\(design)"])
        XCTAssertTrue(sections.pinnedChats.allSatisfy(\.isPinnedChat))
        // Without a pin, nothing moves.
        let none = try pinnedRooms(team: false, design: false)
        XCTAssertEqual(ChatList.pinnedChats(none.rooms), [])
        XCTAssertEqual(ChatList.people(none.rooms).map(\.id), ChatList.people(try rooms().rooms).map(\.id))
        // The rows of the iPhone under the tiles of the pinned agents: the pinned chats first, the Team chat first, then
        // the other rows newest activity first. "All" and "People" have the pinned chats, "Agents" has none.
        let bot = Bot(id: "b1", sessionId: "s1", name: "Reporter", role: "", avatar: "", color: "", instructions: "",
                      status: "idle", lastMessage: "", unreadCount: 0, archived: false,
                      createdAt: "2026-11-12T11:00:00Z")
        let pinnedIds = ["team-\(room)", "team-\(room)-\(launch)"]
        XCTAssertEqual(ChatList.rows(.all, bots: [bot], latest: [:], rooms: list.rooms).map(\.id),
                       pinnedIds + ["bot-b1", "team-\(room)-\(design)"])
        XCTAssertEqual(ChatList.rows(.people, bots: [bot], latest: [:], rooms: list.rooms).map(\.id),
                       pinnedIds + ["team-\(room)-\(design)"])
        XCTAssertEqual(ChatList.rows(.bots, bots: [bot], latest: [:], rooms: list.rooms).map(\.id), ["bot-b1"])
        // Two teams: the teams in the order of the Mac, and in each the Team chat first.
        var other = list.rooms[0]
        other.room = String(repeating: "b", count: 64)
        other.name = "Second Team"
        let two = ChatList.pinnedChats([other, list.rooms[0]]).map(\.id)
        XCTAssertEqual(two, ["team-\(other.room)", "team-\(other.room)-\(launch)"] + pinnedIds)
        // The Mac section "Starred" with a pinned chat and no pinned agent.
        let onlyChat = try pinnedRooms(team: false, design: true)
        let macSections = ChatSections.make(bots: [bot], messages: [], rooms: onlyChat.rooms)
        XCTAssertEqual(macSections.pinned, [])
        XCTAssertEqual(macSections.pinnedChats.map(\.id), ["team-\(room)-\(design)"])
        XCTAssertEqual(macSections.pinnedChats.first?.chat, TeamChatKey(room: room, chat: design),
                       "The row keeps the key of its chat, so the selection of the sidebar stays on it.")
        XCTAssertTrue(ChatFilter.all.showsPinnedChats)
        XCTAssertTrue(ChatFilter.people.showsPinnedChats)
        XCTAssertFalse(ChatFilter.bots.showsPinnedChats)
        XCTAssertFalse(ChatFilter.people.showsPinned)
    }

    func testUnreadKeepsUnreadStarredChatsAndExcludesReadOrMutedStarredChats() throws {
        let list = try pinnedRooms(team: true, design: true, launch: true)
        XCTAssertEqual(ChatList.rows(.unread, bots: [], latest: [:], rooms: list.rooms).map(\.id),
                       ["team-\(room)-\(design)"])
        var muted = list.rooms
        let chat = try XCTUnwrap(muted[0].chats?.firstIndex { $0.id == design })
        muted[0].chats?[chat].notifications = false
        XCTAssertTrue(ChatList.rows(.unread, bots: [], latest: [:], rooms: muted).isEmpty)
    }

    func testAStarredUnreadChatStaysInStarredUntilThePersonRemovesItsStar() throws {
        let list = try pinnedRooms(team: false, design: true)
        let id = "team-\(room)-\(design)"
        let starred = ChatSections.make(bots: [], messages: [], rooms: list.rooms)
        XCTAssertEqual(starred.pinnedChats.map(\.id), [id])
        XCTAssertEqual(starred.pinnedChats.first?.unreadCount, 2)
        XCTAssertFalse((starred.unread + starred.people + starred.bots).contains { $0.id == id })

        let unstarred = TeamChatChoices.applying(mutes: [:], pins: [design: false], to: list.rooms)
        let unread = ChatSections.make(bots: [], messages: [], rooms: unstarred)
        XCTAssertTrue(unread.pinnedChats.isEmpty)
        XCTAssertEqual(unread.unread.map(\.id), [id])
        XCTAssertFalse(unread.people.contains { $0.id == id })

        var readRooms = unstarred
        readRooms[0].chats?[0].unread = 0
        let read = ChatSections.make(bots: [], messages: [], rooms: readRooms)
        XCTAssertTrue(read.unread.isEmpty)
        XCTAssertEqual(read.people.filter { $0.id == id }.count, 1)
    }
}
